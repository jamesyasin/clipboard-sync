#!/usr/bin/env python3
"""剪贴板 -> 飞书多维表格 自动采集。

用法：
  python3 sync.py run          # 独立进程常驻监听（launchd / nohup 用）
  python3 sync.py run --dry    # 试运行：只识别不上传
  python3 sync.py once         # 立刻读取一次剪贴板并处理
  python3 sync.py ocr <图片>    # 测试某张图的 OCR + 屏位判定（验证规则用）
  python3 sync.py status       # 查看当前状态 / 连通性
  python3 sync.py undo         # 撤回上一步

面板（panel.py）会直接 import 本文件的 Monitor，在主线程里 tick()、后台线程池里上传，
因此「关闭窗口 = 进程退出 = 监听彻底停止」，不会留下任何后台常驻进程。
"""
import hashlib
import json
import os
import queue as qmod
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import yaml

import clipboard
import feishu as fsmod
from ocr import OCR, match_screen, normalize

ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION = "v4-panel"
ASYNC = "ASYNC"                   # process() 返回值：任务已交给上传线程池，由它自己出队
CFG_PATH = os.path.join(ROOT, "config.yaml")
STATE_PATH = os.path.join(ROOT, "state.json")
LOG_DIR = os.path.join(ROOT, "logs")
TMP_DIR = os.path.join(ROOT, "tmp")
UNMATCH_DIR = os.path.join(ROOT, "unmatched")
PENDING_DIR = os.path.join(ROOT, "pending")
OPS_LOG = os.path.join(LOG_DIR, "ops.jsonl")   # 结构化操作日志（面板日志区读这个）
QUEUE_PATH = os.path.join(ROOT, "queue.json")  # 待处理队列（落盘：崩溃/退出后不丢图）

for d in (LOG_DIR, TMP_DIR, UNMATCH_DIR, PENDING_DIR, os.path.join(ROOT, ".cache")):
    os.makedirs(d, exist_ok=True)


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "sync.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def ops_log(**kw):
    """写一条结构化操作日志：类型 / 来源 / 动作 / 目标 / 结果 / 耗时。"""
    kw.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
    try:
        with open(OPS_LOG, "a") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return kw


def looks_like_credential(s: str) -> bool:
    """判断这段文本是不是飞书/API 凭据（不该被当成店名写进表）。"""
    s = (s or "").strip()
    if not s or len(s) > 120:
        return False
    if s.startswith("cli_") or s.startswith("app_"):
        return True
    # 纯 24-40 位大小写字母数字、无空格无中文 -> 典型 secret / token
    import re as _re
    if _re.fullmatch(r"[A-Za-z0-9_-]{24,40}", s) and not _re.search(r"[\u4e00-\u9fff]", s):
        return True
    if s.startswith("t-") or s.startswith("u-") and len(s) > 20:
        return True
    return False


