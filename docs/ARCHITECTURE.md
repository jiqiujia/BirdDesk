# BirdDesk 系统架构与设计说明

| 项目 | 内容 |
|---|---|
| 版本 | 0.8.0（… / P5 连拍升格视频 / P6 多选与删除 已完成） |
| 日期 | 2026-11-01 |
| 运行环境 | macOS 12+，Apple Silicon（M1 Pro 32GB 实测），Python 3.13 |
| 安装位置 | 任意目录（`git clone` 后 `./setup.sh`），入口 `BirdDesk.app` |
| 代码规模 | Python 约 3.4k 行，前端约 2.2k 行（无框架）；本地模型 1.8GB |

---

## 1. 产品定位与边界

**定位**：面向拍鸟场景的本地照片整理与选片工具。覆盖从整理到出片的完整链路——按日期/连拍分组、判断合焦、定位鸟眼、按分数挑选、识别鸟种、分区出片（降噪 / 主体增强 / 背景处理）。

**设计红线**（贯穿所有模块）：

1. **本地优先**：模型全部本地推理，推理期间零网络请求（测试中断言 DNS/socket 不可用仍能完成）。
2. **原片只读**：任何分析、缩略图、评级都不修改 RAW/JPG 像素。
3. **移动可撤销**：文件移动先出计划、逐个复核、逐条落盘日志，支持一键还原。
4. **不夸大能力**：不确定就显示“候选/未确认”，并明确哪些结果没有校准、不参与自动淘汰。

**当前不做**：云端同步、多人协作、视频、图片编辑（裁剪/调色）、Windows/Linux 适配。

---

## 2. 总体架构

单进程、双执行体：Python 后端 + 内嵌 WebView 前端，走本机 HTTP。

```
┌──────────────────────────────────────────────────────────────┐
│ BirdDesk.app  (Contents/MacOS/BirdDesk 启动脚本)              │
│                                                              │
│  ┌────────────────────────┐      ┌────────────────────────┐  │
│  │ pywebview 窗口 (WKWebView) │◄────►│ FastAPI + uvicorn      │  │
│  │ index.html / app.js      │ HTTP │ 127.0.0.1:<随机端口>    │  │
│  │ style.css                │      │                        │  │
│  └────────────────────────┘      └──────────┬─────────────┘  │
│         ▲ 原生目录选择框（pywebview 注入）      │                │
│         └──────────────────────────────────┘                │
│                                              │                │
│   ┌──────────────┬──────────────┬────────────┴───────────┐    │
│   │ 元数据层      │ 分析层        │ 文件操作层              │    │
│   │ ExifTool     │ YOLO-seg     │ organizer + journal    │    │
│   │ SQLite 缓存   │ YOLO-pose    │ 预览/执行/撤销          │    │
│   │              │ BioCLIP 2    │                        │    │
│   │              │ OpenCV 指标   │                        │    │
│   └──────────────┴──────────────┴────────────────────────┘    │
│                                                              │
│  本地磁盘：~/Pictures/...（用户照片，只读）                      │
│           ~/Library/Application Support/BirdDesk（缓存/日志）    │
└──────────────────────────────────────────────────────────────┘
```

**为什么不用纯桌面 GUI（Qt/Tk）**：界面是照片网格 + 大图 + 覆层，Web 技术在这块迭代最快；pywebview 只提供窗口和原生文件对话框，业务逻辑全在 Python。代价是要自建 HTTP 层，但换来界面可独立调试（`--no-open` 起服务，用浏览器打开）。

**为什么用随机端口**：避免端口冲突；副作用是浏览器 localStorage 不跨启动保留，所以所有偏好都存在服务端 `settings.json`。

### 2.1 进程与线程模型

```
主线程             uvicorn（守护线程）        Analyzer 线程       解码线程池(3)
  │                    │                        │                  │
  │ webview.start()    ├─ 处理 HTTP             ├─ 逐张取图 ────────►│ load_rgb()
  │  (阻塞到关窗)       │  ├─ 只读接口（并发）    │  YOLO 检测        │ （JPEG draft 降采样）
  │                    │  └─ 写接口（加锁）      │  OpenCV 指标       │
  │                    │                        │  眼睛模型×2        │
  │                    │                        └─ 批量写 SQLite(20) │
```

- **分析串行**：模型推理固定在单线程，避免 MPS 上下文竞争；图片解码用 3 线程预取（提前 4 张）。
- **锁粒度**：`Library.lock`（RLock）保护照片表、分析结果、评级、分组；文件移动期间先 `analyzer.stop()`。
- **停止语义**：`stop()` 若 60 秒内线程未退出会抛错，**不会**清空线程引用后再启动第二个模型使用者（这是 P2.1 修掉的并发缺陷）。

### 2.2 启动流程

```
main.py
 ├─ 选空闲端口 → 后台线程起 uvicorn
 ├─ 轮询 /api/status 就绪
 ├─ 注入 native_pick_folder（pywebview 原生对话框）
 └─ webview.start() → 前端 init()
                      ├─ GET /api/env      （ExifTool / 模型 / 原生能力探测）
                      ├─ GET /api/library  （含上次目录的分组结果）
                      └─ setInterval(poll, 1200)  （增量更新）
```

---

## 3. 目录结构

