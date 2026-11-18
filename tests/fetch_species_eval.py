"""从 Wikimedia Commons 拉取带物种标签的鸟片，用于评估鸟种识别准确率。

用法：python tests/fetch_species_eval.py [输出目录] [每种图片数]
"""
import json
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# 中国常见鸟，覆盖不同目/体型
SPECIES = [
    ("Egretta garzetta", "白鹭"),
    ("Ardeola bacchus", "池鹭"),
    ("Nycticorax nycticorax", "夜鹭"),
    ("Tachybaptus ruficollis", "小鸊鷉"),
    ("Gallinula chloropus", "黑水鸡"),
    ("Anas platyrhynchos", "绿头鸭"),
    ("Charadrius dubius", "金眶鸻"),
    ("Spilopelia chinensis", "珠颈斑鸠"),
    ("Alcedo atthis", "普通翠鸟"),
    ("Upupa epops", "戴胜"),
    ("Picus canus", "灰头绿啄木鸟"),
    ("Lanius schach", "棕背伯劳"),
    ("Urocissa erythroryncha", "红嘴蓝鹊"),
    ("Cyanopica cyanus", "灰喜鹊"),
    ("Pica serica", "喜鹊"),
    ("Parus minor", "大山雀"),
    ("Pycnonotus sinensis", "白头鹎"),
    ("Copsychus saularis", "鹊鸲"),
    ("Passer montanus", "麻雀"),
    ("Hirundo rustica", "家燕"),
    ("Milvus migrans", "黑鸢"),
    ("Apus nipalensis", "小白腰雨燕"),
]

API = "https://commons.wikimedia.org/w/api.php"
UA = "BirdDesk/0.3 (local species evaluation; contact: local user)"


def _curl(url: str, out: Path | None = None, timeout: int = 60) -> bytes:
    cmd = ["curl", "-fsSL", "-A", UA, "-m", str(timeout)]
    if out:
        cmd += ["-o", str(out)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:200] or f"curl {r.returncode}")
    return r.stdout


def api(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            return json.loads(_curl(url, timeout=40))
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))
    return {}


def fetch(latin: str, n: int) -> list[str]:
    d = api({"action": "query", "format": "json", "generator": "search",
             "gsrsearch": f"{latin} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": n * 4,
             "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 1000})
    out = []
    for p in (d.get("query", {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png"):
            continue
        # 文件名里要出现属名或种加词，降低混入无关图片的概率
        title = p["title"].lower()
        genus, epithet = latin.lower().split()[:2]
        if genus not in title and epithet not in title:
            continue
        out.append(ii.get("thumburl") or ii["url"])
        if len(out) >= n:
            break
    return out


def main(out: Path, per: int):
    out.mkdir(parents=True, exist_ok=True)
    truth = {}
    for latin, zh in SPECIES:
        urls = fetch(latin, per)
        got = 0
        for i, u in enumerate(urls):
            name = f"{latin.replace(' ', '_')}__{i}.jpg"
            dst = out / name
            if not dst.exists():
                try:
                    _curl(u, dst)
                except Exception as e:  # noqa: BLE001
                    print("skip", u, e)
                    continue
                time.sleep(1.0)
            truth[name] = {"latin": latin, "zh": zh}
            got += 1
        print(f"{latin:28} {zh:8} {got}/{len(urls)}")
    (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), "utf-8")
    print("total", len(truth), "->", out)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/birdsp"),
         int(sys.argv[2]) if len(sys.argv) > 2 else 2)
