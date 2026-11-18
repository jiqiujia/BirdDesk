"""Bird eye localization, not eye-visibility certainty or an eye-focus classifier.

The pinned pose model emits [beak, left eye, right eye, crown, shoulders].
Infer on a padded primary-bird crop and its horizontal reflection. A candidate
is actionable only if the two views agree. Scores are NOT calibrated accuracy.
Missing/uncertain eyes never change the existing body grade or culling rank.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

os.environ["YOLO_OFFLINE"] = "true"
os.environ["YOLO_AUTOINSTALL"] = "false"
os.environ["YOLO_VERBOSE"] = "false"

import cv2
import numpy as np

from .config import BASE_DIR

MODEL_PATH = BASE_DIR / "models" / "eye_pose_v0.pt"
MODEL_SHA256 = "ec9e4b80a93eb6e2b21633e2ff2df30c49faa1eb7919dd24345c7dc295b2d1be"
VERSION = 2
EYE_MIN_CONF = 0.65
BOX_MIN_CONF = 0.45
MIN_BIRD_LONG = 96
MIN_ROI_SIDE = 12
MAX_AGREEMENT_FRAC = 0.022
SIDES = ((1, "left"), (2, "right"))


def empty_result(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "model": "eye-pose-v0",
            "version": VERSION, "eyes": [], "selected": None}


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    aa = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    bb = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return inter / max(aa + bb - inter, 1e-9)


def choose_pose(predictions: list[dict], target_box) -> dict | None:
    """Associate the pose instance with the already selected bird, not a neighbor."""
    valid = [p for p in predictions if p["confidence"] >= BOX_MIN_CONF
             and _iou(p["box"], target_box) >= 0.30]
    return max(valid, key=lambda p: _iou(p["box"], target_box) * p["confidence"], default=None)


def eye_sharpness(gray: np.ndarray, box) -> dict:
    """A contrast-normalized eye-neighborhood metric, not a calibrated grade.

The square is an analysis neighborhood, not a segmentation of the eyeball.
Do not stretch small patches. No score is given to featureless/tiny patches.
"""
    x1, y1, x2, y2 = box
    crop = gray[y1:y2, x1:x2]
    if min(crop.shape, default=0) < MIN_ROI_SIDE:
        return {"usable": False, "value": None, "reason": "眼区像素不足"}
    if float(crop.std()) < 0.012:
        return {"usable": False, "value": None, "reason": "眼区对比度不足"}
    # Exclude the artificial edge of the ROI from the energy measurement.
    sm1 = cv2.GaussianBlur(crop, (0, 0), 1.0)
    sm3 = cv2.GaussianBlur(crop, (0, 0), 3.0)
    h = (crop - sm1)[3:-3, 3:-3]
    m = (sm1 - sm3)[3:-3, 3:-3]
    mid = float(np.mean(m * m))
    if mid < 1e-5:
        return {"usable": False, "value": None, "reason": "可用眼区细节不足"}
    val = float(np.sqrt(float(np.mean(h * h)) / mid))
    return {"usable": True, "value": round(val, 4),
            "reason": "未校准的眼周锐度参考；受噪声、纹理和像素尺寸影响，不参与自动淘汰"}


def refine_pupil(gray: np.ndarray, x: float, y: float, long_side: float) -> tuple[float, float, bool]:
    """Constrained local refinement of a stable pose candidate to a dark pupil.