def load_cfg():
    with open(CFG_PATH) as f:
        return yaml.safe_load(f)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            s = json.load(open(STATE_PATH))
            if isinstance(s, dict):
                for k, dv in (("counts", {}), ("recent", []), ("pending", []),
                              ("ops", []), ("current_fields", {})):
                    s.setdefault(k, dv)
                s.setdefault("stats", {"text": 0, "images": 0, "uploaded": 0, "unmatched": 0})
                s.setdefault("ops_seq", 1)
                return s
        except Exception:
            pass
    return {"current_record_id": None, "current_name": None, "current_fields": {},
            "counts": {}, "recent": [], "pending": [], "ops": [], "ops_seq": 1,
            "stats": {"text": 0, "images": 0, "uploaded": 0, "unmatched": 0}}


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    json.dump(s, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


class Monitor:
    """剪贴板监听器。tick() 轻量、必须在主线程调用（NSPasteboard 要求）；
    真正的 OCR + 上传丢到后台线程池，界面与轮询都不会被卡住。"""

    def __init__(self, cfg, dry=False):
        self.cfg = cfg
        self.dry = dry
        self.fs = fsmod.Feishu(cfg, log, ROOT)
        self.ocr = OCR(cfg.get("ocr", {}), ROOT, log)
        self.rules = cfg.get("rules", [])
        self.fmap = cfg["feishu"]["fields"]
        self.state = load_state()
        self.ocache = {}                       # 图片指纹 -> [屏位, OCR文本]，避免重复 OCR
        self.ocache_path = os.path.join(ROOT, ".cache", "ocr_cache.json")
        try:
            self.ocache = json.load(open(self.ocache_path))
        except Exception:
            self.ocache = {}
        self.lock = threading.Lock()
        # ---- 队列：严格按复制顺序处理（唯一 dispatcher 线程），上传仍并发 ----
        # 以前是「每张图直接丢进线程池」，3 个 worker 同时 OCR + 判定，
        # 判定的先后顺序可能和截图顺序不一致 -> 串屏；且并发调 AppKit/子进程易踩崩。
        # 现在：捕获 -> 入队(先落盘) -> 单个 dispatcher 顺序做 OCR + 判定 -> 上传交给线程池。
        self._dq = qmod.Queue()
        self._qid = {}                 # qid -> item（用于完成后出队）
        self._disp = threading.Thread(target=self._dispatch_loop, name="dispatch", daemon=True)
        self._disp.start()
        self._recover_queue()
        perf = cfg.get("perf", {}) or {}
        self.workers = max(1, int(perf.get("upload_workers", 3)))
        self.pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="up")
        self.field_locks = {}
        self._last_cc = clipboard.change_count()
        self.running = False
        log(f"版本 {VERSION} | 模式: {self.fs.mode} | 表 {cfg['feishu']['table_id']} | "
            f"轮询 {cfg.get('poll_interval_ms', 120)}ms | 并发 {self.workers} | dry={dry}")
        if self.fs.mode == "cli":
            log("提示：CLI 模式每张图要启动一次 node，较慢；填 App Secret 走官方 API 可提速 2~3 倍")

    # ---------- 轮询（主线程） ----------
    def tick(self):
        """检测一次剪贴板；有新内容就交给后台处理。调用频率 = poll_interval_ms。"""
        try:
            cc = clipboard.change_count()
            if cc == self._last_cc:
                return
            self._last_cc = cc
            item = clipboard.read(TMP_DIR, log)
            if item["kind"] == "none":
                log(f"剪贴板变化 #{cc}：无可识别内容（格式不支持）")
                return
            fp = clipboard.fingerprint(item)
            with self.lock:
                st = self.state = load_state()
                if self.cfg.get("dedupe", True) and fp in st["recent"]:
                    log(f"剪贴板变化 #{cc}：与最近一次重复，已去重跳过")
                    return
                st["recent"].append(fp)
                st["recent"] = st["recent"][-self.cfg.get("dedupe_window", 20):]
                save_state(st)
            self._enqueue(item)
            log("捕获 " + ("文本" if item["kind"] == "text" else "图片") +
                f"（#{cc}），后台处理中…")
        except Exception as e:
            log(f"ERROR tick: {type(e).__name__}: {e}")

    # ---------- 队列（落盘，崩溃也不丢图） ----------
    def _load_q(self):
        try:
            with open(QUEUE_PATH) as f:
                return json.load(f)
        except Exception:
            return []

    def _save_q(self, q):
        try:
            tmp = QUEUE_PATH + ".tmp"
            json.dump(q, open(tmp, "w"), ensure_ascii=False, indent=1)
            os.replace(tmp, QUEUE_PATH)
        except Exception as e:
            log(f"队列落盘失败: {e}")

    def _enqueue(self, item):
        """入队：先落盘再进内存队列，保证崩溃/断电后重新打开面板还能接着传。"""
        qid = uuid.uuid4().hex[:8]
        rec = {"qid": qid, "kind": item.get("kind"), "ts": time.time(),
               "path": item.get("path"), "text": item.get("text"),
               "temp": item.get("temp", True)}
        q = self._load_q()
        q.append(rec)
        self._save_q(q)
        item = dict(item)
        item["_qid"] = qid
        self._qid[qid] = item
        self._dq.put(item)
        return qid

    def _dequeue(self, qid):
        """处理完成（或确认放弃）后出队。"""
        if not qid:
            return
        self._qid.pop(qid, None)
        q = [r for r in self._load_q() if r.get("qid") != qid]
        self._save_q(q)

    def _recover_queue(self):
        """启动时把上次没处理完的图重新放回队列（避免崩溃丢图）。"""
        q = self._load_q()
        if not q:
            return
        try:
            st = load_state()
            done = {o.get("fp") for o in (st.get("ops") or []) if o.get("fp")}
        except Exception:
            done = set()
        back = 0
        for r in q:
            if r.get("kind") == "image":
                if not r.get("path") or not os.path.exists(r["path"]):
                    continue                      # 临时文件已被清掉，没法再传
                try:
                    fp = clipboard.fingerprint({"kind": "image", "path": r["path"]})
                except Exception:
                    fp = None
                if fp and fp in done:             # 其实已经传过了，别重复上传
                    continue
            item = {k: r.get(k) for k in ("kind", "path", "text", "temp") if r.get(k) is not None}
            if item.get("kind") == "image":
                item["temp"] = r.get("temp", True)
            qid = r.get("qid") or uuid.uuid4().hex[:8]
            item["_qid"] = qid
            self._qid[qid] = item
            self._dq.put(item)
            back += 1
        self._save_q([r for r in q if r.get("path") and os.path.exists(r["path"])])
        if back:
            log(f"恢复上次未完成的 {back} 条任务，按顺序继续处理")

    def _dispatch_loop(self):
        """唯一的顺序执行线程：OCR + 屏位判定都在这里按复制顺序做，杜绝串屏。"""
        while True:
            item = self._dq.get()
            try:
                ok = self.process(item)
                if ok is not ASYNC:      # ASYNC = 上传线程完成后自己出队
                    self._dequeue(item.get("_qid"))
            except Exception as e:
                log(f"ERROR dispatch: {type(e).__name__}: {e}")
                self._dequeue(item.get("_qid"))
            finally:
                try:
                    self._dq.task_done()
                except Exception:
                    pass

    def loop(self):
        """独立进程模式：主线程循环 tick。"""
        interval = max(0.02, self.cfg.get("poll_interval_ms", 120) / 1000.0)
        self.running = True
        log(f"开始监听剪贴板（changeCount={self._last_cc}）…")
        while self.running:
            try:
                self.tick()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"ERROR {type(e).__name__}: {e}")
            time.sleep(interval)

    def shutdown(self):
        self.running = False
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def flock(self, key):
        with self.lock:
            if key not in self.field_locks:
                self.field_locks[key] = threading.Lock()
            return self.field_locks[key]

    # ---------- 分发 ----------
    def process(self, item):
        """返回 True=处理完(可出队) / False=失败 / ASYNC=已交给上传线程，由它出队。"""
        try:
            if item["kind"] == "text":
                self.handle_text(item["text"])
                return True
            if item["kind"] == "image":
                return self.handle_image(item["path"], item.get("temp", True),
                                         item.get("_qid"))
        except Exception as e:
            log(f"ERROR process: {type(e).__name__}: {e}")
            ops_log(type="文本" if item["kind"] == "text" else "图片", src="剪贴板",
                    action="处理", target="-", result="失败", reason=f"{type(e).__name__}: {e}", ms=0)
        return False

    # ---------- 文本 ----------
    def handle_text(self, text):
        t0 = time.time()
        tcfg = self.cfg.get("text", {}) or {}
        # 安全阀：复制 App ID / Secret / token 这类凭据时，绝不能当成店名写进表里
        if looks_like_credential(text):
            reason = "疑似飞书凭据（App ID/Secret/Token），已忽略"
            log(f"跳过: {reason}: {text[:24]}…")
            ops_log(type="文本", src="剪贴板", detail=text[:24], action="跳过",
                    target="-", result="跳过", reason=reason,
                    ms=int((time.time() - t0) * 1000))
            return True
        with self.lock:
            st = self.state = load_state()
            st["stats"]["text"] += 1
            save_state(st)
        if len(text) > tcfg.get("max_len", 60):
            reason = f"文本过长({len(text)}> {tcfg.get('max_len', 60)})，视为误复制"
            log(f"跳过: {reason}: {text[:30]}…")
            ops_log(type="文本", src="剪贴板", detail=text[:40], action="跳过",
                    target="-", result="跳过", reason=reason, ms=int((time.time() - t0) * 1000))
            return True
        if not tcfg.get("new_row", True):
            with self.lock:
                st = self.state = load_state()
                rid = st.get("current_record_id")
                if rid:
                    f = {self.fmap[tcfg.get("field", "终端名称")]: text}
                    if not self.dry:
                        self.fs.update_record(rid, f)
                    log(f"文本 -> 当前行「{text}」")
                    ops_log(type="文本", src="剪贴板", detail=text, action="写入当前行字段",
                            target=self._target(rid, tcfg.get("field", "终端名称")),
                            result="成功", reason="", ms=int((time.time() - t0) * 1000))
                save_state(st)
            return

        fields = {self.fmap.get(k, k): v
                  for k, v in (self.cfg["feishu"].get("defaults", {}) or {}).items()}
        fields[self.fmap[tcfg.get("field", "终端名称")]] = text

        mode = tcfg.get("row_selection", "auto")     # auto | new | blank
        rid, used = None, "new"
        if mode in ("auto", "blank"):
            rid = self._find_blank(tcfg)
            if rid:
                used = "blank"
        ms = int((time.time() - t0) * 1000)
        if rid:
            if not self.dry:
                self.fs.update_record(rid, fields)
            log(f"文本 -> 写入已有空行「{text}」 record={rid}")
            action = "写入空白行"
        else:
            if mode == "blank":
                reason = "没找到空白行，且 row_selection=blank（不新建）"
                log(reason)
                ops_log(type="文本", src="剪贴板", detail=text, action="未写入",
                        target="-", result="失败", reason=reason, ms=ms)
                return False
            rid = "DRY" if self.dry else self.fs.create_record(fields)
            log(f"文本 -> 新建行「{text}」 record={rid}")
            action = "新建行"

        with self.lock:
            st = self.state = load_state()
            st["current_record_id"] = rid
            st["current_name"] = text
            st["current_fields"] = fields
            st["counts"] = {}
            st.setdefault("ops", []).append(
                {"kind": "row", "id": st.get("ops_seq", 1), "record_id": rid,
                 "name": text, "mode": used, "fields": list(fields.keys()),
                 "fp": "t:" + hashlib.sha1(text.encode()).hexdigest()})
            st["ops_seq"] = st.get("ops_seq", 1) + 1
            st["ops"] = st["ops"][-100:]
            save_state(st)
        ops_log(type="文本", src="剪贴板", detail=text, action=action,
                target=self._target(rid, "终端名称"), result="成功", reason="",
                ms=int((time.time() - t0) * 1000))
        self.flush_pending()

    def _find_blank(self, tcfg):
        """找一条真正没有数据的行（名称 + 各屏位都为空）。"""
        labels = tcfg.get("blank_check_fields") or \
            ["终端名称", "第一屏", "第二屏", "第三屏", "第四屏"]
        try:
            rid, _f = self.fs.find_blank_row(labels, limit=int(tcfg.get("blank_scan_limit", 50)))
            return rid
        except Exception as e:
            log(f"查找空白行失败: {e}")
            return None

    def _target(self, record_id, label):
        fid = self.fmap.get(label, label)
        return f"{self.cfg['feishu']['table_id']} / {record_id} / {label}({fid})"

    # ---------- 图片 ----------
    def handle_image(self, path, temp=True, qid=None):
        t0 = time.time()
        with self.lock:
            st = self.state = load_state()
            st["stats"]["images"] += 1
            save_state(st)
        fp, hit = None, None
        try:
            fp = clipboard.fingerprint({"kind": "image", "path": path})
            hit = self.ocache.get(fp)
        except Exception:
            pass
        if hit is not None:
            text = hit              # 只缓存 OCR 文本：屏位要按当前顺序状态重新判定
            ocr_ms = 0
        else:
            to = time.time()
            try:
                text = self.ocr.text(path)
            except Exception as e:
                ops_log(type="图片", src=os.path.basename(path), action="OCR",
                        target="-", result="失败", reason=f"OCR 失败: {e}",
                        ms=int((time.time() - t0) * 1000))
                return
            ocr_ms = int((time.time() - to) * 1000)
            if fp:
                self.ocache[fp] = text[:1200]
                if len(self.ocache) > 500:
                    self.ocache = {}
                try:
                    json.dump(self.ocache, open(self.ocache_path, "w"))
                except Exception:
                    pass
        norm = normalize(text)

        # ---- 判定屏位（锁内：顺序判定依赖本行已传数量，避免并发串位）----
        with self.lock:
            st = self.state = load_state()
            force = st.get("force_screen")
            flow = self.cfg.get("flow", {}) or {}
            fscreens = flow.get("screens") or []
            flow_idx = sum(st["counts"].get(s, 0) for s in fscreens)
            screen = match_screen(norm, self.rules,
                                  {"force": force, "flow": flow, "flow_index": flow_idx,
                                   "counts": st.get("counts", {})})
            if force:
                st["force_screen"] = None          # 手动覆盖是一次性的
            # 一张图可能要落到多个屏位：本行第一张「设备与服务」页 ->
            # 同时写第二屏和第三屏（座区少时这两屏长得一样，无法区分，干脆都存）
            screens = [screen] if screen else []
            if (screen in fscreens and flow.get("enabled") and
                    flow.get("mode", "duplicate_first") == "duplicate_first" and flow_idx == 0):
                # 第 2 张图：截图里「设备与服务」区域带着「收起」-> 它就是第二屏，不再给第三屏；
                # 没有「收起」（座区少、两屏长一样）-> 第二屏 + 第三屏 同步都写一份。
                skip = [normalize(w) for w in (flow.get("skip_duplicate_if") or [])]
                if skip and any(w and w in norm for w in skip):
                    screens = [fscreens[0]]
                else:
                    screens = list(fscreens)
            for s in screens:                       # 先占位
                st["counts"][s] = st["counts"].get(s, 0) + 1
            save_state(st)
        ts = datetime.now().strftime("%m%d_%H%M%S")
        src = f"剪贴板图片 {os.path.basename(path)}"

        if not screen:
            with self.lock:
                st = self.state = load_state()
                st["stats"]["unmatched"] += 1
                save_state(st)
            if self.cfg.get("fallback", {}).get("save_unmatched", True):
                dst = os.path.join(UNMATCH_DIR, f"unmatched_{ts}.png")
                try:
                    shutil.copy(path, dst)
                except Exception:
                    dst = "-"
                with open(os.path.join(LOG_DIR, "unmatched.log"), "a") as f:
                    f.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} {dst}\n{text[:400]}\n")
            log(f"未识别（已存 unmatched）OCR前40字: {norm[:40]}")
            ops_log(type="图片", src=src, ocr=norm[:60], action="判定屏位",
                    target="-", result="未识别",
                    reason="所有规则都不命中，已存 unmatched/，未写入表格",
                    ms=int((time.time() - t0) * 1000), ocr_ms=ocr_ms)
            return True

        with self.lock:
            st = self.state = load_state()
            rid = st.get("current_record_id")
            if not rid:
                if self.cfg.get("fallback", {}).get("require_text_first"):
                    dst = os.path.join(PENDING_DIR, f"{screen}_{ts}.png")
                    shutil.copy(path, dst)
                    st["pending"].append({"path": dst, "screen": screen})
                    st["counts"][screen] = max(0, st["counts"].get(screen, 1) - 1)  # 退回占位
                    log(f"无当前行 -> 挂起 {screen}: {dst}")
                    save_state(st)
                    ops_log(type="图片", src=src, ocr=norm[:60], action="挂起等待店名",
                            target="-", result="排队",
                            reason="还没收到店名文本，图片先放 pending/，收到文本后自动回灌",
                            ms=int((time.time() - t0) * 1000), ocr_ms=ocr_ms)
                    return True
                fields = {self.fmap.get(k, k): v
                          for k, v in (self.cfg["feishu"].get("defaults", {}) or {}).items()}
                fields[self.fmap["终端名称"]] = self.cfg.get("fallback", {}).get("unknown_name", "")
                rid = "DRY" if self.dry else self.fs.create_record(fields)
                st["current_record_id"] = rid
                st["current_fields"] = fields
                st["counts"] = {}
                st.setdefault("ops", []).append(
                    {"kind": "row", "id": st.get("ops_seq", 1), "record_id": rid,
                     "name": "", "mode": "new", "fields": list(fields.keys())})
                st["ops_seq"] = st.get("ops_seq", 1) + 1
                st["ops"] = st["ops"][-100:]
                log(f"无文本 -> 自动建行 record={rid}")
            save_state(st)

        with self.lock:
            st = self.state = load_state()
            maxn = self.cfg.get("limits", {}).get("per_screen_max", {}).get(screen, 50)
            overflow = self.cfg.get("limits", {}).get("overflow", "skip")
            if st["counts"].get(screen, 0) > maxn:      # counts 已含本张（占位）
                if overflow != "new_row":
                    # 默认跳过：不偷偷新建行（以前会新建一行并把锁定行换走，很容易把图传散）
                    for s in screens:                 # 退回占位
                        st["counts"][s] = max(0, st["counts"].get(s, 1) - 1)
                    save_state(st)
                    reason = (f"{screen} 已有 {maxn} 张（上限）。想重传这张："
                              f"先点「撤回上一步」删掉旧图，再重新复制这张图")
                    log(f"跳过: {reason}")
                    ops_log(type="图片", src=src, ocr=norm[:60], screen=screen, action="跳过",
                            target=self._target(st.get("current_record_id"), screen),
                            result="跳过", reason=reason,
                            ms=int((time.time() - t0) * 1000), ocr_ms=ocr_ms)
                    return True
                self.new_row()
                st = self.state
                for s in screens:
                    st["counts"][s] = st["counts"].get(s, 0) + 1
            rid = st["current_record_id"]
            name = f"{st.get('current_name') or ''}_{screen}_{ts}.png".lstrip("_")
            seq = st.get("ops_seq", 1)
            st["ops_seq"] = seq + len(screens)
            save_state(st)

        ms0 = int((time.time() - t0) * 1000)
        job = dict(rid=rid, screens=screens, path=path, name=name, seq=seq, fp=fp,
                   src=src, norm=norm, ocr_ms=ocr_ms, t0=t0, temp=temp, qid=qid,
                   maxn=maxn, overflow=overflow)
        if self.dry:
            log(f"[dry] {'+'.join(screens)} -> {rid}  ({os.path.basename(path)})")
            for s in screens:
                ops_log(type="图片", src=src, ocr=norm[:60], screen=s, action="上传附件(试运行)",
                        target=self._target(rid, s), result="成功", reason="dry 模式未真传",
                        ms=ms0, ocr_ms=ocr_ms)
            return True
        # 真正的上传丢回线程池（多屏位可并行），但「判定顺序」已在 dispatcher 里定好，
        # 且同一单元格有锁串行，所以既快又不会串屏
        self.pool.submit(self._upload_job, job)
        return ASYNC

    def _upload_job(self, job):
        """上传一个任务（可能写多个屏位）。完成后才把队列里的记录删掉。"""
        rid, screens, path, name = job["rid"], job["screens"], job["path"], job["name"]
        seq, fp, src, norm = job["seq"], job["fp"], job["src"], job["norm"]
        ocr_ms, t0, temp = job["ocr_ms"], job["t0"], job["temp"]
        results = []
        for i, s in enumerate(screens):
            field = self.fmap.get(s, s)
            # 同单元格串行（读-改-写），不同单元格/不同行可并行
            with self.flock(f"{rid}:{field}"):
                tu = time.time()
                try:
                    tok = self.fs.append_attachment(rid, field, path, name)
                    ok, why = True, ""
                except Exception as e:
                    tok, ok, why = None, False, f"{type(e).__name__}: {e}"
                ms = int((time.time() - tu) * 1000)
            with self.lock:
                st = self.state = load_state()
                if not ok:
                    st["counts"][s] = max(0, st["counts"].get(s, 1) - 1)   # 失败退回占位
                else:
                    st["stats"]["uploaded"] += 1
                    st.setdefault("ops", []).append(
                        {"kind": "attach", "id": seq + i, "record_id": rid, "field": field,
                         "screen": s, "file_token": tok, "fp": fp})
                    st["ops"] = st["ops"][-100:]
                save_state(st)
            n = st["counts"].get(s, 0)
            log(f"{'↑' if ok else '✗'} {s} 第 {n} 张 -> {rid} ({ms}ms){('  ' + why) if why else ''}")
            results.append((s, ok, ms, why))
            ops_log(type="图片", src=src, ocr=norm[:60], screen=s, action="上传附件",
                    target=self._target(rid, s), result="成功" if ok else "失败",
                    reason=why, ms=ms, ocr_ms=ocr_ms,
                    total_ms=int((time.time() - t0) * 1000))
        if temp and all(r[1] for r in results):
            try:
                os.remove(path)
            except Exception:
                pass
        self._dequeue(job.get("qid"))       # 上传完成才出队：中途崩溃 -> 下次启动自动补传

    def new_row(self):
        st = self.state
        copy_fields = self.cfg.get("limits", {}).get("new_row_copy_fields", ["终端名称"])
        wanted = {self.fmap.get(k, k) for k in copy_fields}
        fields = {k: v for k, v in (st.get("current_fields") or {}).items() if k in wanted}
        if not fields:
            fields = {self.fmap.get(k, k): v
                      for k, v in (self.cfg["feishu"].get("defaults", {}) or {}).items()}
            if st.get("current_name"):
                fields[self.fmap["终端名称"]] = st["current_name"]
        rid = "DRY" if self.dry else self.fs.create_record(fields)
        st["current_record_id"] = rid
        st["current_fields"] = fields
        st["counts"] = {}
        st.setdefault("ops", []).append(
            {"kind": "row", "id": st.get("ops_seq", 1), "record_id": rid,
             "name": st.get("current_name") or "", "mode": "new",
             "fields": list(fields.keys())})
        st["ops_seq"] = st.get("ops_seq", 1) + 1
        log(f"行已满 -> 新增一行并锁定 record={rid}")
        save_state(st)

    def flush_pending(self):
        st = self.state
        if not st.get("pending") or not st.get("current_record_id"):
            return
        pend = st["pending"]
        st["pending"] = []
        for p in pend:
            path = p["path"]
            if not os.path.exists(path):
                continue
            field = self.fmap.get(p["screen"], p["screen"])
            t0 = time.time()
            try:
                if not self.dry:
                    self.fs.append_attachment(st["current_record_id"], field, path,
                                              os.path.basename(path))
                st["counts"][p["screen"]] = st["counts"].get(p["screen"], 0) + 1
                log(f"↑ 回灌 {p['screen']} -> {st['current_record_id']}")
                ops_log(type="图片", src="pending 回灌", screen=p["screen"], action="补传附件",
                        target=self._target(st["current_record_id"], p["screen"]),
                        result="成功", reason="", ms=int((time.time() - t0) * 1000))
            except Exception as e:
                log(f"回灌失败: {e}")
                ops_log(type="图片", src="pending 回灌", screen=p["screen"], action="补传附件",
                        target=self._target(st["current_record_id"], p["screen"]),
                        result="失败", reason=str(e), ms=int((time.time() - t0) * 1000))
        save_state(st)