```
<仓库根目录>/
├── BirdDesk.app/                 # 打包产物（make_app.sh 生成）
├── app/
│   ├── main.py                   # 入口：端口、uvicorn、pywebview、原生对话框
│   ├── server.py                 # FastAPI 路由 + Library 状态 + 任务编排（最大文件）
│   ├── scanner.py                # 目录遍历，RAW/JPG/XMP 归并成“照片”
│   ├── metadata.py               # ExifTool 批量读取 + SQLite 元数据缓存
│   ├── grouping.py               # 日期 + 连拍分组（纯计算）
│   ├── organizer.py              # 整理计划 / 移动 / 日志 / 撤销
│   ├── analyzer.py               # 分析流水线（检测 → 合焦 → 鸟眼）
│   ├── focus.py                  # 合焦指标（纯 OpenCV/numpy）
│   ├── eyes.py                   # 鸟眼定位与瞳孔校验
│   ├── species.py                # 鸟种识别（BioCLIP 2 + 中国鸟类名录）
│   ├── denoise.py                # AI 降噪（NAFNet，分块 + 余弦窗融合）
│   ├── develop.py                # 出片流水线（主体蒙版 / 分区增强 / 导出）
│   ├── raw_io.py                 # RAW 16 位解码（sRGB / 线性）+ 16bit TIFF 落盘
│   ├── rawdenoise.py             # RAW 域降噪（噪声模型 + 自适应导向滤波）
│   ├── video.py                  # 连拍 → 升格视频（重复帧 + H.264 编码）
│   ├── trash.py                  # 删除到系统废纸篓 + 可撤销日志
│   ├── ratings.py                # 星级/淘汰 → XMP 旁注
│   ├── thumbs.py                 # 缩略图/大图（RAW 内嵌 JPEG）
│   └── config.py                 # 路径常量、扩展名集合、默认设置
├── web/                          # 纯静态前端
│   ├── index.html                # 骨架与弹窗
│   ├── app.js                    # 全部交互逻辑（含内置自测）
│   └── style.css                 # 亮色主题
├── models/                       # 本地权重
│   ├── yolo11m-seg.pt            # 45MB，COCO 鸟类分割
│   ├── eye_pose_v0.pt            # 5.4MB，鸟类 6 关键点
│   ├── bioclip2/                 # 1.7GB，ViT-L/14 图文模型（含分词器）
│   ├── nafnet/                   # 117MB，SIDD 降噪权重
│   └── eye_pose_v0.provenance.json  # 固定 revision + sha256 + 许可说明
├── data/
│   ├── birds_cn.json             # 1508 种：学名 + 中文名 + 英文名
│   └── birds_cn.provenance.json  # 数据来源与 CC BY-NC 许可说明
├── docs/ARCHITECTURE.md          # 本文档
├── vendor/exiftool/              # ExifTool 13.60（纯 Perl，随包分发）
├── vendor/nafnet/                # 官方 NAFNet 结构（Apache-2.0，已内联 basicsr 依赖）
├── tests/
└── make_app.sh                   # 重建 .app（含图标生成）
```

---

## 4. 数据模型

### 4.1 Photo（内存对象，`scanner.Photo`）

一张“照片”= 同目录同名的一簇文件（`DSC001.ARW` + `DSC001.JPG` + `DSC001.xmp`）。

| 字段 | 说明 |
|---|---|
| `id` | `sha1(主文件名|大小|mtime_ns)[:16]`，**移动后不变**，是全系统缓存键 |
| `files` / `primary` / `display` | 全部文件 / 读元数据用（优先 RAW）/ 出图用（优先 JPG） |
| `dt`, `has_subsec` | 拍摄时间（相机本地时间）及是否含毫秒 |
| `meta` | ExifTool 标签字典（机型、镜头、快门、对焦点…） |
| `day`, `burst`, `gap_prev` | 分组结果（每次重新分组覆盖） |

### 4.2 分析结果（`analyzer.analyze_image` 输出，JSON 入 SQLite）

```jsonc
{
  "v": 306, "w": 4096, "h": 2731, "src": "jpg|raw-preview", "ms": 620,
  "birds": [{"box": [x1,y1,x2,y2], "conf": 0.94, "area": 0.31, "edge": false}],
  "main": 0,
  "focus": {
    "grade": "sharp|soft|blur|missed|motion", "score": 96,
    "sharp": 0.94, "peak": 1.09, "body": 0.71, "bg": 0.42,
    "anis": 0.78, "energy": 21.3, "noise": 0.8,
    "peak_xy": [0.52, 0.34], "tiles": 118
  },
  "eye": {
    "status": "detected|uncertain|not_detected|too_small|unavailable|error|no_bird",
    "reason": "……", "model": "eye-pose-v0", "version": 2, "selected": 0,
    "eyes": [{
      "side": "left|right", "x": 0.51, "y": 0.31,
      "box": [x1,y1,x2,y2], "confidence": 0.89, "stable": true,
      "refined": true, "pose_xy": [0.51, 0.31], "agreement_px": 6.5,
      "sharpness": {"value": 0.42, "usable": true, "reason": "未校准……"}
    }]
  },
  "af": {"x": 0.50, "y": 0.30, "on_bird": true},
  "species": {
    "status": "ok|low_confidence|uncertain|too_small|unavailable|error|no_bird|disabled",
    "model": "bioclip-2", "index_version": "ioc-15.1+wikidata-zh-2026-05-06",
    "p1": 0.94, "margin": 0.72, "ms": 98,
    "reason": "较可能的候选（样本上约九成可靠），仍建议核对",
    "top": [{"latin": "Upupa epops", "zh": "戴胜", "en": "Eurasian Hoopoe", "p": 0.94}]
  }
}
```

**坐标约定**：所有坐标都是“已按 EXIF 方向旋正后的整图归一化坐标”，前端直接按百分比定位，不做二次变换。唯一的例外是索尼 `FocusLocation`（原始未旋转像素坐标），在 `analyzer.af_point()` 里换算。

### 4.3 分组结果（`grouping.Grouping`）

- `Day`：日期、按时间排序的 items（`single` 或 `burst` 引用）、照片数
- `Burst`：`id = "YYYY-MM-DD/burst_HHMMSS[_n]"`、成员 id、起止时间、机身
- 无拍摄时间的照片归入 `undated`，整理时进 `unknown-date/`

### 4.4 整理计划与日志

计划（`plans[plan_id]`，内存）：`moves[]`（src/dst/renamed/src_rel/dst_rel）、`conflicts[]`、`summary`（含 `cross_device`、`enough_space`）、绑定的 `version`。

日志（`journals/<时间>-<plan4>.jsonl`，逐行 fsync）：

```
{"type":"header", ...}
{"type":"mkdir","path":"..."}          # 为撤销记录新建的目录
{"type":"move","src":"...","dst":"..."}
{"type":"rmdir","path":"..."}          # 清理的空目录
{"type":"error","message":"...","at":123}
{"type":"end","status":"ok","moved":53}
```

