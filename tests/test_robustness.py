"""健壮性 / 边界 / 兼容性回归测试（纯离线；HTTP 用本机回环小服务）。

覆盖审计中发现并修复的问题：
  * 参数越界（--timeout / --width / --workers / --limit / --recent）
  * 本地数据文件损坏、结构异常、字段类型错误
  * 时间字段是 dict/list/bool/字符串时不崩
  * HTTP 重定向、gzip 损坏、跨域跳转、Retry-After
  * 归档目录指向文件、题面缓存文件名安全
  * 终端编码降级
"""
import gzip
import http.server
import io
import json
import os
import shutil
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from hydrooj_archive import hydrooj as hj                      # noqa: E402
from nflsoj_archive import nflsoj as core                     # noqa: E402

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")


def make_tmp_dir(prefix="r") -> str:
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, "%s%d" % (prefix, time.time_ns()))
    os.makedirs(path, exist_ok=True)
    return path


def rmtree_force(path):
    if not os.path.isdir(path):
        return
    for root_dir, _dirs, files in os.walk(path):
        for name in files:
            try:
                os.chmod(os.path.join(root_dir, name), 0o666)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir()

    def tearDown(self):
        rmtree_force(self.tmp)


def make_console(width=100):
    stream = io.StringIO()
    console = core.Console(color=False, ascii_only=False, stream=stream, width=width)
    return console, stream


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------
class ArgValidationTests(unittest.TestCase):
    def test_arg_int_range_accepts(self):
        parse = core.arg_int_range(1, 3600, "超时")
        self.assertEqual(parse("30"), 30)
        self.assertEqual(parse(45), 45)

    def test_arg_int_range_rejects_low(self):
        parse = core.arg_int_range(1, 3600, "超时")
        with self.assertRaises(core.argparse.ArgumentTypeError):
            parse("0")
        with self.assertRaises(core.argparse.ArgumentTypeError):
            parse("-1")

    def test_arg_int_range_rejects_high(self):
        parse = core.arg_int_range(1, 3600, "超时")
        with self.assertRaises(core.argparse.ArgumentTypeError):
            parse("3601")

    def test_arg_int_range_rejects_text(self):
        parse = core.arg_int_range(0, 10, "数量")
        with self.assertRaises(core.argparse.ArgumentTypeError):
            parse("abc")

    def test_clamp_timeout(self):
        self.assertEqual(core.clamp_timeout(-1), 30)      # 非法 → 默认值
        self.assertEqual(core.clamp_timeout(0), 30)
        self.assertEqual(core.clamp_timeout(5), 5)
        self.assertEqual(core.clamp_timeout(99999), core.TIMEOUT_MAX)
        self.assertEqual(core.clamp_timeout(None), 30)

    def test_clamp_workers(self):
        self.assertEqual(core.clamp_workers(0), core.WORKERS_MIN)
        self.assertEqual(core.clamp_workers(-5), core.WORKERS_MIN)
        self.assertEqual(core.clamp_workers(999), core.WORKERS_MAX)
        self.assertEqual(core.clamp_workers(6), 6)

    def test_limit_take(self):
        items = list(range(10))
        self.assertEqual(core.limit_take(items, 3), [0, 1, 2])
        self.assertEqual(core.limit_take(items, 0), items)      # 0 = 全部
        self.assertEqual(core.limit_take(items, None), items)
        self.assertEqual(core.limit_take(items, -5), items)     # 负数不再截断

    def test_limit_applies(self):
        self.assertTrue(core.limit_applies(list(range(10)), 3))
        self.assertFalse(core.limit_applies(list(range(2)), 3))
        self.assertFalse(core.limit_applies(list(range(10)), 0))

    def test_parser_rejects_bad_numbers(self):
        parser = core.build_parser()
        for argv in (["--timeout", "-1", "whoami"],
                     ["--timeout", "0", "whoami"],
                     ["sync", "--workers", "0", "--limit", "1"],
                     ["sync", "--workers", "-2"],
                     ["contests", "--limit", "-5"],
                     ["stats", "--recent", "-1"]):
            with self.assertRaises(SystemExit) as ctx:
                parser.parse_args(argv)
            self.assertEqual(ctx.exception.code, 2, argv)

    def test_parser_accepts_limit_zero(self):
        parser = core.build_parser()
        args = parser.parse_args(["contests", "--limit", "0"])
        self.assertEqual(args.limit, 0)

    def test_parser_accepts_unknown_phase(self):
        parser = core.build_parser()
        args = parser.parse_args(["contests", "--phase", "unknown"])
        self.assertEqual(args.phase, "unknown")


