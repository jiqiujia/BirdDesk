"""端到端测试：扫描 → 分组 → 预览 → 移动 → 再扫描（幂等）→ 撤销 → 与初始状态一致。

    BIRDDESK_DATA_DIR=/tmp/bd_data python tests/test_flow.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_data_"))

from fastapi.testclient import TestClient  # noqa: E402

from app import server  # noqa: E402
from tests.make_samples import main as make_samples  # noqa: E402


def snapshot(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.stat().st_size
    return out


def wait(c: TestClient) -> dict:
    for _ in range(600):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            assert not j["error"], j["error"]
            return j
        time.sleep(0.05)
    raise TimeoutError


def main():
    samples = Path(tempfile.mkdtemp(prefix="bd_samples_")) / "鸟片"
    make_samples(samples)
    before = snapshot(samples)
    c = TestClient(server.app)

    assert c.get("/api/env").json()["exiftool"]
    c.post("/api/regroup", json={"gap": 0.5, "min_burst": 2, "split_by_camera": True})
    c.post("/api/scan", json={"root": str(samples), "recursive": True})
    wait(c)
    lib = c.get("/api/library").json()
    st = lib["stats"]
    print("stats:", {k: st[k] for k in ("photos", "days", "bursts", "burst_photos", "singles", "undated", "pairs", "no_subsec", "other_files")})
    assert st["photos"] == 28, st
    assert st["undated"] == 1
    assert st["other_files"] == 1          # MP4
    bursts = {b["id"]: b["count"] for d in lib["days"] for b in d["items"] if b["type"] == "burst"}
    print("bursts:", bursts)
    assert bursts == {
        "2026-10-30/burst_083215": 12,
        "2026-10-30/burst_083215_2": 2,     # 另一台机身
        "2026-10-30/burst_090000": 3,
        "2026-10-31/burst_100000": 2,       # 无毫秒，1 秒容差
        "2026-10-31/burst_235959": 3,       # 跨午夜，整组归前一天
    }, bursts
    days = [d["date"] for d in lib["days"]]
    assert days == ["2026-10-30", "2026-10-31", "2026-11-01"], days

    # 缩略图（JPG）
    any_id = next(iter(lib["photos"]))
    r = c.get(f"/thumb/{any_id}?s=360")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"

    # 整理预览：不应动任何文件
    plan = c.post("/api/plan", json={"rename_conflicts": True}).json()
    s = plan["summary"]
    print("plan:", s)
    assert snapshot(samples) == before
    assert s["renamed"] == 1 and s["conflicts"] == 0
    dsts = {m["dst_rel"] for m in plan["moves"]}
    assert "2026-10-30/burst_083215/DSC00001.ARW" in dsts
    assert "2026-10-30/DSC00013.ARW" in dsts
    assert "2026-10-30/DSC00013_2.ARW" in dsts and "2026-10-30/DSC00013_2.JPG" in dsts
    assert "2026-10-30/DSC00018.ARW.xmp" in dsts
    assert "unknown-date/IMG_0001.JPG" in dsts
    assert "2026-11-01/IMG_0002.JPG" in dsts

    # 执行
    c.post("/api/execute", json={"plan_id": plan["plan_id"], "cleanup_empty": True})
    job = wait(c)
    res = job["result"]
    print("execute:", res)
    assert res["error"] is None and res["moved"] == s["files_to_move"]
    after = snapshot(samples)
    assert sorted(after.values()) == sorted(before.values())          # 文件一个不少
    assert "DCIM/100MSDCF/C0001.MP4" in after                           # 视频没动
    assert not (samples / "网球场边").exists()                           # 空文件夹已清理
    assert (samples / "2026-10-30" / "burst_083215" / "DSC00012.JPG").exists()

    # 再次扫描 + 预览：应该全部已在原位（幂等）
    lib2 = c.get("/api/library").json()
    assert lib2["stats"]["photos"] == 28
    plan2 = c.post("/api/plan", json={"rename_conflicts": True}).json()
    print("plan again:", plan2["summary"]["files_to_move"], "in_place:", plan2["summary"]["in_place"])
    assert plan2["summary"]["files_to_move"] == 0

    # 撤销
    jid = res["journal_id"]
    c.post("/api/undo", json={"journal_id": jid})
    ures = wait(c)["result"]
    print("undo:", {k: ures[k] for k in ("restored", "removed_dirs", "error")}, "skipped:", len(ures["skipped"]))
    assert snapshot(samples) == before, "撤销后应与初始完全一致"
    assert not (samples / "2026-10-30").exists()
    j = c.get("/api/journals").json()[0]
    assert j["undone"]
    # 重复撤销应报错
    r = c.post("/api/undo", json={"journal_id": jid})
    wait_err = None
    for _ in range(100):
        jj = c.get("/api/status").json()["job"]
        if not jj["running"]:
            wait_err = jj["error"]
            break
        time.sleep(0.05)
    assert wait_err and "撤销过" in wait_err

    # 调整连拍阈值：0.2 秒时 0.3 秒间隔的那组应拆开
    lib3 = c.post("/api/regroup", json={"gap": 0.2}).json()
    b3 = {b["id"] for d in lib3["days"] for b in d["items"] if b["type"] == "burst"}
    assert "2026-10-30/burst_090000" not in b3, b3
    c.post("/api/regroup", json={"gap": 0.5})
    print("ALL PASSED")


if __name__ == "__main__":
    main()
