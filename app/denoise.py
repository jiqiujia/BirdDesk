"""AI 降噪：NAFNet（SIDD 真实噪声，width32），分块 + 余弦窗融合。

结构用官方实现（vendor/nafnet/nafnet_official.py，Apache-2.0），权重来自公开镜像。
自测：512² 加噪图（σ=0.03）PSNR 30.45 → 44.22 dB。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

from .config import BASE_DIR

WEIGHTS = BASE_DIR / "models" / "nafnet" / "nafnet_sidd_width32.pth"
VENDOR = BASE_DIR / "vendor" / "nafnet"
WIDTH = 32
MIDDLE_BLOCKS = 12
ENC_BLOCKS = (2, 2, 4, 8)
DEC_BLOCKS = (2, 2, 2, 2)
TILE = 256
OVERLAP = 32
_lock = threading.Lock()
_win = None


def available() -> bool:
    return WEIGHTS.is_file()


class Denoiser:
    def __init__(self):
        self.model = None
        self.device = "cpu"
        self.error: str | None = None
        self._tried = False

    def load(self) -> bool:
        if self._tried:
            return self.model is not None
        self._tried = True
        if not available():
            self.error = "缺少降噪模型文件"
            return False
        try:
            import torch
            if str(VENDOR) not in sys.path:
                sys.path.insert(0, str(VENDOR))
            import nafnet_official as off  # type: ignore

            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
            m = off.NAFNet(img_channel=3, width=WIDTH, middle_blk_num=MIDDLE_BLOCKS,
                           enc_blk_nums=list(ENC_BLOCKS), dec_blk_nums=list(DEC_BLOCKS))
            sd = torch.load(str(WEIGHTS), map_location="cpu", weights_only=False)
            sd = sd.get("params", sd)
            missing, unexpected = m.load_state_dict(sd, strict=False)
            if missing or unexpected:
                self.error = f"权重与结构不匹配（缺 {len(missing)}/多 {len(unexpected)}），未启用 AI 降噪"
                return False
            self.model = m.to(self.device).eval()
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"降噪模型加载失败：{type(e).__name__}: {e}"
            return False

    def run(self, rgb01: np.ndarray, progress=None) -> np.ndarray:
        """rgb01 float32 HxWx3 (0~1) → 同尺寸降噪结果。"""
        import torch
        import torch.nn.functional as F

        if not self.load():
            raise RuntimeError(self.error or "降噪模型不可用")
        global _win
        if _win is None:
            w = np.outer(np.hanning(TILE), np.hanning(TILE)).astype(np.float32)
            _win = np.clip(w, 1e-3, None)[:, :, None]
        H, W = rgb01.shape[:2]
        out = np.zeros_like(rgb01)
        wsum = np.zeros((H, W, 1), np.float32)
        step = TILE - OVERLAP
        ys = sorted({0, *range(0, max(1, H - TILE), step), max(0, H - TILE)})
        xs = sorted({0, *range(0, max(1, W - TILE), step), max(0, W - TILE)})
        total = len(ys) * len(xs)
        done = 0
        with _lock, torch.no_grad():
            for y in ys:
                for x in xs:
                    hh, ww = min(TILE, H - y), min(TILE, W - x)
                    patch = rgb01[y:y + hh, x:x + ww]
                    t = torch.from_numpy(np.ascontiguousarray(patch.transpose(2, 0, 1)))[None].to(self.device)
                    if hh < TILE or ww < TILE:
                        t = F.pad(t, (0, TILE - ww, 0, TILE - hh), mode="replicate")
                    r = self.model(t)[0, :, :hh, :ww].float().cpu().numpy().transpose(1, 2, 0)
                    w = _win[:hh, :ww]
                    out[y:y + hh, x:x + ww] += r * w
                    wsum[y:y + hh, x:x + ww] += w
                    done += 1
                    if progress and (done % 4 == 0 or done == total):
                        progress(done, total)
        return np.clip(out / np.maximum(wsum, 1e-6), 0, 1)


_D: Denoiser | None = None


def denoiser() -> Denoiser:
    global _D
    if _D is None:
        _D = Denoiser()
    return _D
