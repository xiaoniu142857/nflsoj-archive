#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hydrooj - Hydro OJ 命令行工具（NFLSOJ Hydro 版）

和 nflsoj 完全相同的前端与命令集，但对接基于 Hydro（v4.x）搭建的 OJ：

    http://nflsoi.cc:10611

与 nflsoj 版本不同的是，Hydro 提供了 JSON 视图（`Accept: application/json`），
题目正文直接就是 Markdown 原文，因此本工具不需要解析 HTML 页面。

用法示例：
    hydrooj login                  # 登录（用户名密码 / 粘贴 Cookie）
    hydrooj sync                   # 同步比赛与题目
    hydrooj contests               # 比赛列表
    hydrooj problems <比赛ID>       # 题目列表
    hydrooj mark <比赛ID> 1 2 3     # 标记为已完成（默认 done）
    hydrooj archive --all          # 比赛首页 + 题面 → 本地 Markdown

数据目录默认是 ~/.hydrooj，可用 --data-dir 或环境变量 HYDROOJ_DATA_DIR 更改。
"""

from __future__ import annotations

import argparse
import csv
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

_HERE = os.path.dirname(os.path.abspath(__file__))
try:                                    # 作为包的一部分导入（uv run hydrooj / python -m hydrooj_archive）
    from nflsoj_archive import nflsoj as core
except ImportError:                     # 直接 python src/hydrooj_archive/hydrooj.py
    sys.path.insert(0, os.path.dirname(_HERE))
    from nflsoj_archive import nflsoj as core

__version__ = "2.1.0"

DEFAULT_API = "http://nflsoi.cc:10611"
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".hydrooj")
DATA_DIR_ENV = "HYDROOJ_DATA_DIR"
PAGE_SIZE = 20                  # Hydro /contest 每页 20 场
STATEMENT_CACHE_TTL = 30 * 86400
DEFAULT_WORKERS = 6             # Hydro 站点有随机网关拦截，并发不宜过高
USER_AGENT = "hydrooj-cli/%s" % __version__

# 站点网关（WAF）随机拦截的特征串
BLOCK_MARKERS = ("Web Access blocked", "Blocked Type", "Web Filtering")

WRITE_HINT = ("该位置不可写：可能位于只读目录、被其他程序占用，或当前终端运行在受限沙箱中。"
              "可换一个可写目录：hydrooj --data-dir <目录>，或设置环境变量 %s" % DATA_DIR_ENV)

# 复用 nflsoj 的前端与存储实现，保证两个工具界面完全一致
ApiError = core.ApiError
StoreError = core.StoreError
Store = core.Store
Console = core.Console
Progress = core.Progress
STATE_TODO, STATE_DOING, STATE_DONE = core.STATE_TODO, core.STATE_DOING, core.STATE_DONE
STATE_LABEL = core.STATE_LABEL
STATE_ORDER = core.STATE_ORDER
STATE_ALIASES = core.STATE_ALIASES
DEFAULT_MARK_STATE = core.DEFAULT_MARK_STATE
arg_int_range = core.arg_int_range
arg_int_min = core.arg_int_min
clamp_timeout = core.clamp_timeout
clamp_workers = core.clamp_workers
safe_int = core.safe_int
TIMEOUT_MIN, TIMEOUT_MAX = core.TIMEOUT_MIN, core.TIMEOUT_MAX
WORKERS_MIN, WORKERS_MAX = core.WORKERS_MIN, core.WORKERS_MAX
WIDTH_MIN, WIDTH_MAX = core.WIDTH_MIN, core.WIDTH_MAX

# Hydro 的赛制代号 → 展示用名称
RULE_LABELS = {
    "ioi": "IOI",
    "strictioi": "IOI",
    "acm": "ACM",
    "oi": "OI",
    "strictoi": "OI",
    "ledo": "Ledo",
    "homework": "作业",
    "exam": "考试",
    "none": "自定义",
}


# ---------------------------------------------------------------------------
# 路径与站点辅助
# ---------------------------------------------------------------------------
def resolve_data_dir(cli_value=None) -> str:
    """数据目录优先级：--data-dir > 环境变量 HYDROOJ_DATA_DIR > ~/.hydrooj。"""
    raw = cli_value or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    return os.path.abspath(os.path.expanduser(str(raw)))


def rule_label(rule) -> str:
    text = str(rule or "").strip()
    return RULE_LABELS.get(text.lower(), text or "自定义")


def short_id(contest_id) -> str:
    """Hydro 的比赛 ID 是 24 位十六进制，表格里只展示前 8 位。"""
    return str(contest_id or "")[:8]


def contest_url(base_url: str, contest_id) -> str:
    return "%s/contest/%s" % (str(base_url).rstrip("/"), contest_id)


def problem_url(base_url: str, contest_id, problem_id) -> str:
    base = "%s/p/%s" % (str(base_url).rstrip("/"), problem_id)
    return "%s?tid=%s" % (base, contest_id) if contest_id else base


def link_builder_for(base_url: str, pids_by_order=None):
    """给共享的 Markdown 渲染器用的链接构造函数。"""
    def build(contest, order=None):
        cid = contest.get("id") if isinstance(contest, dict) else contest
        if order is None:
            return contest_url(base_url, cid)
        pid = (pids_by_order or {}).get(order)
        return problem_url(base_url, cid, pid) if pid else contest_url(base_url, cid)
    return build


def parse_cookie_string(raw: str) -> dict:
    cookies = {}
    for part in re.split(r"[;\n]", raw or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = urllib.parse.unquote(value.strip())
    return cookies


def cookie_string(cookies: dict) -> str:
    return "; ".join("%s=%s" % (k, v) for k, v in sorted((cookies or {}).items()) if v)


# ---------------------------------------------------------------------------
# 站点客户端
# ---------------------------------------------------------------------------
class HydroClient:
    """Hydro 的 JSON 视图客户端（复用 nflsoj 的 keep-alive 传输层）。"""

    def __init__(self, base_url: str = DEFAULT_API, cookies=None, timeout: int = 30,
                 insecure: bool = False, debug: bool = False, offline: bool = False,
                 retries: int = 4):
        self.base_url = (base_url or DEFAULT_API).rstrip("/")
        self.cookies = dict(cookies or {})
        self.offline = bool(offline)
        self.debug = bool(debug)
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.http = core.HttpTransport(self.base_url, timeout=timeout, insecure=insecure,
                                       retries=0, debug=debug, offline=offline)
        self.blocked = 0          # 被网关拦截并重试的次数（统计用）

    def close(self) -> None:
        self.http.close()

    # -- 请求 -------------------------------------------------------------
    def _headers(self, accept: str = "application/json") -> dict:
        headers = {"Accept": accept, "Referer": self.base_url + "/", "User-Agent": USER_AGENT}
        cookie = cookie_string(self.cookies)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _send_form(self, path: str, data: dict, accept: str):
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        headers = self._headers(accept)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(self.base_url + path, data=body,
                                         headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            with exc:                      # HTTPError 也是响应对象，确定性地释放连接
                status = exc.code
                headers_out = exc.headers
                try:
                    payload = exc.read()
                except OSError:
                    payload = b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc) or exc
            raise ApiError("网络错误：%s" % reason, None,
                           core.network_error_hint(reason, DEFAULT_API))
        else:
            with response:
                status, headers_out, payload = response.status, response.headers, response.read()
        self._capture_cookies(headers_out)
        return status, payload

    def _capture_cookies(self, headers) -> None:
        try:
            values = headers.get_all("Set-Cookie") or []
        except AttributeError:
            values = []
        for raw in values:
            first = str(raw).split(";", 1)[0]
            if "=" in first:
                name, _, value = first.partition("=")
                name = name.strip()
                if name:
                    self.cookies[name] = value.strip()

    def request(self, method: str, path: str, query=None, data=None,
                accept: str = "application/json"):
        """返回 (status, text)；被网关拦截时自动重试。"""
        if self.offline:
            raise ApiError("已启用离线模式（--offline），该操作需要访问站点", None,
                           "去掉 --offline 后重试")
        last_status, last_text = 0, ""
        for attempt in range(self.retries + 1):
            try:
                if method.upper() == "GET":
                    status, _headers, payload = self.http.request(
                        "GET", path, query=query, headers=self._headers(accept))
                else:
                    status, payload = self._send_form(path, data or {}, accept)
            except ApiError as exc:
                if attempt < self.retries and not exc.status:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                raise
            text = payload.decode("utf-8", "replace")
            last_status, last_text = status, text
            blocked = any(marker in text[:4096] for marker in BLOCK_MARKERS)
            if blocked or status == 503 or status >= 500:
                if attempt < self.retries:
                    self.blocked += 1
                    if self.debug:
                        sys.stderr.write("[debug] 网关拦截/5xx（%s），%.1fs 后重试 %d/%d\n"
                                         % (status, 1.0 * (attempt + 1), attempt + 1, self.retries))
                    time.sleep(1.0 * (attempt + 1))
                    continue
                if blocked:
                    raise ApiError("站点网关拦截了本次请求（Web Access blocked）", status,
                                   "该站点存在随机拦截，稍后重试；并发高时可降低 --workers")
            return status, text
        return last_status, last_text

    def get_json(self, path: str, query=None):
        status, text = self.request("GET", path, query=query)
        if status in (401, 403):
            raise ApiError("没有权限访问（可能需要登录，或需要先报名该比赛）", status,
                           "先执行 hydrooj login；受限比赛请先在网页上报过名")
        try:
            data = json.loads(text)
        except ValueError:
            if 'data-page="user_login"' in text or "/login" in text[:400]:
                raise ApiError("登录状态无效或已过期", 401, "请重新执行：hydrooj login")
            if status == 404:
                raise ApiError("请求的资源不存在（%s）" % path, 404,
                               "检查比赛 / 题目 ID 是否正确")
            raise ApiError("服务器返回了非 JSON 内容（可能被网关拦截）", status,
                           "稍后重试；若持续失败，请先在浏览器打开站点确认状态")
        if not isinstance(data, dict):
            raise ApiError("服务器返回了非预期的数据结构（期望 JSON 对象）", status,
                           "站点版本可能不兼容，请确认是否为 Hydro v4")
        error = data.get("error")
        if error:
            if isinstance(error, dict):
                message = error.get("message") or "未知错误"
            else:
                message = str(error)
            raise ApiError("站点返回错误：%s" % message, status,
                           "检查 ID 是否正确，或确认是否有查看权限")
        # 未登录时 Hydro 会返回 {"url": "/login?...", "UiContext": ..., "UserContext": ...}
        if (data.get("url")
                and "tdoc" not in data and "pdoc" not in data and "tdocs" not in data):
            raise ApiError("登录状态无效或已过期", 401, "请重新执行：hydrooj login")
        return data

    # -- 登录 -------------------------------------------------------------
    def user_info(self) -> dict:
        """读取 / 的 UserContext；未登录时返回 {}。"""
        data = self.get_json("/")
        raw = data.get("UserContext")
        user = {}
        if isinstance(raw, str):
            try:
                user = json.loads(raw)
            except ValueError:
                user = {}
        elif isinstance(raw, dict):
            user = raw
        if not isinstance(user, dict) or not user.get("_id"):
            return {}
        return user

    def domain_info(self) -> dict:
        try:
            domain = self.get_json("/").get("domain")
            return domain if isinstance(domain, dict) else {}
        except ApiError:
            return {}

    def login(self, username: str, password: str) -> dict:
        """登录。

        Hydro 要求先建立访客会话（GET /login 拿 sid），再 POST 表单；否则服务端会
        直接返回登录页而不处理凭据。这里用一个独立的 CookieJar 完成整个流程。
        """
        if not username or not password:
            raise ApiError("需要用户名和密码", None,
                           "用 -u/--username 与 -p/--password 指定，或交互式输入")
        if self.offline:
            raise ApiError("已启用离线模式（--offline），登录需要访问站点", None,
                           "去掉 --offline 后重试")
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html",
                   "Referer": self.base_url + "/"}
        try:
            with opener.open(urllib.request.Request(self.base_url + "/login", headers=headers),
                             timeout=self.timeout) as response:
                response.read()
        except (urllib.error.HTTPError, OSError):
            pass                              # 拿不到访客会话也继续尝试，POST 会报错

        body = urllib.parse.urlencode({
            "uname": username, "password": password, "rememberme": "on",
            "tfa": "", "authnChallenge": "", "login_submit": "登录"}).encode("utf-8")
        post_headers = dict(headers)
        post_headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, text = 0, ""
        pw_error = False
        for attempt in range(self.retries + 1):
            try:
                with opener.open(urllib.request.Request(
                        self.base_url + "/login", data=body, headers=post_headers,
                        method="POST"), timeout=self.timeout) as response:
                    status = response.status
                    text = response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                with exc:
                    status = exc.code
                    try:
                        text = exc.read().decode("utf-8", "replace")
                    except OSError:
                        text = ""
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", exc) or exc
                fatal = core.is_fatal_network_error(reason)
                if not fatal and attempt < self.retries:
                    self.blocked += 1
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise ApiError("网络错误：%s" % reason, None,
                           core.network_error_hint(reason, DEFAULT_API))
            blocked = any(marker in text[:4096] for marker in BLOCK_MARKERS)
            pw_error = "密码错误" in text or "用户名不存在" in text
            if (blocked or status == 503 or status >= 500
                    or (status == 403 and not pw_error)) and attempt < self.retries:
                self.blocked += 1
                if self.debug:
                    sys.stderr.write("[debug] 登录被拦/5xx（%s），重试 %d/%d\n"
                                     % (status, attempt + 1, self.retries))
                time.sleep(1.0 * (attempt + 1))
                continue
            break

        for cookie in jar:
            self.cookies[cookie.name] = cookie.value
        if pw_error:
            raise ApiError("登录失败：用户名或密码错误", 403,
                           "确认账号密码；忘记密码请到网页上重置")
        if status == 403:
            raise ApiError("登录被站点拒绝（HTTP 403）", 403,
                           "可能是风控 / 网关拦截，稍等几十秒再试；也可用在浏览器里复制的 Cookie 登录")
        if 'data-page="user_login"' in text:
            raise ApiError("登录失败：站点仍返回登录页", status,
                           "可能触发了验证码或风控，请先在浏览器里登录一次")
        if not self.cookies:
            raise ApiError("登录失败：站点没有返回会话 Cookie", status,
                           "改用在浏览器里登录后复制 Cookie 的方式")
        user = self.user_info()
        if not user:
            raise ApiError("登录后仍无法读取账号信息", None, "稍后重试，或改用 Cookie 登录")
        return user

    # -- 数据接口 ---------------------------------------------------------
    def contest_page(self, page: int = 1):
        return self.get_json("/contest", query={"page": int(page)})

    def contest_detail(self, contest_id):
        return self.get_json("/contest/%s" % contest_id)

    def contest_problems(self, contest_id):
        return self.get_json("/contest/%s/problems" % contest_id)

    def problem(self, problem_id, contest_id=None):
        query = {"tid": contest_id} if contest_id else None
        return self.get_json("/p/%s" % problem_id, query=query)


def _ensure_base(store: Store) -> Store:
    """Store 是通用实现，其默认站点属于 nflsoj；本工具要保证回落到 Hydro 地址。"""
    if not store.api_override and not store.config.get("baseUrl"):
        store.api_override = DEFAULT_API
    return store


def client_for(store: Store, args=None) -> HydroClient:
    args = args or argparse.Namespace()
    _ensure_base(store)
    cookies = store.config.get("cookies")
    if not isinstance(cookies, dict) or not cookies:
        cookies = parse_cookie_string(store.config.get("cookie") or "")
    return HydroClient(
        base_url=store.api_base,
        cookies=cookies,
        timeout=getattr(args, "timeout", None) or 30,
        insecure=getattr(args, "insecure", False),
        debug=getattr(args, "debug", False),
        offline=getattr(args, "offline", False))


def save_session(store: Store, console: Console, client: HydroClient, user: dict) -> str:
    """把 Cookie 与用户名写回配置，返回用户名。"""
    username = str(user.get("uname") or user.get("_id") or "unknown")
    store.config["cookies"] = dict(client.cookies)
    store.config["cookie"] = cookie_string(client.cookies)
    store.config["username"] = username
    try:
        store.save_config()
    except StoreError as exc:
        console.warn("登录成功，但凭据未能写入本地：%s" % exc.message)
    return username


def resolve_username(store: Store, console: Console, client: HydroClient,
                     online: bool = False) -> str:
    """确定当前账号名：online=True 时联网校验，失败抛 ApiError。"""
    cached = store.cached_username()
    if not online or client.offline:
        return cached
    user = client.user_info()
    if not user:
        if cached:
            raise ApiError("登录状态无效或已过期", 401, "请重新执行：hydrooj login")
        raise ApiError("尚未登录", 401, "请先运行：hydrooj login")
    username = str(user.get("uname") or user.get("_id") or "unknown")
    if store.config.get("username") != username:
        store.config["username"] = username
        try:
            store.save_config()
        except StoreError as exc:
            console.warn("用户名未能写回配置：%s" % exc.message)
    return username


# ---------------------------------------------------------------------------
# 数据规整
# ---------------------------------------------------------------------------
def _localized(text, depth: int = 0):
    """Hydro 的 content 可能是字符串，也可能是 {"zh": "..."} 形式的 JSON 字符串。"""
    if isinstance(text, dict):
        for key in ("zh", "zh-CN", "en", "en-US"):
            if isinstance(text.get(key), str):
                return text[key]
        for value in text.values():
            if isinstance(value, str):
                return value
        return ""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    looks_json = ((stripped.startswith("{") and stripped.endswith("}"))
                  or (stripped.startswith('"') and stripped.endswith('"')))
    if looks_json and depth < 3:
        try:
            parsed = json.loads(stripped)
        except ValueError:
            return text
        if isinstance(parsed, (str, dict)):
            # 少数部署会把 content 再包一层字符串，多解一次即可
            return _localized(parsed, depth + 1)
        return text
    return text


def contest_from_tdoc(tdoc: dict, tsdoc=None) -> dict:
    """把 Hydro 的 tdoc 规整成 store 使用的比赛结构。"""
    tdoc = tdoc or {}
    contest_id = str(tdoc.get("_id") or tdoc.get("docId") or "")
    return {
        "id": contest_id,
        "name": tdoc.get("title") or ("比赛 %s" % contest_id),
        "mode": rule_label(tdoc.get("rule")),
        "rule": tdoc.get("rule") or "",
        "startTime": tdoc.get("beginAt"),
        "endTime": tdoc.get("endAt"),
        "phase": None,                     # 由开始/结束时间推算
        "creator": "",
        "groups": [str(g) for g in (tdoc.get("assign") or []) if g],
        "pids": [int(p) for p in (tdoc.get("pids") or []) if str(p).isdigit()],
        "attend": tdoc.get("attend"),
        "attended": bool(tsdoc),
        "description": _localized(tdoc.get("content")).strip(),
    }


def extract_problems(payload: dict) -> list:
    """从 /contest/<id>/problems 的 JSON 中取出题目列表（按题号排序）。"""
    payload = payload or {}
    pdict = payload.get("pdict") or {}
    tdoc = payload.get("tdoc") or {}
    pids = [int(p) for p in (tdoc.get("pids") or []) if str(p).isdigit()]
    if not pids:
        pids = sorted(int(key) for key in pdict if str(key).isdigit())
    status_map = {}
    for source in (payload.get("psdict") or {}, (payload.get("tsdoc") or {}).get("endPsdict") or {}):
        if isinstance(source, dict):
            for pid, record in source.items():
                if isinstance(record, dict):
                    status_map[str(pid)] = record
    problems = []
    for order, pid in enumerate(pids, 1):
        doc = pdict.get(str(pid)) or {}
        config = doc.get("config") or {}
        item = {
            "problemId": int(pid),
            "displayOrder": order,
            "title": doc.get("title") or ("题目 %s" % pid),
            "tags": [str(tag) for tag in (doc.get("tag") or []) if tag],
        }
        time_min, time_max = config.get("timeMin"), config.get("timeMax")
        if time_min is not None:
            item["timeLimit"] = ("%d ms" % time_min if time_max in (None, time_min)
                                 else "%d-%d ms" % (time_min, time_max))
        memory_min, memory_max = config.get("memoryMin"), config.get("memoryMax")
        if memory_min is not None:
            item["memoryLimit"] = ("%d MB" % memory_min if memory_max in (None, memory_min)
                                   else "%d-%d MB" % (memory_min, memory_max))
        record = status_map.get(str(pid))
        if record:
            item["submitted"] = True
            if record.get("statusText"):
                item["bestStatus"] = str(record["statusText"])
            if record.get("score") is not None:
                item["score"] = record["score"]
        problems.append(item)
    return problems


def problem_statement(payload: dict) -> str:
    """取出题目的 Markdown 正文。"""
    pdoc = (payload or {}).get("pdoc") or {}
    return _localized(pdoc.get("content")).strip()


def statement_payload(problem: dict, payload: dict) -> dict:
    pdoc = (payload or {}).get("pdoc") or {}
    return {
        "id": problem.get("problemId"),
        "title": problem.get("title") or "",
        "statement": problem_statement(payload),
        "tags": problem.get("tags") or [],
        "timeLimit": problem.get("timeLimit"),
        "memoryLimit": problem.get("memoryLimit"),
        "fetchedAt": int(time.time()),
        "generator": "hydrooj %s" % __version__,
        "html": bool(pdoc.get("html")),
    }


# ---------------------------------------------------------------------------
# 拉取比赛 / 题目
# ---------------------------------------------------------------------------
def _safe_save_progress(store: Store, console: Console) -> None:
    try:
        store.save_progress()
    except StoreError as exc:
        console.warn("未能写入本地缓存：%s" % exc.message)


MAX_CONTEST_PAGES = 500          # 与 nflsoj 一致的上限，防止异常 tpcount 导致死循环


def _page_batch(data) -> list:
    """把一页数据规整成比赛列表；tdocs 不是列表时按空页处理。"""
    if not isinstance(data, dict):
        return []
    tsdict = data.get("tsdict")
    tsdict = tsdict if isinstance(tsdict, dict) else {}
    tdocs = data.get("tdocs")
    if not isinstance(tdocs, list):
        return []
    batch = []
    for tdoc in tdocs:
        if not isinstance(tdoc, dict):
            continue
        batch.append(contest_from_tdoc(tdoc, tsdict.get(str(tdoc.get("_id")))))
    return batch


def fetch_contest_pages(client: HydroClient, console: Console, max_pages=None,
                        workers: int = DEFAULT_WORKERS, on_page=None):
    """逐页拉取比赛列表，返回 (contests, total_pages)。

    on_page(batch, page_no) 返回 True 时提前结束（增量同步用）。
    第 1、2 页串行（便于增量判断），其余页按批并发抓取。
    """
    progress = Progress(console, "获取比赛列表", unit="页", detail_width=24).start_line()
    batches = {}
    stopped = False
    total = 1
    try:
        first = client.contest_page(1)
        raw_total = first.get("tpcount") if isinstance(first, dict) else None
        total = max(1, min(safe_int(raw_total, 1), MAX_CONTEST_PAGES))
        progress.total = total
        if max_pages:
            total = min(total, max(1, safe_int(max_pages, 1)))
        batches[1] = _page_batch(first)
        progress.update(1, detail="第 1 页")
        if on_page and on_page(batches[1], 1):
            stopped = True
        if not stopped and total >= 2:
            batches[2] = _page_batch(client.contest_page(2))
            progress.update(1, detail="第 2 页")
            if on_page and on_page(batches[2], 2):
                stopped = True
        page = 3
        batch_size = clamp_workers(workers)
        while not stopped and page <= total:
            group = list(range(page, min(page + batch_size, total + 1)))
            pool = ThreadPoolExecutor(max_workers=len(group))
            try:
                results = list(pool.map(client.contest_page, group))
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
                release_connections(client)
            for page_no, data in zip(group, results):
                batches[page_no] = _page_batch(data)
                progress.update(1, detail="第 %d 页" % page_no)
                if on_page and on_page(batches[page_no], page_no):
                    stopped = True
                    break
            page = group[-1] + 1
    finally:
        progress.close()
    contests = []
    for page_no in sorted(batches):
        contests.extend(batches[page_no])
    return contests, total


def _fetch_contest_problems(client: HydroClient, contest_id):
    payload = client.contest_problems(contest_id)
    return payload, extract_problems(payload)


def sync_contests(store: Store, username: str, console: Console, client: HydroClient,
                  keyword=None, only_ids=None, workers: int = DEFAULT_WORKERS,
                  with_problems: bool = True, force: bool = False,
                  incremental: bool = True, limit: int = None) -> dict:
    """同步比赛列表与每场比赛的题目。"""
    stats = {"contests": 0, "problems": 0, "failed": [], "skipped": 0, "pages": 0,
             "elapsed": 0.0, "blocked": 0}
    started = time.time()
    wanted = [str(cid) for cid in only_ids] if only_ids else None

    known = {}
    for entry in store.all_contests(username):
        cached = store.contest_entry(username, entry.get("id")) or {}
        if cached.get("syncedAt"):
            known[str(entry.get("id"))] = cached

    # -- 要同步的比赛 -----------------------------------------------------
    if wanted is not None:
        targets = []
        for cid in wanted:
            cached = store.contest_entry(username, cid) or {}
            targets.append({"id": cid, "name": cached.get("name") or ("比赛 %s" % short_id(cid)),
                            "startTime": cached.get("startTime"), "endTime": cached.get("endTime"),
                            "mode": cached.get("mode") or "", "groups": cached.get("groups") or []})
        stats["pages"] = 0
    else:
        state = {"quiet": 0}

        def on_page(batch, page_no):
            if not incremental:
                return False
            if batch and all(str(item.get("id")) in known for item in batch):
                state["quiet"] += 1
            else:
                state["quiet"] = 0
            return state["quiet"] >= 2

        max_pages = None
        if limit and not keyword:
            max_pages = (limit + PAGE_SIZE - 1) // PAGE_SIZE + 1
        contests, total = fetch_contest_pages(
            client, console, max_pages=max_pages, workers=workers,
            on_page=on_page if incremental else None)
        stats["pages"] = total
        needle = (keyword or "").strip().lower()
        if needle:
            contests = [c for c in contests
                        if needle in (c.get("name") or "").lower() or needle == str(c.get("id"))]
        targets = contests
        if not incremental:
            stats["pages"] = total

    if limit:
        targets = targets[:limit]
    if not targets:
        stats["elapsed"] = time.time() - started
        return stats

    # -- 只同步列表 -------------------------------------------------------
    if not with_problems:
        for contest in targets:
            store.upsert_contest(username, contest, None)
            stats["contests"] += 1
        _safe_save_progress(store, console)
        stats["elapsed"] = time.time() - started
        return stats

    todo = []
    for contest in targets:
        cid = str(contest.get("id"))
        cached = known.get(cid)
        if cached and not force:
            store.upsert_contest(username, contest, None)
            stats["contests"] += 1
            stats["problems"] += len(cached.get("problems") or [])
            stats["skipped"] += 1
        else:
            todo.append(contest)

    progress = Progress(console, "同步题目列表", total=len(targets), unit="场",
                        detail_width=34).start_line()
    progress.update(stats["skipped"])
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 12)))
    futures = {}
    try:
        for contest in todo:
            futures[pool.submit(_fetch_contest_problems, client, str(contest.get("id")))] = contest
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for future in done:
                contest = futures[future]
                try:
                    payload, problems = future.result()
                except ApiError as exc:
                    stats["failed"].append((short_id(contest.get("id")),
                                            contest.get("name") or "", exc.message))
                    progress.update(1, total=len(targets), failed=True,
                                    detail="失败 · %s" % core.clean_text(contest.get("name") or "")[:22])
                    continue
                except Exception as exc:            # noqa: BLE001
                    stats["failed"].append((short_id(contest.get("id")),
                                            contest.get("name") or "", str(exc)))
                    progress.update(1, total=len(targets), failed=True,
                                    detail="失败 · %s" % core.clean_text(contest.get("name") or "")[:22])
                    continue
                meta = contest_from_tdoc(payload.get("tdoc") or {}, payload.get("tsdoc"))
                if not meta.get("name"):
                    meta = dict(contest)
                entry = store.upsert_contest(username, meta, problems)
                entry["attended"] = bool(payload.get("tsdoc"))
                entry["attend"] = (payload.get("tdoc") or {}).get("attend", meta.get("attend"))
                stats["contests"] += 1
                stats["problems"] += len(problems)
                progress.update(1, total=len(targets),
                                detail="%s · %d 题" % (core.clean_text(meta.get("name"))[:22],
                                                      len(problems)))
    except KeyboardInterrupt:
        _safe_save_progress(store, console)
        pool.shutdown(wait=False, cancel_futures=True)
        progress.close()
        raise
    finally:
        progress.close()
        pool.shutdown(wait=True, cancel_futures=True)

    stats["blocked"] = client.blocked
    _safe_save_progress(store, console)
    stats["elapsed"] = time.time() - started
    return stats


def _ensure_problems(store: Store, username: str, console: Console, entry: dict,
                     client: HydroClient, refresh: bool = False):
    """按需补拉某场比赛的题目列表（有缓存时完全本地）。"""
    if entry.get("problems") and not refresh:
        return entry
    try:
        payload = client.contest_problems(entry.get("id"))
    except ApiError as exc:
        if entry.get("problems"):
            console.warn("刷新题目列表失败：%s（继续使用本地缓存）" % exc.message)
            return entry
        core.report_error(console, exc)
        return None
    meta = contest_from_tdoc(payload.get("tdoc") or {}, payload.get("tsdoc"))
    if not meta.get("name"):
        meta = dict(entry)
    problems = extract_problems(payload)
    entry = store.upsert_contest(username, meta, problems)
    entry["attended"] = bool(payload.get("tsdoc"))
    _safe_save_progress(store, console)
    return entry


# ---------------------------------------------------------------------------
# 查找辅助
# ---------------------------------------------------------------------------
def _apply_filters(contests, keyword=None, phase=None, ids=None):
    result = list(contests)
    needle = (keyword or "").strip().lower()
    if needle:
        result = [c for c in result
                  if needle in (c.get("name") or "").lower()
                  or str(c.get("id")).lower().startswith(needle)]
    if phase:
        result = [c for c in result
                  if core.contest_phase(c.get("startTime"), c.get("endTime"), c.get("phase")) == phase]
    if ids:
        wanted = [str(i).strip().lower() for i in ids]
        result = [c for c in result
                  if any(str(c.get("id")).lower() == w or str(c.get("id")).lower().startswith(w)
                         for w in wanted)]
    return result


def _pick_contest(store: Store, username: str, console: Console, spec):
    """按完整 ID / ID 前缀 / 名称关键词查找本地比赛，并给出可操作建议。"""
    contests = store.all_contests(username)
    text = str(spec).strip().lower()
    if not text:
        return None
    for contest in contests:
        if str(contest.get("id")).lower() == text:
            return contest
    if len(text) >= 4:
        prefix = [c for c in contests if str(c.get("id")).lower().startswith(text)]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            console.error("ID 前缀 %s 匹配到 %d 场比赛，请写完整 ID：" % (spec, len(prefix)))
            rows = [[(short_id(c.get("id")), "grey"), (str(c.get("id")), "grey"),
                     (c.get("name") or "",)] for c in prefix[:12]]
            console.table(["短 ID", "完整 ID", "比赛名称"], rows)
            return None
    matches = [c for c in contests if text in (c.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.error("匹配到 %d 场比赛，请用更精确的名称或比赛 ID：" % len(matches))
        rows = [[(short_id(c.get("id")), "grey"), (c.get("name") or "",),
                 (core.fmt_date(c.get("startTime")), "grey")] for c in matches[:12]]
        console.table(["短 ID", "比赛名称", "开始日期"], rows, aligns=["right", "left", "left"])
        console.muted("例如：hydrooj problems %s" % str(matches[0].get("id")))
        return None
    console.error("找不到比赛：%s" % spec)
    if contests:
        import difflib
        names = [c.get("name") or "" for c in contests]
        close = difflib.get_close_matches(str(spec), names, n=3, cutoff=0.45)
        if close:
            console.muted("你是不是想找：" + "  /  ".join(close))
        console.muted("本地共有 %d 场比赛，可用 `hydrooj contests` 查看，或先运行 `hydrooj sync`" % len(contests))
    else:
        console.muted("本地还没有比赛数据，先运行 `hydrooj sync` 同步")
    return None


# ---------------------------------------------------------------------------
# Markdown 归档
# ---------------------------------------------------------------------------
def _contest_bundle(meta: dict, problems, detail: dict, files, has_statement,
                    archived_at: str, base_url: str) -> dict:
    pids_by_order = {p.get("displayOrder"): p.get("problemId") for p in problems}
    return {
        "contest": meta,
        "problems": problems,
        "detail": {
            "description": meta.get("description") or "",
            "announcements": [],
            "permissions": {"canViewProblems": bool(problems)},
        },
        "files": files,
        "has_statement": has_statement,
        "archived_at": archived_at,
        "base_url": base_url,
        "source_url": contest_url(base_url, meta.get("id")),
        "link_builder": link_builder_for(base_url, pids_by_order),
        "generator": "hydrooj %s" % __version__,
    }


def archive_one_contest(store: Store, username: str, console: Console, client: HydroClient,
                        contest: dict, root: str, with_statements: bool = True,
                        force: bool = False, workers: int = DEFAULT_WORKERS,
                        used_dirs=None, with_origin: bool = False,
                        origin_threshold_value: float = core.ORIGIN_THRESHOLD_DEFAULT) -> dict:
    """归档一场比赛：比赛首页 + 每题一份题面。"""
    _ensure_base(store)
    cid = str(contest.get("id"))
    payload = client.contest_problems(cid)
    meta = contest_from_tdoc(payload.get("tdoc") or {}, payload.get("tsdoc"))
    if not meta.get("name"):
        meta = dict(contest)
    problems = extract_problems(payload)
    entry = store.upsert_contest(username, meta, problems)
    entry["attended"] = bool(payload.get("tsdoc"))
    problems = entry.get("problems") or problems

    dir_name = core.contest_dir_name(entry, used_dirs)
    contest_dir = os.path.join(root, dir_name)
    problems_dir = os.path.join(contest_dir, "problems")
    archived_at = core.preserved_timestamp(os.path.join(contest_dir, "index.md"), force)

    statements, errors = {}, {}
    if with_statements and problems:
        def fetch(problem):
            pid = problem.get("problemId")
            if pid is None:
                return problem, None, "题目缺少 ID"
            cached = None if force else store.load_statement(pid, STATEMENT_CACHE_TTL)
            if cached and cached.get("statement"):
                return problem, cached, None
            try:
                data = client.problem(pid, cid)
            except ApiError as exc:
                return problem, None, exc.message
            except Exception as exc:            # noqa: BLE001
                return problem, None, str(exc)
            statement = problem_statement(data)
            if not statement:
                return problem, None, "站点没有返回题面（可能需要报名或权限）"
            record = statement_payload(problem, data)
            try:
                store.save_statement(pid, record)
            except StoreError:
                pass
            return problem, record, None

        with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
            for problem, record, error in pool.map(fetch, problems):
                key = str(problem.get("problemId"))
                if error:
                    errors[key] = error
                elif record:
                    statements[key] = record

    origins = {}
    if with_origin:
        cpret = core.CpretClient(timeout=core.ORIGIN_TIMEOUT, retries=2,
                                 delay=core.ORIGIN_DELAY_DEFAULT, debug=False)
        try:
            for problem in problems:
                pid = problem.get("problemId")
                statement_text = (statements.get(str(pid)) or {}).get("statement") or ""
                if not statement_text:
                    continue
                text = core.origin_query_text(problem.get("title"), statement_text)
                try:
                    record, _cached = core.run_origin_search(
                        store, console, cpret, text, origin_threshold_value)
                except ApiError:
                    continue
                origins[str(pid)] = core.compact_origin(record)
                try:
                    store.set_origin(username, cid, pid, origins[str(pid)])
                except Exception:           # noqa: BLE001
                    pass
        finally:
            cpret.close()
            _safe_save_progress(store, console)

    files, has_statement, used_files = {}, {}, set()
    written = unchanged = 0
    for problem in problems:
        filename = core.problem_file_name(problem, used_files)
        order = str(problem.get("displayOrder"))
        files[order] = "problems/" + filename
        pid = problem.get("problemId")
        record = dict(statements.get(str(pid)) or {})
        record["origin"] = origins.get(str(pid)) or problem.get("origin") or {}
        has_statement[order] = bool(record.get("statement"))
        path = os.path.join(problems_dir, filename)
        content = core.render_problem_markdown({
            "contest": entry,
            "problem": problem,
            "statement": record.get("statement"),
            "error": errors.get(str(pid), ""),
            # 沿用已有文件的归档时间，重复归档时内容不变、不重写
            "archived_at": core.preserved_timestamp(path, force, archived_at),
            "base_url": store.api_base,
            "source_url": problem_url(store.api_base, cid, problem.get("problemId")),
            "generator": "hydrooj %s" % __version__,
        })
        if not with_statements and os.path.isfile(path):
            # 本次不抓题面：保留已有文件，避免把之前存好的题面覆盖成占位文字
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            except OSError:
                existing = ""
            has_statement[order] = bool(existing) and "未能获取题目正文" not in existing
            unchanged += 1
            continue
        status = core.write_if_changed(path, content)
        written += status == "written"
        unchanged += status == "unchanged"

    contest_md = core.render_contest_markdown(
        _contest_bundle(entry, problems, payload, files, has_statement, archived_at,
                        store.api_base))
    status = core.write_if_changed(os.path.join(contest_dir, "index.md"), contest_md)
    written += status == "written"
    unchanged += status == "unchanged"

    stats = core.collection_counts(problems)
    statement_total = (len(statements) if with_statements
                       else sum(1 for value in has_statement.values() if value))
    store.mark_archived(username, cid, contest_dir, len(problems), statement_total,
                        failed=len(errors))
    return {
        "contest": entry,
        "dir": dir_name,
        "path": contest_dir,
        "problems": len(problems),
        "statements": statement_total,
        "origins": len(origins),
        "origins_matched": sum(1 for o in origins.values() if o.get("matched")),
        "failed": errors,
        "files_written": written,
        "files_unchanged": unchanged,
        "done": stats[STATE_DONE],
    }


def archive_contests(store: Store, username: str, console: Console, client: HydroClient,
                     targets, root: str, with_statements: bool = True, force: bool = False,
                     workers: int = DEFAULT_WORKERS, with_origin: bool = False,
                     origin_threshold_value: float = core.ORIGIN_THRESHOLD_DEFAULT) -> dict:
    _ensure_base(store)
    os.makedirs(root, exist_ok=True)
    results, failures = [], []
    used_dirs = set()
    progress = Progress(console, "归档比赛", total=len(targets), unit="场",
                        detail_width=30).start_line()
    try:
        for contest in targets:
            progress.update(0, total=len(targets),
                            detail=core.clean_text(contest.get("name"))[:26])
            try:
                results.append(archive_one_contest(
                    store, username, console, client, contest, root,
                    with_statements=with_statements, force=force, workers=workers,
                    used_dirs=used_dirs, with_origin=with_origin,
                    origin_threshold_value=origin_threshold_value))
            except ApiError as exc:
                failures.append((short_id(contest.get("id")), contest.get("name"), exc.message))
            except Exception as exc:            # noqa: BLE001
                failures.append((short_id(contest.get("id")), contest.get("name"), str(exc)))
            progress.update(1, total=len(targets))
            _safe_save_progress(store, console)
    finally:
        progress.close()

    root_norm = os.path.normcase(os.path.abspath(root))
    records = []
    for contest in store.all_contests(username):
        entry = store.contest_entry(username, contest.get("id")) or {}
        path = entry.get("archivePath")
        if not path or not os.path.isdir(path):
            continue
        path_norm = os.path.normcase(os.path.abspath(path))
        if path_norm != root_norm and not path_norm.startswith(root_norm + os.sep):
            continue
        records.append({
            "contest": entry,
            "dir": os.path.basename(os.path.abspath(path).rstrip("\\/")),
            "problems": entry.get("archivedProblems", len(entry.get("problems") or [])),
            "statements": entry.get("archivedStatements", 0),
            "done": sum(1 for p in entry.get("problems") or [] if p.get("state") == STATE_DONE),
        })
    core.write_if_changed(
        os.path.join(root, "index.md"),
        core.render_root_index(records, username, title="Hydro OJ 本地存档",
                               generator="hydrooj %s" % __version__, tool="hydrooj"))
    _safe_save_progress(store, console)
    return {"results": results, "failures": failures, "records": records, "root": root}


# ---------------------------------------------------------------------------
# 命令：login / logout / whoami / config
# ---------------------------------------------------------------------------
def cmd_login(args, store: Store, console: Console) -> int:
    base_url = (getattr(args, "api", None) or store.api_base or DEFAULT_API).rstrip("/")
    cookie_raw = (getattr(args, "cookie", None) or getattr(args, "token", None) or "").strip()
    username = (getattr(args, "username", None) or "").strip()
    password = getattr(args, "password", None) or ""

    console.title("登录 Hydro OJ", note=base_url)
    cached = store.cached_username()
    if cached and not (cookie_raw or username):
        console.kv("当前账号", cached, value_color="bgreen")
        console.muted("已登录时也可以重复执行 login 来切换账号。")
        console.blank()

    if not (cookie_raw or username):
        console.section("选择登录方式")
        choice = console.choose("请选择", [
            ("1", "用户名 + 密码登录（推荐）"),
            ("2", "粘贴浏览器 Cookie（sid / sid.sig）"),
        ], default="1")
        if choice == "2":
            console.muted("获取方法：浏览器登录后按 F12 → Application → Cookies →")
            console.muted("复制 sid 与 sid.sig，或直接粘贴整段 Cookie。")
            cookie_raw = console.prompt("在此粘贴 Cookie")
            if not cookie_raw:
                console.error("Cookie 不能为空")
                return 1
        else:
            username = console.prompt("用户名")
            password = console.prompt("密码（输入时不显示）", mask=True)
            if not username or not password:
                console.error("用户名和密码都不能为空")
                return 1

    client = HydroClient(base_url=base_url, cookies=parse_cookie_string(cookie_raw),
                         timeout=getattr(args, "timeout", None) or 30,
                         insecure=getattr(args, "insecure", False),
                         debug=getattr(args, "debug", False),
                         offline=getattr(args, "offline", False))
    try:
        if cookie_raw and not username:
            console.info("正在校验 Cookie…")
            user = client.user_info()
            if not user:
                raise ApiError("Cookie 无效或已过期", 401,
                               "请在浏览器里重新复制 sid 与 sid.sig")
        else:
            console.info("正在登录…")
            user = client.login(username, password)
        store.config["baseUrl"] = base_url
        who = save_session(store, console, client, user)
    except ApiError as exc:
        return core.report_error(console, exc)
    finally:
        client.close()

    console.blank()
    console.ok("登录成功，欢迎回来，%s ！" % who)
    console.kv("站点", store.api_base)
    console.kv("数据目录", store.data_dir)
    console.blank()
    console.section("接下来可以")
    console.bullet("hydrooj sync          同步比赛与题目（首次约 1-3 分钟）")
    console.bullet("hydrooj contests      查看比赛列表与完成进度")
    console.bullet("hydrooj archive --all 把比赛与题面归档成本地 Markdown")
    return 0


def cmd_logout(args, store: Store, console: Console) -> int:
    store.load()
    who = store.cached_username()
    store.config.pop("cookies", None)
    store.config.pop("cookie", None)
    store.clear_credentials()
    try:
        store.save_config()
    except StoreError as exc:
        console.error("清除凭据失败：%s" % exc.message)
        if exc.hint:
            console.muted(exc.hint)
        return 1
    console.title("退出登录")
    console.ok("已清除本地登录凭据%s" % (("（原账号 %s）" % who) if who else ""))
    console.muted("做题标记与归档文件都保留在 %s" % store.data_dir)
    return 0


def cmd_whoami(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        data = client.get_json("/")
        raw = data.get("UserContext")
        user = json.loads(raw) if isinstance(raw, str) else (raw or {})
        domain = data.get("domain") or {}
        ui = data.get("UiContext") or {}
    except ApiError as exc:
        return core.report_error(console, exc)
    finally:
        client.close()

    console.title("当前登录状态", note=store.api_base)
    if not isinstance(user, dict) or not user.get("_id"):
        console.warn("未登录（Guest）")
        console.muted("运行 `hydrooj login` 后即可同步比赛与题目")
        return 0
    console.kv("用户名", str(user.get("uname")), value_color="bgreen")
    console.kv("用户 ID", str(user.get("_id")))
    if user.get("priv") is not None:
        console.kv("权限值", str(user.get("priv")))
    console.rule()
    console.kv("站点", domain.get("name") or "-")
    console.kv("站点版本", str(ui.get("constantVersion") or "-"))
    console.kv("数据目录", store.data_dir)
    console.kv("归档目录", store.archive_dir)
    username = store.cached_username()
    if username:
        contests = store.all_contests(username)
        counts = core.collection_counts([p for c in contests for p in c.get("problems") or []])
        console.kv("本地进度", "%d 场 / %d 题 · 已完成 %d" % (
            len(contests), sum(counts.values()), counts[STATE_DONE]))
    return 0


def cmd_config(args, store: Store, console: Console) -> int:
    _ensure_base(store)
    changed = False
    if getattr(args, "set_api", None):
        store.config["baseUrl"] = args.set_api.rstrip("/")
        store.api_override = ""
        changed = True
    if getattr(args, "set_archive_dir", None):
        store.config["archiveDir"] = os.path.abspath(os.path.expanduser(args.set_archive_dir))
        changed = True
    if getattr(args, "clear", False):
        store.config.pop("cookies", None)
        store.config.pop("cookie", None)
        store.clear_credentials()
        changed = True
    if changed:
        try:
            store.save_config()
            console.ok("配置已更新")
        except StoreError as exc:
            console.error("配置未能保存：%s" % exc.message)
            if exc.hint:
                console.muted(exc.hint)
            return 1
    console.title("本地配置")
    console.kv("站点地址", store.api_base)
    console.kv("配置文件", store.config_path)
    console.kv("记录文件", store.progress_path)
    console.kv("归档目录", store.archive_dir)
    cookies = store.config.get("cookies") or parse_cookie_string(store.config.get("cookie") or "")
    names = ", ".join(sorted(cookies)) if cookies else ""
    console.kv("Cookie", ("已保存（%s）" % names) if names else "(未设置)")
    console.kv("记录账号", store.config.get("username") or "(未设置)")
    console.muted("修改站点：hydrooj config --set-api URL")
    console.muted("修改归档目录：hydrooj config --set-archive-dir DIR")
    return 0


# ---------------------------------------------------------------------------
# 命令：sync / contests
# ---------------------------------------------------------------------------
def cmd_sync(args, store: Store, console: Console) -> int:
    if getattr(args, "offline", False):
        console.error("已启用离线模式（--offline），同步需要访问站点")
        console.muted("去掉 --offline 后重试")
        return 1
    client = client_for(store, args)
    console.title("同步比赛与题目", note=store.api_base)
    username = store.cached_username()
    if not username:
        try:
            username = resolve_username(store, console, client, online=True)
        except ApiError as exc:
            return core.report_error(console, exc)
    console.kv("账号", username, value_color="bgreen")
    if args.keyword:
        console.kv("关键词", args.keyword)
    if args.contest:
        console.kv("指定比赛", ", ".join(short_id(c) for c in args.contest))
    effective_workers = clamp_workers(args.workers)
    if effective_workers != args.workers:
        console.kv("并发数", "%d \u2192 %d\uff08\u8d85\u51fa\u5408\u7406\u8303\u56f4\uff0c\u5df2\u94b3\u4f4d\uff09"
                   % (args.workers, effective_workers), value_color="yellow")
    else:
        console.kv("并发数", str(effective_workers))
    incremental = bool(args.incremental) and not args.force and not args.contest and not args.keyword
    console.kv("同步模式", "增量（连续两页已同步即停止）" if incremental else "全量")
    console.blank()
    try:
        stats = sync_contests(store, username, console, client,
                              keyword=args.keyword, only_ids=args.contest,
                              workers=effective_workers, with_problems=not args.no_problems,
                              force=args.force, incremental=incremental,
                              limit=(args.limit or None))
    except ApiError as exc:
        return core.report_error(console, exc)
    finally:
        client.close()

    console.blank()
    console.ok("同步完成：%d 场比赛 / %d 道题目，用时 %s" % (
        stats["contests"], stats["problems"], core.fmt_seconds(stats["elapsed"])))
    if stats["skipped"]:
        console.muted("其中 %d 场已有题目缓存，本次跳过详情请求（--force 可强制刷新）" % stats["skipped"])
    if stats["blocked"]:
        console.muted("期间站点网关拦截了 %d 次请求，已自动重试成功" % stats["blocked"])
    if stats["failed"]:
        console.blank()
        console.warn("%d 场比赛的题目拉取失败：" % len(stats["failed"]))
        console.table(["短 ID", "比赛名称", "错误"],
                      [[(cid, "grey"), (name or "",), (msg, "bred")]
                       for cid, name, msg in stats["failed"][:10]])
        console.muted("可稍后单独重试：hydrooj sync --contest <完整ID>")
    console.blank()
    console.section("下一步")
    console.bullet("hydrooj contests            查看比赛列表与进度")
    console.bullet("hydrooj problems <比赛ID>   查看某场比赛的题目")
    console.bullet("hydrooj archive --all       归档比赛与题面到本地 Markdown")
    return 0


def cmd_contests(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client,
                                    online=bool(args.live) and not store.cached_username())
    except ApiError as exc:
        return core.report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：hydrooj login")
        return 1

    contests = store.all_contests(username)
    source = "本地缓存"
    if args.live or not contests:
        if not contests and not args.live:
            console.warn("本地还没有比赛数据，正在自动同步一次…")
        filters_active = bool(args.keyword or args.phase or args.ids)
        collected = []

        def on_page(batch, page_no):
            collected.extend(batch)
            if not filters_active:
                return False
            filtered = _apply_filters(collected, args.keyword, args.phase, args.ids)
            return bool(args.limit) and len(filtered) >= args.limit

        max_pages = None
        if not filters_active:
            max_pages = max(1, (core.safe_int(args.limit, 1) + PAGE_SIZE - 1) // PAGE_SIZE)
        try:
            fetched, _total = fetch_contest_pages(client, console, max_pages=max_pages,
                                                  workers=args.workers, on_page=on_page)
        except ApiError as exc:
            client.close()
            return core.report_error(console, exc)
        if not collected:
            collected = fetched
        for contest in collected:
            store.upsert_contest(username, contest, None)
        try:
            store.save_progress()
        except StoreError as exc:
            console.warn("列表已显示，但未能写入本地缓存：%s" % exc.message)
        contests = store.all_contests(username)
        source = "实时请求"
    client.close()

    contests = _apply_filters(contests, args.keyword, args.phase, args.ids)
    console.title("比赛列表", note="共 %d 场" % len(contests))
    console.kv("账号", username, value_color="bgreen")
    console.kv("数据来源", source)
    console.blank()
    if not contests:
        console.warn("没有匹配的比赛")
        console.muted("可试试：hydrooj contests --live  （从服务器刷新）")
        return 0

    rows = []
    total_done = total_problems = 0
    phase_count = {"not_started": 0, "running": 0, "ended": 0}
    narrow = console.width < 96
    for contest in core.limit_take(contests, args.limit):
        phase = core.contest_phase(contest.get("startTime"), contest.get("endTime"),
                                   contest.get("phase"))
        phase_count[phase] = phase_count.get(phase, 0) + 1
        problems = contest.get("problems") or []
        if problems:
            done = sum(1 for p in problems if p.get("state") == STATE_DONE)
            total_done += done
            total_problems += len(problems)
            prog = "%s %d/%d" % (console.bar(done, len(problems), width=8), done, len(problems))
        elif contest.get("syncedAt"):
            prog = console.paint("0 题", "grey")
        else:
            prog = console.paint("未同步", "grey")
        if narrow:
            rows.append([
                (console.phase_pill(phase),),
                (short_id(contest.get("id")), "grey"),
                (core.truncate(contest.get("name") or "", 28),),
                (core.fmt_date(contest.get("startTime")), "grey"),
                (prog,),
            ])
        else:
            rows.append([
                (console.phase_pill(phase),),
                (short_id(contest.get("id")), "grey"),
                (core.truncate(contest.get("name") or "", 26),),
                (str(contest.get("mode") or "-"), "magenta"),
                (core.fmt_date(contest.get("startTime")), "grey"),
                (core.fmt_date(contest.get("endTime")), "grey"),
                (prog,),
            ])
    if narrow:
        console.table(["状态", "短 ID", "比赛名称", "开始", "已完成"], rows,
                      aligns=["center", "right", "left", "left", "left"])
    else:
        console.table(["状态", "短 ID", "比赛名称", "规则", "开始", "结束", "已完成"],
                      rows, aligns=["center", "right", "left", "center", "left", "left", "left"])
    if core.limit_applies(contests, args.limit):
        console.muted("仅显示前 %d 场，可用 --limit 调整（当前共 %d 场）" % (args.limit, len(contests)))
    console.blank()
    console.out("  %s 未开始 %d · 进行中 %d · 已结束 %d" % (
        console.paint("状态", "bcyan", bold=True),
        phase_count.get("not_started", 0), phase_count.get("running", 0),
        phase_count.get("ended", 0)))
    if total_problems:
        console.out("  %s %d/%d 题已完成  ·  %s" % (
            console.paint("总计", "bcyan", bold=True), total_done, total_problems,
            console.bar(total_done, total_problems, width=30)))
    console.muted("查看题目：hydrooj problems <ID 或短 ID>    归档：hydrooj archive <ID>")
    console.muted("提示：表格里的「短 ID」是完整 ID 的前 8 位，输入时用完整 ID 或唯一前缀")
    return 0


def _select_contests(store: Store, username: str, console: Console, specs=None,
                     keyword=None, phase=None) -> list:
    contests = store.all_contests(username)
    if specs:
        picked = []
        for spec in specs:
            contest = _pick_contest(store, username, console, spec)
            if contest is not None:
                picked.append(contest)
            else:
                console.warn("忽略无法匹配的比赛：%s" % spec)
        contests = picked
    return _apply_filters(contests, keyword, phase)


# ---------------------------------------------------------------------------
# 命令：problems / mark
# ---------------------------------------------------------------------------
def cmd_problems(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client, online=False)
    except ApiError as exc:
        return core.report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：hydrooj login")
        return 1

    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        client.close()
        return 1
    entry = store.contest_entry(username, contest.get("id")) or store.upsert_contest(
        username, contest, None)
    entry = _ensure_problems(store, username, console, entry, client, refresh=args.refresh)
    client.close()
    if entry is None:
        return 1

    problems = list(entry.get("problems") or [])
    if args.state:
        wanted = {(STATE_ALIASES.get(str(s).lower(), str(s).lower())) for s in args.state}
        problems = [p for p in problems if (p.get("state") or STATE_TODO) in wanted]

    if args.json:
        console.out(json.dumps({"contest": entry, "problems": problems},
                               ensure_ascii=False, indent=2, default=str))
        return 0

    pids_by_order = {p.get("displayOrder"): p.get("problemId") for p in problems}
    core.print_contest_header(console, entry, store.api_base,
                              link_builder=link_builder_for(store.api_base, pids_by_order),
                              note="#%s" % short_id(entry.get("id")))
    console.blank()
    if not problems:
        console.warn("该比赛没有题目（或已被 --state 过滤掉）")
        if entry.get("syncedAt"):
            console.muted("上次同步：%s（--refresh 可重新拉取）"
                          % core.human_age(entry.get("syncedAt")))
        if not entry.get("attended"):
            console.muted("如果是受限比赛，需要先在网页上报过名才能看到题目")
        return 0

    rows = []
    counts = {STATE_TODO: 0, STATE_DOING: 0, STATE_DONE: 0}
    narrow = console.width < 96
    for problem in problems:
        state = problem.get("state") or STATE_TODO
        counts[state] = counts.get(state, 0) + 1
        notes = []
        if problem.get("bestStatus"):
            notes.append(str(problem["bestStatus"]))
        elif problem.get("submitted"):
            notes.append("已提交")
        cells = [
            (console.state_pill(state),),
            (str(problem.get("displayOrder", "")), "grey"),
            (problem.get("title") or "",),
        ]
        if args.tags:
            cells.append((" ".join("#" + tag for tag in problem.get("tags") or []), "magenta"))
        cells.append((str(problem.get("problemId", "")), "grey"))
        if not narrow:
            cells.append((" · ".join(notes) if notes else "", "bcyan" if notes else "grey"))
            cells.append((core.human_age(problem.get("stateUpdatedAt")), "grey"))
        rows.append(cells)

    headers = ["状态", "题号", "题目名称"]
    aligns = ["center", "right", "left"]
    if args.tags:
        headers.append("标签")
        aligns.append("left")
    headers.append("题目 ID")
    aligns.append("right")
    if not narrow:
        headers.extend(["备注", "更新"])
        aligns.extend(["left", "left"])
    console.table(headers, rows, aligns=aligns)

    total = len(problems)
    console.blank()
    console.out("  %s %d/%d 已完成  ·  %s" % (
        console.paint("本场进度", "bcyan", bold=True), counts[STATE_DONE], total,
        console.bar(counts[STATE_DONE], total, width=28)))
    core._print_state_legend(console)
    console.blank()
    cid = entry.get("id")
    console.muted("标记完成：hydrooj mark %s 1        （默认标记为已完成，可一次多题）" % cid)
    console.muted("网页地址：%s" % contest_url(store.api_base, cid))
    console.muted("本地归档：hydrooj archive %s" % cid)
    if args.open:
        url = contest_url(store.api_base, cid)
        console.info("正在打开 %s" % url)
        webbrowser.open(url)
    return 0


def cmd_mark(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client, online=False)
    except ApiError as exc:
        return core.report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：hydrooj login")
        return 1

    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        client.close()
        return 1
    entry = store.contest_entry(username, contest.get("id")) or store.upsert_contest(
        username, contest, None)
    entry = _ensure_problems(store, username, console, entry, client, refresh=args.refresh)
    client.close()
    if entry is None:
        return 1

    specs, state_token = core._split_state_tokens(args.problems)
    if not specs:
        console.error("请至少指定一题，例如：hydrooj mark %s 1" % entry.get("id"))
        console.muted("可用 `hydrooj problems %s` 查看题号" % entry.get("id"))
        return 1

    explicit = args.state or state_token
    if args.clear:
        new_state = None
    elif explicit:
        key = str(explicit).strip().lower()
        if key not in STATE_ALIASES:
            console.error("无法识别的状态：%s" % explicit)
            console.muted("可用状态：todo / doing / done，或：未完成 / 正在做 / 已完成")
            return 1
        new_state = STATE_ALIASES[key]
    else:
        new_state = DEFAULT_MARK_STATE

    changes, missing = [], []
    for spec in specs:
        problem = core.resolve_problem(entry, spec)
        if problem is None:
            missing.append(str(spec))
            continue
        current = problem.get("state") or STATE_TODO
        if current not in STATE_ORDER:
            current = STATE_TODO
        target = (STATE_ORDER[(STATE_ORDER.index(current) + 1) % len(STATE_ORDER)]
                  if args.next else new_state)
        try:
            store.set_state(username, entry.get("id"), problem.get("problemId"), target)
        except KeyError:
            missing.append(str(spec))
            continue
        changes.append((problem, current, target))

    if changes:
        try:
            store.save_progress()
        except StoreError as exc:
            console.warn("标记已更新，但未能保存：%s" % exc.message)

    console.title("更新做题标记", note="%s · %s" % (entry.get("name") or "", short_id(entry.get("id"))))
    if changes:
        console.blank()
        rows = []
        for problem, current, target in changes:
            rows.append([
                (str(problem.get("displayOrder")), "grey"),
                (problem.get("title") or "",),
                (console.state_pill(current),),
                (console.state_pill(target) if target else console.paint("未标记", "grey"),),
            ])
        console.table(["题号", "题目", "原状态", "新状态"], rows,
                      aligns=["right", "left", "center", "center"])
        label = STATE_LABEL.get(new_state, "未标记") if not args.next else "按状态循环"
        console.ok("已保存 %d 题（%s）→ %s" % (len(changes), label, store.progress_path))
    if missing:
        console.error("找不到题目：%s" % "、".join(missing))
        console.muted("可用 `hydrooj problems %s` 查看题号列表" % entry.get("id"))

    problems = entry.get("problems") or []
    done = sum(1 for p in problems if p.get("state") == STATE_DONE)
    if problems:
        console.out("  %s %d/%d  ·  %s" % (
            console.paint("本场进度", "bcyan"), done, len(problems),
            console.bar(done, len(problems), width=28)))
    if missing and not changes:
        return 1
    return 0


# ---------------------------------------------------------------------------
# 命令：stats
# ---------------------------------------------------------------------------
def cmd_stats(args, store: Store, console: Console) -> int:
    """完全本地统计，不联网（秒出）。"""
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据")
        console.muted("请先运行：hydrooj login 然后 hydrooj sync")
        return 1

    contests = _select_contests(store, username, console, args.contest, args.keyword, args.phase)
    if args.contest and not contests:
        console.error("没有匹配的比赛")
        console.muted("可用 `hydrooj contests` 查看本地比赛 ID")
        return 1
    stats = core.collect_stats(contests)
    if args.json:
        console.out(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        return 0

    overall = stats["overall"]
    total = overall["problems"]
    done, doing, todo = overall[STATE_DONE], overall[STATE_DOING], overall[STATE_TODO]

    console.title("做题情况统计", note=username)
    console.kv("比赛数量", "%d 场（已同步题目 %d 场）" % (overall["contests"], overall["synced"]))
    console.kv("题目总数", "%d 题" % total)
    console.blank()
    if total == 0:
        console.warn("还没有题目数据，先运行 `hydrooj sync` 同步吧")
        return 0

    rate = done / float(total) * 100
    console.out("  %s  %s  %s" % (
        console.paint("总完成度", "bcyan", bold=True),
        console.bar(done, total, width=40),
        console.paint("%.1f%%" % rate, "bgreen", bold=True)))
    console.blank()
    for state, count in ((STATE_DONE, done), (STATE_DOING, doing), (STATE_TODO, todo)):
        pct = count / float(total) * 100
        console.out("  %s %s %5d 题  %6.1f%%" % (
            console.state_glyph(state),
            core.pad_display(console.paint(STATE_LABEL[state],
                                           core.STATE_COLOR.get(state, "grey"), bold=True), 6),
            count, pct))
    buckets = core._activity_buckets(contests, 7)
    values = [b[1] for b in buckets]
    console.blank()
    console.out("  %s  %s  %s" % (
        core.pad_display(console.paint("最近 7 天", "grey"), 10),
        console.paint(core._sparkline(values), "bcyan"),
        console.paint("共 %d 次标记 · %s ~ %s" % (
            sum(values), buckets[0][0].strftime("%m-%d"), buckets[-1][0].strftime("%m-%d")),
            "grey")))

    rows = []
    ranked = sorted(stats["per_contest"], key=lambda r: r["total"], reverse=True)
    narrow = console.width < 104
    for row in core.limit_take(ranked, args.limit):
        contest = row["contest"]
        phase = core.contest_phase(contest.get("startTime"), contest.get("endTime"),
                                   contest.get("phase"))
        if row["total"]:
            prog = "%s %3.0f%%" % (console.bar(row[STATE_DONE], row["total"], width=6),
                                   row[STATE_DONE] * 100.0 / row["total"])
        elif contest.get("syncedAt"):
            prog = console.paint("0 题", "grey")
        else:
            prog = console.paint("未同步", "grey")
        cells = [
            (console.phase_pill(phase),),
            (short_id(contest.get("id")), "grey"),
            (core.truncate(contest.get("name") or "", 26),),
            (str(row["total"]),),
            (str(row[STATE_DONE]), "bgreen" if row[STATE_DONE] else "grey"),
            (str(row[STATE_DOING]), "yellow" if row[STATE_DOING] else "grey"),
            (str(row[STATE_TODO]), "grey"),
            (prog,),
        ]
        if narrow:
            cells = [cells[0], cells[1], cells[2], cells[3], cells[4], cells[7]]
        rows.append(cells)
    if rows:
        console.blank()
        console.section("按比赛统计")
        if narrow:
            console.table(["状态", "短 ID", "比赛名称", "题目", "已完成", "进度"], rows,
                          aligns=["center", "right", "left", "right", "right", "left"])
        else:
            console.table(["状态", "短 ID", "比赛名称", "题目", "已完成", "正在做", "未完成", "进度"],
                          rows, aligns=["center", "right", "left", "right", "right",
                                        "right", "right", "left"])
        if core.limit_applies(ranked, args.limit):
            console.muted("仅显示前 %d 场，可用 --limit 调整（当前共 %d 场）"
                          % (args.limit, len(ranked)))

    if stats["recent"] and not args.no_recent:
        console.blank()
        console.section("最近标记")
        recent_rows = []
        for ts, contest, problem in stats["recent"][: args.recent]:
            recent_rows.append([
                (console.state_pill(problem.get("state")),),
                (core.human_age(ts), "grey"),
                (core.truncate(contest.get("name") or "", 22),),
                ("%s. %s" % (problem.get("displayOrder"), problem.get("title") or ""),),
            ])
        console.table(["状态", "时间", "比赛", "题目"], recent_rows,
                      aligns=["center", "left", "left", "left"])
    console.muted("提示：hydrooj mark <比赛ID> <题号> 直接把题目标记为已完成")
    return 0


# ---------------------------------------------------------------------------
# 命令：open / export
# ---------------------------------------------------------------------------
def cmd_open(args, store: Store, console: Console) -> int:
    _ensure_base(store)
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据，请先登录并同步")
        return 1
    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        return 1

    url = contest_url(store.api_base, contest.get("id"))
    if args.problem:
        entry = store.contest_entry(username, contest.get("id")) or {}
        problem = core.resolve_problem(entry, args.problem)
        if problem is None:
            console.error("找不到题目：%s" % args.problem)
            console.muted("可用 `hydrooj problems %s` 查看题号" % contest.get("id"))
            return 1
        url = problem_url(store.api_base, contest.get("id"), problem.get("problemId"))
    console.info(url)
    if not args.print_only:
        webbrowser.open(url)
    return 0


def cmd_export(args, store: Store, console: Console) -> int:
    _ensure_base(store)
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据，请先登录并同步")
        return 1
    contests = _select_contests(store, username, console, args.contest, args.keyword, args.phase)
    if args.contest and not contests:
        console.error("没有匹配的比赛")
        console.muted("可用 `hydrooj contests` 查看本地比赛 ID")
        return 1

    fmt = args.format
    buffer = io.StringIO()
    if fmt == "json":
        payload = {"user": username, "exportedAt": int(time.time()), "site": store.api_base,
                   "contests": [{"id": c.get("id"), "name": c.get("name"),
                                 "mode": c.get("mode"), "startTime": c.get("startTime"),
                                 "endTime": c.get("endTime"),
                                 "problems": c.get("problems") or []} for c in contests]}
        buffer.write(json.dumps(payload, ensure_ascii=False, indent=2))
        buffer.write("\n")
    elif fmt == "csv":
        writer = csv.DictWriter(buffer, fieldnames=[
            "contest_id", "contest_name", "display_order", "problem_id",
            "problem_title", "tags", "state", "state_label", "updated_at"])
        writer.writeheader()
        for row in core.iter_rows(contests):
            writer.writerow(row)
    else:
        buffer.write("# Hydro OJ 做题记录 · %s\n\n" % username)
        buffer.write("导出时间：%s  \n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        buffer.write("站点：%s  \n" % store.api_base)
        buffer.write("统计口径：本地手动标记（未完成 / 正在做 / 已完成）\n\n")
        for contest in contests:
            problems = contest.get("problems") or []
            done = sum(1 for p in problems if p.get("state") == STATE_DONE)
            phase = core.contest_phase(contest.get("startTime"), contest.get("endTime"),
                                       contest.get("phase"))
            buffer.write("## %s (#%s) — %d/%d · %s\n\n" % (
                contest.get("name") or "", contest.get("id"), done, len(problems),
                core.PHASE_LABEL.get(phase, "-")))
            buffer.write("| 题号 | 题目 | 状态 | 标签 | 题目 ID |\n|---:|---|---|---|---:|\n")
            for problem in problems:
                state = problem.get("state") or STATE_TODO
                buffer.write("| %s | %s | %s | %s | %s |\n" % (
                    problem.get("displayOrder"),
                    str(problem.get("title") or "").replace("|", "\\|"),
                    STATE_LABEL.get(state, state),
                    ", ".join(problem.get("tags") or []) or "-",
                    problem.get("problemId")))
            buffer.write("\n")

    text = buffer.getvalue()
    if args.output and args.output != "-":
        path = os.path.abspath(args.output)
        try:
            core.atomic_write_text(path, text)
        except StoreError as exc:
            console.error(exc.message)
            if exc.hint:
                console.muted(exc.hint)
            return 1
        console.ok("已导出到 %s" % path)
        console.muted("共 %d 场比赛" % len(contests))
    else:
        console.out(text.rstrip())
    return 0


# ---------------------------------------------------------------------------
# 命令：origin（原题查询）
# ---------------------------------------------------------------------------
def problem_statement_for(store: Store, client: HydroClient, problem: dict, contest_id,
                          refresh: bool = False) -> tuple:
    """取 Hydro 题面：优先本地缓存，缺失时抓 /p/<id> 并写回缓存。"""
    pid = problem.get("problemId")
    if pid is None:
        return "", "题目缺少 ID"
    cached = None if refresh else store.load_statement(pid, STATEMENT_CACHE_TTL)
    if cached and cached.get("statement"):
        return cached["statement"], ""
    try:
        data = client.problem(pid, contest_id)
    except ApiError as exc:
        return "", exc.message
    except Exception as exc:            # noqa: BLE001
        return "", str(exc) or exc.__class__.__name__
    statement = problem_statement(data)
    if not statement:
        return "", "站点没有返回题面（可能需要报名或权限）"
    record = statement_payload(problem, data)
    try:
        store.save_statement(pid, record)
    except StoreError:
        pass
    return statement, ""


class HydroOriginHelper:
    """hydrooj 侧的原题查询适配：比赛选择 / 题目列表 / 题面抓取。"""

    tool_name = "hydrooj"

    def __init__(self, store: Store, args, console: Console):
        self.store = store
        self.args = args
        self.console = console
        self.client = client_for(store, args)

    def pick_contest(self, store, username, console, spec, quiet=False):
        return _pick_contest(store, username, console, spec)

    def entry(self, store, username, contest, args):
        cid = contest.get("id")
        self.contest_id = cid
        entry = (store.contest_entry(username, cid)
                 or store.upsert_contest(username, contest, None))
        return _ensure_problems(store, username, self.console, entry, self.client,
                                refresh=getattr(args, "refresh_problems", False))

    def statement(self, problem, refresh):
        return problem_statement_for(self.store, self.client, problem,
                                     getattr(self, "contest_id", None), refresh)

    def close(self):
        self.client.close()


def cmd_origin(args, store: Store, console: Console) -> int:
    helper = HydroOriginHelper(store, args, console)
    return core.origin_cli(args, store, console, helper)


# ---------------------------------------------------------------------------
# 命令：archive
# ---------------------------------------------------------------------------
def cmd_archive(args, store: Store, console: Console) -> int:
    if getattr(args, "offline", False):
        console.error("已启用离线模式（--offline），归档需要访问站点")
        console.muted("去掉 --offline 后重试；纯本地查看可用 contests / problems / stats")
        return 1
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据")
        console.muted("请先运行：hydrooj login 然后 hydrooj sync")
        return 1

    if not args.contest and not args.all and not (args.keyword or args.phase or args.limit):
        console.error("请指定要归档的比赛")
        console.muted("全部归档：hydrooj archive --all")
        console.muted("单场归档：hydrooj archive <比赛ID>")
        console.muted("按关键词：hydrooj archive --keyword 模拟赛")
        return 1

    if args.contest:
        targets = []
        for spec in args.contest:
            contest = _pick_contest(store, username, console, spec)
            if contest is not None:
                targets.append(contest)
        if not targets:
            return 1
    else:
        targets = _select_contests(store, username, console, None, args.keyword, args.phase)
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        console.error("没有可归档的比赛")
        console.muted("先运行 `hydrooj sync` 同步比赛数据")
        return 1

    root = os.path.abspath(os.path.expanduser(args.dir or store.archive_dir))
    document = not args.no_statements
    if args.json:
        console.progress_enabled = False
        console.quiet = True
    else:
        console.title("归档比赛与题面", note="%d 场 → %s" % (len(targets), root))
        console.kv("账号", username, value_color="bgreen")
        console.kv("题目正文", "抓取并保存" if document else "跳过（--no-statements）")
        if args.force:
            console.kv("缓存", "忽略本地缓存，强制重新抓取")
        console.blank()

    client = client_for(store, args)
    try:
        result = archive_contests(store, username, console, client, targets, root,
                                 with_statements=document, force=args.force,
                                 workers=args.workers,
                                 with_origin=bool(getattr(args, "origin", False)),
                                 origin_threshold_value=core.origin_threshold(store, args))
    except ApiError as exc:
        return core.report_error(console, exc)
    except StoreError as exc:
        console.error(exc.message)
        if exc.hint:
            console.muted(exc.hint)
        return 1
    finally:
        client.close()

    if args.json:
        payload = {
            "root": result["root"],
            "contests": [{
                "id": item["contest"].get("id"),
                "name": item["contest"].get("name"),
                "path": item["path"],
                "problems": item["problems"],
                "statements": item["statements"],
                "failed": item["failed"],
            } for item in result["results"]],
            "failures": [{"id": cid, "name": name, "error": msg}
                         for cid, name, msg in result["failures"]],
        }
        console.out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    console.blank()
    if result["results"]:
        console.section("归档结果")
        rows = []
        for item in result["results"]:
            failed = len(item["failed"])
            origin_cell = "-"
            if item.get("origins"):
                origin_cell = "%d/%d" % (item.get("origins_matched", 0), item["origins"])
            rows.append([
                (short_id(item["contest"].get("id")), "grey"),
                (item["contest"].get("name") or "",),
                (str(item["problems"]),),
                (str(item["statements"]), "bgreen" if item["statements"] else "grey"),
                (origin_cell, "bgreen" if item.get("origins_matched") else "grey"),
                (str(failed), "bred" if failed else "grey"),
                (item["dir"], "grey"),
            ])
        console.table(["短 ID", "比赛", "题目", "题面", "原题", "失败", "归档目录"], rows,
                      aligns=["right", "left", "right", "right", "right", "right",
                              "left"])
        problems = sum(item["problems"] for item in result["results"])
        statements = sum(item["statements"] for item in result["results"])
        written = sum(item["files_written"] for item in result["results"])
        unchanged = sum(item["files_unchanged"] for item in result["results"])
        console.blank()
        console.ok("已归档 %d 场比赛、%d 道题目（题面 %d 份）到 %s" % (
            len(result["results"]), problems, statements, result["root"]))
        console.muted("新增/更新 %d 个文件，%d 个文件内容未变（已跳过写入）" % (written, unchanged))
    if result["failures"]:
        console.warn("%d 场比赛归档失败：" % len(result["failures"]))
        console.table(["ID", "比赛", "错误"],
                      [[(cid, "grey"), (name or "",), (msg, "bred")]
                       for cid, name, msg in result["failures"][:10]])
    if not result["results"] and not result["failures"]:
        console.warn("没有任何比赛被归档")

    index_path = os.path.join(result["root"], "index.md")
    if os.path.isfile(index_path):
        console.blank()
        console.muted("总目录：%s" % index_path)
        console.muted("用 Obsidian / Typora / VS Code 打开上面的目录即可浏览全部存档")
        if args.open:
            core._open_path(index_path)
    return 0


# ---------------------------------------------------------------------------
# 概览面板
# ---------------------------------------------------------------------------
def cmd_dashboard(args, store: Store, console: Console) -> int:
    """不带子命令时显示的概览（默认全本地，不联网，瞬时返回）。"""
    _ensure_base(store)
    console.title("Hydro OJ 命令行工具", note="v%s" % __version__)
    username = store.cached_username()
    if getattr(args, "online", False):
        client = client_for(store, args)
        try:
            username = resolve_username(store, console, client, online=True)
            console.kv("站点连接", "正常", value_color="bgreen")
        except ApiError as exc:
            console.warn("无法连接站点：%s" % exc.message)
        finally:
            client.close()

    if username:
        has_cookie = bool(store.config.get("cookies") or store.config.get("cookie"))
        console.kv("登录账号", username if has_cookie else "%s（本地缓存）" % username,
                   value_color="bgreen" if has_cookie else "yellow")
    else:
        console.kv("登录账号", "未登录", value_color="yellow")
    console.kv("站点", store.api_base)
    console.kv("数据目录", store.data_dir)

    contests = store.all_contests(username) if username else []
    if not username:
        console.blank()
        console.muted("首次使用：hydrooj login → hydrooj sync")
    elif not contests:
        console.blank()
        console.muted("还没有本地数据，运行 `hydrooj sync` 拉取全部比赛与题目")
    else:
        stats = core.collect_stats(contests)["overall"]
        total = stats["problems"]
        console.blank()
        if total:
            console.out("  %s  %s  %s" % (
                console.paint("总完成度", "bcyan", bold=True),
                console.bar(stats[STATE_DONE], total, width=36),
                console.paint("%.1f%%" % (stats[STATE_DONE] / float(total) * 100),
                              "bgreen", bold=True)))
            console.out("  %s 已完成 %-4d %s 正在做 %-4d %s 未完成 %-4d" % (
                console.state_glyph(STATE_DONE), stats[STATE_DONE],
                console.state_glyph(STATE_DOING), stats[STATE_DOING],
                console.state_glyph(STATE_TODO), stats[STATE_TODO]))
        else:
            console.muted("本地有 %d 场比赛记录，但还没有题目数据（可试 --force 重新同步）"
                          % len(contests))

        phases = {"not_started": 0, "running": 0, "ended": 0}
        archived = 0
        for contest in contests:
            phase = core.contest_phase(contest.get("startTime"), contest.get("endTime"),
                                       contest.get("phase"))
            phases[phase] = phases.get(phase, 0) + 1
            if contest.get("archivePath"):
                archived += 1
        console.out("  %s 未开始 %d · 进行中 %d · 已结束 %d · 已归档 %d 场" % (
            console.paint("比赛", "grey"), phases.get("not_started", 0),
            phases.get("running", 0), phases.get("ended", 0), archived))

        buckets = core._activity_buckets(contests, 7)
        values = [b[1] for b in buckets]
        if sum(values):
            console.out("  %s %s 共 %d 次标记" % (
                console.paint("最近 7 天", "grey"),
                console.paint(core._sparkline(values), "bcyan"), sum(values)))

    console.blank()
    console.section("常用命令")
    console.table(["命令", "作用"], [
        [("login", "bcyan"), ("登录（用户名密码，或粘贴 Cookie）",)],
        [("sync", "bcyan"), ("同步比赛与题目（增量，默认跳过已同步）",)],
        [("contests", "bcyan"), ("查看比赛列表与完成进度",)],
        [("problems <ID>", "bcyan"), ("查看某场比赛的题目列表",)],
        [("mark <ID> <题号>", "bcyan"), ("标记完成（默认 done，可一次多题）",)],
        [("archive <ID> | --all", "bcyan"), ("把比赛首页与题面归档成本地 Markdown",)],
        [("stats", "bcyan"), ("做题情况统计（本地，秒出）",)],
    ], aligns=["left", "left"])

    if contests:
        next_steps = []
        unfinished = [c for c in contests
                      if any((p.get("state") or STATE_TODO) != STATE_DONE
                             for p in c.get("problems") or [])]
        if unfinished:
            next_steps.append("hydrooj problems %s（继续最近一场未完成的比赛）"
                              % unfinished[0].get("id"))
        if not any(c.get("archivePath") for c in contests):
            next_steps.append("hydrooj archive --all（首次本地归档）")
        if next_steps:
            console.blank()
            console.section("建议下一步")
            for step in next_steps[:2]:
                console.bullet(step, "bcyan")
    console.blank()
    console.muted("完整帮助：hydrooj --help    数据目录：%s" % store.data_dir)
    return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
EPILOG = """示例：
  登录与同步
    hydrooj login                               交互式登录（用户名密码 / Cookie）
    hydrooj login -u yangjinhua -p 密码          直接用户名密码登录
    hydrooj login --cookie "sid=...; sid.sig=..." 用浏览器 Cookie 登录
    hydrooj sync                                增量同步比赛与题目（推荐）
    hydrooj sync --full                         全量重新同步所有比赛

  浏览
    hydrooj contests                            查看比赛列表与完成进度
    hydrooj contests --phase running            只看进行中的比赛
    hydrooj problems 6a323c90                   查看比赛题目（支持 ID 前缀）
    hydrooj problems 6a323c90 --state todo      只看未完成的题目

  标记（省略状态时默认标记为「已完成」）
    hydrooj mark 6a323c90 1                     把第 1 题标记为已完成
    hydrooj mark 6a323c90 1 2 3                 一次标记第 1、2、3 题
    hydrooj mark 6a323c90 1 doing               标记为正在做
    hydrooj mark 6a323c90 1 --clear             清除标记
    hydrooj mark 6a323c90 1 --next              在 未完成→正在做→已完成 间循环

  归档与统计
    hydrooj archive 6a323c90                    把该比赛的首页与全部题面存成 Markdown
    hydrooj archive --all                       归档全部已同步的比赛
    hydrooj archive --keyword 模拟赛 --limit 5   按关键词归档
    hydrooj stats                               做题情况统计（本地，秒出）
    hydrooj export -o 做题记录.md                导出做题记录

  离线 / 数据目录
    hydrooj --offline contests                  只用本地缓存，不发任何请求
    hydrooj --data-dir D:\\ojdata login          临时指定数据目录
    set HYDROOJ_DATA_DIR=D:\\ojdata              设置默认数据目录（PowerShell：$env:HYDROOJ_DATA_DIR=...）

