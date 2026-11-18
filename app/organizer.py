"""整理计划 / 执行移动 / 操作日志 / 撤销。

安全原则：
- 先出计划，确认后才执行；执行前逐个复核源文件存在、目标不存在，绝不覆盖。
- 每移动一个文件就写一行 JSONL 日志并 fsync，即使中途崩溃也能撤销已完成的部分。
- 跨磁盘移动 = 复制到临时文件 → 校验大小 → 改名 → 删除源文件。
- 遇到任何错误立即停止。
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import JOURNAL_DIR, REJECT_FOLDER, UNDATED_FOLDER
from .scanner import Photo

IGNORABLE = {".DS_Store", "Thumbs.db", "desktop.ini"}


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _norm(a) == _norm(b)


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- plan
def make_plan(photos: list[Photo], source_root: Path, target_root: Path,
              rename_conflicts: bool = True, reject_ids: Optional[set[str]] = None) -> dict:
    target_root = target_root.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    reject_ids = reject_ids or set()
    claimed: set[str] = set()
    moves: list[dict] = []
    conflicts: list[dict] = []
    in_place = 0
    renamed = 0
    move_photos = 0
    total_bytes = 0

    ordered = sorted(photos, key=lambda p: (p.ts if p.ts is not None else float("inf"), p.stem))
    # 先占住"已在正确位置"的文件，避免别的照片被规划到同一路径
    targets: dict[str, Path] = {}
    for p in ordered:
        dest_dir = target_root / (p.day or UNDATED_FOLDER)
        if p.id in reject_ids:
            dest_dir = dest_dir / REJECT_FOLDER
        if p.burst:
            dest_dir = dest_dir / p.burst
        targets[p.id] = dest_dir
        if all(_norm(f.parent) == _norm(dest_dir) for f in p.files):
            for f in p.files:
                claimed.add(_norm(f))

    for p in ordered:
        dest_dir = targets[p.id]
        if all(_norm(f.parent) == _norm(dest_dir) for f in p.files):
            in_place += 1
            continue

        def candidates(stem: str) -> list[tuple[Path, Path]]:
            return [(f, dest_dir / (stem + f.name[len(p.stem):])) for f in p.files]

        def blocked(pairs: list[tuple[Path, Path]]) -> bool:
            for src, dst in pairs:
                if _norm(dst) in claimed:
                    return True
                if dst.exists() and not _same(src, dst):
                    return True
            return False

        pairs = candidates(p.stem)
        new_stem = p.stem
        if blocked(pairs):
            if not rename_conflicts:
                conflicts.append({"photo_id": p.id, "name": p.primary.name,
                                  "dst": _rel(pairs[0][1], target_root)})
                continue
            n = 2
            while True:
                new_stem = f"{p.stem}_{n}"
                pairs = candidates(new_stem)
                if not blocked(pairs):
                    break
                n += 1
            renamed += 1

        move_photos += 1
        for src, dst in pairs:
            claimed.add(_norm(dst))
            if _norm(src) == _norm(dst):
                continue
            sz = src.stat().st_size
            total_bytes += sz
            moves.append({
                "photo_id": p.id, "src": str(src), "dst": str(dst), "size": sz,
                "renamed": new_stem != p.stem,
                "src_rel": _rel(src, source_root), "dst_rel": _rel(dst, target_root),
            })

    cross = False
    try:
        cross = os.stat(source_root).st_dev != os.stat(_existing_parent(target_root)).st_dev
    except OSError:
        pass
    free = shutil.disk_usage(_existing_parent(target_root)).free

    day_set = {p.day for p in photos if p.day}
    burst_set = {(p.day, p.burst) for p in photos if p.burst}
    return {
        "plan_id": uuid.uuid4().hex[:12],
        "created": time.time(),
        "source_root": str(source_root),
        "target_root": str(target_root),
        "moves": moves,
        "conflicts": conflicts,
        "summary": {
            "photos": len(photos),
            "photos_to_move": move_photos,
            "files_to_move": len(moves),
            "in_place": in_place,
            "renamed": renamed,
            "conflicts": len(conflicts),
            "days": len(day_set),
            "bursts": len(burst_set),
            "undated": sum(1 for p in photos if not p.day),
            "rejected": sum(1 for p in photos if p.id in reject_ids),
            "bytes": total_bytes,
            "cross_device": cross,
            "free_bytes": free,
            "enough_space": (not cross) or free > total_bytes * 1.05,
        },
    }


def _existing_parent(p: Path) -> Path:
    while not p.exists():
        p = p.parent
    return p


# ---------------------------------------------------------------- move
def _move(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(f"目标已存在，拒绝覆盖：{dst}")
    if not src.exists():
        raise FileNotFoundError(f"源文件不存在：{src}")
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    # 跨磁盘：复制 → 校验 → 改名 → 删除源
    tmp = dst.with_name(dst.name + ".birddesk-part")
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(src, tmp)
    if tmp.stat().st_size != src.stat().st_size:
        tmp.unlink()
        raise IOError(f"复制校验失败：{src}")
    os.rename(tmp, dst)
    os.remove(src)


def _mkdirs(d: Path, created: list[Path]) -> None:
    missing = []
    cur = d
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    for m in reversed(missing):
        m.mkdir()
        created.append(m)


def _is_effectively_empty(d: Path) -> bool:
    try:
        return all(e.name in IGNORABLE for e in os.scandir(d))
    except OSError:
        return False


def _remove_empty_dir(d: Path) -> bool:
    if not d.is_dir() or not _is_effectively_empty(d):
        return False
    for e in os.scandir(d):
        os.remove(e.path)
    d.rmdir()
    return True


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.f = open(path, "a", encoding="utf-8")

    def write(self, rec: dict) -> None:
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()


def execute(plan: dict, cleanup_empty: bool = True,
            progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    moves = plan["moves"]
    source_root = Path(plan["source_root"])
    jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + plan["plan_id"][:4]
    journal = Journal(JOURNAL_DIR / f"{jid}.jsonl")
    journal.write({"type": "header", "id": jid, "created": time.time(),
                   "source_root": plan["source_root"], "target_root": plan["target_root"],
                   "planned": len(moves), "photos": plan["summary"]["photos_to_move"]})
    done = 0
    error = None
    src_dirs: set[Path] = set()
    try:
        for i, m in enumerate(moves):
            src, dst = Path(m["src"]), Path(m["dst"])
            created: list[Path] = []
            _mkdirs(dst.parent, created)
            for c in created:
                journal.write({"type": "mkdir", "path": str(c)})
            _move(src, dst)
            journal.write({"type": "move", "src": str(src), "dst": str(dst)})
            src_dirs.add(src.parent)
            done += 1
            if progress and (i % 10 == 0 or i == len(moves) - 1):
                progress(done, len(moves), f"正在移动 {done}/{len(moves)}：{dst.name}")
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        journal.write({"type": "error", "message": error, "at": done})

    removed = 0
    if cleanup_empty and error is None:
        root_n = _norm(source_root)
        for d in sorted(src_dirs, key=lambda x: -len(str(x))):
            cur = d
            while _norm(cur).startswith(root_n + os.sep) and _norm(cur) != root_n:
                if _remove_empty_dir(cur):
                    journal.write({"type": "rmdir", "path": str(cur)})
                    removed += 1
                    cur = cur.parent
                else:
                    break

    journal.write({"type": "end", "status": "error" if error else "ok",
                   "moved": done, "removed_dirs": removed, "finished": time.time()})
    journal.close()
    return {"journal_id": jid, "moved": done, "planned": len(moves),
            "removed_dirs": removed, "error": error}


# ---------------------------------------------------------------- journals / undo
def _read_journal(jid: str) -> list[dict]:
    path = JOURNAL_DIR / f"{jid}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"找不到操作日志：{jid}")
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass   # 崩溃时最后一行可能不完整
    return out


def list_journals() -> list[dict]:
    res = []
    for p in sorted(JOURNAL_DIR.glob("*.jsonl"), reverse=True):
        jid = p.stem
        try:
            recs = _read_journal(jid)
        except Exception:  # noqa: BLE001
            continue
        head = next((r for r in recs if r["type"] == "header"), {})
        if head.get("kind") == "trash":      # 删除日志由 trash 模块单独列出
            continue
        end = next((r for r in recs if r["type"] == "end"), None)
        undo_path = JOURNAL_DIR / f"{jid}.undo.json"
        undo = json.loads(undo_path.read_text("utf-8")) if undo_path.exists() else None
        res.append({
            "id": jid,
            "created": head.get("created"),
            "source_root": head.get("source_root"),
            "target_root": head.get("target_root"),
            "moved": sum(1 for r in recs if r["type"] == "move"),
            "status": end["status"] if end else "incomplete",
            "error": next((r["message"] for r in recs if r["type"] == "error"), None),
            "undone": undo is not None,
            "undo": undo,
        })
    return res


def undo(jid: str, progress: Optional[Callable[[int, int, str], None]] = None) -> dict:
    undo_path = JOURNAL_DIR / f"{jid}.undo.json"
    if undo_path.exists():
        raise RuntimeError("这次整理已经撤销过了")
    recs = _read_journal(jid)
    moves = [r for r in recs if r["type"] == "move"]
    mkdirs = [Path(r["path"]) for r in recs if r["type"] == "mkdir"]
    restored, skipped = 0, []
    error = None
    for i, m in enumerate(reversed(moves)):
        src, dst = Path(m["src"]), Path(m["dst"])
        try:
            if not dst.exists():
                skipped.append({"file": str(dst), "reason": "整理后的文件已不存在"})
                continue
            if src.exists():
                skipped.append({"file": str(src), "reason": "原位置已有同名文件"})
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            _move(dst, src)
            restored += 1
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            break
        if progress and (i % 10 == 0 or i == len(moves) - 1):
            progress(i + 1, len(moves), f"正在还原 {i + 1}/{len(moves)}")
    removed = 0
    if error is None:
        for d in sorted(set(mkdirs), key=lambda x: -len(str(x))):
            if _remove_empty_dir(d):
                removed += 1
    result = {"finished": time.time(), "restored": restored, "skipped": skipped,
              "removed_dirs": removed, "error": error}
    if error is None:
        undo_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), "utf-8")
    return result
