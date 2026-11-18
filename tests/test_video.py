"""升格视频测试：编码正确性、时长/帧数、目录规则、接口与扫描排除。

    BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_video.py
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
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_vid_"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import scanner, server, video  # noqa: E402
from tests.make_bird_samples import main as make_birds  # noqa: E402

CAL = Path("/tmp/birdcal")


def main():
    if not CAL.exists():
        print("缺少 /tmp/birdcal，跳过")
        return

    # 1. 编码器可用
    exe = video.ffmpeg()
    print("编码器:", "ffmpeg " + str(exe) if exe else "opencv（无 ffmpeg）")

    # 2. 直接调用 render：用带 EXIF 的合成连拍（5 帧一组，间隔 0.1s）
    lib = Path(tempfile.mkdtemp()) / "lib"
    make_birds(CAL, lib)
    burst_dirs = sorted({p.parent for p in lib.rglob("*.JPG")})
    src = burst_dirs[0]
    allp = sorted(scanner.scan(src).photos, key=lambda p: p.dt or 0)
    photos = allp[:5]                       # 取第一组连拍的 5 帧
    frames_src = [p.display for p in photos]
    print("测试帧:", src.name, len(allp), "张中取前 5 张，间隔 0.1s")
    out = Path(tempfile.mkdtemp()) / "t.mp4"
    res = video.render(photos, out, fps=30, max_long=1920, slow=4, src_fps=10)
    info = video.probe(out)
    print(f"render: 5 源帧 ×{res['repeat']} → {res['frames']} 帧 / {info['fps']} fps / {info['size']} / {info['seconds']}s / {res['bytes']//1024}KB [{res['encoder']}]")
    assert res["source_frames"] == 5 and info["frames"] == res["frames"], (res, info)
    assert abs(info["fps"] - 30) < 0.6, info
    assert info["size"][0] % 2 == 0 and info["size"][1] % 2 == 0
    # 4× 慢放：0.4 秒的连拍应变成约 2 秒
    assert 1.6 <= info["seconds"] <= 2.4, info
    assert res["repeat"] == 12, res

    # 3. 倍数越大时长越长；原始尺寸不应放大
    out1 = Path(tempfile.mkdtemp()) / "t1.mp4"
    r1 = video.render(photos, out1, fps=30, max_long=0, slow=1, src_fps=10)
    i1 = video.probe(out1)
    print(f"1×: {i1['seconds']}s/{r1['frames']}帧 · 4×: {info['seconds']}s/{res['frames']}帧")
    assert i1["seconds"] < info["seconds"] * 0.6
    src_w = cv2.imread(str(frames_src[0])).shape[1]
    assert i1["size"][0] <= src_w, (i1["size"], src_w)

    # 4. 输出命名与不覆盖
    root = lib
    p1 = video.output_path(root, "2026-10-30", "burst_083215", "4x")
    p1.parent.mkdir(parents=True, exist_ok=True)
    p1.write_bytes(b"x")
    p2 = video.output_path(root, "2026-10-30", "burst_083215", "4x")
    print("同名不覆盖:", p1.name, "→", p2.name)
    assert p2.name.endswith("_2.mp4")
    assert "_视频" in str(p1)

    # 5. 接口：连拍 → 视频；_视频 不进扫描
    c = TestClient(server.app)
    c.post("/api/settings", json={"auto_species": False})
    c.post("/api/scan", json={"root": str(root)})
    for _ in range(900):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            assert not j["error"], j["error"]
            break
        time.sleep(0.1)
    lib = c.get("/api/library").json()
    bursts = [it for d in lib["days"] for it in d["items"] if it["type"] == "burst"]
    print("识别到连拍:", len(bursts), [b["count"] for b in bursts])
    assert bursts, "应识别出一组连拍"
    bid = bursts[0]["id"]
    r = c.post("/api/burst/video", json={"burst_id": bid, "fps": 30, "slow": 4, "max_long": 1280})
    assert r.status_code == 200, r.text[:200]
    for _ in range(1200):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            break
        time.sleep(0.1)
    res = j["result"]
    assert "error" not in res, res
    vp = Path(res["path"])
    print("接口产出:", vp.name, res["source_frames"], "源帧 ×", res["repeat"], "→", res["frames"], "帧", res["fps"], "fps", res["seconds"], "s")
    assert vp.exists() and vp.suffix == ".mp4" and "_视频" in str(vp) and "_4x" in vp.name
    assert video.probe(vp)["frames"] == res["frames"]
    names = [p["name"] for p in c.get("/api/library").json()["photos"].values()]
    assert not any(n.endswith(".mp4") or "_视频" in n for n in names)
    print("_视频 已从扫描中排除")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
