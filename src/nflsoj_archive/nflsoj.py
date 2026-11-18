#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NFLSOJ (nflsoi.cc) 命令行工具。

功能：
  * 使用浏览器 Cookie / Token / 用户名密码登录
  * 爬取全部比赛以及每场比赛的题目列表（连接复用 + 并发 + 增量同步）
  * 把任意一题标记为 未完成 / 正在做 / 已完成（默认直接标记为已完成）
  * 以美观的终端界面统计做题情况
  * 把比赛首页与题目正文归档成本地 Markdown 文档

依赖：仅使用 Python 标准库（Python 3.9+；本项目在 3.14 上开发与验证）。

站点后端说明（通过分析前端 bundle 得到）：
  * 站点是 SPA（Lyrio 前端），数据全部来自 JSON REST API：<base>/api/...
  * 鉴权方式为 `Authorization: Bearer <token>`（token 由 /api/auth/login 返回，
    网页端把它存在 localStorage 的 "session-swr" 里）。
  * GET  /api/auth/getSessionInfo                 -> {userMeta, isManager, ...}
  * POST /api/auth/login  {username,password}     -> {token, username, userId} | {error}
  * GET  /api/contest/getContestList?skipCount&takeCount&keyword&returnCount
                                                  -> {contests:[...], count}
  * GET  /api/contest/getContest?contestId&description&problemInfos&announcements
                                                  -> {meta:{...}, problemInfos:[...],
                                                      description, announcements, permissions}
  * GET  /api/problem/getProblem?id&statement      -> {meta:{id,title}, statement, statistics, ...}
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import errno
import gzip
import http.client
import io
import json
import os
import pathlib
import re
import socket
import ssl
import stat
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
import zlib
from concurrent.futures import (FIRST_COMPLETED, ThreadPoolExecutor, as_completed,
                                wait)
from functools import lru_cache

__version__ = "1.2.0"

DEFAULT_API = "https://nflsoi.cc:20035"
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".nflsoj")
DATA_DIR_ENV = "NFLSOJ_DATA_DIR"
CONTEST_PAGE_SIZE = 20          # 与站点 serverPreference.pagination.contestList 保持一致
STATEMENT_CACHE_TTL = 30 * 86400   # 题目正文本地缓存有效期（秒）
DEFAULT_WORKERS = 8             # 实测 8 并发最稳定（16 并发反而被站点拖慢）

# 写盘失败时统一给出的可操作提示
WRITE_HINT = ("该位置不可写：可能位于只读目录、被其他程序占用，"
              "或当前终端运行在受限沙箱中。可换一个可写目录："
              "nflsoj --data-dir <目录>，或设置环境变量 %s" % DATA_DIR_ENV)

# ---------------------------------------------------------------------------
# 状态定义
# ---------------------------------------------------------------------------
STATE_TODO, STATE_DOING, STATE_DONE = "todo", "doing", "done"
STATE_ORDER = [STATE_TODO, STATE_DOING, STATE_DONE]
STATE_LABEL = {STATE_TODO: "未完成", STATE_DOING: "正在做", STATE_DONE: "已完成"}
STATE_COLOR = {STATE_TODO: "grey", STATE_DOING: "yellow", STATE_DONE: "green"}
DEFAULT_MARK_STATE = STATE_DONE     # `mark` 省略状态时默认标记为已完成

# 用户输入 -> 内部状态（None 表示清除标记）
STATE_ALIASES = {
    "todo": STATE_TODO, "0": STATE_TODO, "未完成": STATE_TODO, "未做": STATE_TODO,
    "没做": STATE_TODO, "new": STATE_TODO, "undone": STATE_TODO,
    "none": None, "clear": None, "reset": None, "取消": None, "rm": None,
    "doing": STATE_DOING, "1": STATE_DOING, "正在做": STATE_DOING, "在做": STATE_DOING,
    "进行中": STATE_DOING, "inprogress": STATE_DOING, "wip": STATE_DOING, "ing": STATE_DOING,
    "done": STATE_DONE, "2": STATE_DONE, "已完成": STATE_DONE, "完成": STATE_DONE,
    "已做": STATE_DONE, "ac": STATE_DONE, "ok": STATE_DONE, "过了": STATE_DONE,
}

PHASE_LABEL = {"not_started": "未开始", "running": "进行中", "ended": "已结束",
               "unknown": "时间未知"}
PHASE_COLOR = {"not_started": "blue", "running": "bgreen", "ended": "grey",
               "unknown": "yellow"}
# 站点直接给出的 phase（getContest.meta.phase）
PHASE_FROM_SERVER = {
    "PENDING": "not_started", "NOT_STARTED": "not_started", "UPCOMING": "not_started",
    "PREPARING": "not_started",
    "SUBMISSION": "running", "RUNNING": "running", "IN_PROGRESS": "running",
    "HACKING": "running", "HACK": "running",
    "ENDED": "ended", "FINISHED": "ended", "CLOSED": "ended",
}
# 256 色配色：(前景, 背景)
PILL_COLORS = {
    STATE_TODO: (250, 238),
    STATE_DOING: (221, 58),
    STATE_DONE: (231, 28),
    "not_started": (117, 237),
    "running": (231, 64),
    "ended": (245, 237),
}


# ---------------------------------------------------------------------------
# 文本宽度工具（正确处理中日韩全角字符与 ANSI 转义）
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8192)
def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def char_width(ch: str) -> int:
    return _char_width(ch)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_SPLIT = re.compile(r"(\x1b\[[0-9;]*[A-Za-z])")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色转义序列，便于按终端可见宽度计算。"""
    if "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


def len_display(text: str) -> int:
    """终端中实际占用的列数（ASCII 走快速路径）。"""
    if "\x1b" in text:
        text = strip_ansi(text)
    if text.isascii():
        return len(text)
    return sum(_char_width(c) for c in text)


def pad_display(text: str, width: int, align: str = "left") -> str:
    space = max(0, width - len_display(text))
    if align == "right":
        return " " * space + text
    if align == "center":
        left = space // 2
        return " " * left + text + " " * (space - left)
    return text + " " * space


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """按终端显示宽度截断，保留 ANSI 颜色（截断后自动补 reset）。"""
    if width <= 0:
        return ""
    if len_display(text) <= width:
        return text
    ell_w = len_display(ellipsis)
    limit = max(0, width - ell_w)
    parts = _ANSI_SPLIT.split(text)
    out, acc, opened = [], 0, False
    for part in parts:
        if not part:
            continue
        if part.startswith("\x1b["):
            out.append(part)
            opened = True
            continue
        for ch in part:
            w = _char_width(ch)
            if acc + w > limit:
                out.append(ellipsis)
                if opened:
                    out.append("\033[0m")
                return "".join(out)
            out.append(ch)
            acc += w
    if opened:
        out.append("\033[0m")
    return "".join(out)


def wrap_display(text: str, width: int, indent: str = "") -> list:
    """按显示宽度折行（保留 ANSI 颜色），用于长文本换行。"""
    if width <= 0 or len_display(text) <= width:
        return [text]
    lines, current, acc = [], [], 0
    for part in _ANSI_SPLIT.split(text):
        if not part:
            continue
        if part.startswith("\x1b["):
            current.append(part)
            continue
        for ch in part:
            w = _char_width(ch)
            if acc + w > width:
                lines.append("".join(current).rstrip())
                current = [indent] if indent else []
                acc = len_display(indent)
            current.append(ch)
            acc += w
    if current:
        lines.append("".join(current).rstrip())
    return lines or [""]


def clean_text(text) -> str:
    """清掉控制字符（换行/制表符等），但保留 ANSI 颜色序列，避免把彩色胶囊洗掉。"""
    if not text:
        return ""
    text = str(text)
    if "\x1b" not in text:
        return _CONTROL_RE.sub(" ", text).strip()
    pieces = []
    for part in _ANSI_SPLIT.split(text):
        if not part:
            continue
        if part.startswith("\x1b["):
            pieces.append(part)
        else:
            pieces.append(_CONTROL_RE.sub(" ", part))
    return "".join(pieces).strip()


def safe_int(value, default=0) -> int:
    """宽容的 int()：任何无法转换的值返回 default（不抛异常）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, OverflowError):
        return default


TIMEOUT_MIN, TIMEOUT_MAX = 1, 3600
WORKERS_MIN, WORKERS_MAX = 1, 16
WIDTH_MIN, WIDTH_MAX = 40, 200


def clamp_timeout(value, default: int = 30) -> int:
    """把超时钳到合法区间（socket 不接受负数/0，会抛 ValueError）。"""
    seconds = safe_int(value, default)
    if seconds <= 0:
        return default
    return max(TIMEOUT_MIN, min(seconds, TIMEOUT_MAX))


def clamp_workers(value, default: int = DEFAULT_WORKERS) -> int:
    workers = safe_int(value, default)
    return max(WORKERS_MIN, min(workers, WORKERS_MAX))


def arg_int_min(minimum, name: str = "该参数"):
    """生成 argparse 的 type 校验器：只要求下限（上限在使用处钳位，避免硬拒大值）。"""
    def parse(raw):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("%s 需要整数，收到 %r" % (name, raw))
        if value < minimum:
            raise argparse.ArgumentTypeError(
                "%s 不能小于 %d，收到 %d" % (name, minimum, value))
        return value
    parse.__name__ = "int_min_%d" % minimum
    return parse


# 连接类「不可恢复」错误：重试只是浪费时间，直接快速失败并给出准确提示
_FATAL_NET_ERRORS = (ConnectionRefusedError, ConnectionResetError, socket.gaierror)


def is_fatal_network_error(exc) -> bool:
    """判断是否属于重试也没用的连接错误（拒绝连接 / DNS 解析失败等）。"""
    if exc is None:
        return False
    if isinstance(exc, _FATAL_NET_ERRORS):
        return True
    text = str(exc).lower()
    for marker in ("connection refused", "actively refused", "积极拒绝",
                   "name or service not known", "nodename nor servname",
                   "getaddrinfo failed", "no address associated",
                   "temporary failure in name resolution"):
        if marker in text:
            return True
    return False


def _mask_secret(text: str) -> str:
    """把 URL / 串里的凭据掩码（用于 --debug 输出，避免泄露 token/cookie）。"""
    if not text:
        return ""
    text = re.sub(r"(?i)(token|password|passwd|pwd|sid|sid\.sig|authorization|"
                  r"access_token|session-swr)=([^&;\s]+)",
                  lambda m: "%s=***" % m.group(1), str(text))
    text = re.sub(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.=]+)", r"\1***", text)
    return text


def network_error_hint(exc, default_api: str = None) -> str:
    if is_fatal_network_error(exc):
        return "站点地址可能不正确或服务未启动；检查 --api（当前默认 %s）" % (
            default_api or DEFAULT_API)
    return "请检查网络连接与 --api 地址，或用 --timeout 调大超时"


def arg_int_range(minimum, maximum, name: str = "该参数"):
    """生成 argparse 的 type 校验器：越界直接以用法错误拒绝（退出码 2）。"""
    def parse(raw):
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("%s 需要整数，收到 %r" % (name, raw))
        if value < minimum or value > maximum:
            raise argparse.ArgumentTypeError(
                "%s 需要在 %d..%d 之间，收到 %d" % (name, minimum, maximum, value))
        return value
    parse.__name__ = "int_%d_%d" % (minimum, maximum)
    return parse


def _contest_sort_key(contest: dict):
    """比赛排序键：时间优先，其次 id；两者都容忍脏数据。"""
    start_ts, _ = parse_time(contest.get("startTime"))
    cid = contest.get("id")
    cid_key = (0, safe_int(cid, 0), "") if isinstance(cid, (int, float)) else (1, 0, str(cid or ""))
    return (start_ts or 0.0, cid_key)


def limit_take(items, limit):
    """按 --limit 取前 N 项；limit 为 0 或 None 表示「不限制」（取全部）。"""
    values = list(items)
    if not limit or limit <= 0:
        return values
    return values[:limit]


def limit_applies(items, limit) -> bool:
    """是否因为 --limit 而截断了结果（用于决定要不要提示）。"""
    return bool(limit and limit > 0 and len(items) > limit)


def safe_cache_name(value, fallback: str = "unknown") -> str:
    """把任意值变成安全的文件名片段（用于题面缓存等）。"""
    text = str(value).strip() if value is not None else ""
    if not text:
        return fallback
    if text.isdigit():
        return text
    cleaned = _INVALID_FS_CHARS.sub("-", text)
    cleaned = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", cleaned).strip("- .")
    return cleaned[:64] or fallback


