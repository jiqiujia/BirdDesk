"""缩略图 / 大图预览，缓存到 Application Support。RAW 优先取内嵌 JPEG，不做解码。"""
from __future__ import annotations

import io
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps

from .config import EXIFTOOL, JPG_EXTS, RAW_EXTS, THUMB_DIR
from .scanner import Photo

try:
    import rawpy  # type: ignore
except Exception:  # noqa: BLE001
    rawpy = None

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

_ORIENT = {3: Image.Transpose.ROTATE_180, 6: Image.Transpose.ROTATE_270, 8: Image.Transpose.ROTATE_90}


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _raw_embedded(path: Path) -> Image.Image:
    if rawpy is not None:
        try:
            with rawpy.imread(str(path)) as raw:
                th = raw.extract_thumb()
            if th.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(th.data))
            return Image.fromarray(th.data)
        except Exception:  # noqa: BLE001
            pass
    for tag in ("-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"):
        out = subprocess.run(["/usr/bin/perl", str(EXIFTOOL), "-b", tag, str(path)],
                             capture_output=True, timeout=60)
        if out.returncode == 0 and len(out.stdout) > 1000:
            return Image.open(io.BytesIO(out.stdout))
    try:
        return Image.open(path)   # 某些 TIFF 系 RAW / 未知格式 Pillow 能直接打开
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(f"无法读取 RAW 预览：{path.name}")


def _open(photo: Photo, want: int) -> Image.Image:
    src = photo.display
    ext = src.suffix.lower()
    if ext in RAW_EXTS:
        img = _raw_embedded(src)
        img.draft("RGB", (want, want))
        orient = photo.meta.get("Orientation")
        if isinstance(orient, int) and orient in _ORIENT:
            img = img.transpose(_ORIENT[orient])
        return img
    img = Image.open(src)
    if ext in JPG_EXTS:
        img.draft("RGB", (want, want))   # JPEG 解码时直接降采样，快很多
    return ImageOps.exif_transpose(img)


def render(photo: Photo, size: int) -> Path:
    size = int(size)
    out = THUMB_DIR / f"{photo.id}_{size}.jpg"
    if out.exists():
        return out
    with _lock(out.name):
        if out.exists():
            return out
        img = _open(photo, size)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS if size <= 800 else Image.Resampling.BICUBIC)
        tmp = out.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=84 if size <= 800 else 90, optimize=size <= 800)
        tmp.rename(out)
    return out
