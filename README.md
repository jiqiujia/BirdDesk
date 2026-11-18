# BirdDesk

> macOS 上的**拍鸟照片工作流**：分组整理 → 合焦判定 → 鸟眼定位 → 鸟种识别 → 分区出片 → 批量导出 → 连拍升格视频。
>
> 全部本地运行，不联网、不上传照片。界面为中文。

## 它能做什么

| 模块 | 能力 |
|---|---|
| **整理** | 按日期 + 连拍分组；预览 → 执行 → 撤销；保留目录结构，只移动不重命名 |
| **合焦** | 分块 acutance（高频/中频能量比）+ 四方向一致性，判出 合焦 / 偏软 / 失焦 / 跑焦 / 运动模糊；连拍内自动择优 |
| **鸟眼** | 6 点关键点定位（喙、双眼、头顶、双肩）+ 双视角瞳孔校验，区分「稳定」与「候选」 |
| **鸟种** | BioCLIP 2 零样本识别 + 中国鸟类名录 1508 种，可一键写入 XMP 关键词 |
| **出片** | AI 降噪（NAFNet）或 RAW 域线性降噪；主体蒙版 + 主体/背景分区增强；暗角；输出 JPG 或 16bit TIFF |
| **批量出片** | 按「当前筛选 / 当前鸟种 / 每组最佳 / 已打星 / 全部」批量，后台跑，不阻塞浏览 |
| **升格视频** | 一组连拍 → MP4（5/8/10/15/20/24/30 帧每秒可选），用于看清扇翅、起跳 |
| **删除** | 多选 → 移入系统废纸篓（可恢复、有日志）；一键清理所有已淘汰照片 |
| **隔空传送** | 把已打星的成片（JPG 优先）交给 macOS 隔空传送面板 |

## 环境要求

- macOS 12+（Apple Silicon 推荐，降噪走 MPS 加速）
- Python 3.11+
- 磁盘约 3.5 GB（模型 1.8 GB + 虚拟环境 1.7 GB）

## 快速开始

```bash
git clone git@github.com:jiqiujia/BirdDesk.git
cd BirdDesk
./setup.sh          # 建 .venv、装依赖、下载模型与权重
./make_app.sh       # 打包成 BirdDesk.app
open BirdDesk.app
```

也可以直接跑源码：

```bash
./.venv/bin/python -m app.main                      # 起服务 + 打开窗口
./.venv/bin/python -m app.main --no-open --port 8765  # 只起服务（浏览器调试用）
```

只想要代码、不下载模型：

```bash
./setup.sh --code-only
```

## 使用流程

1. **选目录**：指向放照片的文件夹（如 `~/Pictures/SonyFTP/2026-10-31`），会自动扫描并开始合焦分析。
2. **看结果**：卡片显示合焦分、鸟种建议；点开连拍可看逐帧分数、鸟眼、对焦点。
3. **挑选**：打星（1–5）、标记淘汰（`-1`）、「保留最佳其余淘汰」或「整组淘汰」。
4. **出片**：大图右上角「出片」，选预设与降噪方式，预览后导出到 `_出片/`。
5. **收尾**：批量出片、生成升格视频、把打星的照片隔空传送到手机。

常用快捷键（大图内）：`←/→` 翻页（到组末自动跳下一组）、`Z` 100% 查看、`X` 淘汰、`1–5` 打星、`P` 出片、`Esc` 关闭。

## 数据放在哪（重要）

| 类型 | 位置 | 说明 |
|---|---|---|
| 原始照片 | 你自己的目录 | **只读**。唯一例外是「删除」，走系统废纸篓，可恢复 |
| 写回元数据 | 同目录 `.xmp` 旁注 | 星级与鸟种关键词；不动原片像素 |
| 整理移动 | 你自己的目录 | 有日志，可在「整理记录」里撤销 |
| 应用数据 | `~/Library/Application Support/BirdDesk` | 缩略图、缓存、日志 |
| 产出 | `<照片目录>/_出片/`、`_视频/` | 扫描时自动跳过，不会被当成新照片 |

