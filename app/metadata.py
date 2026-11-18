"""通过 ExifTool 批量读取元数据，结果按照片 id 缓存在 SQLite。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import DB_PATH, EXIFTOOL
from .scanner import Photo

TAGS = [
    "DateTimeOriginal", "SubSecTimeOriginal", "SubSecDateTimeOriginal", "CreateDate",
    "Make", "Model", "SerialNumber", "InternalSerialNumber", "LensModel", "LensID",
    "FocalLength", "ExposureTime", "FNumber", "ISO", "Orientation#",
    "ImageWidth", "ImageHeight", "SequenceNumber", "ReleaseMode", "ReleaseMode2",
    "DriveMode", "FocusMode", "AFAreaMode", "FocusLocation", "FocusPosition2", "FocusFrameSize",
    "AFPointSelected", "AFTracking", "ShutterCount",
]
CACHE_VERSION = 3
CHUNK = 250

_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS meta (id TEXT PRIMARY KEY, v INTEGER, data TEXT)")
    return con


def exiftool_available() -> tuple[bool, str]:
    try:
        out = subprocess.run(["/usr/bin/perl", str(EXIFTOOL), "-ver"],
                             capture_output=True, text=True, timeout=20)
        return out.returncode == 0, out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def run_exiftool(paths: list[Path], extra: Optional[list[str]] = None) -> list[dict]:
    if not paths:
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        for p in paths:
            f.write(str(p) + "\n")
        argfile = f.name
    try:
        cmd = ["/usr/bin/perl", str(EXIFTOOL), "-json", "-fast", "-charset", "filename=utf8",
               "-api", "largefilesupport=1", "-q", "-q"]
        cmd += [f"-{t}" for t in TAGS]
        cmd += extra or []
        cmd += ["-@", argfile]
        out = subprocess.run(cmd, capture_output=True, timeout=600)
        txt = out.stdout.decode("utf-8", errors="replace").strip()
        if not txt:
            return []
        return json.loads(txt)
    finally:
        os.unlink(argfile)


def _norm(rec: dict) -> dict:
    rec = {k: v for k, v in rec.items() if k != "SourceFile"}
    for k in ("SubSecTimeOriginal", "SerialNumber", "InternalSerialNumber"):
        if k in rec and rec[k] is not None:
            rec[k] = str(rec[k])
    return rec


def parse_datetime(meta: dict) -> tuple[Optional[datetime], bool]:
    raw = meta.get("DateTimeOriginal") or meta.get("CreateDate")
    if not raw or not isinstance(raw, str) or raw.startswith("0000"):
        return None, False
    try:
        dt = datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, False
    sub = None
    sst = meta.get("SubSecTimeOriginal")
    if sst:
        digits = "".join(ch for ch in str(sst) if ch.isdigit())
        if digits:
            sub = digits
    if sub is None:
        sdt = meta.get("SubSecDateTimeOriginal")
        if isinstance(sdt, str) and len(sdt) > 20 and sdt[19] == ".":
            digits = ""
            for ch in sdt[20:]:
                if not ch.isdigit():
                    break
                digits += ch
            sub = digits or None
    if sub:
        frac = float("0." + sub)
        return dt.replace(microsecond=min(int(round(frac * 1_000_000)), 999_999)), True
    return dt, False


def _apply(photo: Photo, meta: dict) -> None:
    photo.meta = meta
    photo.dt, photo.has_subsec = parse_datetime(meta)


def load_metadata(photos: Iterable[Photo],
                  progress: Optional[Callable[[int, int, str], None]] = None) -> None:
    photos = list(photos)
    total = len(photos)
    with _db_lock:
        con = _db()
        cached: dict[str, dict] = {}
        ids = [p.id for p in photos]
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            q = f"SELECT id, data FROM meta WHERE v=? AND id IN ({','.join('?' * len(part))})"
            for pid, data in con.execute(q, [CACHE_VERSION, *part]):
                cached[pid] = json.loads(data)
        con.close()

    todo = []
    for p in photos:
        if p.id in cached:
            _apply(p, cached[p.id])
        else:
            todo.append(p)
    done = total - len(todo)
    if progress:
        progress(done, total, f"读取元数据 {done}/{total}")

    new_rows: list[tuple[str, int, str]] = []
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        recs = run_exiftool([p.primary for p in chunk])
        by_path = {os.path.normcase(os.path.realpath(r.get("SourceFile", ""))): r for r in recs}
        retry: list[Photo] = []
        for p in chunk:
            rec = by_path.get(os.path.normcase(os.path.realpath(p.primary)), {})
            _apply(p, _norm(rec))
            if p.dt is None and p.display != p.primary:
                retry.append(p)
        # RAW 里没读到时间时，退回同名 JPG 再读一次
        if retry:
            recs2 = run_exiftool([p.display for p in retry])
            by2 = {os.path.normcase(os.path.realpath(r.get("SourceFile", ""))): r for r in recs2}
            for p in retry:
                rec = by2.get(os.path.normcase(os.path.realpath(p.display)))
                if rec:
                    merged = {**_norm(rec), **{k: v for k, v in p.meta.items() if v not in (None, "")}}
                    _apply(p, merged)
        for p in chunk:
            new_rows.append((p.id, CACHE_VERSION, json.dumps(p.meta, ensure_ascii=False)))
        done += len(chunk)
        if progress:
            progress(done, total, f"读取元数据 {done}/{total}")

    if new_rows:
        with _db_lock:
            con = _db()
            con.executemany("INSERT OR REPLACE INTO meta (id, v, data) VALUES (?,?,?)", new_rows)
            con.commit()
            con.close()
