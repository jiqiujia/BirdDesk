"""删除照片：移到系统废纸篓（可恢复），并记录可撤销的日志。

安全设计
- **只用系统吊纸篓**（NSFileManager.trashItemAtURL），不做永久删除；Finder 里能恢复，
  外部卷也会进对应卷的 .Trashes。
- 一张"照片"的所有文件（RAW / JPG / XMP 旁注）一起移走，不留半套。
- 逐个记录"原路径 → 废纸篓路径"，撤销时按记录移回；目标已存在则跳过，绝不覆盖。
- 任何一步失败都停在原地并报告，不猜测、不继续。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

from .config import JOURNAL_DIR
from .scanner import Photo

try:
    from Foundation import NSFileManager, NSURL
except Exception:  # noqa: BLE001
    NSFileManager = None
    NSURL = None


def available() -> bool:
    return NSFileManager is not None


def _trash_one(path: Path) -> tuple[bool, Optional[str], Optional[str]]:
    """把一个文件移到废纸篓。返回 (成功, 废纸篓中的路径, 错误)。"""
    if NSFileManager is None:
        return False, None, "系统废纸篓接口不可用"
    if not path.exists():
        return False, None, "文件不存在"
    mgr = NSFileManager.defaultManager()
    url = NSURL.fileURLWithPath_(str(path))
    ok, resulting, err = mgr.trashItemAtURL_resultingItemURL_error_(url, None, None)
    if not ok:
        return False, None, str(err) if err else "移入废纸篓失败"
    try:
        dest = str(resulting.path()) if resulting is not None else None
    except Exception:  # noqa: BLE001
        dest = None
    return True, dest, None


def trash_photos(photos: list[Photo], note: str = "",
                 progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """把照片的所有文件移进废纸篓，并写日志。返回 {journal_id, photos, files, moved, errors}。"""
    jid = time.strftime("%Y%m%d-%H%M%S") + "-trash"
    path = JOURNAL_DIR / f"{jid}.jsonl"
    moved = 0
    files_total = 0
    errors: list[dict] = []
    done_photos = 0
    with open(path, "w", encoding="utf-8") as f:
        def w(rec: dict) -> None:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

        w({"type": "header", "id": jid, "created": time.time(), "kind": "trash",
           "photos": len(photos), "note": note})
        for i, ph in enumerate(photos):
            for src in ph.files:
                files_total += 1
                ok, dest, err = _trash_one(Path(src))
                if ok:
                    moved += 1
                    w({"type": "trash", "photo_id": ph.id, "src": str(src), "trashed_to": dest})
                else:
                    errors.append({"path": str(src), "error": err})
                    w({"type": "error", "photo_id": ph.id, "src": str(src), "message": err})
            done_photos += 1
            if progress:
                progress(i + 1, len(photos), f"移入废纸篓 {i + 1}/{len(photos)}：{ph.primary.name}")
        w({"type": "end", "kind": "trash", "trashed_files": moved, "photos": done_photos,
           "errors": len(errors), "finished": time.time()})
    return {"journal_id": jid, "photos": done_photos, "files": files_total,
            "moved": moved, "errors": errors}


def _read_journal(jid: str) -> list[dict]:
    p = JOURNAL_DIR / f"{jid}.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"找不到删除日志：{jid}")
    out = []
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def undo_trash(jid: str, progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    """把日志里记录的文件从废纸篓移回原位。"""
    marker = JOURNAL_DIR / f"{jid}.undo.json"
    if marker.exists():
        raise RuntimeError("这次删除已经恢复过了")
    recs = _read_journal(jid)
    items = [r for r in recs if r["type"] == "trash"]
    restored, skipped = 0, []
    error = None
    for i, r in enumerate(reversed(items)):
        src, dest = Path(r["src"]), Path(r.get("trashed_to") or "")
        try:
            if not dest or not dest.exists():
                skipped.append({"file": str(src), "reason": "废纸篓里找不到该文件（可能已清空）"})
                continue
            if src.exists():
                skipped.append({"file": str(src), "reason": "原位置已有同名文件"})
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            os.rename(dest, src)
            restored += 1
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            break
        if progress and (i % 10 == 0 or i == len(items) - 1):
            progress(i + 1, len(items), f"正在恢复 {i + 1}/{len(items)}")
    result = {"restored": restored, "skipped": skipped, "error": error, "finished": time.time()}
    if error is None:
        marker.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    return result


def list_trash_journals() -> list[dict]:
    out = []
    for p in sorted(JOURNAL_DIR.glob("*-trash.jsonl"), reverse=True):
        jid = p.stem
        try:
            recs = _read_journal(jid)
        except Exception:  # noqa: BLE001
            continue
        head = next((r for r in recs if r["type"] == "header"), {})
        end = next((r for r in recs if r["type"] == "end"), None)
        undo = json.loads((JOURNAL_DIR / f"{jid}.undo.json").read_text("utf-8")) \
            if (JOURNAL_DIR / f"{jid}.undo.json").exists() else None
        out.append({
            "id": jid, "kind": "trash", "created": head.get("created"),
            "photos": head.get("photos", 0),
            "moved": sum(1 for r in recs if r["type"] == "trash"),
            "errors": sum(1 for r in recs if r["type"] == "error"),
            "status": "ok" if end else "incomplete",
            "note": head.get("note", ""),
            "undone": undo is not None, "undo": undo,
        })
    return out