撤销成功后另写 `<id>.undo.json`（还原数、跳过原因），并据此禁止二次撤销。

### 4.5 设置（`settings.json`）

| 键 | 默认 | 作用 |
|---|---|---|
| `gap` | 0.5 | 连拍判定阈值（秒） |
| `min_burst` | 2 | 最少几张算一组 |
| `split_by_camera` | true | 多机身分别判定，防止混组 |
| `recursive` | true | 是否扫描子文件夹 |
| `rename_conflicts` | true | 同名自动加 `_2` |
| `cleanup_empty` | true | 移动后删空目录 |
| `strictness` | normal | 合焦严格度（宽松/标准/严格） |
| `sort` | time | 显示排序（不影响分组） |
| `auto_analyze` | true | 扫描后自动分析 |
| `reject_to_folder` | false | 淘汰照片放 `_淘汰/` |
| `last_root` | "" | 上次目录 |

---

## 5. 核心算法与设计决策

### 5.1 照片归并（scanner）

同一目录下按“逻辑主名”合并：`DSC001.ARW`、`DSC001.JPG`、`DSC001.ARW.xmp`、`DSC001.xmp` 归为一张。跳过隐藏目录（含 `.birddesk`、`.Trashes`）和隐藏文件。视频等非图片只计数不处理。

### 5.2 连拍判定（grouping）

- 排序键：机身 → 时间 → 文件名；按机身分别扫描，避免两台相机混组。
- 相邻两张间隔 ≤ `gap` 归入同一组；**缺少毫秒时至少放宽到 1 秒**（老机身 EXIF 精度不足）。
- 少于 `min_burst` 张的“组”退化为单拍。
- 命名取组内首张的时分秒；同秒多组加 `_2`、`_3`。
- 跨午夜的连拍整组归到开始那天。

### 5.3 合焦评估（focus）

不用模型，纯频域/分块统计。实测约 270ms/张（1920×1920、鸟占约半个画面），是当前分析流水线里最耗时的一步。

1. 取鸟框外扩 50% 区域，把鸟的长边缩到 ≤1024（不放大），切成 ~1/22 鸟长的小块。
2. 每块算 **acutance = 高频能量 / 中频能量**（高频=与 σ1 高斯之差，中频=σ1 与 σ3 之差），与亮度/对比度无关。
3. **噪声底数据驱动**：各频段取小块能量的 5 分位作为底噪并扣除——真实相机噪声经去马赛克/JPEG/缩放后空间相关，白噪声模型会严重低估。
4. **方向性**：0°/45°/90°/135° 四个方向各算一遍，取“最弱方向”作为锐度。失焦各向同性，运动模糊只抹掉运动方向，因此一个指标同时覆盖两者，再用各向异性区分。
5. 定级（阈值可整体缩放，`strictness`：宽松 0.85 / 标准 1.0 / 严格 1.18）：

| 条件 | 等级 |
|---|---|
| 最锐区最弱方向 ≥ 0.52 | 合焦 |
| 周边背景 > 自身 × 1.3 且 ≥ 阈值 | 跑焦 |
| 方向一致性 < 0.6 | 运动模糊 |
| ≥ 0.30 | 偏软 |
| 其余 | 失焦 |

6. **原始指标入库、定级在读时计算**：改严格度不需要重新分析，只重算标签。

### 5.4 鸟眼定位（eyes）

四道关，任何一关不过只降级为“候选”，不报错、不影响合焦结论：

1. **模型**：`eye-pose-v0`（YOLO11n-pose，6 关键点：喙、左眼、右眼、头顶、双肩）。固定 revision + 启动时校验 sha256，不匹配拒绝加载（避免加载来路不明的 `.pt`）。
2. **双视角一致性**：原图与水平翻转图各推理一次，左右眼在翻转后按 `flip_idx` 交换，两点距离 ≤ 鸟长 2.2% 才算一致。
3. **瞳孔校验**：在点位附近搜索圆形暗区（Hough + 连通域圆度），要求内部更暗（对比 ≥35 灰度）、环带更亮、16 个方向中 ≥80% 方向都有内暗外亮过渡。**只有通过这一步才标“已识别”**。
4. **归属校验**：点位须在鸟体分割掩膜内、在鸟框外扩范围内、不贴画面边缘；鸟长边 <96px 直接放弃（不靠插值硬造）。

主体关联用 IoU≥0.3 且“IoU×置信度”最大，只定位主鸟，避免把旁边的鸟认成主体。

### 5.5 连拍择优（server.rank_value）

```
v = sharp / (T_SHARP × 严格度系数)        # 主项：最锐区最弱方向锐度
  + 0.05 × min(高频能量/50, 1)            # 同分时偏好细节更足
  + 0.05 × min(主体面积/0.1, 1)
  - 0.15 × 贴边
  - 0.08 × 对焦点不在鸟身
```

**鸟眼结果不参与排序**（眼区锐度未校准），避免误定位把好片排到最后。

### 5.6 鸟种识别（species）

零样本，不训练，只做“从 1508 个候选里排序 + 明确弃权”。

1. **类别表**：`data/birds_cn.json`（1508 种）来自中国鸟类名录映射表，含学名 / 中文名 / 英文名。
   限定名录而不是全生物界，能显著减少“名录外最近邻”这种荒谬答案。
2. **文本嵌入**：每种用 3 条模板（学名 / 英文名 / “a bird of the species …”），各自 L2 归一化后取均值，
   一次性算完缓存成 `cache/species_emb_*.npz`（首次 23 秒，之后 0 秒加载）。
3. **图像侧**：只取**主鸟**方框外扩 18% 的方形裁切 → BioCLIP 预处理（短边 224、bicubic、CLIP mean/std）。
   不整图分类，因为背景会主导 CLIP 相似度。
4. **打分**：`softmax(logit_scale × cos(img, text))`，取 Top-5，同时给出 top1−top2 的差距。
5. **弃权而非硬答**（阈值按 33 张带标签公开图标定）：

