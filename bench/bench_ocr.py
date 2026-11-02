#!/usr/bin/env python3
"""识别性能剖析：对比「优化前基线」与「优化后」在同一批真实截图上的耗时。

用法：
  python3 bench/bench_ocr.py                 # 默认跑基线 vs 优化后，各 2 轮，每轮 8 张
  python3 bench/bench_ocr.py --rounds 3 --limit 12
  python3 bench/bench_ocr.py --only optimized

说明：
  * 基线目录 bench/ 是 2026-10-31 01:08 封存的「重要版本」代码副本（识别结果与线上一致）。
  * optimized 直接 import 当前目录（clipboard_sync）的 ocr.py。
  * 判定的准确率不变：两边都用同样的 match_screen + rules，本脚本会逐张比对判定结果。
"""
import argparse
import glob
import importlib
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def pick_images(limit):
    pats = [os.path.join(ROOT, "tmp", "*.png"), os.path.join(ROOT, "tmp", "named", "*.png"),
            os.path.join(ROOT, "unmatched", "*.png")]
    files = []
    for p in pats:
        files += sorted(glob.glob(p))
    files = [f for f in files if os.path.getsize(f) > 1000]
    if not files:
        raise SystemExit("没有可用于测量的图片（tmp/ 或 unmatched/ 为空）")
    step = max(1, len(files) // max(1, limit))
    return files[::step][:limit]


def load_module(tag):
    """加载 baseline(bench/) 或 optimized(当前目录) 的 ocr / sync 模块。"""
    if tag == "baseline":
        d = os.path.join(ROOT, "bench")
    else:
        d = ROOT
    sys.path.insert(0, d)
    for m in ("ocr", "sync", "spawn", "clipboard", "feishu"):
        sys.modules.pop(m, None)
    ocr = importlib.import_module("ocr")
    sync = importlib.import_module("sync")
    sys.path.remove(d)
    return ocr, sync


def run(tag, images, rounds):
    ocr_mod, sync_mod = load_module(tag)
    cfg = sync_mod.load_cfg()
    engine = ocr_mod.OCR(cfg.get("ocr", {}), ROOT, lambda *a, **k: None)
    rules = cfg.get("rules", [])
    flow = cfg.get("flow", {}) or {}
    fscreens = flow.get("screens") or []
    per_image = {}
    verdicts = {}
    total = 0.0
    n = 0
    for r in range(rounds):
        # 每轮清空 OCR 缓存，确保测的是真实识别耗时（而不是缓存命中）
        try:
            engine.clear_cache()
        except Exception:
            pass
        counts = {}                      # 模拟真实顺序：每轮按顺序累进
        for p in images:
            t0 = time.time()
            text = engine.text(p)
            ms = (time.time() - t0) * 1000
            norm = ocr_mod.normalize(text)
            fidx = sum(counts.get(s, 0) for s in fscreens)
            sc = ocr_mod.match_screen(norm, rules,
                                      {"force": None, "flow": flow, "flow_index": fidx,
                                       "counts": dict(counts)})
            targets = [sc] if sc else []
            if (sc in fscreens and flow.get("enabled") and
                    flow.get("mode", "duplicate_first") == "duplicate_first" and fidx == 0):
                skip = [ocr_mod.normalize(w) for w in (flow.get("skip_duplicate_if") or [])]
                targets = [fscreens[0]] if (skip and any(w and w in norm for w in skip)) \
                    else list(fscreens)
            for s in targets:
                counts[s] = counts.get(s, 0) + 1
            verdicts.setdefault(os.path.basename(p), []).append(
                (sc, "+".join(targets)))
            per_image.setdefault(os.path.basename(p), []).append(ms)
            total += ms
            n += 1
    flat = [x for v in per_image.values() for x in v]
    return {
        "tag": tag,
        "n": n,
        "total_ms": total,
        "avg_ms": statistics.mean(flat),
        "median_ms": statistics.median(flat),
        "p95_ms": sorted(flat)[max(0, int(len(flat) * 0.95) - 1)],
        "max_ms": max(flat),
        "min_ms": min(flat),
        "per_image": per_image,
        "verdicts": verdicts,
    }


def summarize(res):
    print(f"[{res['tag']}] 共 {res['n']} 次识别 | "
          f"平均 {res['avg_ms']:.0f}ms | 中位 {res['median_ms']:.0f}ms | "
          f"p95 {res['p95_ms']:.0f}ms | 最大 {res['max_ms']:.0f}ms | "
          f"总计 {res['total_ms']/1000:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--only", choices=["baseline", "optimized"], default=None)
    a = ap.parse_args()

    images = pick_images(a.limit)
    print(f"样本：{len(images)} 张真实截图，每轮全跑，共 {a.rounds} 轮\n")

    results = {}
    for tag in (["baseline", "optimized"] if not a.only else [a.only]):
        results[tag] = run(tag, images, a.rounds)
        summarize(results[tag])

    if len(results) == 2:
        b, o = results["baseline"], results["optimized"]
        print(f"\n提速：平均 {b['avg_ms']:.0f}ms -> {o['avg_ms']:.0f}ms "
              f"（{b['avg_ms']/max(1e-6, o['avg_ms']):.2f}x，"
              f"总耗时 {b['total_ms']/1000:.1f}s -> {o['total_ms']/1000:.1f}s）")

        # 准确率核对：同一张图两侧的判定/写入屏位必须完全一致
        diff = []
        for k, vb in b["verdicts"].items():
            vo = o["verdicts"].get(k)
            if vo is None:
                continue
            if vb[0] != vo[0]:
                diff.append((k, vb[0], vo[0]))
        print("判定一致性：" + ("✅ 全部一致（识别结果未变）" if not diff
                             else f"❌ 有 {len(diff)} 张不一致：{diff[:5]}"))


if __name__ == "__main__":
    main()
