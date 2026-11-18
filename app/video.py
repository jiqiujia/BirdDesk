"""连拍 → 视频（升格 / 逐帧动画）。

"升格"在这里的含义：把连拍里连续的帧按较高帧率逐帧播放，于是原本 0.3 秒的瞬间
被拉成几秒钟，能看到翅膀扇动、抖动这些肉眼错过的细节。

实现要点
- 编码用 imageio-ffmpeg 自带的 ffmpeg（libx264 / yuv420p / faststart），
  QuickTime、照片 App、微信都能直接播放；没有 ffmpeg 时退回 OpenCV 的 avc1。
- 尺寸先按长边上限缩一次，再统一成偶数宽高（yuv420p 要求），避免逐帧编码失败。
- 帧来自现有的 load_rgb（RAW 用内嵌 JPEG，速度快），不做降噪/调色——这一步是"看动作"，
  要成片质量请走出片流程。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

from . import analyzer
from .config import VIDEO_FOLDER
from .scanner import Photo

FFMPEG = None


def ffmpeg() -> Optional[str]:
    """优先用 imageio-ffmpeg 自带的可执行文件，其次系统的 ffmpeg。"""
    global FFMPEG
    if FFMPEG is not None:
        return FFMPEG or None
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return FFMPEG
    except Exception:  # noqa: BLE001
        pass
    FFMPEG = shutil.which("ffmpeg") or ""
    return FFMPEG or None


def plan_sizes(rgb: np.ndarray, max_long: int) -> tuple[int, int]:
    h, w = rgb.shape[:2]
    s = min(1.0, max_long / max(h, w)) if max_long else 1.0
    W = max(2, int(round(w * s)) // 2 * 2)
    Hh = max(2, int(round(h * s)) // 2 * 2)
    return W, Hh


def _fit(rgb: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    W, Hh = size
    if (rgb.shape[1], rgb.shape[0]) == (W, Hh):
        return rgb
    interp = cv2.INTER_AREA if rgb.shape[1] > W else cv2.INTER_CUBIC
    return cv2.resize(rgb, (W, Hh), interpolation=interp)


def render(photos: list[Photo], out_path: Path, fps: int = 24, max_long: int = 1920,
           progress: Optional[Callable[[int, int, str], None]] = None,
           fade: float = 0.0, slow: float = 4.0,
           src_fps: Optional[float] = None, repeat: Optional[int] = None) -> dict:
    """把 photos（按拍摄顺序）编码成一个 MP4。

    **升格的关键是重复帧**：把每张源帧在时间轴上重复 N 次，N 由"慢放倍数 / 原始帧率"
    决定。这样输出仍是 24/30fps 的顺滑视频，但动作被拉长——而不是简单地把帧率调低
    （那样画面会一顿一顿，看着像幻灯片）。
    """
    if not photos:
        raise ValueError("没有可用的帧")
    fps = max(2, min(120, int(fps)))

    frames: list[np.ndarray] = []
    size = None
    total = len(photos)
    for i, ph in enumerate(photos):
        rgb, _ = analyzer.load_rgb(ph, max_side=max(max_long, 2560))
        if size is None:
            size = plan_sizes(rgb, max_long)
        frames.append(_fit(rgb, size))
        if progress:
            progress(i + 1, total + 1, f"读取第 {i + 1}/{total} 帧")
    src = src_fps if src_fps and src_fps > 0.5 else 10.0     # 连拍通常 10–30 fps
    # 前端若按"目标时长"算好了重复次数，直接用（避免二次取整导致 3 秒变 4 秒）
    if repeat is None:
        repeat = max(1, int(round(slow * fps / src)))
    repeat = max(1, int(repeat))
    if repeat > 1:
        frames = [f for f in frames for _ in range(repeat)]
    if fade > 0 and len(frames) > 1:
        frames = _apply_fade(frames, fade)

    W, Hh = size
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    exe = ffmpeg()
    if exe:
        _encode_ffmpeg(exe, frames, out_path, W, Hh, fps)
        encoder = "ffmpeg/libx264"
    else:
        _encode_cv2(frames, out_path, W, Hh, fps)
        encoder = "opencv/avc1"
    if progress:
        progress(total + 1, total + 1, "完成")
    return {"path": str(out_path), "frames": len(frames), "source_frames": total,
            "fps": fps, "repeat": repeat, "slow": round(slow, 2),
            "src_fps": round(src, 1), "seconds": round(len(frames) / fps, 2),
            "size": [W, Hh], "bytes": out_path.stat().st_size, "encoder": encoder}


def _apply_fade(frames: list[np.ndarray], seconds: float) -> list[np.ndarray]:
    """对首尾做短暂淡入淡出，循环播放时不跳。"""
    import math
    n = max(1, int(round(seconds * 24)))
    n = min(n, len(frames) // 3)
    if n <= 0:
        return frames
    out = list(frames)
    for i in range(n):
        k = 0.5 - 0.5 * math.cos(math.pi * (i + 1) / (n + 1))
        out[i] = np.uint8(out[i].astype(np.float32) * k)
        out[-1 - i] = np.uint8(out[-1 - i].astype(np.float32) * k)
    return out


def _encode_ffmpeg(exe: str, frames: list[np.ndarray], out: Path, W: int, Hh: int, fps: int) -> None:
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{Hh}", "-r", str(fps), "-i", "-",
           "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for f in frames:
            p.stdin.write(np.ascontiguousarray(f).tobytes())
        p.stdin.close()
    except BrokenPipeError:
        pass
    err = p.stderr.read().decode("utf-8", "ignore")
    if p.wait() != 0:
        raise RuntimeError(f"ffmpeg 编码失败：{err.strip()[:300]}")


def _encode_cv2(frames: list[np.ndarray], out: Path, W: int, Hh: int, fps: int) -> None:
    vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"avc1"), fps, (W, Hh))
    if not vw.isOpened():
        vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, Hh))
    if not vw.isOpened():
        raise RuntimeError("没有可用的视频编码器")
    try:
        for f in frames:
            vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    finally:
        vw.release()
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("视频写入失败")


def output_path(root: Path, burst_day: str, burst_name: str, tag: str = "4x") -> Path:
    """`<root>/_视频/<日期>_<连拍名>_<倍数>.mp4`，不覆盖已有文件（自动加 _2）。"""
    d = Path(root) / VIDEO_FOLDER
    base = f"{burst_day}_{burst_name}_{tag}"
    p = d / f"{base}.mp4"
    n = 2
    while p.exists():
        p = d / f"{base}_{n}.mp4"
        n += 1
    return p


def probe(path: Path) -> dict:
    """读取视频元信息（帧数/帧率/尺寸），用于自检与测试。"""
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    return {"frames": n, "fps": round(fps, 2), "size": [w, h],
            "seconds": round(n / fps, 2) if fps else 0}