| top1 概率 | 状态 | 含义 |
|---|---|---|
| ≥ 0.60 | `ok` | 较可能（该档实测 top-1 89%） |
| 0.20–0.60 | `low_confidence` | 候选分不开，人工从 Top-5 里挑 |
| < 0.20 | `uncertain` | 不建议采信 |
| 鸟长边 < 64px | `too_small` | 直接不判断 |

6. **人工确认**：点击候选即写 XMP `dc:Subject`（中文名 + 学名），并记入 `species_confirm` 表；
   连拍按帧投票（人工确认权重 ×2），在连拍面板显示“鸟种：戴胜（4/5 帧）”。

### 5.7 出片流水线（develop / denoise）

按社区通行的后期顺序串起来，每一步都可单独关掉：

| 步骤 | 做法 | 默认参数（标准） |
|---|---|---|
| 1. AI 降噪 | NAFNet-SIDD width32，256² 分块、32px 重叠、Hann 窗融合；MPS 加速 | 开 |
| 2. 主体蒙版 | YOLO 分割多边形 → 导向滤波吸附真实边缘 → 轻腐蚀内缩 → 羽化 | 覆盖率约 10% |
| 3. 主体增强 | 纹理（σ=1.2 反锐化 1.6×amount）、清晰度（σ=12 中频 0.55×）、暗部提亮（≤+25%）、高光回收（≤−18%）、眼区高通+提亮 | texture .55 / clarity .35 / shadows .45 |
| 4. 背景处理 | 高斯柔化（σ 0.6–6.6）、压暗（≤−38%）、降饱和（≤−28%）、双边额外平滑 | bg_blur .35 / bg_darken .22 |
| 5. 暗角 | 径向渐变压暗 | 0.25 |

**为什么先降噪再锐化**：先锐化会把噪点一起放大，再降噪反而把细节抹平 —— 这是后期社区的共识，也是 Lightroom/DxO 工作流的实际顺序。

**主体与背景如何过渡**：所有分区操作都是 `result = subject×mask + background×(1−mask)`，mask 用导向滤波贴边并羽化，避免"剪纸感"和光晕（halo）。眼部处理只在稳定定位的眼点半径内做，防止"发光的眼睛"。

**预设**：轻度 / 标准 / 强力，参数按比例缩放；实测"全关"时与原图逐像素一致（差异 0）。

**降噪有三种模式**（无 RAW 时自动退回 AI）：

| 模式 | 在哪做 | 实测（20MP，合成噪声 σ²=0.0016·I+2e-6） | 耗时 |
|---|---|---|---|
| 不降噪 | — | 40.66 dB | — |
| RAW 域（线性） | 线性 16bit，噪声模型 + 自适应导向滤波 | **45.41 dB** | 2.8 秒 |
| AI（sRGB） | 转 sRGB 后 NAFNet | **54.19 dB**（512² 裁切对比） | 36 秒 |

RAW 域的做法：同色像素差分 + MAD 按亮度分箱估出 `Var = a·I + b`（实测 a 估到 0.00164，真值 0.0016），
再在**每个像素**上用 `eps = k·(a·I + b)` 的导向滤波——噪声大的地方平滑、纹理强的地方保留。
它是物理正确且快的，但**质量不如 NAFNet**；"AI"仍是画质优先的选择。

**精度与格式**：整条流水线是 float32 0~1，只在落盘时才降到 8bit；勾选 16 位时输出 `.tif`。
RAW 存在时用 rawpy 解码（AHD 去马赛克、相机白平衡、Blend 高光恢复、sRGB 编码 16bit），
预览用半尺寸解码提速。**注意：这不是"RAW 域降噪"**——NAFNet 训练在 sRGB 8bit 图上，
喂线性 RAW 会破坏亮度关系，所以仍然是 sRGB 编码后降噪，只是精度提升到 16 位。
实测提亮阴影后 16bit TIFF 有 6.4 万个不同层级（8bit 上限 256），不会出现台阶。

**批量出片**：范围可选取「当前筛选结果 / 每组连拍最佳 / 已打星 / 全部（不含淘汰）」，
后台逐张处理并复用同一个检测器；输出保持原目录结构，`_出片/` 目录本身在扫描时会被跳过
（否则成品会被当成新照片二次入库）。

**入口**：大图查看器右上角「出片」按钮（或快捷键 P）、顶栏「批量出片」（按筛选/鸟种/每组最佳/打星批量）。

### 5.8 连拍升格视频（video）

把一组连拍的帧按时间顺序编码成 MP4，用于看翅膀扇动、起跳、抖动这些肉眼错过的瞬间。

**"升格"的关键是重复帧，不是调低帧率**：直接输出 10fps 会一顿一顿像幻灯片。
正确做法是每张源帧重复 N 次，N 由 `慢放倍数 / 原始帧率` 推算：

```
源连拍 5 帧、间隔 0.1 秒（10 fps）→ 4× 慢放 / 30fps 输出
repeat = round(4 × 30 / 10) = 12  →  5 × 12 = 60 帧  →  2.0 秒
```

- **参数就是"成片帧率"**：原始帧率 / 5 / 8 / 10（默认）/ 15 / 20 / 24 / 30 帧每秒，每帧只出现一次。
  成片时长 = 帧数 ÷ 帧率，弹窗里直接显示（如 5 帧 @ 5fps → 1.0 秒；@ 10fps → 0.5 秒）。
  想要慢动作就选更低的帧率；这不是"补帧"，输出文件里没有重复帧。
- 编码：imageio-ffmpeg 自带的 ffmpeg（libx264、yuv420p、+faststart），QuickTime 与微信都能播；
  没有 ffmpeg 时退回 OpenCV 的 avc1。
- 尺寸按长边上限缩放并强制偶数宽高（yuv420p 要求），逐帧统一尺寸避免编码失败。
- 可选：包含已淘汰帧、首尾淡入淡出（循环播放不跳）。
- 输出：`<root>/_视频/<日期>_<连拍名>_<倍数>.mp4`，重名自动加 `_2`；`_视频/` 与 `_出片/` 都在扫描时跳过。
- 只做缩放与编码，不降噪不调色——要成片质量走「出片」。

### 5.9 多选与删除（trash）