# ---------------------------------------------------------------------------
# 终端渲染
# ---------------------------------------------------------------------------
def enable_windows_ansi() -> bool:
    """在 Windows 控制台启用 ANSI 转义序列（并尽量切到 UTF-8 代码页）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VT_PROCESSING
        try:
            kernel32.SetConsoleOutputCP(65001)  # 中文不乱码
        except Exception:
            pass
        return True
    except Exception:
        return False


class Console:
    """极简但足够漂亮的终端输出层（表格 / 进度条 / 彩色文字 / 交互提示）。"""

    CODES = {
        "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m", "underline": "\033[4m",
        "black": "\033[30m", "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
        "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m", "white": "\033[37m",
        "grey": "\033[90m", "bred": "\033[91m", "bgreen": "\033[92m", "byellow": "\033[93m",
        "bblue": "\033[94m", "bmagenta": "\033[95m", "bcyan": "\033[96m", "bwhite": "\033[97m",
    }

    UNICODE_GLYPHS = {
        "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "h": "─", "v": "│",
        "lt": "├", "rt": "┤", "tt": "┬", "bt": "┴", "cross": "┼",
        "bar_full": "█", "bar_empty": "░", "bar_half": "▒",
        "todo": "○", "doing": "◐", "done": "✔", "dot": "·", "arrow": "→",
        "cross_mark": "✖", "check": "✔", "bullet": "•", "ellipsis": "…",
        "mark": "▌", "star": "★", "flag": "⚑",
    }
    ASCII_GLYPHS = {
        "tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|",
        "lt": "+", "rt": "+", "tt": "+", "bt": "+", "cross": "+",
        "bar_full": "#", "bar_empty": ".", "bar_half": ":",
        "todo": "[ ]", "doing": "[~]", "done": "[x]", "dot": ".", "arrow": "->",
        "cross_mark": "[!]", "check": "[x]", "bullet": "-", "ellipsis": "...",
        "mark": ">", "star": "*", "flag": "!",
    }
    SPINNER_UNICODE = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    SPINNER_ASCII = ("|", "/", "-", "\\")

    def __init__(self, color: bool = True, ascii_only: bool = False, stream=None,
                 width=None, progress: bool = True):
        self.stream = stream or sys.stdout
        self.ascii_only = bool(ascii_only)
        self.glyph = dict(self.ASCII_GLYPHS if ascii_only else self.UNICODE_GLYPHS)
        self.spinner = self.SPINNER_ASCII if ascii_only else self.SPINNER_UNICODE
        try:
            self.isatty = bool(self.stream.isatty())
        except Exception:
            self.isatty = False
        self.color = bool(color) and not ascii_only and self.isatty
        self.supports_256 = self._detect_256()
        self.width = self._detect_width(width)
        self.progress_enabled = bool(progress) and self.isatty
        self.quiet = False           # True 时连进度提示都不打印（JSON 输出用）
        self._live = None            # 当前活动的 Progress
        self._lock = threading.RLock()

    # -- 环境探测 ---------------------------------------------------------
    @staticmethod
    def _detect_width(width=None) -> int:
        """宽度优先级：--width > 环境变量 > 终端；0 或非法表示「跟随终端」。"""
        if width is not None:
            try:
                value = int(width)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return max(WIDTH_MIN, min(value, WIDTH_MAX))
        env = (os.environ.get("NFLSOJ_WIDTH") or os.environ.get("HYDROOJ_WIDTH")
               or os.environ.get("COLUMNS"))
        if env:
            try:
                return max(WIDTH_MIN, min(int(env), WIDTH_MAX))
            except (TypeError, ValueError):
                pass
        try:
            return max(WIDTH_MIN, min(os.get_terminal_size().columns, WIDTH_MAX))
        except Exception:
            return 100

    def _detect_256(self) -> bool:
        if not self.color:
            return False
        if os.environ.get("TERM", "").strip().lower() == "dumb":
            return False
        if os.name == "nt":
            return True
        if os.environ.get("COLORTERM"):
            return True
        term = os.environ.get("TERM", "")
        return "256" in term or "truecolor" in term

    # -- 基础 -------------------------------------------------------------
    def paint(self, text, color=None, bold=False, dim=False, bg=None) -> str:
        if not self.color or not text:
            return text
        prefix = ""
        if bold:
            prefix += self.CODES["bold"]
        if dim:
            prefix += self.CODES["dim"]
        if bg is not None and self.supports_256:
            prefix += "\033[48;5;%dm" % int(bg)
        if color:
            prefix += self.CODES.get(color, "")
        return prefix + text + self.CODES["reset"] if prefix else text

    def out(self, text: str = "") -> None:
        with self._lock:
            self._erase_live()
            self._emit(str(text) + "\n")
            self._redraw_live()

    def _emit(self, text: str) -> None:
        """写出一段文本；任何编码错误都降级为可打印字符，绝不抛异常。"""
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            try:
                self.stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            except (UnicodeEncodeError, LookupError):
                self.stream.write(text.encode("ascii", "replace").decode("ascii"))
        except (ValueError, OSError):
            # 流已关闭 / 断管：静默忽略，由调用方或 main() 处理退出码
            pass

    def write(self, text: str) -> None:
        with self._lock:
            self._emit(text)
            try:
                self.stream.flush()
            except Exception:
                pass

    # -- 活动进度行管理 ---------------------------------------------------
    def _erase_live(self) -> None:
        live = self._live
        if live is not None and live.active and live.drawn:
            self.write("\r" + " " * live.line_len + "\r")
            live.drawn = False

    def _redraw_live(self) -> None:
        live = self._live
        if live is not None and live.active:
            live.draw(force=True)

    # -- 常用块 -----------------------------------------------------------
    def title(self, text: str, note: str = "") -> None:
        """顶部标题条：┌─ 标题 ──── 右侧备注 ─┐"""
        g = self.glyph
        total = min(self.width, 96)
        note_txt = (" " + note + " ") if note else ""
        note_w = len_display(note_txt)
        room = max(1, total - 3 - note_w - 2)
        inner = " " + truncate(clean_text(text), room) + " "
        pad = max(0, total - 3 - len_display(inner) - note_w)
        line = g["tl"] + g["h"] + inner + g["h"] * pad
        if note_txt:
            line += self.paint(note_txt, "grey", dim=True)
        line += g["tr"]
        self.out(self.paint(line, "bcyan", bold=True))

    def section(self, text: str, note: str = "") -> None:
        """小节标题：▌ 文本"""
        line = "  %s %s" % (self.paint(self.glyph["mark"], "bcyan", bold=True),
                            self.paint(clean_text(text), "bwhite", bold=True))
        if note:
            line += "  " + self.paint(note, "grey")
        self.out(line)

    def subtitle(self, text: str) -> None:
        self.out(self.paint("  " + clean_text(text), "bblue", bold=True))

    def muted(self, text: str, indent: int = 6) -> None:
        pad = " " * max(0, indent)
        for line in wrap_display(clean_text(text), max(10, self.width - len(pad))):
            self.out(self.paint(pad + line, "grey"))

    def hint(self, text: str) -> None:
        """下一步建议（统一样式）。"""
        self.muted("提示：" + text)

    def ok(self, text: str) -> None:
        self.out(self.paint("  " + self.glyph["check"] + " " + text, "green", bold=True))

    def info(self, text: str) -> None:
        self.out(self.paint("  " + self.glyph["arrow"] + " " + text, "cyan"))

    def warn(self, text: str) -> None:
        self.out(self.paint("  ! " + text, "yellow", bold=True))

    def error(self, text: str) -> None:
        self.out(self.paint("  " + self.glyph["cross_mark"] + " " + text, "bred", bold=True))

    def kv(self, key: str, value: str, key_width: int = 12, value_color=None) -> None:
        self.out("  %s %s" % (pad_display(self.paint(key, "grey"), key_width),
                              self.paint(str(value), value_color)))

    def bullet(self, text: str, color=None) -> None:
        self.out("   %s %s" % (self.paint(self.glyph["bullet"], color or "grey"), text))

    def rule(self, char=None) -> None:
        self.out(self.paint((char or self.glyph["h"]) * min(self.width, 96), "grey", dim=True))

    def blank(self) -> None:
        self.out()

    # -- 状态 / 阶段徽标 --------------------------------------------------
    def pill(self, text: str, kind: str) -> str:
        fg, bg = PILL_COLORS.get(kind, (250, 238))
        if not self.color:
            return "[%s]" % text
        if self.supports_256:
            return "\033[48;5;%dm\033[38;5;%dm\033[1m %s \033[0m" % (bg, fg, text)
        color = {STATE_DONE: "green", STATE_DOING: "yellow", STATE_TODO: "grey",
                 "not_started": "blue", "running": "bgreen", "ended": "grey"}.get(kind, "grey")
        return self.paint("[%s]" % text, color, bold=True)

    def state_pill(self, state) -> str:
        key = state if state in STATE_ORDER else STATE_TODO
        return self.pill(STATE_LABEL[key], key)

    def phase_pill(self, phase) -> str:
        key = phase if phase in PHASE_LABEL else "ended"
        return self.pill(PHASE_LABEL[key], key)

    def state_glyph(self, state) -> str:
        key = state if state in STATE_ORDER else STATE_TODO
        return self.paint(self.glyph[key], STATE_COLOR.get(key, "grey"), bold=(key == STATE_DONE))

    # -- 表格 -------------------------------------------------------------
    def _fit_widths(self, widths, budget):
        """按比例收缩列宽，保证总和不超过终端预算。"""
        excess = sum(widths) - budget
        if excess <= 0:
            return widths
        floors = [min(w, 8) for w in widths]
        order = sorted(range(len(widths)), key=lambda i: widths[i], reverse=True)
        while excess > 0:
            shrinkable = [i for i in order if widths[i] > floors[i]]
            if not shrinkable:
                break
            pool = sum(widths[i] - floors[i] for i in shrinkable)
            changed = False
            for i in shrinkable:
                room = widths[i] - floors[i]
                take = max(1, int(round(excess * room / pool))) if pool else 1
                take = min(take, room, excess)
                widths[i] -= take
                excess -= take
                changed = True
                if excess <= 0:
                    break
            if not changed:
                break
        return widths

    def table(self, headers, rows, aligns=None, max_width=None) -> None:
        """headers: list[str]；rows: list[list[tuple(text[, color[, bold]])]]。"""
        g = self.glyph
        if not headers:
            return
        cols = len(headers)
        aligns = list(aligns or ["left"] * cols)
        max_width = max_width or self.width
        widths = [len_display(clean_text(h)) for h in headers]
        for row in rows:
            for i in range(cols):
                if i >= len(row) or not row[i]:
                    continue
                widths[i] = max(widths[i], len_display(clean_text(row[i][0])))
        # 渲染开销：左右边框 2 + 左右各 1 空格 + 列间 " │ " 3*(cols-1)
        budget = max(0, max_width - (3 * cols + 1))
        widths = self._fit_widths(widths, budget)

        def render(cells, color_all=None, bold=False):
            parts = []
            for i in range(cols):
                cell = cells[i] if i < len(cells) else ("",)
                if not cell:
                    cell = ("",)
                text = truncate(clean_text(str(cell[0])), widths[i], self.glyph["ellipsis"])
                padded = pad_display(text, widths[i], aligns[i])
                if len(cell) > 1 and cell[1]:
                    padded = self.paint(padded, cell[1], len(cell) > 2 and bool(cell[2]))
                elif color_all:
                    padded = self.paint(padded, color_all, bold)
                parts.append(padded)
            return g["v"] + " " + (" " + g["v"] + " ").join(parts) + " " + g["v"]

        def border(left, mid, right):
            return self.paint(left + mid.join(g["h"] * (w + 2) for w in widths) + right, "grey")

        self.out(border(g["tl"], g["tt"], g["tr"]))
        self.out(render([(clean_text(h),) for h in headers], color_all="bcyan", bold=True))
        self.out(border(g["lt"], g["cross"], g["rt"]))
        for row in rows:
            self.out(render(row))
        self.out(border(g["bl"], g["bt"], g["br"]))

    # -- 进度条 -----------------------------------------------------------
    def bar(self, done: int, total: int, width: int = 24, color: str = "bgreen") -> str:
        g = self.glyph
        if total <= 0:
            return self.paint(g["bar_empty"] * width, "grey")
        ratio = max(0.0, min(1.0, done / float(total)))
        filled = int(round(ratio * width))
        return (self.paint(g["bar_full"] * filled, color) +
                self.paint(g["bar_empty"] * (width - filled), "grey"))

    # -- 交互 -------------------------------------------------------------
    def prompt(self, question: str, default: str = "", mask: bool = False) -> str:
        """带默认值的输入；Ctrl+C / Ctrl+D 抛 KeyboardInterrupt。"""
        suffix = (" [%s]" % default) if default else ""
        label = self.paint("? ", "bcyan", bold=True) + question + self.paint(suffix + "：", "grey")
        self._erase_live()
        try:
            if mask:
                try:
                    import getpass
                    value = getpass.getpass(label)
                except Exception:
                    value = input(strip_ansi(label))
            else:
                value = input(label)
        finally:
            self._redraw_live()
        value = (value or "").strip()
        return value or default

    def choose(self, question: str, options, default: str = None) -> str:
        """从若干选项中选一个；options 为 [(value, label)]。"""
        for index, (value, label) in enumerate(options, 1):
            marker = self.paint("●", "bgreen") if value == default else self.paint("○", "grey")
            self.out("    %s %s %s" % (marker, self.paint(str(index), "bcyan", bold=True), label))
        keys = {str(i): v for i, (v, _) in enumerate(options, 1)}
        while True:
            raw = self.prompt(question, default=str(default or ""))
            if raw in keys:
                return keys[raw]
            matched = [v for v, label in options if str(v).lower() == raw.lower()]
            if matched:
                return matched[0]
            self.warn("无效的选项，请输入 1-%d" % len(options))

    def confirm(self, question: str, default: bool = False) -> bool:
        raw = self.prompt(question + (" [Y/n]" if default else " [y/N]")).lower()
        if not raw:
            return default
        return raw in ("y", "yes", "是", "1", "ok")


class Progress:
    """单行活动进度（旋转指示 + 进度条 + 速率 + 预计剩余）。

    非 TTY（重定向到文件/管道）时自动降级为按百分比打点，不污染日志。
    """

    def __init__(self, console: Console, label: str, total=None, unit: str = "",
                 width: int = 22, detail_width: int = 26):
        self.console = console
        self.label = label
        self.total = total
        self.unit = unit
        self.bar_width = width
        self.detail_width = detail_width
        self.enabled = console.progress_enabled
        self.done = 0
        self.failed = 0
        self.detail = ""
        self.start = time.time()
        self.active = True
        self.drawn = False
        self.line_len = 0
        self._frame = 0
        self._last_draw = 0.0
        self._last_pct = -1
        self._lock = threading.Lock()

    # -- 内部 -------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return max(1e-6, time.time() - self.start)

    def _rate(self) -> float:
        return self.done / self.elapsed

    def _render(self) -> str:
        c = self.console
        g = c.glyph
        pct = (self.done / float(self.total) * 100.0) if self.total else 0.0
        spinner = c.spinner[self._frame % len(c.spinner)]
        bar_w = self.bar_width
        if self.total:
            filled = int(round(max(0.0, min(1.0, self.done / float(self.total))) * bar_w))
            bar = c.paint(g["bar_full"] * filled, "bgreen") + c.paint(g["bar_empty"] * (bar_w - filled), "grey")
            counter = "%d/%d" % (self.done, self.total)
        else:
            bar = c.paint(g["bar_half"] * (bar_w // 3), "bgreen") + c.paint(g["bar_empty"] * (bar_w - bar_w // 3), "grey")
            counter = str(self.done)
        bits = [c.paint(spinner, "bcyan", bold=True),
                self.label,
                bar,
                counter]
        if self.total:
            bits.append("%3d%%" % int(pct))
        else:
            bits.append("")
        if self.done:
            bits.append("%.1f/s" % self._rate())
            if self.total and self.total > self.done:
                remain = (self.total - self.done) / max(0.01, self._rate())
                bits.append("剩余 %s" % fmt_seconds(remain))
        if self.failed:
            bits.append(c.paint("失败 %d" % self.failed, "bred"))
        if self.detail:
            bits.append(c.paint(truncate(clean_text(self.detail), self.detail_width,
                                         c.glyph["ellipsis"]), "grey"))
        line = "  " + "  ".join(b for b in bits if b)
        return truncate(line, c.width - 1, c.glyph["ellipsis"])

    def draw(self, force: bool = False) -> None:
        if not self.enabled or not self.active:
            return
        with self._lock:
            self._frame += 1
            now = time.time()
            if not force and now - self._last_draw < 0.08:
                return
            self._last_draw = now
            line = self._render()
            pad = max(0, self.line_len - len_display(line))
            self.console.write("\r" + line + " " * pad)
            self.line_len = len_display(line)
            self.drawn = True

    # -- 对外 -------------------------------------------------------------
    def start_line(self) -> "Progress":
        if self.enabled:
            self.console._live = self
            self.draw(force=True)
        elif not getattr(self.console, "quiet", False):
            self.console.muted(self.label + "…")
        return self

    def update(self, advance: int = 1, total=None, detail=None, failed: bool = False) -> None:
        with self._lock:
            self.done += advance
            if failed:
                self.failed += 1
            if total is not None:
                self.total = total
            if detail is not None:
                self.detail = detail
        if self.enabled:
            self.draw()
        else:
            self._tick_non_tty()

    def _tick_non_tty(self) -> None:
        if not self.total or getattr(self.console, "quiet", False):
            return
        pct = int(self.done * 100 / max(1, self.total))
        if pct >= self._last_pct + 20 or self.done >= self.total:
            self._last_pct = pct
            self.console.muted("%s %d%% (%d/%d)" % (self.label, pct, self.done, self.total))

    def close(self, message: str = "", ok: bool = True) -> None:
        if not self.active:
            return
        self.active = False
        if self.console._live is self:
            self.console._live = None
        if self.enabled:
            self.console.write("\r" + " " * max(self.line_len, 1) + "\r")
            self.drawn = False
        if message:
            (self.console.ok if ok else self.console.warn)(message)

    def __enter__(self) -> "Progress":
        return self.start_line()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.active = False
            if self.console._live is self:
                self.console._live = None
            if self.enabled:
                self.console.write("\r" + " " * max(self.line_len, 1) + "\r")


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------
_TIME_CACHE = {}
_TIME_CACHE_MAX = 4096


def _time_cache_key(text):
    """只把可哈希的标量放进缓存；dict/list 等异常值直接判为不可解析。"""
    if isinstance(text, (str, int, float)) and not isinstance(text, bool):
        return text
    return None


def parse_time(text):
    """把站点返回的时间解析为 (epoch_seconds, datetime) 或 (None, None)。

    对任何输入都不抛异常：站点把时间序列化成对象/数组、或本地数据被手工改坏时，
    统一降级为 (None, None)，由调用方显示 "-"。
    """
    if text is None or text == "":
        return None, None
    key = _time_cache_key(text)
    if key is not None and key in _TIME_CACHE:
        return _TIME_CACHE[key]
    result = _parse_time_uncached(text)
    if key is not None:
        if len(_TIME_CACHE) >= _TIME_CACHE_MAX:
            _TIME_CACHE.clear()
        _TIME_CACHE[key] = result
    return result


def _parse_time_uncached(text):
    if isinstance(text, bool):
        return None, None
    if isinstance(text, (int, float)):
        ts = float(text)
        if ts <= 0:
            return None, None
        if ts > 1e12:               # 毫秒时间戳
            ts /= 1000.0
        try:
            return ts, _dt.datetime.fromtimestamp(ts)
        except (OverflowError, OSError, ValueError):
            return None, None
    if not isinstance(text, str):
        return None, None             # dict / list / 其他类型一律视为缺失
    raw = text.strip()
    if not raw:
        return None, None
    try:
        dt = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            ts = dt.timestamp()
        else:
            ts = dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        return ts, _dt.datetime.fromtimestamp(ts)
    except (ValueError, OSError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
        try:
            ts = _dt.datetime.strptime(raw, fmt).replace(tzinfo=_dt.timezone.utc).timestamp()
            return ts, _dt.datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            continue
    return None, None


def fmt_time(text) -> str:
    _, dt = parse_time(text)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else (str(text) if text else "-")


def fmt_date(text) -> str:
    _, dt = parse_time(text)
    return dt.strftime("%Y-%m-%d") if dt else "-"


def fmt_interval(start, end) -> str:
    """把起止时间显示成紧凑形式（同一天只显示一次日期）。"""
    s_ts, s_dt = parse_time(start)
    e_ts, e_dt = parse_time(end)
    if not s_dt and not e_dt:
        return "-"
    if s_dt and e_dt:
        if s_dt.date() == e_dt.date():
            return "%s %s ~ %s" % (s_dt.strftime("%Y-%m-%d"), s_dt.strftime("%H:%M"), e_dt.strftime("%H:%M"))
        return "%s ~ %s" % (s_dt.strftime("%Y-%m-%d %H:%M"), e_dt.strftime("%Y-%m-%d %H:%M"))
    return fmt_time(start if s_dt else end)


def fmt_seconds(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "%d 秒" % seconds
    if seconds < 3600:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    return "%d 时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)


def contest_phase(start, end, server_phase=None) -> str:
    """优先使用服务端给出的 phase；否则按时间推断。

    开始与结束时间都无法解析时返回 "unknown"（而不是误报「进行中」）。
    """
    if server_phase:
        mapped = PHASE_FROM_SERVER.get(str(server_phase).upper())
        if mapped:
            return mapped
    now = time.time()
    start_ts, _ = parse_time(start)
    end_ts, _ = parse_time(end)
    if start_ts is None and end_ts is None:
        return "unknown"
    if start_ts and now < start_ts:
        return "not_started"
    if end_ts and now > end_ts:
        return "ended"
    return "running"


def human_age(ts) -> str:
    """把时间戳/时间字符串显示成「N 分钟前」。非法值返回 "-"，绝不抛异常。"""
    if isinstance(ts, str) and ts.strip():
        parsed, _ = parse_time(ts)
        if parsed is None:
            return "-"
        ts = parsed
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "-"
    if ts <= 0:
        return "-"
    if ts > 1e12:
        ts /= 1000.0
    try:
        delta = max(0, int(time.time() - ts))
    except (OverflowError, OSError, ValueError):
        return "-"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    if delta < 86400 * 2:
        return "昨天"
    if delta < 86400 * 30:
        return "%d 天前" % (delta // 86400)
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "-"


def fmt_local_iso(ts) -> str:
    """本地时区的 ISO 8601 时间（带 +08:00 形式的偏移）。"""
    try:
        dt = _dt.datetime.fromtimestamp(ts).astimezone()
        return dt.strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":" + dt.strftime("%z")[-2:]
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# HTTP 传输层（连接复用）
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message: str, status=None, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint


class StoreError(Exception):
    """本地文件读写失败（只读目录、被占用、沙箱受限等）。"""

    def __init__(self, message: str, path: str = "", hint: str = ""):
        super().__init__(message)
        self.message = message
        self.path = path
        self.hint = hint


def _write_hint(exc) -> str:
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return WRITE_HINT
    return "请检查磁盘空间与路径是否合法，或改用 --data-dir 指定其他目录"


def atomic_write_text(path: str, text: str) -> None:
    """原子写入文本文件（临时文件 + os.replace），失败时抛 StoreError。

    细节：
      * 临时文件名带进程号与线程 id，避免并发/残留的 .tmp 互相踩；
      * 目标文件只读时先去掉只读属性再替换（Windows 上否则会 EACCES）；
      * Windows 上被其他程序短暂占用时自动重试几次（编辑器/杀软/同步盘常见）；
      * 任何失败都会清理临时文件，并给出可操作的中文提示而不是裸异常。
    """
    check_path_length(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise StoreError("无法创建目录：%s" % directory, directory,
                             _write_hint(exc)) from exc
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if os.path.exists(path):
            try:
                if not os.access(path, os.W_OK):
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        last_error = None
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                last_error = None
                break
            except PermissionError as exc:      # 目标被占用：短暂等待后重试
                last_error = exc
                if attempt < 3:
                    time.sleep(0.12 * (attempt + 1))
                    continue
                raise
        if last_error is not None:
            raise last_error
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        errno_value = getattr(exc, "errno", None)
        if errno_value == errno.EISDIR:
            hint = "-o / 目标路径需要是文件而不是目录；换一个文件名重试"
        elif errno_value == errno.ENOSPC:
            hint = "磁盘空间不足，请清理后重试"
        elif isinstance(exc, PermissionError):
            hint = ("目标文件可能被其他程序占用（编辑器/同步盘/杀毒软件），关闭后重试；"
                    "若目录本身只读，可用 --data-dir 指定可写目录，或换一个输出路径")
        else:
            hint = _write_hint(exc)
        raise StoreError("无法写入文件：%s（%s）" % (path, getattr(exc, "strerror", None) or exc),
                         path, hint) from exc


# Windows 上未启用长路径支持时，超过约 260 字符会直接失败
_MAX_PATH = 250 if os.name == "nt" else 4000


def check_path_length(path: str) -> None:
    """路径过长时给出明确提示（Windows 260 字符限制），而不是含糊的“无法写入”。"""
    if not path:
        return
    full = os.path.abspath(path)
    if len(full) <= _MAX_PATH:
        return
    raise StoreError(
        "路径过长（%d 字符，上限约 %d）：%s" % (len(full), _MAX_PATH, full), path,
        "用 --dir 指定更短的归档目录，或缩短比赛/题目名称；"
        "也可以在 Windows 上启用长路径支持（组策略 LongPathsEnabled）")


def file_uri(path: str) -> str:
    """把本地路径转成可用的 file:// URI（正确处理中文、空格、#）。"""
    try:
        return pathlib.Path(os.path.abspath(path)).as_uri()
    except (ValueError, OSError):
        return "file://" + urllib.parse.quote(os.path.abspath(path).replace(os.sep, "/"))