## 模型与许可

仓库**公开**，模型权重与第三方数据不在仓库内（体积大），由 `setup.sh` 下载。

| 组件 | 来源 | 许可 | 注意 |
|---|---|---|---|
| BioCLIP 2 | `imageomics/bioclip-2` | MIT | 训练数据 TreeOfLife-10M 另有条款；零样本，中国鸟种易混近缘种 |
| YOLO11m-seg | Ultralytics | **AGPL-3.0**（或商业许可） | 商用需向 Ultralytics 购买授权 |
| eye-pose-v0 | `synthet/eye-pose-v0` | MIT（模型卡） | 上游依赖 Ultralytics，训练源 CUB-200-2011 有研究用途限制 |
| NAFNet | `megvii-research/NAFNet`（代码 Apache-2.0） | 权重来自公开镜像，**未标注许可** | 仅本地个人使用；商用请从官方渠道重新获取并核对 |
| 中国鸟类名录 | 见 `data/birds_cn.provenance.json` | **CC BY-NC 4.0** | 非商业使用、需署名；商业分发须替换 |
| ExifTool 13.60 | Phil Harvey | Perl Artistic / GPL | 随仓库分发 |

结论：**本项目按个人/学术非商业用途发布**。要做商业产品，至少需要替换名录数据、取得 YOLO 商业许可、核对各权重条款。

## 测试

测试**不自带照片**（仓库里没有图片），需要指向一份你自己的照片目录。缺目录时相关测试会打印「跳过」而不是失败。

```bash
# 分组 / 整理 / 撤销（自带合成样本，不需要照片）
BIRDDESK_DATA_DIR=$(mktemp -d) ./.venv/bin/python tests/test_flow.py

# 合焦 / 连拍择优：第一个参数是照片目录
BIRDDESK_DATA_DIR=$(mktemp -d) ./.venv/bin/python tests/test_analysis.py /path/to/photos

# 鸟眼定位
./.venv/bin/python tests/test_eyes.py --images /path/to/photos

# 出片 / 16bit TIFF / RAW（需要一批 JPG）
BIRDDESK_SAMPLE_DIR=/path/to/photos/DCIM/100MSDCF \
BIRDDESK_DATA_DIR=$(mktemp -d) ./.venv/bin/python tests/test_develop.py

# 升格视频（默认读 /tmp/birdcal，可用 make_bird_samples 生成）
BIRDDESK_DATA_DIR=$(mktemp -d) ./.venv/bin/python tests/test_video.py

# 删除到废纸篓 / 恢复（会真的移动文件，建议用副本）
BIRDDESK_SAMPLE_DIR=/path/to/photos/DCIM/100MSDCF \
BIRDDESK_DATA_DIR=$(mktemp -d) ./.venv/bin/python tests/test_trash.py
```

`tests/calibrate_focus.py`、`tests/eval_species.py` 是标定与评估脚本，需要额外的样本集，用法见文件头注释。

## 已知限制

- 合焦阈值标定自少量公开样本，不同机身/镜头的锐化风格会有偏差；可在工具栏切「宽松/标准/严格」。
- 鸟种识别是零样本，同属近缘种（白鹭 vs 黄嘴白鹭）容易混，结果需要人工确认。
- 20MP RAW 开 AI 降噪约 38 秒/张；5000 万像素约 2–3 分钟。RAW 域降噪快但画质不如 AI。
- 背景柔化是高斯模糊，不是真实景深；主体蒙版目前不能手工修改。
- 升格视频没有转场、字幕、音轨；隔空传送只负责把文件交给系统面板，不确认送达。

## 架构与算法

算法细节、参数、性能数据、接口清单都在 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。
关键取舍（为什么先降噪再锐化、为什么分区增强、为什么 16 位不等于 RAW 域处理）在对应的 5.x 小节里。
