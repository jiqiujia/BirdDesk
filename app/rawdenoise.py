"""RAW 域降噪：在线性空间做，用噪声模型而不是"看起来像噪声就抹掉"。

为什么不能在线性空间直接套 NAFNet：
- NAFNet 在 sRGB 8bit 图像上训练，噪声是"编码后"的样子；线性 RAW 的噪声幅度与
  亮度强相关（散粒噪声），直接喂进去模型会把暗部当成噪声、亮部当成干净区域。

采用经典且可解释的做法（VST + 边缘保持）：
1. **估噪声模型**：var = a·I + b（a 为散粒/增益项，b 为读出噪声）。
   用高通残差的鲁棒统计按亮度分箱拟合，不需要 EXIF 以外的先验。
2. **方差稳定变换**（广义 Anscombe）：把"随亮度变化的噪声"变成"近似恒定的单位方差噪声"，
   于是同一个强度参数在所有亮度上都合适。
3. 在变换域做**导向滤波**（自引导，边缘保持），再反变换回线性。
4. 输出仍是线性 16 位，交给后续的色调/分区处理。

优点：物理上正确、快（20MP 约 1–2 秒）、不会把高光细节抹平。
缺点：对强色噪和极端高感的抑制不如专用深度学习模型；这里不冒称"AI"。
"""
from __future__ import annotations

import cv2
import numpy as np

from . import develop

# 广义 Anscombe 变换。归一化到 0~1 的数据里 gain 通常很小，
# 必须除以 gain 才能把噪声方差真正拉到 1（否则信号被淹没在单位噪声里）。
def vst(x: np.ndarray, gain: float, read: float) -> np.ndarray:
    g = max(gain, 1e-9)
    return (2.0 / g) * np.sqrt(np.maximum(g * x + 3.0 / 8.0 * g * g + read, 0.0))


def ivst(z: np.ndarray, gain: float, read: float) -> np.ndarray:
    g = max(gain, 1e-9)
    return np.maximum((g * z / 2.0) ** 2 - 3.0 / 8.0 * g * g - read, 0.0) / g


def estimate_noise(linear: np.ndarray, bins: int = 16) -> tuple[float, float, dict]:
    """估计噪声模型 Var = a·I + b（I 为 0~1 的相对亮度）。

    做法：同一颜色平面上的相邻像素差分（Bayer 上隔一个像素仍是同色），
    差分把低频纹理抵消掉，剩下的主要是噪声；按亮度分箱后用 MAD 稳健估 σ²，
    再对 (I, σ²) 做最小二乘直线拟合。不需要 EXIF，也不用假设具体机型。
    """
    lum = linear.mean(axis=2)
    d = (linear - np.roll(linear, 2, axis=1))[:, 2:, :]
    lumb = lum[:, 2:]
    xs, ys = [], []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for i in range(bins):
        m = (lumb >= edges[i]) & (lumb < edges[i + 1])
        if int(m.sum()) < 2000:
            continue
        vals = d[m]
        mad = float(np.median(np.abs(vals - np.median(vals))))
        sigma2 = (mad / 0.6745) ** 2 / 2.0
        if sigma2 <= 0:
            continue
        xs.append((edges[i] + edges[i + 1]) / 2)
        ys.append(sigma2)
    if len(xs) < 3:
        mad = float(np.median(np.abs(d - np.median(d))))
        sigma2 = (mad / 0.6745) ** 2 / 2.0
        sigma2 = max(sigma2, 1e-10)
        return sigma2, 0.0, {"fallback": True, "bins": len(xs), "a": sigma2, "b": 0.0}
    A = np.vstack([np.array(xs, np.float64), np.ones(len(xs))]).T
    a, b = np.linalg.lstsq(A, np.array(ys, np.float64), rcond=None)[0]
    a = float(np.clip(a, 1e-7, 0.2))
    b = float(np.clip(b, 0.0, 0.01))
    return a, b, {"bins": len(xs), "a": a, "b": b}


def adaptive_guided(img: np.ndarray, a: float, b: float, r: int, k: float) -> np.ndarray:
    """噪声自适应导向滤波：eps 跟着每个像素的噪声方差走。

    eps = k·(a·I + b)。k 越大越平滑；k≈1.5–3 在"抹平噪声"和"保住羽毛"之间比较平衡。
    """
    eps = k * (a * img + b) + 1e-8
    box = (2 * r + 1, 2 * r + 1)
    mi = cv2.blur(img, box)
    var = np.maximum(cv2.blur(img * img, box) - mi * mi, 0)
    A = var / (var + eps)
    B = mi - A * mi
    return cv2.blur(A, box) * img + cv2.blur(B, box)


def denoise_linear(linear: np.ndarray, strength: float = 1.0,
                   progress=None) -> tuple[np.ndarray, dict]:
    """在线性域降噪。strength≈1 为标准；0.5 保守，2 更强。"""
    H, W = linear.shape[:2]
    a, b, info = estimate_noise(linear)
    r = max(2, int(min(H, W) * 0.0018))
    k = 1.9 * max(strength, 0.2)
    out = np.empty_like(linear)
    for c in range(3):
        out[:, :, c] = adaptive_guided(linear[:, :, c], a, b, r, k)
        if progress:
            progress(c + 1, 3, f"线性域降噪 {c + 1}/3")
    info.update({"strength": strength, "k": round(k, 2), "radius": r})
    return np.clip(out, 0, 1), info


def to_srgb(linear: np.ndarray) -> np.ndarray:
    """线性 → sRGB 编码（与 rawpy gamma=(2.4,12.92) 等价）。"""
    x = np.clip(linear, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055).astype(np.float32)


def from_srgb(srgb: np.ndarray) -> np.ndarray:
    x = np.clip(srgb, 0, 1)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)
