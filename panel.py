#!/usr/bin/env python3
"""桌面原生控制面板（PyObjC，可自由拉伸、记住大小与位置、自带日志区）。

本次改动：
  1. 窗口可自由缩放：主窗口 + 设置窗口都带 NSResizableWindowMask，
     设置窗口内容放进 NSScrollView（再小的窗口也能滚到按钮，不会点不到），
     大小与位置写入 ui.json，下次打开自动恢复。
  2. 第二屏可验证：设置里每条屏位规则都能勾选启用/填关键词；主窗口「🔍 测试图片」
     会列出每条规则的命中情况与最终屏位（第二屏默认关闭，勾上就能验证）。
  3. 关闭窗口即彻底停止：监听跑在本进程内的后台线程池，窗口关闭 -> 停止轮询、
     关闭线程池、顺手 bootout 可能残留的 launchd 服务 -> 进程退出，不留后台常驻。
  4. 上传提速：OCR+上传全在后台线程池（可并发），飞书请求连接复用，大图可选压缩。
  5. 日志区：逐条显示 时间/类型/来源/动作/目标/结果/耗时。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

import AppKit
import objc
from Foundation import NSMakeRange, NSMakeRect, NSObject, NSTimer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

CFG = os.path.join(ROOT, "config.yaml")
STATE = os.path.join(ROOT, "state.json")
UI = os.path.join(ROOT, "ui.json")
OPS_LOG = os.path.join(ROOT, "logs", "ops.jsonl")
SYNC_LOG = os.path.join(ROOT, "logs", "sync.log")
LABEL = "com.james.clipboard-sync"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
PY = "/Users/james/.workbuddy/binaries/python/versions/3.13.12/bin/python3"

for _d in ("/opt/homebrew/bin", "/usr/local/bin",
           "/Users/james/.workbuddy/binaries/node/versions/current/bin",
           "/Users/james/.workbuddy/binaries/node/versions/22.22.2-6/bin"):
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


# ---------- 日志区工具（模块级：避免 PyObjC 把下划线方法当 selector） ----------
def tail_new_lines(path, pos):
    """读文件新增部分，返回 (新行列表, 新位置)。"""
    if not os.path.exists(path):
        return [], pos
    try:
        size = os.path.getsize(path)
        if size < pos:
            pos = 0
        if size == pos:
            return [], pos
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read()
            pos = f.tell()
        return data.decode("utf-8", "ignore").splitlines(), pos
    except Exception:
        return [], pos


def append_log_line(tv, txt, color, font):
    tv.textStorage().appendAttributedString_(
        AppKit.NSAttributedString.alloc().initWithString_attributes_(
            txt, {AppKit.NSForegroundColorAttributeName: color,
                  AppKit.NSFontAttributeName: font}))


def scroll_log_bottom(tv):
    """滚到底部，保证永远看到最新一条。"""
    if tv is None:
        return
    try:
        tv.layoutManager().ensureLayoutForTextContainer_(tv.textContainer())
    except Exception:
        pass
    n = tv.textStorage().length()
    tv.scrollRangeToVisible_(NSMakeRange(max(0, n - 1), 1))


# ---------- 配置读写（保留注释） ----------
def load_cfg():
    from ruamel.yaml import YAML
    y = YAML()
    with open(CFG) as f:
        return y.load(f), y


def save_cfg(data, y):
    with open(CFG, "w") as f:
        y.dump(data, f)


def getv(d, path):
    cur = d
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
        if cur is None:
            return ""
    return cur


def setv(d, path, val):
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = val


def load_ui():
    try:
        return json.load(open(UI))
    except Exception:
        return {}


def save_ui(d):
    try:
        json.dump(d, open(UI, "w"))
    except Exception:
        pass


def frame_of(win):
    f = win.frame()
    return [round(float(f.origin.x), 1), round(float(f.origin.y), 1),
            round(float(f.size.width), 1), round(float(f.size.height), 1)]


def service_running():
    p = subprocess.run(["launchctl", "list", LABEL], capture_output=True, text=True)
    return p.returncode == 0


def stop_service():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                   capture_output=True)


def start_service():
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST],
                       capture_output=True, text=True)
    return p.returncode == 0


class Controller(NSObject):
    win = None
    status = None
    detail = None
    btn = None
    lastop = None
    logtv = None
    setwin = None
    autocb = None
    forcepop = None
    fields = {}
    popup = None
    rules = []
    monitor = None
    timer = None
    _logpos = 0
    _syncpos = 0
    _autoscroll = True
    _last_cc_shown = -1
    _resume_after_settings = False

    # ---------- 主面板 ----------
    def build(self):
        # ---- 崩溃哨兵：正常退出会删掉 .alive；下次启动还在 = 上次是崩溃/被杀 ----
        self.alive = os.path.join(ROOT, ".alive")
        self._crashed_last = False
        if os.path.exists(self.alive):
            try:
                old = open(self.alive).read().strip()
                old_pid = int(old.split()[0]) if old else 0
                running = False
                if old_pid:
                    try:
                        os.kill(old_pid, 0)
                        running = True
                    except Exception:
                        running = False
                if old_pid != os.getpid() and not running:
                    self._crashed_last = True
            except Exception:
                self._crashed_last = True
        try:
            with open(self.alive, "w") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        ui = load_ui().get("main") or [1180, 520, 430, 620]
        x, y, w, h = ui
        style = (AppKit.NSTitledWindowMask | AppKit.NSClosableWindowMask |
                 AppKit.NSResizableWindowMask | AppKit.NSMiniaturizableWindowMask)
        self.win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), style, AppKit.NSBackingStoreBuffered, False)
        self.win.setTitle_("剪贴板采集")
        self.win.setLevel_(AppKit.NSFloatingWindowLevel)
        self.win.setOpaque_(True)
        self.win.setDelegate_(self)
        self.win.setMinSize_(AppKit.NSMakeSize(300, 380))

        cv = self.win.contentView()
        TOP = AppKit.NSViewMinYMargin          # 贴顶
        WID = AppKit.NSViewWidthSizable

        self.status = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(14, h - 32, w - 28, 22))
        self.status.setEditable_(False)
        self.status.setBezeled_(False)
        self.status.setDrawsBackground_(False)
        self.status.setFont_(AppKit.NSFont.boldSystemFontOfSize_(14))
        self.status.setAutoresizingMask_(WID | TOP)
        cv.addSubview_(self.status)

        self.detail = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(14, h - 82, w - 28, 46))
        self.detail.setEditable_(False)
        self.detail.setBezeled_(False)
        self.detail.setDrawsBackground_(False)
        self.detail.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        self.detail.setAutoresizingMask_(WID | TOP)
        cv.addSubview_(self.detail)

        self.lastop = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(14, h - 104, w - 28, 18))
        self.lastop.setEditable_(False)
        self.lastop.setBezeled_(False)
        self.lastop.setDrawsBackground_(False)
        self.lastop.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        self.lastop.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self.lastop.setAutoresizingMask_(WID | TOP)
        cv.addSubview_(self.lastop)

        specs = [("▶ 开始监听 / ⏸ 停止", "toggle:"), ("↩ 撤回上一步", "doUndo:"),
                 ("🧹 清空剪贴板 + 去重记忆", "clearClip:"), ("🗑 清理本地图片缓存", "cleanCache:"),
                 ("🔍 测试图片（验证屏位规则）", "doTest:"),
                 ("🔗 打开当前行", "openRow:"), ("🔌 测试飞书连接", "testConn:"),
                 ("📋 复制日志", "copyLog:"), ("⚙ 设置", "openSettings:")]
        yy = h - 140
        for title, act in specs:
            b = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(14, yy, w - 28, 28))
            b.setTitle_(title)
            b.setBezelStyle_(AppKit.NSBezelStyleRounded)
            b.setTarget_(self)
            b.setAction_(act)
            b.setAutoresizingMask_(WID | TOP)
            cv.addSubview_(b)
            if act == "toggle:":
                self.btn = b
            yy -= 31

        # 「下一张强制为」：顺序判定偶尔会串（比如漏截了第二屏），用这个手动纠正一次
        flab = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(14, yy - 2, 120, 16))
        flab.setStringValue_("下一张强制为")
        flab.setEditable_(False)
        flab.setBezeled_(False)
        flab.setDrawsBackground_(False)
        flab.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        flab.setAutoresizingMask_(TOP)
        cv.addSubview_(flab)
        self.forcepop = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(134, yy - 6, w - 148, 24))
        self.forcepop.addItemsWithTitles_(["自动（按顺序判定）", "第一屏", "第二屏", "第三屏", "第四屏"])
        self.forcepop.setTarget_(self)
        self.forcepop.setAction_("forceChanged:")
        self.forcepop.setAutoresizingMask_(WID | TOP)
        cv.addSubview_(self.forcepop)
        yy -= 30

        lab = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(14, yy - 2, 70, 16))
        lab.setStringValue_("操作日志")
        lab.setEditable_(False)
        lab.setBezeled_(False)
        lab.setDrawsBackground_(False)
        lab.setFont_(AppKit.NSFont.boldSystemFontOfSize_(11))
        lab.setAutoresizingMask_(TOP)
        cv.addSubview_(lab)
        cb = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(84, yy - 4, 110, 18))
        cb.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb.setTitle_("自动滚动")
        cb.setState_(1)
        cb.setTarget_(self)
        cb.setAction_("autoScrollToggled:")
        cb.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        cb.setAutoresizingMask_(TOP)
        cv.addSubview_(cb)
        self.autocb = cb
        yy -= 20

        sc = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(14, 10, w - 28, yy - 12))
        sc.setHasVerticalScroller_(True)
        sc.setBorderType_(AppKit.NSBezelBorder)
        sc.setAutoresizingMask_(WID | AppKit.NSViewHeightSizable)
        sc.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.10, 0.10, 0.12, 1.0))          # 深底，白字才看得清
        tv = AppKit.NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 28, yy - 12))
        tv.setEditable_(False)
        tv.setSelectable_(True)              # 允许选中 -> ⌘C 复制（也可点「📋 复制日志」）
        tv.setRichText_(True)                # 需要富文本才能给每行指定颜色
        tv.setBackgroundColor_(AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.10, 0.10, 0.12, 1.0))
        tv.setTextColor_(AppKit.NSColor.whiteColor())
        tv.setFont_(AppKit.NSFont.userFixedPitchFontOfSize_(11))
        tv.setAutoresizingMask_(WID)
        sc.setDocumentView_(tv)
        cv.addSubview_(sc)
        self.logtv = tv
        self.logfont = AppKit.NSFont.userFixedPitchFontOfSize_(11)
        self.logcolors = {
            "成功": AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.95, 0.60, 1.0),
            "失败": AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.55, 0.50, 1.0),
            # 未识别 = 识别不成功，按用户要求也用红色（和失败同级醒目）
            "未识别": AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.50, 0.48, 1.0),
        }
        self.logdefault = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.97, 0.97, 0.99, 1.0)

        self.win.makeKeyAndOrderFront_(None)
        # 首次只回看最近一小段历史，避免把整份旧日志倒进日志区
        for p, attr in ((OPS_LOG, "_logpos"), (SYNC_LOG, "_syncpos")):
            try:
                setattr(self, attr, max(0, os.path.getsize(p) - 2000))
            except Exception:
                setattr(self, attr, 0)
        self.refresh_(None)
        self.pumpLog_(None)
        if self._crashed_last:
            append_log_line(
                self.logtv,
                "⚠ 检测到上次是异常退出（崩溃/被杀）。若再次发生，请打开「控制台.app」→ 崩溃报告，"
                "把「剪贴板采集 / python3.13」的最新报告发我，按堆栈精确定位\n",
                self.logcolors["失败"], self.logfont)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.7, self, "refresh:", None, True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.6, self, "pumpLog:", None, True)
        # 剪贴板轮询：NSPasteboard 只能在主线程读，所以用主线程定时器高频 tick
        try:
            import sync as _s
            ms = int(_s.load_cfg().get("poll_interval_ms", 120))
        except Exception:
            ms = 120
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            max(0.05, ms / 1000.0), self, "pollTick:", None, True)

    # ---------- 监听开关（内嵌，不依赖 launchd，关窗即停） ----------
    def toggle_(self, sender):
        if self.monitor is not None:
            self.monitor.shutdown()
            self.monitor = None
            try:
                import sync
                sync.log("面板停止监听")
            except Exception:
                pass
        else:
            try:
                import sync
                cfg = sync.load_cfg()
                if service_running():
                    stop_service()          # 交给面板接管，避免双份监听
                self.monitor = sync.Monitor(cfg, dry=(os.environ.get("CS_DRY") == "1"))
                self.monitor.running = True
                sync.log("面板开始监听剪贴板…")
            except Exception as e:
                self.showMsg_(f"启动监听失败：{e}")
        self.refresh_(None)

    def start_if_enabled(self):
        cfg = {}
        try:
            import sync
            cfg = sync.load_cfg()
        except Exception:
            pass
        if (cfg.get("ui") or {}).get("auto_start", True):
            self.toggle_(None)

    def forceChanged_(self, sender):
        """把「下一张强制为」写进 state.json，sync 判定后用完即清。"""
        idx = self.forcepop.indexOfSelectedItem()
        val = [None, "第一屏", "第二屏", "第三屏", "第四屏"][idx] if 0 <= idx < 5 else None
        try:
            s = json.load(open(STATE))
        except Exception:
            s = {}
        s["force_screen"] = val
        json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)

    def doUndo_(self, sender):
        try:
            import sync
            msg = sync.undo(sync.load_cfg())
        except Exception as e:
            msg = f"撤回失败：{e}"
        self.showMsg_(msg)
        self.refresh_(None)

    def clearClip_(self, sender):
        pb = AppKit.NSPasteboard.generalPasteboard()
        pb.clearContents()
        try:
            # 一并清掉「去重记忆」：撤回重传 / 上次没传好想重来的，不再被当重复跳过
            s = json.load(open(STATE))
            n = len(s.get("recent", []))
            s["recent"] = []
            json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)
            import sync
            sync.log(f"已清空剪贴板 + 去重记忆（{n} 条指纹）")
        except Exception:
            pass
        self.refresh_(None)

    # ---------- 清理本地图片缓存 ----------
    def cleanCache_(self, sender):
        """弹窗显示各类缓存占用，让用户选择清哪些。

        图片确实存在本地：tmp/（临时截图，传成功会自动删，失败/中断会残留）、
        unmatched/（判定不出屏位保留下来的）、pending/（挂起，清了会真丢图，默认不清）。
        清理时会自动跳过队列/挂起里还在引用的文件。
        """
        try:
            import sync
            rep = sync.cache_report()
            mb = lambda b: f"{b / 1048576:.1f} MB"
            txt = (f"临时截图  tmp/        {rep['tmp'][0]} 张   {mb(rep['tmp'][1])}\n"
                   f"未识别图  unmatched/  {rep['unmatched'][0]} 张   {mb(rep['unmatched'][1])}\n"
                   f"挂起图    pending/    {rep['pending'][0]} 张   {mb(rep['pending'][1])}\n"
                   f"日志      logs/       {rep['logs'][0]} 个   {mb(rep['logs'][1])}\n\n"
                   f"队列/挂起里还在用的图会自动跳过，不会被删。\n"
                   f"已上传成功的图在飞书里，清掉本地副本不影响表格。")
        except Exception as e:
            self.showMsg_(f"统计缓存失败：{e}")
            return
        al = AppKit.NSAlert.alloc().init()
        al.setMessageText_("清理本地图片缓存")
        al.setInformativeText_(txt)
        al.addButtonWithTitle_("全部清理（临时图 + 未识别图）")
        al.addButtonWithTitle_("只清未识别图")
        al.addButtonWithTitle_("取消")
        r = al.runModal()
        try:
            import sync
            if r == AppKit.NSAlertFirstButtonReturn:
                n, freed, sk = sync.cleanup_cache(["tmp", "unmatched"], log=sync.log)
                self.showMsg_(f"已清理 {n} 个文件，释放 {freed / 1048576:.1f} MB"
                              + (f"（跳过 {sk} 个）" if sk else ""))
            elif r == AppKit.NSAlertSecondButtonReturn:
                n, freed, sk = sync.cleanup_cache(["unmatched"], log=sync.log)
                self.showMsg_(f"已清理未识别图 {n} 张，释放 {freed / 1048576:.1f} MB"
                              + (f"（跳过 {sk} 个）" if sk else ""))
        except Exception as e:
            self.showMsg_(f"清理失败：{e}")
        self.refresh_(None)

    def openRow_(self, sender):
        try:
            st = json.load(open(STATE))
            rid = st.get("current_record_id")
            import sync
            cfg = sync.load_cfg()
            f = cfg["feishu"]
            base = (cfg.get("ui") or {}).get("web_url") or \
                f"https://feishu.cn/base/{f['app_token']}"
            url = f"{base}?table={f['table_id']}" + (f"&record={rid}" if rid else "")
            subprocess.run(["open", url], capture_output=True)
        except Exception as e:
            self.showMsg_(f"打开失败：{e}")

    def openLog_(self, sender):
        subprocess.run(["open", os.path.join(ROOT, "logs", "sync.log")], capture_output=True)

    # ---------- 复制日志 ----------
    def copyLog_(self, sender):
        try:
            tv = self.logtv
            txt = tv.textStorage().string() if tv is not None else ""
            if not txt:
                txt = "".join(open(SYNC_LOG, errors="ignore").readlines()[-300:])
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(txt, AppKit.NSStringPboardType)
            # 复制动作本身会把日志塞进剪贴板：把它的指纹记进去重表，
            # 免得监听立刻把这段日志当成「店名」处理（比清空去重记忆精准）
            try:
                import hashlib
                fp = "t:" + hashlib.sha1(txt.strip().encode()).hexdigest()
                s = json.load(open(STATE))
                recent = s.get("recent") or []
                if fp not in recent:
                    recent.append(fp)
                s["recent"] = recent[-20:]
                json.dump(s, open(STATE, "w"), ensure_ascii=False, indent=1)
            except Exception:
                pass
            self.showMsg_(f"日志已复制（{len(txt)} 字符），可直接 ⌘V 贴给我或贴进备忘录")
        except Exception as e:
            self.showMsg_(f"复制失败：{e}")

    # ---------- 测试飞书连接（定位权限 / 凭据问题） ----------
    def testConn_(self, sender):
        def work():
            try:
                import sync
                import feishu
                cfg = sync.load_cfg()
                f = cfg["feishu"]
                out = []
                if f.get("app_secret"):
                    out.append("模式：官方 API（tenant_access_token）")
                    try:
                        feishu.Feishu(cfg, sync.log, ROOT).token()
                        out.append("① 取 token：✅ 通过（App ID / Secret 有效）")
                    except Exception as e:
                        out.append(f"① 取 token：❌ 失败\n{str(e)[:600]}")
                        self.performSelectorOnMainThread_withObject_waitUntilDone_(
                            "showMsg:", "\n".join(out), False)
                        sync.log("测试连接：取 token 失败 " + str(e)[:120])
                        return
                else:
                    out.append("模式：lark-cli 已授权身份（App Secret 留空）")
                try:
                    api = feishu.Feishu(cfg, sync.log, ROOT)
                    recs = api.list_records(limit=1)
                    out.append(f"② 读表格：✅ 通过（表 {f.get('table_id')}，"
                               f"读到 {len(recs) if isinstance(recs, list) else '?'} 条）")
                    out.append("结论：连接正常，可以开始采集。")
                except Exception as e:
                    out.append(f"② 读表格：❌ 失败\n{str(e)[:900]}")
                    out.append("\n（这就是上传时报错的原因，按上面「➜ 解决」步骤处理即可）")
                sync.log("测试连接：" + " / ".join(
                    l.split("：")[0] + l.split("：")[1][:12]
                    for l in out if "：" in l)[:300])
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "showMsg:", "\n".join(out), False)
            except Exception as e:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "showMsg:", f"测试连接异常：{e}", False)

        threading.Thread(target=work, daemon=True).start()
        try:
            import sync as _sy
            _sy.log("测试飞书连接中…（结果会弹窗，同时写在日志里）")
        except Exception:
            pass

    # ---------- 测试图片（验证第二屏等规则） ----------
    def doTest_(self, sender):
        p = AppKit.NSOpenPanel.openPanel()
        p.setCanChooseFiles_(True)
        p.setCanChooseDirectories_(False)
        p.setAllowedFileTypes_(["png", "jpg", "jpeg", "webp"])
        p.setMessage_("选一张截图，看它被判成第几屏（按顺序匹配，命中即停）")
        if p.runModal() != AppKit.NSOKButton:
            return
        path = p.URL().path()

        def work():
            try:
                import sync
                from ocr import OCR, normalize, match_screen
                cfg = sync.load_cfg()
                t = OCR(cfg.get("ocr", {}), ROOT, sync.log).text(path)
                n = normalize(t)
                flow = cfg.get("flow", {}) or {}
                fscreens = flow.get("screens") or []

                # 1) 每条规则的命中明细
                det = []
                for i, r in enumerate(cfg.get("rules", [])):
                    en = r.get("enabled", True)
                    mode = r.get("mode", "any")
                    kws = r.get("keywords") or []
                    hits, ds = [], []
                    for g in kws:
                        cs = g if isinstance(g, list) else [g]
                        hit = [c for c in cs if normalize(c) in n]
                        hits.append(bool(hit))
                        ds.append(f"{'/'.join(cs)}{'✅' if hit else '✗'}")
                    ok = all(hits) if mode == "all" else (any(hits) if kws else True)
                    tag = "兜底(前三屏都不命中才用它)" if r.get("fallback") else f"[{mode}]"
                    stt = "禁用" if not en else ("✅命中" if ok else "未命中")
                    det.append(f"{i+1}. {r['screen']} {tag} {stt}  " + " , ".join(ds))

                # 2) 用真正的判定函数模拟两种场景
                cases = [("本行第 1 张（第二屏/第三屏都还没有图）", {}, 0),
                         ("本行第 2 张起（已有图）", {s: 1 for s in fscreens}, len(fscreens))]
                sims, final = [], None
                for label, counts, fidx in cases:
                    sc = match_screen(n, cfg.get("rules", []),
                                      {"force": None, "flow": flow, "flow_index": fidx,
                                       "counts": counts})
                    targets = [sc] if sc else []
                    if (sc in fscreens and flow.get("enabled") and
                            flow.get("mode", "duplicate_first") == "duplicate_first" and fidx == 0):
                        targets = list(fscreens)
                    if final is None:
                        final = sc
                    sims.append(f"{label}\n   判定 {sc or '未识别'} -> 写入 "
                                f"{'+'.join(targets) or '（不写表，存 unmatched/）'}")
                msg = ("\n".join(det) + "\n\n" + "\n".join(sims)
                       + "\n\n最终判定：" + (final or "未识别（不会写入表格）")
                       + f"\n\nOCR 前 160 字：\n{n[:160]}")
            except Exception as e:
                msg = f"测试失败：{type(e).__name__}: {e}"
            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "showMsg:", msg, False)

        threading.Thread(target=work, daemon=True).start()

    # ---------- 刷新 ----------
    def pollTick_(self, sender):
        """主线程高频轮询剪贴板。NSPasteboard 只能在主线程读，
        所以这里不能用后台线程——之前漏了这个定时器，面板就完全不读剪贴板。"""
        if self.monitor is None:
            return None
        try:
            self.monitor.tick()
        except Exception:
            pass
        return None

    def refresh_(self, sender):
        try:
            os.utime(self.alive, None)        # 心跳：活着就刷新哨兵时间
        except Exception:
            pass
        running = self.monitor is not None
        self.status.setStringValue_("● 监听中（本窗口内）" if running else "○ 已停止")
        self.btn.setTitle_("⏸ 停止监听" if running else "▶ 开始监听")
        try:
            st = json.load(open(STATE))
        except Exception:
            st = {}
        name = st.get("current_name") or "(未锁定行)"
        s = st.get("stats", {})
        ops = st.get("ops", [])
        last = ops[-1] if ops else None
        if not last:
            lt = "上一步：无"
        elif last["kind"] == "row":
            lt = f"上一步：{'写入空白行' if last.get('mode')=='blank' else '新建行'}「{last.get('name') or '(无名)'}」"
        else:
            lt = f"上一步：上传 {last.get('screen')} 一张图"
        try:
            import clipboard
            cc = clipboard.change_count()
            beat = "·" if cc == self._last_cc_shown else "●"
            self._last_cc_shown = cc
            ccinfo = f"  |  剪贴板 #{cc} {beat}"
        except Exception:
            ccinfo = ""
        uniq = s.get("unique")
        uniq_s = f" / 唯一值 {uniq}" if uniq else ""
        self.detail.setStringValue_(
            f"当前行：{name}\n文本 {s.get('text',0)} / 图片 {s.get('images',0)} / "
            f"已传 {s.get('uploaded',0)} / 未识别 {s.get('unmatched',0)}{uniq_s}{ccinfo}")
        self.lastop.setStringValue_(lt)
        return None

    def autoScrollToggled_(self, sender):
        self._autoscroll = bool(sender.state())
        if self._autoscroll:
            scroll_log_bottom(self.logtv)
        return None

    def pumpLog_(self, sender):
        """把 logs/ops.jsonl（结构化）+ logs/sync.log（流水）的新行追加到日志区。"""
        try:
            tv = self.logtv
            if tv is None:
                return None
            added = False

            # 1) 结构化操作日志（ops.jsonl）
            lines, self._logpos = tail_new_lines(OPS_LOG, self._logpos)
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                tgt = str(e.get("target", "-"))
                if "/" in tgt:
                    tgt = tgt.split(" / ")[-1]      # 只显示 字段(fld..)
                res = e.get("result", "")
                mark = {"成功": "✔", "失败": "✘", "跳过": "⌀",
                        "未识别": "?", "排队": "…"}.get(res, "·")
                txt = (f"{e.get('ts','')} [{mark}] {e.get('type','')} {e.get('action','')} "
                       f"{e.get('screen') or ''} {tgt} {res} {e.get('ms',0)}ms"
                       + (f"  {e.get('reason','')}" if e.get("reason") else "")
                       + "\n")
                append_log_line(tv, txt, self.logcolors.get(res, self.logdefault),
                                self.logfont)
                added = True

            # 2) 运行流水（sync.log）：启动/停止/报错/跳过，保证任何动作都有回显
            dim = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.72, 0.76, 0.85, 1.0)
            slines, self._syncpos = tail_new_lines(SYNC_LOG, self._syncpos)
            for line in slines:
                s = line.strip()
                if not s:
                    continue
                if s.startswith("[") and "]" in s:
                    s = s.split("]", 1)[1].strip()
                col = dim
                # 识别不成功/失败/重复 一律红色（用户要求：未识别也要红）
                if ("ERROR" in s or "失败" in s or "未识别" in s
                        or "识别为空" in s or "重复" in s or "回退" in s):
                    col = self.logcolors["失败"]
                elif "开始监听" in s or "已上传" in s or "上传" in s:
                    col = self.logdefault
                append_log_line(tv, s + "\n", col, self.logfont)
                added = True

            if not added:
                return None
            # 只保留最近约 600 行，避免越滚越卡
            if tv.textStorage().length() > 60000:
                keep = tv.textStorage().attributedSubstringFromRange_(
                    NSMakeRange(tv.textStorage().length() - 40000, 40000))
                tv.textStorage().setAttributedString_(keep)
            if self._autoscroll:
                scroll_log_bottom(tv)
        except Exception:
            pass
        return None

    # ---------- 设置窗口 ----------
    def openSettings_(self, sender):
        if self.setwin:
            self.setwin.makeKeyAndOrderFront_(None)
            return
        # 打开设置时自动暂停监听：否则你复制的 Secret / App ID 会被当成「终端名称」写进表里
        self._resume_after_settings = self.monitor is not None
        if self._resume_after_settings:
            self.toggle_(None)
            try:
                import sync
                sync.log("打开设置，已自动暂停监听（避免 Secret/App ID 被当成店名写进表）；关掉设置会自动恢复")
            except Exception:
                pass
        ui = load_ui().get("settings") or [820, 160, 640, 700]
        x, y, w, h = ui
        style = (AppKit.NSTitledWindowMask | AppKit.NSClosableWindowMask |
                 AppKit.NSResizableWindowMask | AppKit.NSMiniaturizableWindowMask)
        sw = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h), style, AppKit.NSBackingStoreBuffered, False)
        sw.setTitle_("设置")
        sw.setLevel_(AppKit.NSFloatingWindowLevel)
        sw.setDelegate_(self)
        sw.setMinSize_(AppKit.NSMakeSize(520, 420))
        cv = sw.contentView()

        data, _ = load_cfg()
        rows = [
            ("飞书 App ID", "feishu.app_id"),
            ("App Secret（留空=用已授权身份）", "feishu.app_secret"),
            ("多维表格链接（粘贴自动解析）", "__link__"),
            ("App Token", "feishu.app_token"),
            ("表格网页地址（用于「打开当前行」）", "ui.web_url"),
            ("Sheet / 表 ID", "feishu.table_id"),
            ("字段-终端名称", "feishu.fields.终端名称"),
            ("字段-第一屏", "feishu.fields.第一屏"),
            ("字段-第二屏", "feishu.fields.第二屏"),
            ("字段-第三屏", "feishu.fields.第三屏"),
            ("字段-第四屏", "feishu.fields.第四屏"),
            ("字段-街道/区名", "feishu.fields.街道/区名"),
            ("字段-终端类型", "feishu.fields.终端类型"),
            ("新建行默认区名", "feishu.defaults.街道/区名"),
            ("新建行默认类型", "feishu.defaults.终端类型"),
            ("轮询间隔(ms)", "poll_interval_ms"),
            ("上传并发数", "perf.upload_workers"),
            ("第三屏最多几张", "limits.per_screen_max.第三屏"),
            ("文本最长字符（超过跳过）", "text.max_len"),
            ("空行检查字段（逗号分隔）", "text.blank_check_fields"),
        ]
        docw = w - 30
        rowh = 30
        doc_h = 40 + rowh * (len(rows) + 1) + 40 + rowh * 6 + 120

        sc = AppKit.NSScrollView.alloc().initWithFrame_(NSMakeRect(10, 52, w - 20, h - 62))
        sc.setHasVerticalScroller_(True)
        sc.setBorderType_(AppKit.NSNoBorder)
        sc.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        doc = AppKit.NSView.alloc().initWithFrame_(NSMakeRect(0, 0, docw, doc_h))
        sc.setDocumentView_(doc)
        cv.addSubview_(sc)

        yy = doc_h - 34
        self.fields = {}
        for label, path in rows:
            lab = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, yy, 250, 18))
            lab.setStringValue_(label)
            lab.setEditable_(False)
            lab.setBezeled_(False)
            lab.setDrawsBackground_(False)
            lab.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            doc.addSubview_(lab)

            if path == "text.row_selection":
                pass
            tf = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(262, yy - 2, 300, 22))
            if path == "__link__":
                tf.setPlaceholderString_("https://xxx.feishu.cn/base/xxxx?table=tblxxx")
            elif path == "text.blank_check_fields":
                v = getv(data, path)
                tf.setStringValue_(",".join(v) if isinstance(v, list)
                                   else "终端名称,第一屏,第二屏,第三屏,第四屏")
            elif path == "perf.upload_workers":
                tf.setStringValue_(str(getv(data, path) or 3))
                tf.setPlaceholderString_("3（同时传几张图）")
            elif path == "ui.web_url":
                tf.setStringValue_(str(getv(data, path) or ""))
                tf.setPlaceholderString_("留空则按 app_token 拼默认地址")
            else:
                v = getv(data, path)
                if isinstance(v, list):
                    v = ",".join(map(str, v))
                tf.setStringValue_("" if v is None else str(v))
                if "secret" in path.lower():
                    tf.setPlaceholderString_("留空 = 用已授权的 lark-cli 身份")
            tf.setTarget_(self)
            tf.setAction_("linkChanged:")
            doc.addSubview_(tf)
            self.fields[path] = tf
            yy -= rowh

        # 行选择策略
        lab = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, yy, 250, 18))
        lab.setStringValue_("文本写入方式（空行判断）")
        lab.setEditable_(False)
        lab.setBezeled_(False)
        lab.setDrawsBackground_(False)
        lab.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        doc.addSubview_(lab)
        selpop = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(262, yy - 3, 300, 24))
        selpop.addItemsWithTitles_(["自动：有空行就写空行，没有才新建",
                                    "总是新建一行",
                                    "只写空白行（没空行就不写）"])
        cur = str(getv(data, "text.row_selection") or "auto")
        selpop.selectItemAtIndex_({"auto": 0, "new": 1, "blank": 2}.get(cur, 0))
        doc.addSubview_(selpop)
        self.selpop = selpop
        yy -= rowh

        lab2 = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, yy, 250, 18))
        lab2.setStringValue_("第二屏 / 第三屏 区分方式")
        lab2.setEditable_(False)
        lab2.setBezeled_(False)
        lab2.setDrawsBackground_(False)
        lab2.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        doc.addSubview_(lab2)
        flowpop = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(262, yy - 3, 340, 24))
        flowpop.addItemsWithTitles_(["第一张同时写第二屏+第三屏（推荐）",
                                     "第一张只写第二屏，之后只写第三屏",
                                     "完全靠关键词（座区少时会串屏）"])
        fcur = str((data.get("flow") or {}).get("mode") or "duplicate_first")
        flowpop.selectItemAtIndex_({"duplicate_first": 0, "sequence": 1, "off": 2}.get(fcur, 0))
        doc.addSubview_(flowpop)
        self.flowpop = flowpop
        yy -= rowh + 8

        # 屏位规则
        head = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, yy, 400, 18))
        head.setStringValue_("屏位判定规则（按顺序匹配，命中即停；keywords 用 / 分隔同义、用 , 分组）")
        head.setEditable_(False)
        head.setBezeled_(False)
        head.setDrawsBackground_(False)
        head.setFont_(AppKit.NSFont.boldSystemFontOfSize_(11))
        doc.addSubview_(head)
        yy -= 26
        self.rules = []
        for r in (data.get("rules") or []):
            cb = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(6, yy - 2, 66, 22))
            cb.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb.setTitle_(r.get("screen", ""))
            cb.setState_(1 if r.get("enabled", True) else 0)
            cb.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            doc.addSubview_(cb)
            kw = r.get("keywords", [])
            parts = []
            for g in kw:
                parts.append("/".join(g) if isinstance(g, list) else str(g))
            tf = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(76, yy - 2, 218, 22))
            tf.setStringValue_(",".join(parts))
            tf.setPlaceholderString_("关键词（,分组 /同义）")
            tf.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            doc.addSubview_(tf)
            md = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(300, yy - 2, 52, 22))
            md.setStringValue_(r.get("mode", "any"))
            md.setPlaceholderString_("any/all")
            md.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            doc.addSubview_(md)
            ex = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(358, yy - 2, 200, 22))
            exv = r.get("exclude") or []
            ex.setStringValue_(",".join(exv) if isinstance(exv, list) else str(exv or ""))
            ex.setPlaceholderString_("排除词（出现则跳过本条）")
            ex.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            doc.addSubview_(ex)
            self.rules.append((r.get("screen"), cb, tf, md, ex))
            yy -= 28

        hint = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(6, yy - 60, docw - 20, 90))
        hint.setStringValue_(
            "说明：\n"
            "· 关键词用 , 分组；mode=all 要每组都出现，any 是任一组出现即可。\n"
            "· 第二屏默认关闭：它的特征（设备与服务 + 收起/总计）与第三屏容易重叠，\n"
            "  需要时勾上，并用「🔍 测试图片」确认它不会被第一/三屏抢先命中。\n"
            "· 上传慢的主要瓶颈：CLI 模式每张图要启动一次 node（约 0.5s）。\n"
            "  填上 App Secret 走官方 API 最快（约 0.3s/张），其次开大「上传并发数」。")
        hint.setEditable_(False)
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        hint.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        doc.addSubview_(hint)

        # 底部固定按钮（不随滚动）
        save = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(w - 210, 12, 100, 30))
        save.setTitle_("保存")
        save.setBezelStyle_(AppKit.NSBezelStyleRounded)
        save.setTarget_(self)
        save.setAction_("saveSettings:")
        save.setAutoresizingMask_(AppKit.NSViewMinXMargin)
        cv.addSubview_(save)

        fetch = AppKit.NSButton.alloc().initWithFrame_(NSMakeRect(w - 108, 12, 96, 30))
        fetch.setTitle_("拉取表列表")
        fetch.setBezelStyle_(AppKit.NSBezelStyleRounded)
        fetch.setTarget_(self)
        fetch.setAction_("fetchTables:")
        fetch.setAutoresizingMask_(AppKit.NSViewMinXMargin)
        cv.addSubview_(fetch)

        note = AppKit.NSTextField.alloc().initWithFrame_(NSMakeRect(12, 18, w - 230, 20))
        note.setStringValue_("保存后写回 config.yaml；若正在监听会自动重启监听。")
        note.setEditable_(False)
        note.setBezeled_(False)
        note.setDrawsBackground_(False)
        note.setFont_(AppKit.NSFont.systemFontOfSize_(10))
        note.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        cv.addSubview_(note)

        self.popup = selpop
        self.setwin = sw
        sw.makeKeyAndOrderFront_(None)

    def linkChanged_(self, sender):
        txt = str(sender.stringValue())
        m = re.search(r"/base/([A-Za-z0-9]+)", txt)
        m2 = re.search(r"[?&]table=([A-Za-z0-9]+)", txt)
        if m and "feishu.app_token" in self.fields:
            self.fields["feishu.app_token"].setStringValue_(m.group(1))
        if m2 and "feishu.table_id" in self.fields:
            self.fields["feishu.table_id"].setStringValue_(m2.group(1))
        if m and "ui.web_url" in self.fields:
            cur = str(self.fields["ui.web_url"].stringValue()).strip()
            if not cur:
                self.fields["ui.web_url"].setStringValue_(f"https://feishu.cn/base/{m.group(1)}")

    def fetchTables_(self, sender):
        token = str(self.fields["feishu.app_token"].stringValue()).strip()
        if not token:
            self.showMsg_("先填 App Token")
            return
        cli = "/Users/james/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli"
        p = subprocess.run([cli, "base", "+table-list", "--base-token", token,
                            "--as", "user"], capture_output=True, text=True, timeout=60)
        try:
            d = json.loads(p.stdout)
            rows = (d.get("data") or {}).get("items") or (d.get("data") or {}).get("tables") \
                   or (d.get("data") or {}).get("data") or []
        except Exception:
            rows = []
        if not rows:
            self.showMsg_("没取到表列表：\n" + (p.stdout or p.stderr)[:300])
            return
        lines = [f"{t.get('name') or ''} — {t.get('table_id') or t.get('id')}" for t in rows]
        self.showMsg_("表列表（把 table_id 填到「Sheet / 表 ID」）：\n" + "\n".join(lines))
        try:
            self.fields["feishu.table_id"].setStringValue_(str(rows[0].get("table_id") or
                                                               rows[0].get("id")))
        except Exception:
            pass

    def saveSettings_(self, sender):
        data, y = load_cfg()
        for path, tf in self.fields.items():
            if path == "__link__":
                continue
            raw = str(tf.stringValue()).strip()
            if path in ("poll_interval_ms", "text.max_len"):
                try:
                    raw = int(raw)
                except Exception:
                    continue
            elif path == "perf.upload_workers":
                try:
                    raw = max(1, min(8, int(raw or 3)))
                except Exception:
                    raw = 3
            elif path == "limits.per_screen_max.第三屏":
                try:
                    raw = int(raw)
                except Exception:
                    continue
            elif path == "text.blank_check_fields":
                raw = [x.strip() for x in raw.split(",") if x.strip()]
            elif path == "feishu.defaults.终端类型":
                raw = [x.strip() for x in raw.split(",") if x.strip()]
            setv(data, path, raw)
        idx = self.selpop.indexOfSelectedItem() if self.selpop else 0
        setv(data, "text.row_selection", ["auto", "new", "blank"][idx] if 0 <= idx < 3 else "auto")
        fidx = self.flowpop.indexOfSelectedItem() if self.flowpop else 0
        setv(data, "flow.mode",
             ["duplicate_first", "sequence", "off"][fidx] if 0 <= fidx < 3 else "duplicate_first")
        # 规则：启用状态 + 关键词 + mode
        for i, (screen, cb, tf, md, ex) in enumerate(self.rules):
            if i >= len(data.get("rules") or []):
                break
            data["rules"][i]["enabled"] = bool(cb.state())
            groups = []
            for g in str(tf.stringValue()).split(","):
                g = g.strip()
                if not g:
                    continue
                items = [x.strip() for x in g.split("/") if x.strip()]
                groups.append(items if len(items) > 1 else items[0])
            data["rules"][i]["keywords"] = groups
            m = str(md.stringValue()).strip().lower()
            data["rules"][i]["mode"] = "all" if m == "all" else "any"
            exl = [x.strip() for x in str(ex.stringValue()).split(",") if x.strip()]
            data["rules"][i]["exclude"] = exl
        save_cfg(data, y)
        if self.monitor is not None:      # 重启监听以加载新配置
            self.toggle_(None)
            self.toggle_(None)
        self.showMsg_("已保存" + ("，监听已按新配置重启" if self.monitor is not None else ""))
        self.refresh_(None)

    # ---------- 窗口几何记忆 / 关闭 ----------
    def windowDidMove_(self, notification):
        self.persist_ui()

    def windowDidResize_(self, notification):
        self.persist_ui()

    def persist_ui(self):
        d = load_ui()
        if self.win:
            d["main"] = frame_of(self.win)
        if self.setwin:
            d["settings"] = frame_of(self.setwin)
        save_ui(d)

    def windowWillClose_(self, notification):
        w = notification.object()
        if w == self.setwin:
            self.setwin = None
            self.persist_ui()
            if getattr(self, "_resume_after_settings", False):
                self._resume_after_settings = False
                try:
                    import sync
                    sync.log("设置窗口关闭，自动恢复监听")
                except Exception:
                    pass
                self.toggle_(None)
            return
        # 主窗口关闭：彻底停止监听并退出进程，不留后台常驻
        self.cleanup()
        AppKit.NSApp().terminate_(None)

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def cleanup(self):
        try:
            if self.alive and os.path.exists(self.alive):
                os.remove(self.alive)         # 正常退出：删掉哨兵
        except Exception:
            pass
        try:
            if self.monitor is not None:
                self.monitor.shutdown()
                self.monitor = None
                import sync
                sync.log("窗口关闭，监听已停止（进程即将退出）")
        except Exception:
            pass
        try:
            if service_running():
                stop_service()      # 清理历史遗留的常驻服务，确保无残留进程
        except Exception:
            pass
        try:
            for f in ("sync.pid",):
                p = os.path.join(ROOT, f)
                if os.path.exists(p):
                    os.remove(p)
        except Exception:
            pass
        self.persist_ui()

    # ---------- 工具 ----------
    def showMsg_(self, msg):
        a = AppKit.NSAlert.alloc().init()
        a.setMessageText_(str(msg)[:1200])
        a.runModal()


def build_main_menu():
    """自建主菜单：没有它就没有「编辑」菜单，⌘C/⌘V/⌘A 全部失效
    （这就是之前日志复制不了、Secret 只能一个字母手打的根因）。"""
    app = AppKit.NSApp()
    bar = AppKit.NSMenu.alloc().init()
    app.setMainMenu_(bar)

    def item(title, action, key):
        it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
        return it

    # 应用菜单（退出）
    m_app = AppKit.NSMenu.alloc().initWithTitle_("剪贴板采集")
    it = item("关于 剪贴板采集", "orderFrontStandardAboutPanel:", "")
    m_app.addItem_(it)
    m_app.addItem_(AppKit.NSMenuItem.separatorItem())
    m_app.addItem_(item("隐藏", "hide:", "h"))
    m_app.addItem_(item("退出 剪贴板采集", "terminate:", "q"))
    top = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("剪贴板采集", None, "")
    top.setSubmenu_(m_app)
    bar.addItem_(top)

    # 编辑菜单：复制/粘贴/全选/撤销 —— target 为 nil，走响应链找当前输入框/日志区
    m_edit = AppKit.NSMenu.alloc().initWithTitle_("编辑")
    for t, a, k in (("撤销", "undo:", "z"), ("重做", "redo:", "Z"),
                    ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                    ("粘贴", "paste:", "v"), ("粘贴为纯文本", "pasteAsPlainText:", "V"),
                    ("删除", "delete:", ""), ("全选", "selectAll:", "a")):
        m_edit.addItem_(item(t, a, k))
    top = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("编辑", None, "")
    top.setSubmenu_(m_edit)
    bar.addItem_(top)

    # 窗口菜单（把面板叫回来）
    m_win = AppKit.NSMenu.alloc().initWithTitle_("窗口")
    m_win.addItem_(item("前置全部窗口", "arrangeInFront:", ""))
    top = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("窗口", None, "")
    top.setSubmenu_(m_win)
    bar.addItem_(top)


def install_crash_log():
    """崩溃取证：SIGSEGV/SIGABRT 等致命信号时把 Python 堆栈写进 logs/crash.log。

    之前崩溃一次连 .ips 都没落盘，只能靠猜。现在无论怎么死，都会留下：
      · logs/crash.log  —— 致命信号的 Python 堆栈（faulthandler）
      · logs/panel.log  —— 未捕获异常（excepthook）
    """
    try:
        import faulthandler
        f = open(os.path.join(ROOT, "logs", "crash.log"), "a", buffering=1)
        f.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} =====\n")
        faulthandler.enable(file=f, all_threads=True)
        for sig in ("SIGSEGV", "SIGABRT", "SIGBUS", "SIGILL", "SIGFPE"):
            try:
                import signal as _sig
                faulthandler.register(getattr(_sig, sig), file=f, all_threads=True, chain=True)
            except Exception:
                pass
    except Exception:
        pass

    def _hook(etype, val, tb):
        try:
            import traceback
            with open(os.path.join(ROOT, "logs", "crash.log"), "a") as f:
                f.write(f"\n----- 未捕获异常 {time.strftime('%H:%M:%S')} -----\n")
                traceback.print_exception(etype, val, tb, file=f)
        except Exception:
            pass
        sys.__excepthook__(etype, val, tb)

    sys.excepthook = _hook
    try:
        threading.excepthook = lambda a: _hook(a.exc_type, a.exc_value, a.exc_traceback)
    except Exception:
        pass


def main():
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    # 关键：所有会 import AppKit 的模块都在主线程先导入一遍。
    # 若第一次 import 发生在工作线程里，NSPasteboard 之类的对象就会在非主线程创建，
    # 这是 PyObjC 程序随机崩溃（SIGSEGV）的典型来源。
    import clipboard  # noqa: F401
    import sync  # noqa: F401
    install_crash_log()
    build_main_menu()
    c = Controller.alloc().init()
    c.build()
    c.start_if_enabled()
    app.activateIgnoringOtherApps_(True)
    # 被 kill（非崩溃）时也走正常清理：删掉哨兵、停掉监听
    import signal

    def _sig(_s, _f):
        try:
            c.cleanup()
        except Exception:
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    app.run()


if __name__ == "__main__":
    main()