def resolve_data_dir(cli_value=None) -> str:
    """数据目录优先级：--data-dir > 环境变量 NFLSOJ_DATA_DIR > ~/.nflsoj。"""
    raw = cli_value or os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR
    return os.path.abspath(os.path.expanduser(str(raw)))


STATUS_HINT = {
    400: "请求参数有误（站点 API 可能已升级，请检查工具版本）",
    401: "登录状态无效或已过期，请重新执行 login 命令",
    403: "没有权限访问该资源（账号可能未激活或被限制）",
    404: "资源不存在",
    429: "请求过于频繁，请稍后再试",
    500: "服务器内部错误",
    502: "网关错误（站点可能在维护）",
    503: "服务暂时不可用",
    504: "网关超时",
}


def report_error(console: "Console", err: ApiError) -> int:
    console.error(err.message)
    if err.hint:
        console.muted(err.hint)
    return 1


def extract_token(raw: str) -> str:
    """从用户粘贴的任意内容中提取 token。

    支持：裸 token、localStorage 中的 session-swr JSON、完整 Cookie 串、
          `token=xxx` 形式的键值对、`Bearer xxx` 头。
    """
    if not raw:
        return ""
    text = raw.strip().strip('"').strip("'").strip()
    if not text:
        return ""

    def from_json(blob):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            return ""
        if isinstance(data, dict):
            for key in ("token", "Token", "accessToken", "access_token", "jwt", "value"):
                if isinstance(data.get(key), str) and data[key].strip():
                    return data[key].strip()
            inner = data.get("sessionInfo")
            if isinstance(inner, dict) and isinstance(inner.get("token"), str):
                return inner["token"].strip()
        if isinstance(data, str):
            return data.strip()
        return ""

    if text.startswith("{") and text.endswith("}"):
        token = from_json(text)
        if token:
            return token

    # Cookie 串：k=v; k=v
    if "=" in text and (";" in text or re.match(r"^[A-Za-z0-9_\-\.]+=", text)):
        for part in re.split(r"[;\n]", text):
            piece = part.strip()
            if not piece or "=" not in piece:
                continue
            name, _, value = piece.partition("=")
            name = name.strip()
            value = urllib.parse.unquote(value.strip())
            if value.startswith("{"):
                token = from_json(value)
                if token:
                    return token
            if name.lower() in ("session-swr", "token", "access_token", "authorization", "jwt"):
                value = re.sub(r"^Bearer\s+", "", value, flags=re.I)
                if value:
                    return value

    text = re.sub(r"^Bearer\s+", "", text, flags=re.I)
    return text if re.match(r"^[A-Za-z0-9_\-\.%+/=:~]+$", text) else ""


class HttpTransport:
    """基于 http.client 的传输层：每线程一条 keep-alive 长连接。

    相比 urllib 每次新建连接，可省掉一次 TLS 握手（本项目实测约 1.2~1.8 秒）；
    连接被服务端关闭、超时或 5xx 时自动重连并按指数退避重试。
    """

    MAX_REDIRECTS = 5
    MAX_BODY = 32 * 1024 * 1024      # 单次响应体上限，避免异常响应吃满内存

    def __init__(self, base_url: str = DEFAULT_API, timeout: int = 30, insecure: bool = False,
                 retries: int = 2, debug: bool = False, offline: bool = False):
        self.base_url = str(base_url or DEFAULT_API).rstrip("/")
        try:
            parts = urllib.parse.urlsplit(self.base_url)
            port = parts.port
        except ValueError as exc:
            raise ApiError("站点地址无法解析：%s" % self.base_url, None,
                           "检查 --api 是否为形如 https://host:port 的地址（%s）" % exc)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ApiError("站点地址无效：%s" % self.base_url, None,
                           "检查 --api 是否为形如 https://host:port 的地址")
        self.scheme = parts.scheme.lower()
        self.host = parts.hostname
        self.port = port or (443 if self.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.timeout = clamp_timeout(timeout)
        self.retries = max(0, retries)
        self.debug = debug
        self.offline = offline
        self.ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self._local = threading.local()
        self._all = []
        self._lock = threading.Lock()
        self.requests = 0
        self.retried = 0
        self.redirected = 0

    # -- 连接管理 ---------------------------------------------------------
    def _new_conn(self):
        try:
            if self.scheme == "https":
                conn = http.client.HTTPSConnection(self.host, self.port,
                                                   timeout=self.timeout, context=self.ctx)
            else:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        except (ValueError, OSError) as exc:
            raise ApiError("无法连接站点：%s" % exc, None,
                           "检查 --api 与网络设置")
        with self._lock:
            self._all.append(conn)
        return conn

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def _drop(self, conn=None) -> None:
        conn = conn if conn is not None else getattr(self._local, "conn", None)
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        with self._lock:
            try:
                self._all.remove(conn)
            except ValueError:
                pass

    def release_thread_connections(self) -> None:
        """关闭当前线程持有的连接（线程池用完后调用，避免 socket 泄漏）。"""
        self._drop(getattr(self._local, "conn", None))

    def close(self) -> None:
        with self._lock:
            conns = list(self._all)
            self._all = []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def _same_origin(self, parsed) -> bool:
        """判断跳转目标是否仍是同一个站点（协议/主机/端口都要一致）。"""
        if not parsed.hostname:
            return True
        return ((parsed.scheme or self.scheme).lower() == self.scheme
                and parsed.hostname.lower() == self.host.lower()
                and (parsed.port or (443 if (parsed.scheme or self.scheme).lower() == "https" else 80))
                == self.port)

    # -- 请求 -------------------------------------------------------------
    def request(self, method: str, path: str, query=None, body=None, headers=None,
                retries=None):
        """返回 (status, headers, body_bytes)；4xx/5xx 不直接抛异常，由 Client 解释。"""
        if self.offline:
            raise ApiError("已启用离线模式（--offline），该操作需要访问站点",
                           hint="去掉 --offline 后重试")
        target = self.prefix + path
        if query:
            clean = []
            for key, value in query.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    value = "true" if value else "false"
                clean.append((key, value))
            if clean:
                target += "?" + urllib.parse.urlencode(clean)
        send_headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Connection": "keep-alive",
        }
        send_headers.update(headers or {})
        payload = None
        if body is not None:
            send_headers["Content-Type"] = "application/json"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        attempts = self.retries if retries is None else max(0, retries)
        # 重定向单独计数，不占用重试预算（否则 retries=0 时任何 3xx 都会退化成空响应）
        redirects = 0
        while True:
            attempt = 0
            while True:
                conn = self._conn()
                started = time.time()
                try:
                    if self.debug:
                        sys.stderr.write("[debug] %s %s%s\n" % (method, self.base_url, target))
                    conn.request(method, target, body=payload, headers=send_headers)
                    resp = conn.getresponse()
                    data = resp.read(self.MAX_BODY)
                    status = resp.status
                    got_headers = {k.lower(): v for k, v in resp.getheaders()}
                    self.requests += 1
                    if "gzip" in (got_headers.get("content-encoding") or "").lower():
                        try:
                            data = gzip.decompress(data)
                        except (OSError, EOFError, zlib.error) as exc:
                            self._drop(conn)
                            raise ApiError(
                                "响应 gzip 解码失败（%s），可能被代理/网关改写" % exc, status,
                                "检查代理设置，或加 --insecure 后再试")
                    if resp.will_close:
                        self._drop(conn)
                    if self.debug:
                        sys.stderr.write(
                            "[debug] <- %s %d bytes %.0fms%s\n"
                            % (status, len(data), (time.time() - started) * 1000,
                               " (重定向)" if status in (301, 302, 303, 307, 308) else ""))
                    if status in (500, 502, 503, 504, 429) and attempt < attempts:
                        self.retried += 1
                        self._drop(conn)
                        wait = self._retry_delay(attempt, got_headers.get("retry-after"))
                        if self.debug:
                            sys.stderr.write("[debug] 重试 %d/%d，等待 %.1fs\n"
                                             % (attempt + 1, attempts, wait))
                        time.sleep(wait)
                        attempt += 1
                        continue
                    if status in (301, 302, 303, 307, 308):
                        location = (got_headers.get("location") or "").strip()
                        redirects += 1
                        if location and redirects <= self.MAX_REDIRECTS:
                            resolved = urllib.parse.urljoin(self.base_url + target, location)
                            parsed = urllib.parse.urlsplit(resolved)
                            if not self._same_origin(parsed):
                                raise ApiError("站点跳转到其他地址：%s" % resolved, status,
                                               "检查 --api 是否正确；为安全起见不会把凭据发往其他站点")
                            target = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
                            self.redirected += 1
                            if status == 303 or (status in (301, 302) and method.upper() != "GET"):
                                # 按 HTTP 语义降级为 GET 并丢弃请求体
                                method, payload = "GET", None
                                for key in ("Content-Type", "Content-Length"):
                                    send_headers.pop(key, None)
                            if self.debug:
                                sys.stderr.write("[debug] 跟随跳转 -> %s\n" % target)
                            break            # 跳出内层，重新发一次请求
                        raise ApiError("站点跳转次数过多（>%d）" % self.MAX_REDIRECTS, status,
                                       "检查 --api 是否指向最终地址")
                    return status, got_headers, data
                except ApiError:
                    raise
                except (http.client.HTTPException, OSError, ValueError) as exc:
                    self._drop(conn)
                    cause = getattr(exc, "reason", exc) or exc
                    fatal = is_fatal_network_error(cause) or is_fatal_network_error(exc)
                    if not fatal and attempt < attempts:
                        self.retried += 1
                        time.sleep(0.35 * (attempt + 1))
                        attempt += 1
                        continue
                    raise ApiError("网络错误：%s" % (cause or cause.__class__.__name__), None,
                                   network_error_hint(cause))

    @staticmethod
    def _retry_delay(attempt: int, retry_after=None) -> float:
        """优先尊重服务端 Retry-After，否则指数退避（上限 4 秒）。"""
        if retry_after:
            try:
                return max(0.0, min(float(str(retry_after).strip()), 10.0))
            except (TypeError, ValueError):
                pass
        return min(4.0, 0.6 * (2 ** attempt))


