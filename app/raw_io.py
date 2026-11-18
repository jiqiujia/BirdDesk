"""16 位 / RAW 解码与导出。

为什么需要单独一层：
- 8 位 JPEG 走完全程会在暗部/高光留下台阶，提亮阴影时尤其明显；
- RAW 用 rawpy 解出 16 位，再走同一条浮点流程，阴影/高光操作有余量；
- 导出 16 位 TIFF 便于后续进 Lightroom/PS 继续处理。

注意：这里做的是"16 位精度 + RAW 解码"，**不是 RAW 域（未去马赛克线性空间）降噪**。
NAFNet 是在 sRGB 8bit 图像上训练的，直接喂线性 RAW 会得到错误的亮度关系，
所以解码后仍转成 sRGB 编码的浮点再降噪——精度是 16 位，色彩空间不变。
"""
from __future__ import annotations

import numpy as np

from .config import RAW_EXTS
from .scanner import Photo

try:
    import rawpy  # type: ignore
except Exception:  # noqa: BLE001
    rawpy = None


def has_raw(photo: Photo) -> bool:
    return any(f.suffix.lower() in RAW_EXTS for f in photo.files)


def load_linear(photo: Photo, max_side: int | None = None) -> tuple[np.ndarray | None, dict]:
    """解码为**线性** 16 位浮点（gamma=(1,1)、不做自动提亮），用于 RAW 域降噪。

    没有 RAW 或解码失败时返回 (None, info)，调用方自行回退。
    """
    raw_path = next((f for f in photo.files if f.suffix.lower() in RAW_EXTS), None)
    info = {"source": "none", "bits": 0}
    if raw_path is None or rawpy is None:
        return None, {**info, "reason": "没有 RAW 文件" if raw_path is None else "rawpy 不可用"}
    try:
        with rawpy.imread(str(raw_path)) as raw:
            half = bool(max_side and max_side <= 2000)
            lin16 = raw.postprocess(
                half_size=half,
                output_bps=16,
                use_camera_wb=True,
                no_auto_bright=True,
                gamma=(1, 1),                   # 线性：不做 gamma 编码
                highlight_mode=rawpy.HighlightMode.Blend,
                demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                output_color=rawpy.ColorSpace.sRGB,
            )
        lin = lin16.astype(np.float32) / 65535.0
        return _fit(lin, max_side), {**info, "source": "raw-linear", "bits": 16,
                                    "size": [lin.shape[1], lin.shape[0]], "raw": raw_path.name}
    except Exception as e:  # noqa: BLE001
        return None, {**info, "reason": f"{type(e).__name__}"}


def load(photo: Photo, max_side: int | None = None) -> tuple[np.ndarray, dict]:
    """返回 (float32 RGB 0~1, 信息)。RAW 优先 16 位解码，否则回退 JPG。"""
    raw_path = next((f for f in photo.files if f.suffix.lower() in RAW_EXTS), None)
    info = {"source": "jpg", "bits": 8, "demosaic": None}
    if raw_path is not None and rawpy is not None:
        try:
            with rawpy.imread(str(raw_path)) as raw:
                # 预览用半尺寸解码即可，速度快一倍（长边仍 >1600）
                half = bool(max_side and max_side <= 2000)
                rgb16 = raw.postprocess(
                    half_size=half,
                    output_bps=16,
                    use_camera_wb=True,
                    no_auto_bright=True,
                    gamma=(2.4, 12.92),          # sRGB 编码，与相机 JPEG 观感一致
                    highlight_mode=rawpy.HighlightMode.Blend,
                    demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                    output_color=rawpy.ColorSpace.sRGB,
                )
            img = rgb16.astype(np.float32) / 65535.0
            info.update(source="raw", bits=16, demosaic="AHD",
                        raw=str(raw_path.name), size=[img.shape[1], img.shape[0]])
            return _fit(img, max_side), info
        except Exception as e:  # noqa: BLE001
            info["raw_error"] = f"{type(e).__name__}"
            info["source"] = "jpg-fallback"
    from . import analyzer
    rgb, kind = analyzer.load_rgb(photo, max_side=max_side or 4096)
    info.update(source=kind, bits=8, size=[rgb.shape[1], rgb.shape[0]])
    return rgb.astype(np.float32) / 255.0, info


def _fit(img: np.ndarray, max_side: int | None) -> np.ndarray:
    if not max_side:
        return img
    import cv2
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s >= 1.0:
        return img
    return cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)


def save_tiff(path, rgb01: np.ndarray, quality: int = 90) -> None:
    """写 16 位 TIFF（LZW 压缩）。"""
    import cv2
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    u16 = np.uint16(np.clip(rgb01, 0, 1) * 65535 + 0.5)
    ok, buf = cv2.imencode(".tif", cv2.cvtColor(u16, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_TIFF_COMPRESSION, 5])   # 5 = LZW
    if not ok:
        raise IOError("TIFF 编码失败")
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    tmp.replace(path)


def read_bit_depth(path) -> int:
    """读回文件实际位深（PIL 会把 16bit RGB TIFF 降成 8bit，故用 OpenCV 原样读）。"""
    import cv2
    arr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise IOError(f"无法读取 {path}")
    return 16 if arr.dtype == np.uint16 else 8