**删除只到废纸篓，不做永久删除**，并且整条链路可撤销：

| 环节 | 做法 |
|---|---|
| 移入废纸篓 | `NSFileManager.trashItemAtURL`，由系统决定目标（内置盘进 `~/.Trash`，外置盘进该卷 `.Trashes`），并返回实际路径用于恢复 |
| 一张照片的所有文件 | RAW + JPG + XMP 一起移走，不留半套 |
| 日志 | `journals/<时间>-trash.jsonl`，逐文件记录 `src → trashed_to`，每行 fsync |
| 恢复 | 按日志逆序移回原位；原位置已有同名文件则跳过，**绝不覆盖**；成功后写 `.undo.json` 防重复恢复 |
| 越界保护 | 只允许删除当前根目录树内的文件（`os.path.commonpath` 校验），拒绝目录外路径 |
| 并发 | 删除前先停后台分析，删除后从内存库移除并重新分组 |

**界面**：顶栏「选择」进入多选（也可按住 ⌘ 点选、Shift 连选；连拍卡片按整组选中）；
选择栏提供「全选当前 / 取消选择 / 标记淘汰 / 取消标记 / 删除…」；
Delete/Backspace 直接触发删除；「清理淘汰 (N)」一键删除所有已标记淘汰的照片。
删除前弹出确认框，明确列出**张数、文件总数和文件名**，按钮写「移到废纸篓」而非「删除」。

### 5.10 隔空传送（airdrop）

把**已打星**的照片（或选择栏里选中的照片）交给 macOS 的隔空传送面板。

- macOS 没有 AirDrop 命令行，只能用 AppKit 的 `NSSharingService`（`com.apple.share.AirDrop.send`），
  面板弹出后由用户在系统 UI 里选设备——这一步无法自动化。
- `NSSharingService` 必须在主线程：桌面版由 pywebview 跑着 `NSApplication`，所以用
  `AppHelper.callAfter` 把 `performWithItems_` 派发到**主线程**，面板属于本 App，才能成为 key window
  接收键盘（AirDrop 验证码要打字）。
- **踩过的坑**：最初用子进程 + `runConsoleEventLoop` + Accessory 策略，面板能弹出但打不了字——
  控制台事件循环不泵模态模式、进程也没有 key window。现在只有无 GUI 的调试模式才走子进程，
  且子进程改用 `app.run()` 正规循环（`--check` 可只做可用性检查、不弹面板）。
- 送的是 `photo.display`：**JPG 优先**（有 JPG 就发 JPG，没有 RAW 才发 RAW），不做 RAW/JPEG 混发。
- 面板打开是异步的，接口先返回"已打开面板"，不等待传输结果。

### 5.11 评级与淘汰（ratings）

- 星级 `-1` = 淘汰，`0` = 未评，`1–5` = 星；只写 XMP 旁注，不动原片。
- 连拍面板提供两种批量标记：**保留最佳，其余淘汰**（组内排名第一的保留，已打星的不动）、
  **整组淘汰**（整组都标 -1，已全部淘汰时按钮变为「取消整组淘汰」）；两者都只改 XMP，可随时清除。
- 已存在旁注 → 用 ExifTool 只改 `xmp:Rating`，保留 Lightroom 写入的调色参数；不存在 → 写最小 XMP 模板。
- 新建的旁注会加入 `photo.files`，整理时跟着照片一起移动。

---

## 6. 文件操作安全设计（organizer）

这是整个项目风险最高的部分，按“金融交易”标准做：

| 风险 | 措施 |
|---|---|
| 覆盖已有文件 | 目标存在即拒动；执行前对每个文件复核“源在、目标不在” |
| 中途崩溃 | 每移动一个文件写一行日志并 fsync；已完成的动作可单独撤销 |
| 跨磁盘 | `os.rename` 失败且 `EXDEV` → 复制到 `.birddesk-part` → 校验大小 → 改名 → 删源 |
| 空间不足 | 计划阶段估算总字节与目标剩余空间，不足直接禁止执行 |
| 计划过期 | 计划绑定分组 `version`；分组/评级变化后执行被拒（409），要求重新预览 |
| 空目录残留 | 只删“除 `.DS_Store` 外为空”的目录，且只删源目录树内部 |
| 撤销顺序 | 逆序还原；源位置被占用则跳过并报告，不覆盖 |
| 并发 | 移动/撤销前先停分析线程；同一时刻只允许一个阻塞任务 |

**唯一不做自动重命名的场景**：目标同名且 `rename_conflicts=false` → 该照片跳过并列入 `conflicts`。

---

## 7. 存储与缓存

```
~/Library/Application Support/BirdDesk/
├── cache.sqlite3     meta(id,v,data) / analysis(id,v,data) / species_confirm(id,zh,latin,ts)
├── cache/            species_emb_*.npz（文本嵌入，约 4.6MB）
（用户目录下：`_出片/` 成片、`_视频/` 升格视频，两者都不参与扫描）
├── thumbs/           <id>_360.jpg / _800 / _2048 / _heat_<ver>.png
├── journals/*.jsonl  整理日志（+ .undo.json）
├── settings.json
└── birddesk.log      .app 启动时的 stdout/stderr
```

**版本化失效**：`metadata.CACHE_VERSION=3`、`analyzer.VERSION=300+focus.ALGO_VERSION(4)+eyes.VERSION(2)=306`。任一算法或模型变更递增版本，旧缓存自然不再命中、后台重新计算，无需手工清库。

**刻意跳过缓存的情况**：分析结果为 `unavailable`/`error` 的条目不写回读（下次仍会重试），避免一次失败永久卡住。

---

