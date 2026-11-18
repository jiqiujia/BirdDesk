"""后台分析：鸟体检测（YOLO 分割，Apple GPU）+ 合焦评估 + 对焦点，结果按照片 id 缓存在 SQLite。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

# 必须在导入 ultralytics 之前设置：完全离线，不自动安装依赖、不检查更新、不上报统计
os.environ["YOLO_OFFLINE"] = "true"
os.environ["YOLO_AUTOINSTALL"] = "false"
os.environ["YOLO_VERBOSE"] = "false"

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from . import eyes, focus, species  # noqa: E402
from .config import BASE_DIR, DB_PATH, JPG_EXTS, RAW_EXTS
from .scanner import Photo  # noqa: E402

MODEL_PATH = BASE_DIR / "models" / "yolo11m-seg.pt"
BIRD_CLASS = 14           # COCO: bird
DET_IMGSZ = 1024
DET_CONF = 0.25
SECONDARY_CONF = 0.45
ANALYSIS_MAX = 4096       # 解码长边上限（JPEG draft 降采样，快）
VERSION = 400 + focus.ALGO_VERSION + eyes.VERSION
SPECIES_ENABLED = True      # 由设置控制；关闭时不加载 BioCLIP

_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS analysis (id TEXT PRIMARY KEY, v INTEGER, data TEXT)")
    return con


def load_cached(ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with _db_lock:
        con = _db()
        for i in range(0, len(ids), 900):
            part = ids[i:i + 900]
            q = f"SELECT id, data FROM analysis WHERE v=? AND id IN ({','.join('?' * len(part))})"
            for pid, data in con.execute(q, [VERSION, *part]):
                value = json.loads(data)
                if (value.get("eye") or {}).get("status") not in {"unavailable", "error"}:
                    out[pid] = value
        con.close()
    return out


def _save(rows: list[tuple[str, dict]]) -> None:
    if not rows:
        return
    with _db_lock:
        con = _db()
        con.executemany("INSERT OR REPLACE INTO analysis (id, v, data) VALUES (?,?,?)",
                        [(pid, VERSION, json.dumps(d, ensure_ascii=False)) for pid, d in rows])
        con.commit()
        con.close()


# ---------------------------------------------------------------- image loading
def load_rgb(photo: Photo, max_side: int = ANALYSIS_MAX) -> tuple[np.ndarray, str]:
    """返回已按方向旋正的 RGB uint8 数组。优先用同名 JPG（全分辨率），否则用 RAW 内嵌预览。"""
    src = photo.display
    ext = src.suffix.lower()
    if ext in RAW_EXTS:
        from .thumbs import _open
        img = _open(photo, max_side)
        kind = "raw-preview"
    else:
        img = Image.open(src)
        if ext in JPG_EXTS:
            img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img)
        kind = "jpg" if ext in JPG_EXTS else ext.lstrip(".")
    if img.mode != "RGB":
        img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.BILINEAR)
    return np.asarray(img), kind


def _orient_point(u: float, v: float, orient) -> tuple[float, float]:
    if orient == 3:
        return 1 - u, 1 - v
    if orient == 6:
        return 1 - v, u
    if orient == 8:
        return v, 1 - u
    return u, v


def af_point(meta: dict) -> Optional[tuple[float, float]]:
    """索尼 FocusLocation = "原图宽 原图高 X Y"（未随方向旋转）。返回显示方向下的归一化坐标。"""
    raw = meta.get("FocusLocation")
    if not raw:
        return None
    try:
        w, h, x, y = [float(v) for v in str(raw).replace(",", " ").split()[:4]]
    except ValueError:
        return None
    if w <= 0 or h <= 0 or (x == 0 and y == 0):
        return None
    return _orient_point(x / w, y / h, meta.get("Orientation"))


# ---------------------------------------------------------------- detector
class Detector:
    def __init__(self):
        import torch
        from ultralytics import YOLO
        from ultralytics.utils import SETTINGS
        SETTINGS.update({"sync": False})
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = YOLO(str(MODEL_PATH))
        self.eye_locator = eyes.EyeLocator(self.device)
        self.model.predict(np.zeros((640, 640, 3), np.uint8), device=self.device, imgsz=DET_IMGSZ, verbose=False)

    def detect(self, rgb: np.ndarray) -> list[dict]:
        # Ultralytics interprets numpy images as BGR, while load_rgb returns RGB.
        r = self.model.predict(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), device=self.device, imgsz=DET_IMGSZ, classes=[BIRD_CLASS],
                               conf=DET_CONF, verbose=False)[0]
        out = []
        polys = r.masks.xy if r.masks is not None else [None] * len(r.boxes)
        for b, poly in zip(r.boxes, polys):
            out.append({"box": [float(v) for v in b.xyxy[0].tolist()], "conf": float(b.conf),
                        "poly": poly})
        # 最可信的一只用低阈值保证召回；其余要求更高置信度，避免把树枝、石头算成鸟
        out.sort(key=lambda d: -d["conf"])
        return out[:1] + [d for d in out[1:] if d["conf"] >= SECONDARY_CONF]


def analyze_image(det: Detector, rgb: np.ndarray, meta: dict) -> dict:
    H, W = rgb.shape[:2]
    birds = det.detect(rgb)
    res: dict = {"v": VERSION, "w": W, "h": H, "birds": [], "main": None, "focus": None, "af": None,
                 "eye": eyes.empty_result("no_bird", "未检测到主鸟，未进行眼部定位"),
                 "species": {"status": "no_bird", "top": [], "model": "bioclip-2", "reason": "未检测到主鸟"}}
    for b in birds:
        x1, y1, x2, y2 = b["box"]
        area = (x2 - x1) * (y2 - y1) / (W * H)
        edge = x1 <= W * 0.004 or y1 <= H * 0.004 or x2 >= W * 0.996 or y2 >= H * 0.996
        res["birds"].append({"box": [round(x1 / W, 4), round(y1 / H, 4), round(x2 / W, 4), round(y2 / H, 4)],
                             "conf": round(b["conf"], 3), "area": round(area, 4), "edge": edge})
    if birds:
        # 主体：置信度 × 面积，偏好完整入画的鸟
        sc = [bd["conf"] * np.sqrt(bd["area"]) * (0.8 if bd["edge"] else 1.0) for bd in res["birds"]]
        mi = int(np.argmax(sc))
        res["main"] = mi
        b = birds[mi]
        mask = None
        if b["poly"] is not None and len(b["poly"]) >= 3:
            mask = np.zeros((H, W), np.uint8)
            cv2.fillPoly(mask, [np.round(b["poly"]).astype(np.int32)], 1)
            if mask.sum() < 50:
                mask = None
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
        fr = focus.assess(gray, b["box"], mask)
        res["focus"] = focus.to_dict(fr)
        # Eye errors must never discard a valid body analysis.
        locator = getattr(det, "eye_locator", None)
        if locator is None:
            res["eye"] = eyes.empty_result("unavailable", "鸟眼定位模块不可用")
        else:
            try:
                res["eye"] = locator.locate(rgb, b["box"], mask)
            except Exception as e:
                res["eye"] = eyes.empty_result("error", f"鸟眼定位失败（{type(e).__name__}），保留鸟体分析")
        # 鸟种：失败只降级，不影响合焦与鸟眼结果
        try:
            res["species"] = (species.classifier().classify(rgb, b["box"]) if SPECIES_ENABLED
                              else {"status": "disabled", "top": [], "model": "bioclip-2", "reason": "鸟种识别已关闭"})
        except Exception as e:  # noqa: BLE001
            res["species"] = {"status": "error", "top": [], "model": "bioclip-2",
                              "reason": f"鸟种识别失败（{type(e).__name__}）"}
    af = af_point(meta)
    if af is not None:
        on = None
        if res["main"] is not None:
            x1, y1, x2, y2 = res["birds"][res["main"]]["box"]
            mx, my = (x2 - x1) * 0.08, (y2 - y1) * 0.08
            on = (x1 - mx) <= af[0] <= (x2 + mx) and (y1 - my) <= af[1] <= (y2 + my)
        res["af"] = {"x": round(af[0], 4), "y": round(af[1], 4), "on_bird": on}
    return res


# ---------------------------------------------------------------- worker
class Analyzer:
    """单线程跑模型，3 个线程预解码图片。可随时暂停（整理文件前必须先停）。"""

    def __init__(self, on_result: Callable[[str, dict], None]):
        self.on_result = on_result
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None
        self.stop_flag = threading.Event()
        self.queue: list[Photo] = []
        self.done = 0
        self.total = 0
        self.current = ""
        self.error: Optional[str] = None
        self.started = 0.0
        self.det: Optional[Detector] = None
        self.loading_model = False
        self.failed: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def status(self) -> dict:
        el = time.time() - self.started if self.running and self.started else 0
        rate = self.done / el if el > 0 and self.done else 0
        return {"running": self.running, "done": self.done, "total": self.total, "current": self.current,
                "loading_model": self.loading_model, "error": self.error, "failed": len(self.failed),
                "eta": round((self.total - self.done) / rate) if rate else None,
                "model_ready": MODEL_PATH.exists()}

    def start(self, photos: list[Photo]) -> None:
        with self.lock:
            self.stop()
            self.failed.clear()
            self.error = None
            if not photos:
                self.done = self.total = 0
                return
            self.queue = list(photos)
            self.done, self.total = 0, len(photos)
            self.stop_flag.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self, wait: bool = True) -> None:
        with self.lock:
            self.stop_flag.set()
            if wait and self.thread is not None:
                self.thread.join(timeout=60)
                if self.thread.is_alive():
                    # Never clear the stop flag/start a second model user while it lives.
                    raise RuntimeError("后台分析仍在停止中，请稍后重试；未执行照片移动")
                self.thread = None

    def _run(self) -> None:
        self.started = time.time()
        pool = None
        futs = {}
        batch: list[tuple[str, dict]] = []
        try:
            if self.det is None:
                self.loading_model = True
                self.current = "正在加载识别模型…"
                self.det = Detector()
                self.loading_model = False
            pool = ThreadPoolExecutor(max_workers=3)
            photos = self.queue
            futs = {}
            ahead = 4

            def submit(i):
                if i < len(photos) and i not in futs:
                    futs[i] = pool.submit(load_rgb, photos[i])

            for i in range(ahead):
                submit(i)
            batch: list[tuple[str, dict]] = []
            for i, p in enumerate(photos):
                if self.stop_flag.is_set():
                    break
                submit(i + ahead)
                self.current = p.primary.name
                t0 = time.time()
                try:
                    rgb, kind = futs.pop(i).result()
                    if self.stop_flag.is_set():
                        break
                    res = analyze_image(self.det, rgb, p.meta)
                    res["src"] = kind
                    res["ms"] = int((time.time() - t0) * 1000)
                    batch.append((p.id, res))
                    self.on_result(p.id, res)
                except Exception as e:  # noqa: BLE001
                    self.failed[p.id] = f"{type(e).__name__}: {e}"
                    self.on_result(p.id, {"v": VERSION, "error": self.failed[p.id], "birds": [], "focus": None})
                self.done = i + 1
                if len(batch) >= 20:
                    _save(batch)
                    batch = []
            _save(batch)
            batch = []
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.error = f"{type(e).__name__}: {e}"
        finally:
            for f in futs.values():
                f.cancel()
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
            if batch:
                try:
                    _save(batch)
                except Exception as e:
                    self.error = f"分析缓存保存失败：{type(e).__name__}"
            self.loading_model = False
            self.current = ""


def heat_png(photo: Photo, analysis: dict, out_long: int = 1600) -> bytes:
    rgb, _ = load_rgb(photo)
    H, W = rgb.shape[:2]
    s = out_long / max(H, W)
    ow, oh = int(round(W * s)), int(round(H * s))
    out = np.zeros((oh, ow, 4), np.uint8)
    if analysis and analysis.get("main") is not None:
        x1, y1, x2, y2 = analysis["birds"][analysis["main"]]["box"]
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
        out = focus.heatmap(gray, (x1 * W, y1 * H, x2 * W, y2 * H), ow, oh)
    ok, buf = cv2.imencode(".png", cv2.cvtColor(out, cv2.COLOR_RGBA2BGRA))
    return buf.tobytes()


def crop_jpg(photo: Photo, cx: float, cy: float, w: int = 1400, h: int = 933) -> tuple[bytes, dict]:
    """100% 原始像素裁切（以 cx,cy 为中心，归一化坐标）。"""
    rgb, kind = load_rgb(photo, max_side=100_000)
    H, W = rgb.shape[:2]
    w, h = min(w, W), min(h, H)
    x = int(round(cx * W - w / 2))
    y = int(round(cy * H - h / 2))
    x = max(0, min(W - w, x))
    y = max(0, min(H - h, y))
    crop = rgb[y:y + h, x:x + w]
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes(), {"x": x / W, "y": y / H, "w": w / W, "h": h / H, "full_w": W, "full_h": H, "src": kind}


def model_present() -> bool:
    return Path(MODEL_PATH).exists()
