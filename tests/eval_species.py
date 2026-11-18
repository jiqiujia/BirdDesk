"""在带标签的公开鸟片上评估鸟种识别：top-1 / top-5 / 弃权率。

用法：python tests/eval_species.py /tmp/birdsp
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app import analyzer, species  # noqa: E402


def main(d: Path) -> None:
    truth = json.loads((d / "truth.json").read_text("utf-8"))
    det = analyzer.Detector()
    clf = species.classifier()
    t0 = time.time()
    clf._load()
    load_s = time.time() - t0
    rows, skipped = [], 0
    for name, meta in sorted(truth.items()):
        p = d / name
        if not p.exists():
            continue
        bgr = cv2.imread(str(p))
        if bgr is None:
            skipped += 1
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        birds = det.detect(rgb)
        if not birds:
            skipped += 1
            print(f"{name:44} 未检测到鸟（跳过）")
            continue
        main_i = int(np.argmax([b["conf"] * max(1.0, (b["box"][2]-b["box"][0]) * (b["box"][3]-b["box"][1])) ** 0.5
                                for b in birds]))
        ta = time.time()
        r = clf.classify(rgb, birds[main_i]["box"])
        ms = (time.time() - ta) * 1000
        top = r.get("top") or []
        # 名录用新分类（如 金眶鸻=Thinornis dubius），中文名才是稳定键
        hit = lambda t: t["latin"] == meta["latin"] or t["zh"] == meta["zh"]  # noqa: E731
        top1 = bool(top) and hit(top[0])
        in5 = any(hit(t) for t in top)
        rows.append({"file": name, "truth": meta, "top1": top1, "in5": in5,
                     "status": r.get("status"), "p1": r.get("p1"), "margin": r.get("margin"), "ms": ms,
                     "pred": top[:3]})
        mark = "✓" if top1 else ("~" if in5 else "✗")
        best = top[0] if top else {"zh": "-", "p": 0}
        print(f"{mark} {name:42} 真值 {meta['zh']:8} 预测 {best['zh']:8} p={best.get('p', 0):.3f} "
              f"[{r.get('status')}] {ms:.0f}ms")

    n = len(rows)
    if not n:
        print("没有可用样本")
        return
    top1 = sum(r["top1"] for r in rows) / n
    in5 = sum(r["in5"] for r in rows) / n
    ok = [r for r in rows if r["status"] == "ok"]
    ok1 = (sum(r["top1"] for r in ok) / len(ok)) if ok else 0
    print("\n=== 汇总 ===")
    print(f"样本 {n}（跳过 {skipped}） 模型加载 {load_s:.1f}s  平均 {(sum(r['ms'] for r in rows)/n):.0f} ms/张")
    print(f"top-1 {top1:.1%}   top-5 {in5:.1%}")
    print(f"status=ok 的 {len(ok)} 张里 top-1 {ok1:.1%}")
    # 按 p1 分档看准确率，用于选阈值
    print("\np1 分档（样本数 / top-1 / top-5）:")
    for lo, hi in [(0, .08), (.08, .15), (.15, .20), (.20, .35), (.35, .6), (.6, 1.01)]:
        g = [r for r in rows if lo <= (r["p1"] or 0) < hi]
        if g:
            print(f"  [{lo:.2f},{hi:.2f}) {len(g):3d}  {sum(r['top1'] for r in g)/len(g):.0%}  {sum(r['in5'] for r in g)/len(g):.0%}")
    print("\nmargin 分档（样本数 / top-1）:")
    for lo, hi in [(0, .05), (.05, .12), (.12, .25), (.25, .5), (.5, 1.01)]:
        g = [r for r in rows if lo <= (r["margin"] or 0) < hi]
        if g:
            print(f"  [{lo:.2f},{hi:.2f}) {len(g):3d}  {sum(r['top1'] for r in g)/len(g):.0%}")
    print("\n错的样本（p1 / margin）:")
    for r in rows:
        if not r["top1"]:
            print(f"  {r['file']:42} {r['truth']['zh']:8} → {r['pred'][0]['zh']:10} p={r['p1']:.2f} m={r['margin']:.2f}")
    bad = [r for r in rows if not r["in5"]]
    if bad:
        print("\n未进入 top-5 的样本:")
        for r in bad:
            print(f"  {r['file']:44} 真值 {r['truth']['zh']:8} → " + ", ".join(f"{t['zh']}({t['p']:.2f})" for t in r["pred"]))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/birdsp"))
