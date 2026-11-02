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
import re
import shutil
import subprocess
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


# ---------------- 本地图片缓存：统计与清理 ----------------
# 图片确实会落在本地：
#   tmp/         每次复制的截图（上传成功后会自动删；失败/中断/程序被杀时会残留）
#   tmp/named/   命名后的副本
#   unmatched/   判定不出屏位的图（保留着方便你回看/复判）
#   pending/     还没收到店名、暂时挂起的图（**不能随便删**，删了就真没了）
# 所以做一个「一键清理」，并在清理时自动跳过队列/挂起里还在引用的文件。
CACHE_KINDS = {
    "tmp":       ("临时截图 tmp/", (TMP_DIR,), False),
    "unmatched": ("未识别图 unmatched/", (UNMATCH_DIR,), False),
    "pending":   ("挂起图 pending/", (PENDING_DIR,), False),
    "logs":      ("日志 logs/", (LOG_DIR,), False),
}


def _cache_files(kind, root=ROOT):
    """列出某类缓存的文件（只列图片/日志，不动 state.json / queue.json 等）。"""
    _label, dirs, _ = CACHE_KINDS[kind]
    exts = (".log", ".jsonl", ".err") if kind == "logs" else (".png", ".jpg", ".jpeg")
    out = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for dirpath, _dn, names in os.walk(d):
            for n in names:
                if n.lower().endswith(exts):
                    out.append(os.path.join(dirpath, n))
    return out


def _protected_paths():
    """队列/挂起里还在引用的文件 —— 清缓存时绝不删这些，否则会丢图。"""
    keep = set()
    try:
        if os.path.exists(QUEUE_PATH):
            for r in (json.load(open(QUEUE_PATH)) or []):
                if r.get("path"):
                    keep.add(os.path.abspath(r["path"]))
    except Exception:
        pass
    try:
        st = load_state()
        for p in (st.get("pending") or []):
            if p.get("path"):
                keep.add(os.path.abspath(p["path"]))
    except Exception:
        pass
    return keep


def cache_report(root=ROOT):
    """{kind: (文件数, 字节数)} —— 面板弹窗展示用。"""
    rep = {}
    for k in CACHE_KINDS:
        fs_ = _cache_files(k, root)
        rep[k] = (len(fs_), sum((os.path.getsize(p) for p in fs_ if os.path.exists(p)), 0))
    return rep