# ---------- 撤回 ----------
def undo(cfg, log=log):
    """撤回上一步：
      * 新建的行 -> 整行删除；
      * 写入的空白行 -> 只把写入的字段清空（行留着，不删用户的空行）；
      * 图片 -> 只删那一张，同单元格其它图片保留。
    """
    st = load_state()
    ops = st.get("ops", [])
    if not ops:
        return "没有可撤回的操作"
    op = ops.pop()
    fs = fsmod.Feishu(cfg, log, ROOT)
    if op["kind"] == "row":
        if op.get("mode") == "blank":
            cleared = {k: None for k in (op.get("fields") or [])}
            if cleared:
                fs.update_record(op["record_id"], cleared)
            msg = f"撤回：清空写入的空白行（行保留）"
        else:
            fs.delete_record(op["record_id"])
            msg = f"撤回：删除整行「{op.get('name') or '(无名)'}」"
        prev = next((o for o in reversed(ops) if o["kind"] == "row"), None)
        st["current_record_id"] = prev["record_id"] if prev else None
        st["current_name"] = prev["name"] if prev else None
        st["counts"] = {}
    else:
        fs.remove_attachment(op["record_id"], op["field"], op.get("file_token"))
        sname = op.get("screen")
        if sname:
            st["counts"][sname] = max(0, st["counts"].get(sname, 1) - 1)
        msg = f"撤回：删掉 {sname} 里的那一张（其余保留）"
    # 撤回后把这条内容的指纹从去重记忆里移除：马上重传同一内容不会被跳过
    fp = op.get("fp")
    if fp and fp in st.get("recent", []):
        st["recent"] = [x for x in st["recent"] if x != fp]
    st["ops"] = ops
    save_state(st)
    log("UNDO " + msg)
    ops_log(type=("文本" if op["kind"] == "row" else "图片"), src="手动撤回",
            action="撤回", target=op.get("record_id", "-"), result="成功", reason=msg, ms=0)
    return msg


