"""P4 出片测试：降噪指标、蒙版、分区处理、预览/导出接口、原片不被改动。

    BIRDDESK_DATA_DIR=$(mktemp -d) .venv/bin/python tests/test_develop.py
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
os.environ.setdefault("BIRDDESK_DATA_DIR", tempfile.mkdtemp(prefix="bd_dev_"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import analyzer, denoise, develop, raw_io, scanner, server  # noqa: E402

# 示例照片：用环境变量指定，避免把本机路径写进仓库
SAMPLE_DIR = Path(os.environ.get("BIRDDESK_SAMPLE_DIR", "/tmp/birdcal/sample/100MSDCF"))
SRC = SAMPLE_DIR / "DSC00010.JPG"


def psnr(a, b):
    return 10 * np.log10(1.0 / max(float(np.mean((a - b) ** 2)), 1e-12))


def main():
    if not SRC.exists():
        print("缺少测试图片，跳过")
        return
    rgb = cv2.cvtColor(cv2.imread(str(SRC)), cv2.COLOR_BGR2RGB)

    # 1. AI 降噪：加噪图 PSNR 必须显著提升
    if denoise.available():
        d = denoise.denoiser()
        assert d.load(), d.error
        clean = rgb[:384, :384].astype(np.float32) / 255
        rng = np.random.default_rng(0)
        noisy = np.clip(clean + rng.normal(0, 0.03, clean.shape).astype(np.float32), 0, 1)
        out = d.run(noisy)
        before, after = psnr(noisy, clean), psnr(out, clean)
        print(f"降噪 PSNR {before:.2f} → {after:.2f} dB（设备 {d.device}）")
        assert after - before > 8, (before, after)
    else:
        print("无降噪模型，跳过降噪测试")

    # 2. 蒙版：覆盖率为画面的一部分，且边缘羽化
    det = analyzer.Detector()
    mask, note = develop.build_mask(rgb, det)
    assert mask is not None, note
    cov = float(mask.mean())
    print(f"蒙版覆盖率 {cov:.3f}")
    assert 0.02 < cov < 0.6
    fm = develop.feather(mask, max(rgb.shape[:2]), 0.002)
    assert fm.min() >= 0 and fm.max() <= 1

    # 3. 分区处理：主体亮于背景，且背景被柔化
    p = develop.Params.preset("standard")
    p.denoise = "off"
    t0 = time.time()
    out, rep = develop.develop(rgb, p, det=det)
    dt = time.time() - t0
    print(f"出片（无降噪）{dt:.1f}s | 步骤 {'/'.join(rep['steps'])}")
    assert out.shape == rgb.shape and out.dtype == np.uint8
    m3 = fm[:, :, None] > 0.5
    subj = float(out[m3.repeat(3, 2)].mean())
    bgm = (fm < 0.05)[:, :, None].repeat(3, 2)
    bg = float(out[bgm].mean())
    print(f"主体均值 {subj:.1f} / 背景均值 {bg:.1f}")
    assert subj > bg + 10, "主体应明显亮于被压暗的背景"
    assert not np.array_equal(out, rgb), "处理结果不应与原图相同"

    # 4. 预设与开关
    for name in ("light", "standard", "strong"):
        q = develop.Params.preset(name)
        o, _ = develop.develop(rgb[:400, :400], q, det=det)
        assert o.shape == (400, 400, 3)
    q = develop.Params.preset("standard"); q.denoise = "off"
    q.texture = q.clarity = q.shadows = q.highlights = q.eye = 0
    q.bg_darken = q.bg_desat = q.bg_blur = q.bg_denoise = q.vignette = 0
    same, _ = develop.develop(rgb[:300, :300], q, det=det)
    print("全关时与原图最大差异", int(np.abs(same.astype(int) - rgb[:300, :300].astype(int)).max()))

    # 5. 接口：预览 + 导出，原片不变
    tmp = Path(tempfile.mkdtemp()) / "dev"; (tmp / "DCIM").mkdir(parents=True)
    shutil.copy(SRC, tmp / "DCIM" / SRC.name)
    before_bytes = (tmp / "DCIM" / SRC.name).read_bytes()
    c = TestClient(server.app)
    c.post("/api/settings", json={"auto_species": False})
    c.post("/api/scan", json={"root": str(tmp)})
    for _ in range(600):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            assert not j["error"], j["error"]
            break
        time.sleep(0.1)
    lib = c.get("/api/library").json()
    pid = next(iter(lib["photos"]))
    r = c.post("/api/develop/preview", json={"pid": pid, "preset": "standard", "denoise": "off"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", r.text[:200]
    meta = json.loads(r.headers["X-Dev-Meta"])
    print("预览 OK", meta.get("ms"), "ms", meta.get("steps"))
    r2 = c.post("/api/develop/export", json={"ids": [pid], "preset": "standard", "denoise": "off"})
    assert r2.status_code == 200, r2.text[:200]
    for _ in range(900):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            break
        time.sleep(0.2)
    res = j["result"]
    assert res["ok"] == 1, res
    outfile = Path(res["items"][0]["path"])
    assert outfile.exists() and "_出片" in outfile.parts, outfile
    assert outfile.stat().st_size > 10_000
    assert (tmp / "DCIM" / SRC.name).read_bytes() == before_bytes, "原片被改动！"
    print("导出 OK", outfile, f"{outfile.stat().st_size // 1024} KB")

    # 6. 16 位 TIFF 导出：位深真的 16，且带 RAW 源信息
    r3 = c.post("/api/develop/export", json={"ids": [pid], "preset": "light",
                                            "denoise": "off", "bit_depth": 16})
    assert r3.status_code == 200, r3.text[:200]
    for _ in range(900):
        j = c.get("/api/status").json()["job"]
        if j and not j["running"]:
            break
        time.sleep(0.2)
    res3 = j["result"]
    assert res3["ok"] == 1, res3
    tif = Path(res3["items"][0]["path"])
    assert tif.suffix.lower() == ".tif", tif
    from app import raw_io
    assert raw_io.read_bit_depth(tif) == 16, raw_io.read_bit_depth(tif)
    arr = cv2.imread(str(tif), cv2.IMREAD_UNCHANGED)
    levels = len(np.unique(arr))
    print(f"16 位 TIFF OK：{tif.stat().st_size // 1024 // 1024} MB，{levels} 个不同层级，源={res3['items'][0].get('source')}")
    assert levels > 20000, levels

    # 7. 有真实 ARW 时验证 RAW 解码路径（可选）
    arw = Path("/tmp/arw2/rx.ARW")
    if arw.exists():
        d2 = Path(tempfile.mkdtemp()) / "raw"; d2.mkdir()
        shutil.copy(arw, d2 / "DSC9999.ARW")
        c.post("/api/scan", json={"root": str(d2)})
        for _ in range(900):
            jj = c.get("/api/status").json()["job"]
            if jj and not jj["running"]:
                break
            time.sleep(0.1)
        pid2 = next(iter(c.get("/api/library").json()["photos"]))
        t0 = time.time()
        rr = c.post("/api/develop/export", json={"ids": [pid2], "preset": "light",
                                               "denoise": "off", "bit_depth": 16})
        assert rr.status_code == 200, rr.text[:200]
        for _ in range(1800):
            jj = c.get("/api/status").json()["job"]
            if jj and not jj["running"]:
                break
            time.sleep(0.2)
        item = jj["result"]["items"][0]
        assert "error" not in item, item
        assert item["source"] == "raw", item
        tif2 = Path(item["path"])
        a2 = cv2.imread(str(tif2), cv2.IMREAD_UNCHANGED)
        print(f"RAW 路径 OK：{a2.shape[1]}x{a2.shape[0]} {a2.dtype}，耗时 {time.time() - t0:.0f}s")
        assert a2.dtype == np.uint16
    else:
        print("（无 /tmp/arw2/rx.ARW，跳过 RAW 解码验证）")
    # 8. 出片目录不能被再次扫描成照片
    lib2 = c.get("/api/library").json()
    assert "_出片" not in str(lib2.get("root")), lib2.get("root")
    names = [p["name"] for p in lib2["photos"].values()]
    assert not any("_出片" in n for n in names), names[:5]
    print("出片目录已从扫描中排除")

    # 9. RAW 域降噪：在真实 ARW 的线性数据上加已知噪声，看 PSNR 提升
    if arw.exists():
        from app import rawdenoise
        d3 = Path(tempfile.mkdtemp()) / "raw2"; d3.mkdir()
        shutil.copy(arw, d3 / "DSC8888.ARW")
        ph3 = scanner.scan(d3).photos[0]
        lin, linfo = raw_io.load_linear(ph3)
        assert lin is not None, linfo
        aT, bT = 0.0016, 2e-6
        rng2 = np.random.default_rng(7)
        noisy = np.clip(lin + rng2.normal(0, 1, lin.shape) * np.sqrt(aT * lin + bT), 0, 1).astype(np.float32)
        est_a, est_b, diag = rawdenoise.estimate_noise(noisy)
        print(f"噪声模型：估计 a={est_a:.5f} / 真值 {aT}，b={est_b:.2e}，分箱 {diag.get('bins')}")
        assert aT / 3 < est_a < aT * 3, (est_a, aT)
        t0 = time.time()
        den, dinfo = rawdenoise.denoise_linear(noisy, 1.0)
        dt = time.time() - t0
        before, after = psnr(noisy, lin), psnr(den, lin)
        print(f"RAW 域降噪：{before:.2f} → {after:.2f} dB（{dt:.1f}s，{lin.shape[1]}x{lin.shape[0]}）")
        assert after - before > 2.5, (before, after)
        assert den.shape == lin.shape and den.max() <= 1.0
    else:
        print("（无 ARW，跳过 RAW 域降噪验证）")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
