"""P3 鸟种识别测试：名录数据、裁切、阈值分档、XMP 关键词往返、接口。

    BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_species.py /tmp/birdsp
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_sp_"))

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import analyzer, ratings, scanner, server, species  # noqa: E402


def wait(c, key="job"):
    for _ in range(3000):
        j = c.get("/api/status").json()
        if key == "job":
            if j["job"] and not j["job"]["running"]:
                assert not j["job"]["error"], j["job"]["error"]
                return j["job"]
        else:
            a = j["analysis"]
            if not a["running"] and a["done"] == a["total"]:
                return a
        time.sleep(0.1)
    raise TimeoutError


def main(eval_dir: Path | None):
    # 1. 名录数据
    idx = species.load_index()
    sp = idx["species"]
    assert len(sp) == idx["count"] >= 1400, len(sp)
    assert all(s["latin"] and s["zh"] for s in sp)
    assert len({s["zh"] for s in sp}) == len(sp), "中文名应唯一"
    assert len({s["latin"] for s in sp}) == len(sp), "学名应唯一"
    print(f"名录 OK：{len(sp)} 种，版本 {idx['version']}")

    # 2. 裁切：太小返回 None，正常返回方形区域
    clf = species.Classifier()
    rgb = np.zeros((400, 600, 3), np.uint8)
    assert clf._crop(rgb, [10, 10, 40, 40]) is None
    c = clf._crop(rgb, [100, 100, 300, 260])
    assert c is not None and c.shape[0] == c.shape[1] or c.shape[0] > 100
    print("裁切 OK")

    # 3. 状态阈值（与评估口径一致）
    def status_of(p1):
        return "ok" if p1 >= species.P_OK else ("low_confidence" if p1 >= species.P_WEAK else "uncertain")
    assert status_of(0.95) == "ok" and status_of(0.4) == "low_confidence" and status_of(0.05) == "uncertain"
    print("阈值分档 OK", species.P_OK, species.P_WEAK)

    # 4. 真实推理（可选，需要模型）
    if eval_dir and species.model_available():
        import cv2
        det = analyzer.Detector()
        truth = json.loads((eval_dir / "truth.json").read_text("utf-8"))
        hits5 = n = 0
        t0 = time.time()
        for name, meta in sorted(truth.items()):
            f = eval_dir / name
            if not f.exists():
                continue
            bgr = cv2.imread(str(f))
            if bgr is None:
                continue
            rgb2 = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            birds = det.detect(rgb2)
            if not birds:
                continue
            r = species.classifier().classify(rgb2, birds[0]["box"])
            top = r.get("top") or []
            ok = top and (top[0]["latin"] == meta["latin"] or top[0]["zh"] == meta["zh"])
            in5 = any(t["latin"] == meta["latin"] or t["zh"] == meta["zh"] for t in top)
            hits5 += in5
            n += 1
            print(f"  {'✓' if ok else ('~' if in5 else '✗')} {meta['zh']:8} → {(top[0]['zh'] if top else '-'):10} "
                  f"p={r.get('p1')} [{r.get('status')}]")
        print(f"真实推理：top-5 {hits5}/{n}，耗时 {time.time()-t0:.1f}s")
        assert hits5 / n >= 0.85, f"top-5 命中率过低：{hits5}/{n}"

    # 5. XMP 关键词往返 + 接口
    tmp = Path(tempfile.mkdtemp()) / "sp"; tmp.mkdir()
    src = eval_dir / "truth.json" if eval_dir else None
    if eval_dir:
        imgs = [p for p in eval_dir.glob("*.jpg")][:1]
        for p in imgs:
            (tmp / p.name).write_bytes(p.read_bytes())
    if not list(tmp.glob("*.jpg")):
        print("跳过接口测试（无样本）")
        print("ALL PASSED")
        return
    c = TestClient(server.app)
    c.post("/api/settings", json={"auto_species": False, "strictness": "normal"})
    c.post("/api/scan", json={"root": str(tmp)})
    wait(c)
    lib = c.get("/api/library").json()
    pid = next(iter(lib["photos"]))
    r = c.post("/api/species/confirm", json={"ids": [pid], "zh": "黑鸢", "latin": "Milvus migrans"})
    assert r.status_code == 200, r.text
    xmp = list(tmp.glob("*.xmp"))[0]
    text = xmp.read_text("utf-8")
    assert "黑鸢" in text and "Milvus migrans" in text, text[:200]
    lib2 = c.get("/api/library").json()
    assert lib2["photos"][pid]["spc"] == "黑鸢"
    # 重复确认不产生重复关键词
    c.post("/api/species/confirm", json={"ids": [pid], "zh": "黑鸢", "latin": "Milvus migrans"})
    assert ratings.read_keywords(scanner.scan(tmp).photos[0]).count("黑鸢") == 1
    print("XMP 关键词与确认接口 OK")
    print("ALL PASSED")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
