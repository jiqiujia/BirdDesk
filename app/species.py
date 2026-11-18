"""鸟种识别：BioCLIP 2 图文对齐 + 中国鸟类名录（1508 种）约束的零样本分类。

设计取舍
- 只用**主鸟的裁切图**分类，不整图：背景会主导 CLIP 的相似度。
- 类别限定在《中国鸟类名录》，避免把普通翠鸟认成非洲翠鸟这类“名录外最近邻”。
- 相似度按 logit_scale 缩放后 softmax，但零样本概率**不是校准过的置信度**；
  因此额外给出 top1 与 top2 的差距，并在低于阈值时标“不确定”，不自动写标签。
- 文本嵌入一次性算好并缓存（文件名带 prompt 版本 + 模型标识），启动只读缓存。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# 运行期完全离线：只用本地权重与分词器
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np

from .config import BASE_DIR, DATA_DIR

MODEL_DIR = BASE_DIR / "models" / "bioclip2"
INDEX_PATH = BASE_DIR / "data" / "birds_cn.json"
EMB_DIR = DATA_DIR / "cache"
WEIGHTS = MODEL_DIR / "open_clip_pytorch_model.bin"
CONFIG = MODEL_DIR / "open_clip_config.json"
ARCH = "ViT-L-14"
PROMPT_VERSION = 1
TOP_K = 5
CROP_PAD = 0.18          # 鸟框外扩比例
MIN_BIRD_PX = 64         # 太小的鸟不给结论
# 阈值由 tests/eval_species.py 在 33 张带标签公开鸟片上标定：
#   p1 ≥ 0.60 → top-1 89%；0.20~0.60 → 60%；< 0.20 → 基本不可信
# 注意：样本是构图干净的图库照片，真实野外照片会更差，故“较可能”不等于确定。
P_OK = 0.60              # ≥ 此值标“较可能”
P_WEAK = 0.20            # 低于则视为无法判断

PROMPTS = (
    "a photo of a {latin}.",
    "a photo of a {en}.",
    "a bird of the species {latin}.",
)

_lock = threading.Lock()


def index_available() -> bool:
    return INDEX_PATH.is_file()


def model_available() -> bool:
    return WEIGHTS.is_file() and CONFIG.is_file()


def load_index() -> dict:
    return json.loads(INDEX_PATH.read_text("utf-8"))


class Classifier:
    def __init__(self):
        self.model = None
        self.preprocess = None
        self.tok = None
        self.species: list[dict] = []
        self.text_emb = None
        self.device = "cpu"
        self.load_error: str | None = None
        self._attempted = False
        self.text_ms = 0

    # ---------------- 加载
    def _load(self) -> None:
        if self._attempted:
            return
        self._attempted = True
        if not model_available():
            self.load_error = "缺少鸟种模型文件"
            return
        try:
            import open_clip
            import torch
            from open_clip.transform import image_transform

            cfg = json.loads(CONFIG.read_text("utf-8"))
            self.device = "mps" if torch.backends.mps.is_available() else "cpu"
            model, _, _ = open_clip.create_model_and_transforms(
                ARCH, pretrained=str(WEIGHTS), precision="fp32", device=self.device)
            model.eval()
            self.model = model
            self.tok = open_clip.get_tokenizer(ARCH)
            pc = cfg.get("preprocess_cfg", {})
            self.preprocess = image_transform(
                cfg["model_cfg"]["vision_cfg"]["image_size"], is_train=False,
                mean=pc.get("mean"), std=pc.get("std"),
                interpolation=pc.get("interpolation", "bicubic"),
                resize_mode=pc.get("resize_mode", "shortest"))
            self.species = load_index()["species"]
            self._ensure_text_embeddings()
        except Exception as e:  # noqa: BLE001
            self.model = None
            self.load_error = f"鸟种模型加载失败：{type(e).__name__}: {e}"

    def _emb_path(self) -> Path:
        tag = f"{ARCH}-{WEIGHTS.stat().st_size}-{len(self.species)}-p{PROMPT_VERSION}"
        return EMB_DIR / f"species_emb_{tag}.npz"

    def _ensure_text_embeddings(self) -> None:
        import torch

        path = self._emb_path()
        if path.exists():
            z = np.load(path)
            if z["emb"].shape[0] == len(self.species):
                self.text_emb = torch.from_numpy(z["emb"]).to(self.device)
                return
        t0 = time.time()
        mat = np.zeros((len(self.species), self.model.text_projection.out_features
                        if hasattr(self.model.text_projection, "out_features") else 768), np.float32)
        B = 64
        with torch.no_grad():
            for i in range(0, len(self.species), B):
                batch = self.species[i:i + B]
                per_sp = []
                for tpl in PROMPTS:
                    texts = [tpl.format(latin=s["latin"], en=s["en"] or s["latin"]) for s in batch]
                    ids = self.tok(texts).to(self.device)
                    e = self.model.encode_text(ids)
                    e = e / e.norm(dim=-1, keepdim=True)
                    per_sp.append(e)
                mean = torch.stack(per_sp).mean(0)
                mean = mean / mean.norm(dim=-1, keepdim=True)
                mat[i:i + len(batch)] = mean.cpu().numpy()
        EMB_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, emb=mat)
        self.text_emb = torch.from_numpy(mat).to(self.device)
        self.text_ms = int((time.time() - t0) * 1000)

    # ---------------- 推理
    def _crop(self, rgb: np.ndarray, box) -> np.ndarray | None:
        H, W = rgb.shape[:2]
        x1, y1, x2, y2 = [float(v) for v in box]
        bw, bh = x2 - x1, y2 - y1
        if max(bw, bh) < MIN_BIRD_PX:
            return None
        side = max(bw, bh) * (1 + CROP_PAD * 2)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        sx1, sy1 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        sx2, sy2 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        crop = rgb[sy1:sy2, sx1:sx2]
        return crop if crop.size else None

    def classify(self, rgb: np.ndarray, box) -> dict:
        from PIL import Image
        import torch

        base = {"status": "ok", "top": [], "model": "bioclip-2", "index_version": None,
                "prompt_version": PROMPT_VERSION, "ms": 0}
        if not model_available() or not index_available():
            return {**base, "status": "unavailable", "reason": "缺少鸟种模型或名录数据"}
        with _lock:
            try:
                self._load()
            except Exception as e:  # noqa: BLE001
                return {**base, "status": "error", "reason": f"{type(e).__name__}"}
        if self.model is None:
            return {**base, "status": "unavailable", "reason": self.load_error or "模型不可用"}
        crop = self._crop(rgb, box)
        if crop is None:
            return {**base, "status": "too_small", "reason": f"鸟体小于 {MIN_BIRD_PX} 像素，不做鸟种判断"}
        t0 = time.time()
        try:
            with _lock, torch.no_grad():
                x = self.preprocess(Image.fromarray(crop)).unsqueeze(0).to(self.device)
                img = self.model.encode_image(x)
                img = img / img.norm(dim=-1, keepdim=True)
                logits = float(self.model.logit_scale.exp()) * (img @ self.text_emb.T)
                probs = logits.softmax(-1)[0].float().cpu().numpy()
        except Exception as e:  # noqa: BLE001
            return {**base, "status": "error", "reason": f"识别失败：{type(e).__name__}"}
        order = np.argsort(-probs)[:TOP_K]
        top = []
        for i in order:
            s = self.species[int(i)]
            top.append({"latin": s["latin"], "zh": s["zh"], "en": s["en"], "p": round(float(probs[i]), 4)})
        p1 = top[0]["p"]
        margin = round(p1 - (top[1]["p"] if len(top) > 1 else 0.0), 4)
        status = "ok" if p1 >= P_OK else ("low_confidence" if p1 >= P_WEAK else "uncertain")
        return {**base, "status": status, "top": top, "p1": p1, "margin": margin,
                "reason": {"ok": "较可能的候选（样本上约九成可靠），仍建议核对",
                           "low_confidence": "候选之间分不开，请从列表中人工挑选",
                           "uncertain": "无法可靠判断，建议忽略或人工确认"}[status],
                "ms": int((time.time() - t0) * 1000),
                "index_version": load_index().get("version")}


# 全局单例，按需加载（不进后台分析默认流程，P3 先做手动识别）
_CLF: Classifier | None = None


def classifier() -> Classifier:
    global _CLF
    if _CLF is None:
        _CLF = Classifier()
    return _CLF