Not a standalone eye detector. Require a circular edge and a dark interior;
otherwise retain the model point. A failed refinement is not a hidden eye.
"""
    H, W = gray.shape
    search = int(np.clip(long_side * 0.065, 14, 110))
    x0, y0 = max(0, int(x)-search), max(0, int(y)-search)
    x2, y2 = min(W, int(x)+search+1), min(H, int(y)+search+1)
    patch = np.uint8(np.clip(gray[y0:y2, x0:x2] * 255, 0, 255))
    if min(patch.shape) < 20:
        return x, y, False
    small = max(3, int(long_side * .006))
    large = max(small+1, int(min(45, long_side * .035)))
    smooth = cv2.GaussianBlur(patch, (3, 3), .7)
    circles = cv2.HoughCircles(smooth, cv2.HOUGH_GRADIENT, dp=1,
                              minDist=max(5, small), param1=70, param2=13,
                              minRadius=small, maxRadius=large)
    proposals = [] if circles is None else circles[0].tolist()
    # Tiny dark pupils may have too few edge votes for Hough; also inspect
    # compact connected dark regions. Elongated feather lines are excluded.
    for level in (20, 35, 50, 65):
        binary = np.uint8(smooth < level) * 255
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3,3), np.uint8))
        contours,_ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = cv2.contourArea(contour)
            perimeter = cv2.arcLength(contour,True)
            if not (np.pi*small*small*.6 <= area <= np.pi*large*large) or perimeter <= 0:
                continue
            circularity = 4*np.pi*area/(perimeter*perimeter)
            if circularity < .58:
                continue
            moments = cv2.moments(contour)
            if moments['m00']:
                proposals.append([moments['m10']/moments['m00'], moments['m01']/moments['m00'], np.sqrt(area/np.pi)])
    yy, xx = np.indices(patch.shape)
    candidates = []
    for cx, cy, radius in proposals:
        distance = float(np.hypot(cx+x0-x, cy+y0-y))
        if distance > min(search * .8, long_side * .05):
            continue
        rr = np.hypot(xx-cx, yy-cy)
        inner = patch[rr <= radius*.65]
        ring = patch[(rr >= radius*1.1) & (rr <= radius*1.6)]
        if inner.size < 12 or ring.size < 20:
            continue
        contrast = float(np.median(ring) - np.median(inner))
        if contrast < 35 or float(np.median(inner)) > 65:
            continue
        # Sample eight directions: an isolated line must not look like a pupil.
        theta = np.arange(16)*np.pi/8
        xi = np.rint(cx + radius*.5*np.cos(theta)).astype(int)
        yi = np.rint(cy + radius*.5*np.sin(theta)).astype(int)
        xo = np.rint(cx + radius*1.35*np.cos(theta)).astype(int)
        yo = np.rint(cy + radius*1.35*np.sin(theta)).astype(int)
        good = (xo>=0)&(xo<patch.shape[1])&(yo>=0)&(yo<patch.shape[0])&(xi>=0)&(yi>=0)&(xi<patch.shape[1])&(yi<patch.shape[0])
        if good.sum() < 14:
            continue
        dif = patch[yo[good],xo[good]].astype(float)-patch[yi[good],xi[good]].astype(float)
        if (dif > 20).mean() < .80:
            continue
        score = contrast / 255 - .3 * distance / max(search, 1)
        candidates.append((score, float(cx+x0), float(cy+y0)))
    if not candidates:
        return x, y, False
    _, rx, ry = max(candidates)
    return rx, ry, True


def reconcile(primary: dict | None, mirrored: dict | None, origin: tuple[int, int],
              crop_width: int, image_shape: tuple[int, int], bird_box,
              gray: np.ndarray, mask: np.ndarray | None = None) -> dict:
    """Convert two crop-space poses to oriented full-image normalized coordinates."""
    out = empty_result("not_detected", "未找到可信的主鸟眼睛；可能被遮挡、背向或超出模型适用范围")
    if primary is None:
        return out
    H, W = image_shape
    ox, oy = origin
    bx1, by1, bx2, by2 = map(float, bird_box)
    long_side = max(bx2 - bx1, by2 - by1)
    tolerance = max(3.0, long_side * MAX_AGREEMENT_FRAC)
    roi_half = float(np.clip(long_side * 0.028, 6, 70))
    for idx, side in SIDES:
        x, y, c = map(float, primary["keypoints"][idx])
        if not np.isfinite([x, y, c]).all() or c < EYE_MIN_CONF:
            continue
        x += ox
        y += oy
        if not (0 <= x < W and 0 <= y < H):
            continue
        if not (bx1 - roi_half <= x <= bx2 + roi_half and by1 - roi_half <= y <= by2 + roi_half):
            continue
        agreement = None
        stable = False
        conf = c * float(primary["confidence"])
        if mirrored is not None:
            # Anatomical left/right swap under reflection, as in model flip_idx.
            qx, qy, qc = map(float, mirrored["keypoints"][3 - idx])
            qx, qy = crop_width - qx + ox, qy + oy
            if np.isfinite([qx, qy, qc]).all() and qc >= EYE_MIN_CONF:
                agreement = float(np.hypot(qx - x, qy - y))
                stable = agreement <= tolerance
                conf = min(conf, qc * float(mirrored["confidence"]))
                if stable:
                    x, y = (x + qx) / 2, (y + qy) / 2
        pose_x, pose_y = x, y
        refined = False
        if stable:
            x, y, refined = refine_pupil(gray, x, y, long_side)
            # Pose agreement alone can be systematically wrong. Require local
            # pupil-like evidence before calling the point actionable.
            stable = refined
        # A mask is advisory: disagreement marks a candidate uncertain, not an eye.
        on_subject = True
        if mask is not None:
            px, py = int(np.clip(x, 0, W - 1)), int(np.clip(y, 0, H - 1))
            rr = max(2, int(roi_half * 0.5))
            on_subject = bool(np.any(mask[max(0, py-rr):min(H, py+rr+1), max(0, px-rr):min(W, px+rr+1)]))
            stable = stable and on_subject
        bounds = [max(0, int(x-roi_half)), max(0, int(y-roi_half)),
                  min(W, int(x+roi_half+1)), min(H, int(y+roi_half+1))]
        if min(bounds[2]-bounds[0], bounds[3]-bounds[1]) < MIN_ROI_SIDE:
            stable = False
        metric = eye_sharpness(gray, bounds) if stable else {
            "usable": False, "value": None, "reason": "定位不确定，不计算眼区锐度"}
        out["eyes"].append({"side": side, "x": round(x / W, 6), "y": round(y / H, 6),
                            "box": [round(bounds[0]/W, 6), round(bounds[1]/H, 6),
                                    round(bounds[2]/W, 6), round(bounds[3]/H, 6)],
                            "confidence": round(conf, 4), "stable": bool(stable),
                            "refined": refined, "pose_xy": [round(pose_x/W,6), round(pose_y/H,6)],
                            "agreement_px": round(agreement, 2) if agreement is not None else None,
                            "sharpness": metric})
    # Avoid displaying two inferred anatomical eyes at the same physical point.
    if len(out["eyes"]) == 2:
        a, b = out["eyes"]
        if np.hypot((a["x"]-b["x"])*W, (a["y"]-b["y"])*H) < max(4, long_side*0.014):
            out["eyes"] = [max(out["eyes"], key=lambda p: (p["stable"], p["confidence"]))]
    stable_ids = [i for i,e in enumerate(out["eyes"]) if e["stable"]]
    if stable_ids:
        out["selected"] = max(stable_ids, key=lambda i: out["eyes"][i]["confidence"])
        out["status"] = "detected"
        out["reason"] = "主鸟眼部通过双视角一致性与瞳孔校验；仍可能偏移，请放大核对。模型分数不是定位正确率"
    elif out["eyes"]:
        out["status"] = "uncertain"
        out["reason"] = "仅有眼部候选，双视角或主体校验未通过；不自动放大候选、不影响合焦与淘汰"
    return out


class EyeLocator:
    def __init__(self, device: str = "cpu"):
        self.device = device
        self.model = None
        self.load_error = None
        self._attempted = False

    def _load(self):
        if self._attempted:
            return
        self._attempted = True
        if not MODEL_PATH.is_file():
            self.load_error = "缺少鸟眼模型文件"
            return
        if hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() != MODEL_SHA256:
            self.load_error = "鸟眼模型校验失败，已跳过加载"
            return
        try:
            from ultralytics import YOLO
            self.model = YOLO(str(MODEL_PATH), task="pose")
            if list(self.model.model.kpt_shape) != [6, 3]:
                raise ValueError("关键点格式不匹配")
        except Exception as e:
            self.model = None
            self.load_error = f"鸟眼模型加载失败：{type(e).__name__}"

    def _predict(self, bgr: np.ndarray) -> list[dict]:
        r = self.model.predict(np.ascontiguousarray(bgr), device=self.device, imgsz=640,
                               conf=0.30, iou=0.5, verbose=False)[0]
        if r.keypoints is None:
            return []
        return [{"box": b.xyxy[0].cpu().numpy().tolist(), "confidence": float(b.conf[0]),
                 "keypoints": k.tolist()}
                for b,k in zip(r.boxes, r.keypoints.data.cpu().numpy())]

    def locate(self, rgb: np.ndarray, bird_box, mask=None) -> dict:
        H,W = rgb.shape[:2]
        bx1,by1,bx2,by2 = map(float, bird_box)
        long_side = max(bx2-bx1, by2-by1)
        if long_side < MIN_BIRD_LONG or min(bx2-bx1, by2-by1) < 32:
            return empty_result("too_small", "主鸟像素不足，放大插值不能恢复眼部细节")
        self._load()
        if self.model is None:
            return empty_result("unavailable", self.load_error or "鸟眼模型不可用")
        pad = long_side * 0.12
        x1,y1 = max(0,int(bx1-pad)),max(0,int(by1-pad))
        x2,y2 = min(W,int(bx2+pad+1)),min(H,int(by2+pad+1))
        crop = rgb[y1:y2,x1:x2]
        if min(crop.shape[:2]) < 32:
            return empty_result("too_small", "可用鸟体裁切过小")
        # Ultralytics numpy input is BGR; load_rgb and our geometry are RGB.
        bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
        target = [bx1-x1, by1-y1, bx2-x1, by2-y1]
        cw = crop.shape[1]
        flip_target = [cw-target[2], target[1], cw-target[0], target[3]]
        try:
            a = choose_pose(self._predict(bgr), target)
            b = choose_pose(self._predict(bgr[:, ::-1]), flip_target)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
            return reconcile(a,b,(x1,y1),cw,(H,W),bird_box,gray,mask)
        except Exception as e:
            return empty_result("error", f"鸟眼分析失败（{type(e).__name__}），鸟体分析保留")