## 8. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/env` | 版本、ExifTool、模型、原生能力、设置 |
| POST | `/api/pick_folder` | 原生目录选择（无窗口时退回 osascript） |
| POST | `/api/scan` | 扫描目录（后台任务，返回任务快照） |
| GET | `/api/status` | 任务 + 分析进度 + 版本号/序号 |
| GET | `/api/library` | 全量：统计、日期、items、照片摘要 |
| GET | `/api/updates?since=` | **增量**：变化的照片/连拍 + 分析进度 |
| POST | `/api/regroup` | 改连拍阈值/最小张数/是否分机身 → 重新分组 |
| POST | `/api/settings` | 严格度（会递增 version）/自动分析/排序 |
| POST | `/api/analysis/start|stop` | 启动（可 force）/暂停分析 |
| GET | `/api/photo/{pid}` | 详情：元数据、完整分析、连拍排名 |
| GET | `/thumb/{pid}?s=` | 360 / 800 / 2048 缩略图 |
| GET | `/heat/{pid}` | 锐度热力图 PNG（首次生成后缓存） |
| GET | `/crop/{pid}?x=&y=&w=&h=` | 100% 原始像素裁切（响应头带实际裁切范围） |
| POST | `/api/rate` | 批量打星/淘汰 → 写 XMP |
| POST | `/api/burst/cull` | 保留本组前 N 张，其余未评分的标记淘汰 |
| POST | `/api/species/confirm` | 确认鸟种 → 写 XMP 关键词 + 记入本地库 |
| POST | `/api/airdrop` | 把 ids 对应的成片交给隔空传送面板（返回 ok/count/message） |
| POST | `/api/trash` | 移入废纸篓（`ids`），返回张数/文件数/日志 id |
| POST | `/api/trash/undo` | 按删除日志恢复回原位 |
| POST | `/api/burst/video` | 连拍 → 升格 MP4（`fps` / `slow` / `max_long` / `include_rejected` / `fade`） |
| POST | `/api/develop/preview` | 出片预览（长边 1600，带缓存，返回 meta 头；支持 `denoise=raw`） |
| POST | `/api/develop/export` | 批量出片 → `<root>/_出片/`，保留目录结构；`bit_depth=16` 时输出 16bit TIFF |
| POST | `/api/plan` | 生成整理预览（不动文件） |
| POST | `/api/execute` | 执行移动（校验 plan 版本与空间） |
| GET | `/api/journals` | 整理历史 |
| POST | `/api/undo` | 撤销某次整理 |
| POST | `/api/reveal` | Finder 中显示 |

约定：写接口串行（同一时刻一个阻塞任务，冲突返回 409）；错误统一为 `{"detail": "中文原因"}`。

---

## 9. 前端设计

- **无框架**：`app.js` 单文件，模板字符串拼 DOM，按需局部刷新。理由是界面形态稳定、依赖零安装、调试直接。
- **三类刷新**：
  1. `render()` 全量重绘（扫描/分组/筛选/排序变化）；
  2. `applyUpdates()` 增量打补丁（分析结果、评级变化）——只改对应卡片与连拍汇总；
  3. 查看器内部局部刷新（`showInfo`）。
- **轮询**：1.2 秒一次 `/api/updates?since=seq`；`BUSY` 期间（扫描/整理/撤销）暂停轮询。筛选态下最多 1.5 秒节流重绘，避免滚动跳动。
- **查看器**：800px 先出图、2048 替换、100% 用 `/crop` 取原像素并按裁切偏移把目标点对齐舞台中心。
- **连拍展开**：手风琴——同时只展开一组，点另一组会自动收起前一组；面板 `max-height: 62vh` 内部滚动，
  长连拍不会把整页顶飞；点击不再自动 `scrollIntoView`，并用"卡片视口位置"做锚点校正滚动，
  尽量让被点的卡片停在原处（列表顶部无滚动余量时会贴顶，这是浏览器滚动的硬限制）。
- **跨组翻页**：本组第一张/最后一张再按 ← / → 会跳到相邻组的最后一张/第一张（顺序按当前筛选与排序后的卡片），
  放大状态下不跨组，避免误跳。
- **覆层分层**：B（鸟框/黄圈/对焦点）、E（鸟眼）、H（热力图）三个独立开关，互不遮挡；黄圈标注为“鸟体最锐区域，非眼睛”。
- **内置自测**：`node web/app.js --self-test` 跑 10 组断言（坐标透传、状态机、覆层组合、Z 回退、过期响应丢弃等），不需要浏览器。

**性能取舍**：一次性渲染整库 DOM。数千张时可接受（连拍折叠后可见元素有限），上万张需要虚拟滚动（已列入技术债）。

---

## 10. 性能实测（M1 Pro 32GB）

以下为 2026-11-01 在 1920×1920 真实鸟片上的实测（30 张样本，M1 Pro）：

| 操作 | 耗时 |
|---|---|
| 扫描（遍历 + RAW/JPG 归并） | <0.05ms/张 |
| 元数据（ExifTool 批量 250/批） | 6.8ms/张；缓存命中约 0 |
| 合焦指标（纯算，鸟占约半画面） | 约 270ms/张 ← 当前热点 |
| 完整分析（检测 + 合焦 + 鸟眼 + 鸟种） | 0.65–0.8 秒/张（分批实测：检测 170 + 合焦 260 + 鸟眼 124 + 鸟种 98 ms） |
| 鸟种文本嵌入（首次 / 之后） | 23 秒 / 直接读 npz |
| AI 降噪（512² 块，MPS / CPU） | 0.75s / 1.77s → 5000 万像素约 2.5 / 6 分钟 |
| 出片预览（1600px，含 AI 降噪） | 约 4 秒（首次），命中缓存 <0.2 秒 |
| 出片预览（1600px，不含降噪） | 约 0.5 秒 |
| 单张出片（1920px，不含降噪） | 0.3–0.7 秒 |
| RAW 解码（RX100 20MP，AHD 16bit） | 0.9 秒；预览半尺寸更快 |
| 单张出片（20MP RAW，不含降噪） | 约 4 秒 |
| 单张出片（20MP RAW + AI 降噪） | 约 38 秒（76 个 256² 块） |
| RAW 域降噪（20MP 线性） | 2.8 秒 |
| 升格视频（5 帧 → 4× / 30fps，1920 长边） | 约 1 秒，2 MB |
| 删除（移入废纸篓，每张照片全部文件） | 每张约 10–30ms |
| 16bit TIFF 体积（20MP） | 约 90MB（LZW） |
| BioCLIP 2 加载（首次进process） | 约 6 秒（1.7GB fp32，MPS） |
| 缩略图 360 / 2048 | 0.10s / 0.20s（生成后命中缓存） |
| 热力图 | 0.49s（首次，之后走缓存） |
| 100% 裁切 | 0.17s |