# ---------------------------------------------------------------------------
# 时间与阶段
# ---------------------------------------------------------------------------
class TimeRobustnessTests(unittest.TestCase):
    def test_parse_time_non_scalar(self):
        for value in ({"$date": 1}, [1, 2], object(), True, False, b"123"):
            self.assertEqual(core.parse_time(value), (None, None), value)

    def test_parse_time_zero_and_empty(self):
        self.assertEqual(core.parse_time(0), (None, None))
        self.assertEqual(core.parse_time(""), (None, None))
        self.assertEqual(core.parse_time(None), (None, None))

    def test_parse_time_valid_still_works(self):
        ts, _ = core.parse_time("2026-11-14T09:00:00.000Z")
        self.assertIsNotNone(ts)
        ts2, _ = core.parse_time(1700000000)
        self.assertAlmostEqual(ts2, 1700000000, delta=1)

    def test_parse_time_cache_does_not_break(self):
        # 同一值多次解析走缓存，结果一致
        for _ in range(3):
            self.assertEqual(core.parse_time("2026-01-01T00:00:00Z")[0],
                             core.parse_time("2026-01-01T00:00:00Z")[0])

    def test_human_age_tolerates_bad_values(self):
        for value in (None, "", 0, -1, {"a": 1}, [1], "不是时间", True, float("nan")):
            self.assertIsInstance(core.human_age(value), str)

    def test_human_age_accepts_string_time(self):
        # 很久以前的时间会显示成日期而不是「N 天前」，两者都算正常
        text = core.human_age("2020-01-01T00:00:00Z")
        self.assertTrue("前" in text or text.startswith("2020"), text)

    def test_contest_phase_unknown(self):
        self.assertEqual(core.contest_phase(None, None), "unknown")
        self.assertEqual(core.contest_phase({"a": 1}, [1, 2]), "unknown")
        self.assertEqual(core.PHASE_LABEL["unknown"], "时间未知")

    def test_phase_pill_unknown(self):
        console, _ = make_console()
        self.assertIn("时间未知", console.phase_pill("unknown"))

    def test_collect_stats_tolerates_bad_problems(self):
        contests = [{"id": 1, "problems": [
            {"state": "weird", "stateUpdatedAt": "2026-01-01T00:00:00Z"},
            {"state": None, "stateUpdatedAt": {"a": 1}},
            {"stateUpdatedAt": [1]},
        ]}]
        stats = core.collect_stats(contests)
        self.assertEqual(stats["overall"]["problems"], 3)
        self.assertEqual(stats["overall"][core.STATE_TODO], 3)
        self.assertEqual(len(stats["recent"]), 1)

    def test_activity_buckets_tolerates_bad_timestamps(self):
        buckets = core._activity_buckets([{"problems": [
            {"stateUpdatedAt": {"a": 1}},
            {"stateUpdatedAt": "不是时间"},
            {"stateUpdatedAt": int(time.time())},
        ]}], 7)
        self.assertEqual(sum(b[1] for b in buckets), 1)


