#!/usr/bin/env python3
"""干净进程下的三种识别模式基准：耗时 + 文本一致性 + 关键词命中率。

用法：python3 bench/bench_modes.py --trials 4
需要：/tmp/ocr_single（由 /tmp/ocr_single.swift 编译），样本目录 /tmp/bench_imgs
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ocr import normalize  # noqa: E402

EXE = "/tmp/ocr_single"
IMGS = sorted(glob.glob("/tmp/bench_imgs/*.png"))
MODES = ["ane-accurate", "cpu-accurate", "cpu-fast"]
KEYS = ["设备与服务", "座区", "收起", "高端外设专区", "营业中", "团购"]


def call(mode, img):
    t0 = time.time()
    p = subprocess.run([EXE, img, mode], capture_output=True, timeout=120)
    ms = (time.time() - t0) * 1000
    return ms, p.stdout.decode("utf-8", "ignore"), p.returncode, p.stderr.decode("utf-8", "ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=4)
    a = ap.parse_args()
    if not IMGS:
        raise SystemExit("没有样本：/tmp/bench_imgs")
    if not os.path.exists(EXE):
        raise SystemExit(f"缺少 {EXE}")

    res = {}
    for mode in MODES:
        rows = []
        for img in IMGS:
            for _ in range(a.trials):
                ms, txt, rc, err = call(mode, img)
                rows.append({"img": os.path.basename(img), "ms": ms, "ok": rc == 0,
                             "lines": len([x for x in txt.split("\n") if x.strip()]),
                             "text": txt, "err": err[:120]})
        ok = [r for r in rows if r["ok"]]
        times = [r["ms"] for r in ok]
        res[mode] = rows
        print(f"[{mode}] 成功 {len(ok)}/{len(rows)} | "
              f"平均 {sum(times)/len(times):.0f}ms | 最小 {min(times):.0f}ms | 最大 {max(times):.0f}ms"
              if ok else f"[{mode}] 全部失败")

    # 以 ane-accurate 为基准比较文本一致性与关键词命中
    base = {(r["img"], i): r["text"] for i, r in enumerate(
        [x for x in res["ane-accurate"] if x["ok"]])}
    print("\n=== 与 ANE-accurate 的文本一致性（按图片首次成功结果比较）===")
    for mode in MODES:
        if mode == "ane-accurate":
            continue
        same = 0
        tot = 0
        for img in IMGS:
            b = next((r["text"] for r in res["ane-accurate"] if r["img"] == os.path.basename(img) and r["ok"]), None)
            o = next((r["text"] for r in res[mode] if r["img"] == os.path.basename(img) and r["ok"]), None)
            if b is None or o is None:
                continue
            tot += 1
            if normalize(b) == normalize(o):
                same += 1
        print(f"  {mode}: {same}/{tot} 张归一化后完全一致")

    print("\n=== 关键词命中（判定依据，用归一化文本）===")
    for mode in MODES:
        hits = {k: 0 for k in KEYS}
        n = 0
        seen = set()
        for r in res[mode]:
            if not r["ok"] or r["img"] in seen:
                continue
            seen.add(r["img"])
            n += 1
            t = normalize(r["text"])
            for k in KEYS:
                if normalize(k) in t:
                    hits[k] += 1
        print(f"  {mode}: " + " ".join(f"{k}={v}/{n}" for k, v in hits.items()))


if __name__ == "__main__":
    main()
