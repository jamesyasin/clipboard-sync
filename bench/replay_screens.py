#!/usr/bin/env python3
"""分屏回放验证：拿 tmp/named/ 里已归档的真实图，按店分组、按时间顺序重跑一遍判定，
对比「文件名里的屏位」和「现在判定出来的屏位」，并打印每张图的判定依据。

用法：python3 bench/replay_screens.py [--old]
      --old = 关掉兜底守卫，对比修复前的行为
"""
import os
import re
import sys
import glob
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync                      # noqa: E402
from ocr import OCR, match_screen, normalize   # noqa: E402

NAMED = os.path.join(sync.ROOT, "tmp", "named")
SCREENS = ["第一屏", "第二屏", "第三屏", "第四屏"]


def main():
    old = "--old" in sys.argv
    cfg = sync.load_cfg()
    if old:
        cfg["flow"]["fallback_guard"] = False
    rules = cfg["rules"]
    flow = cfg.get("flow", {}) or {}
    fscreens = flow.get("screens") or []

    files = sorted(glob.glob(os.path.join(NAMED, "*.png")))
    groups = collections.OrderedDict()
    for f in files:
        base = os.path.basename(f)
        m = re.match(r"(.+?)_(第一屏|第二屏|第三屏|第四屏)_(\d{4}_\d{6})\.png$", base)
        if not m:
            continue
        groups.setdefault(m.group(1), []).append((m.group(3), m.group(2), f))

    ocr = OCR(cfg["ocr"], sync.ROOT, lambda m: None)
    try:
        ocr.prewarm()
    except Exception:
        pass

    print(f"模式：{'修复前（关掉兜底守卫）' if old else '修复后（开启兜底守卫）'}\n")
    bad = 0
    total = 0
    for store, items in groups.items():
        items.sort()
        print(f"===== {store} =====")
        counts = {s: 0 for s in SCREENS}
        for i, (_ts, label, path) in enumerate(items, 1):
            text = ocr.text(path)
            norm = normalize(text)
            fidx = sum(counts.get(s, 0) for s in fscreens)
            ctx = {"force": None, "flow": flow, "flow_index": fidx, "counts": dict(counts)}
            sc = match_screen(norm, rules, ctx)
            why = ctx.get("decision") or ("无", "-")
            targets = [sc] if sc else []
            if (sc in fscreens and flow.get("enabled") and
                    flow.get("mode", "duplicate_first") == "duplicate_first" and fidx == 0):
                skip = [normalize(w) for w in (flow.get("skip_duplicate_if") or [])]
                targets = ([fscreens[0]] if (skip and any(w and w in norm for w in skip))
                           else list(fscreens))
            for s in targets:
                counts[s] = counts.get(s, 0) + 1
            ok = (sc == label)
            total += 1
            if not ok:
                bad += 1
            print(f"  {'✅' if ok else '❌'} 第{i}张 {os.path.basename(path)[-24:]:26s} "
                  f"标注={label} 判定={sc or '未识别'} 依据={why[0]}")
            if not ok:
                print(f"        ↳ {why[1]}")
                print(f"        ↳ OCR: {norm[:70]}")
        print(f"  本店分屏结果: {dict(counts)}\n")

    print(f"总计 {total} 张，与归档标注不一致 {bad} 张")
    ocr.close()


if __name__ == "__main__":
    main()
