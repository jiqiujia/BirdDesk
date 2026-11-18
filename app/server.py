"""本地 HTTP 服务：界面 + API。只监听 127.0.0.1。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (__version__, airdrop, analyzer, denoise, develop, focus, grouping, metadata,
               organizer, ratings, raw_io, species, thumbs, trash, video)
from .config import (DB_PATH, DEFAULT_SETTINGS, EXPORT_FOLDER, SETTINGS_PATH, THUMB_DIR, VIDEO_FOLDER,
                     WEB_DIR)
from .scanner import Photo, scan

SORTS = {"time", "score", "score_asc", "rating"}

# 由 main.py 注入：原生窗口里的选择目录对话框
native_pick_folder: Optional[Callable[[str], Optional[str]]] = None


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return s


def save_settings(s: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf-8")


# ---------------------------------------------------------------- 鸟种确认
def _conf_db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("CREATE TABLE IF NOT EXISTS species_confirm "
                "(id TEXT PRIMARY KEY, zh TEXT, latin TEXT, ts REAL)")
    return con


def _load_confirmed() -> dict[str, dict]:
    try:
        con = _conf_db()
        rows = con.execute("SELECT id, zh, latin, ts FROM species_confirm").fetchall()
        con.close()
        return {r[0]: {"zh": r[1], "latin": r[2], "ts": r[3]} for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _save_confirmed(pid: str, zh: str, latin: str) -> None:
    con = _conf_db()
    con.execute("INSERT OR REPLACE INTO species_confirm (id, zh, latin, ts) VALUES (?,?,?,?)",
                (pid, zh, latin or "", time.time()))
    con.commit()
    con.close()


# ---------------------------------------------------------------- state
class Job:
    def __init__(self, kind: str):
        self.kind = kind
        self.running = True
        self.done = 0
        self.total = 0
        self.message = ""
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.started = time.time()

    def to_dict(self) -> dict:
        return {"kind": self.kind, "running": self.running, "done": self.done, "total": self.total,
                "message": self.message, "error": self.error, "result": self.result}


class Library:
    def __init__(self):
        self.lock = threading.RLock()
        self.root: Optional[Path] = None
        self.photos: dict[str, Photo] = {}
        self.grouping: Optional[grouping.Grouping] = None
        self.burst_of: dict[str, str] = {}
        self.other_files = 0
        self.plans: dict[str, dict] = {}
        self.job: Optional[Job] = None
        self.settings = load_settings()
        self.version = 0          # 分组/文件变化 +1，前端整页刷新
        self.analysis: dict[str, dict] = {}
        self.confirmed: dict[str, dict] = {}     # 用户确认的鸟种（另写入 XMP 关键词）
        self.ratings: dict[str, int] = {}
        self.seq = 0              # 分析结果 / 评级的增量序号，前端据此局部更新
        self.changed: dict[str, int] = {}
        self.analyzer = analyzer.Analyzer(self._on_result)

    # ---- jobs
    def start_job(self, kind: str, fn: Callable[[Job], Optional[dict]]) -> Job:
        with self.lock:
            if self.job and self.job.running:
                raise HTTPException(409, f"正在进行：{self.job.kind}，请稍候")
            job = Job(kind)
            self.job = job

        def run():
            try:
                job.result = fn(job)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                job.error = f"{e}"
            finally:
                job.running = False
        threading.Thread(target=run, daemon=True).start()
        return job

    def _touch(self, pid: str) -> None:
        self.seq += 1
        self.changed[pid] = self.seq

    def _on_result(self, pid: str, res: dict) -> None:
        with self.lock:
            if pid in self.photos:
                self.analysis[pid] = res
                self._touch(pid)

    # ---- scanning
    def do_scan(self, job: Job, root: Path) -> dict:
        self.analyzer.stop()
        job.message = "正在查找照片…"

        def p1(n, msg):
            job.done, job.message = n, msg
        res = scan(root, recursive=self.settings["recursive"], progress=p1)

        seen: dict[str, Photo] = {}
        for p in res.photos:
            if p.id in seen:  # 极少见：不同目录下同名同大小同时间的文件
                p.id = p.id + "-" + hashlib.sha1(str(p.dir).encode()).hexdigest()[:4]
            seen[p.id] = p

        def p2(d, t, msg):
            job.done, job.total, job.message = d, t, msg
        metadata.load_metadata(res.photos, progress=p2)
        job.message = "读取评级与分析缓存…"
        cached = analyzer.load_cached(list(seen))
        rts = {pid: ratings.read_rating(p) for pid, p in seen.items()}
        conf = _load_confirmed()

        with self.lock:
            self.root = res.root
            self.photos = seen
            self.other_files = res.other_files
            self.plans.clear()
            self.analysis = cached
            self.ratings = {k: v for k, v in rts.items() if v}
            self.confirmed = {k: v for k, v in conf.items() if k in seen}
            self.changed.clear()
            self.settings["last_root"] = str(res.root)
            save_settings(self.settings)
            self.regroup()
        if self.settings.get("auto_analyze", True):
            self.start_analysis()
        return {"photos": len(seen)}

    def start_analysis(self, force: bool = False) -> None:
        if not analyzer.model_present():
            return
        with self.lock:
            todo = [p for pid, p in self.photos.items() if force or pid not in self.analysis
                    or self.analysis[pid].get("error")]
            # 按时间顺序处理，界面上从前往后逐步出结果
            todo.sort(key=lambda p: (p.ts if p.ts is not None else float("inf"), p.stem))
        self.analyzer.start(todo)

    def regroup(self) -> None:
        s = self.settings
        self.grouping = grouping.group(list(self.photos.values()), gap=s["gap"],
                                       min_burst=s["min_burst"], split_by_camera=s["split_by_camera"])
        self.burst_of = {pid: bid for bid, b in self.grouping.bursts.items() for pid in b.photo_ids}
        self.plans.clear()
        self.version += 1

    # ---- analysis helpers
    def fsum(self, pid: str) -> Optional[dict]:
        """单张照片的合焦摘要（按当前严格度定级）。None = 尚未分析。"""
        a = self.analysis.get(pid)
        if a is None:
            return None
        if a.get("error"):
            return {"g": "error", "s": None, "nb": 0, "err": a["error"]}
        if not a.get("focus"):
            return {"g": "nobird", "s": None, "nb": 0}
        st = self.settings.get("strictness", "normal")
        f = a["focus"]
        af = a.get("af") or {}
        out = {"g": focus.grade_of(f, st), "s": focus.score_of(f, st), "nb": len(a["birds"]),
               "af": af.get("on_bird")}
        sp = a.get("species") or {}
        if sp.get("top"):
            top = sp["top"][0]
            out["sp"] = {"zh": top["zh"], "latin": top["latin"], "p": top["p"], "st": sp.get("status")}
        elif sp.get("status"):
            out["sp"] = {"zh": None, "st": sp.get("status")}
        return out

    def rank_value(self, pid: str) -> Optional[float]:
        a = self.analysis.get(pid)
        if a is None:
            return None
        if not a.get("focus"):
            return -1.0
        f = a["focus"]
        k = focus.STRICTNESS.get(self.settings.get("strictness", "normal"), 1.0)
        v = f["sharp"] / (focus.T_SHARP * k)
        v += 0.05 * min(f.get("energy", 0) / 50.0, 1.0)
        main = a["birds"][a["main"]]
        if main.get("edge"):
            v -= 0.15
        if (a.get("af") or {}).get("on_bird") is False:
            v -= 0.08
        v += 0.05 * min(main.get("area", 0) / 0.1, 1.0)
        return v

    def burst_info(self, bid: str) -> dict:
        b = self.grouping.bursts[bid]
        vals = [(self.rank_value(pid), pid) for pid in b.photo_ids]
        known = [(v, pid) for v, pid in vals if v is not None]
        best = max(known)[1] if known and max(known)[0] > -1 else None
        sharp = sum(1 for pid in b.photo_ids if (self.fsum(pid) or {}).get("g") == "sharp")
        votes: dict[str, int] = {}
        for pid in b.photo_ids:
            c = self.confirmed.get(pid)
            if c:
                votes[c["zh"]] = votes.get(c["zh"], 0) + 2
                continue
            sp = (self.fsum(pid) or {}).get("sp") or {}
            if sp.get("zh") and sp.get("st") in ("ok", "low_confidence"):
                votes[sp["zh"]] = votes.get(sp["zh"], 0) + 1
        vote = None
        if votes:
            zh, cnt = max(votes.items(), key=lambda kv: kv[1])
            latin = next((x["latin"] for pid in b.photo_ids
                          for x in ((self.analysis.get(pid) or {}).get("species") or {}).get("top", [])
                          if x["zh"] == zh), None)
            vote = {"zh": zh, "latin": latin, "votes": cnt, "of": len(b.photo_ids)}
        return {"best": best, "analyzed": len(known), "sharp": sharp, "species": vote}

    # ---- serialisation
    def light(self, p: Photo) -> dict:
        t = None
        if p.dt:
            t = p.dt.strftime("%H:%M:%S") + (f".{p.dt.microsecond // 1000:03d}" if p.has_subsec else "")
        rel = str(p.dir.relative_to(self.root)) if self.root and p.dir.is_relative_to(self.root) else str(p.dir)
        return {"id": p.id, "name": p.primary.name, "stem": p.stem, "exts": p.exts, "time": t,
                "gap": round(p.gap_prev, 3) if p.gap_prev is not None else None,
                "dir": "" if rel == "." else rel, "burst": p.burst, "day": p.day,
                "f": self.fsum(p.id), "r": self.ratings.get(p.id, 0),
                "spc": (self.confirmed.get(p.id) or {}).get("zh"),
                "nf": len(p.files)}

    def stats(self) -> dict:
        ph = list(self.photos.values())
        g = self.grouping
        grades: dict[str, int] = {}
        for pid in self.photos:
            fs = self.fsum(pid)
            if fs:
                grades[fs["g"]] = grades.get(fs["g"], 0) + 1
        return {
            "photos": len(ph),
            "files": sum(len(p.files) for p in ph),
            "bytes": sum(p.size for p in ph),
            "days": len(g.days),
            "bursts": len(g.bursts),
            "burst_photos": sum(len(b.photo_ids) for b in g.bursts.values()),
            "singles": sum(d.single_count for d in g.days),
            "undated": len(g.undated),
            "pairs": sum(1 for p in ph if len(p.exts) > 1),
            "no_subsec": sum(1 for p in ph if p.dt and not p.has_subsec),
            "other_files": self.other_files,
            "cameras": sorted({p.meta.get("Model") for p in ph if p.meta.get("Model")}),
            "analyzed": sum(grades.values()),
            "grades": grades,
            "rejected": sum(1 for v in self.ratings.values() if v < 0),
            "starred": sum(1 for v in self.ratings.values() if v > 0),
        }

    def library_json(self) -> dict:
        with self.lock:
            if self.root is None or self.grouping is None:
                return {"root": None, "version": self.version, "seq": self.seq, "settings": self.settings}
            g = self.grouping
            days = []
            for d in g.days:
                items = []
                for it in d.items:
                    if it["type"] == "single":
                        items.append({"type": "single", "id": it["id"]})
                    else:
                        b = g.bursts[it["id"]]
                        info = self.burst_info(b.id)
                        mid = b.photo_ids[len(b.photo_ids) // 2]
                        items.append({"type": "burst", "id": b.id, "name": b.name, "count": len(b.photo_ids),
                                      "duration": round(b.duration, 2), "cover": info["best"] or mid,
                                      "photo_ids": b.photo_ids, "camera": b.camera, **info,
                                      "fps": round((len(b.photo_ids) - 1) / b.duration, 1) if b.duration > 0 else None})
                days.append({"date": d.date, "weekday": grouping.weekday_cn(d.date), "photo_count": d.photo_count,
                             "burst_count": d.burst_count, "single_count": d.single_count, "items": items})
            return {"root": str(self.root), "version": self.version, "seq": self.seq, "settings": self.settings,
                    "stats": self.stats(), "days": days, "undated": g.undated,
                    "photos": {pid: self.light(p) for pid, p in self.photos.items()}}

    def updates(self, since: int) -> dict:
        with self.lock:
            pids = [pid for pid, s in self.changed.items() if s > since and pid in self.photos]
            bids = {self.burst_of[pid] for pid in pids if pid in self.burst_of}
            return {"seq": self.seq, "version": self.version,
                    "photos": {pid: {"f": self.fsum(pid), "r": self.ratings.get(pid, 0)} for pid in pids},
                    "bursts": {bid: self.burst_info(bid) for bid in bids if bid in self.grouping.bursts},
                    "stats": self.stats(), "analysis": self.analyzer.status()}

    def get(self, pid: str) -> Photo:
        p = self.photos.get(pid)
        if p is None:
            raise HTTPException(404, "照片不存在，请重新扫描")
        return p


LIB = Library()
app = FastAPI(title="BirdDesk", version=__version__)


# ---------------------------------------------------------------- models
class ScanReq(BaseModel):
    root: str
    recursive: Optional[bool] = None


class GroupReq(BaseModel):
    gap: Optional[float] = None
    min_burst: Optional[int] = None
    split_by_camera: Optional[bool] = None


class SettingsReq(BaseModel):
    strictness: Optional[str] = None
    auto_analyze: Optional[bool] = None
    sort: Optional[str] = None
    auto_species: Optional[bool] = None


class PlanReq(BaseModel):
    target_root: Optional[str] = None
    rename_conflicts: bool = True
    reject_to_folder: Optional[bool] = None


class ExecReq(BaseModel):
    plan_id: str
    cleanup_empty: bool = True


class UndoReq(BaseModel):
    journal_id: str


class PathReq(BaseModel):
    path: Optional[str] = None
    id: Optional[str] = None


class RateReq(BaseModel):
    ids: list[str]
    rating: int


class CullReq(BaseModel):
    burst_id: str
    keep: int = 1


class AirDropReq(BaseModel):
    ids: list[str]


class TrashReq(BaseModel):
    ids: list[str]
    note: str = ""


class TrashUndoReq(BaseModel):
    journal_id: str


class VideoReq(BaseModel):
    burst_id: str
    fps: int = 30            # 输出帧率
    slow: float = 4.0        # 慢放倍数（仅展示/兜底）
    repeat: Optional[int] = None   # 每帧重复次数（前端按目标时长算好，优先使用）
    tag: Optional[str] = None      # 输出文件名里的倍数标记
    max_long: int = 1920
    include_rejected: bool = False
    fade: float = 0.0


class DevelopReq(BaseModel):
    pid: str
    preset: str = "standard"
    denoise: Optional[str] = None      # ai / off
    subject: Optional[bool] = None     # 主体增强
    background: Optional[bool] = None  # 背景处理
    vignette: Optional[float] = None
    bit_depth: Optional[int] = 8       # 预览始终 8；导出可为 16


class DevelopBatchReq(BaseModel):
    ids: list[str]
    preset: str = "standard"
    denoise: Optional[str] = None
    subject: Optional[bool] = None
    background: Optional[bool] = None
    bit_depth: Optional[int] = 8       # 16 → 16bit TIFF（RAW 源优先）


class ConfirmReq(BaseModel):
    ids: list[str]
    zh: str
    latin: Optional[str] = None


class AnalyzeReq(BaseModel):
    force: bool = False


# ---------------------------------------------------------------- api
@app.get("/api/env")
def env():
    ok, ver = metadata.exiftool_available()
    return {"version": __version__, "native": native_pick_folder is not None,
            "exiftool": ok, "exiftool_version": ver, "rawpy": thumbs.rawpy is not None,
            "model": analyzer.model_present(), "settings": LIB.settings,
            "species": {"model": species.model_available(), "index": species.index_available(),
                        "count": species.load_index()["count"] if species.index_available() else 0},
            "denoise": {"model": denoise.available()},
            "video": {"ffmpeg": bool(video.ffmpeg())},
            "airdrop": {"available": airdrop.available()}}


@app.post("/api/pick_folder")
def pick_folder(req: PathReq):
    start = req.path or LIB.settings.get("last_root") or str(Path.home() / "Pictures")
    if native_pick_folder is not None:
        path = native_pick_folder(start)
    else:
        script = 'POSIX path of (choose folder with prompt "选择照片目录")'
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        path = out.stdout.strip() if out.returncode == 0 else None
    return {"path": path.rstrip("/") if path else None}


@app.post("/api/scan")
def api_scan(req: ScanReq):
    root = Path(req.root).expanduser()
    if not root.is_dir():
        raise HTTPException(400, f"目录不存在：{root}")
    if req.recursive is not None:
        LIB.settings["recursive"] = bool(req.recursive)
    job = LIB.start_job("扫描", lambda j: LIB.do_scan(j, root))
    return job.to_dict()


@app.get("/api/status")
def status():
    return {"job": LIB.job.to_dict() if LIB.job else None, "version": LIB.version,
            "seq": LIB.seq, "analysis": LIB.analyzer.status()}


@app.get("/api/library")
def library():
    return JSONResponse(LIB.library_json())


@app.get("/api/updates")
def updates(since: int = 0):
    return JSONResponse(LIB.updates(since))


@app.post("/api/regroup")
def regroup(req: GroupReq):
    with LIB.lock:
        if req.gap is not None:
            LIB.settings["gap"] = max(0.05, min(30.0, float(req.gap)))
        if req.min_burst is not None:
            LIB.settings["min_burst"] = max(2, min(100, int(req.min_burst)))
        if req.split_by_camera is not None:
            LIB.settings["split_by_camera"] = bool(req.split_by_camera)
        save_settings(LIB.settings)
        if LIB.photos:
            LIB.regroup()
    return JSONResponse(LIB.library_json())


@app.post("/api/settings")
def settings(req: SettingsReq):
    with LIB.lock:
        if req.strictness in focus.STRICTNESS:
            LIB.settings["strictness"] = req.strictness
            LIB.version += 1
        if req.auto_analyze is not None:
            LIB.settings["auto_analyze"] = bool(req.auto_analyze)
        # 排序只影响界面展示，不改动分组，也不需要让整理预览失效
        if req.auto_species is not None:
            LIB.settings["auto_species"] = bool(req.auto_species)
            analyzer.SPECIES_ENABLED = bool(req.auto_species)
            LIB.version += 1
        if req.sort in SORTS:
            LIB.settings["sort"] = req.sort
        save_settings(LIB.settings)
    return JSONResponse(LIB.library_json())


@app.post("/api/analysis/start")
def analysis_start(req: AnalyzeReq):
    if not analyzer.model_present():
        raise HTTPException(400, "缺少识别模型文件 models/yolo11m-seg.pt")
    if LIB.job and LIB.job.running:
        raise HTTPException(409, "请等当前任务完成")
    LIB.start_analysis(force=req.force)
    return LIB.analyzer.status()


@app.post("/api/analysis/stop")
def analysis_stop():
    LIB.analyzer.stop(wait=False)
    return LIB.analyzer.status()


@app.get("/api/photo/{pid}")
def photo_detail(pid: str):
    p = LIB.get(pid)
    with LIB.lock:
        d = LIB.light(p)
        d["files"] = [{"name": f.name, "path": str(f), "size": f.stat().st_size if f.exists() else 0} for f in p.files]
        d["date"] = p.dt.strftime("%Y-%m-%d") if p.dt else None
        d["has_subsec"] = p.has_subsec
        d["meta"] = p.meta
        d["analysis"] = LIB.analysis.get(pid)
        bid = LIB.burst_of.get(pid)
        if bid:
            b = LIB.grouping.bursts[bid]
            vals = sorted(((LIB.rank_value(x) or -9, x) for x in b.photo_ids), reverse=True)
            d["burst_rank"] = [x for _, x in vals].index(pid) + 1
            d["burst_size"] = len(b.photo_ids)
            d["burst_best"] = LIB.burst_info(bid)["best"]
    return d


@app.get("/thumb/{pid}")
def thumb(pid: str, s: int = 360):
    s = 360 if s <= 400 else (800 if s <= 900 else 2048)
    p = LIB.get(pid)
    try:
        path = thumbs.render(p, s)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"无法生成预览：{e}")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/heat/{pid}")
def heat(pid: str):
    p = LIB.get(pid)
    a = LIB.analysis.get(pid)
    if not a or not a.get("focus"):
        raise HTTPException(404, "没有可用的合焦分析")
    out = THUMB_DIR / f"{pid}_heat_{analyzer.VERSION}.png"
    if not out.exists():
        out.write_bytes(analyzer.heat_png(p, a))
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/crop/{pid}")
def crop(pid: str, x: float = 0.5, y: float = 0.5, w: int = 1400, h: int = 933):
    p = LIB.get(pid)
    w, h = max(200, min(3000, w)), max(200, min(3000, h))
    data, info = analyzer.crop_jpg(p, x, y, w, h)
    return Response(data, media_type="image/jpeg",
                    headers={"X-Crop": json.dumps(info), "Cache-Control": "no-store"})


@app.post("/api/rate")
def rate(req: RateReq):
    rating = max(-1, min(5, int(req.rating)))
    with LIB.lock:
        photos = [LIB.get(pid) for pid in req.ids]
        before = LIB.seq
    errs = ratings.write_ratings(photos, rating)
    with LIB.lock:
        for p in photos:
            if rating:
                LIB.ratings[p.id] = rating
            else:
                LIB.ratings.pop(p.id, None)
            LIB._touch(p.id)
        LIB.plans.clear()
    if errs:
        raise HTTPException(500, "部分评级写入失败：" + "；".join(errs[:3]))
    return LIB.updates(before) | {"ok": True}


@app.post("/api/burst/cull")
def burst_cull(req: CullReq):
    """保留连拍里排名前 keep 张，其余"未评级"的标记为淘汰（已打星的不动）。"""
    with LIB.lock:
        b = LIB.grouping.bursts.get(req.burst_id) if LIB.grouping else None
        if b is None:
            raise HTTPException(404, "找不到这组连拍")
        vals = [(LIB.rank_value(pid), pid) for pid in b.photo_ids]
        if any(v is None for v, _ in vals):
            raise HTTPException(409, "这组连拍还没分析完，请稍候")
        order = [pid for _, pid in sorted(vals, reverse=True)]
        keep = set(order[: max(1, req.keep)])
        rej = [pid for pid in b.photo_ids if pid not in keep and LIB.ratings.get(pid, 0) == 0]
    if rej:
        rate(RateReq(ids=rej, rating=-1))
    return {"rejected": len(rej), "kept": list(keep)}


def _dev_params(req) -> develop.Params:
    p = develop.Params.preset(req.preset)
    if req.denoise in ("ai", "off"):
        p.denoise = req.denoise
    if req.subject is False:
        p.texture = p.clarity = p.shadows = p.highlights = p.eye = 0.0
    if req.background is False:
        p.bg_darken = p.bg_desat = p.bg_blur = p.bg_denoise = 0.0
    if getattr(req, "vignette", None) is not None:
        p.vignette = max(0.0, min(1.0, float(req.vignette)))
    return p


def _dev_eyes(photo_id: str) -> list[dict]:
    a = LIB.analysis.get(photo_id) or {}
    eyes = ((a.get("eye") or {}).get("eyes")) or []
    return [e for e in eyes if e.get("stable")]


def _dev_cache(pid: str, params: develop.Params) -> Path:
    import hashlib
    key = hashlib.sha1(f"{pid}|{params.to_dict()}|{analyzer.VERSION}".encode()).hexdigest()[:12]
    return THUMB_DIR / f"dev_{key}.jpg"


@app.post("/api/airdrop")
def airdrop_send(req: AirDropReq):
    """把选中照片的成片（JPG 优先）交给系统隔空传送面板。"""
    with LIB.lock:
        photos = [LIB.get(pid) for pid in req.ids]
    if not photos:
        raise HTTPException(400, "没有选中照片")
    paths = [p.display for p in photos]
    res = airdrop.send_files(paths)
    return {**res, "names": [p.display.name for p in photos][:10]}


@app.post("/api/trash")
def trash_photos(req: TrashReq):
    """把选中的照片（含 RAW/JPG/XMP）移到系统废纸篓，可撤销。"""
    if not trash.available():
        raise HTTPException(400, "当前环境不支持系统废纸篓")
    with LIB.lock:
        if LIB.root is None:
            raise HTTPException(400, "请先选择目录")
        if LIB.job and LIB.job.running:
            raise HTTPException(409, "有任务正在进行")
        photos = [LIB.get(pid) for pid in req.ids]
        root = LIB.root
    # 只允许删自己目录树里的文件，避免误删外部路径
    for p in photos:
        for f in p.files:
            if os.path.commonpath([str(root), str(f)]) != str(root):
                raise HTTPException(400, f"拒绝删除目录外的文件：{f}")

    def run(job: Job):
        job.message = "暂停后台分析…"
        LIB.analyzer.stop()
        res = trash.trash_photos(photos, note=req.note,
                                 progress=lambda d, t, m: (setattr(job, "done", d),
                                                           setattr(job, "total", t),
                                                           setattr(job, "message", m)))
        with LIB.lock:
            for ph in photos:
                LIB.photos.pop(ph.id, None)
                LIB.ratings.pop(ph.id, None)
                LIB.confirmed.pop(ph.id, None)
                LIB.analysis.pop(ph.id, None)
            LIB.regroup()
        job.message = "完成"
        return res
    return LIB.start_job("删除", run).to_dict()


@app.post("/api/trash/undo")
def trash_undo(req: TrashUndoReq):
    def run(job: Job):
        res = trash.undo_trash(req.journal_id,
                               progress=lambda d, t, m: (setattr(job, "done", d),
                                                         setattr(job, "total", t),
                                                         setattr(job, "message", m)))
        if LIB.root is not None and LIB.root.is_dir():
            job.message = "正在重新扫描…"
            LIB.do_scan(job, LIB.root)
        job.message = "完成"
        return res
    return LIB.start_job("恢复", run).to_dict()


@app.post("/api/burst/video")
def burst_video(req: VideoReq):
    """把一组连拍按指定帧率编码成 MP4（升格），输出到 <root>/_视频/。"""
    with LIB.lock:
        if LIB.root is None:
            raise HTTPException(400, "请先选择目录")
        if LIB.job and LIB.job.running:
            raise HTTPException(409, "有任务正在进行")
        b = LIB.grouping.bursts.get(req.burst_id) if LIB.grouping else None
        if b is None:
            raise HTTPException(404, "找不到这组连拍")
        photos = [LIB.photos[pid] for pid in b.photo_ids if pid in LIB.photos]
        if not req.include_rejected:
            photos = [p for p in photos if (LIB.ratings.get(p.id, 0) or 0) >= 0]
        if len(photos) < 2:
            raise HTTPException(400, "可用帧少于 2 张，无法生成视频")
        root = LIB.root
        day, name = b.day, b.name
        src_fps = (len(b.photo_ids) - 1) / b.duration if b.duration > 0 else None
    src_int = int(round(src_fps)) if src_fps else 0
    # fps <= 0 表示"跟随源帧率"：不补帧，按连拍原本的节奏拼成视频
    fps = src_int if (int(req.fps) <= 0 and src_int >= 2) else int(req.fps)
    fps = max(2, min(120, fps))
    max_long = int(req.max_long) if req.max_long else 0
    slow = max(1.0, float(req.slow))
    out = video.output_path(root, day, name, (req.tag or f"{slow:g}x"))

    def run(job: Job):
        def prog(d, t, msg):
            job.done, job.total, job.message = d, t, msg
        return video.render(photos, out, fps=fps, max_long=max_long, progress=prog,
                            fade=req.fade, slow=slow, src_fps=src_fps, repeat=req.repeat)
    return LIB.start_job("升格视频", run).to_dict()


@app.post("/api/develop/preview")
def develop_preview(req: DevelopReq):
    """生成出片预览（长边 1400，带缓存）。同步返回，约 1–3 秒。"""
    photo = LIB.get(req.pid)
    params = _dev_params(req)
    cached = _dev_cache(req.pid, params)
    if cached.exists():
        return FileResponse(cached, media_type="image/jpeg", headers={"X-Dev-Meta": '{"cached":true}'})
    t0 = time.time()
    try:
        with LIB.lock:
            det = LIB.analyzer.det if LIB.analyzer.det is not None else analyzer.Detector()
        rgb01, src, mode = develop.prepare(photo, params)
        p2 = develop.Params(**{**params.to_dict(), "denoise": mode})
        out, report = develop.develop_f(rgb01, p2, det=det, eyes=_dev_eyes(req.pid))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"出片失败：{type(e).__name__}: {e}")
    develop.save(cached, out, quality=90)
    # src 里的 denoise 表示实际走了哪条路（含 fallback），要覆盖 develop 的默认值
    meta = {"cached": False, "ms": int((time.time() - t0) * 1000), **report, **src}
    # HTTP 头只能是 latin-1：中文必须转义
    return FileResponse(cached, media_type="image/jpeg",
                        headers={"X-Dev-Meta": json.dumps(meta, ensure_ascii=True)})


@app.post("/api/develop/export")
def develop_export(req: DevelopBatchReq):
    """批量出片：原分辨率处理 → 写到 <root>/_出片/，不覆盖原片。"""
    with LIB.lock:
        if LIB.root is None:
            raise HTTPException(400, "请先选择目录")
        if LIB.job and LIB.job.running:
            raise HTTPException(409, "有任务正在进行")
        photos = [LIB.get(pid) for pid in req.ids]
        root = LIB.root
    params = _dev_params(req)
    out_dir = root / EXPORT_FOLDER

    want16 = int(req.bit_depth or 8) == 16

    def run(job: Job):
        det = None
        done = []
        for i, ph in enumerate(photos):
            job.message = f"出片 {i + 1}/{len(photos)}：{ph.primary.name}"
            job.done, job.total = i, len(photos)
            try:
                with LIB.lock:
                    det = det or (LIB.analyzer.det if LIB.analyzer.det is not None else analyzer.Detector())
                rgb01, src, mode = develop.prepare(ph, params)
                p2 = develop.Params(**{**params.to_dict(), "denoise": mode})
                out, rep = develop.develop_f(rgb01, p2, det=det, eyes=_dev_eyes(ph.id))
                ext = ".tif" if want16 else ".jpg"
                dst = develop.export_paths(ph.display, root, out_dir, ext=ext)
                develop.save(dst, out, quality=94)
                done.append({"id": ph.id, "path": str(dst), "steps": rep.get("steps", []),
                             "bits": 16 if want16 else 8, "source": src.get("source")})
            except Exception as e:  # noqa: BLE001
                done.append({"id": ph.id, "error": f"{type(e).__name__}: {e}"})
        job.done, job.total = len(photos), len(photos)
        return {"out_dir": str(out_dir), "items": done,
                "ok": sum(1 for d in done if "error" not in d)}

    return LIB.start_job("出片", run).to_dict()


@app.post("/api/species/confirm")
def species_confirm(req: ConfirmReq):
    """人工确认鸟种：写 XMP 关键词（Lightroom 可见）+ 记入本地库。"""
    zh = (req.zh or "").strip()
    if not zh:
        raise HTTPException(400, "缺少鸟种名称")
    latin = (req.latin or "").strip()
    with LIB.lock:
        photos = [LIB.get(pid) for pid in req.ids]
        before = LIB.seq
    errs = ratings.write_keywords(photos, [k for k in (zh, latin) if k])
    with LIB.lock:
        for ph in photos:
            LIB.confirmed[ph.id] = {"zh": zh, "latin": latin, "ts": time.time()}
            _save_confirmed(ph.id, zh, latin)
            LIB._touch(ph.id)
    if errs:
        raise HTTPException(500, "部分关键词写入失败：" + "；".join(errs[:3]))
    return LIB.updates(before) | {"ok": True}


@app.post("/api/plan")
def plan(req: PlanReq):
    with LIB.lock:
        if LIB.root is None:
            raise HTTPException(400, "请先选择目录并扫描")
        if LIB.job and LIB.job.running:
            raise HTTPException(409, "有任务正在进行")
        target = Path(req.target_root).expanduser() if req.target_root else LIB.root
        if target.exists() and not target.is_dir():
            raise HTTPException(400, "目标不是文件夹")
        LIB.settings["rename_conflicts"] = req.rename_conflicts
        if req.reject_to_folder is not None:
            LIB.settings["reject_to_folder"] = req.reject_to_folder
        save_settings(LIB.settings)
        rej = {pid for pid, v in LIB.ratings.items() if v < 0} if LIB.settings["reject_to_folder"] else set()
        pl = organizer.make_plan(list(LIB.photos.values()), LIB.root, target,
                                 rename_conflicts=req.rename_conflicts, reject_ids=rej)
        pl["version"] = LIB.version
        pl["seq"] = LIB.seq
        LIB.plans = {pl["plan_id"]: pl}
        return pl


@app.post("/api/execute")
def execute(req: ExecReq):
    with LIB.lock:
        pl = LIB.plans.get(req.plan_id)
        if pl is None or pl.get("version") != LIB.version:
            raise HTTPException(409, "整理预览已过期（分组或评级变了），请重新生成预览")
        if not pl["summary"]["enough_space"]:
            raise HTTPException(400, "目标磁盘空间不足")
        LIB.settings["cleanup_empty"] = req.cleanup_empty
        save_settings(LIB.settings)

    def run(job: Job):
        job.message = "暂停后台分析…"
        LIB.analyzer.stop()

        def prog(d, t, msg):
            job.done, job.total, job.message = d, t, msg
        res = organizer.execute(pl, cleanup_empty=req.cleanup_empty, progress=prog)
        job.message = "整理完成，正在刷新…"
        LIB.do_scan(job, Path(pl["target_root"]))
        job.message = "完成"
        return res
    return LIB.start_job("整理", run).to_dict()


@app.get("/api/journals")
def journals():
    items = [dict(j, kind="move") for j in organizer.list_journals()]
    items += trash.list_trash_journals()
    items.sort(key=lambda j: j.get("created") or 0, reverse=True)
    return items


@app.post("/api/undo")
def undo(req: UndoReq):
    info = next((j for j in organizer.list_journals() if j["id"] == req.journal_id), None)
    if info is None:
        raise HTTPException(404, "找不到这次整理记录")

    def run(job: Job):
        LIB.analyzer.stop()

        def prog(d, t, msg):
            job.done, job.total, job.message = d, t, msg
        res = organizer.undo(req.journal_id, progress=prog)
        if info.get("source_root") and Path(info["source_root"]).is_dir():
            LIB.do_scan(job, Path(info["source_root"]))
        job.message = "完成"
        return res
    return LIB.start_job("撤销", run).to_dict()


@app.post("/api/reveal")
def reveal(req: PathReq):
    if req.id:
        p = LIB.get(req.id)
        subprocess.run(["open", "-R", str(p.primary)])
    elif req.path and Path(req.path).exists():
        subprocess.run(["open", req.path])
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
