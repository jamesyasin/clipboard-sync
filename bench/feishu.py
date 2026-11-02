"""飞书多维表格读写：支持 app_id/app_secret（官方 API）与 lark-cli 两种模式。

本次改动（性能 & 行选择）：
  * 所有 HTTP 走同一个 requests.Session（连接复用 / keep-alive），不再每次新建连接
  * 新增 list_records()：CLI 走 +record-list --json，API 走官方列表接口
  * 新增 find_blank_row()：一次请求筛出「终端名称为空」的候选行，再逐条确认其它字段也为空，
    返回第一条真正没有数据的行（用于「写入空白行」而不是永远追加到末尾）
  * 新增 CLI 模式的 _cli_update（）：+record-batch-update，写入空白行必须用到
  * append_attachment(fetch_token=False)：CLI 模式默认不再为拿 file_token 多起一次 node 进程
    （file_token 由后台线程补，或撤回时现查），单张上传省掉约 0.5s
  * optimize_image()：大图可选压缩（>min_bytes 才动），减小上传体积
"""
import json
import os
import shutil
import subprocess
import time

import requests

from spawn import spawn_capture

BASE = "https://open.feishu.cn/open-apis"

# launchd / 后台环境下 PATH 常常不含 node，这里补齐，保证 lark-cli 能跑
for _d in ("/opt/homebrew/bin", "/usr/local/bin",
           "/Users/james/.workbuddy/binaries/node/versions/current/bin",
           "/Users/james/.workbuddy/binaries/node/versions/22.22.2-6/bin"):
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


class FeishuError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


SCOPE_FIX = (
    "\n➜ 这是「应用身份权限」没开通或没生效，不是程序 bug。按下面 3 步处理：\n"
    "  1) 打开 https://open.feishu.cn/app 找到这个应用 →「权限管理」，开通：\n"
    "     · 多维表格 bitable:app（含 bitable:app:readonly、bitable:app:base:record:retrieve、\n"
    "       bitable:app:base:record:create/update）\n"
    "     · 云文档/素材 drive:file（上传截图用）\n"
    "  2)「版本管理与发布」→ 创建版本 → 申请发布（企业自建应用可自助发布）。\n"
    "     权限改完必须发版本才生效，这是最常见的漏点。\n"
    "  3) 打开目标多维表格 → 右上角「···」→「添加文档应用/协作者」，把这个应用加为「可编辑」。\n"
    "  4) 回到面板「设置」点保存，再点「🔌 测试飞书连接」直到显示通过。\n"
    "  ⚠ 不想折腾：把 App Secret 清空保存 → 自动改用已授权的 lark-cli 身份（约 1.2s/张，无需权限）。"
)


def friendly(what: str, d) -> str:
    """把飞书 API 的错误码翻译成人话 + 可执行的处理步骤。"""
    code = None
    msg = ""
    try:
        if isinstance(d, dict):
            code = d.get("code")
            msg = str(d.get("msg") or d.get("message") or "")
    except Exception:
        pass
    if not msg:
        msg = str(d)[:300]
    tip = ""
    m = (msg or "").lower()
    if code == 1254045:
        tip = ("\n➜ 这个应用写表不认字段 ID（fldXXXXXX），只认中文列名。"
               "程序已自动换成列名重试；若还报错，到「设置」把字段映射直接填中文列名。")
    elif code in (99991672, 99991663, 99991668) or "scopes is required" in m \
            or "尚未开通" in msg or "access denied" in m:
        tip = SCOPE_FIX
        import re as _re
        link = _re.search(r"https://open\.feishu\.cn/app/\S+", msg)
        if link:
            tip += "\n🔗 一键开通链接（点开按需勾选后提交）：\n   " + link.group(0).rstrip("（(")
    elif code in (99991663, 10003, 20005) or "app_secret" in m or "invalid app" in m:
        tip = ("\n➜ App ID 与 App Secret 不匹配或手打错了一位。"
               "建议：在开放平台点「复制」按钮，然后 ⌘V 直接粘进面板设置（面板现已支持粘贴）。")
    elif code in (1254005, 1254006, 1254000) or "not found" in m:
        tip = ("\n➜ 表格/app_token 不对，或该应用没被加进这张多维表格。"
               "在多维表格右上角「···」→ 添加文档应用，把应用加为可编辑协作者。")
    elif code == 1254062:
        tip = ("\n➜ 单选字段只接受文本，配置里写成了列表（如「终端类型: - 网吧」）。"
               "程序会自动取第一项；也可到「设置」把默认值直接写成 网吧。")
    elif "field" in m and ("not exist" in m or "invalid" in m):
        tip = "\n➜ 字段 ID 不对（列被删/改名过）。到「设置」重新填 fldXXXXXX（用「测试连接」验证）。"
    return f"{what}：{msg}（code={code}）{tip}"