数据存储：%s
""" % DEFAULT_DATA_DIR


def build_parser() -> argparse.ArgumentParser:
    # 统一用 SUPPRESS 作为默认值：子解析器会把自身默认值写回命名空间，
    # 若用 None/False 作默认值，`hydrooj --api X contests` 中的 --api 会被子命令覆盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, metavar="PATH",
                        help="数据目录（默认环境变量 %s，否则 %s）" % (DATA_DIR_ENV, DEFAULT_DATA_DIR))
    common.add_argument("--api", default=argparse.SUPPRESS, metavar="URL",
                        help="站点地址（默认 %s）" % DEFAULT_API)
    common.add_argument("--timeout", type=arg_int_min(1),
                        default=argparse.SUPPRESS, metavar="N",
                        help="HTTP 超时秒数（≥1，超出 %d 会钳位；默认 30）" % TIMEOUT_MAX)
    common.add_argument("--insecure", action="store_true", default=argparse.SUPPRESS,
                        help="跳过 HTTPS 证书校验")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="打印 HTTP 请求细节")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS,
                        help="离线模式：只读本地数据，不访问站点")
    common.add_argument("--online", action="store_true", default=argparse.SUPPRESS,
                        help="强制联网校验登录状态（概览面板）")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="禁用彩色输出")
    common.add_argument("--ascii", action="store_true", default=argparse.SUPPRESS,
                        help="使用纯 ASCII 字符（兼容老终端）")
    common.add_argument("--no-progress", action="store_true", default=argparse.SUPPRESS,
                        help="不显示实时进度条")
    common.add_argument("--width", type=arg_int_min(0), default=argparse.SUPPRESS,
                        metavar="N",
                        help="输出宽度（0 = 跟随终端，其余钳到 %d-%d）"
                        % (WIDTH_MIN, WIDTH_MAX))

    parser = argparse.ArgumentParser(
        prog="hydrooj",
        description="Hydro OJ 命令行工具 —— 登录、同步比赛与题目、标记做题状态、统计进度、"
                    "把比赛与题面归档成本地 Markdown（与 nflsoj 同款前端）。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version="hydrooj %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser("login", parents=[common], help="登录（用户名密码 / Cookie）",
                       description="登录 Hydro OJ。不带参数运行时采用交互式引导。")
    p.add_argument("--cookie", help="直接提供浏览器 Cookie（如 sid=...; sid.sig=...）")
    p.add_argument("--token", help="等价于 --cookie（Hydro 没有 Bearer Token）")
    p.add_argument("-u", "--username", help="用户名")
    p.add_argument("-p", "--password", help="密码")
    p.set_defaults(handler=cmd_login, writes=True)

    p = sub.add_parser("logout", parents=[common], help="清除本地登录凭据")
    p.set_defaults(handler=cmd_logout, writes=True)

    p = sub.add_parser("whoami", parents=[common], help="显示当前登录状态（联网校验）")
    p.set_defaults(handler=cmd_whoami)

    p = sub.add_parser("config", parents=[common], help="查看 / 修改本地配置")
    p.add_argument("--set-api", metavar="URL", help="设置站点地址")
    p.add_argument("--set-archive-dir", metavar="DIR", help="设置默认归档目录")
    p.add_argument("--clear", action="store_true", help="清除登录凭据")
    p.set_defaults(handler=cmd_config, writes=True)

    p = sub.add_parser("sync", parents=[common], help="同步比赛与题目列表",
                       description="同步比赛列表与每场比赛的题目。默认增量：连续两页都已同步即停止，"
                                   "并跳过已有题目缓存的比赛。")
    p.add_argument("--keyword", help="只同步名称包含该关键词的比赛")
    p.add_argument("--contest", nargs="*", metavar="ID", help="只同步指定 ID 的比赛")
    p.add_argument("--workers", type=arg_int_min(1),
                   default=DEFAULT_WORKERS, metavar="N",
                   help="并发线程数（默认 %d，实际钳到 %d-%d）"
                   % (DEFAULT_WORKERS, WORKERS_MIN, WORKERS_MAX))
    p.add_argument("--limit", type=arg_int_min(0), metavar="N",
                   help="最多同步多少场比赛（0 = 不限制）")
    p.add_argument("--no-problems", action="store_true", help="只同步比赛列表，不拉题目")
    p.add_argument("--force", action="store_true", help="强制重新拉取已有比赛的题目")
    p.add_argument("--full", dest="incremental", action="store_false",
                   help="关闭增量模式，完整遍历所有分页")
    p.set_defaults(handler=cmd_sync, incremental=True, writes=True)

    p = sub.add_parser("contests", parents=[common], aliases=["ls"], help="列出比赛",
                       description="列出比赛（默认读本地缓存，--live 从服务器刷新）。")
    p.add_argument("--live", action="store_true", help="直接请求服务器（并写回缓存）")
    p.add_argument("--keyword", help="按名称过滤")
    p.add_argument("--phase", choices=["not_started", "running", "ended", "unknown"], help="按状态过滤")
    p.add_argument("--ids", nargs="*", metavar="ID", help="只显示指定 ID（支持唯一前缀）")
    p.add_argument("--limit", type=arg_int_min(0), default=50, metavar="N",
                   help="最多显示多少场（默认 50；0 = 全部）")
    p.add_argument("--workers", type=arg_int_min(1),
                   default=DEFAULT_WORKERS, metavar="N",
                   help="--live 时的并发线程数（默认 %d）" % DEFAULT_WORKERS)
    p.set_defaults(handler=cmd_contests)

    p = sub.add_parser("problems", parents=[common], aliases=["ps"], help="列出某场比赛的题目",
                       description="列出某场比赛的题目与本地标记。默认读缓存，必要时才联网补拉。")
    p.add_argument("contest", help="比赛 ID（或唯一前缀 / 名称关键词）")
    p.add_argument("--refresh", action="store_true", help="强制从服务器刷新题目列表")
    p.add_argument("--state", nargs="*", metavar="STATE",
                   help="按状态过滤：todo / doing / done")
    p.add_argument("--tags", action="store_true", help="显示题目标签")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.add_argument("--open", action="store_true", help="在浏览器中打开该比赛")
    p.set_defaults(handler=cmd_problems)

    p = sub.add_parser("mark", parents=[common], aliases=["m"], help="标记题目状态（默认已完成）",
                       description="标记题目状态。省略状态时默认标记为「已完成」，"
                                   "可一次标记多道题；末尾用字母/中文别名指定状态（todo/doing/done）。")
    p.add_argument("contest", help="比赛 ID（或唯一前缀 / 名称关键词）")
    p.add_argument("problems", nargs="+", metavar="题号",
                   help="题号（可多个）；也可写成 `题号 状态`，如 `3 doing`")
    p.add_argument("--state", metavar="STATE", help="todo / doing / done（等价于末尾写状态）")
    p.add_argument("--clear", action="store_true", help="清除标记")
    p.add_argument("--next", action="store_true", help="在 未完成→正在做→已完成 间循环")
    p.add_argument("--refresh", action="store_true", help="先刷新题目列表")
    p.set_defaults(handler=cmd_mark, writes=True)

    p = sub.add_parser("stats", parents=[common], aliases=["st"], help="做题情况统计（本地）")
    p.add_argument("--contest", nargs="*", metavar="SPEC", help="只统计指定比赛（ID 或名称）")
    p.add_argument("--keyword", help="按比赛名称过滤")
    p.add_argument("--phase", choices=["not_started", "running", "ended", "unknown"], help="按状态过滤")
    p.add_argument("--limit", type=arg_int_min(0), default=20, metavar="N",
                   help="表格最多显示行数（默认 20；0 = 全部）")
    p.add_argument("--recent", type=arg_int_min(0), default=8, metavar="N",
                   help="最近标记条数（默认 8；0 = 不显示）")
    p.add_argument("--no-recent", action="store_true", help="不显示最近标记")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    p.set_defaults(handler=cmd_stats)

    p = sub.add_parser("open", parents=[common], aliases=["o"], help="在浏览器中打开比赛 / 题目")
    p.add_argument("contest", help="比赛 ID（或唯一前缀 / 名称关键词）")
    p.add_argument("problem", nargs="?", help="题号（可选）")
    p.add_argument("--print-only", action="store_true", help="只打印链接，不打开浏览器")
    p.set_defaults(handler=cmd_open)

    p = sub.add_parser("export", parents=[common], help="导出做题记录（本地）")
    p.add_argument("--format", choices=["md", "csv", "json"], default="md",
                   help="导出格式（默认 md）")
    p.add_argument("-o", "--output", metavar="FILE", help="输出文件（默认打印到屏幕；- 代表屏幕）")
    p.add_argument("--contest", nargs="*", metavar="SPEC", help="只导出指定比赛")
    p.add_argument("--keyword", help="按比赛名称过滤")
    p.add_argument("--phase", choices=["not_started", "running", "ended", "unknown"], help="按状态过滤")
    p.set_defaults(handler=cmd_export)

    p = sub.add_parser("origin", parents=[common], aliases=["orig", "src"],
                       help="查询题目原题（送入 CPRet，按 VJudge 题号缓存）",
                       description="把题目正文送入 https://cpret.online/ 做相似度检索；"
                                   "最高相似度 > 阈值（默认 %.2f）时判为命中，"
                                   "并记录成 VJudge 题号（形如 洛谷-P1000、CodeForces-1234A）。"
                                   % core.ORIGIN_THRESHOLD_DEFAULT)
    p.add_argument("contest", help="比赛 ID（短 ID / 唯一前缀 / 名称关键词）")
    p.add_argument("problems", nargs="*", metavar="题号",
                   help="只查询这些题（题号或题目 ID，可多个；省略则整场）")
    p.add_argument("--threshold", type=core.arg_float_range(0.0, 1.0), metavar="F",
                   default=core.ORIGIN_THRESHOLD_DEFAULT,
                   help="相似度阈值，严格大于才算命中（默认 %.2f）"
                   % core.ORIGIN_THRESHOLD_DEFAULT)
    p.add_argument("--top", type=core.arg_int_min(1), default=3, metavar="N",
                   help="JSON 里保留几个候选（默认 3）")
    p.add_argument("--limit", type=core.arg_int_min(0), default=0, metavar="N",
                   help="最多查询多少题（默认 0 = 全部）")
    p.add_argument("--workers", type=core.arg_int_min(1),
                   default=core.ORIGIN_WORKERS_DEFAULT, metavar="N",
                   help="并发查询数（默认 %d，内部上限 8）"
                   % core.ORIGIN_WORKERS_DEFAULT)
    p.add_argument("--refresh", action="store_true",
                   help="忽略缓存，重新抓题面并重新查询 CPRet")
    p.add_argument("--refresh-problems", action="store_true",
                   help="先重新同步该比赛的题目列表")
    p.add_argument("--json", action="store_true", help="以 JSON 输出查询结果")
    p.set_defaults(handler=cmd_origin, writes=True)

    p = sub.add_parser("archive", parents=[common], aliases=["arc"],
                       help="把比赛首页与题面归档成本地 Markdown",
                       description="把比赛首页（说明 / 题目列表）与每道题的题面保存为本地 "
                                   "Markdown，并生成总目录。题面会缓存，重复运行只写有变化的文件。")
    p.add_argument("contest", nargs="*", metavar="SPEC", help="比赛 ID（或唯一前缀 / 名称关键词）")
    p.add_argument("--all", action="store_true", help="归档本地全部已同步的比赛")
    p.add_argument("--keyword", help="按比赛名称过滤（可单独使用）")
    p.add_argument("--phase", choices=["not_started", "running", "ended", "unknown"], help="按状态过滤")
    p.add_argument("--limit", type=arg_int_min(0), metavar="N",
                   help="最多归档多少场比赛（0 = 不限制）")
    p.add_argument("--dir", metavar="DIR", help="归档目录（默认 %s）" % "<数据目录>/archive")
    p.add_argument("--no-statements", action="store_true", help="只存比赛首页，不抓题目正文")
    p.add_argument("--origin", action="store_true",
                   help="归档时顺带查询原题（送入 CPRet，结果写进题面与 progress.json）")
    p.add_argument("--threshold", type=core.arg_float_range(0.0, 1.0),
                   default=core.ORIGIN_THRESHOLD_DEFAULT, metavar="F",
                   help="配合 --origin 的相似度阈值（默认 %.2f）"
                   % core.ORIGIN_THRESHOLD_DEFAULT)
    p.add_argument("--force", action="store_true", help="忽略本地题面缓存，强制重新抓取")
    p.add_argument("--workers", type=arg_int_min(1),
                   default=DEFAULT_WORKERS, metavar="N",
                   help="并发抓取题面的线程数（默认 %d）" % DEFAULT_WORKERS)
    p.add_argument("--open", action="store_true", help="归档完成后打开总目录")
    p.add_argument("--json", action="store_true", help="以 JSON 输出归档结果")
    p.set_defaults(handler=cmd_archive, writes=True)

    return parser


def main(argv=None) -> int:
    ansi_ok = core.enable_windows_ansi()
    core.prepare_streams()
    console = None
    try:
        parser = build_parser()
        args = parser.parse_args(argv)

        ascii_only = bool(getattr(args, "ascii", False))
        if not ascii_only:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            try:
                "─│┌✔○◐→█░…".encode(encoding)
            except (LookupError, UnicodeEncodeError):
                ascii_only = True

        color = not bool(getattr(args, "no_color", False))
        if "NO_COLOR" in os.environ:          # 该变量「存在即生效」（含空值）
            color = False
        if os.environ.get("TERM", "").strip().lower() == "dumb":
            color = False
        try:
            if not sys.stdout.isatty():
                color = False
        except Exception:
            color = False
        if os.name == "nt" and color and not ansi_ok:
            color = False

        console = Console(color=color, ascii_only=ascii_only,
                          width=getattr(args, "width", None),
                          progress=not getattr(args, "no_progress", False))
        data_dir = resolve_data_dir(getattr(args, "data_dir", None))
        store = Store(data_dir)
        store.load()
        core.report_store_problems(store, console)
        # --api 只覆盖本次运行的地址（login / config 才会写入配置）
        store.api_override = (getattr(args, "api", None) or "").rstrip("/")
        # Store 是通用实现，默认站点地址属于 nflsoj；补齐 Hydro 的默认地址
        if not store.api_override and not store.config.get("baseUrl"):
            store.api_override = DEFAULT_API

        # 需要写盘的命令先探一次数据目录，避免做完网络请求才在最后一步崩掉
        if getattr(args, "writes", False) and not store.writable():
            console.error("数据目录不可写：%s" % store.data_dir)
            console.muted("该目录可能只读、被其他程序占用，或当前终端运行在受限沙箱中")
            console.muted("换一个可写目录后重试：")
            console.muted("hydrooj --data-dir <目录> %s" % (getattr(args, "command", "") or ""),
                          indent=8)
            console.muted("或设置环境变量 %s 指向可写目录" % DATA_DIR_ENV)
            console.muted("只读命令（contests / problems / stats / export / open）不受影响")
            return 1

        handler = getattr(args, "handler", None) or cmd_dashboard
        return handler(args, store, console)
    except SystemExit:
        raise
    except ApiError as exc:
        return core.report_error(console or Console(color=False), exc)
    except StoreError as exc:
        active = console or Console(color=False)
        active.error(exc.message)
        if exc.hint:
            active.muted(exc.hint)
        return 1
    except KeyboardInterrupt:
        active = console or Console(color=False)
        active.blank()
        active.warn("已取消")
        return 130
    except BrokenPipeError:
        core.silence_broken_pipe()
        return 0
    except Exception as exc:                     # noqa: BLE001 - 兜底：不再向用户抛 traceback
        active = console or Console(color=False)
        active.error("内部错误：%s: %s" % (type(exc).__name__, exc))
        if os.environ.get("HYDROOJ_TRACEBACK") or getattr(locals().get("args", None),
                                                          "debug", False):
            import traceback
            traceback.print_exc()
        else:
            active.muted("这是工具自身的缺陷；加 --debug 或设置 HYDROOJ_TRACEBACK=1 可查看完整堆栈")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        core.silence_broken_pipe()
        sys.exit(0)
