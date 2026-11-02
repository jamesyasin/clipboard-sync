#!/usr/bin/env python3
"""判定首张 15s 是「每个进程一次」还是「机器一次」，以及常驻模式能否摊掉。"""
import os
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "bin", "visionocr_worker")
IMG = os.path.join(ROOT, "tmp", "clip_1793373848202.png")
END = b"\x01\n"
NEWLINE = b"\n"


def timed_call(proc, path):
    t0 = time.time()
    proc.stdin.write((path + "\n").encode())
    proc.stdin.flush()
    chunks = []
    while True:
        b = proc.stdout.read(65536)
        if not b:
            break
        i = b.find(END)
        if i >= 0:
            chunks.append(b[:i])
            break
        chunks.append(b)
    return (time.time() - t0), b"".join(chunks)


print("=== A) 一个常驻进程内连续 4 次 ===")
w = subprocess.Popen([WORKER, "--worker", "zh-Hans,en-US", "0"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, bufsize=0)
try:
    for i in range(4):
        dt, txt = timed_call(w, IMG)
        print(f"  第{i+1}次: {dt*1000:8.0f}ms  文本行数={txt.count(NEWLINE)}")
except Exception as e:
    print("  err", e)
finally:
    try:
        w.stdin.close()
    except Exception:
        pass
    try:
        w.wait(timeout=5)
    except Exception:
        w.kill()

print("=== B) 连续 3 个独立进程，各跑 1 次（看是否机器级缓存）===")
for i in range(3):
    p = subprocess.Popen([WORKER, "--worker", "zh-Hans,en-US", "0"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, bufsize=0)
    t0 = time.time()
    dt, txt = timed_call(p, IMG)
    print(f"  进程{i+1}: 本次识别 {dt*1000:8.0f}ms（含进程启动共 {(time.time()-t0)*1000:.0f}ms）")
    try:
        p.stdin.close()
    except Exception:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