# ---------------------------------------------------------------------------
# Store：损坏文件与结构异常
# ---------------------------------------------------------------------------
class StoreCorruptionTests(TmpCase):
    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def new_store(self):
        store = core.Store(self.tmp)
        store.load()
        return store

    def test_corrupt_config_is_backed_up(self):
        self.write("config.json", '{"token": "SECRET", "username": "alice"')   # 截断
        store = self.new_store()
        self.assertEqual(store.config, {})
        self.assertTrue(store.load_errors, "应记录载入错误")
        path, backup, _reason = store.load_errors[0]
        self.assertTrue(os.path.isfile(backup), "损坏文件必须被备份")
        with open(backup, encoding="utf-8") as handle:
            self.assertIn("SECRET", handle.read())      # 原数据没丢

    def test_corrupt_progress_is_backed_up(self):
        self.write("progress.json", "[1,2,3]")
        store = self.new_store()
        self.assertTrue(store.load_errors)
        self.assertTrue(os.path.isfile(store.load_errors[0][1]))

    def test_corrupt_file_reported_once_then_usable(self):
        self.write("progress.json", "{坏的")
        store = self.new_store()
        console, stream = make_console()
        core.report_store_problems(store, console)
        self.assertIn("本地数据文件损坏", stream.getvalue())
        self.assertEqual(store.all_contests("anyone"), [])

    def test_users_not_dict(self):
        self.write("progress.json", json.dumps({"version": 1, "users": []}))
        store = self.new_store()
        self.assertEqual(store.known_usernames(), [])
        self.assertEqual(store.all_contests("u"), [])
        self.assertTrue(store.structure_warnings)

    def test_user_not_dict(self):
        self.write("progress.json", json.dumps({"users": {"u": []}}))
        store = self.new_store()
        self.assertEqual(store.all_contests("u"), [])
        self.assertEqual(store.user_data("u"), {"contests": {}})

    def test_contests_not_dict(self):
        self.write("progress.json", json.dumps({"users": {"u": {"contests": []}}}))
        store = self.new_store()
        self.assertEqual(store.all_contests("u"), [])

    def test_problems_not_list(self):
        self.write("progress.json", json.dumps(
            {"users": {"u": {"contests": {"1": {"id": 1, "problems": "xyz"}}}}}))
        store = self.new_store()
        entry = store.contest_entry("u", 1)
        self.assertEqual(entry["problems"], [])
        self.assertTrue(store.structure_warnings)

    def test_problem_not_dict(self):
        self.write("progress.json", json.dumps(
            {"users": {"u": {"contests": {"1": {"id": 1, "problems": [None, 3, "x",
                                                                    {"problemId": 7}]}}}}}))
        store = self.new_store()
        problems = store.contest_entry("u", 1)["problems"]
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["problemId"], 7)

    def test_invalid_state_is_dropped(self):
        self.write("progress.json", json.dumps(
            {"users": {"u": {"contests": {"1": {"id": 1, "problems": [
                {"problemId": 1, "state": "weird", "stateUpdatedAt": 1}]}}}}}))
        store = self.new_store()
        problem = store.contest_entry("u", 1)["problems"][0]
        self.assertNotIn("state", problem)
        self.assertNotIn("stateUpdatedAt", problem)

    def test_config_field_types_are_coerced(self):
        self.write("config.json", json.dumps(
            {"baseUrl": 123, "username": ["a"], "token": 45, "archiveDir": 99}))
        store = self.new_store()
        # baseUrl 被归一成字符串，但它不是合法 URL：构造客户端时给出可操作错误
        self.assertIsInstance(store.api_base, str)
        self.assertIsInstance(store.cached_username(), str)
        with self.assertRaises(core.ApiError) as ctx:
            store.client()
        self.assertIn("站点地址无效", ctx.exception.message)
        self.assertEqual(store.config.get("token"), "45")

    def test_config_valid_url_still_builds_client(self):
        self.write("config.json", json.dumps(
            {"baseUrl": "https://example.com:20035", "token": "abc", "cookie": "c=1"}))
        store = self.new_store()
        client = store.client()
        self.assertEqual(client.token, "abc")
        self.assertEqual(client.cookie, "c=1")
        client.close()

    def test_config_cookies_type(self):
        self.write("config.json", json.dumps({"cookies": "sid=x"}))
        store = self.new_store()
        self.assertNotIn("cookies", store.config)

    def test_all_contests_mixed_id_types(self):
        self.write("progress.json", json.dumps(
            {"users": {"u": {"contests": {
                "1": {"id": 2, "startTime": "2026-01-01T00:00:00Z"},
                "2": {"id": "abc", "startTime": "2026-02-01T00:00:00Z"},
                "3": {"id": 3, "startTime": {"bad": 1}},
                "4": "not a dict",
            }}}}))
        store = self.new_store()
        contests = store.all_contests("u")
        self.assertEqual(len(contests), 3)      # 非 dict 条目被丢弃

    def test_statement_cache_bad_timestamp(self):
        store = self.new_store()
        path = store.statement_cache_path(42)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"statement": "正文", "fetchedAt": "不是数字"}, handle)
        self.assertIsNone(store.load_statement(42))        # 视为过期而不是崩溃

    def test_statement_cache_path_is_safe(self):
        store = self.new_store()
        for bad in ("../../etc/passwd", "a/b", "..", ""):
            path = store.statement_cache_path(bad)
            parent = os.path.dirname(os.path.abspath(path))
            self.assertEqual(parent, os.path.abspath(store.statement_cache_dir), bad)

    def test_archive_dir_default_when_invalid(self):
        self.write("config.json", json.dumps({"archiveDir": ["x"]}))
        store = self.new_store()
        self.assertTrue(store.archive_dir.endswith("archive"))