def cmd_ocr(cfg, path):
    o = OCR(cfg.get("ocr", {}), ROOT, log)
    t = o.text(path)
    print("---- OCR 原文 ----")
    print(t)
    print("---- 归一化 ----")
    n = normalize(t)
    print(n[:600])
    print("---- 规则命中明细 ----")
    for r in cfg.get("rules", []):
        if not r.get("enabled", True):
            print(f"  [禁用] {r['screen']}")
            continue
        mode = r.get("mode", "any")
        hits = []
        for g in r.get("keywords", []):
            cands = g if isinstance(g, list) else [g]
            hit = [c for c in cands if normalize(c) in n]
            hits.append(bool(hit))
            print(f"    组{cands} -> 命中={bool(hit)} {hit}")
        ok = all(hits) if mode == "all" else (any(hits) if hits else True)
        tag = "  [兜底：前三屏都不命中才用]" if r.get("fallback") else ""
        if r.get("fallback") and not hits:
            verdict = "无条件兜底"
        else:
            verdict = "✅命中" if ok else "✗"
        print(f"  {r['screen']}: mode={mode}{tag} -> {verdict}")
    print("---- 最终（用真实判定函数 match_screen）----")
    flow = cfg.get("flow", {}) or {}
    fscreens = flow.get("screens") or []
    for label, counts, fidx in (("本行第 1 张", {}, 0),
                                ("本行第 2 张起", {s: 1 for s in fscreens}, len(fscreens))):
        sc = match_screen(n, cfg.get("rules", []),
                          {"force": None, "flow": flow, "flow_index": fidx, "counts": counts})
        targets = [sc] if sc else []
        if (sc in fscreens and flow.get("enabled") and
                flow.get("mode", "duplicate_first") == "duplicate_first" and fidx == 0):
            targets = list(fscreens)
        print(f"  {label}: 判定={sc or '未识别'} -> 写入 "
              f"{'+'.join(targets) or '（不写表）'}")


def cmd_status(cfg):
    st = load_state()
    print(json.dumps(st, ensure_ascii=False, indent=1))
    fs = fsmod.Feishu(cfg, log, ROOT)
    print("模式:", fs.mode)
    if fs.mode == "api":
        try:
            print("token 正常:", bool(fs.token()))
        except Exception as e:
            print("token 失败:", e)


def main():
    cfg = load_cfg()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "ocr":
        cmd_ocr(cfg, sys.argv[2])
        return
    if cmd == "status":
        cmd_status(cfg)
        return
    if cmd == "undo":
        print(undo(cfg))
        return
    dry = "--dry" in sys.argv
    app = Monitor(cfg, dry=dry)
    if cmd == "once":
        item = clipboard.read(TMP_DIR, log)
        print("剪贴板:", item["kind"], item.get("text") or item.get("path"))
        app.process(item)
        time.sleep(6)
        return
    try:
        app.loop()
    except KeyboardInterrupt:
        app.shutdown()


if __name__ == "__main__":
    main()