# ---------------------------------------------------------------------------
# API 客户端
# ---------------------------------------------------------------------------
class Client:
    """NFLSOJ JSON API 客户端（线程安全：每个线程复用自己的一条连接）。"""

    def __init__(self, base_url: str = DEFAULT_API, token: str = "", cookie: str = "",
                 timeout: int = 30, insecure: bool = False, retries: int = 2,
                 debug: bool = False, offline: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.cookie = cookie or ""
        self.timeout = timeout
        self.offline = offline
        self.debug = debug
        self.http = HttpTransport(self.base_url, timeout=timeout, insecure=insecure,
                                  retries=retries, debug=debug, offline=offline)
        if debug:
            proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                     or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
            if proxy:
                sys.stderr.write("[debug] 注意：本工具的连接复用不走环境代理（检测到 %s）\n"
                                 % _mask_secret(proxy))
            sys.stderr.write("[debug] base=%s timeout=%ss retries=%d\n"
                             % (self.base_url, self.http.timeout, self.http.retries))

    def close(self) -> None:
        if self.debug:
            sys.stderr.write("[debug] 本次共 %d 次请求 / %d 次重试 / %d 次跳转\n"
                             % (self.http.requests, self.http.retried, self.http.redirected))
        self.http.close()

    # -- 底层 -------------------------------------------------------------
    def _headers(self) -> dict:
        headers = {
            "User-Agent": "nflsoj-cli/%s (+python-http.client)" % __version__,
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def request(self, method: str, path: str, query=None, body=None):
        status, _, data = self.http.request(method, path, query=query, body=body,
                                           headers=self._headers())
        text = data.decode("utf-8", "replace") if data else ""
        if status >= 400 or status == 0:
            message = "HTTP %s" % status if status else "请求失败"
            hint = STATUS_HINT.get(status, "")
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                detail = parsed.get("message") or parsed.get("error")
                if isinstance(detail, list):
                    detail = "；".join(str(item) for item in detail)
                if detail:
                    message = str(detail)
            if status == 403 and "not activated" in message.lower():
                message, hint = "账号未激活", "请先到站点激活账号后再使用"
            if status == 401 and not self.token:
                hint = hint or "请先运行：nflsoj login"
            raise ApiError(message, status, hint)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ApiError("服务器返回了非 JSON 数据", status, text[:200])

    # -- 具体接口 ---------------------------------------------------------
    def session_info(self):
        return self.request("GET", "/api/auth/getSessionInfo")

    def current_user(self):
        """返回已登录用户信息，未登录返回 None。"""
        info = self.session_info()
        if not isinstance(info, dict):
            raise ApiError("服务器返回了非预期的会话信息结构")
        return info.get("userMeta") or None

    def login(self, username: str, password: str):
        return self.request("POST", "/api/auth/login",
                            body={"username": username, "password": password})

    def contest_list(self, skip=0, take=CONTEST_PAGE_SIZE, keyword=None, created_by=None):
        return self.request("GET", "/api/contest/getContestList", query={
            "skipCount": int(skip),
            "takeCount": int(take),
            "keyword": keyword or None,
            "returnCount": True,
            "createdBy": created_by or None,
        })

    def contest_detail(self, contest_id, problems=True, description=False, announcements=False):
        return self.request("GET", "/api/contest/getContest", query={
            "contestId": int(contest_id),
            "description": bool(description),
            "problemInfos": bool(problems),
            "announcements": bool(announcements),
        })

    def problem_detail(self, problem_id, statement=True):
        return self.request("GET", "/api/problem/getProblem", query={
            "id": int(problem_id),
            "statement": bool(statement),
        })


# ---------------------------------------------------------------------------
# 数据规整
# ---------------------------------------------------------------------------
def _first_of(raw: dict, *keys):
    for key in keys:
        if raw.get(key) not in (None, ""):
            return raw[key]
    return None


def normalize_contest(raw: dict) -> dict:
    """把 getContestList / getContest 返回的比赛规整成统一结构（兼容多种字段名）。"""
    if not isinstance(raw, dict):
        raw = {}
    groups = []
    for item in raw.get("groups") or []:
        if isinstance(item, dict):
            groups.append(item.get("name") or "")
        elif item:
            groups.append(str(item))
    creator = raw.get("creator") or raw.get("owner") or raw.get("createdBy")
    if isinstance(creator, dict):
        creator = creator.get("username") or creator.get("name")
    pid = raw.get("id")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        pass
    return {
        "id": pid,
        "name": _first_of(raw, "name", "title") or "",
        "mode": _first_of(raw, "mode", "rule") or "CUSTOM",
        "startTime": _first_of(raw, "startTime", "beginAt", "start_time"),
        "endTime": _first_of(raw, "endTime", "endAt", "end_time"),
        "phase": _first_of(raw, "phase", "status", "state"),
        "groups": [g for g in groups if g],
        "creator": creator or "",
    }


def extract_tags(raw_tags) -> list:
    tags = []
    for tag in raw_tags or []:
        if isinstance(tag, dict) and tag.get("name"):
            tags.append(str(tag["name"]))
        elif isinstance(tag, str) and tag:
            tags.append(tag)
    return tags


def extract_problems(detail: dict) -> list:
    """从 getContest(problemInfos=True) 的响应中提取题目列表。"""
    if not isinstance(detail, dict):
        return []
    infos = detail.get("problemInfos")
    problems = []
    if isinstance(infos, list):
        for item in infos:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            pid = meta.get("id", item.get("problemId"))
            title = meta.get("title") or item.get("title") or (("题目 %s" % pid) if pid else "题目")
            try:
                order = int(item.get("displayOrder"))
            except (TypeError, ValueError):
                order = len(problems) + 1
            stats = item.get("attendeeStatistics")
            stats = stats if isinstance(stats, dict) else {}
            record = {
                "problemId": pid,
                "displayOrder": order,
                "title": title,
                "tags": extract_tags(item.get("tags")),
                "submitted": bool(item.get("bestSubmission")),
            }
            for key in ("scoreWeight",):
                if item.get(key) not in (None, ""):
                    record[key] = item[key]
            for key in ("numAcceptedUsers", "numSubmittedUsers", "numNonZeroUsers"):
                if stats.get(key) is not None:
                    record[key] = stats[key]
            best = item.get("bestSubmission")
            if isinstance(best, dict) and best.get("status"):
                record["bestStatus"] = str(best["status"])
            problems.append(record)
    if not problems:
        # 兼容只返回 {displayOrder: problemId} 映射的其他格式
        raw_map = detail.get("problems")
        if isinstance(raw_map, dict):
            for key, value in raw_map.items():
                try:
                    order = int(key)
                except (TypeError, ValueError):
                    order = len(problems) + 1
                problems.append({"problemId": value, "displayOrder": order,
                                 "title": "题目 %s" % value, "tags": [], "submitted": False})
    problems.sort(key=lambda p: p["displayOrder"])
    return problems


def extract_announcements(detail: dict) -> list:
    """把公告规整成 [{title, content, time}]。"""
    out = []
    raw = detail.get("announcements")
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, dict):
            out.append({
                "title": item.get("title") or item.get("name") or "公告",
                "content": item.get("content") or item.get("description") or "",
                "time": item.get("time") or item.get("createdAt") or item.get("publishTime"),
            })
        elif isinstance(item, str) and item.strip():
            out.append({"title": "公告", "content": item, "time": None})
    return out


# ---------------------------------------------------------------------------
# 本地存储
# ---------------------------------------------------------------------------
class Store:
    """保存登录凭据、做题标记与归档元数据（全部为 JSON 文件）。"""

    CONFIG_VERSION = 1
    PROGRESS_VERSION = 1

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self.config_path = os.path.join(data_dir, "config.json")
        self.progress_path = os.path.join(data_dir, "progress.json")
        self.config = {}
        self.progress = {}
        self.api_override = ""      # 仅本次运行的 --api 覆盖（不写盘）
        self.dirty = False
        self.load_errors = []       # [(path, 备份路径, 原因)]
        self.structure_warnings = []  # 结构被降级时的提示（由调用方决定是否打印）
        self._loading_path = ""

    # -- 文件读写 ---------------------------------------------------------
    @property
    def cache_dir(self) -> str:
        return os.path.join(self.data_dir, "cache")

    @property
    def statement_cache_dir(self) -> str:
        return os.path.join(self.cache_dir, "statements")

    @property
    def archive_dir(self) -> str:
        # 手工改坏的配置（archiveDir 是数字/列表）不应该让整条命令崩掉
        raw = self.config.get("archiveDir")
        if isinstance(raw, str) and raw.strip():
            return os.path.expanduser(raw.strip())
        return os.path.join(self.data_dir, "archive")

    def _ensure_dir(self, path) -> None:
        if path and not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)

    @staticmethod
    def _read_json(path, default, on_error=None):
        """读 JSON；损坏时备份原文件并通过 on_error 通知调用方，绝不静默丢弃。"""
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as exc:
            if on_error is not None:
                on_error(exc)
            return default
        if not isinstance(data, dict):
            if on_error is not None:
                on_error(ValueError("顶层不是 JSON 对象（实际是 %s）" % type(data).__name__))
            return default
        return data

    def _backup_corrupt(self, path, exc) -> str:
        """把损坏文件改名保留，返回备份路径（供提示用户）。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = "%s.corrupt-%s" % (path, stamp)
        try:
            os.replace(path, target)
            return target
        except OSError:
            return ""

    def _write_json(self, path, payload) -> None:
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if os.name != "nt":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def writable(self) -> bool:
        """真正试写一次，判断数据目录能否保存（比 os.access 可靠）。"""
        probe_dir = self.data_dir
        while probe_dir and not os.path.isdir(probe_dir):
            if os.path.exists(probe_dir):
                return False          # 路径上有一层是同名文件，目录永远建不出来
            parent = os.path.dirname(probe_dir)
            if parent in ("", probe_dir):
                return False
            probe_dir = parent
        if not probe_dir or not os.path.isdir(probe_dir):
            return False
        probe = os.path.join(probe_dir, ".nflsoj-write-test-%d" % os.getpid())
        try:
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("ok")
            os.remove(probe)
            return True
        except OSError:
            try:
                os.remove(probe)
            except OSError:
                pass
            return False

    def load(self) -> None:
        """载入本地数据，并把结构异常的数据降级成可用状态（不抛异常）。"""
        self.load_errors = []          # [(path, 备份路径, 原因)]
        self.structure_warnings = []
        self._loading_path = self.config_path
        self.config = self._read_json(self.config_path, {}, self._note_load_error)
        self._loading_path = self.progress_path
        self.progress = self._read_json(self.progress_path, {}, self._note_load_error)
        self._loading_path = ""
        self._normalize_config()
        self._normalize_progress()
        self.dirty = False

    def _note_load_error(self, exc) -> None:
        # 延迟到有 console 时再提示：这里只记录，并立刻备份，避免后续写回把原数据冲掉
        path = self._loading_path
        backup = self._backup_corrupt(path, exc) if os.path.isfile(path) else ""
        self.load_errors.append((path, backup, getattr(exc, "msg", None) or str(exc)))

    def _normalize_config(self) -> None:
        """把 config.json 里的字段类型归一，避免下游 .rstrip()/expanduser() 崩掉。"""
        config = self.config
        for key in ("baseUrl", "token", "cookie", "username"):
            value = config.get(key)
            if value is not None and not isinstance(value, str):
                config[key] = str(value)
        # 归档目录必须是「像路径的字符串」；列表/字典之类直接丢弃，回退默认目录
        archive_dir = config.get("archiveDir")
        if archive_dir is not None:
            if isinstance(archive_dir, str):
                if not archive_dir.strip():
                    config.pop("archiveDir", None)
            elif isinstance(archive_dir, (int, float)) and not isinstance(archive_dir, bool):
                config["archiveDir"] = str(archive_dir)
            else:
                config.pop("archiveDir", None)
        if isinstance(config.get("cookies"), dict):
            config["cookies"] = {str(k): str(v) for k, v in config["cookies"].items()
                                 if isinstance(v, (str, int, float))}
        elif config.get("cookies") is not None:
            config.pop("cookies", None)
        for key in ("token", "cookie", "username", "baseUrl", "archiveDir"):
            if key in config and config[key] is None:
                config.pop(key)

    def _normalize_progress(self) -> None:
        """把 progress.json 的嵌套结构归一：users/user/contests/problems 逐层校验。"""
        progress = self.progress
        users = progress.get("users")
        if not isinstance(users, dict):
            if users is not None:
                self.structure_warnings.append("progress.json 的 users 不是对象，已重建为 {}")
            users = {}
            progress["users"] = users
        for username in list(users):
            user = users.get(username)
            if not isinstance(user, dict):
                self.structure_warnings.append("账号 %s 的记录不是对象，已重建" % username)
                user = {"contests": {}}
                users[username] = user
            contests = user.get("contests")
            if not isinstance(contests, dict):
                if contests is not None:
                    self.structure_warnings.append(
                        "账号 %s 的 contests 不是对象，已重建" % username)
                contests = {}
                user["contests"] = contests
            for key in list(contests):
                entry = contests.get(key)
                if not isinstance(entry, dict):
                    self.structure_warnings.append("比赛 %s 的记录不是对象，已丢弃" % key)
                    contests.pop(key, None)
                    continue
                problems = entry.get("problems")
                if not isinstance(problems, list):
                    if problems is not None:
                        self.structure_warnings.append(
                            "比赛 %s 的题目列表不是数组，已重建" % key)
                    entry["problems"] = []
                    continue
                cleaned = []
                for problem in problems:
                    if not isinstance(problem, dict):
                        self.structure_warnings.append(
                            "比赛 %s 有非对象的题目记录，已丢弃" % key)
                        continue
                    if problem.get("state") not in (None,) + tuple(STATE_ORDER):
                        problem.pop("state", None)
                        problem.pop("stateUpdatedAt", None)
                    cleaned.append(problem)
                entry["problems"] = cleaned

    def save_config(self) -> None:
        self.config["version"] = self.CONFIG_VERSION
        self._write_json(self.config_path, self.config)

    def save_progress(self) -> None:
        self.progress["version"] = self.PROGRESS_VERSION
        self._write_json(self.progress_path, self.progress)
        self.dirty = False

    # -- 凭据 -------------------------------------------------------------
    @property
    def api_base(self) -> str:
        override = self.api_override if isinstance(self.api_override, str) else ""
        configured = self.config.get("baseUrl")
        configured = configured if isinstance(configured, str) else ""
        return (override or configured or DEFAULT_API).strip()

    def set_credentials(self, token: str = "", cookie: str = "", username: str = "",
                        base_url: str = "") -> None:
        if base_url:
            self.config["baseUrl"] = base_url
        self.config["token"] = token or ""
        self.config["cookie"] = cookie or ""
        self.config["username"] = username or ""

    def clear_credentials(self) -> None:
        self.config["token"] = ""
        self.config["cookie"] = ""
        self.config["username"] = ""

    def client(self, **overrides) -> Client:
        options = {
            "base_url": self.api_base,
            "token": str(self.config.get("token") or ""),
            "cookie": str(self.config.get("cookie") or ""),
        }
        options.update({k: v for k, v in overrides.items() if v is not None})
        return Client(**options)

    # -- 账号 / 做题记录 --------------------------------------------------
    def known_usernames(self) -> list:
        users = self.progress.get("users")
        return [str(name) for name in users] if isinstance(users, dict) else []

    def cached_username(self) -> str:
        """本地已知的账号（完全不联网）。"""
        name = self.config.get("username")
        name = name.strip() if isinstance(name, str) else ""
        if name:
            return name
        users = self.known_usernames()
        return users[0] if users else ""

    def user_data(self, username: str) -> dict:
        users = self.progress.get("users")
        if not isinstance(users, dict):
            users = {}
            self.progress["users"] = users
        user = users.get(username)
        if not isinstance(user, dict):
            user = {"contests": {}}
            users[username] = user
        if not isinstance(user.get("contests"), dict):
            user["contests"] = {}
        return user

    def contest_entry(self, username: str, contest_id):
        return self.user_data(username)["contests"].get(str(contest_id))

    def upsert_contest(self, username: str, contest: dict, problems=None) -> dict:
        """合并比赛信息，并保留已有题目的标记状态与归档信息。"""
        contests = self.user_data(username)["contests"]
        key = str(contest.get("id"))
        entry = contests.get(key)
        if not isinstance(entry, dict):
            entry = {"id": contest.get("id"), "name": ""}
            contests[key] = entry
        for field in ("name", "mode", "startTime", "endTime", "creator", "groups", "phase"):
            if contest.get(field) not in (None, "", []):
                entry[field] = contest[field]
        if not isinstance(entry.get("problems"), list):
            entry["problems"] = []

        if problems is not None:
            previous = {}
            for problem in entry["problems"]:
                if isinstance(problem, dict):
                    previous[str(problem.get("problemId"))] = problem
            merged = []
            for item in problems:
                if not isinstance(item, dict):
                    continue
                old = previous.get(str(item.get("problemId")))
                record = dict(item)
                if old:
                    for keep in ("state", "stateUpdatedAt"):
                        if old.get(keep):
                            record[keep] = old[keep]
                    if old.get("submitted"):
                        record["submitted"] = True
                    if old.get("archivedAt"):
                        record["archivedAt"] = old["archivedAt"]
                merged.append(record)
            entry["problems"] = merged
            entry["syncedAt"] = int(time.time())
        self.dirty = True
        return entry

    def set_state(self, username: str, contest_id, problem_id, state) -> dict:
        entry = self.contest_entry(username, contest_id)
        if not entry:
            raise KeyError("contest %s not found" % contest_id)
        for problem in entry.get("problems") or []:
            if not isinstance(problem, dict):
                continue
            if str(problem.get("problemId")) == str(problem_id):
                if state is None:
                    problem.pop("state", None)
                    problem.pop("stateUpdatedAt", None)
                else:
                    problem["state"] = state
                    problem["stateUpdatedAt"] = int(time.time())
                self.dirty = True
                return problem
        raise KeyError("problem %s not found" % problem_id)

    def all_contests(self, username: str) -> list:
        """按开始时间倒序返回比赛；id 类型混杂或时间非法时也不会抛异常。"""
        contests = self.user_data(username)["contests"]
        entries = [entry for entry in contests.values() if isinstance(entry, dict)]
        return sorted(entries, key=_contest_sort_key, reverse=True)

    # -- 归档元数据 -------------------------------------------------------
    def mark_archived(self, username: str, contest_id, path: str, problems: int,
                      statements, failed: int = 0) -> None:
        """记录归档结果；statements 为 None 表示本次没有抓题面，保留旧计数。"""
        entry = self.contest_entry(username, contest_id)
        if entry is None:
            return
        entry["archivedAt"] = int(time.time())
        entry["archivePath"] = path
        entry["archivedProblems"] = problems
        if statements is not None:
            entry["archivedStatements"] = statements
        if failed:
            entry["archiveFailed"] = failed
        else:
            entry.pop("archiveFailed", None)
        self.dirty = True

    # -- 题目正文缓存 -----------------------------------------------------
    def statement_cache_path(self, problem_id) -> str:
        return os.path.join(self.statement_cache_dir, "%s.json" % safe_cache_name(problem_id))

    def load_statement(self, problem_id, ttl: int = STATEMENT_CACHE_TTL):
        """读取题目正文缓存；过期或损坏返回 None（不抛异常）。"""
        payload = self._read_json(self.statement_cache_path(problem_id), None)
        if not isinstance(payload, dict) or not payload.get("statement"):
            return None
        fetched = payload.get("fetchedAt")
        try:
            fetched = float(fetched) if fetched is not None else 0.0
        except (TypeError, ValueError):
            fetched = 0.0          # 时间戳被改坏：当作已过期，重新抓取
        if ttl and (not fetched or time.time() - fetched > ttl):
            return None
        return payload

    def save_statement(self, problem_id, payload: dict) -> None:
        self._ensure_dir(self.statement_cache_dir)
        payload = dict(payload)
        payload["fetchedAt"] = int(time.time())
        self._write_json(self.statement_cache_path(problem_id), payload)


# ---------------------------------------------------------------------------
# 业务逻辑：客户端构造 / 登录 / 拉取
# ---------------------------------------------------------------------------
def client_for(store: Store, args=None) -> Client:
    """按命令行选项构造 API 客户端（--timeout / --insecure / --debug / --offline）。"""
    options = {"timeout": clamp_timeout(getattr(args, "timeout", None) if args else None)}
    if args is not None:
        if getattr(args, "insecure", False):
            options["insecure"] = True
        if getattr(args, "debug", False):
            options["debug"] = True
        if getattr(args, "offline", False):
            options["offline"] = True
    return store.client(**options)


def report_store_problems(store: Store, console: Console) -> None:
    """把 Store 载入时发现的数据文件损坏/结构降级信息提示出来（每类只提示一次）。"""
    for path, backup, reason in getattr(store, "load_errors", []):
        console.warn("本地数据文件损坏，已备份：%s" % path)
        if backup:
            console.muted("原文件已保留为：%s" % backup)
        console.muted("原因：%s" % reason)
        console.muted("本次将以空数据继续；如需恢复，请修好备份文件后改回原名")
    seen = set()
    for message in getattr(store, "structure_warnings", []):
        if message in seen:
            continue
        seen.add(message)
        console.warn(message)


def release_connections(client) -> None:
    """关闭当前线程持有的 keep-alive 连接（线程池用完后调用，避免 socket 泄漏）。"""
    http = getattr(client, "http", None)
    if http is None:
        return
    try:
        http.release_thread_connections()
    except Exception:
        pass


def _safe_save_progress(store: Store, console: Console) -> bool:
    """保存进度；失败只警告并返回 False（不打断已经完成的工作）。"""
    try:
        store.save_progress()
        return True
    except StoreError as exc:
        console.warn("未能写入本地缓存：%s" % exc.message)
        console.muted("已完成的操作仍在内存中，退出后不会保留")
        return False


def resolve_username(store: Store, console: Console, client: Client, online: bool = False) -> str:
    """确定当前账号。

    默认走本地缓存（不联网，命令瞬间返回）；online=True 时校验 token 并刷新用户名。
    """
    cached = store.cached_username()
    if not online:
        return cached
    if getattr(client, "offline", False):
        return cached
    try:
        user = client.current_user()
    except ApiError as exc:
        if cached and exc.status in (None, 502, 503, 504):
            console.warn("站点暂时无法访问（%s），继续使用本地数据" % exc.message)
            return cached
        raise
    if not user:
        if cached:
            raise ApiError("登录状态无效或已过期", 401, "请重新执行：nflsoj login")
        raise ApiError("尚未登录或登录状态已失效", 401, "请先运行：nflsoj login")
    username = str(user.get("username") or user.get("id") or "unknown")
    if store.config.get("username") != username:
        store.config["username"] = username
        try:
            store.save_config()
        except StoreError as exc:
            console.warn("用户名未能写回配置：%s" % exc.message)
    return username


def _contest_page_data(client: Client, skip, page_size, keyword):
    """拉取一页比赛列表，返回 (规范化后的列表, 服务端总数或 None)。"""
    data = client.contest_list(skip=skip, take=page_size, keyword=keyword)
    if not isinstance(data, dict):
        raise ApiError("服务器返回了非预期的数据结构（期望 JSON 对象）",
                       hint="站点 API 可能已升级，请检查工具版本")
    items = data.get("contests") or []
    if not isinstance(items, list):
        raise ApiError("服务器返回了非预期的比赛列表结构",
                       hint="站点 API 可能已升级，请检查工具版本")
    count = data.get("count")
    return ([normalize_contest(item) for item in items if isinstance(item, dict)],
            count if isinstance(count, int) else None)


def iter_contest_pages(client: Client, keyword=None, page_size=CONTEST_PAGE_SIZE,
                       stop_after=None):
    """分页产出比赛（每页一个 list），便于边拉取边处理。

    注意：不能因为某页数量少于 page_size 就停止——部分部署会把 takeCount
    压得更小；只要服务端给出了总数（count）就按总数继续翻页。
    """
    skip, total, pages = 0, None, 0
    while True:
        page, count = _contest_page_data(client, skip, page_size, keyword)
        if total is None:
            total = count
        skip += len(page)
        pages += 1
        yield page, total
        if not page or pages > 500:
            break
        if stop_after is not None and stop_after():
            break
        if total is not None:
            if skip >= total:
                break
        elif len(page) < page_size:
            break


def fetch_all_contest_pages(client: Client, keyword=None, page_size=CONTEST_PAGE_SIZE,
                            workers: int = DEFAULT_WORKERS, max_pages=None,
                            on_page=None):
    """并发拉取比赛列表（第 1 页串行拿到总数，其余页并发）。

    只用于「只需要列表」的场景（contests --live / sync --no-problems）：
    列表阶段从「页数 × 单次延迟」降到约 1 次往返。
    on_page(已获取页数, 累计场数, 总数) 用于进度展示。
    返回 (按页序拼接的比赛列表, 页数)。
    """
    first, count = _contest_page_data(client, 0, page_size, keyword)
    if on_page:
        on_page(1, len(first), count)
    total_pages = 1
    if count:
        total_pages = max(1, (count + page_size - 1) // page_size)
    elif len(first) >= page_size:
        total_pages = 2              # 服务端没给总数：至少再探一页
    if max_pages:
        total_pages = min(total_pages, max(1, int(max_pages)))
    batches = {1: first}
    if total_pages > 1:
        remaining = list(range(2, total_pages + 1))
        pool = ThreadPoolExecutor(max_workers=clamp_workers(workers))
        try:
            futures = {pool.submit(_contest_page_data, client, (page - 1) * page_size,
                                   page_size, keyword): page for page in remaining}
            done_pages = 1
            fetched = len(first)
            for future in as_completed(futures):
                page_no = futures[future]
                try:
                    batch, _ = future.result()
                except ApiError as exc:
                    if exc.status in (400, 404):
                        batches[page_no] = []       # 末页越界：正常结束
                        continue
                    raise
                batches[page_no] = batch
                done_pages += 1
                fetched += len(batch)
                if on_page:
                    on_page(done_pages, fetched, count)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            release_connections(client)
    contests = []
    for page_no in sorted(batches):
        contests.extend(batches[page_no])
    return contests, len(batches)


def fetch_contest_detail(client: Client, contest_id, description=False, announcements=False):
    """返回 (比赛信息, 题目列表, 详情原始响应)。"""
    detail = client.contest_detail(contest_id, problems=True,
                                   description=description, announcements=announcements)
    if not isinstance(detail, dict):
        raise ApiError("服务器返回了非预期的比赛详情结构")
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    contest = normalize_contest(meta)
    contest["id"] = contest.get("id") or contest_id
    if not contest.get("name"):
        contest["name"] = "比赛 %s" % contest_id
    return contest, extract_problems(detail), detail


def sync_contests(store: Store, username: str, console: Console, client: Client,
                  keyword=None, only_ids=None, workers: int = DEFAULT_WORKERS,
                  with_problems: bool = True, force: bool = False,
                  incremental: bool = False, limit: int = None) -> dict:
    """拉取比赛列表（以及每场比赛的题目），写入本地缓存。

    列表分页与题目详情并发进行（生产者/消费者流水线），列表还没拉完就先开跑详情，
    因此总耗时约等于「11 次列表请求」与「全部详情请求」中较大的一个，而不是两者之和。
    """
    stats = {"contests": 0, "problems": 0, "failed": [], "skipped": 0, "pages": 0,
             "elapsed": 0.0}
    started = time.time()
    wanted = {str(i) for i in only_ids} if only_ids else None

    # -- 无需题目：只刷比赛列表 ------------------------------------------
    if not with_problems:
        progress = Progress(console, "获取比赛列表", unit="页").start_line()
        try:
            if wanted is not None:
                # 指定了 ID：直接按 ID 取元信息，不必翻遍所有分页
                for cid in only_ids:
                    try:
                        detail = client.contest_detail(cid, problems=False)
                        meta = normalize_contest(detail.get("meta") or {})
                        meta["id"] = meta.get("id") or cid
                        store.upsert_contest(username, meta, None)
                        stats["contests"] += 1
                        progress.update(1, total=len(only_ids),
                                        detail=clean_text(meta.get("name"))[:26])
                    except ApiError as exc:
                        stats["failed"].append((cid, "", exc.message))
                        progress.update(1, total=len(only_ids), failed=True)
            else:
                # 列表阶段并发抓取（只需列表时最划算）
                max_pages = None
                if limit:
                    max_pages = (int(limit) + CONTEST_PAGE_SIZE - 1) // CONTEST_PAGE_SIZE
                fetched, pages = fetch_all_contest_pages(
                    client, keyword=keyword, workers=workers, max_pages=max_pages,
                    on_page=lambda done_pages, got, total: progress.update(
                        1, total=None, detail="已获取 %d 场 / %s 页" % (got, total or "?")))
                stats["pages"] = pages
                for contest in fetched:
                    if limit and stats["contests"] >= limit:
                        break
                    store.upsert_contest(username, contest, None)
                    stats["contests"] += 1
        finally:
            progress.close()
            store.save_progress()
        stats["elapsed"] = time.time() - started
        return stats

    # 本地已同步过题目的比赛：跳过详情请求，并作为增量同步的判定依据
    # 注意只判断 syncedAt：有些比赛（讲座 / 未开放题目列表）合法地就是 0 题，
    # 若要求 problems 非空，增量同步会永远停不下来。
    known = {}
    for contest in store.all_contests(username):
        entry = store.contest_entry(username, contest.get("id")) or {}
        if entry.get("syncedAt"):
            known[str(contest.get("id"))] = entry

    incremental = bool(incremental) and not force and not wanted and not keyword
    quiet_pages = {"n": 0}
    stop_flag = {"stop": False}
    total = {"n": 0}

    def work(contest):
        try:
            meta, problems, _ = fetch_contest_detail(client, contest.get("id"))
            return contest, meta, problems, None
        except ApiError as exc:
            return contest, None, None, exc.message
        except Exception as exc:      # noqa: BLE001 - 单个比赛失败不应中断整体同步
            return contest, None, None, str(exc)

    progress = Progress(console, "同步题目列表", unit="场", detail_width=34).start_line()
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, 16)))
    futures = set()

    def collect(done_futures):
        for future in done_futures:
            contest, meta, problems, error = future.result()
            if error is None:
                store.upsert_contest(username, meta, problems)
                stats["contests"] += 1
                stats["problems"] += len(problems)
                progress.update(1, total=total["n"],
                                detail="%s · %d 题" % (clean_text(contest.get("name"))[:22],
                                                       len(problems)))
            else:
                stats["failed"].append((contest.get("id"), contest.get("name"), error))
                progress.update(1, total=total["n"], failed=True,
                                detail="失败 · %s" % clean_text(contest.get("name"))[:22])

    try:
        if wanted is not None:
            # 指定了 ID：直接对这些比赛拉详情，跳过全部列表分页（省掉 10 次请求）
            stubs = []
            for cid in only_ids:
                entry = store.contest_entry(username, cid) or {}
                stubs.append({"id": safe_int(cid, cid), "name": entry.get("name") or "",
                              "phase": entry.get("phase")})
            pages = iter([(stubs, len(stubs))])
        else:
            pages = iter_contest_pages(client, keyword=keyword,
                                       stop_after=lambda: stop_flag["stop"])
        for page, _page_total in pages:
            stats["pages"] += 1
            page_known = 0
            for contest in page:
                cid = str(contest.get("id"))
                if wanted is not None and cid not in wanted:
                    continue
                if limit and total["n"] >= limit:
                    break
                total["n"] += 1
                entry = known.get(cid)
                if entry is not None and not force:
                    # 已有缓存：只更新比赛基本信息，跳过详情请求
                    store.upsert_contest(username, contest, None)
                    stats["contests"] += 1
                    stats["problems"] += len(entry.get("problems") or [])
                    stats["skipped"] += 1
                    page_known += 1
                    progress.update(1, total=total["n"],
                                    detail="跳过 · %s" % clean_text(contest.get("name"))[:22])
                    continue
                futures.add(pool.submit(work, contest))
            if page and page_known == len(page):
                quiet_pages["n"] += 1
            else:
                quiet_pages["n"] = 0
            if incremental and quiet_pages["n"] >= 2:
                stop_flag["stop"] = True
            while True:
                done, futures = wait(futures, timeout=0, return_when=FIRST_COMPLETED)
                if not done:
                    break
                collect(done)
            if limit and total["n"] >= limit:
                break
        # 收尾：等所有在途请求完成
        while futures:
            done, futures = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
            collect(done)
    except KeyboardInterrupt:
        _safe_save_progress(store, console)
        # 关闭连接可让在途请求立刻失败，线程很快结束，不再阻塞退出
        try:
            client.close()
        except Exception:
            pass
        progress.close()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        progress.close()
        # 成功路径已在上面排空所有 future；取消路径不等满，避免 Ctrl+C 后卡住
        pool.shutdown(wait=False, cancel_futures=True)
        release_connections(client)

    store.save_progress()
    stats["elapsed"] = time.time() - started
    return stats


# ---------------------------------------------------------------------------
# 查找辅助
# ---------------------------------------------------------------------------
def resolve_contest(store: Store, username: str, spec):
    """按 ID 或名称（不区分大小写的子串）查找本地缓存的比赛。"""
    contests = store.all_contests(username)
    text = str(spec).strip()
    for contest in contests:
        if str(contest.get("id")) == text:
            return contest, []
    needle = text.lower()
    matches = [c for c in contests if needle in (c.get("name") or "").lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def resolve_problem(entry: dict, spec):
    """按题号(displayOrder) 或 题目 ID 查找题目。"""
    problems = entry.get("problems") or []
    text = str(spec).strip()
    for problem in problems:
        if str(problem.get("displayOrder")) == text:
            return problem
    for problem in problems:
        if str(problem.get("problemId")) == text:
            return problem
    return None


def contest_link(base_url: str, contest_id, display_order=None) -> str:
    url = "%s/c/%s" % (base_url.rstrip("/"), contest_id)
    if display_order is not None:
        url += "/p/%s" % display_order
    return url


def print_contest_header(console: Console, entry: dict, base_url: str = "",
                         link_builder=None, note: str = None) -> None:
    name = entry.get("name") or ("比赛 %s" % entry.get("id"))
    phase = contest_phase(entry.get("startTime"), entry.get("endTime"), entry.get("phase"))
    console.title(name, note="#%s" % entry.get("id") if note is None else note)
    bits = [console.phase_pill(phase), console.paint(str(entry.get("mode") or "-"), "magenta")]
    bits.append(console.paint(fmt_interval(entry.get("startTime"), entry.get("endTime")), "grey"))
    if entry.get("creator"):
        bits.append(console.paint("创建者 " + str(entry["creator"]), "grey"))
    if entry.get("groups"):
        bits.append(console.paint("分组 " + ", ".join(entry["groups"]), "grey"))
    console.out("  " + console.paint(" · ", "grey").join(bits))
    if base_url:
        url = (link_builder(entry) if link_builder
               else contest_link(base_url, entry.get("id")))
        console.out("  " + console.paint(url, "blue", dim=True))


# ---------------------------------------------------------------------------
# Markdown 归档
# ---------------------------------------------------------------------------
_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED_NAMES = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                   | {"COM%d" % i for i in range(1, 10)}
                   | {"LPT%d" % i for i in range(1, 10)})


def slugify(text, max_chars: int = 48, fallback: str = "untitled") -> str:
    """把标题变成安全、可读的文件名片段（保留中文，去掉非法字符）。"""
    raw = clean_text(text)
    raw = _INVALID_FS_CHARS.sub("-", raw)
    raw = re.sub(r"\s+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("- .")
    if len(raw) > max_chars:
        raw = raw[:max_chars].rstrip("- .")
    if not raw:
        raw = fallback
    if raw.upper() in _RESERVED_NAMES:
        raw = "_" + raw
    return raw


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", "\\n").replace("\r", "")
    return '"%s"' % text


def build_front_matter(pairs) -> str:
    """pairs: [(key, value)]；value 为 list/tuple 时输出 YAML 行内序列。"""
    lines = ["---"]
    for key, value in pairs:
        if isinstance(value, (list, tuple)):
            items = ", ".join(_yaml_scalar(item) for item in value)
            lines.append("%s: [%s]" % (key, items))
        else:
            lines.append("%s: %s" % (key, _yaml_scalar(value)))
    lines.append("---")
    return "\n".join(lines)


def normalize_markdown(text) -> str:
    """规范化站点返回的 Markdown：统一换行、去掉行尾空白、压缩空行、补齐代码块。"""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out, fence, blanks = [], False, 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = not fence
            out.append(line.rstrip() if not fence else line)
            blanks = 0
            continue
        if fence:
            out.append(line)          # 代码块内部保持原样
            continue
        line = line.rstrip()
        if not line:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    while out and not out[-1]:
        out.pop()
    if fence:
        out.append("```")             # 代码块没闭合时补上，避免后续内容被吞
    return "\n".join(out).strip("\n")


def md_cell(text) -> str:
    """表格单元格转义（`|` 会撑破表格、`[`/`]` 会破坏链接语法）。"""
    text = clean_text(text) if text is not None else ""
    return (text.replace("\\", "\\\\").replace("|", "\\|")
            .replace("[", "\\[").replace("]", "\\]"))


_SITE_LINK_RE = re.compile(r"(\]\(\s*)(/[^)\s]*)")


def absolutize_links(text, base_url: str) -> str:
    """把站点返回内容中的站内绝对链接（/xxx）补成完整网址，离线打开也能跳转。"""
    if not text or not base_url:
        return text
    base = base_url.rstrip("/")
    return _SITE_LINK_RE.sub(lambda m: m.group(1) + base + m.group(2), text)


def md_link(text, url) -> str:
    return "[%s](%s)" % (text, url)


def write_if_changed(path: str, content: str) -> str:
    """内容有变化才写盘（保持文件 mtime 稳定），返回 written / unchanged。"""
    old = None
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            old = handle.read()
    except (OSError, UnicodeDecodeError):
        old = None
    if old == content:
        return "unchanged"
    atomic_write_text(path, content)
    return "written"


_ARCHIVED_AT_RE = re.compile(r'^archived_at:\s*"([^"]+)"', re.MULTILINE)


def preserved_timestamp(path: str, force: bool = False, default: str = None) -> str:
    """返回归档时间戳：默认沿用文件里已有的 archived_at，保证重复归档不改写文件。

    force=True（或文件不存在 / 读不到时间戳）时使用当前时间。
    """
    if not force:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                head = handle.read(4096)
            match = _ARCHIVED_AT_RE.search(head)
            if match:
                return match.group(1)
        except (OSError, UnicodeDecodeError):
            pass
    return default or fmt_local_iso(time.time())


def ensured_timestamp(path: str, force: bool = False, default: str = None) -> str:
    return preserved_timestamp(path, force, default)


def ensure_archive_root(root: str) -> None:
    """确保归档根目录可用；指向文件/不可创建时给出可操作提示而不是 traceback。"""
    if os.path.isfile(root):
        raise StoreError("归档目录指向了一个文件：%s" % root, root,
                         "用 --dir 指定一个目录（或不传该参数使用默认目录）")
    if os.path.isdir(root):
        return
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        raise StoreError("无法创建归档目录：%s（%s）" % (root, getattr(exc, "strerror", None) or exc),
                         root, _write_hint(exc)) from exc


def contest_dir_name(contest: dict, used=None) -> str:
    base = "%s-%s" % (contest.get("id"), slugify(contest.get("name"), 44, "contest"))
    name = base
    if used is not None:
        index = 2
        while name.lower() in used:
            name = "%s-%d" % (base, index)
            index += 1
        used.add(name.lower())
    return name


def problem_file_name(problem: dict, used) -> str:
    base = "%02d-%s" % (safe_int(problem.get("displayOrder"), 0),
                        slugify(problem.get("title"), 44, "problem"))
    name = base + ".md"
    index = 2
    while name.lower() in used:
        name = "%s-%d.md" % (base, index)
        index += 1
    used.add(name.lower())
    return name


def render_contest_markdown(bundle: dict) -> str:
    """生成比赛首页 Markdown。

    bundle: contest / problems / detail / files（displayOrder -> 相对路径）/ archived_at
    可选：link_builder(contest, display_order=None) 自定义站点链接格式，
          generator 自定义页脚署名（默认 nflsoj）。
    """
    contest = bundle["contest"]
    problems = bundle.get("problems") or []
    detail = bundle.get("detail") or {}
    files = bundle.get("files") or {}
    has_statement = bundle.get("has_statement") or {}
    archived_at = bundle.get("archived_at") or fmt_local_iso(time.time())
    base_url = bundle.get("base_url") or ""
    generator = bundle.get("generator") or ("nflsoj %s" % __version__)
    link_builder = bundle.get("link_builder")

    def site_link(target, order=None):
        if link_builder:
            return link_builder(target, order)
        return contest_link(base_url, target.get("id") if isinstance(target, dict) else target, order)

    contest_id = contest.get("id")
    phase = contest_phase(contest.get("startTime"), contest.get("endTime"), contest.get("phase"))
    stats = collection_counts(problems)
    source = bundle.get("source_url") or (site_link(contest) if base_url else "")

    pairs = [
        ("title", contest.get("name") or "比赛 %s" % contest_id),
        ("contest_id", contest_id),
        ("mode", contest.get("mode") or ""),
        ("phase", phase),
        ("start_time", fmt_time(contest.get("startTime"))),
        ("end_time", fmt_time(contest.get("endTime"))),
        ("creator", contest.get("creator") or ""),
        ("groups", list(contest.get("groups") or [])),
        ("problems", len(problems)),
        ("local_done", stats[STATE_DONE]),
        ("local_doing", stats[STATE_DOING]),
        ("archived_at", archived_at),
        ("source", source),
        ("generator", generator),
    ]
    lines = [build_front_matter(pairs), "",
             "# %s" % (contest.get("name") or "比赛 %s" % contest_id), ""]
    meta_bits = ["比赛 #%s" % contest_id, str(contest.get("mode") or "-"),
                 PHASE_LABEL.get(phase, "-"),
                 fmt_interval(contest.get("startTime"), contest.get("endTime"))]
    if contest.get("creator"):
        meta_bits.append("创建者 %s" % contest["creator"])
    lines.append("> " + " · ".join(bit for bit in meta_bits if bit))
    lines.append("")
    if problems:
        lines.append("**本地进度**：%d/%d 已完成%s" % (
            stats[STATE_DONE], len(problems),
            "，另有 %d 题正在做" % stats[STATE_DOING] if stats[STATE_DOING] else ""))
    lines.append("")
    lines.append("## 比赛说明")
    lines.append("")
    description = absolutize_links(normalize_markdown(detail.get("description")), base_url)
    lines.append(description if description else "*（这场比赛没有额外的说明文字）*")
    lines.append("")

    announcements = extract_announcements(detail)
    if announcements:
        lines.append("## 公告")
        lines.append("")
        for item in announcements:
            title = item.get("title") or "公告"
            when = fmt_time(item.get("time")) if item.get("time") else ""
            lines.append("### %s%s" % (title, ("（%s）" % when) if when else ""))
            lines.append("")
            content = absolutize_links(normalize_markdown(item.get("content")), base_url)
            lines.append(content or "*（无正文）*")
            lines.append("")

    permissions = detail.get("permissions") if isinstance(detail.get("permissions"), dict) else {}
    lines.append("## 题目列表")
    lines.append("")
    if problems:
        lines.append("| # | 题目 | 本地标记 | 标签 | 分数 | 通过 / 提交 | 题面 |")
        lines.append("|---:|---|---|---|---:|---:|---|")
        for problem in problems:
            state = problem.get("state") or STATE_TODO
            tags = problem.get("tags") or []
            order = str(problem.get("displayOrder"))
            file_rel = files.get(order)
            if file_rel:
                cell = md_link(md_cell(problem.get("title") or ""), file_rel)
                if has_statement.get(order):
                    statement_state = "已保存"
                elif has_statement:
                    statement_state = "缺失"
                else:
                    statement_state = "未抓取"
            else:
                cell = md_cell(problem.get("title") or "") + "（未存档题面）"
                statement_state = "未归档"
            accepted = problem.get("numAcceptedUsers")
            submitted = problem.get("numSubmittedUsers")
            ratio = "%s / %s" % ("-" if accepted is None else accepted,
                                 "-" if submitted is None else submitted)
            score = problem.get("scoreWeight")
            lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                problem.get("displayOrder"), cell, STATE_LABEL.get(state, state),
                ", ".join("`%s`" % md_cell(tag) for tag in tags) or "-",
                "-" if score is None else score, ratio, statement_state))
        lines.append("")
    else:
        if permissions.get("canViewProblems") is False:
            lines.append("*该比赛未对外开放题目列表（`canViewProblems = false`），因此没有题面可归档。*")
        else:
            lines.append("*这场比赛没有题目（可能是讲座 / 公告类比赛）。*")
        lines.append("")

    lines.append("## 相关链接")
    lines.append("")
    if source:
        lines.append("- 原站比赛页：%s" % source)
        if problems:
            lines.append("- 原站题目页：%s" % site_link(contest, 1))
    lines.append("- 本地索引：[返回总目录](../index.md)")
    lines.append("")
    lines.append("---")
    lines.append("*归档于 %s · %s · 内容以原站为准*" % (archived_at, generator))
    lines.append("")
    return "\n".join(lines)


def render_problem_markdown(bundle: dict) -> str:
    """生成单题 Markdown（题面 + 元信息 + 本地标记）。

    可选 bundle 键：link_builder(contest, display_order=None)、generator。
    """
    contest = bundle["contest"]
    problem = bundle["problem"]
    statement = normalize_markdown(bundle.get("statement") or "")
    statement = absolutize_links(statement, bundle.get("base_url") or "")
    error = bundle.get("error") or ""
    archived_at = bundle.get("archived_at") or fmt_local_iso(time.time())
    base_url = bundle.get("base_url") or ""
    generator = bundle.get("generator") or ("nflsoj %s" % __version__)
    link_builder = bundle.get("link_builder")
    state = problem.get("state") or STATE_TODO
    if bundle.get("source_url"):
        source = bundle["source_url"]
    elif link_builder:
        source = link_builder(contest, problem.get("displayOrder")) if base_url else ""
    else:
        source = (contest_link(base_url, contest.get("id"), problem.get("displayOrder"))
                  if base_url else "")
    pairs = [
        ("title", problem.get("title") or ""),
        ("problem_id", problem.get("problemId")),
        ("contest_id", contest.get("id")),
        ("contest_name", contest.get("name") or ""),
        ("display_order", problem.get("displayOrder")),
        ("tags", list(problem.get("tags") or [])),
        ("score_weight", problem.get("scoreWeight")),
        ("local_state", state),
        ("submitted", bool(problem.get("submitted"))),
        ("accepted_users", problem.get("numAcceptedUsers")),
        ("submitted_users", problem.get("numSubmittedUsers")),
        ("archived_at", archived_at),
        ("source", source),
        ("generator", generator),
    ]
    lines = [build_front_matter(pairs), "",
             "# %s. %s" % (problem.get("displayOrder"), problem.get("title") or ""), ""]
    contest_name = contest.get("name") or "比赛 %s" % contest.get("id")
    lines.append("> 比赛 %s · 题目 #%s · 本地标记：%s" % (
        md_link(md_cell(contest_name), "../index.md"),
        problem.get("problemId"), STATE_LABEL.get(state, state)))
    lines.append("")
    extras = []
    tags = problem.get("tags") or []
    if tags:
        extras.append("**标签**：" + " ".join("`%s`" % tag for tag in tags))
    if problem.get("numAcceptedUsers") is not None or problem.get("numSubmittedUsers") is not None:
        extras.append("**通过 / 提交**：%s / %s" % (
            problem.get("numAcceptedUsers", "-"), problem.get("numSubmittedUsers", "-")))
    if problem.get("scoreWeight") not in (None, ""):
        extras.append("**分值**：%s" % problem.get("scoreWeight"))
    if problem.get("timeLimit") not in (None, ""):
        extras.append("**时限**：%s" % problem.get("timeLimit"))
    if problem.get("memoryLimit") not in (None, ""):
        extras.append("**内存**：%s" % problem.get("memoryLimit"))
    if problem.get("bestStatus"):
        extras.append("**你的最好提交**：%s" % problem["bestStatus"])
    if extras:
        lines.append("  \n".join(extras))     # 两空格换行，保证渲染成分行
        lines.append("")
    lines.append("---")
    lines.append("")
    if statement:
        lines.append(statement)
    else:
        reason = error or "站点没有返回题面内容（可能需要报名比赛或该题受权限保护）"
        lines.append("*（未能获取题目正文：%s）*" % reason)
    lines.append("")
    lines.append("---")
    if source:
        lines.append("原始链接：%s  " % source)
    lines.append("归档于 %s · %s" % (archived_at, generator))
    lines.append("")
    return "\n".join(lines)


def render_root_index(records, username: str = "", title: str = "NFLSOJ 本地存档",
                      generator: str = None, tool: str = "nflsoj") -> str:
    """生成存档根目录的 index.md。"""
    now = fmt_local_iso(time.time())
    generator = generator or ("nflsoj %s" % __version__)
    total_problems = sum(item.get("problems", 0) for item in records)
    total_statements = sum(item.get("statements", 0) for item in records)
    lines = [build_front_matter([
        ("title", title),
        ("user", username or ""),
        ("contests", len(records)),
        ("problems", total_problems),
        ("statements", total_statements),
        ("updated_at", now),
        ("generator", generator),
    ]), "",
        "# %s" % title, "",
        "> 账号 %s · 更新于 %s" % (username or "-", now), "",
        "共 **%d** 场比赛、**%d** 道题目，其中 **%d** 份题面已保存。" % (
            len(records), total_problems, total_statements), ""]
    if not records:
        lines.extend(["*还没有任何存档。运行 `%s archive --all` 开始归档。*" % tool, ""])
        return "\n".join(lines)
    lines.append("| 比赛 | ID | 模式 | 时间 | 题目 | 已完成 | 题面 |")
    lines.append("|---|---:|---|---|---:|---:|---:|")
    for item in records:
        contest = item.get("contest") or {}
        link = "%s/index.md" % item.get("dir")
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            md_link(md_cell(contest.get("name") or ""), link),
            contest.get("id"), md_cell(contest.get("mode") or "-"),
            fmt_interval(contest.get("startTime"), contest.get("endTime")),
            item.get("problems", 0), item.get("done", 0), item.get("statements", 0)))
    lines.append("")
    lines.append("---")
    lines.append("*由 %s 生成，内容以原站为准。*" % generator)
    lines.append("")
    return "\n".join(lines)


def collection_counts(problems) -> dict:
    counts = {STATE_TODO: 0, STATE_DOING: 0, STATE_DONE: 0}
    for problem in problems:
        state = problem.get("state") or STATE_TODO
        counts[state] = counts.get(state, 0) + 1
    return counts


def statement_cache_payload(problem_id, detail: dict) -> dict:
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    stats = detail.get("statistics") if isinstance(detail.get("statistics"), dict) else {}
    return {
        "id": problem_id,
        "title": meta.get("title") or "",
        "statement": detail.get("statement") or "",
        "statistics": stats,
    }


def archive_one_contest(store: Store, username: str, console: Console, client: Client,
                        contest: dict, root: str, with_statements: bool = True,
                        force: bool = False, workers: int = DEFAULT_WORKERS,
                        used_dirs=None) -> dict:
    """归档一场比赛，返回统计信息（不打印，由调用方汇总）。"""
    contest_id = contest.get("id")
    detail = client.contest_detail(contest_id, problems=True,
                                   description=True, announcements=True)
    if not isinstance(detail, dict):
        raise ApiError("服务器返回了非预期的比赛详情结构")
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    info = normalize_contest(meta)
    info["id"] = info.get("id") or contest_id
    if not info.get("name"):
        info["name"] = contest.get("name") or "比赛 %s" % contest_id
    problems = extract_problems(detail)
    # 合并本地标记与比赛信息
    entry = store.upsert_contest(username, info, problems)
    problems = entry.get("problems") or problems

    dir_name = contest_dir_name(entry, used_dirs)
    contest_dir = os.path.join(root, dir_name)
    problems_dir = os.path.join(contest_dir, "problems")
    archived_at = preserved_timestamp(os.path.join(contest_dir, "index.md"), force)

    statements = {}
    errors = {}
    if with_statements and problems:
        def fetch(problem):
            pid = problem.get("problemId")
            if pid is None:
                return problem, None, "题目缺少 ID"
            cached = None if force else store.load_statement(pid)
            if cached:
                return problem, cached, None
            try:
                data = client.problem_detail(pid, statement=True)
            except ApiError as exc:
                return problem, None, exc.message
            except Exception as exc:      # noqa: BLE001
                return problem, None, str(exc)
            payload = statement_cache_payload(pid, data)
            if payload.get("statement"):
                store.save_statement(pid, payload)
            else:
                return problem, None, "站点未返回题面"
            return problem, payload, None

        todo = [p for p in problems if p.get("problemId") is not None]
        if todo:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, 12))) as pool:
                for problem, payload, error in pool.map(fetch, todo):
                    if error:
                        errors[str(problem.get("problemId"))] = error
                    elif payload:
                        statements[str(problem.get("problemId"))] = payload

    files = {}
    has_statement = {}
    used_files = set()
    written = unchanged = 0
    for problem in problems:
        filename = problem_file_name(problem, used_files)
        rel = "problems/" + filename
        files[str(problem.get("displayOrder"))] = rel
        pid = problem.get("problemId")
        payload = statements.get(str(pid)) or {}
        has_statement[str(problem.get("displayOrder"))] = bool(payload.get("statement"))
        path = os.path.join(problems_dir, filename)
        content = render_problem_markdown({
            "contest": entry,
            "problem": problem,
            "statement": payload.get("statement"),
            "error": errors.get(str(pid), ""),
            # 沿用已有文件的归档时间，重复归档时内容不变、不重写
            "archived_at": preserved_timestamp(path, force, archived_at),
            "base_url": store.api_base,
        })
        if not with_statements and os.path.isfile(path):
            # 本次不抓题面：保留已有文件，别把之前存好的题面覆盖成占位文字
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    existing = handle.read()
            except OSError:
                existing = ""
            has_statement[str(problem.get("displayOrder"))] = (
                bool(existing) and "未能获取题目正文" not in existing)
            unchanged += 1
            continue
        status = write_if_changed(path, content)
        written += status == "written"
        unchanged += status == "unchanged"

    contest_md = render_contest_markdown({
        "contest": entry,
        "problems": problems,
        "detail": detail,
        "files": files,
        "has_statement": has_statement,
        "archived_at": archived_at,
        "base_url": store.api_base,
    })
    status = write_if_changed(os.path.join(contest_dir, "index.md"), contest_md)
    written += status == "written"
    unchanged += status == "unchanged"

    stats = collection_counts(problems)
    statement_total = len(statements) if with_statements else sum(1 for v in has_statement.values() if v)
    store.mark_archived(username, contest_id, contest_dir, len(problems), statement_total,
                        failed=len(errors))
    return {
        "contest": entry,
        "dir": dir_name,
        "path": contest_dir,
        "problems": len(problems),
        "statements": statement_total,
        "failed": errors,
        "files_written": written,
        "files_unchanged": unchanged,
        "done": stats[STATE_DONE],
    }


def archive_contests(store: Store, username: str, console: Console, client: Client,
                     targets, root: str, with_statements: bool = True, force: bool = False,
                     workers: int = DEFAULT_WORKERS) -> dict:
    """批量归档（逐场串行，内部并发抓题面），并刷新根目录 index.md。"""
    ensure_archive_root(root)
    results, failures = [], []
    used_dirs = set()
    progress = Progress(console, "归档比赛", total=len(targets), unit="场",
                        detail_width=30).start_line()
    try:
        for contest in targets:
            progress.update(0, total=len(targets),
                            detail=clean_text(contest.get("name"))[:26])
            try:
                result = archive_one_contest(store, username, console, client, contest, root,
                                             with_statements=with_statements, force=force,
                                             workers=workers, used_dirs=used_dirs)
                results.append(result)
            except ApiError as exc:
                failures.append((contest.get("id"), contest.get("name"), exc.message))
            except StoreError as exc:
                raise
            except Exception as exc:      # noqa: BLE001
                failures.append((contest.get("id"), contest.get("name"), str(exc)))
            progress.update(1, total=len(targets))
            _safe_save_progress(store, console)
    finally:
        progress.close()
        release_connections(client)

    # 根索引：合并「同一归档目录下」的既有存档记录
    root_norm = os.path.normcase(os.path.abspath(root))
    records = []
    for contest in store.all_contests(username):
        entry = store.contest_entry(username, contest.get("id")) or {}
        path = entry.get("archivePath")
        if not path or not os.path.isdir(path):
            continue
        path_norm = os.path.normcase(os.path.abspath(path))
        if path_norm != root_norm and not path_norm.startswith(root_norm + os.sep):
            continue          # 属于另一个归档目录，不写进本目录索引
        records.append({
            "contest": entry,
            "dir": os.path.basename(os.path.abspath(path).rstrip("\\/")),
            "problems": entry.get("archivedProblems", len(entry.get("problems") or [])),
            "statements": entry.get("archivedStatements", 0),
            "done": sum(1 for p in entry.get("problems") or [] if p.get("state") == STATE_DONE),
        })
    write_if_changed(os.path.join(root, "index.md"), render_root_index(records, username))
    store.save_progress()
    return {"results": results, "failures": failures, "records": records, "root": root}


# ---------------------------------------------------------------------------
# 命令辅助
# ---------------------------------------------------------------------------
def _pick_contest(store: Store, username: str, console: Console, spec):
    """按 ID / 名称找比赛，找不到时给出可操作的建议。"""
    contest, matches = resolve_contest(store, username, spec)
    if contest is not None:
        return contest
    if matches:
        console.error("匹配到 %d 场比赛，请用更精确的名称或直接使用 ID：" % len(matches))
        rows = [[(str(c.get("id")), "grey"), (c.get("name") or "",),
                 (fmt_date(c.get("startTime")), "grey")] for c in matches[:12]]
        console.table(["ID", "比赛名称", "开始日期"], rows,
                      aligns=["right", "left", "left"])
        console.muted("例如：nflsoj problems %s" % matches[0].get("id"))
        return None
    console.error("找不到比赛：%s" % spec)
    contests = store.all_contests(username)
    if contests:
        import difflib
        names = [c.get("name") or "" for c in contests]
        close = difflib.get_close_matches(str(spec), names, n=3, cutoff=0.45)
        if close:
            console.muted("你是不是想找：" + "  /  ".join(close))
        console.muted("本地共有 %d 场比赛，可用 `nflsoj contests` 查看，或先运行 `nflsoj sync`" % len(contests))
    else:
        console.muted("本地还没有比赛数据，先运行 `nflsoj sync` 同步")
    return None


def _ensure_problems(store: Store, username: str, console: Console, entry: dict, client: Client,
                     refresh: bool = False):
    """只在需要时联网补拉题目列表；失败但有缓存时降级使用缓存。"""
    # 已同步过（syncedAt 存在）就直接用缓存——哪怕题目列表为空：
    # 讲座 / 未开放题目的比赛本来就合法地有 0 题，重复联网既慢又会误报错误。
    if entry.get("syncedAt") and not refresh:
        return entry
    if entry.get("problems") and not refresh:
        return entry
    try:
        meta, problems, _ = fetch_contest_detail(client, entry.get("id"))
    except ApiError as exc:
        if entry.get("problems") or entry.get("syncedAt"):
            console.warn("刷新题目列表失败：%s（继续使用本地缓存）" % exc.message)
            return entry
        report_error(console, exc)
        return None
    entry = store.upsert_contest(username, meta, problems)
    _safe_save_progress(store, console)
    return entry


def _print_state_legend(console: Console) -> None:
    parts = []
    for state in STATE_ORDER:
        parts.append("%s %s" % (console.state_glyph(state), STATE_LABEL[state]))
    console.out("  " + "   ".join(parts))


def _split_state_tokens(tokens):
    """从 mark 的参数里拆出 (题号列表, 状态串或 None)。

    纯数字结尾一律当作题号（题号是数字），状态请用 todo / doing / done / 已做 等别名。
    """
    problems = list(tokens)
    state = None
    if len(problems) >= 2:
        last = str(problems[-1]).strip().lower()
        if last in STATE_ALIASES and not last.isdigit():
            state = problems.pop()
    elif len(problems) == 1:
        last = str(problems[0]).strip().lower()
        if last in STATE_ALIASES and not last.isdigit():
            # mark 12 done —— 只给了状态却没给题号，交由调用方提示
            return [], last
    return problems, state


# ---------------------------------------------------------------------------
# 命令：login / logout / whoami / config
# ---------------------------------------------------------------------------
def _verify_credentials(client: Client, console: Console) -> str:
    try:
        user = client.current_user()
    except ApiError as exc:
        raise ApiError("无法连接站点：%s" % exc.message, exc.status, exc.hint)
    if not user:
        raise ApiError("凭据无效：站点未识别为已登录状态", 401,
                       "请确认复制的是最新的 token/Cookie（网页登录后打开 F12 → Application → "
                       "Local Storage → session-swr 复制整段值）")
    return str(user.get("username") or user.get("id") or "unknown")


def cmd_logout(args, store: Store, console: Console) -> int:
    store.load()
    who = store.cached_username()
    store.clear_credentials()
    store.save_config()
    console.title("退出登录")
    console.ok("已清除本地登录凭据%s" % (("（原账号 %s）" % who) if who else ""))
    console.muted("做题标记与归档文件都保留在 %s" % store.data_dir)
    return 0


def cmd_whoami(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        info = client.session_info()
    except ApiError as exc:
        return report_error(console, exc)
    finally:
        client.close()
    info = info if isinstance(info, dict) else {}
    user = info.get("userMeta") if isinstance(info.get("userMeta"), dict) else None
    server_pref = info.get("serverPreference") if isinstance(info.get("serverPreference"), dict) else {}
    version = info.get("serverVersion") if isinstance(info.get("serverVersion"), dict) else {}
    console.title("当前登录状态", note=store.api_base)
    if not user:
        console.warn("未登录（匿名访问）")
        console.muted("运行 `nflsoj login` 后即可同步比赛与题目")
        return 0
    console.kv("用户名", str(user.get("username")), value_color="bgreen")
    console.kv("用户 ID", str(user.get("id")))
    console.kv("管理员", "是" if info.get("isManager") else "否")
    if user.get("joinedGroupsCount") is not None:
        console.kv("已加入小组", str(user.get("joinedGroupsCount")))
    console.rule()
    console.kv("站点", server_pref.get("siteName") or "-")
    version_text = str(version.get("hash") or "-")
    if version.get("date"):
        version_text += " (%s)" % str(version["date"])[:10]
    console.kv("服务端版本", version_text)
    console.kv("数据目录", store.data_dir)
    console.kv("归档目录", store.archive_dir)
    if store.cached_username():
        contests = store.all_contests(store.cached_username())
        stats = collection_counts([p for c in contests for p in c.get("problems") or []])
        console.kv("本地进度", "%d 场 / %d 题 · 已完成 %d" % (
            len(contests), sum(stats.values()), stats[STATE_DONE]))
    return 0


def cmd_config(args, store: Store, console: Console) -> int:
    changed = False
    if getattr(args, "set_api", None):
        store.config["baseUrl"] = args.set_api.rstrip("/")
        store.api_override = ""
        changed = True
    if getattr(args, "set_archive_dir", None):
        path = os.path.abspath(os.path.expanduser(args.set_archive_dir))
        store.config["archiveDir"] = path
        changed = True
    if getattr(args, "clear", False):
        store.clear_credentials()
        changed = True
    if changed:
        store.save_config()
        console.ok("配置已更新")
    console.title("本地配置")
    console.kv("站点地址", store.api_base)
    console.kv("配置文件", store.config_path)
    console.kv("记录文件", store.progress_path)
    console.kv("归档目录", store.archive_dir)
    token = store.config.get("token") or ""
    console.kv("Token", (token[:6] + "…" + token[-4:]) if len(token) > 16 else (token or "(未设置)"))
    console.kv("Cookie", "已保存" if store.config.get("cookie") else "(未设置)")
    console.kv("记录账号", store.config.get("username") or "(未设置)")
    console.muted("修改站点：nflsoj config --set-api URL")
    console.muted("修改归档目录：nflsoj config --set-archive-dir DIR")
    return 0


def cmd_login(args, store: Store, console: Console) -> int:
    base_url = (getattr(args, "api", None) or store.api_base or DEFAULT_API).rstrip("/")
    token = (args.token or os.environ.get("NFLSOJ_TOKEN", "") or "").strip()
    cookie = (args.cookie or "").strip()
    username = (args.username or "").strip()
    password = args.password or ""

    if token:
        token = extract_token(token) or token

    console.title("登录 NFLSOJ", note=base_url)
    cached = store.cached_username()
    if cached and not (token or cookie or username):
        console.kv("当前账号", cached, value_color="bgreen")
        console.muted("已登录时可以反复执行 login 来切换账号。")
        console.blank()

    if not (token or cookie or username):
        console.section("选择登录方式")
        choice = console.choose("请选择", [
            ("1", "粘贴浏览器 Cookie / Token / session-swr（推荐，最快）"),
            ("2", "使用用户名 + 密码登录"),
        ], default="1")
        if choice == "2":
            username = console.prompt("用户名")
            password = console.prompt("密码（输入时不显示）", mask=True)
            if not username or not password:
                console.error("用户名和密码都不能为空")
                return 1
        else:
            console.muted("获取方法：网页登录后按 F12 → Application → Local Storage →")
            console.muted("复制 session-swr 的值；或从 Network 面板复制 Cookie / Authorization。")
            raw = console.prompt("在此粘贴（支持裸 token、Cookie 串、session-swr JSON）")
            token = extract_token(raw) or ""
            if not token and raw:
                cookie = raw
            if not token and not cookie:
                console.error("无法从输入中识别出登录凭据，请检查是否粘贴完整")
                return 1

    client = Client(base_url=base_url, token=token, cookie=cookie,
                    insecure=getattr(args, "insecure", False),
                    timeout=getattr(args, "timeout", None) or 30,
                    debug=getattr(args, "debug", False),
                    offline=getattr(args, "offline", False))
    try:
        if username and not token and not cookie:
            console.info("正在使用用户名密码登录…")
            result = client.login(username, password)
            if not isinstance(result, dict):
                console.error("登录失败：服务器返回了非预期的响应")
                return 1
            if result.get("error"):
                console.error("登录失败：%s" % result["error"])
                return 1
            token = result.get("token") or ""
            if not token:
                console.error("登录失败：服务器未返回 token")
                return 1
            client.token = token
            username = result.get("username") or username
        console.info("正在校验登录状态…")
        who = _verify_credentials(client, console)
    except ApiError as exc:
        return report_error(console, exc)
    finally:
        client.close()

    store.set_credentials(token=token, cookie=cookie, username=who, base_url=base_url)
    store.save_config()
    console.blank()
    console.ok("登录成功，欢迎回来，%s ！" % who)
    console.kv("站点", store.api_base)
    console.kv("数据目录", store.data_dir)
    console.blank()
    console.section("接下来可以")
    console.bullet("nflsoj sync          同步全部比赛与题目（首次约 1 分钟）")
    console.bullet("nflsoj contests      查看比赛列表与完成进度")
    console.bullet("nflsoj archive --all 把比赛与题面归档成本地 Markdown")
    return 0


# ---------------------------------------------------------------------------
# 命令：sync / contests
# ---------------------------------------------------------------------------
def cmd_sync(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    console.title("同步比赛与题目", note=store.api_base)
    username = store.cached_username()
    if not username:
        # 只有本地还没有账号信息时才去问服务器（省掉一次约 2-3 秒的会话校验）
        try:
            username = resolve_username(store, console, client, online=True)
        except ApiError as exc:
            return report_error(console, exc)
    console.kv("账号", username, value_color="bgreen")
    if args.keyword:
        console.kv("关键词", args.keyword)
    if args.contest:
        console.kv("指定比赛", ", ".join(str(c) for c in args.contest))
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
        return report_error(console, exc)
    finally:
        client.close()

    console.blank()
    console.ok("同步完成：%d 场比赛 / %d 道题目，用时 %s" % (
        stats["contests"], stats["problems"], fmt_seconds(stats["elapsed"])))
    if stats["skipped"]:
        console.muted("其中 %d 场已有题目缓存，本次跳过详情请求（--force 可强制刷新）" % stats["skipped"])
    if stats["failed"]:
        console.blank()
        console.warn("%d 场比赛的题目拉取失败：" % len(stats["failed"]))
        rows = [[(str(cid), "grey"), (name or "",), (msg, "bred")]
                for cid, name, msg in stats["failed"][:10]]
        console.table(["比赛 ID", "比赛名称", "错误"], rows)
        console.muted("可稍后单独重试：nflsoj sync --contest <ID>")
    console.blank()
    console.section("下一步")
    console.bullet("nflsoj contests            查看比赛列表与进度")
    console.bullet("nflsoj problems <比赛ID>   查看某场比赛的题目")
    console.bullet("nflsoj archive --all       归档比赛与题面到本地 Markdown")
    return 0


def cmd_contests(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client,
                                    online=bool(args.live) and not store.cached_username())
    except ApiError as exc:
        return report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：nflsoj login")
        return 1

    contests = store.all_contests(username)
    source = "本地缓存"
    if args.live or not contests:
        if not contests and not args.live:
            console.warn("本地还没有比赛数据，正在自动同步一次…")
        progress = Progress(console, "获取比赛列表", unit="页").start_line()
        fetched = []
        try:
            fetched, pages = fetch_all_contest_pages(
                client, keyword=args.keyword,
                workers=getattr(args, "workers", DEFAULT_WORKERS),
                on_page=lambda done_pages, got, total: progress.update(
                    1, total=None, detail="已获取 %d 场 / %s 页" % (got, total or "?")))
        except ApiError as exc:
            progress.close()
            client.close()
            return report_error(console, exc)
        finally:
            progress.close()
        for contest in fetched:
            store.upsert_contest(username, contest, None)
        _safe_save_progress(store, console)
        contests = store.all_contests(username)
        source = "实时请求"
    client.close()

    keyword = (args.keyword or "").strip().lower()
    if keyword:
        contests = [c for c in contests
                    if keyword in (c.get("name") or "").lower() or keyword == str(c.get("id"))]
    if args.phase:
        contests = [c for c in contests
                    if contest_phase(c.get("startTime"), c.get("endTime"), c.get("phase")) == args.phase]
    if args.ids:
        wanted = {str(i) for i in args.ids}
        contests = [c for c in contests if str(c.get("id")) in wanted]

    console.title("比赛列表", note="共 %d 场" % len(contests))
    console.kv("账号", username, value_color="bgreen")
    console.kv("数据来源", source)
    console.blank()
    if not contests:
        console.warn("没有匹配的比赛")
        console.muted("可试试：nflsoj contests --live  （从服务器刷新）")
        return 0

    rows = []
    total_done = total_problems = 0
    phase_count = {"not_started": 0, "running": 0, "ended": 0}
    narrow = console.width < 96
    for contest in limit_take(contests, args.limit):
        phase = contest_phase(contest.get("startTime"), contest.get("endTime"), contest.get("phase"))
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
            # 窄终端：只显示最关键的 5 列，避免日期被截断
            rows.append([
                (console.phase_pill(phase),),
                (str(contest.get("id")), "grey"),
                (truncate(contest.get("name") or "", 30),),
                (fmt_date(contest.get("endTime")), "grey"),
                (prog,),
            ])
        else:
            rows.append([
                (console.phase_pill(phase),),
                (str(contest.get("id")), "grey"),
                (truncate(contest.get("name") or "", 26),),
                (str(contest.get("mode") or "-"), "magenta"),
                (fmt_date(contest.get("startTime")), "grey"),
                (fmt_date(contest.get("endTime")), "grey"),
                (prog,),
            ])
    if narrow:
        console.table(["状态", "ID", "比赛名称", "结束", "已完成"], rows,
                      aligns=["center", "right", "left", "left", "left"])
    else:
        console.table(["状态", "ID", "比赛名称", "模式", "开始", "结束", "已完成"],
                      rows, aligns=["center", "right", "left", "center", "left", "left", "left"])
    if limit_applies(contests, args.limit):
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
    console.muted("查看题目：nflsoj problems <ID>    归档：nflsoj archive <ID>")
    return 0


# ---------------------------------------------------------------------------
# 命令：problems / mark
# ---------------------------------------------------------------------------
def cmd_problems(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client, online=False)
    except ApiError as exc:
        return report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：nflsoj login")
        return 1

    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        return 1
    entry = store.contest_entry(username, contest.get("id")) or store.upsert_contest(username, contest, None)
    entry = _ensure_problems(store, username, console, entry, client, refresh=args.refresh)
    client.close()
    if entry is None:
        return 1

    problems = list(entry.get("problems") or [])
    if args.state:
        wanted = set()
        for raw in args.state:
            key = str(raw).strip().lower()
            if key not in STATE_ALIASES and key not in STATE_ORDER:
                console.error("无法识别的状态：%s" % raw)
                console.muted("可用状态：todo / doing / done，或：未完成 / 正在做 / 已完成")
                return 1
            wanted.add(STATE_ALIASES.get(key, key))
        problems = [p for p in problems
                    if isinstance(p, dict) and (p.get("state") or STATE_TODO) in wanted]

    if args.json:
        console.out(json.dumps({"contest": entry, "problems": problems},
                               ensure_ascii=False, indent=2, default=str))
        return 0

    print_contest_header(console, entry, store.api_base)
    console.blank()
    if not problems:
        if args.state:
            console.warn("没有处于「%s」状态的题目" % "/".join(str(s) for s in args.state))
            console.muted("去掉 --state 可查看全部题目")
        else:
            console.warn("该比赛没有题目（讲座类比赛或站点未开放题目列表）")
        if entry.get("syncedAt"):
            console.muted("上次同步：%s（--refresh 可重新拉取）" % human_age(entry.get("syncedAt")))
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
            cells.append((" ".join("#" + t for t in problem.get("tags") or []), "magenta"))
        cells.append((str(problem.get("problemId", "")), "grey"))
        if not narrow:
            cells.append((" · ".join(notes) if notes else "", "bcyan" if notes else "grey"))
            cells.append((human_age(problem.get("stateUpdatedAt")), "grey"))
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
    _print_state_legend(console)
    console.blank()
    first = entry.get("id")
    console.muted("标记完成：nflsoj mark %s 1        （默认标记为已完成，可一次多题）" % first)
    console.muted("网页地址：%s" % contest_link(store.api_base, entry.get("id")))
    console.muted("本地归档：nflsoj archive %s" % first)
    if args.open:
        url = contest_link(store.api_base, entry.get("id"))
        console.info("正在打开 %s" % url)
        webbrowser.open(url)
    return 0


def cmd_mark(args, store: Store, console: Console) -> int:
    client = client_for(store, args)
    try:
        username = resolve_username(store, console, client, online=False)
    except ApiError as exc:
        return report_error(console, exc)
    if not username:
        console.error("尚未登录")
        console.muted("请先运行：nflsoj login")
        return 1

    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        client.close()
        return 1
    entry = store.contest_entry(username, contest.get("id")) or store.upsert_contest(username, contest, None)
    entry = _ensure_problems(store, username, console, entry, client, refresh=args.refresh)
    client.close()
    if entry is None:
        return 1

    specs, state_token = _split_state_tokens(args.problems)
    if not specs:
        console.error("请至少指定一题，例如：nflsoj mark %s 1" % entry.get("id"))
        console.muted("可用 `nflsoj problems %s` 查看题号" % entry.get("id"))
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
        problem = resolve_problem(entry, spec)
        if problem is None:
            missing.append(str(spec))
            continue
        current = problem.get("state") or STATE_TODO
        if current not in STATE_ORDER:
            current = STATE_TODO
        target = STATE_ORDER[(STATE_ORDER.index(current) + 1) % len(STATE_ORDER)] if args.next else new_state
        try:
            store.set_state(username, entry.get("id"), problem.get("problemId"), target)
        except KeyError:
            missing.append(str(spec))
            continue
        changes.append((problem, current, target))

    saved = True
    if changes:
        saved = _safe_save_progress(store, console)
    console.title("更新做题标记", note="%s · #%s" % (entry.get("name") or "", entry.get("id")))
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
        if saved:
            console.ok("已保存 %d 题（%s）→ %s" % (len(changes), label, store.progress_path))
        else:
            console.error("标记未保存：%d 题（%s）写入失败，请检查数据目录" % (len(changes), label))
    if missing:
        console.error("找不到题目：%s" % "、".join(missing))
        console.muted("可用 `nflsoj problems %s` 查看题号列表" % entry.get("id"))

    problems = entry.get("problems") or []
    done = sum(1 for p in problems if p.get("state") == STATE_DONE)
    if problems:
        console.out("  %s %d/%d  ·  %s" % (
            console.paint("本场进度", "bcyan"), done, len(problems),
            console.bar(done, len(problems), width=28)))
    if missing and not changes:
        return 1
    return 0 if saved else 1


# ---------------------------------------------------------------------------
# 命令：stats
# ---------------------------------------------------------------------------
_SPARK = "▁▂▃▄▅▆▇█"


def _activity_buckets(contests, days: int = 7):
    today = _dt.date.today()
    buckets = [[today - _dt.timedelta(days=i), 0] for i in range(days - 1, -1, -1)]
    index = {bucket[0]: bucket for bucket in buckets}
    for contest in contests:
        for problem in contest.get("problems") or []:
            if not isinstance(problem, dict):
                continue
            ts = problem.get("stateUpdatedAt")
            if not ts:
                continue
            if isinstance(ts, str):
                ts, _ = parse_time(ts)
                if ts is None:
                    continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            try:
                day = _dt.date.fromtimestamp(ts)
            except (OverflowError, OSError, ValueError, TypeError):
                continue
            if day in index:
                index[day][1] += 1
    return buckets


def _sparkline(values) -> str:
    if not values:
        return ""
    top = max(values)
    if top <= 0:
        return "·" * len(values)
    out = []
    for value in values:
        if value <= 0:
            out.append("·")
        else:
            out.append(_SPARK[min(7, max(0, int(round(value / top * 7))))])
    return "".join(out)


def collect_stats(contests) -> dict:
    overall = {"contests": len(contests), "problems": 0,
               STATE_TODO: 0, STATE_DOING: 0, STATE_DONE: 0, "submitted": 0, "synced": 0}
    per_contest = []
    recent = []
    for contest in contests:
        problems = [p for p in (contest.get("problems") or []) if isinstance(p, dict)]
        row = {"contest": contest, "total": len(problems),
               STATE_TODO: 0, STATE_DOING: 0, STATE_DONE: 0, "submitted": 0}
        for problem in problems:
            state = problem.get("state")
            if state not in STATE_ORDER:
                state = STATE_TODO
            row[state] = row.get(state, 0) + 1
            overall[state] = overall.get(state, 0) + 1
            if problem.get("submitted"):
                row["submitted"] += 1
                overall["submitted"] += 1
            stamp = problem.get("stateUpdatedAt")
            if isinstance(stamp, str):
                stamp, _ = parse_time(stamp)
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
                recent.append((stamp, contest, problem))
        overall["problems"] += row["total"]
        if problems:
            overall["synced"] += 1
        per_contest.append(row)
    recent.sort(key=lambda item: item[0], reverse=True)
    return {"overall": overall, "per_contest": per_contest, "recent": recent}


def _select_contests(store: Store, username: str, console: Console, specs=None,
                     keyword=None, phase=None) -> list:
    contests = store.all_contests(username)
    if specs:
        picked = []
        for spec in specs:
            contest, _ = resolve_contest(store, username, spec)
            if contest is not None:
                picked.append(contest)
            else:
                console.warn("忽略无法匹配的比赛：%s" % spec)
        contests = picked
    if keyword:
        needle = keyword.lower()
        contests = [c for c in contests if needle in (c.get("name") or "").lower()]
    if phase:
        contests = [c for c in contests
                    if contest_phase(c.get("startTime"), c.get("endTime"), c.get("phase")) == phase]
    return contests


def cmd_stats(args, store: Store, console: Console) -> int:
    """完全本地统计，不联网（秒出）。"""
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据")
        console.muted("请先运行：nflsoj login 然后 nflsoj sync")
        return 1

    contests = _select_contests(store, username, console, args.contest, args.keyword, args.phase)
    if args.contest and not contests:
        console.error("没有匹配的比赛")
        console.muted("可用 `nflsoj contests` 查看本地比赛 ID")
        return 1
    stats = collect_stats(contests)
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
        console.warn("还没有题目数据，先运行 `nflsoj sync` 同步吧")
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
            pad_display(console.paint(STATE_LABEL[state],
                                      STATE_COLOR.get(state, "grey"), bold=True), 6),
            count, pct))
    buckets = _activity_buckets(contests, 7)
    values = [b[1] for b in buckets]
    console.blank()
    console.out("  %s  %s  %s" % (
        pad_display(console.paint("最近 7 天", "grey"), 10),
        console.paint(_sparkline(values), "bcyan"),
        console.paint("共 %d 次标记 · %s ~ %s" % (
            sum(values), buckets[0][0].strftime("%m-%d"), buckets[-1][0].strftime("%m-%d")),
            "grey")))
    if overall["submitted"]:
        console.blank()
        console.out("  %s 服务端记录你已提交过 %d 题（仅供参考）" % (
            console.paint("i", "bcyan"), overall["submitted"]))

    rows = []
    ranked = sorted(stats["per_contest"], key=lambda r: r["total"], reverse=True)
    narrow = console.width < 104
    for row in limit_take(ranked, args.limit):
        contest = row["contest"]
        phase = contest_phase(contest.get("startTime"), contest.get("endTime"), contest.get("phase"))
        if row["total"]:
            prog = "%s %3.0f%%" % (console.bar(row[STATE_DONE], row["total"], width=6),
                                   row[STATE_DONE] * 100.0 / row["total"])
        elif contest.get("syncedAt"):
            prog = console.paint("0 题", "grey")
        else:
            prog = console.paint("未同步", "grey")
        cells = [
            (console.phase_pill(phase),),
            (str(contest.get("id")), "grey"),
            (truncate(contest.get("name") or "", 26),),
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
            console.table(["状态", "ID", "比赛名称", "题目", "已完成", "进度"], rows,
                          aligns=["center", "right", "left", "right", "right", "left"])
        else:
            console.table(["状态", "ID", "比赛名称", "题目", "已完成", "正在做", "未完成", "进度"],
                          rows, aligns=["center", "right", "left", "right", "right", "right",
                                        "right", "left"])
        if limit_applies(ranked, args.limit):
            console.muted("仅显示前 %d 场，可用 --limit 调整（当前共 %d 场）" % (args.limit, len(ranked)))

    if stats["recent"] and not args.no_recent:
        console.blank()
        console.section("最近标记")
        recent_rows = []
        for ts, contest, problem in stats["recent"][: args.recent]:
            recent_rows.append([
                (console.state_pill(problem.get("state")),),
                (human_age(ts), "grey"),
                (truncate(contest.get("name") or "", 22),),
                ("%s. %s" % (problem.get("displayOrder"), problem.get("title") or ""),),
            ])
        console.table(["状态", "时间", "比赛", "题目"], recent_rows,
                      aligns=["center", "left", "left", "left"])
    console.muted("提示：nflsoj mark <比赛ID> <题号> 直接把题目标记为已完成")
    return 0


# ---------------------------------------------------------------------------
# 命令：open / export
# ---------------------------------------------------------------------------
def _open_path(path: str) -> bool:
    try:
        if os.name == "nt":
            os.startfile(path)       # noqa: S606 - 用户显式要求打开本地文件
        else:
            webbrowser.open(file_uri(path))
        return True
    except Exception:
        return False


def cmd_open(args, store: Store, console: Console) -> int:
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据，请先登录并同步")
        return 1
    contest = _pick_contest(store, username, console, args.contest)
    if contest is None:
        return 1

    display_order = None
    if args.problem:
        entry = store.contest_entry(username, contest.get("id")) or {}
        problem = resolve_problem(entry, args.problem)
        if problem is None:
            console.error("找不到题目：%s" % args.problem)
            console.muted("可用 `nflsoj problems %s` 查看题号" % contest.get("id"))
            return 1
        display_order = problem.get("displayOrder")

    url = contest_link(store.api_base, contest.get("id"), display_order)
    console.info(url)
    if not args.print_only:
        webbrowser.open(url)
    return 0


def iter_rows(contests):
    for contest in contests:
        for problem in contest.get("problems") or []:
            state = problem.get("state") or STATE_TODO
            yield {
                "contest_id": contest.get("id"),
                "contest_name": contest.get("name") or "",
                "display_order": problem.get("displayOrder"),
                "problem_id": problem.get("problemId"),
                "problem_title": problem.get("title") or "",
                "tags": " ".join(problem.get("tags") or []),
                "state": state,
                "state_label": STATE_LABEL.get(state, state),
                "updated_at": problem.get("stateUpdatedAt") or "",
            }


def cmd_export(args, store: Store, console: Console) -> int:
    username = store.cached_username()
    if not username:
        console.error("本地还没有账号数据，请先登录并同步")
        return 1
    contests = _select_contests(store, username, console, args.contest, args.keyword, args.phase)
    if args.contest and not contests:
        console.error("没有匹配的比赛")
        console.muted("可用 `nflsoj contests` 查看本地比赛 ID")
        return 1

    fmt = args.format
    buffer = io.StringIO()
    if fmt == "json":
        payload = {"user": username, "exportedAt": int(time.time()),
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
        for row in iter_rows(contests):
            writer.writerow(row)
    else:  # markdown
        buffer.write("# NFLSOJ 做题记录 · %s\n\n" % username)
        buffer.write("导出时间：%s  \n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        buffer.write("统计口径：本地手动标记（未完成 / 正在做 / 已完成）\n\n")
        for contest in contests:
            problems = contest.get("problems") or []
            done = sum(1 for p in problems if p.get("state") == STATE_DONE)
            phase = contest_phase(contest.get("startTime"), contest.get("endTime"),
                                  contest.get("phase"))
            buffer.write("## %s (#%s) — %d/%d · %s\n\n" % (
                contest.get("name") or "", contest.get("id"), done, len(problems),
                PHASE_LABEL.get(phase, "-")))
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
        atomic_write_text(path, text)
        console.ok("已导出到 %s" % path)
        console.muted("共 %d 场比赛" % len(contests))
    else:
        console.out(text.rstrip())
    return 0


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
        console.muted("请先运行：nflsoj login 然后 nflsoj sync")
        return 1

    if not args.contest and not args.all and not (args.keyword or args.phase or args.limit):
        console.error("请指定要归档的比赛")
        console.muted("全部归档：nflsoj archive --all")
        console.muted("单场归档：nflsoj archive 2189")
        console.muted("按关键词：nflsoj archive --keyword 树上")
        return 1

    if args.limit is not None and args.limit <= 0:
        console.warn("--limit %d 无效，已按不限制处理" % args.limit)

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
    if args.limit and args.limit > 0:
        targets = targets[: args.limit]
    if not targets:
        console.error("没有可归档的比赛")
        console.muted("先运行 `nflsoj sync` 同步比赛数据")
        return 1

    root = os.path.abspath(os.path.expanduser(args.dir or store.archive_dir))
    try:
        ensure_archive_root(root)
    except StoreError as exc:
        console.error(exc.message)
        if exc.hint:
            console.muted(exc.hint)
        return 1
    document = not args.no_statements
    if args.json:
        # JSON 模式保持 stdout 只有 JSON，方便脚本消费
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
                                 workers=args.workers)
    except ApiError as exc:
        return report_error(console, exc)
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
            rows.append([
                (str(item["contest"].get("id")), "grey"),
                (item["contest"].get("name") or "",),
                (str(item["problems"]),),
                (str(item["statements"]), "bgreen" if item["statements"] else "grey"),
                (str(failed), "bred" if failed else "grey"),
                (item["dir"], "grey"),
            ])
        console.table(["ID", "比赛", "题目", "题面", "失败", "归档目录"], rows,
                      aligns=["right", "left", "right", "right", "right", "left"])
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
                      [[(str(cid), "grey"), (name or "",), (msg, "bred")]
                       for cid, name, msg in result["failures"][:10]])
    if not result["results"] and not result["failures"]:
        console.warn("没有任何比赛被归档")

    index_path = os.path.join(result["root"], "index.md")
    if os.path.isfile(index_path):
        console.blank()
        console.muted("总目录：%s" % index_path)
        console.muted("用 Obsidian / Typora / VS Code 打开上面的目录即可浏览全部存档")
        if args.open:
            _open_path(index_path)
    return 0


# ---------------------------------------------------------------------------
# 概览面板
# ---------------------------------------------------------------------------
def cmd_dashboard(args, store: Store, console: Console) -> int:
    """不带子命令时显示的概览（默认全本地，不联网，瞬时返回）。"""
    console.title("NFLSOJ 命令行工具", note="v%s" % __version__)
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
        online = bool(store.config.get("token") or store.config.get("cookie"))
        console.kv("登录账号", username if online else "%s（本地缓存）" % username,
                   value_color="bgreen" if online else "yellow")
    else:
        console.kv("登录账号", "未登录", value_color="yellow")
    console.kv("站点", store.api_base)
    console.kv("数据目录", store.data_dir)

    contests = store.all_contests(username) if username else []
    if not username:
        console.blank()
        console.muted("首次使用：nflsoj login → nflsoj sync")
    elif not contests:
        console.blank()
        console.muted("还没有本地数据，运行 `nflsoj sync` 拉取全部比赛与题目")
    else:
        stats = collect_stats(contests)["overall"]
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
            console.muted("本地有 %d 场比赛记录，但还没有题目数据（试试 --force 重新同步）" % len(contests))

        phases = {"not_started": 0, "running": 0, "ended": 0}
        archived = 0
        for contest in contests:
            phase = contest_phase(contest.get("startTime"), contest.get("endTime"),
                                  contest.get("phase"))
            phases[phase] = phases.get(phase, 0) + 1
            if contest.get("archivePath"):
                archived += 1
        console.out("  %s 未开始 %d · 进行中 %d · 已结束 %d · 已归档 %d 场" % (
            console.paint("比赛", "grey"), phases.get("not_started", 0),
            phases.get("running", 0), phases.get("ended", 0), archived))

        buckets = _activity_buckets(contests, 7)
        values = [b[1] for b in buckets]
        if sum(values):
            console.out("  %s %s 共 %d 次标记" % (
                console.paint("最近 7 天", "grey"),
                console.paint(_sparkline(values), "bcyan"), sum(values)))

    console.blank()
    console.section("常用命令")
    console.table(["命令", "作用"], [
        [("login", "bcyan"), ("登录（粘贴 Cookie / Token，或用户名密码）",)],
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
            next_steps.append("nflsoj problems %s（继续最近一场未完成的比赛）"
                              % unfinished[0].get("id"))
        if not any(c.get("archivePath") for c in contests):
            next_steps.append("nflsoj archive --all（首次本地归档）")
        if next_steps:
            console.blank()
            console.section("建议下一步")
            for step in next_steps[:2]:
                console.bullet(step, "bcyan")
    console.blank()
    console.muted("完整帮助：nflsoj --help    数据目录：%s" % store.data_dir)
    return 0


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
EPILOG = """示例：
  登录与同步
    nflsoj login                                 交互式登录（推荐：粘贴浏览器 Cookie）
    nflsoj login -u 用户名 -p 密码                用账号密码登录
    nflsoj sync                                  增量同步比赛与题目（推荐）
    nflsoj sync --full                           全量重新同步所有比赛

  浏览
    nflsoj contests                              查看比赛列表与完成进度
    nflsoj contests --phase running              只看进行中的比赛
    nflsoj problems 2189                         查看 2189 号比赛的题目
    nflsoj problems 2189 --state todo --tags     只看未完成并显示标签

  标记（省略状态时默认标记为「已完成」）
    nflsoj mark 2189 1                           把第 1 题标记为已完成
    nflsoj mark 2189 1 2 3                       一次标记第 1、2、3 题
    nflsoj mark 2189 1 doing                     标记为正在做
    nflsoj mark 2189 1 todo                      标记为未完成
    nflsoj mark 2189 1 --clear                   清除标记
    nflsoj mark 2189 1 --next                    在 未完成→正在做→已完成 间循环

  归档与统计
    nflsoj archive 2189                          把该比赛的首页与全部题面存成 Markdown
    nflsoj archive --all                         归档全部已同步的比赛
    nflsoj archive --keyword 树上 --limit 5      按关键词归档
    nflsoj stats                                 做题情况统计（本地，秒出）
    nflsoj export -o 做题记录.md                  导出做题记录

  离线
    nflsoj --offline contests                    只用本地缓存，不发任何请求

  数据目录（默认 $NFLSOJ_DATA_DIR 或 ~/.nflsoj）
    nflsoj --data-dir D:\\ojdata login           临时指定数据目录
    set NFLSOJ_DATA_DIR=D:\\ojdata               设置默认数据目录（Windows PowerShell：$env:NFLSOJ_DATA_DIR=...）