1000 张一轮完整分析约 11–14 分钟（含一次模型加载），后台进行，期间可正常浏览、筛选、打星。

---

## 11. 已知限制与技术债

**功能限制**

0. 鸟种是零样本识别：33 张带标签公开图上 top-1 81.8%、top-5 100%；错误集中在同属近缘种
   （白鹭→黄嘴白鹭、棕背伯劳→褐背伯劳、喜鹊→青藏喜鹊）。真实野外（小鸟、遮挡、逆光）会更差，
   图库级清晰度不能外推。概率未经校准，阈值只按这批样本设定。
1. 没有真正的鸟眼检测保障：公开样本 16 张中只有 5 张通过“已识别”，其余为候选；未用用户真实照片校准。
2. 合焦阈值基于 16 张网络鸟片 + 人工退化校准，真实弱光/高感场景可能偏严或偏松。
3. 眼区锐度未校准，不参与评级与自动淘汰。
4. 排序只在同一日期内生效，没有跨日期全局排行。
5. 撤销只覆盖文件整理，不覆盖评级/淘汰（后者只写旁注，可由用户在 Lightroom 覆盖）。
6. EXIF 镜像方向（2/4/5/7）未处理，仅处理 1/3/6/8。
6b. RAW 域降噪是**经典方法**（噪声模型 + 导向滤波），不是深度学习：在高感强色噪下不如 NAFNet
    （同条件 45.4 dB vs 54.2 dB）。它的价值是快（2.8s vs 36s）且物理上不破坏线性关系。
7. 鸟种只识别主鸟；多鸟同框时其余个体不参与，也不做“一张图多个物种”的标注。
9. 出片在 **sRGB 8bit（JPG / RAW 内嵌预览）** 上处理，没有 RAW 域降噪；高光恢复能力受限于已压缩的 8bit 数据。
10. 背景柔化是高斯模糊，不是真实景深（不做深度估计），主体与背景交界靠蒙版羽化过渡；对细羽毛、细树枝边缘仍可能出现轻微不自然。
11. 主体蒙版来自 COCO 鸟体分割，细腿、爪、喙尖可能被漏掉或误含树枝；目前没有手工修蒙版的入口。
12. 导出支持 JPG（质量 94）与 16bit TIFF（LZW）；**16 位不等于 RAW 域处理**：降噪仍在 sRGB 编码空间进行，
    只是喂给算法的数据从 8bit 变成 16bit，精度更高但噪声模型没变。
13. 出片是"另存一份"，不回写原片、也不生成 Lightroom 侧车调整（关键词与星级才会写 XMP）。
14. 鸟种筛选只能按**单一物种**筛（"某一种"或"未识别"），不支持多选、也不支持按科/目聚合。
15. 20MP RAW 开 AI 降噪约 38 秒/张，50MP 约 2–3 分钟；批量时没有断点续跑，中途关掉会重来。
16. 升格视频只处理"一组连拍"，不能跨组拼接；没有音频、没有转场；帧率高时文件较大
    （60 帧 1080p 约 2MB，4K 会到十几 MB）。
17. 删除是"移到废纸篓"，没有永久删除入口——刻意不做；清空废纸篓是不可逆的，应用管不到。
18. 多选没有矩形框选/拖拽选择，只有点选、⌘ 点选与 Shift 连选；选中状态不持久化（刷新即清空）。
20. 隔空传送依赖图形会话与系统 AirDrop 服务：没有登录桌面、关闭了 AirDrop、或附近没有接收设备时，
    系统面板会拒绝或一直等待；本应用只负责把文件交给面板，不确认是否真正送达。
19. 删除日志记录的是"废纸篓里的路径"，如果用户在 Finder 里改名或用第三方清理工具移动，
    恢复会因找不到文件而跳过，不会误删别的东西。
8. 关闭鸟种（设置 `auto_species=false`）后不再加载 1.7GB 模型，但已缓存的旧结果仍会显示。

**工程债**

1. 分析缓存键基于“文件名+大小+mtime”，RAW+JPG 配对时以 RAW 为准；JPG 单独被替换可能命中旧分析。
2. `thumbs/` 与热力图缓存无容量上限、无清理策略。
3. `focus.assess()` 对整块裁切图做 4 方向 × 2 尺度卷积，单张约 270ms，占分析总时长三分之一以上；可改为在更小尺度计算或走 FFT，属于性价比最高的优化点。
4. 全库照片与分析结果常驻内存；十万张级目录需要改为按需加载。
5. 前端全量重绘在超大目录下有卡顿风险。
6. `server.py` 已 610 行，职责偏多（路由 + 状态 + 编排），后续应拆出 `Library` 与 `routes`。
7. 无数据库迁移机制，表结构变化需手工处理。
8. 无 i18n，文案硬编码中文。

**许可证**

- **本仓库自有代码与文档：CC BY-NC 4.0**（署名—非商业性使用 4.0 国际），见根目录 `LICENSE`；
  可复制/修改/再分发，但须署名并保留许可链接，且不得用于商业目的。
- YOLO11m-seg：Ultralytics **AGPL-3.0**（自用无碍，公开分发或商用需商业授权）。
- BioCLIP 2：模型卡标 MIT；训练数据（TreeOfLife-10M 等）另有条款，见 `models/bioclip2.provenance.json`。
- 中国鸟类名录数据 `data/birds_cn.json`：**CC BY-NC 4.0**，仅个人/学术/非商业使用，必须署名；
  商业分发需替换为自建或商用许可名录，见 `data/birds_cn.provenance.json`。
- NAFNet 结构：Apache-2.0（`vendor/nafnet/`）；**权重来自公开镜像且未标注许可证**，
  仅本地个人使用，商用前须从官方渠道重新获取并核对，见 `models/nafnet.provenance.json`。