def jout(out):
    """lark-cli 输出常常在 JSON 前带一行进度提示，这里把 JSON 部分抠出来。"""
    if not out:
        return {}
    i = out.find("{")
    if i < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(out[i:])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def is_blank(v):
    """单元格是否为空：None / 空串 / 空列表 / 空字典 都算空。"""
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def optimize_image(src, cfg, log=None):
    """按配置压缩大图加快上传。返回 (路径, 是否临时文件)。失败时原样返回。"""
    try:
        if not cfg or not cfg.get("enabled"):
            return src, False
        min_bytes = int(cfg.get("min_bytes", 900 * 1024))
        if os.path.getsize(src) < min_bytes:
            return src, False
        max_px = int(cfg.get("max_pixel", 1600))
        quality = int(cfg.get("jpeg_quality", 85))
        from PIL import Image
        im = Image.open(src).convert("RGB")
        if max(im.size) > max_px:
            r = max_px / float(max(im.size))
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                           Image.LANCZOS)
        out = os.path.splitext(src)[0] + "_opt.jpg"
        im.save(out, "JPEG", quality=quality, optimize=True)
        if log:
            log(f"压缩 {os.path.basename(src)} -> {os.path.basename(out)} "
                f"({os.path.getsize(src)//1024}KB -> {os.path.getsize(out)//1024}KB)")
        return out, True
    except Exception:
        return src, False