数据存储：%s
""" % DEFAULT_DATA_DIR


def build_parser() -> argparse.ArgumentParser:
    # 统一用 SUPPRESS 作为默认值：子解析器会把自身默认值写回命名空间，
    # 若用 None/False 作默认值，`nflsoj --api X contests` 中的 --api 会被子命令覆盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, metavar="PATH",
                        help="数据目录（默认环境变量 %s，否则 %s）" % (DATA_DIR_ENV, DEFAULT_DATA_DIR))
    common.add_argument("--api", default=argparse.SUPPRESS, metavar="URL",
                        help="站点 API 地址（默认 %s）" % DEFAULT_API)
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
        prog="nflsoj",
        description="NFLSOJ (nflsoi.cc) 命令行工具 —— 登录、同步比赛与题目、标记做题状态、"
                    "统计进度、把比赛与题面归档成本地 Markdown。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument("--version", action="version", version="nflsoj %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    p = sub.add_parser("login", parents=[common], help="登录（Cookie / Token / 用户名密码）",
                       description="登录 NFLSOJ。不带参数运行时采用交互式引导。")
    p.add_argument("--token", help="直接提供 token")
    p.add_argument("--cookie", help="直接提供浏览器 Cookie 串")
    p.add_argument("-u", "--username", help="用户名")
    p.add_argument("-p", "--password", help="密码")
    p.set_defaults(handler=cmd_login, writes=True)

    p = sub.add_parser("logout", parents=[common], help="清除本地登录凭据")
    p.set_defaults(handler=cmd_logout, writes=True)

    p = sub.add_parser("whoami", parents=[common], help="显示当前登录状态（联网校验）")
    p.set_defaults(handler=cmd_whoami)

    p = sub.add_parser("config", parents=[common], help="查看 / 修改本地配置")
    p.add_argument("--set-api", metavar="URL", help="设置站点 API 地址")
    p.add_argument("--set-archive-dir", metavar="DIR", help="设置默认归档目录")
    p.add_argument("--clear", action="store_true", help="清除登录凭据")
    p.set_defaults(handler=cmd_config, writes=True)

    p = sub.add_parser("sync", parents=[common], help="同步比赛与题目列表",
                       description="同步比赛列表与每场比赛的题目。默认增量：连续两页都已同步即停止，"
                                   "并跳过已有题目缓存的比赛。")
    p.add_argument("--keyword", help="只同步名称包含该关键词的比赛")
    p.add_argument("--contest", nargs="*", type=int, metavar="ID", help="只同步指定 ID 的比赛")
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
    p.add_argument("--ids", nargs="*", type=int, metavar="ID", help="只显示指定 ID")
    p.add_argument("--limit", type=arg_int_min(0), default=50, metavar="N",
                   help="最多显示多少场（默认 50；0 = 全部）")
    p.set_defaults(handler=cmd_contests)

    p = sub.add_parser("problems", parents=[common], aliases=["ps"], help="列出某场比赛的题目",
                       description="列出某场比赛的题目与本地标记。默认读缓存，必要时才联网补拉。")
    p.add_argument("contest", help="比赛 ID 或名称关键词")
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
    p.add_argument("contest", help="比赛 ID 或名称关键词")
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
    p.add_argument("contest", help="比赛 ID 或名称关键词")
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

    p = sub.add_parser("archive", parents=[common], aliases=["arc"],
                       help="把比赛首页与题面归档成本地 Markdown",
                       description="把比赛首页（说明 / 公告 / 题目列表）与每道题的题面保存为本地 "
                                   "Markdown，并生成总目录。题面会缓存，重复运行只写有变化的文件。")
    p.add_argument("contest", nargs="*", metavar="SPEC", help="比赛 ID 或名称关键词（可多个）")
    p.add_argument("--all", action="store_true", help="归档本地全部已同步的比赛")
    p.add_argument("--keyword", help="按比赛名称过滤（可与 --all 一起用，也可单独使用）")
    p.add_argument("--phase", choices=["not_started", "running", "ended", "unknown"], help="按状态过滤")
    p.add_argument("--limit", type=arg_int_min(0), metavar="N",
                   help="最多归档多少场比赛（0 = 不限制）")
    p.add_argument("--dir", metavar="DIR", help="归档目录（默认 %s）" % "<数据目录>/archive")
    p.add_argument("--no-statements", action="store_true", help="只存比赛首页，不抓题目正文")
    p.add_argument("--force", action="store_true", help="忽略本地题面缓存，强制重新抓取")
    p.add_argument("--workers", type=arg_int_min(1),
                   default=DEFAULT_WORKERS, metavar="N",
                   help="并发抓取题面的线程数（默认 %d）" % DEFAULT_WORKERS)
    p.add_argument("--open", action="store_true", help="归档完成后打开总目录")
    p.add_argument("--json", action="store_true", help="以 JSON 输出归档结果")
    p.set_defaults(handler=cmd_archive, writes=True)

    return parser


def _prepare_streams() -> None:
    """把 stdout/stderr 切成 UTF-8 + replace；失败就静默跳过（不因编码问题崩掉）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None:
                continue
            encoding = (getattr(stream, "encoding", "") or "").lower()
            if encoding.replace("-", "") in ("utf8", "utf8mb4"):
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def prepare_streams() -> None:
    """公开别名（供 hydrooj 复用）。"""
    _prepare_streams()


