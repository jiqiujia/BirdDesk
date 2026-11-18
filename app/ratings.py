"""星级 / 淘汰标记，写入 XMP 旁注文件（Lightroom / Capture One / Bridge 兼容）。

- 评级 -1 = 淘汰（Lightroom 的 Rejected），0 = 无星，1~5 星。
- RAW 的旁注文件名：DSC00001.xmp（Lightroom 约定）；若已存在 DSC00001.ARW.xmp（darktable 约定）则沿用。
- 已有旁注文件：用 ExifTool 只改 Rating，不动其他内容（比如 Lightroom 写入的调色参数）。
- 绝不修改 RAW / JPG 原文件。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

from .config import EXIFTOOL, RAW_EXTS
from .scanner import Photo

_RATING_RE = re.compile(r"xmp:Rating\s*(?:=\s*[\"']|>)\s*(-?\d+)")

TEMPLATE = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="BirdDesk">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmp:Rating="{rating}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def sidecar_of(photo: Photo) -> Optional[Path]:
    for f in photo.files:
        if f.suffix.lower() == ".xmp":
            return f
    return None


def target_sidecar(photo: Photo) -> Path:
    ex = sidecar_of(photo)
    if ex is not None:
        return ex
    base = photo.primary if photo.primary.suffix.lower() in RAW_EXTS else photo.display
    return base.with_name(photo.stem + ".xmp")


def read_rating(photo: Photo) -> int:
    sc = sidecar_of(photo)
    if sc is None:
        return 0
    try:
        txt = sc.read_text("utf-8", errors="ignore")[:200_000]
    except OSError:
        return 0
    m = _RATING_RE.search(txt)
    if not m:
        return 0
    try:
        return max(-1, min(5, int(m.group(1))))
    except ValueError:
        return 0


def read_keywords(photo: Photo) -> list[str]:
    sc = sidecar_of(photo)
    if sc is None:
        return []
    try:
        txt = sc.read_text("utf-8", errors="ignore")[:300_000]
    except OSError:
        return []
    out = []
    for m in re.finditer(r"<rdf:li[^>]*>([^<]{1,40})</rdf:li>", txt):
        v = m.group(1).strip()
        if v and v not in out:
            out.append(v)
    return out


def write_keywords(photos: list[Photo], keywords: list[str]) -> list[str]:
    """把关键词并入 XMP dc:Subject（Lightroom 的关键字面板能看到），不写进原图。"""
    errors: list[str] = []
    keywords = [k.strip() for k in keywords if k and k.strip()]
    if not keywords:
        return errors
    for p in photos:
        sc = sidecar_of(p)
        tgt = sc or target_sidecar(p)
        try:
            if not tgt.exists():
                tgt.write_text(TEMPLATE.format(rating=0), "utf-8")
            cur = list(dict.fromkeys(read_keywords(p) + keywords))
            cmd = ["/usr/bin/perl", str(EXIFTOOL), "-q", "-q", "-overwrite_original", "-m"]
            # 列表型标签：重复赋值即写入整个列表（exiftool 语义）
            cmd += [f"-XMP-dc:Subject={k}" for k in cur]
            cmd.append(str(tgt))
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.returncode != 0:
                errors.append(f"{tgt.name}: {out.stderr.strip()[:160]}")
            elif tgt not in p.files:
                p.files = sorted(set(p.files) | {tgt}, key=lambda x: x.name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{p.primary.name}: {e}")
    return errors


def write_ratings(photos: list[Photo], rating: int) -> list[str]:
    """返回出错信息列表。成功写入后会把新建的旁注文件加入 photo.files，整理时随照片一起移动。"""
    rating = max(-1, min(5, int(rating)))
    errors: list[str] = []
    existing: list[Path] = []
    for p in photos:
        sc = sidecar_of(p)
        if sc is not None:
            existing.append(sc)
            continue
        tgt = target_sidecar(p)
        try:
            if tgt.exists():          # 理论上不会发生（扫描时已归入），防御性处理
                existing.append(tgt)
            else:
                tgt.write_text(TEMPLATE.format(rating=rating), "utf-8")
            p.files = sorted(set(p.files) | {tgt}, key=lambda x: x.name)
        except OSError as e:
            errors.append(f"{p.primary.name}: {e}")
    for i in range(0, len(existing), 200):
        part = existing[i:i + 200]
        cmd = ["/usr/bin/perl", str(EXIFTOOL), "-q", "-q", "-overwrite_original", "-m",
               f"-XMP-xmp:Rating={rating}", *map(str, part)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            errors.append(out.stderr.strip()[:300] or "ExifTool 写入失败")
    return errors