class Feishu:
    def __init__(self, cfg, logger, root):
        self.cfg = cfg
        self.log = logger
        self.root = root
        f = cfg["feishu"]
        self.app_token = f["app_token"]
        self.table_id = f["table_id"]
        self.fmap = f.get("fields", {}) or {}
        self.app_id = f.get("app_id") or ""
        self.app_secret = f.get("app_secret") or ""
        self.cli = f.get("lark_cli") or ""
        self.cli_as = f.get("lark_cli_as") or "user"
        self.mode = "api" if (self.app_id and self.app_secret) else "cli"
        self._token = None
        self._token_exp = 0
        self._cache = os.path.join(root, ".cache", "token.json")
        self._namecache = os.path.join(root, ".cache", "field_mode.json")
        self._id2name = None
        self._ftypes = None
        # 读取 .cache：上次已经发现「只能用列名写入」，这次直接走列名，省掉第一次失败
        self.use_names = False
        try:
            nm = json.load(open(self._namecache))
            if nm.get("key") == self._cachekey():
                self.use_names = bool(nm.get("use_names"))
        except Exception:
            pass
        self._acache = {}          # (record_id, field) -> [file_token]，省掉每次上传前的读记录
        # 连接复用：一个进程内只建一次连接池（keep-alive）
        self.session = requests.Session()
        try:
            from requests.adapters import HTTPAdapter
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
            self.session.mount("https://", ad)
            self.session.mount("http://", ad)
        except Exception:
            pass

    def _cachekey(self):
        return f"{self.app_id}|{self.app_token}|{self.table_id}"

    # ---------- 鉴权 ----------
    def _load_cache(self):
        try:
            d = json.load(open(self._cache))
            if d.get("exp", 0) > time.time() + 60 and d.get("app_id") == self.app_id:
                self._token, self._token_exp = d["token"], d["exp"]
                return True
        except Exception:
            pass
        return False

    def token(self):
        if self._token and self._token_exp > time.time() + 60:
            return self._token
        if self._load_cache():
            return self._token
        r = self.session.post(
            f"{BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=15,
        )
        d = r.json()
        if d.get("code") != 0 or d.get("tenant_access_token") is None:
            raise FeishuError(friendly("取 token", d), (d or {}).get("code") if isinstance(d, dict) else None)
        self._token = d["tenant_access_token"]
        self._token_exp = time.time() + d.get("expire", 7200)
        try:
            os.makedirs(os.path.dirname(self._cache), exist_ok=True)
            json.dump({"token": self._token, "exp": self._token_exp, "app_id": self.app_id},
                      open(self._cache, "w"))
            os.chmod(self._cache, 0o600)
        except Exception:
            pass
        return self._token

    def _h(self):
        return {"Authorization": "Bearer " + self.token(),
                "Content-Type": "application/json; charset=utf-8"}

    # ---------- 字段 ID / 名称 ----------
    # 有些应用（尤其是新建的自建应用）用 tenant_access_token 写表时，
    # fields 只认「列名」，传 fldXXXXXX 会报 1254045 FieldNameNotFound。
    # 这里：默认按配置原样写；一旦撞到 1254045，就查一次字段表换成列名重试，并记进缓存。
    def _name_map(self):
        if self._id2name is not None:
            return self._id2name
        m = {}
        types = {}
        try:
            r = self.session.get(
                f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/fields",
                headers=self._h(), params={"page_size": 100}, timeout=20)
            d = r.json()
            for it in (d.get("data") or {}).get("items") or []:
                if it.get("field_id"):
                    m[it["field_id"]] = it.get("field_name") or it["field_id"]
                    types[it["field_id"]] = it.get("type")
                    if it.get("field_name"):
                        types[it["field_name"]] = it.get("type")
        except Exception as e:
            if self.log:
                self.log(f"读取字段列表失败（无法改用列名写入）: {e}")
        self._id2name = m
        self._ftypes = types
        return m

    def _out_fields(self, fields):
        """写表前：①必要时把 fldXXXXXX 换成中文列名；②按字段类型纠正值（单选/多选）。"""
        if self._id2name is None:      # 首次写入前先取一次字段表（列名 + 类型），之后缓存
            self._name_map()
        out = {}
        for k, v in fields.items():
            key = self._id2name.get(k, k) if (self.use_names and self._id2name) else k
            t = (self._ftypes or {}).get(k)
            if v is not None:
                # 单选（type 3）：只收字符串，配置里写成了列表也帮忙纠正
                if t == 3 and isinstance(v, (list, tuple)):
                    v = v[0] if len(v) == 1 else (v[0] if v else None)
                # 多选（type 4）：只收数组
                elif t == 4 and isinstance(v, str):
                    v = [v]
            out[key] = v
        return out

    def _norm_in(self, fields):
        """读回来的 fields 的 key 可能是中文列名（本应用就是），统一翻译成配置里的 fldXXXXXX。"""
        if not isinstance(fields, dict):
            return fields
        if self._id2name is None:      # 需要「列名 -> 字段ID」表（列名可能与配置里的简称不同）
            try:
                self._name_map()
            except Exception:
                pass
        rev = {v: k for k, v in (self.fmap or {}).items()}       # 配置里的中文名 -> ID
        for fid, nm in (self._id2name or {}).items():            # 线上真实列名 -> ID
            rev.setdefault(nm, fid)
        return {rev.get(k, k): v for k, v in fields.items()}

    def _retry_with_names(self, err):
        """遇到 1254045 -> 切到列名模式（只切一次，避免死循环）。"""
        if getattr(err, "code", None) != 1254045 or self.use_names:
            return False
        self.use_names = True
        try:
            os.makedirs(os.path.dirname(self._namecache), exist_ok=True)
            json.dump({"key": self._cachekey(), "use_names": True},
                      open(self._namecache, "w"))
        except Exception:
            pass
        if self.log:
            self.log("提示：该应用写表不认字段 ID（1254045），已自动切换为「按列名写入」，后续照常运行")
        return True

    # ---------- 记录 ----------
    def create_record(self, fields):
        if self.mode == "cli":
            return self._cli_create(fields)
        try:
            return self._create_once(fields)
        except FeishuError as e:
            if self._retry_with_names(e):
                return self._create_once(fields)
            raise

    def _create_once(self, fields):
        r = self.session.post(
            f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records",
            headers=self._h(), json={"fields": self._out_fields(fields)}, timeout=20,
        )
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(friendly("新建行", d), (d or {}).get("code") if isinstance(d, dict) else None)
        return d["data"]["record"]["record_id"]

    def get_record(self, record_id, field_ids=None):
        if self.mode == "cli":
            return self._cli_get(record_id, field_ids)
        params = {}
        if field_ids:
            params["field_ids"] = json.dumps(list(field_ids), ensure_ascii=False)
        r = self.session.get(
            f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/{record_id}",
            headers=self._h(), params=params, timeout=20,
        )
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(friendly("读记录", d), (d or {}).get("code") if isinstance(d, dict) else None)
        return self._norm_in(d["data"]["record"]["fields"])

    def update_record(self, record_id, fields):
        if self.mode == "cli":
            return self._cli_update(record_id, fields)
        try:
            return self._update_once(record_id, fields)
        except FeishuError as e:
            if self._retry_with_names(e):
                return self._update_once(record_id, fields)
            raise

    def _update_once(self, record_id, fields):
        r = self.session.put(
            f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/{record_id}",
            headers=self._h(), json={"fields": self._out_fields(fields)}, timeout=20,
        )
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(friendly("更新记录", d), (d or {}).get("code") if isinstance(d, dict) else None)
        return True

    # ---------- 列表 / 空白行 ----------
    def list_records(self, field_ids=None, filter_blank_field=None, limit=50):
        """列出记录，返回 [{'record_id':..., 'fields':{fid:value}}, ...]

        filter_blank_field 传中文列名（如「终端名称」）时，只返回该列为空的记录 —— 服务端过滤，
        一次请求即可拿到候选空行，比翻全表快得多。
        """
        if self.mode == "cli":
            return self._cli_list(field_ids, filter_blank_field, limit)
        out = []
        page_token = None
        params = {"page_size": min(max(limit, 1), 100)}
        if field_ids:
            params["field_ids"] = json.dumps(list(field_ids), ensure_ascii=False)
        if filter_blank_field:
            params["filter"] = f'AND(CurrentValue.[{filter_blank_field}]="")'
        while len(out) < limit:
            p = dict(params)
            if page_token:
                p["page_token"] = page_token
            r = self.session.get(
                f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records",
                headers=self._h(), params=p, timeout=20)
            d = r.json()
            if d.get("code") != 0:
                raise FeishuError(friendly("列记录", d), (d or {}).get("code") if isinstance(d, dict) else None)
            data = d.get("data") or {}
            for rec in data.get("items") or data.get("records") or []:
                out.append({"record_id": rec.get("record_id"),
                            "fields": self._norm_in(rec.get("fields") or {})})
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        return out[:limit]

    def find_blank_row(self, check_labels, limit=50):
        """找第一条「真正没有数据」的行。

        check_labels: 中文列名列表（如 ["终端名称","第一屏","第二屏","第三屏","第四屏"]）
        返回 (record_id, fields)；没有空行返回 (None, None)。
        策略：先用「终端名称为空」做服务端筛选拿候选，再确认其余字段也全空。
        """
        if not check_labels:
            return None, None
        name_label = check_labels[0]
        ids = [self.fmap.get(k, k) for k in check_labels]
        try:
            rows = self.list_records(field_ids=ids, filter_blank_field=name_label, limit=limit)
        except Exception as e:
            if self.log:
                self.log(f"空白行筛选失败，回退全表扫描: {e}")
            rows = self.list_records(field_ids=ids, limit=limit)
        # 未做服务端过滤时（回退）按原顺序找第一个全空
        for r in rows:
            f = r.get("fields") or {}
            if all(is_blank(f.get(fid)) for fid in ids):
                return r.get("record_id"), f
        return None, None

    # ---------- 素材 ----------
    def upload_media(self, path, name=None):
        name = name or os.path.basename(path)
        if self.mode == "cli":
            raise FeishuError("CLI 模式请直接用 append_attachment")
        url = f"{BASE}/drive/v1/medias/upload_all"
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            files = {
                "file": (name, f, "application/octet-stream"),
                "file_name": (None, name),
                "parent_type": (None, "bitable_image"),
                "parent_node": (None, self.app_token),
                "size": (None, str(size)),
            }
            r = self.session.post(url, headers={"Authorization": "Bearer " + self.token()},
                                  files=files, timeout=60)
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(friendly("上传素材", d), (d or {}).get("code") if isinstance(d, dict) else None)
        return d["data"]["file_token"]

    def append_attachment(self, record_id, field, path, name=None, fetch_token=True):
        """上传并把附件追加到指定单元格（保留已有附件），返回本次的 file_token（可为 None）。

        CLI 模式的 file_token 直接从上传命令的返回里解析（原先要再跑一次 record-get，
        多一次 node 冷启动 ≈ 1.1s，现已去掉）。
        """
        name = name or os.path.basename(path)
        tmp_opt = None
        try:
            src, is_tmp = optimize_image(path, (self.cfg.get("optimize") or {}), self.log)
            if is_tmp:
                tmp_opt = src
            if self.mode == "cli":
                self._cli_upload(record_id, field, src, name)
                if not fetch_token:
                    return None
                items = self.get_record(record_id, [field]).get(field) or []
                return items[-1].get("file_token") if items else None
            tok = self.upload_media(src, name)
            key = (record_id, field)
            toks = self._acache.get(key)
            if toks is None:
                cur = self.get_record(record_id, [field]).get(field) or []
                toks = [a.get("file_token") for a in cur if a.get("file_token")]
            new = list(toks) + [tok]
            try:
                self.update_record(record_id, {field: [{"file_token": t} for t in new]})
            except FeishuError:      # 缓存与线上不一致 -> 读一次再写
                cur = self.get_record(record_id, [field]).get(field) or []
                toks = [a.get("file_token") for a in cur if a.get("file_token")]
                new = list(toks) + [tok]
                self.update_record(record_id, {field: [{"file_token": t} for t in new]})
            self._acache[key] = new
            return tok
        finally:
            if tmp_opt and os.path.exists(tmp_opt):
                try:
                    os.remove(tmp_opt)
                except Exception:
                    pass

    def last_file_token(self, record_id, field):
        """现查某单元格最后一张附件的 file_token（撤回兜底用）。"""
        try:
            items = self.get_record(record_id, [field]).get(field) or []
            return items[-1].get("file_token") if items else None
        except Exception:
            return None

    def count_attachment(self, record_id, field):
        v = self.get_record(record_id, [field]).get(field) or []
        return len([a for a in v if a.get("file_token")])

    # ---------- 撤回用 ----------
    def delete_record(self, record_id):
        if self.mode == "cli":
            self._cli(["base", "+record-delete", "--base-token", self.app_token,
                       "--table-id", self.table_id, "--record-id", record_id, "--yes"])
            return True
        r = self.session.delete(
            f"{BASE}/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records/{record_id}",
            headers=self._h(), timeout=20)
        d = r.json()
        if d.get("code") != 0:
            raise FeishuError(friendly("删除记录", d), (d or {}).get("code") if isinstance(d, dict) else None)
        return True

    def remove_attachment(self, record_id, field, file_token):
        """只删单元格里指定的那一张，其余保留。"""
        if not file_token:   # 异步补 token 还没回来 -> 现查最后一个
            file_token = self.last_file_token(record_id, field)
        if not file_token:
            raise FeishuError("拿不到 file_token，无法精确删除该张图")
        if self.mode == "cli":
            self._cli(["base", "+record-remove-attachment", "--base-token", self.app_token,
                       "--table-id", self.table_id, "--record-id", record_id,
                       "--field-id", field, "--file-token", file_token, "--yes"])
            self._acache.pop((record_id, field), None)
            return True
        cur = self.get_record(record_id, [field]).get(field) or []
        items = [{"file_token": a.get("file_token")} for a in cur
                 if a.get("file_token") and a.get("file_token") != file_token]
        r = self.update_record(record_id, {field: items})
        self._acache.pop((record_id, field), None)
        return r

    # ---------- lark-cli 兜底 ----------
    def _cli(self, args):
        cmd = [self.cli] + args + ["--as", self.cli_as]
        cwd = os.path.join(self.root, "tmp")
        os.makedirs(cwd, exist_ok=True)
        rc, out, err = spawn_capture(cmd, timeout=120, cwd=cwd)
        if rc != 0 and "code" not in out:
            raise FeishuError(f"lark-cli 失败: {out[:300]} {err[:200]}")
        return out

    def _cli_create(self, fields):
        out = self._cli(["base", "+record-batch-create",
                         "--base-token", self.app_token, "--table-id", self.table_id,
                         "--json", json.dumps({"create_records": [{"fields": fields}]},
                                              ensure_ascii=False)])
        try:
            d = jout(out)
            data = d.get("data") or {}
            ids = data.get("record_id_list") or data.get("record_ids") or []
            if ids:
                return ids[0] if isinstance(ids[0], str) else ids[0].get("record_id")
            recs = data.get("records") or data.get("data") or []
            if recs:
                return recs[0].get("record_id") if isinstance(recs[0], dict) else recs[0]
            raise FeishuError(f"lark-cli 未返回 record_id: {out[:200]}")
        except Exception as e:
            raise FeishuError(f"解析 lark-cli 建记录返回失败: {out[:300]} {e}")

    def _cli_update(self, record_id, fields):
        """CLI 模式写字段（写入空白行需要）。"""
        payload = {"update_records": {record_id: fields}}
        out = self._cli(["base", "+record-batch-update",
                         "--base-token", self.app_token, "--table-id", self.table_id,
                         "--json", json.dumps(payload, ensure_ascii=False)])
        try:
            d = jout(out)
            if d and d.get("ok") is False:
                raise FeishuError(f"lark-cli 更新失败: {str(d)[:200]}")
        except FeishuError:
            raise
        except Exception:
            pass
        return True

    def _cli_upload(self, record_id, field, path, name=None):
        src = path
        if name:
            d = os.path.join(self.root, "tmp", "named")
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, name)
            shutil.copy(path, dst)
            src = dst
        out = self._cli(["base", "+record-upload-attachment",
                         "--base-token", self.app_token, "--table-id", self.table_id,
                         "--record-id", record_id, "--field-id", field, "--file", src])
        # 返回里直接带了本次的 file_token，省掉一次 record-get 反查
        try:
            arr = (((jout(out).get("data") or {}).get("attachments") or {})
                   .get(record_id) or {}).get(field) or []
            if arr:
                return arr[-1].get("file_token")
        except Exception:
            pass
        return None

    def _cli_list(self, field_ids, filter_blank_field=None, limit=50):
        """+record-list --json，返回 [{'record_id','fields'}]"""
        args = ["base", "+record-list", "--base-token", self.app_token,
                "--table-id", self.table_id, "--limit", str(min(max(limit, 1), 200))]
        for f in (field_ids or []):
            args += ["--field-id", f]
        if filter_blank_field:
            args += ["--filter-json",
                     json.dumps({"logic": "and",
                                 "conditions": [[filter_blank_field, "isEmpty", ""]]},
                                ensure_ascii=False)]
        args += ["--format", "json"]
        out = self._cli(args)
        try:
            dd = jout(out).get("data") or {}
            ids = dd.get("field_id_list") or []
            rows = dd.get("data") or []
            rids = dd.get("record_id_list") or []
            res = []
            for i, row in enumerate(rows):
                rid = rids[i] if i < len(rids) else None
                res.append({"record_id": rid,
                            "fields": {fid: (row[j] if j < len(row) else None)
                                       for j, fid in enumerate(ids)}})
            return res
        except Exception:
            return []

    def _cli_get(self, record_id, field_ids=None):
        """用 --format json 读记录，返回 {field_id: value}"""
        args = ["base", "+record-get", "--base-token", self.app_token,
                "--table-id", self.table_id, "--record-id", record_id]
        for f in (field_ids or []):
            args += ["--field-id", f]
        args += ["--format", "json"]
        out = self._cli(args)
        try:
            dd = jout(out).get("data") or {}
            ids = dd.get("field_id_list") or []
            rows = dd.get("data") or []
            rids = dd.get("record_id_list") or []
            if not rows:
                return {}
            # 多 record-id 时取第一个；单条时就是它
            row = rows[0]
            res = {fid: (row[i] if i < len(row) else None) for i, fid in enumerate(ids)}
            if rids:
                res["__record_id__"] = rids[0]
            return res
        except Exception:
            return {}