def silence_broken_pipe() -> None:
    """公开别名（供 hydrooj 复用）。"""
    _silence_broken_pipe()


def _silence_broken_pipe() -> None:
    """下游已关闭管道时，把 stdout 重定向到 devnull，避免退出时刷屏 'Exception ignored'。"""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except Exception:
        pass


def main(argv=None) -> int:
    ansi_ok = enable_windows_ansi()
    _prepare_streams()
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
            color = False       # 老控制台不支持 ANSI，直接降级为无色

        console = Console(color=color, ascii_only=ascii_only,
                          width=getattr(args, "width", None),
                          progress=not getattr(args, "no_progress", False))
        data_dir = resolve_data_dir(getattr(args, "data_dir", None))
        store = Store(data_dir)
        store.load()
        report_store_problems(store, console)
        # --api 仅覆盖本次运行使用的地址（login / config 会显式写入配置）
        store.api_override = (getattr(args, "api", None) or "").rstrip("/")

        # 需要写盘的命令先探一次数据目录，避免做完网络请求才在最后一步崩掉
        if getattr(args, "writes", False) and not store.writable():
            console.error("数据目录不可写：%s" % store.data_dir)
            console.muted("该目录可能只读、被其他程序占用，或当前终端运行在受限沙箱中")
            console.muted("换一个可写目录后重试：")
            console.muted("nflsoj --data-dir <目录> %s" % (getattr(args, "command", "") or ""),
                          indent=8)
            console.muted("或设置环境变量 %s 指向可写目录" % DATA_DIR_ENV)
            console.muted("只读命令（contests / problems / stats / export / open）不受影响")
            return 1

        handler = getattr(args, "handler", None) or cmd_dashboard
        return handler(args, store, console)
    except SystemExit:
        raise                                    # --help / --version / 用法错误
    except ApiError as exc:
        return report_error(console or Console(color=False), exc)
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
        _silence_broken_pipe()
        return 0
    except Exception as exc:                     # noqa: BLE001 - 兜底：不再向用户抛 traceback
        active = console or Console(color=False)
        active.error("内部错误：%s: %s" % (type(exc).__name__, exc))
        if os.environ.get("NFLSOJ_TRACEBACK") or getattr(locals().get("args", None),
                                                         "debug", False):
            import traceback
            traceback.print_exc()
        else:
            active.muted("这是工具自身的缺陷；加 --debug 或设置 NFLSOJ_TRACEBACK=1 可查看完整堆栈")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        _silence_broken_pipe()
        sys.exit(0)
