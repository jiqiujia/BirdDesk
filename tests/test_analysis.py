"""P2 端到端测试：分析 → 分级 → 连拍择优 → 评级写 XMP → 一键淘汰 → 整理到 _淘汰 → 撤销。

    BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_analysis.py /tmp/birdcal
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_data_"))

from fastapi.testclient import TestClient  # noqa: E402

from app import analyzer, server  # noqa: E402
from tests.make_bird_samples import main as make  # noqa: E402

NOT_SHARP = {"soft", "blur", "motion", "missed"}


def wait_job(c):
    for _ in range(2000):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            assert not j["error"], j["error"]
            return j
        time.sleep(0.05)
    raise TimeoutError


def wait_analysis(c, timeout=300):
    t = time.time()
    while time.time() - t < timeout:
        a = c.get("/api/status").json()["analysis"]
        if not a["running"] and a["done"] == a["total"]:
            return a
        time.sleep(0.3)
    raise TimeoutError


def snapshot(root):
    return {str(p.relative_to(root)): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def main(src):
    base = Path(tempfile.mkdtemp(prefix="bd_birds_"))
    out = base / "鸟片"
    make(src, out)
    truth = json.loads((base / "鸟片_truth.json").read_text("utf-8"))
    before = snapshot(out)
    c = TestClient(server.app)
    c.post("/api/settings", json={"strictness": "normal", "auto_analyze": True})
    c.post("/api/regroup", json={"gap": 0.5, "min_burst": 2})

    # 方向换算 / 对焦点解析
    assert analyzer.af_point({"FocusLocation": "6000 4000 3000 1000", "Orientation": 1}) == (0.5, 0.25)
    assert analyzer.af_point({"FocusLocation": "6000 4000 3000 1000", "Orientation": 6}) == (0.75, 0.5)
    assert analyzer.af_point({"FocusLocation": "6000 4000 0 0"}) is None

    t0 = time.time()
    c.post("/api/scan", json={"root": str(out), "recursive": True})
    wait_job(c)
    a = wait_analysis(c)
    el = time.time() - t0
    lib = c.get("/api/library").json()
    n = lib["stats"]["photos"]
    print(f"analysis: {a['done']}/{a['total']} failed={a['failed']}  {el:.1f}s 总计（{el / n:.2f}s/张，含模型加载）")
    assert a["failed"] == 0, a
    P = lib["photos"]
    by_stem = {p["stem"]: p for p in P.values()}

    # 单张
    ok = 0
    for stem, kind in truth["singles"].items():
        g = by_stem[stem]["f"]["g"]
        good = (g == kind) or (kind == "nobird" and g == "nobird") or (kind in NOT_SHARP and g in NOT_SHARP)
        ok += good
        print(f"  single {stem} 真实={kind:11} 判定={g:7} 分数={by_stem[stem]['f']['s']}  {'✓' if good else '✗'}")

    # 连拍：最佳帧必须是两张清晰帧之一，模糊帧不能被判为合焦
    bursts = [it for d in lib["days"] for it in d["items"] if it["type"] == "burst"]
    assert len(bursts) == len(truth["bursts"]), len(bursts)
    best_ok = 0
    grade_ok = grade_n = 0
    for b, tb in zip(bursts, truth["bursts"]):
        kinds = {fr["stem"]: fr["kind"] for fr in tb["frames"]}
        best_stem = P[b["best"]]["stem"]
        rows = []
        for pid in b["photo_ids"]:
            p = P[pid]
            k, g = kinds[p["stem"]], p["f"]["g"]
            sharp_kind = k in ("sharp", "noisy_sharp")
            good = (sharp_kind and g == "sharp") or (not sharp_kind and g in NOT_SHARP)
            grade_ok += good
            grade_n += 1
            rows.append(f"{k}:{g}:{p['f']['s']}{'' if good else '✗'}")
        good_best = kinds[best_stem] in ("sharp", "noisy_sharp")
        best_ok += good_best
        print(f"  burst {tb['src']}: 最佳={kinds[best_stem]:11} {'✓' if good_best else '✗'}  合焦 {b['sharp']}/{b['count']}  | " + "  ".join(rows))
    print(f"连拍择优 {best_ok}/{len(bursts)}，逐帧分级 {grade_ok}/{grade_n}，单张 {ok}/{len(truth['singles'])}")
    assert best_ok == len(bursts)
    assert grade_ok >= grade_n - 1

    # 严格度切换：不重新分析，只改分级
    loose = c.post("/api/settings", json={"strictness": "loose"}).json()
    strict = c.post("/api/settings", json={"strictness": "strict"}).json()
    ns = lambda L: L["stats"]["grades"].get("sharp", 0)  # noqa: E731
    print(f"合焦张数：宽松 {ns(loose)} / 严格 {ns(strict)}")
    assert ns(loose) >= ns(strict)
    c.post("/api/settings", json={"strictness": "normal"})

    # 热力图 / 100% 裁切
    pid = bursts[0]["best"]
    r = c.get(f"/heat/{pid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    d = c.get(f"/api/photo/{pid}").json()
    fx, fy = d["analysis"]["focus"]["peak_xy"]
    r = c.get(f"/crop/{pid}?x={fx}&y={fy}")
    assert r.status_code == 200 and json.loads(r.headers["x-crop"])["full_w"] > 0
    assert d["burst_rank"] == 1

    # 一键淘汰：保留最佳，其余标记 -1，写入 XMP
    b0 = bursts[0]
    r = c.post("/api/burst/cull", json={"burst_id": b0["id"], "keep": 1}).json()
    assert r["rejected"] == b0["count"] - 1, r
    lib = c.get("/api/library").json()
    rej = [pid for pid in b0["photo_ids"] if lib["photos"][pid]["r"] == -1]
    assert len(rej) == b0["count"] - 1 and lib["photos"][b0["best"]]["r"] == 0
    xmps = sorted(out.rglob("*.xmp"))
    assert len(xmps) == b0["count"] - 1, xmps
    assert 'xmp:Rating="-1"' in xmps[0].read_text("utf-8")
    # 给最佳打 5 星，再改成 4 星（走 ExifTool 更新已有旁注）
    c.post("/api/rate", json={"ids": [b0["best"]], "rating": 5})
    c.post("/api/rate", json={"ids": [b0["best"]], "rating": 4})
    best_xmp = out / "DCIM" / "100MSDCF" / (P[b0["best"]]["stem"] + ".xmp")
    txt = best_xmp.read_text("utf-8")
    assert "Rating" in txt and ("4" in txt), txt
    # 重新扫描能读回评级
    c.post("/api/scan", json={"root": str(out)})
    wait_job(c)
    lib = c.get("/api/library").json()
    assert lib["photos"][b0["best"]]["r"] == 4
    assert lib["stats"]["rejected"] == b0["count"] - 1
    wait_analysis(c)

    # 整理：淘汰照片放进 _淘汰，XMP 跟着走
    plan = c.post("/api/plan", json={"rename_conflicts": True, "reject_to_folder": True}).json()
    dsts = {m["dst_rel"] for m in plan["moves"]}
    rej_stem = lib["photos"][rej[0]]["stem"]
    day = lib["photos"][rej[0]]["day"]
    bname = lib["photos"][rej[0]]["burst"]
    assert f"{day}/_淘汰/{bname}/{rej_stem}.JPG" in dsts, sorted(dsts)[:8]
    assert f"{day}/_淘汰/{bname}/{rej_stem}.xmp" in dsts
    c.post("/api/execute", json={"plan_id": plan["plan_id"]})
    res = wait_job(c)["result"]
    assert res["error"] is None
    assert (out / day / "_淘汰" / bname / f"{rej_stem}.JPG").exists()
    # 整理后缓存命中，不需要重新分析
    lib2 = c.get("/api/library").json()
    assert lib2["stats"]["analyzed"] == lib2["stats"]["photos"], lib2["stats"]
    plan2 = c.post("/api/plan", json={"rename_conflicts": True, "reject_to_folder": True}).json()
    assert plan2["summary"]["files_to_move"] == 0
    # 撤销 → 回到原状（多了 XMP 旁注）
    c.post("/api/undo", json={"journal_id": res["journal_id"]})
    wait_job(c)
    now = snapshot(out)
    assert {k: v for k, v in now.items() if not k.endswith(".xmp")} == before
    wait_analysis(c)
    print("ALL PASSED")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/birdcal")
