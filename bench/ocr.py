"""本地 OCR：默认用 macOS 内置 Vision（离线、中文准确），可扩展其他引擎。"""
import os
import re
import threading
import time

from spawn import spawn_capture

# 串行锁：并发跑多个 visionocr 会争抢神经网络引擎(ANE)，
# 系统繁忙时容易抛 esrtError(...precompiled_compute_operation...) —— 串行后基本不再出现
_OCR_LOCK = threading.Lock()

# 全角 -> 半角
_FW = {i: i - 0xFEE0 for i in range(0xFF01, 0xFF5F)}
_FW[0x3000] = 0x20


def normalize(text: str) -> str:
    """归一化：去空格/换行/标点差异，便于关键词容错匹配。"""
    t = text.translate(_FW)
    t = re.sub(r"\s+", "", t)
    t = t.replace("：", ":").replace("，", ",").replace("。", ".")
    return t


class OCR:
    def __init__(self, cfg, root, logger):
        self.cfg = cfg
        self.root = root
        self.log = logger
        self.engine = cfg.get("engine", "vision")
        self.langs = ",".join(cfg.get("langs", ["zh-Hans", "en-US"]))
        self.correction = "1" if cfg.get("correction") else "0"
        self.bin = cfg.get("bin", "bin/visionocr")
        if not os.path.isabs(self.bin):
            self.bin = os.path.join(root, self.bin)

    def text(self, path: str) -> str:
        if self.engine == "vision":
            if not os.path.exists(self.bin):
                raise RuntimeError(f"找不到 OCR 程序: {self.bin}")
            last = None
            for attempt in (1, 2):
                try:
                    with _OCR_LOCK:
                        p2 = spawn_capture([self.bin, path, self.langs, self.correction],
                                            timeout=60)
                    if p2[0] != 0:
                        raise RuntimeError(f"OCR 失败: {(p2[2] or '').strip()[:200]}")
                    return p2[1]
                except Exception as e:
                    last = e
                    if attempt == 1:
                        try:
                            if self.log:
                                self.log("OCR 偶发失败，1 秒后自动重试一次…")
                        except Exception:
                            pass
                        time.sleep(1.0)
            raise RuntimeError(f"OCR 失败（已自动重试一次仍失败）: {last}")
        raise RuntimeError(f"暂不支持的 OCR 引擎: {self.engine}")


def match_screen(text_norm: str, rules: list, ctx=None):
    """判定屏位。ctx = {"force": 屏位|None, "flow": {...}, "flow_index": int, "counts": {...}}

    判定顺序（第四屏是「前三屏都不命中」的兜底，绝不抢先匹配）：
      1) force（面板上「下一张强制为」指定）最优先；
      2) 非顺序屏位规则（第一屏：营业中 / 暂停营业）；
      3) 第二屏：本行第二屏还没有图，且命中第二屏规则（设备与服务 + 收起）-> 第二屏
         （第二屏永远只放一张，已有一张后就不再判它）；
      4) 顺序判定：含 flow.trigger（设备与服务）的页，按本行第几张分屏，
         第 1 张 -> fscreens[0]（第二屏，调用方会同时补一份到第三屏），
         之后每张 -> fscreens[-1]（第三屏）；
      5) 顺序屏位关键词兜底（第二屏 / 第三屏 规则本身，trigger 没识别出来时用）；
      6) 兜底屏位（rules 里 fallback: true 的那条，通常是第四屏）：
         前面都没命中 -> 第四屏。若它配了 keywords 则需命中其一；
         OCR 一个字都没认出来的图仍按未识别处理，不上传。
    """
    ctx = ctx or {}
    if ctx.get("force"):
        return ctx["force"]
    flow = ctx.get("flow") or {}
    fscreens = flow.get("screens") or []
    counts = ctx.get("counts") or {}
    first_screen = fscreens[0] if fscreens else None
    last_screen = fscreens[-1] if fscreens else None

    # 兜底屏位单独拿出来，不参与抢匹配
    fb = None
    active = []
    for r in rules or []:
        if not r.get("enabled", True):
            continue
        if r.get("fallback"):
            if fb is None:
                fb = r
            continue
        active.append(r)

    # 2) 非顺序屏位（第一屏）
    for r in active:
        if r.get("screen") in fscreens:
            continue
        s = _hit_rule(r, text_norm)
        if s:
            return s

    # 3) 明确的第二屏（设备与服务 + 收起），且第二屏还没图
    if first_screen:
        for r in active:
            if r.get("screen") != first_screen:
                continue
            if _hit_rule(r, text_norm) and int(counts.get(first_screen, 0) or 0) == 0:
                return first_screen

    # 4) 顺序判定：本行第几张「设备与服务」页
    if flow.get("enabled") and fscreens:
        tri = [normalize(t) for t in (flow.get("trigger") or [])]
        if any(t and t in text_norm for t in tri):
            idx = int(ctx.get("flow_index") or 0)
            return fscreens[min(idx, len(fscreens) - 1)]

    # 5) 顺序屏位关键词兜底
    for r in active:
        if r.get("screen") not in fscreens:
            continue
        s = _hit_rule(r, text_norm)
        if not s:
            continue
        # 第二屏只放一张：已经有一张了，后续的页都归第三屏
        if s == first_screen and int(counts.get(first_screen, 0) or 0) > 0:
            return last_screen or s
        return s

    # 6) 兜底屏位（第四屏）
    if fb is not None:
        if not text_norm.strip():
            return None                      # OCR 什么都没认出来 -> 按未识别处理
        kws = [k for k in (fb.get("keywords") or []) if k]
        if kws:                              # 配了关键词就要求命中其一，没配就是无条件兜底
            for r in (fb,):
                if _hit_rule(r, text_norm):
                    return r["screen"]
            return None
        return fb["screen"]
    return None


def _hit_rule(r, text_norm):
    if not r.get("enabled", True):
        return None
    ex = [normalize(x) for x in (r.get("exclude") or [])]
    if any(e and e in text_norm for e in ex):
        return None
    mode = r.get("mode", "any")
    hits = []
    for g in r.get("keywords", []):
        cands = g if isinstance(g, list) else [g]
        hits.append(any(normalize(c) in text_norm for c in cands))
    ok = all(hits) if mode == "all" else any(hits)
    return r["screen"] if ok else None
