"""按日期 + 连拍分组。纯计算，不碰文件。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import BURST_PREFIX
from .scanner import Photo

WEEKDAYS = "一二三四五六日"


@dataclass
class Burst:
    id: str
    day: str
    name: str
    photo_ids: list[str]
    start: float
    end: float
    camera: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Day:
    date: str
    items: list[dict] = field(default_factory=list)   # 时间顺序：{"type": "burst"/"single", ...}
    photo_count: int = 0
    burst_count: int = 0
    single_count: int = 0


@dataclass
class Grouping:
    days: list[Day]
    bursts: dict[str, Burst]
    undated: list[str]
    settings: dict


def _is_continuous(prev: Photo, cur: Photo, gap: float) -> bool:
    d = cur.ts - prev.ts
    if prev.has_subsec and cur.has_subsec:
        return d <= gap + 1e-6
    # 缺少亚秒信息时只能精确到 1 秒，放宽到至少 1 秒
    return d <= max(gap, 1.0) + 1e-6


def group(photos: list[Photo], gap: float = 0.5, min_burst: int = 2,
          split_by_camera: bool = True) -> Grouping:
    gap = max(0.05, float(gap))
    min_burst = max(2, int(min_burst))
    dated = [p for p in photos if p.dt is not None]
    undated = [p for p in photos if p.dt is None]
    for p in photos:
        p.day = p.burst = None
        p.gap_prev = None

    def cam(p: Photo) -> str:
        return p.camera_key if split_by_camera else ""

    dated.sort(key=lambda p: (cam(p), p.ts, p.stem))

    runs: list[list[Photo]] = []
    prev: Optional[Photo] = None
    for p in dated:
        if prev is not None and cam(prev) == cam(p):
            p.gap_prev = p.ts - prev.ts
            if _is_continuous(prev, p, gap):
                runs[-1].append(p)
                prev = p
                continue
        runs.append([p])
        prev = p

    days: dict[str, Day] = {}
    bursts: dict[str, Burst] = {}
    used_names: dict[str, set[str]] = {}
    runs.sort(key=lambda r: (r[0].ts, r[0].stem))
    for run in runs:
        day_key = run[0].dt.strftime("%Y-%m-%d")
        day = days.setdefault(day_key, Day(date=day_key))
        if len(run) >= min_burst:
            base = BURST_PREFIX + run[0].dt.strftime("%H%M%S")
            names = used_names.setdefault(day_key, set())
            name, n = base, 2
            while name in names:
                name = f"{base}_{n}"
                n += 1
            names.add(name)
            bid = f"{day_key}/{name}"
            b = Burst(id=bid, day=day_key, name=name, photo_ids=[p.id for p in run],
                      start=run[0].ts, end=run[-1].ts, camera=run[0].meta.get("Model", ""))
            bursts[bid] = b
            for p in run:
                p.day, p.burst = day_key, name
            day.items.append({"type": "burst", "id": bid, "ts": b.start})
            day.burst_count += 1
        else:
            for p in run:
                p.day = day_key
                day.items.append({"type": "single", "id": p.id, "ts": p.ts})
                day.single_count += 1
        day.photo_count += len(run)

    for d in days.values():
        d.items.sort(key=lambda it: it["ts"])
    return Grouping(
        days=[days[k] for k in sorted(days)],
        bursts=bursts,
        undated=[p.id for p in sorted(undated, key=lambda p: p.stem)],
        settings={"gap": gap, "min_burst": min_burst, "split_by_camera": split_by_camera},
    )


def weekday_cn(date_str: str) -> str:
    from datetime import date
    y, m, d = map(int, date_str.split("-"))
    return "周" + WEEKDAYS[date(y, m, d).weekday()]
