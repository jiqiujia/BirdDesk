"""在真实鸟片 + 人工退化版本上标定合焦阈值。

    .venv/bin/python tests/calibrate_focus.py /tmp/birdcal
"""
import glob
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import focus  # noqa: E402
from ultralytics import YOLO  # noqa: E402

MODEL = Path(__file__).resolve().parent.parent / "models" / "yolo11m-seg.pt"


def motion(img, length=15, angle=0):
    k = np.zeros((length, length), np.float32)
    k[length // 2, :] = 1
    M = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    k = cv2.warpAffine(k, M, (length, length))
    k /= k.sum()
    return cv2.filter2D(img, -1, k)


def variants(rgb, rng, mask=None, other=None):
    f = rgb.astype(np.float32) / 255
    if mask is not None and other is not None:
        o = cv2.resize(other, (f.shape[1], f.shape[0])).astype(np.float32) / 255
        mm = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 2)[..., None]
        yield "bg+sh", f * mm + o * (1 - mm), "sharp"
        yield "bg+bf", cv2.GaussianBlur(f, (0, 0), 2.5) * mm + o * (1 - mm), "missed"
    yield "orig", f, "sharp"
    yield "noise", np.clip(f + rng.normal(0, 0.03, f.shape).astype(np.float32), 0, 1), "sharp"
    yield "up2x", cv2.resize(cv2.resize(f, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), None, fx=1, fy=1), "sharp"
    yield "g1.2", cv2.GaussianBlur(f, (0, 0), 1.2), "soft"
    yield "g2.5", cv2.GaussianBlur(f, (0, 0), 2.5), "blur"
    yield "g4", cv2.GaussianBlur(f, (0, 0), 4), "blur"
    yield "mot18", motion(f, 19, 20), "motion"
    yield "g2.5+n", np.clip(cv2.GaussianBlur(f, (0, 0), 2.5) + rng.normal(0, 0.03, f.shape).astype(np.float32), 0, 1), "blur"


def main(d):
    model = YOLO(str(MODEL))
    rng = np.random.default_rng(1)
    rows = []
    for p in sorted(glob.glob(f"{d}/*.jpg")):
        bgr = cv2.imread(p)
        rgb = bgr[:, :, ::-1].copy()
        r = model.predict(rgb, device="mps", imgsz=1024, classes=[14], conf=0.3, verbose=False, retina_masks=True)[0]
        if not len(r.boxes):
            continue
        i = int(r.boxes.conf.argmax())
        box = [int(v) for v in r.boxes.xyxy[i].tolist()]
        mask = r.masks.data[i].cpu().numpy() > 0.5 if r.masks is not None else None
        other = cv2.imread(sorted(glob.glob(f"{d}/*.jpg"))[3])[:, :, ::-1].copy()
        for name, img, expect in variants(rgb, rng, mask, other):
            if img.shape[:2] != rgb.shape[:2]:
                s = img.shape[0] / rgb.shape[0]
                b = [int(v * s) for v in box]
                m = cv2.resize(mask.astype(np.uint8), (img.shape[1], img.shape[0]), interpolation=cv2.INTER_NEAREST) if mask is not None else None
            else:
                b, m = box, mask
            g = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
            res = focus.assess(g, b, m)
            rows.append((Path(p).name, name, expect, res))
    bad = 0
    for f, n, e, r in rows:
        ok = r.grade == e or (e == "blur" and r.grade in ("blur", "motion")) or (e == "motion" and r.grade in ("motion", "blur", "soft")) or (e == "missed" and r.grade in ("missed",))
        if not ok:
            bad += 1
            print(f"✗ {f:8} {n:7} 期望 {e:6} 实际 {r.grade:7} sharp {r.sharp:.3f} peak {r.peak:.3f} bg {r.bg:.3f} anis {r.anis:.2f} noise {r.noise:.1f}")
    print()
    for name in ["bg+sh", "bg+bf", "orig", "noise", "up2x", "g1.2", "g2.5", "g4", "mot18", "g2.5+n"]:
        v = [r for _, n, _, r in rows if n == name]
        s = np.array([r.sharp for r in v])
        an = np.array([r.anis for r in v])
        from collections import Counter
        print(f"{name:7} sharp {s.min():.2f}/{np.median(s):.2f}/{s.max():.2f}  anis {an.min():.2f}/{np.median(an):.2f}/{an.max():.2f}  {dict(Counter(r.grade for r in v))}")
    print(f"\n误判 {bad}/{len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/birdcal")
