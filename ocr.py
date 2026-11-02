"""本地 OCR：默认用 macOS 内置 Vision（离线、中文准确），可扩展其他引擎。"""
import os
import re
import select
import subprocess
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
        # ---- 常驻模式：模型只加载一次（约 15s），之后每张约 0.1~0.3s ----
        # worker: false 可整体关掉（回到每张图新起进程的旧方式）
        self.worker_bin = cfg.get("worker_bin", "bin/visionocr_worker")
        if not os.path.isabs(self.worker_bin):
            self.worker_bin = os.path.join(root, self.worker_bin)
        self.use_worker = (bool(cfg.get("worker", True))
                           and self.engine == "vision"
                           and os.path.exists(self.worker_bin))
        self._wp = None            # 常驻子进程
        # ⚠ 常驻进程里必须用 ANE（cpu_only=False）。
        # 2026-11-01 实测：usesCPUOnly=true 在常驻进程里 60/60 全部返回空文本（每张 0.01s，
        # 根本没干活），这才是「运行一会儿就未识别」的真正根因；
        # 改成 ANE 后同一批图 59/60 正常，每张 0.18~0.25s。
        self.cpu_only = bool(cfg.get("cpu_only", False))
        # 一个常驻进程处理这么多张后主动换新进程（防止长时间运行后状态累积）；0 = 不轮换
        self.worker_max_jobs = int(cfg.get("worker_max_jobs", 400) or 0)
        self._jobs = 0
        # worker 的 stderr 写到日志文件，方便排查「识别为空」类问题（别再用 DEVNULL 静默吞掉）
        self._err_path = os.path.join(root, "logs", "ocr_worker.err")

    def _worker_stderr(self):
        try:
            os.makedirs(os.path.dirname(self._err_path), exist_ok=True)
            return open(self._err_path, "ab", buffering=0)
        except Exception:
            return subprocess.DEVNULL

    # ---------- 常驻 worker ----------
    def _get_worker(self):
        """启动/复用常驻 OCR 进程。调用方需持有 _OCR_LOCK。"""
        if self._wp is not None and self._wp.poll() is None:
            # 定期换新进程：长时间运行后 Vision 内部状态可能累积出错，换进程比等它坏掉划算
            if self.worker_max_jobs and self._jobs >= self.worker_max_jobs:
                if self.log:
                    self.log(f"OCR 常驻进程已处理 {self._jobs} 张，主动换新进程")
                self._kill_worker()
            else:
                return self._wp
        self._jobs = 0
        self._wp = subprocess.Popen(
            [self.worker_bin, "--worker", self.langs, self.correction,
             "1" if self.cpu_only else "0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._worker_stderr(), bufsize=0)
        return self._wp

    def _kill_worker(self):
        p, self._wp = self._wp, None
        if p is None:
            return
        for f in (p.stdin, p.stdout):
            try:
                f.close()
            except Exception:
                pass
        try:
            p.kill()
        except Exception:
            pass

    def _worker_text(self, path, timeout=60):
        """发一张图给常驻进程，读回识别文本（以 \\x01\\n 结尾）。"""
        p = self._get_worker()
        p.stdin.write((path + "\n").encode("utf-8"))
        p.stdin.flush()
        self._jobs += 1
        fd = p.stdout.fileno()
        deadline = time.time() + timeout
        buf = b""
        while not buf.endswith(b"\x01\n"):
            remain = deadline - time.time()
            if remain <= 0:
                raise RuntimeError("OCR 常驻进程响应超时")
            r, _, _ = select.select([fd], [], [], remain)
            if not r:
                raise RuntimeError("OCR 常驻进程响应超时")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("OCR 常驻进程已退出")
            buf += chunk
        return buf[:-2].decode("utf-8", "ignore")

    def prewarm(self):
        """启动后台线程预热常驻进程（把 15s 的一次性模型加载挪到空闲时做）。

        失败静默：正式请求会自动重试 worker，再不行回退单次模式。
        """
        if not self.use_worker:
            return

        def _w():
            warm = os.path.join(self.root, "tmp", "_warm.png")
            try:
                if not os.path.exists(warm):
                    os.makedirs(os.path.dirname(warm), exist_ok=True)
                    from PIL import Image
                    Image.new("RGB", (8, 8), "white").save(warm)
                with _OCR_LOCK:
                    self._worker_text(warm, timeout=90)
                if self.log:
                    self.log("OCR 常驻进程已预热（模型加载完成，后续每张约 0.1~0.3s）")
            except Exception:
                self._kill_worker()

        threading.Thread(target=_w, daemon=True, name="ocr-prewarm").start()

    def close(self):
        """停掉常驻进程（监听结束时调用）。"""
        self._kill_worker()

    def text(self, path: str) -> str:
        if self.engine == "vision":
            if not os.path.exists(self.bin):
                raise RuntimeError(f"找不到 OCR 程序: {self.bin}")
            # 优先走常驻进程；进程异常/超时/返回空 都自动回退到单次模式
            if self.use_worker:
                for attempt in (1, 2):
                    try:
                        with _OCR_LOCK:
                            t = self._worker_text(path)
                        if t.strip():
                            return t
                    except Exception as e:
                        if self.log:
                            self.log(f"OCR 常驻进程异常（{e}）")
                    # 返回空 / 抛异常 = 这个 worker 已经处于坏状态：
                    # 杀掉它，下一轮启一个全新进程重试（新进程实测基本都能出文本）
                    with _OCR_LOCK:
                        self._kill_worker()
                    if attempt == 1 and self.log:
                        self.log("OCR 常驻进程没出文本，换新进程重试一次")
                if self.log:
                    self.log("OCR 常驻进程两次都没出文本，本张回退单次模式")
            last = None
            for attempt in (1, 2):
                try:
                    with _OCR_LOCK:
                        p2 = spawn_capture([self.bin, path, self.langs, self.correction],
                                            timeout=60)
                    if p2[0] != 0:
                        raise RuntimeError(f"OCR 失败: {(p2[2] or '').strip()[:200]}")
                    if p2[1].strip():
                        return p2[1]
                    # 单次模式也返回空：图片可能真的没文字，重试一次确认
                    if attempt == 1:
                        if self.log:
                            self.log("OCR 识别为空，1 秒后重试一次…")
                        time.sleep(1.0)
                        continue
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
        ctx["decision"] = ("手动指定", f"面板「下一张强制为」= {ctx['force']}")
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
            ctx["decision"] = ("关键词规则", _why(r, text_norm))
            return s

    # 3) 明确的第二屏（设备与服务 + 收起），且第二屏还没图
    if first_screen:
        for r in active:
            if r.get("screen") != first_screen:
                continue
            if _hit_rule(r, text_norm) and int(counts.get(first_screen, 0) or 0) == 0:
                ctx["decision"] = ("关键词规则", _why(r, text_norm))
                return first_screen

    # 4) 顺序判定：本行第几张「设备与服务」页
    if flow.get("enabled") and fscreens:
        tri = [normalize(t) for t in (flow.get("trigger") or [])]
        hit_t = [t for t in tri if t and t in text_norm]
        if hit_t:
            idx = int(ctx.get("flow_index") or 0)
            sc = fscreens[min(idx, len(fscreens) - 1)]
            ctx["decision"] = ("顺序判定",
                               f"命中序列触发词{'/'.join(hit_t)}，本行第 {idx + 1} 张序列页 -> {sc}")
            return sc

    # 5) 顺序屏位关键词兜底
    for r in active:
        if r.get("screen") not in fscreens:
            continue
        s = _hit_rule(r, text_norm)
        if not s:
            continue
        # 第二屏只放一张：已经有一张了，后续的页都归第三屏
        if s == first_screen and int(counts.get(first_screen, 0) or 0) > 0:
            ctx["decision"] = ("关键词规则(第二屏已占)",
                               f"{_why(r, text_norm)}；第二屏已有图 -> 归 {last_screen or s}")
            return last_screen or s
        ctx["decision"] = ("关键词规则", _why(r, text_norm))
        return s

    # 本行是否已经开始传图（第一屏这类非顺序屏位已有内容）—— 用来判断
    # 「本行第 1 张序列页」的兜底守卫该不该介入（还没收到店名/首屏时不介入）
    non_flow = [r.get("screen") for r in active if r.get("screen") not in fscreens]
    row_started = any(int(counts.get(s, 0) or 0) > 0 for s in non_flow)

    # 5.5) 兜底守卫（fallback_guard）
    #   本行已经在传「设备与服务」序列页时，如果某张图 OCR 没认出「设备与服务」
    #   （截图只截到一半 / 识别抖动），前面 2~5 步全部落空，就会直接掉进第 6 步的
    #   「无条件兜底」被判成第四屏 —— 这就是「第三屏的图被塞进第四屏」的根因。
    #   守卫：先用 fourth_markers 判断它到底像不像第四屏，不像（且像序列页）
    #   -> 接着走序列，而不是跳到兜底屏。
    #
    #   ⚠ 2026-11-02：守卫原来只覆盖「第 2 张序列页起」（idx > 0）。
    #   但用户实际丢图/串屏最多的恰恰是**本行第 1 张设备页**（idx == 0）：
    #   它一旦 OCR 漏掉「设备与服务」，就会被无条件兜底成第四屏，
    #   结果 = 「第四屏多一张其实该进第三屏的图」+「第二屏拿到的不是截的第一张」。
    #   现在 idx == 0 也纳入守卫（前提是这行已经有第一屏，说明序列确实开始了）。
    if fb is not None and flow.get("enabled") and fscreens and last_screen:
        idx = int(ctx.get("flow_index") or 0)
        if flow.get("fallback_guard", True):
            markers = [normalize(m) for m in (flow.get("fourth_markers") or [])]
            hints = [normalize(h) for h in (flow.get("sequence_hints") or [])]
            looks_4th = any(m and m in text_norm for m in markers)
            looks_seq = (not hints) or any(h and h in text_norm for h in hints)
            if not looks_4th and looks_seq:
                if idx > 0:
                    ctx["decision"] = ("序列续接(兜底守卫)",
                                       f"本行第 {idx + 1} 张序列页，OCR 未识别到「设备与服务」且无第四屏特征，"
                                       f"按序列续接判 {last_screen}")
                    return last_screen
                # 本行第 1 张序列页：判 fscreens[0]（第二屏），不再掉第四屏兜底
                if flow.get("guard_first_page", True) and row_started:
                    ctx["decision"] = ("序列续接(兜底守卫)",
                                       f"本行第 1 张序列页，OCR 未识别到「设备与服务」且无第四屏特征，"
                                       f"按序列判 {first_screen}")
                    return first_screen

    # 6) 兜底屏位（第四屏）
    if fb is not None:
        if not text_norm.strip():
            return None                      # OCR 什么都没认出来 -> 按未识别处理
        kws = [k for k in (fb.get("keywords") or []) if k]
        if kws:                              # 配了关键词就要求命中其一，没配就是无条件兜底
            for r in (fb,):
                if _hit_rule(r, text_norm):
                    ctx["decision"] = ("兜底规则命中关键词", f"命中 {fb['screen']} 兜底规则关键词")
                    return r["screen"]
            return None
        ctx["decision"] = ("无条件兜底", f"前 5 步都没命中 -> {fb['screen']}")
        return fb["screen"]
    return None


def _why(r, text_norm):
    """把某条规则的命中情况写成一句人话，用于日志追溯。"""
    parts = []
    for g in (r.get("keywords") or []):
        cands = g if isinstance(g, list) else [g]
        hit = [c for c in cands if normalize(c) in text_norm]
        parts.append(("命中「%s」" % "/".join(hit)) if hit
                     else ("缺「%s」" % "/".join(cands)))
    ex = [x for x in (r.get("exclude") or []) if normalize(x) in text_norm]
    if ex:
        parts.append("排除词「%s」出现" % "/".join(ex))
    return f"{r.get('screen')} 规则(mode={r.get('mode', 'any')}): " + ("，".join(parts) or "无关键词")


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
