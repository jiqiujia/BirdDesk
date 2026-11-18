"""扫描目录，把同名的 RAW / JPG / XMP 归并成一张"照片"。"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import EXPORT_FOLDER, IMAGE_EXTS, VIDEO_FOLDER, JPG_EXTS, RAW_EXTS, SIDECAR_EXTS


@dataclass
class Photo:
    id: str
    stem: str
    dir: Path
    files: list[Path]               # 该照片的全部文件（RAW、JPG、XMP…）
    primary: Path                   # 读取元数据的文件（优先 RAW）
    display: Path                   # 生成缩略图的文件（优先 JPG）
    size: int = 0                   # 所有文件总字节数
    # 以下由 metadata 填充
    dt: Optional[datetime] = None   # 拍摄时间（相机本地时间，含亚秒）
    has_subsec: bool = False
    meta: dict = field(default_factory=dict)
    # 以下由 grouping 填充
    day: Optional[str] = None
    burst: Optional[str] = None
    gap_prev: Optional[float] = None

    @property
    def exts(self) -> list[str]:
        out = []
        for f in self.files:
            e = f.suffix.lower()
            if e in SIDECAR_EXTS:
                continue
            out.append(e.lstrip(".").upper())
        return sorted(set(out))

    @property
    def ts(self) -> Optional[float]:
        if self.dt is None:
            return None
        return (self.dt - datetime(1970, 1, 1)).total_seconds()

    @property
    def camera_key(self) -> str:
        m = self.meta
        return f"{m.get('Model', '')}|{m.get('SerialNumber') or m.get('InternalSerialNumber') or ''}"


def _logical_stem(p: Path) -> Optional[str]:
    """DSC001.ARW -> DSC001；DSC001.ARW.xmp -> DSC001；DSC001.xmp -> DSC001"""
    ext = p.suffix.lower()
    if ext in SIDECAR_EXTS:
        inner = Path(p.stem)
        if inner.suffix.lower() in IMAGE_EXTS:
            return inner.stem
        return p.stem
    if ext in IMAGE_EXTS:
        return p.stem
    return None


def _photo_id(primary: Path, st: os.stat_result) -> str:
    # 名称 + 大小 + 修改时间：文件被移动后仍保持不变，用作缓存键
    key = f"{primary.name}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class ScanResult:
    root: Path
    photos: list[Photo]
    other_files: int        # 非照片文件（视频等），不会被处理
    orphan_sidecars: int


def scan(root: Path, recursive: bool = True,
         progress: Optional[Callable[[int, str], None]] = None) -> ScanResult:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")

    buckets: dict[tuple[str, str], list[Path]] = {}
    other = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录（含 .birddesk、.Trashes 等）与自己的出片目录
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d not in (EXPORT_FOLDER, VIDEO_FOLDER))
        if not recursive:
            dirnames[:] = []
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            stem = _logical_stem(p)
            if stem is None:
                other += 1
                continue
            buckets.setdefault((dirpath, stem.lower()), []).append(p)
            count += 1
            if progress and count % 200 == 0:
                progress(count, f"已发现 {count} 个文件")

    photos: list[Photo] = []
    orphans = 0
    for (dirpath, _), files in buckets.items():
        images = [f for f in files if f.suffix.lower() in IMAGE_EXTS]
        if not images:
            orphans += len(files)
            continue
        raws = [f for f in images if f.suffix.lower() in RAW_EXTS]
        jpgs = [f for f in images if f.suffix.lower() in JPG_EXTS]
        primary = raws[0] if raws else (jpgs[0] if jpgs else images[0])
        display = jpgs[0] if jpgs else images[0]
        try:
            st = primary.stat()
            total = sum(f.stat().st_size for f in files)
        except OSError:
            continue
        photos.append(Photo(
            id=_photo_id(primary, st),
            stem=primary.stem,
            dir=Path(dirpath),
            files=sorted(files, key=lambda x: x.name),
            primary=primary,
            display=display,
            size=total,
        ))
    photos.sort(key=lambda p: (str(p.dir), p.stem))
    return ScanResult(root=root, photos=photos, other_files=other, orphan_sidecars=orphans)
