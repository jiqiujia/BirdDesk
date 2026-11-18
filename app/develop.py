"""出片流水线：主体分离 → 分区增强 → 可选 AI 降噪 → 导出。

顺序按摄影后期惯例（也是社区实践的顺序）：
  1. 降噪（先做，避免后续锐化把噪点放大）
  2. 主体蒙版（鸟体分割 + 导向滤波贴边）
  3. 主体增强（纹理/清晰度、阴影提亮、高光回收、鸟眼微对比）
  4. 背景处理（压暗、降饱和、轻微柔化、额外平滑）
  5. 暗角
  6. 导出（另存，不覆盖原片；支持 8bit JPG / 16bit TIFF）

**全程 float32 0~1**：8 位只在最后落盘时出现，这样 16 位 TIFF 才有意义
（提亮阴影时不会出现台阶）。所有参数都可关掉，全关时输出与输入逐像素一致。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from . import analyzer, denoise, rawdenoise, raw_io
from .config import EXPORT_FOLDER

PRESETS = {
    # amount 均以 0–1 归一，0 表示不做
    "light": dict(denoise="ai", texture=0.30, clarity=0.20, shadows=0.25, highlights=0.20,
                  eye=0.35, bg_darken=0.12, bg_desat=0.10, bg_blur=0.0, vignette=0.15, bg_denoise=0.0),
    "standard": dict(denoise="ai", texture=0.55, clarity=0.35, shadows=0.45, highlights=0.35,
                     eye=0.55, bg_darken=0.22, bg_desat=0.18, bg_blur=0.35, vignette=0.25, bg_denoise=0.35),
    "strong": dict(denoise="ai", texture=0.80, clarity=0.55, shadows=0.60, highlights=0.50,
                   eye=0.75, bg_darken=0.32, bg_desat=0.28, bg_blur=0.60, vignette=0.35, bg_denoise=0.55),
}
LABELS = {"light": "轻度", "standard": "标准", "strong": "强力"}
MASK_LONG = 1600      # 蒙版计算的目标长边


@dataclass
class Params:
    denoise: str = "ai"        # ai / off
    texture: float = 0.55      # 主体纹理（细节锐化）
    clarity: float = 0.35      # 主体中间调对比
    shadows: float = 0.45      # 主体阴影提亮
    highlights: float = 0.35   # 主体高光回收
    eye: float = 0.55          # 眼部微对比（需鸟眼定位成功）
    bg_darken: float = 0.22    # 背景压暗
    bg_desat: float = 0.18     # 背景降饱和
    bg_blur: float = 0.35      # 背景柔化（模拟浅景深）
    bg_denoise: float = 0.35   # 背景额外平滑（保护主体细节）
    vignette: float = 0.25     # 暗角
    feather: float = 0.002     # 蒙版羽化（相对长边）

    @staticmethod
    def preset(name: str) -> "Params":
        return Params(**PRESETS.get(name, PRESETS["standard"]))

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 蒙版
def guided_filter(guide: np.ndarray, src: np.ndarray, r: int, eps: float) -> np.ndarray:
    """He 等人的导向滤波（自实现，避免依赖 opencv-contrib）。

    把粗多边形蒙版当作 src、原图灰度当作 guide：边界会吸附到原图真实边缘，
    这样羽毛与树枝的交界处不会出现"剪纸感"。
    """
    k = (2 * r + 1, 2 * r + 1)
    mean_i = cv2.blur(guide, k)
    mean_p = cv2.blur(src, k)
    corr_i = cv2.blur(guide * guide, k)
    corr_ip = cv2.blur(guide * src, k)
    var_i = corr_i - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    return cv2.blur(a, k) * guide + cv2.blur(b, k)


def build_mask(rgb: np.ndarray, det=None) -> tuple[np.ndarray | None, str]:
    """返回 float32 0~1 主体蒙版（HxW）。rgb 可以是 uint8 或 float。"""
    if det is None:
        det = analyzer.Detector()
    u8 = rgb if rgb.dtype == np.uint8 else np.uint8(np.clip(rgb, 0, 1) * 255)
    H, W = u8.shape[:2]
    try:
        birds = det.detect(u8)
    except Exception as e:  # noqa: BLE001
        return None, f"检测失败：{type(e).__name__}"
    if not birds:
        return None, "没有检测到鸟，跳过主体分离"
    b = max(birds, key=lambda d: (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]) * d["conf"])
    poly = b.get("poly")
    m = np.zeros((H, W), np.uint8)
    if poly is None or len(poly) < 3:
        x1, y1, x2, y2 = [int(v) for v in b["box"]]
        m[max(0, y1):min(H, y2), max(0, x1):min(W, x2)] = 255
    else:
        cv2.fillPoly(m, [np.round(poly).astype(np.int32)], 255)
    guide = cv2.cvtColor(u8, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    r = max(4, int(min(H, W) * 0.006))
    mf = np.clip(guided_filter(guide, m.astype(np.float32) / 255, r, 1e-3), 0, 1)
    # 轻微内缩，避免把背景边缘算进主体
    k = max(3, int(min(H, W) * 0.0015) | 1)
    mf = cv2.erode(mf, np.ones((k, k), np.uint8))
    return mf.astype(np.float32), ""


def _shrink(img: np.ndarray, long_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = min(1.0, long_side / max(h, w))
    if s >= 1.0:
        return img
    import cv2 as _cv
    return _cv.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=_cv.INTER_AREA)


def feather(mask: np.ndarray, long_side: int, frac: float) -> np.ndarray:
    sigma = max(0.8, long_side * max(frac, 0.0002))
    return np.clip(cv2.GaussianBlur(mask, (0, 0), sigma), 0.0, 1.0)


# ---------------------------------------------------------------- 分步处理（全浮点）
def _luma(img: np.ndarray) -> np.ndarray:
    return img[:, :, 0] * 0.299 + img[:, :, 1] * 0.587 + img[:, :, 2] * 0.114


def subject_enhance(img: np.ndarray, mask: np.ndarray, p: Params,
                    eyes: list[dict] | None = None) -> np.ndarray:
    """只在主体上做纹理/清晰度/阴影/高光/眼部处理，按蒙版混回。"""
    out = img
    if p.texture > 0:
        blur = cv2.GaussianBlur(out, (0, 0), 1.2)
        out = np.clip(out + (out - blur) * (1.6 * p.texture), 0, 1)
    if p.clarity > 0:
        blur = cv2.GaussianBlur(out, (0, 0), 12.0)
        out = np.clip(out + (out - blur) * (0.55 * p.clarity), 0, 1)
    if p.shadows > 0 or p.highlights > 0:
        lum = _luma(out)
        gain = np.ones_like(lum, np.float32)
        if p.shadows > 0:
            gain += p.shadows * 0.25 * np.clip((0.45 - lum) / 0.45, 0, 1) ** 1.2
        if p.highlights > 0:
            gain -= p.highlights * 0.18 * np.clip((lum - 0.75) / 0.25, 0, 1)
        out = np.clip(out * gain[:, :, None], 0, 1)
    if p.eye > 0 and eyes:
        out = _eye_boost(out, eyes, p.eye)
    m3 = mask[:, :, None]
    return np.clip(img * (1 - m3) + out * m3, 0, 1)


def _eye_boost(img: np.ndarray, eyes: list[dict], amount: float) -> np.ndarray:
    H, W = img.shape[:2]
    out = img.copy()
    for e in eyes:
        if not e.get("stable"):
            continue
        x, y = int(e["x"] * W), int(e["y"] * H)
        r = max(8, int(min(H, W) * 0.02))
        x0, y0 = max(0, x - r), max(0, y - r)
        x1, y1 = min(W, x + r), min(H, y + r)
        patch = out[y0:y1, x0:x1]
        hp = patch - cv2.GaussianBlur(patch, (0, 0), 1.0)
        patch = np.clip(patch + hp * (0.9 * amount), 0, 1)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        d = np.sqrt((xx - x) ** 2 + (yy - y) ** 2) / max(r, 1)
        w = (np.clip(1 - d, 0, 1) ** 1.5)[:, :, None] * amount
        out[y0:y1, x0:x1] = patch * w + out[y0:y1, x0:x1] * (1 - w)
    return out


def background_enhance(img: np.ndarray, mask: np.ndarray, p: Params) -> np.ndarray:
    bg = img
    if p.bg_denoise > 0:
        u8 = np.uint8(np.clip(bg, 0, 1) * 255)
        sm = cv2.bilateralFilter(u8, 5, 25 * p.bg_denoise, 5).astype(np.float32) / 255
        bg = bg * mask[:, :, None] + sm * (1 - mask[:, :, None])
    if p.bg_blur > 0:
        bg = cv2.GaussianBlur(bg, (0, 0), 0.6 + 6.0 * p.bg_blur)
    if p.bg_darken > 0:
        bg = np.clip(bg * (1 - 0.38 * p.bg_darken), 0, 1)
    if p.bg_desat > 0:
        gray = _luma(bg)[:, :, None]
        bg = bg * (1 - p.bg_desat) + gray * p.bg_desat
    m3 = mask[:, :, None]
    return np.clip(img * m3 + bg * (1 - m3), 0, 1)


def vignette(img: np.ndarray, amount: float) -> np.ndarray:
    """径向暗角。在 512 的粗网格上算再放大——暗角是极低频，看不出差别，
    但省掉全分辨率 float64 网格（20MP 时这一步能从 ~2.5s 降到 ~10ms）。"""
    if amount <= 0:
        return img
    H, W = img.shape[:2]
    s = min(1.0, 512 / max(H, W))
    gh, gw = max(2, int(H * s)), max(2, int(W * s))
    yy, xx = np.mgrid[0:gh, 0:gw].astype(np.float32)
    cy, cx = gh / 2, gw / 2
    d = np.sqrt(((yy - cy) / cy) ** 2 + ((xx - cx) / cx) ** 2) / np.sqrt(2)
    v = (1 - amount * np.clip((d - 0.45) / 0.55, 0, 1) ** 1.6).astype(np.float32)
    if (gh, gw) != (H, W):
        v = cv2.resize(v, (W, H), interpolation=cv2.INTER_LINEAR)
    return np.clip(img * v[:, :, None], 0, 1).astype(np.float32)


# ---------------------------------------------------------------- 主流程
def prepare(photo, params: "Params", progress=None):
    """按 denoise 模式准备输入图，返回 (srgb float, info, 剩余要跑的降噪模式)。

    - off / ai：解码为 sRGB，ai 留给 develop_f 里做（NAFNet 在 sRGB 域）
    - raw：解码为线性 → 线性域降噪 → 转 sRGB
    没有 RAW 时会自动退回 sRGB 路径（并注明）。
    """
    mode = params.denoise
    if mode == "raw":
        lin, info = raw_io.load_linear(photo)
        if lin is not None:
            lin, dinfo = rawdenoise.denoise_linear(lin, 1.0, progress=progress)
            img = rawdenoise.to_srgb(lin)
            return img, {**info, "denoise": "raw", "noise": dinfo}, "off"
        img, info = raw_io.load(photo)
        return img, {**info, "denoise": "ai-fallback", "note": info.get("reason", "无 RAW")}, "ai"
    img, info = raw_io.load(photo)
    return img, {**info, "denoise": mode}, mode


def develop_f(img01: np.ndarray, params: Params, det=None, eyes: list[dict] | None = None,
              progress=None) -> tuple[np.ndarray, dict]:
    """输入/输出都是 float32 0~1，全程保精度（16 位导出走这条）。"""
    img = np.asarray(img01, dtype=np.float32)
    if img.size and float(img.max()) > 1.5:      # 容错：8bit 输入自动归一
        img = img / 255.0
    H, W = img.shape[:2]
    report: dict = {"steps": [], "mask": None, "denoise": params.denoise}

    if params.denoise != "off" and denoise.available():
        img = denoise.denoiser().run(
            img, progress=(lambda d, t: progress(0.0, 0.55, f"AI 降噪 {d}/{t}")) if progress else None)
        report["steps"].append("AI 降噪")
    elif params.denoise != "off":
        report["denoise"] = "off"
        report["steps"].append("AI 降噪不可用，已跳过")

    # 蒙版在低分辨率上算再放大：全分辨率做导向滤波很贵，而鸟类边缘的尺度远大于
    # 这个分辨率损失；实测 20MP 从 ~10s 降到 ~3s，视觉上看不出差别。
    mask, note = build_mask(_shrink(img, MASK_LONG), det)
    report["mask"] = note or "ok"
    if mask is not None and mask.shape[:2] != (H, W):
        mask = cv2.resize(mask, (W, H), interpolation=cv2.INTER_LINEAR)
    if mask is None:
        out = vignette(img, params.vignette) if params.vignette > 0 else img
        return np.clip(out, 0, 1), report
    mask = feather(mask, max(H, W), params.feather)
    report["mask_coverage"] = round(float(mask.mean()), 4)
    if progress:
        progress(0.6, 1.0, "主体增强")
    img = subject_enhance(img, mask, params, eyes)
    report["steps"].append("主体增强")
    img = background_enhance(img, mask, params)
    report["steps"].append("背景处理")
    if params.vignette > 0:
        img = vignette(img, params.vignette)
        report["steps"].append("暗角")
    return np.clip(img, 0, 1), report


def develop(rgb: np.ndarray, params: Params, det=None, eyes: list[dict] | None = None,
            progress=None) -> tuple[np.ndarray, dict]:
    """兼容旧接口：uint8 进、uint8 出（内部走浮点）。"""
    arr = rgb if rgb.dtype != np.uint8 else rgb.astype(np.float32) / 255.0
    f, rep = develop_f(arr, params, det=det, eyes=eyes, progress=progress)
    return np.uint8(np.clip(f, 0, 1) * 255 + 0.5), rep


def export_paths(src: Path, root: Path, out_dir: Path, ext: str = ".jpg") -> Path:
    """保持相对目录结构，统一放到 out_dir 下，文件名加后缀，绝不覆盖原片。"""
    try:
        rel = src.parent.relative_to(root)
    except ValueError:
        rel = Path("")
    return out_dir / rel / f"{src.stem}_出片{ext}"


def save(path: Path, rgb: np.ndarray, quality: int = 94) -> None:
    """按扩展名落盘：.tif 写 16 位，其余写 8 位 JPEG。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in (".tif", ".tiff"):
        from . import raw_io
        raw_io.save_tiff(path, rgb)
        return
    u8 = rgb if rgb.dtype == np.uint8 else np.uint8(np.clip(rgb, 0, 1) * 255 + 0.5)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(u8, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError("JPEG 编码失败")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(path)
