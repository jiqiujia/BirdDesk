"""删除/恢复测试：移到废纸篓、日志、恢复回原位、库计数变化。

    BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_trash.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_trash_"))

from fastapi.testclient import TestClient  # noqa: E402

from app import server, trash  # noqa: E402

SRC = Path("/Users/qiujiaji/WorkBuddy/2026-10-31-19-04-22/demo_photos/真鸟连拍/DCIM/100MSDCF")


def wait(c, key="job"):
    for _ in range(900):
        j = c.get("/api/status").json()
        if not j[key]["running"]:
            return j[key]
        time.sleep(0.1)
    raise TimeoutError


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def main():
    if not SRC.exists():
        print("缺少示例目录，跳过")
        return
    if not trash.available():
        print("当前环境不支持系统废纸篓（NSFileManager 不可用），跳过")
        return

    root = Path(tempfile.mkdtemp()) / "lib"
    (root / "DCIM").mkdir(parents=True)
    files = sorted(SRC.glob("*.JPG"))[:3]
    for f in files:
        shutil.copy(f, root / "DCIM" / f.name)
    before = snapshot(root)

    c = TestClient(server.app)
    c.post("/api/settings", json={"auto_species": False})
    c.post("/api/scan", json={"root": str(root)})
    wait(c)
    lib = c.get("/api/library").json()
    ids = sorted(lib["photos"].keys(), key=lambda k: lib["photos"][k]["stem"])
    print("库内照片:", len(ids))
    assert len(ids) == 3

    victim = ids[0]
    name = lib["photos"][victim]["name"]
    r = c.post("/api/trash", json={"ids": [victim], "note": "测试"})
    assert r.status_code == 200, r.text[:200]
    res = wait(c)["result"]
    print("删除结果:", {k: res[k] for k in ("photos", "files", "moved")}, "错误数", len(res["errors"]))
    assert res["moved"] >= 1 and res["photos"] == 1

    # 源目录里该照片已消失；库计数减少
    assert not (root / "DCIM" / name).exists(), "源文件应已移走"
    lib2 = c.get("/api/library").json()
    assert len(lib2["photos"]) == 2, len(lib2["photos"])
    assert victim not in lib2["photos"]

    # 日志里有记录，且记录到废纸篓路径
    js = c.get("/api/journals").json()
    entry = next(j for j in js if j["kind"] == "trash")
    print("删除日志:", entry["photos"], "张", entry["moved"], "个文件，状态", entry["status"])
    assert entry["moved"] >= 1
    jpath = Path(os.environ["BIRDDESK_DATA_DIR"]) / "journals" / f"{entry['id']}.jsonl"
    recs = [json.loads(x) for x in jpath.read_text("utf-8").splitlines() if x.strip()]
    trashed = [x for x in recs if x["type"] == "trash"]
    assert all(x.get("trashed_to") for x in trashed), trashed
    assert any("Trash" in (x.get("trashed_to") or "") or "废纸篓" in (x.get("trashed_to") or "")
               for x in trashed), trashed

    # 恢复：文件回到原路径，字节一致，库恢复
    r = c.post("/api/trash/undo", json={"journal_id": entry["id"]})
    assert r.status_code == 200, r.text[:200]
    res2 = wait(c)["result"]
    print("恢复结果:", res2["restored"], "个文件，跳过", len(res2["skipped"]))
    assert res2["restored"] >= 1
    assert (root / "DCIM" / name).exists(), "文件应被恢复"
    after = snapshot(root)
    assert after.get(f"DCIM/{name}") == before[f"DCIM/{name}"], "恢复后应字节一致"
    lib3 = c.get("/api/library").json()
    assert len(lib3["photos"]) == 3, len(lib3["photos"])
    # 重复恢复应被拒绝
    r = c.post("/api/trash/undo", json={"journal_id": entry["id"]})
    err = wait(c)["error"]
    assert err and "恢复过" in err, err
    print("ALL PASSED")


if __name__ == "__main__":
    main()
