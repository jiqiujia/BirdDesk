"""用真实鸟片构造"连拍"测试集：每组包含清晰/偏软/失焦/运动模糊帧，用于验证合焦分级和连拍择优。

    python tests/make_bird_samples.py /tmp/birdcal OUT_DIR
"""
import glob
import json
import random
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

EXIF_IFD = 0x8769


def motion(img, length=19, angle=20):
    k = np.zeros((length, length), np.float32)
    k[length // 2, :] = 1
    M = cv2.getRotationMatrix2D((length / 2 - 0.5, length / 2 - 0.5), angle, 1)
    k = cv2.warpAffine(k, M, (length, length))
    return cv2.filter2D(img, -1, k / k.sum())


FRAMES = {
    "sharp": lambda f: f,
    "soft": lambda f: cv2.GaussianBlur(f, (0, 0), 1.3),
    "blur": lambda f: cv2.GaussianBlur(f, (0, 0), 3.0),
    "motion": lambda f: motion(f),
    "noisy_sharp": lambda f: np.clip(f + np.random.default_rng(3).normal(0, 6, f.shape), 0, 255),
}


def save(path: Path, rgb: np.ndarray, dt: datetime, model="ILCE-1"):
    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    exif = Image.Exif()
    exif[0x010F] = "SONY"
    exif[0x0110] = model
    ifd = exif.get_ifd(EXIF_IFD)
    ifd[0x9003] = dt.strftime("%Y:%m:%d %H:%M:%S")
    ifd[0x9291] = f"{dt.microsecond // 1000:03d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=92, exif=exif.tobytes())


def main(src: str, out: Path):
    if out.exists():
        shutil.rmtree(out)
    files = sorted(glob.glob(f"{src}/*.jpg"))
    rnd = random.Random(7)
    truth = {"bursts": [], "singles": {}}
    n = 1
    t0 = datetime(2026, 10, 25, 7, 30, 0)
    card = out / "DCIM" / "100MSDCF"
    for bi, f in enumerate(files[:5]):
        rgb = cv2.imread(f)[:, :, ::-1].astype(np.float32)
        kinds = list(FRAMES)
        rnd.shuffle(kinds)
        start = t0 + timedelta(minutes=7 * bi)
        burst = []
        for i, k in enumerate(kinds):
            stem = f"DSC{n:05d}"
            n += 1
            save(card / f"{stem}.JPG", FRAMES[k](rgb), start + timedelta(milliseconds=100 * i))
            burst.append({"stem": stem, "kind": k})
        truth["bursts"].append({"src": Path(f).name, "frames": burst})
    for i, f in enumerate(files[5:9]):
        rgb = cv2.imread(f)[:, :, ::-1].astype(np.float32)
        k = ["sharp", "blur", "sharp", "soft"][i]
        stem = f"DSC{n:05d}"
        n += 1
        save(card / f"{stem}.JPG", FRAMES[k](rgb), t0 + timedelta(hours=2, minutes=3 * i))
        truth["singles"][stem] = k
    # 一张没有鸟的风景
    land = np.full((1280, 1920, 3), 120, np.uint8)
    cv2.putText(land, "no bird here", (300, 640), cv2.FONT_HERSHEY_SIMPLEX, 5, (240, 240, 240), 12)
    stem = f"DSC{n:05d}"
    save(card / f"{stem}.JPG", land, t0 + timedelta(hours=3))
    truth["singles"][stem] = "nobird"
    (out.parent / f"{out.name}_truth.json").write_text(json.dumps(truth, indent=1), "utf-8")
    print(f"bird samples -> {out}")


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