# ---------------------------------------------------------------------------
# HTTP 传输层（本机回环服务）
# ---------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    hits = []

    def do_GET(self):                     # noqa: N802
        type(self).hits.append(self.path)
        spec = self.routes.get(self.path)
        if spec is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = spec.get("status", 200)
        body = spec.get("body", b"{}")
        self.send_response(status)
        for key, value in spec.get("headers", {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):         # 静默
        pass


class HttpTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.routes = {}
        _Handler.hits = []

    def transport(self, **kw):
        kw.setdefault("retries", 0)
        kw.setdefault("timeout", 5)
        return core.HttpTransport(self.base, **kw)

    def test_redirect_does_not_consume_retries(self):
        """retries=0 时也必须能跟随 302（修复：重定向此前占用重试预算）。"""
        _Handler.routes = {
            "/old": {"status": 302, "headers": {"Location": "/new"}},
            "/new": {"body": b'{"ok": true}'},
        }
        transport = self.transport()
        status, _headers, body = transport.request("GET", "/old")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(transport.redirected, 1)
        transport.close()

    def test_relative_redirect_location(self):
        _Handler.routes = {
            "/a/old": {"status": 301, "headers": {"Location": "new"}},
            "/a/new": {"body": b'{"ok": 1}'},
        }
        transport = self.transport()
        status, _headers, _body = transport.request("GET", "/a/old")
        self.assertEqual(status, 200)
        transport.close()

    def test_redirect_loop_is_bounded(self):
        _Handler.routes = {"/loop": {"status": 302, "headers": {"Location": "/loop"}}}
        transport = self.transport()
        with self.assertRaises(core.ApiError) as ctx:
            transport.request("GET", "/loop")
        self.assertIn("跳转次数过多", ctx.exception.message)
        transport.close()

    def test_cross_origin_redirect_rejected(self):
        _Handler.routes = {"/away": {"status": 302,
                                     "headers": {"Location": "http://example.com/x"}}}
        transport = self.transport()
        with self.assertRaises(core.ApiError) as ctx:
            transport.request("GET", "/away")
        self.assertIn("其他地址", ctx.exception.message)
        transport.close()

    def test_retry_on_5xx(self):
        state = {"n": 0}

        class Flaky(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                state["n"] += 1
                code = 503 if state["n"] == 1 else 200
                body = b'{"ok": true}' if code == 200 else b"busy"
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Flaky)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = core.HttpTransport("http://127.0.0.1:%d" % server.server_address[1],
                                          retries=2, timeout=5)
            status, _headers, _body = transport.request("GET", "/x")
            self.assertEqual(status, 200)
            self.assertEqual(transport.retried, 1)
            transport.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_gzip_response_decoded(self):
        payload = json.dumps({"text": "中文"}).encode("utf-8")
        _Handler.routes = {"/gz": {"body": gzip.compress(payload),
                                   "headers": {"Content-Encoding": "gzip"}}}
        transport = self.transport()
        status, _headers, body = transport.request("GET", "/gz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"text": "中文"})
        transport.close()

    def test_broken_gzip_raises_apierror(self):
        _Handler.routes = {"/bad": {"body": b"\x1f\x8b\x08\x00broken",
                                    "headers": {"Content-Encoding": "gzip"}}}
        transport = self.transport()
        with self.assertRaises(core.ApiError) as ctx:
            transport.request("GET", "/bad")
        self.assertIn("gzip", ctx.exception.message)
        transport.close()

    def test_retry_after_is_honoured(self):
        state = {"n": 0}

        class Throttle(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                state["n"] += 1
                if state["n"] == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Throttle)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            transport = core.HttpTransport("http://127.0.0.1:%d" % server.server_address[1],
                                          retries=1, timeout=5)
            started = time.time()
            status, _headers, _body = transport.request("GET", "/x")
            self.assertEqual(status, 200)
            self.assertLess(time.time() - started, 3.0)      # 遵守 Retry-After: 0
            transport.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_bad_base_url_raises_apierror(self):
        for bad in ("http://[::1", "not a url", "ftp://host/x"):
            url = bad.replace("not a url", "://bad")
            with self.assertRaises(core.ApiError):
                core.HttpTransport(url).close()

    def test_negative_timeout_is_clamped(self):
        transport = core.HttpTransport(self.base, timeout=-5)
        self.assertGreaterEqual(transport.timeout, core.TIMEOUT_MIN)
        transport.close()

    def test_release_thread_connections_is_idempotent(self):
        _Handler.routes = {"/ok": {"body": b"{}"}}
        transport = self.transport()
        transport.request("GET", "/ok")
        transport.release_thread_connections()
        transport.release_thread_connections()
        transport.close()
        transport.close()


class HydroClientErrorTests(unittest.TestCase):
    def test_non_dict_json_raises_apierror(self):
        client = hj.HydroClient("http://example.invalid", cookies={})
        client.request = lambda *a, **k: (200, "[1,2,3]")
        with self.assertRaises(core.ApiError):
            client.get_json("/")

    def test_error_string_field(self):
        client = hj.HydroClient("http://example.invalid", cookies={})
        client.request = lambda *a, **k: (400, '{"error": "boom"}')
        with self.assertRaises(core.ApiError) as ctx:
            client.get_json("/")
        self.assertIn("boom", ctx.exception.message)

    def test_page_batch_filters_bad_rows(self):
        data = {"tdocs": [{"_id": "a", "title": "t"}, None, 3], "tsdict": {}}
        batch = hj._page_batch(data)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["id"], "a")

    def test_page_batch_non_list(self):
        self.assertEqual(hj._page_batch({"tdocs": "x"}), [])
        self.assertEqual(hj._page_batch(None), [])

    def test_max_contest_pages_guard(self):
        self.assertGreater(hj.MAX_CONTEST_PAGES, 0)


# ---------------------------------------------------------------------------
# 归档路径与文件名安全
# ---------------------------------------------------------------------------
class ArchivePathTests(TmpCase):
    def test_archive_root_pointing_to_file(self):
        target = os.path.join(self.tmp, "a-file")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(core.StoreError) as ctx:
            core.ensure_archive_root(target)
        self.assertIn("--dir", ctx.exception.hint)

    def test_archive_root_created(self):
        target = os.path.join(self.tmp, "deep", "archive")
        core.ensure_archive_root(target)
        self.assertTrue(os.path.isdir(target))

    def test_safe_cache_name(self):
        self.assertEqual(core.safe_cache_name(123), "123")
        self.assertEqual(core.safe_cache_name("../x"), "x")     # 路径分隔符被清掉，无法穿越
        self.assertEqual(core.safe_cache_name(""), "unknown")
        self.assertEqual(core.safe_cache_name(None), "unknown")
        self.assertNotIn("/", core.safe_cache_name("a/b/c"))
        self.assertNotIn("..", core.safe_cache_name("../../etc/passwd"))

    def test_problem_file_name_bad_order(self):
        used = set()
        name = core.problem_file_name({"displayOrder": "第1题", "title": "t"}, used)
        self.assertTrue(name.endswith(".md"))


# ---------------------------------------------------------------------------
# 终端输出健壮性
# ---------------------------------------------------------------------------
class ConsoleEncodingTests(unittest.TestCase):
    def test_out_falls_back_on_ascii_stream(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="")
        console = core.Console(color=False, stream=stream, width=80)
        console.out("中文标题 ✔")
        stream.flush()
        self.assertIn(b"?", raw.getvalue())          # 已降级而不是抛异常

    def test_write_falls_back_on_ascii_stream(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="")
        console = core.Console(color=False, stream=stream, width=80)
        console.write("中文")
        stream.flush()
        self.assertTrue(raw.getvalue())

    def test_width_clamped(self):
        # 0 / 负数表示「跟随终端」；过小的正数钳到下限；过大的钳到上限
        for given in (0, -5):
            self.assertEqual(core.Console._detect_width(given), 100, given)
        self.assertEqual(core.Console._detect_width(1), core.WIDTH_MIN)
        self.assertEqual(core.Console._detect_width(10000), core.WIDTH_MAX)

    def test_width_from_env(self):
        old = os.environ.get("NFLSOJ_WIDTH")
        try:
            os.environ["NFLSOJ_WIDTH"] = "77"
            self.assertEqual(core.Console._detect_width(None), 77)
            os.environ["NFLSOJ_WIDTH"] = "not-a-number"
            self.assertEqual(core.Console._detect_width(None), 100)
        finally:
            if old is None:
                os.environ.pop("NFLSOJ_WIDTH", None)
            else:
                os.environ["NFLSOJ_WIDTH"] = old

    def test_progress_close_is_idempotent(self):
        console, _ = make_console()
        progress = core.Progress(console, "x", total=2).start_line()
        progress.update(1)
        progress.close()
        progress.close()

    def test_stream_closed_does_not_raise(self):
        stream = io.StringIO()
        console = core.Console(color=False, stream=stream, width=80)
        stream.close()
        console.out("写不进去也不该抛")
        console.write("同样")


# ---------------------------------------------------------------------------
# 交互：空题单不再重复联网
# ---------------------------------------------------------------------------
class EnsureProblemsCacheTests(TmpCase):
    class Client:
        """任何联网调用都会失败的假客户端。"""

        def __init__(self):
            self.calls = 0

        def contest_detail(self, contest_id, problems=True, description=False,
                           announcements=False):
            self.calls += 1
            raise AssertionError("不应联网")

    def test_synced_empty_contest_uses_cache(self):
        store = core.Store(self.tmp)
        store.load()
        store.upsert_contest("u", {"id": 1, "name": "讲座"}, [])
        entry = store.contest_entry("u", 1)
        self.assertTrue(entry.get("syncedAt"))
        console, _ = make_console()
        client = self.Client()
        result = core._ensure_problems(store, "u", console, entry, client, refresh=False)
        self.assertIs(result, entry)
        self.assertEqual(client.calls, 0)

    def test_refresh_forces_network(self):
        store = core.Store(self.tmp)
        store.load()
        store.upsert_contest("u", {"id": 1, "name": "c"}, [])
        entry = store.contest_entry("u", 1)
        console, _ = make_console()
        client = self.Client()
        with self.assertRaises(AssertionError):
            core._ensure_problems(store, "u", console, entry, client, refresh=True)


if __name__ == "__main__":
    unittest.main()
