#!/usr/bin/env python3
"""常驻模式 vs 单次进程模式：耗时与识别文本一致性对比。"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from spawn import spawn_capture  # noqa: E402

IMGS = []
for d in ("tmp", os.path.join("tmp", "named"), "unmatched"):
    p = os.path.join(ROOT, d)
    if os.path.isdir(p):
        IMGS += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith(".png")]
IMGS = IMGS[:8]
if not IMGS:
    raise SystemExit("没有样本图片")

LANGS = "zh-Hans,en-US"
CORR = "0"
END = b"\x01\n"


def one_shot(imgs):
    out = {}
    for p in imgs:
        t0 = time.time()
        rc, so, se = spawn_capture([os.path.join(ROOT, "bin", "visionocr"), p, LANGS, CORR], timeout=90)
        out[p] = ((time.time() - t0) * 1000, so, rc, se)
    return out


def persistent(imgs):
    out = {}
    exe = os.path.join(ROOT, "bin", "visionocr_worker")
    w = subprocess.Popen([exe, "--worker", LANGS, CORR],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, bufsize=0)
    try:
        for p in imgs:
            t0 = time.time()
            w.stdin.write((p + "\n").encode())
            w.stdin.flush()
            chunks = []
            while True:
                b = w.stdout.read(65536)
                if not b:
                    break
                i = b.find(END)
                if i >= 0:
                    chunks.append(b[:i])
                    break
                chunks.append(b)
            txt = b"".join(chunks).decode("utf-8", "ignore")
            out[p] = ((time.time() - t0) * 1000, txt, 0, "")
    finally:
        try:
            w.stdin.close()
        except Exception:
            pass
        try:
            w.wait(timeout=5)
        except Exception:
            w.kill()
    return out


def report(name, res):
    times = [v[0] for v in res.values()]
    print(f"[{name}] 平均 {sum(times)/len(times):.0f}ms | "
          f"最小 {min(times):.0f}ms | 最大 {max(times):.0f}ms | 总计 {sum(times)/1000:.1f}s")
    return {k: v[1] for k, v in res.items()}


a = one_shot(IMGS)
b = persistent(IMGS)
ta = report("单次进程(现状)", a)
tb = report("常驻进程(优化)", b)

sa = sum(v[0] for v in a.values())
sb = sum(v[0] for v in b.values())
print(f"\n提速：{sa/1000:.1f}s -> {sb/1000:.1f}s（{sa/max(1e-6, sb):.2f}x）")

diff = [k for k in ta if ta[k].strip() != tb.get(k, "").strip()]
print(f"识别文本一致性：{len(ta)-len(diff)}/{len(ta)} 张完全相同")
for k in diff[:3]:
    print("  差异：", os.path.basename(k))
    print("   旧:", repr(ta[k][:100]))
    print("   新:", repr(tb.get(k, "")[:100]))