def cleanup_cache(kinds, min_age_sec=0, log=None, root=ROOT):
    """删掉指定类别的本地缓存图片。返回 (删除数, 释放字节数, 跳过数)。

    min_age_sec：只删这么久以前的文件（传 0 = 全删）。
    队列 / 挂起里还在引用的文件永远跳过。
    """
    keep = _protected_paths()
    now = time.time()
    n = freed = skipped = 0
    for k in (kinds or []):
        if k not in CACHE_KINDS:
            continue
        for p in _cache_files(k, root):
            try:
                if os.path.abspath(p) in keep:
                    skipped += 1
                    continue
                if min_age_sec and (now - os.path.getmtime(p)) < min_age_sec:
                    skipped += 1
                    continue
                sz = os.path.getsize(p)
                os.remove(p)
                n += 1
                freed += sz
            except Exception:
                pass
    if log and n:
        log(f"已清理本地缓存 {n} 个文件，释放 {freed / 1048576:.1f} MB"
            + (f"（跳过 {skipped} 个：队列/挂起仍在用或太新）" if skipped else ""))
    return n, freed, skipped


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
        self.ocr.prewarm()                 # 后台预热常驻 OCR 进程（一次性模型加载）
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
        self._row_born = {}         # record_id -> 建行时刻（新行需等飞书索引就绪）
        # 启动时自动清掉 N 天前的临时图残留（默认 3 天）：
        # 上传成功的图会自动删临时文件，但失败 / 强退 / 系统休眠留下的会一直攒着。
        try:
            days = int((cfg.get("cache") or {}).get("auto_clean_days", 3))
            if days > 0:
                _n, _freed, _sk = cleanup_cache(["tmp"], min_age_sec=days * 86400)
                if _n:
                    log(f"自动清理 {days} 天前的临时图 {_n} 张，释放 {_freed / 1048576:.1f} MB")
        except Exception:
            pass
        self._last_cc = clipboard.change_count()
        self.running = False
        self._names = None          # 终端名称列的去重集合（唯一值）；None=还没加载
        self._names_ready = threading.Event()
        # ---- 空白行缓存池（2026-11-02：修「收到店名后建行要十几秒」）----
        # 以前每个店名都要重新翻一遍整张表（365 行按 100/页要 4 次请求 ≈ 12s）。
        # 现在一次扫描攒一批空行放这儿，连续建行直接挨个取，几乎 0 耗时。
        self._blank_pool = []       # [(record_id, 界面行号)]，按界面从上到下
        self._blank_ts = 0.0        # 池子生成时刻（超过 TTL 就重新扫，避免用到失效行）
        self._blank_lock = threading.Lock()
        self._blank_ttl = float((self.cfg.get("text", {}) or {}).get("blank_pool_ttl", 90))
        # 「刚扫过、确认表里没有空行」的负缓存：这种时候每个店名都要新建行，
        # 没必要每次都再翻一遍整张表（实测 428 次建行里大部分是这种情况）。
        self._blank_empty_ts = 0.0
        self._blank_empty_ttl = float((self.cfg.get("text", {}) or {}).get("blank_empty_ttl", 30))
        if not dry:
            threading.Thread(target=self._load_unique_names, daemon=True,
                             name="unique-names").start()
            # 启动即后台预热一批空行：等你复制第一个店名时已经准备好了
            threading.Thread(target=self._warm_blank_pool, daemon=True,
                             name="blank-warm").start()
        else:
            self._names_ready.set()     # dry 模式不加载，也不让等待卡住
        log(f"{VERSION} | {self.fs.mode} 模式 | 并发 {self.workers} | dry={dry}")
        if self.fs.mode == "cli":
            log("提示：CLI 模式较慢；填 App Secret 走官方 API 可提速 2~3 倍")

    def _load_unique_names(self):
        """后台拉一次「终端名称」列的全量去重集合（= 界面底部「唯一值 N」）。"""
        try:
            label = (self.cfg.get("text", {}) or {}).get("field", "终端名称")
            names = self.fs.unique_names(label, limit=3000)
            self._names = names
            with self.lock:
                st = self.state = load_state()
                st["stats"]["unique"] = len(names)
                save_state(st)
            log(f"唯一值 {len(names)}（表内已有店名去重数）")
        except Exception as e:
            log(f"唯一值加载失败（不影响写入）: {e}")
        finally:
            self._names_ready.set()

    def _creator_target(self):
        """(中文列名, 写入用的 key, 字段类型)。类型用来判断能不能写文本。"""
        label = self.cfg["feishu"].get("creator_field") or "创建人"
        key = self.fmap.get(label, label)
        try:
            if getattr(self.fs, "_ftypes", None) is None:
                self.fs._name_map()
        except Exception:
            pass
        ft = getattr(self.fs, "_ftypes", None) or {}
        return label, key, (ft.get(key) if ft else None) or ft.get(label)

    def _app_name(self):
        """当前上传身份/应用名称（自动取，不需要手填）。

        来源优先级：① 配置 feishu.app_name；
        ② .cache/creator_identity.json 里缓存的「飞书自动填出来的身份名」；
        ③ 兜底 "飞书应用 <app_id>"。
        """
        n = (self.cfg.get("feishu", {}).get("app_name") or "").strip()
        if n:
            return n
        try:
            d = json.load(open(os.path.join(ROOT, ".cache", "creator_identity.json")))
            if d.get("app_name"):
                return d["app_name"]
        except Exception:
            pass
        return "飞书应用 " + (self.cfg.get("feishu", {}).get("app_id") or "")

    def _open_id(self):
        """当前登录用户的飞书 open_id（自动取）。

        来源优先级：① 配置 feishu.creator_user_id（已预置好）；
        ② 调一次 lark-cli 取当前用户，结果缓存进 .cache/creator_identity.json；
        ③ 都没有 -> 返回空串。
        """
        uid = (self.cfg.get("feishu", {}).get("creator_user_id") or "").strip()
        if uid:
            return uid
        p = os.path.join(ROOT, ".cache", "creator_identity.json")
        try:
            d = json.load(open(p))
            if d.get("open_id"):
                return d["open_id"]
        except Exception:
            pass
        cli = self.cfg.get("feishu", {}).get("lark_cli") or ""
        if not cli or not os.path.exists(cli):
            return ""
        try:
            out = subprocess.run([cli, "contact", "+get-user", "--as", "user", "--format", "json"],
                                 capture_output=True, text=True, timeout=30).stdout
            m = re.search(r'"open_id"\s*:\s*"([^"]+)"', out)
            if m:
                try:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    json.dump({"open_id": m.group(1)}, open(p, "w"))
                except Exception:
                    pass
                return m.group(1)
        except Exception:
            pass
        return ""

    def _creator_fields(self):
        """创建人列：全自动取值，不需要任何手工填写。

        优先级：① 当前应用/上传身份名称  ->  ② 当前登录用户的飞书 open_id  ->  ③ 不写。

        实测结论（2026-10-31）：本表「创建人」是「人员」类型（type 11），
        只接受飞书通讯录里的真实用户 open_id；写「应用名称」这类文字一律被拒
        （1254066 UserFieldConvFail）。所以：
          * 人员列   -> 直接走 open_id（应用名在这里必然失败，不再浪费一次请求）；
          * 文本列   -> 写应用名称（文本列存得下文字）。
        两者都拿不到值时跳过该列，不影响建行（_create_row 还有去掉该列重试的兜底）。
        """
        label, key, ftype = self._creator_target()
        if ftype == 11:                                  # 人员字段：只认 open_id
            uid = self._open_id()
            if not uid:
                if not getattr(self, "_creator_warned", False):
                    self._creator_warned = True
                    log(f"提示：「{label}」是「人员」类型，且没取到 open_id，本行不写该列（其余字段照常）")
                return {}
            return {key: [{"id": uid}]}
        name = self._app_name()                          # 文本字段：写应用名称
        if not name:
            return {}
        return {key: name}

    def _create_row(self, fields):
        """新建行；若「创建人」这类字段写不进去（1254066），去掉它重试一次，保证行能建起来。"""
        if self.dry:
            return "DRY"
        try:
            rid = self.fs.create_record(fields)
        except fsmod.FeishuError as e:
            if getattr(e, "code", None) != 1254066:
                raise
            _, key, _ = self._creator_target()
            if key not in fields:
                raise
            f2 = {k: v for k, v in fields.items() if k != key}
            log(f"「{key}」写入被拒（1254066 人员/用户字段），已去掉该列重试建行")
            rid = self.fs.create_record(f2)
        # 记住建行时刻：新行在飞书侧要 1~2 秒才可被读（读太早会 1254607 not ready），
        # 上传前据此稍等一下，能大幅降低「新建行后第一张图丢失」的概率
        self._row_born[rid] = time.time()
        # 新行本来就不可能有附件 -> 预置空列表，上传前那次「读记录」全省掉。
        # 这一步同时解决了新建行后第一张图最容易丢的问题（读新行常返回 not ready）。
        try:
            acache = getattr(self.fs, "_acache", None)
            if acache is not None:
                for s in self.fmap:
                    fid = self.fmap.get(s)
                    if fid:
                        acache[(rid, fid)] = []
        except Exception:
            pass
        return rid

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
                log(f"剪贴板 #{cc}：无可识别内容，跳过")
                return
            fp = clipboard.fingerprint(item)
            with self.lock:
                st = self.state = load_state()
                if self.cfg.get("dedupe", True) and fp in st["recent"]:
                    log(f"剪贴板 #{cc}：与上次重复，跳过")
                    return
                st["recent"].append(fp)
                st["recent"] = st["recent"][-self.cfg.get("dedupe_window", 20):]
                save_state(st)
            self._enqueue(item)
            log(("文本" if item["kind"] == "text" else "图片") + f" #{cc} 已捕获")
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
            # 被撤回过的内容也算「已完成」：绝不因为恢复队列又把撤回的图补传上去
            done |= {c.get("fp") for c in (st.get("canceled") or []) if c.get("fp")}
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
        try:
            self.ocr.close()            # 停掉常驻 OCR 进程
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
        fields.update(self._creator_fields())

        mode = tcfg.get("row_selection", "auto")     # auto | new | blank
        rid, used = None, "new"
        if mode in ("auto", "blank"):
            rid = self._take_blank(tcfg)          # 多数情况走缓存池，0 次网络请求
            if rid:
                used = "blank"
        ms = int((time.time() - t0) * 1000)
        if rid:
            try:
                if not self.dry:
                    self.fs.update_record(rid, fields)
                log(f"店名「{text}」-> 写入空行 {rid[-6:]}")
                action = "写入空白行"
            except Exception as e:
                # 缓存的空行可能已失效（在飞书界面里被删掉 / 被手动填了内容）：
                # 丢掉整池重新扫一次再写；还不行就退回新建行。
                log(f"写入空行失败（{e}），重新扫描后重试")
                rid = None
                rid2 = self._take_blank(tcfg, force=True)
                if rid2:
                    try:
                        if not self.dry:
                            self.fs.update_record(rid2, fields)
                        rid = rid2
                        log(f"店名「{text}」-> 写入空行 {rid[-6:]}")
                        action = "写入空白行"
                    except Exception:
                        rid = None
        if not rid:
            if mode == "blank":
                reason = "没找到空白行，且 row_selection=blank（不新建）"
                log(reason)
                ops_log(type="文本", src="剪贴板", detail=text, action="未写入",
                        target="-", result="失败", reason=reason, ms=ms)
                return False
            rid = self._create_row(fields)
            log(f"店名「{text}」-> 新建行 {rid[-6:]}")
            action = "新建行"

        # 唯一值检查：店名写进去后唯一值没增加（= 这个名字表里已存在）-> 红字提醒有重复数据
        # 后台拉全量唯一值要 10s+，这里只等 3s：等不到就跳过本次检查，
        # 不能让「等唯一值」把建行拖慢（以前等 15s，启动后第一个店名会明显卡一下）。
        if not self.dry and not self._names_ready.wait(timeout=3):
            log("唯一值尚未加载完，跳过本次重复检查（不影响写入）")
        if not self.dry and self._names is not None:
            nm = text.strip()
            if nm and nm in self._names:
                log(f"重复数据：唯一值仍是 {len(self._names)}，「{nm}」表里已存在")
            elif nm:
                self._names.add(nm)
                log(f"唯一值 {len(self._names)}（+1）")
                with self.lock:
                    st = self.state = load_state()
                    st["stats"]["unique"] = len(self._names)
                    save_state(st)

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

    def _blank_labels(self, tcfg):
        return (tcfg.get("blank_check_fields") or
                ["终端名称", "第一屏", "第二屏", "第三屏", "第四屏"])

    def _warm_blank_pool(self, tcfg=None, want=12):
        """后台扫一批空行放进池子（启动预热 / 池子见底时补货用）。"""
        try:
            tcfg = tcfg or (self.cfg.get("text", {}) or {})
            rows = self.fs.find_blank_rows(
                self._blank_labels(tcfg),
                max_scan=int(tcfg.get("blank_scan_limit", 600)), want=want)
            with self._blank_lock:
                self._blank_pool = rows
                self._blank_ts = time.time()
                self._blank_empty_ts = 0.0 if rows else time.time()
            if rows:
                log(f"空白行已就绪：攒了 {len(rows)} 行（界面第 {rows[0][1]} 行起）")
        except Exception:
            pass

    def _take_blank(self, tcfg, force=False):
        """取一条空行。

        优先从缓存池拿（0 次网络请求，几乎瞬时）；池子空了 / 超过 TTL 才扫表，
        并且一次扫就顺带攒一批（want=12），这样连续建十几个店名只扫一次表。
        force=True 表示池子可能失效了，强制重新扫描。
        """
        if not force:
            with self._blank_lock:
                if self._blank_pool and (time.time() - self._blank_ts) < self._blank_ttl:
                    rid, pos = self._blank_pool.pop(0)
                    self.fs.last_blank_pos = pos
                    log(f"空白行：界面第 {pos} 行（{rid[-6:]}）")
                    return rid
                self._blank_pool = []          # 过期，丢掉
                # 刚扫过且确认没空行 -> 这段时间内不再重复扫，直接让调用方新建行
                if self._blank_empty_ts and \
                        (time.time() - self._blank_empty_ts) < self._blank_empty_ttl:
                    return None
        try:
            rows = self.fs.find_blank_rows(
                self._blank_labels(tcfg),
                max_scan=int(tcfg.get("blank_scan_limit", 600)), want=12)
        except Exception as e:
            log(f"查找空白行失败: {e}")
            return None
        with self._blank_lock:
            self._blank_pool = rows[1:] if rows else []
            self._blank_ts = time.time()
            self._blank_empty_ts = 0.0 if rows else time.time()
        if not rows:
            log("表里没有空白行 -> 新建一行（30 秒内不再重复扫表）")
            return None
        rid, pos = rows[0]
        self.fs.last_blank_pos = pos
        log(f"空白行：界面第 {pos} 行（{rid[-6:]}）")
        return rid

    def _find_blank(self, tcfg):
        """找一条真正没有数据的行（名称 + 各屏位都为空）。兼容旧调用。"""
        return self._take_blank(tcfg)

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
        if hit:                 # 空文本不认（不缓存空结果，避免把偶发的识别失败固化下来）
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
            if fp and text.strip():
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
            img_no = sum(st["counts"].values()) + 1          # 本行第几张图（1 起）
            ctx = {"force": force, "flow": flow, "flow_index": flow_idx,
                   "counts": st.get("counts", {})}
            screen = match_screen(norm, self.rules, ctx)
            why = ctx.get("decision") or ("无", "-")
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

        # ---- 分屏日志：每张图一行，只留核心：序号、结果、判定来源、各屏累计 ----
        why_text = f"依据={why[0]}：{why[1]}"
        cnt = st["counts"]
        cnt_s = " ".join(f"{k[1]}{int(cnt.get(k, 0) or 0)}"     # 一/二/三/四
                         for k in ("第一屏", "第二屏", "第三屏", "第四屏"))
        if screen:
            log(f"[分屏] 第{img_no}张 -> {'+'.join(screens)}（{why[0]}）| {cnt_s}")
        else:
            log(f"[分屏] 第{img_no}张 -> 未识别（{why[0]}）| {cnt_s} | OCR: {norm[:30]}")

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
            ops_log(type="图片", src=src, ocr=norm[:60], action="判定屏位",
                    target="-", result="未识别",
                    reason=f"本行第 {img_no} 张；{why_text}；所有规则都不命中，"
                           f"已存 unmatched/，未写入表格",
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
                    log(f"无当前行，{screen} 图先挂起（收到店名后自动回灌）")
                    save_state(st)
                    ops_log(type="图片", src=src, ocr=norm[:60], action="挂起等待店名",
                            target="-", result="排队",
                            reason=f"本行第 {img_no} 张，判定 {screen}；{why_text}；"
                                   f"还没收到店名文本，图片先放 pending/，收到文本后自动回灌",
                            ms=int((time.time() - t0) * 1000), ocr_ms=ocr_ms)
                    return True
                fields = {self.fmap.get(k, k): v
                          for k, v in (self.cfg["feishu"].get("defaults", {}) or {}).items()}
                fields[self.fmap["终端名称"]] = self.cfg.get("fallback", {}).get("unknown_name", "")
                fields.update(self._creator_fields())
                rid = self._create_row(fields)
                st["current_record_id"] = rid
                st["current_fields"] = fields
                st["counts"] = {}
                st.setdefault("ops", []).append(
                    {"kind": "row", "id": st.get("ops_seq", 1), "record_id": rid,
                     "name": "", "mode": "new", "fields": list(fields.keys())})
                st["ops_seq"] = st.get("ops_seq", 1) + 1
                st["ops"] = st["ops"][-100:]
                log(f"无店名，先自动建行 {rid[-6:]}")
            save_state(st)

        with self.lock:
            st = self.state = load_state()
            maxn = self.cfg.get("limits", {}).get("per_screen_max", {}).get(screen, 50)
            overflow = self.cfg.get("limits", {}).get("overflow", "skip")
            rid0 = st.get("current_record_id")
            over_limit = st["counts"].get(screen, 0) > maxn   # counts 已含本张（占位）

        # 超上限先别急着跳过：用户可能在飞书里手动删过附件，本地 counts 是旧值，
        # 会出现「表里明明没满却一直提示已满、只能点撤回」的死结（撤回还可能删错图）。
        # 这里以远端实际数量校准一次，校准后没满就正常放行。
        if over_limit and overflow != "new_row" and rid0 and not self.dry:
            try:
                fid_s = self.fmap.get(screen, screen)
                cur = self.fs.get_record(rid0, [fid_s]).get(fid_s) or []
                actual = (len(cur) if isinstance(cur, list) else 0) + 1   # +1 = 本张占位
                with self.lock:
                    st = self.state = load_state()
                    if actual < st["counts"].get(screen, 0):
                        st["counts"][screen] = actual
                        save_state(st)
                        log(f"{screen} 本地计数比远端多（可能手动删过），已按远端校准为 {actual}")
                over_limit = actual > maxn
            except Exception:
                pass

        with self.lock:
            st = self.state = load_state()
            if over_limit and st["counts"].get(screen, 0) > maxn:      # counts 已含本张（占位）
                if overflow != "new_row":
                    # 默认跳过：不偷偷新建行（以前会新建一行并把锁定行换走，很容易把图传散）
                    for s in screens:                 # 退回占位
                        st["counts"][s] = max(0, st["counts"].get(s, 1) - 1)
                    save_state(st)
                    reason = (f"本行第 {img_no} 张，判定 {screen}；{why_text}；"
                              f"{screen} 已有 {maxn} 张（上限）。想重传这张："
                              f"先点「撤回上一步」删掉旧图，再重新复制这张图")
                    log(f"跳过：{screen} 已满 {maxn} 张（先「撤回上一步」再重传）")
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
                   maxn=maxn, overflow=overflow, img_no=img_no, why_text=why_text)
        if self.dry:
            log(f"[dry] {'+'.join(screens)} -> {rid}  ({os.path.basename(path)})")
            for s in screens:
                ops_log(type="图片", src=src, ocr=norm[:60], screen=s, action="上传附件(试运行)",
                        target=self._target(rid, s), result="成功",
                        reason=f"本行第 {img_no} 张；{why_text}；dry 模式未真传",
                        ms=ms0, ocr_ms=ocr_ms)
            return True
        # 真正的上传丢回线程池（多屏位可并行），但「判定顺序」已在 dispatcher 里定好，
        # 且同一单元格有锁串行，所以既快又不会串屏
        self.pool.submit(self._upload_job, job)
        return ASYNC

    def _canceled(self, rid, fp):
        """该上传任务是否已被「撤回」取消（撤回时会把 rid / (rid+fp) 记进 state）。"""
        try:
            st = load_state()
            if rid and rid in (st.get("canceled_rows") or []):
                return True
            if fp:
                for c in (st.get("canceled") or [])[-100:]:
                    if c.get("record_id") == rid and c.get("fp") == fp:
                        return True
        except Exception:
            pass
        return False

    def _upload_job(self, job):
        """上传一个任务（可能写多个屏位）。完成后才把队列里的记录删掉。"""
        rid, screens, path, name = job["rid"], job["screens"], job["path"], job["name"]
        seq, fp, src, norm = job["seq"], job["fp"], job["src"], job["norm"]
        ocr_ms, t0, temp = job["ocr_ms"], job["t0"], job["temp"]
        # 新行刚建的 1~2 秒内飞书还读不到它（1254607 not ready），这里先等它就绪，
        # 否则「刚收到店名就截的第一张图」最容易撞上这个错而丢失
        born = self._row_born.get(rid, 0)
        wait = born + 1.2 - time.time()
        if 0 < wait < 4:
            time.sleep(wait)
        # 任务排队/上传期间用户点了撤回 -> 这张就别再传了（否则撤回后图又冒出来）
        if self._canceled(rid, fp):
            log(f"已撤回，放弃补传：{name}")
            self._dequeue(job.get("qid"))
            return
        results = []
        for i, s in enumerate(screens):
            field = self.fmap.get(s, s)
            # 同单元格串行（读-改-写），不同单元格/不同行可并行
            with self.flock(f"{rid}:{field}"):
                tu = time.time()
                # 失败必须重试：一次 1254607/限流就把整张图丢掉 = 用户眼里的「丢图」。
                # 这里最多 3 次（退避 1.5s / 3s）；仍失败会保留在待传队列里，下次启动补传。
                ok, why, tok = False, "", None
                for att in range(3):
                    try:
                        tok = self.fs.append_attachment(rid, field, path, name)
                        ok, why = True, ""
                        break
                    except Exception as e:
                        tok = None
                        why = f"{type(e).__name__}: {e}"
                        if att < 2:
                            time.sleep(1.5 * (att + 1))
                ms = int((time.time() - tu) * 1000)
            # 传完再查一次：上传这几秒内用户点了撤回 -> 立刻把刚传上去的删掉，不留重复
            if ok and self._canceled(rid, fp):
                try:
                    self.fs.remove_attachment(rid, field, tok)
                    log(f"↑ {s} 传完时发现已被撤回，已顺手删掉")
                except Exception as e2:
                    log(f"撤回后清理刚传的图失败: {e2}")
                with self.lock:
                    st = self.state = load_state()
                    st["counts"][s] = max(0, st["counts"].get(s, 1) - 1)
                    save_state(st)
                results.append((s, True, ms, "已撤回"))
                continue
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
                    reason=why if why else
                    f"本行第 {job.get('img_no', '?')} 张；{job.get('why_text', '')}",
                    ms=ms, ocr_ms=ocr_ms,
                    total_ms=int((time.time() - t0) * 1000))
        if temp and all(r[1] for r in results):
            try:
                os.remove(path)
            except Exception:
                pass
        if all(r[1] for r in results):
            self._dequeue(job.get("qid"))       # 上传完成才出队：中途崩溃 -> 下次启动自动补传
        else:
            # 全部重试都失败：这张图绝不能悄悄丢掉。
            # 留在队列里（临时文件也不删），面板重启后会「恢复未完成任务」自动补传。
            bad = "、".join(r[0] for r in results if not r[1])
            log(f"⚠ {bad} 连续重试 3 次仍失败，图片已保留在待传队列（重启面板会自动补传）")

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
        fields.update(self._creator_fields())
        rid = self._create_row(fields)
        st["current_record_id"] = rid
        st["current_fields"] = fields
        st["counts"] = {}
        st.setdefault("ops", []).append(
            {"kind": "row", "id": st.get("ops_seq", 1), "record_id": rid,
             "name": st.get("current_name") or "", "mode": "new",
             "fields": list(fields.keys())})
        st["ops_seq"] = st.get("ops_seq", 1) + 1
        log(f"行已满 -> 自动新增一行 {rid[-6:]} 并锁定")
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
                    tok = self.fs.append_attachment(st["current_record_id"], field, path,
                                                    os.path.basename(path))
                else:
                    tok = None
                st["counts"][p["screen"]] = st["counts"].get(p["screen"], 0) + 1
                # 记入 ops：回灌的图也要能被「撤回上一步」撤掉（以前漏记，撤回不到它们）
                try:
                    fp = clipboard.fingerprint({"kind": "image", "path": path})
                except Exception:
                    fp = None
                st.setdefault("ops", []).append(
                    {"kind": "attach", "id": st.get("ops_seq", 1),
                     "record_id": st["current_record_id"], "field": field,
                     "screen": p["screen"], "file_token": tok, "fp": fp})
                st["ops_seq"] = st.get("ops_seq", 1) + 1
                st["ops"] = st["ops"][-100:]
                log(f"↑ 回灌 {p['screen']}")
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
    # 关键：撤回不只是删「已传完的」，还要取消「还在排队/上传中」的任务，
    # 否则撤回后池子里的任务继续跑，图又会冒出来（用户看到的就是"重复上传"）。
    if op["kind"] == "row":
        rows = st.get("canceled_rows") or []
        rows.append(op["record_id"])
        st["canceled_rows"] = rows[-50:]
    else:
        canceled = st.get("canceled") or []
        canceled.append({"record_id": op["record_id"], "field": op.get("field"),
                         "fp": fp, "t": time.time()})
        st["canceled"] = canceled[-100:]
    st["ops"] = ops
    save_state(st)
    # 队列（queue.json）里同一张图的待传任务也一并清掉：
    # 不然下次启动「恢复未完成任务」会把刚撤回的图再补传一遍
    if fp:
        try:
            q = json.load(open(QUEUE_PATH)) if os.path.exists(QUEUE_PATH) else []
            keep = []
            for r in q:
                if r.get("kind") == "image" and r.get("path") and os.path.exists(r["path"]):
                    try:
                        if clipboard.fingerprint({"kind": "image", "path": r["path"]}) == fp:
                            continue
                    except Exception:
                        pass
                keep.append(r)
            json.dump(keep, open(QUEUE_PATH, "w"), ensure_ascii=False, indent=1)
        except Exception:
            pass
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
