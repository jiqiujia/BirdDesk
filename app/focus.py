"""合焦评估：纯数值计算（numpy / OpenCV），不依赖模型。

思路
- 鸟体区域缩放到统一尺度（鸟的长边 ≤ ANALYSIS_LONG，不放大），切成小块（tile）。
- 锐利度 acutance = 高频能量 / 中频能量：与亮度、对比度无关。清晰的羽毛细节≈0.6~1+，失焦≈0.2。
- 噪声：各频段的"噪声底"直接取平坦块能量的低分位并扣除。真实相机噪声经过去马赛克、JPEG、
  缩放后是空间相关的，白噪声模型会严重低估，数据驱动更稳。
- 方向性：分别沿 0°/45°/90°/135° 计算锐利度。失焦是各向同性的；运动模糊只抹掉运动方向的细节，
  所以"最弱方向的锐利度"能同时抓住失焦和运动模糊，两者再用各向异性区分。
- 鸟体内最锐的一片区域（通常是眼睛/头部）作为合焦判断依据，周边背景用于判断跑焦。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import cv2
import numpy as np

ANALYSIS_LONG = 1024
ALGO_VERSION = 4

# 由 tests/calibrate_focus.py 在真实鸟片 + 人工退化样本上标定
T_SHARP = 0.52       # 最锐区域"最弱方向锐利度"≥ 此值 → 合焦
T_SOFT = 0.30        # 低于此值 → 失焦
T_ANIS = 0.60        # 方向一致性低于此值且不合焦 → 运动模糊
BACKFOCUS_RATIO = 1.3

GRADES = {
    "sharp": "合焦",
    "soft": "偏软",
    "blur": "失焦",
    "missed": "跑焦",
    "motion": "运动模糊",
    "nobird": "未检测到鸟",
}
DIRS = (0.0, np.pi / 4, np.pi / 2, 3 * np.pi / 4)


def _dir_kernel(sigma: float, theta: float) -> np.ndarray:
    n = int(sigma * 3.5) * 2 + 1
    k = np.zeros((n, n), np.float32)
    c = n // 2
    g = cv2.getGaussianKernel(n, sigma).ravel()
    for i in range(n):
        d = i - c
        x, y = c + d * np.cos(theta), c + d * np.sin(theta)
        x0, y0 = int(np.floor(x)), int(np.floor(y))
        fx, fy = x - x0, y - y0
        for xx, yy, w in ((x0, y0, (1 - fx) * (1 - fy)), (x0 + 1, y0, fx * (1 - fy)),
                          (x0, y0 + 1, (1 - fx) * fy), (x0 + 1, y0 + 1, fx * fy)):
            if 0 <= xx < n and 0 <= yy < n:
                k[yy, xx] += g[i] * w
    return k / k.sum()


_KERNELS = [(_dir_kernel(1.0, t), _dir_kernel(3.0, t)) for t in DIRS]


def _tile_mean(x: np.ndarray, t: int) -> np.ndarray:
    h, w = x.shape
    th, tw = h // t, w // t
    if th == 0 or tw == 0:
        return np.zeros((0, 0), np.float32)
    return x[: th * t, : tw * t].reshape(th, t, tw, t).mean(axis=(1, 3))


def _floor(e: np.ndarray) -> float:
    return float(np.percentile(e, 5)) if e.size else 0.0


@dataclass
class _Region:
    crop: np.ndarray
    cov: np.ndarray       # 每块被鸟覆盖的比例
    t: int
    scale: float
    off: tuple[int, int]


def _region(gray: np.ndarray, box, mask) -> _Region:
    H, W = gray.shape
    x1, y1, x2, y2 = [int(v) for v in box]
    bw, bh = max(1, x2 - x1), max(1, y2 - y1)
    scale = min(1.0, ANALYSIS_LONG / max(bw, bh))
    px, py = int(bw * 0.5), int(bh * 0.5)
    cx1, cy1 = max(0, x1 - px), max(0, y1 - py)
    cx2, cy2 = min(W, x2 + px), min(H, y2 + py)
    crop = gray[cy1:cy2, cx1:cx2]
    if mask is None:
        m = np.zeros((cy2 - cy1, cx2 - cx1), np.float32)
        ix, iy = int(bw * 0.15), int(bh * 0.15)
        m[y1 - cy1 + iy:y2 - cy1 - iy, x1 - cx1 + ix:x2 - cx1 - ix] = 1
    else:
        m = mask[cy1:cy2, cx1:cx2].astype(np.float32)
    if scale < 1.0:
        size = (max(16, int(crop.shape[1] * scale)), max(16, int(crop.shape[0] * scale)))
        crop = cv2.resize(crop, size, interpolation=cv2.INTER_AREA)
        m = cv2.resize(m, size, interpolation=cv2.INTER_AREA)
    t = int(max(12, min(40, round(max(bw, bh) * scale / 22))))
    return _Region(crop=np.ascontiguousarray(crop, np.float32), cov=_tile_mean(m, t), t=t, scale=scale, off=(cx1, cy1))


def _iso(c: np.ndarray, t: int):
    g1 = cv2.GaussianBlur(c, (0, 0), 1.0, borderType=cv2.BORDER_REFLECT)
    g3 = cv2.GaussianBlur(c, (0, 0), 3.0, borderType=cv2.BORDER_REFLECT)
    ehf, emf = _tile_mean((c - g1) ** 2, t), _tile_mean((g1 - g3) ** 2, t)
    fh, fm = _floor(ehf), _floor(emf)
    hf = np.maximum(ehf - fh, 0)
    mf = np.maximum(emf - fm, 0)
    valid = np.sqrt(mf) > max(3 * np.sqrt(fm), 0.006)
    acut = np.clip(np.sqrt(hf / np.maximum(mf, 1e-9)), 0, 4)
    return acut, np.sqrt(hf), valid, fm


@dataclass
class FocusResult:
    grade: str
    score: int                    # 0~100
    sharp: float                  # 最锐区域的最弱方向锐利度（定级依据）
    peak: float                   # 鸟体各向同性锐利度 90 分位
    body: float                   # 鸟体各向同性锐利度中位数
    bg: float                     # 周边背景锐利度 90 分位
    anis: float                   # 方向一致性（1=各向同性，低=运动模糊）
    energy: float                 # 最锐区域高频能量（连拍内排序用）
    noise: float                  # 噪声底（0~255 灰度）
    peak_xy: tuple[float, float]  # 最锐区域中心，整图归一化坐标
    tiles: int


def _score(s: float) -> int:
    # T_SOFT → 40，T_SHARP → 70，1.0 → 100
    if s <= T_SOFT:
        v = 40 * s / T_SOFT
    elif s <= T_SHARP:
        v = 40 + 30 * (s - T_SOFT) / (T_SHARP - T_SOFT)
    else:
        v = 70 + 30 * (s - T_SHARP) / (1.0 - T_SHARP)
    return int(round(max(0.0, min(100.0, v))))


def assess(gray: np.ndarray, box, mask: Optional[np.ndarray] = None) -> FocusResult:
    """gray: float32 0~1 整图灰度；box: (x1,y1,x2,y2) 像素；mask: 与 gray 同尺寸的布尔数组或 None。"""
    H, W = gray.shape
    R = _region(gray, box, mask)
    c, t = R.crop, R.t
    acut, energy, valid, fm = _iso(c, t)
    cov = R.cov[: acut.shape[0], : acut.shape[1]]
    # 只用完全落在鸟身上的块，避免边缘混入背景细节；鸟太小时逐级放宽
    inside = (cov >= 0.95) & valid
    if inside.sum() < 8:
        inside = (cov >= 0.6) & valid
    if inside.sum() < 4:
        inside = (cov >= 0.3) & valid
    outside = (cov <= 0.02) & valid
    if inside.sum() == 0:
        return FocusResult("blur", 0, 0, 0, 0, 0, 1, 0, 0, ((box[0] + box[2]) / 2 / W, (box[1] + box[3]) / 2 / H), 0)
    a_in = acut[inside]
    peak, body = float(np.percentile(a_in, 90)), float(np.percentile(a_in, 50))
    bg = float(np.percentile(acut[outside], 90)) if outside.sum() >= 6 else 0.0

    # 最锐区域：鸟体内 acutance 前 40% 的块；背景同样取最锐的 40%
    thr = np.percentile(a_in, 60)
    top = inside & (acut >= thr)
    bg_top = np.zeros_like(top)
    if outside.sum() >= 6:
        bg_top = outside & (acut >= np.percentile(acut[outside], 60))
    A, B = [], []
    for k1, k3 in _KERNELS:
        a1 = cv2.filter2D(c, -1, k1, borderType=cv2.BORDER_REFLECT)
        a3 = cv2.filter2D(c, -1, k3, borderType=cv2.BORDER_REFLECT)
        h = _tile_mean((c - a1) ** 2, t)[: acut.shape[0], : acut.shape[1]]
        m = _tile_mean((a1 - a3) ** 2, t)[: acut.shape[0], : acut.shape[1]]
        h = np.maximum(h - _floor(h), 0)
        m = np.maximum(m - _floor(m), 0)
        A.append(np.sqrt(h[top].sum() / max(m[top].sum(), 1e-9)))
        if bg_top.any():
            B.append(np.sqrt(h[bg_top].sum() / max(m[bg_top].sum(), 1e-9)))
    A = np.array(A)
    sharp = float(A.min())
    anis = float(A.min() / max(A.max(), 1e-9))
    bg_sharp = float(min(B)) if B else 0.0

    ys, xs = np.nonzero(top)
    w = acut[ys, xs] + 1e-6
    cy = (np.average(ys, weights=w) + 0.5) * t / R.scale + R.off[1]
    cx = (np.average(xs, weights=w) + 0.5) * t / R.scale + R.off[0]

    res = FocusResult(
        grade="", score=_score(sharp), sharp=round(sharp, 4), peak=round(peak, 4), body=round(body, 4),
        bg=round(bg_sharp, 4), anis=round(anis, 3), energy=round(float(np.mean(energy[top])) * 1000, 3),
        noise=round(float(np.sqrt(fm) * 255), 2),
        peak_xy=(round(float(cx / W), 4), round(float(cy / H), 4)), tiles=int(inside.sum()))
    res.grade = grade_of(asdict(res))
    del bg
    return res


STRICTNESS = {"loose": 0.85, "normal": 1.0, "strict": 1.18}


def grade_of(m: dict, strictness: str = "normal") -> str:
    """根据存储的原始指标定级；严格度只缩放阈值，不需要重新分析。"""
    k = STRICTNESS.get(strictness, 1.0)
    ts, tf = T_SHARP * k, T_SOFT * k
    s = m["sharp"]
    if s >= ts:
        return "sharp"
    if m["bg"] > max(s * BACKFOCUS_RATIO, ts):
        return "missed"
    if m["anis"] < T_ANIS:
        return "motion"
    if s >= tf:
        return "soft"
    return "blur"


def score_of(m: dict, strictness: str = "normal") -> int:
    return _score(m["sharp"] / STRICTNESS.get(strictness, 1.0))


def heatmap(gray: np.ndarray, box, out_w: int, out_h: int) -> np.ndarray:
    """RGBA 锐度热力图，尺寸 out_w × out_h（与预览图一致），只覆盖鸟及周边区域。"""
    H, W = gray.shape
    R = _region(gray, box, None)
    acut, _, valid, _ = _iso(R.crop, R.t)
    out = np.zeros((out_h, out_w, 4), np.uint8)
    if acut.size == 0:
        return out
    # 对数刻度：0.15（很糊）→ 蓝，合焦线附近 → 黄绿，≥2.5 → 深红，清晰照片里也能看出层次
    val = np.clip(np.log(np.maximum(acut, 1e-3) / 0.15) / np.log(2.5 / 0.15), 0, 1)
    rgb = cv2.applyColorMap((val * 255).astype(np.uint8), cv2.COLORMAP_TURBO)[:, :, ::-1]
    alpha = np.where(valid, 125, 0).astype(np.uint8)
    rgba = np.dstack([rgb, alpha])
    sx, sy = out_w / W, out_h / H
    rw = int(round(acut.shape[1] * R.t / R.scale * sx))
    rh = int(round(acut.shape[0] * R.t / R.scale * sy))
    if rw < 1 or rh < 1:
        return out
    rgba = cv2.resize(rgba, (rw, rh), interpolation=cv2.INTER_NEAREST)
    ox, oy = int(R.off[0] * sx), int(R.off[1] * sy)
    rw, rh = min(rw, out_w - ox), min(rh, out_h - oy)
    out[oy:oy + rh, ox:ox + rw] = rgba[:rh, :rw]
    return out


def to_dict(r: FocusResult) -> dict:
    d = asdict(r)
    d["label"] = GRADES[r.grade]
    return d
