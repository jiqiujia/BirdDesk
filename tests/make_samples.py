"""生成带 EXIF 的模拟鸟片，用于测试分组 / 移动 / 撤销。

    python tests/make_samples.py /tmp/birddesk_samples
"""
import random
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

EXIF_IFD = 0x8769


def make_jpg(path: Path, label: str, dt: datetime | None, subsec: bool = True,
             model: str = "ILCE-1", serial: str = "1234567", seed: int = 0):
    rnd = random.Random(seed)
    w, h = 1200, 800
    img = Image.new("RGB", (w, h), (rnd.randint(90, 140), rnd.randint(120, 170), rnd.randint(80, 120)))
    d = ImageDraw.Draw(img)
    for _ in range(25):
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        d.ellipse([x - 90, y - 90, x + 90, y + 90], fill=(rnd.randint(60, 150), rnd.randint(100, 170), rnd.randint(60, 120)))
    bx, by = 500 + rnd.randint(-60, 60), 380 + rnd.randint(-30, 30)
    d.ellipse([bx - 120, by - 60, bx + 120, by + 60], fill=(200, 120, 40))
    d.ellipse([bx + 80, by - 90, bx + 160, by - 20], fill=(210, 130, 50))
    d.ellipse([bx + 128, by - 66, bx + 140, by - 54], fill=(10, 10, 10))
    d.polygon([(bx + 158, by - 60), (bx + 200, by - 52), (bx + 158, by - 44)], fill=(40, 40, 40))
    d.rectangle([20, 20, 520, 80], fill=(255, 255, 255))
    d.text((32, 36), label, fill=(0, 0, 0), font_size=32)

    exif = Image.Exif()
    exif[0x010F] = "SONY"
    exif[0x0110] = model
    if dt is not None:
        ifd = exif.get_ifd(EXIF_IFD)
        ifd[0x9003] = dt.strftime("%Y:%m:%d %H:%M:%S")
        if subsec:
            ifd[0x9291] = f"{dt.microsecond // 1000:03d}"
        ifd[0xA431] = serial            # BodySerialNumber
        ifd[0x8827] = 1600              # ISO
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=85, exif=exif.tobytes())


def pair(dirp: Path, stem: str, dt, **kw):
    """RAW+JPG：这里的 .ARW 只是占位（内容=JPG），用来测试配对与"RAW 读不到时间时退回 JPG"。"""
    jpg = dirp / f"{stem}.JPG"
    make_jpg(jpg, f"{stem} {dt:%H:%M:%S.%f}"[:-3] if dt else stem, dt, **kw)
    shutil.copyfile(jpg, dirp / f"{stem}.ARW")


def main(out: Path):
    if out.exists():
        shutil.rmtree(out)
    c1 = out / "DCIM" / "100MSDCF"
    c2 = out / "DCIM" / "101MSDCF"
    n = 1

    def stem():
        nonlocal n
        s = f"DSC{n:05d}"
        n += 1
        return s

    # Day 1：一组 12 张高速连拍（0.1s）
    t = datetime(2026, 10, 30, 8, 32, 15, 0)
    for i in range(12):
        pair(c1, stem(), t + timedelta(milliseconds=100 * i), seed=i)
    # 单张
    pair(c1, stem(), datetime(2026, 10, 30, 8, 40, 0, 250_000), seed=50)
    # 一组 3 张低速连拍（0.3s）+ 1.4 秒后一张单拍
    t = datetime(2026, 10, 30, 9, 0, 0, 0)
    for i in range(3):
        pair(c1, stem(), t + timedelta(milliseconds=300 * i), seed=60 + i)
    pair(c1, stem(), t + timedelta(milliseconds=2000), seed=70)
    # 带 XMP 旁注的单张
    s = stem()
    pair(c1, s, datetime(2026, 10, 30, 10, 15, 3, 0), seed=80)
    (c1 / f"{s}.ARW.xmp").write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'/>", "utf-8")

    # Day 2：跨午夜连拍（应整组归到 10-31）
    t = datetime(2026, 10, 31, 23, 59, 59, 700_000)
    for i in range(3):
        pair(c1, stem(), t + timedelta(milliseconds=200 * i), seed=90 + i)
    # 无毫秒信息的两张 JPG，相差 1 秒 → 放宽到 1 秒判断为连拍
    for i in range(2):
        make_jpg(c1 / f"{stem()}.JPG", "no-subsec", datetime(2026, 10, 31, 10, 0, i), subsec=False, seed=100 + i)

    # 第二张卡：文件号与 08:40 那张单拍重复（同一天）→ 应自动重命名为 DSC00013_2
    pair(c2, "DSC00013", datetime(2026, 10, 30, 15, 20, 0, 0), model="ILCE-9M3", serial="999", seed=200)
    # 另一台机身同一时刻连拍：不应与 ILCE-1 混成一组
    for i in range(2):
        pair(c2, f"DSC2{i:04d}", datetime(2026, 10, 30, 8, 32, 15, 50_000 + i * 100_000), model="ILCE-9M3", serial="999", seed=210 + i)

    # 中文文件夹 + 无 EXIF 时间
    make_jpg(out / "网球场边" / "IMG_0001.JPG", "no exif", None, seed=300)
    make_jpg(out / "网球场边" / "IMG_0002.JPG", "cn folder", datetime(2026, 11, 1, 7, 5, 5, 500_000), seed=301)
    # 非照片文件（不处理）
    (c1 / "C0001.MP4").write_bytes(b"\x00" * 1024)
    (out / ".hidden").mkdir()
    make_jpg(out / ".hidden" / "skip.JPG", "hidden", datetime(2026, 10, 30, 1, 0, 0), seed=400)
    print(f"samples -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/birddesk_samples"))