- eye-pose-v0：模型卡标 MIT，但底座与训练数据（CUB-200-2011）另有研究用途限制，**不能据此认定可无条件分发**；细节见 `models/eye_pose_v0.provenance.json`。
- ExifTool：Perl Artistic/GPL 双许可，随包分发需保留版权声明。

---

## 12. 演进路线

| 阶段 | 内容 | 接入点 |
|---|---|---|
| ~~P3 鸟种识别~~（已完成，0.3.0） | BioCLIP 2 零样本 → 中国鸟类名录 1508 种 → Top-5 → 人工确认写 XMP → 连拍投票 | `app/species.py`、`/api/species/confirm`、鸟种卡 |
| ~~P4 出片~~（已完成，0.4.0） | AI 降噪 + 主体蒙版 + 主体/背景分区增强 + 暗角 + 另存 `_出片/` | `app/denoise.py`、`app/develop.py`、`/api/develop/*` |
| ~~P4.1 批量出片~~（0.5.0） | 按筛选/每组最佳/打星/全部批量出片，后台进度 | 复用 `/api/develop/export` |
| ~~P4.2 16bit + RAW~~（0.5.0） | RAW 16 位解码、全浮点流水线、16bit TIFF 导出 | `app/raw_io.py` |
| ~~P4.3 RAW 域降噪~~（0.6.0） | 线性域噪声模型 + 自适应导向滤波；四种降噪模式可选 | `app/rawdenoise.py`、`develop.prepare()` |
| ~~鸟种进筛选器~~（0.6.0） | 按物种/未识别筛选，批量出片可"当前鸟种" | `web/app.js` `speciesOf()` |
| ~~P5 连拍升格视频~~（0.7.0） | 重复帧实现真慢放、H.264 输出、`_视频/` 目录 | `app/video.py`、`/api/burst/video` |
| ~~P6 多选与删除~~（0.8.0） | 多选（点选/⌘/Shift）、批量淘汰、Delete 删除、一键清理淘汰、废纸篓恢复 | `app/trash.py`、`/api/trash*` |
| 下一步（候选） | 专用 RAW 降噪网络（需 RAW 训练数据且权重多在国外网盘）；鸟种多选/科属聚合；手工修蒙版；升格视频加转场/字幕/音乐 | — |
| 后续 | 虚拟滚动、缓存清理、跨日期排行、手动校正鸟眼、学习用户确认结果做小样本微调 | — |

P3 的预留设计：分析结果已是“可扩展 JSON + 版本号”，新增字段只需递增 `analyzer.VERSION`；鸟种结果同样按 `id` 缓存在 `analysis` 表，不新增表。

---

## 13. 测试策略

| 入口 | 覆盖 |
|---|---|
| `tests/test_flow.py` | 扫描 → 分组 → 预览 → 移动 → 幂等 → 撤销 → 与初始快照逐字节一致 |
| `tests/test_analysis.py` | 合焦分级准确率、连拍择优、严格度切换、XMP 评级往返、`_淘汰` 整理、缓存命中 |
| `tests/test_eyes.py` | 16 项单测（坐标/翻转/离群/掩膜/缺模型/坏权重/太小/无鸟/异常隔离/线程停止）+ 公开样本 smoke + 断网推理 |
| `tests/test_trash.py` | 移入废纸篓、源文件消失、库计数变化、日志含废纸篓路径、恢复后字节一致、防重复恢复 |
| `tests/test_video.py` | 编码帧数/帧率/时长、慢放倍数换算、尺寸偶数化、重名不覆盖、接口产出、`_视频` 不被扫描 |
| `tests/test_develop.py` | 降噪 PSNR 提升、蒙版覆盖率、分区亮度关系、全关时逐像素一致、预览/导出接口、原片字节不变、16bit TIFF 位深、真实 ARW 解码、出片目录不被二次扫描、RAW 域降噪噪声估计与 PSNR 增益 |
| `tests/test_species.py` | 名录完整性、裁切、阈值分档、真实推理、XMP 关键词去重、确认接口 |
| `tests/eval_species.py` + `fetch_species_eval.py` | 拉取带标签公开鸟片，评估 top-1/top-5 与置信度分档 |
| `tests/calibrate_focus.py` | 在真实鸟片 + 人工退化（失焦/运动模糊/噪声/放大）上标定阈值 |
| `make_samples.py` / `make_bird_samples.py` | 生成带真实 EXIF 的合成样本，含跨午夜、缺亚秒、重名、损坏文件等边界 |
| `node web/app.js --self-test` | 前端纯函数与状态机 10 组断言 |

原则：**所有测试在临时目录内进行，不触碰用户照片**；涉及移动的测试必须验证“文件数量与内容快照前后一致”。

---

## 14. 附录：本地开发与运行

```bash
git clone git@github.com:jiqiujia/BirdDesk.git
cd BirdDesk
./setup.sh                    # 首次：建 .venv、装依赖、下载模型（约 1.8GB）

# 桌面窗口（正常使用）
./BirdDesk.app/Contents/MacOS/BirdDesk        # 或双击 BirdDesk.app
.venv/bin/python -m app.main                  # 等价，日志直接打到终端

# 浏览器调试模式（改前端时最方便：刷新即生效）
.venv/bin/python -m app.main --no-open --port 8765
# 然后浏览器打开 http://127.0.0.1:8765/

# 测试（需要一份照片目录，仓库不含图片）
BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_flow.py                  # 分组/整理/撤销
.venv/bin/python tests/test_analysis.py /path/to/photos                            # 合焦/择优
.venv/bin/python tests/test_eyes.py --images /path/to/photos                       # 鸟眼
BIRDDESK_SAMPLE_DIR=/path/to/photos/DCIM/100MSDCF \
  .venv/bin/python tests/test_develop.py                                           # 出片/16bit/RAW
.venv/bin/python tests/test_video.py                                               # 升格视频
BIRDDESK_SAMPLE_DIR=/path/to/photos/DCIM/100MSDCF \
  .venv/bin/python tests/test_trash.py                                             # 删除/恢复
```

`tests/calibrate_focus.py`（合焦阈值标定）与 `tests/eval_species.py`（鸟种准确率评估）需要额外样本集，用法见文件头注释。
