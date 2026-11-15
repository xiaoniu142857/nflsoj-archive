"""hydrooj（Hydro OJ 版）的离线单元测试（不访问网络）。

运行：
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from hydrooj_archive import hydrooj as hj                      # noqa: E402
from nflsoj_archive import nflsoj as core                     # noqa: E402

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")

CID = "6a323c90c9f0ca7cf29b5b0c"


def make_tmp_dir() -> str:
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, "h%d" % time.time_ns())
    os.makedirs(path, exist_ok=True)
    return path


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tmp_dir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def make_console(width=100, ascii_only=False):
    stream = io.StringIO()
    console = core.Console(color=False, ascii_only=ascii_only, stream=stream, width=width)
    return console, stream


def make_payload():
    """模拟 /contest/<id>/problems 的返回（2 道题，与 pdict 一致）。"""
    return {
        "tdoc": {
            "_id": CID, "title": "NOI2026模拟赛63", "rule": "strictioi",
            "beginAt": "2026-07-09T23:45:00.000Z", "endAt": "2026-07-10T04:45:00.000Z",
            "pids": [37834, 37795], "attend": 26,
            "assign": ["nfls", "noi2026"], "content": "IOI赛制",
        },
        "pdict": {
            "37834": {"docId": 37834, "title": "Don't Look Back in Anger", "tag": [],
                      "config": {"timeMin": 1000, "timeMax": 1000, "memoryMin": 256, "memoryMax": 256}},
            "37795": {"docId": 37795, "title": "献给空白的无败冠冕（T2）", "tag": ["构造"],
                      "config": {"timeMin": 2000, "memoryMin": 512}},
        },
        "psdict": {},
        "tsdoc": {"attend": 1, "endPsdict": {
            "37795": {"docId": 37795, "uid": 1458, "score": 100, "status": 1, "statusText": "AC"}}},
    }


class LocalizedTests(unittest.TestCase):
    def test_json_localized_string(self):
        self.assertEqual(hj._localized('{"zh":"中文题面"}'), "中文题面")

    def test_dict_with_en(self):
        self.assertEqual(hj._localized({"en": "english"}), "english")

    def test_dict_fallback_first_string(self):
        self.assertEqual(hj._localized({"ja": "日本語"}), "日本語")

    def test_plain_string(self):
        self.assertEqual(hj._localized("普通文本"), "普通文本")

    def test_nested_json_string(self):
        inner = json.dumps({"zh": "双层"}, ensure_ascii=False)
        self.assertEqual(hj._localized(json.dumps(inner, ensure_ascii=False)), "双层")

    def test_invalid_json_returns_raw(self):
        raw = "{不是合法 json"
        self.assertEqual(hj._localized(raw), raw)

    def test_none_and_number(self):
        self.assertEqual(hj._localized(None), "")
        self.assertEqual(hj._localized(123), "")


class UrlTests(unittest.TestCase):
    def test_rule_label(self):
        self.assertEqual(hj.rule_label("strictioi"), "IOI")
        self.assertEqual(hj.rule_label("ACM"), "ACM")
        self.assertEqual(hj.rule_label("unknown-rule"), "unknown-rule")
        self.assertEqual(hj.rule_label(None), "自定义")

    def test_short_id(self):
        self.assertEqual(hj.short_id(CID), "6a323c90")
        self.assertEqual(hj.short_id(None), "")

    def test_contest_url(self):
        self.assertEqual(hj.contest_url("http://x:1/", CID), "http://x:1/contest/" + CID)

    def test_problem_url_with_and_without_tid(self):
        self.assertEqual(hj.problem_url("http://x:1", CID, 37834),
                         "http://x:1/p/37834?tid=" + CID)
        self.assertEqual(hj.problem_url("http://x:1", None, 37834), "http://x:1/p/37834")

    def test_link_builder(self):
        build = hj.link_builder_for("http://x", {1: 37834, 2: 37795})
        self.assertEqual(build({"id": CID}), "http://x/contest/" + CID)
        self.assertEqual(build({"id": CID}, 2), "http://x/p/37795?tid=" + CID)
        self.assertEqual(build({"id": CID}, 9), "http://x/contest/" + CID)   # 缺题号回退


class CookieTests(unittest.TestCase):
    def test_parse_cookie_string(self):
        cookies = hj.parse_cookie_string("sid=abc; sid.sig=def;  other=1 ")
        self.assertEqual(cookies["sid"], "abc")
        self.assertEqual(cookies["sid.sig"], "def")
        self.assertEqual(cookies["other"], "1")

    def test_parse_cookie_url_encoded(self):
        self.assertEqual(hj.parse_cookie_string("x=a%20b")["x"], "a b")

    def test_parse_cookie_ignores_junk(self):
        self.assertEqual(hj.parse_cookie_string(";;bad=;=x;ok=1")["ok"], "1")

    def test_cookie_string_roundtrip(self):
        text = hj.cookie_string({"sid": "a", "sid.sig": "b"})
        self.assertEqual(text, "sid=a; sid.sig=b")
        self.assertEqual(hj.parse_cookie_string(text), {"sid": "a", "sid.sig": "b"})


class NormalizeTests(unittest.TestCase):
    def test_contest_from_tdoc(self):
        contest = hj.contest_from_tdoc(make_payload()["tdoc"], None)
        self.assertEqual(contest["id"], CID)
        self.assertEqual(contest["name"], "NOI2026模拟赛63")
        self.assertEqual(contest["mode"], "IOI")
        self.assertEqual(contest["rule"], "strictioi")
        self.assertEqual(contest["pids"], [37834, 37795])
        self.assertEqual(contest["groups"], ["nfls", "noi2026"])
        self.assertFalse(contest["attended"])
        self.assertEqual(contest["description"], "IOI赛制")

    def test_contest_attended(self):
        contest = hj.contest_from_tdoc(make_payload()["tdoc"], {"attend": 1})
        self.assertTrue(contest["attended"])

    def test_contest_from_empty(self):
        contest = hj.contest_from_tdoc({}, None)
        self.assertEqual(contest["name"], "比赛 ")
        self.assertEqual(contest["pids"], [])

    def test_extract_problems_order_and_limits(self):
        problems = hj.extract_problems(make_payload())
        self.assertEqual([p["displayOrder"] for p in problems], [1, 2])
        first = problems[0]
        self.assertEqual(first["problemId"], 37834)
        self.assertEqual(first["title"], "Don't Look Back in Anger")
        self.assertEqual(first["timeLimit"], "1000 ms")
        self.assertEqual(first["memoryLimit"], "256 MB")
        second = problems[1]
        self.assertEqual(second["tags"], ["构造"])
        self.assertEqual(second["timeLimit"], "2000 ms")     # 只有 timeMin 时显示单值
        self.assertEqual(second["memoryLimit"], "512 MB")

    def test_extract_problems_time_range(self):
        payload = make_payload()
        payload["pdict"]["37834"]["config"] = {"timeMin": 1000, "timeMax": 2000,
                                               "memoryMin": 256, "memoryMax": 512}
        first = hj.extract_problems(payload)[0]
        self.assertEqual(first["timeLimit"], "1000-2000 ms")
        self.assertEqual(first["memoryLimit"], "256-512 MB")

    def test_extract_problems_status_from_endPsdict(self):
        problems = hj.extract_problems(make_payload())
        self.assertTrue(problems[1]["submitted"])
        self.assertEqual(problems[1]["bestStatus"], "AC")
        self.assertEqual(problems[1]["score"], 100)
        self.assertNotIn("submitted", problems[0])

    def test_extract_problems_fallback_to_pdict_keys(self):
        payload = {"tdoc": {"pids": []},
                   "pdict": {"11": {"title": "A"}, "10": {"title": "B"}}}
        problems = hj.extract_problems(payload)
        self.assertEqual([p["problemId"] for p in problems], [10, 11])

    def test_extract_problems_empty(self):
        self.assertEqual(hj.extract_problems({}), [])

    def test_problem_statement(self):
        payload = {"pdoc": {"content": json.dumps({"zh": "## 题目描述\n内容"}, ensure_ascii=False)}}
        self.assertEqual(hj.problem_statement(payload), "## 题目描述\n内容")

    def test_problem_statement_missing(self):
        self.assertEqual(hj.problem_statement({}), "")
        self.assertEqual(hj.problem_statement({"pdoc": {"content": ""}}), "")

    def test_statement_payload(self):
        problem = {"problemId": 1, "title": "T", "tags": ["dp"],
                   "timeLimit": "1000 ms", "memoryLimit": "256 MB"}
        record = hj.statement_payload(problem, {"pdoc": {"content": "正文", "html": False}})
        self.assertEqual(record["statement"], "正文")
        self.assertEqual(record["id"], 1)
        self.assertEqual(record["tags"], ["dp"])
        self.assertIn("fetchedAt", record)
        self.assertTrue(record["generator"].startswith("hydrooj "))

    def test_data_dir_priority(self):
        old = os.environ.get(hj.DATA_DIR_ENV)
        try:
            os.environ[hj.DATA_DIR_ENV] = os.path.join(TMP_ROOT, "env-dir")
            self.assertTrue(hj.resolve_data_dir(None).endswith("env-dir"))
            self.assertTrue(hj.resolve_data_dir(os.path.join(TMP_ROOT, "cli-dir")).endswith("cli-dir"))
        finally:
            if old is None:
                os.environ.pop(hj.DATA_DIR_ENV, None)
            else:
                os.environ[hj.DATA_DIR_ENV] = old
        self.assertTrue(hj.resolve_data_dir(None).startswith(os.path.expanduser("~")))


class FilterTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.console, self.stream = make_console()
        base = {"startTime": "2026-01-01T00:00:00.000Z", "endTime": "2026-01-02T00:00:00.000Z"}
        self.store.upsert_contest("u", dict(base, id="aaaa1111", name="模拟赛 一"), [])
        self.store.upsert_contest("u", dict(base, id="aaaa2222", name="模拟赛 二"), [])
        self.store.upsert_contest("u", dict(base, id="bbbb3333", name="周赛"), [])

    def test_apply_filters_keyword(self):
        contests = self.store.all_contests("u")
        self.assertEqual(len(hj._apply_filters(contests, keyword="模拟赛")), 2)
        self.assertEqual(len(hj._apply_filters(contests, keyword="周赛")), 1)

    def test_apply_filters_id_prefix(self):
        contests = self.store.all_contests("u")
        picked = hj._apply_filters(contests, ids=["aaaa"])
        self.assertEqual({c["id"] for c in picked}, {"aaaa1111", "aaaa2222"})

    def test_apply_filters_phase(self):
        contests = self.store.all_contests("u")
        self.assertEqual(len(hj._apply_filters(contests, phase="ended")), 3)
        self.assertEqual(len(hj._apply_filters(contests, phase="running")), 0)

    def test_pick_contest_full_id(self):
        contest = hj._pick_contest(self.store, "u", self.console, "bbbb3333")
        self.assertEqual(contest["id"], "bbbb3333")

    def test_pick_contest_unique_prefix(self):
        contest = hj._pick_contest(self.store, "u", self.console, "bbbb")
        self.assertEqual(contest["id"], "bbbb3333")

    def test_pick_contest_ambiguous_prefix(self):
        self.assertIsNone(hj._pick_contest(self.store, "u", self.console, "aaaa"))
        self.assertIn("匹配到", self.stream.getvalue())

    def test_pick_contest_by_name(self):
        contest = hj._pick_contest(self.store, "u", self.console, "周赛")
        self.assertEqual(contest["id"], "bbbb3333")

    def test_pick_contest_missing(self):
        self.assertIsNone(hj._pick_contest(self.store, "u", self.console, "不存在"))
        self.assertIn("找不到比赛", self.stream.getvalue())


class ClientErrorTests(unittest.TestCase):
    def client(self):
        return hj.HydroClient("http://example.invalid", cookies={"sid": "x"}, retries=0)

    def test_headers_carry_cookies(self):
        client = self.client()
        headers = client._headers()
        self.assertEqual(headers["Cookie"], "sid=x")
        self.assertEqual(headers["Accept"], "application/json")

    def test_blocked_response_raises(self):
        client = self.client()
        client.request = lambda *a, **k: (503, "<h1>Web Access blocked</h1>")
        with self.assertRaises(core.ApiError) as ctx:
            client.get_json("/")
        self.assertIn("网关拦截", ctx.exception.message)

    def test_login_page_html_raises(self):
        client = self.client()
        client.request = lambda *a, **k: (200, '<html data-page="user_login">')
        with self.assertRaises(core.ApiError) as ctx:
            client.get_json("/")
        self.assertIn("登录状态无效", ctx.exception.message)

    def test_non_json_raises(self):
        client = self.client()
        client.request = lambda *a, **k: (200, "<html>nope</html>")
        with self.assertRaises(core.ApiError):
            client.get_json("/")

    def test_error_field_raises(self):
        client = self.client()
        body = json.dumps({"error": {"message": "Contest {1} not found."}})
        client.request = lambda *a, **k: (404, body)
        with self.assertRaises(core.ApiError) as ctx:
            client.get_json("/contest/x")
        self.assertIn("not found", ctx.exception.message)

    def test_guest_redirect_raises(self):
        client = self.client()
        body = json.dumps({"url": "/login?redirect=%2F", "UiContext": {}})
        client.request = lambda *a, **k: (200, body)
        with self.assertRaises(core.ApiError) as ctx:
            client.get_json("/")
        self.assertIn("登录状态无效", ctx.exception.message)

    def test_offline_raises(self):
        client = hj.HydroClient("http://example.invalid", offline=True)
        with self.assertRaises(core.ApiError):
            client.request("GET", "/")

    def test_user_info_parses_json_string(self):
        client = self.client()
        body = json.dumps({"UserContext": json.dumps({"_id": 1458, "uname": "yangjinhua"})})
        client.request = lambda *a, **k: (200, body)
        user = client.user_info()
        self.assertEqual(user["uname"], "yangjinhua")

    def test_user_info_guest_returns_empty(self):
        client = self.client()
        body = json.dumps({"UserContext": json.dumps({"_id": 0, "uname": "Guest"})})
        client.request = lambda *a, **k: (200, body)
        self.assertEqual(client.user_info(), {})

    def test_block_retry_then_success(self):
        """网关随机 503 时应自动重试（走传输层，不是替换 request）。"""
        client = hj.HydroClient("http://example.invalid", retries=2)
        calls = {"n": 0}

        def fake_http(method, path, query=None, body=None, headers=None, retries=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return 503, {}, b"<h1>Web Access blocked</h1>"
            return 200, {}, b'{"ok": true}'

        client.http.request = fake_http
        self.assertEqual(client.get_json("/"), {"ok": True})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(client.blocked, 1)


class FakeClient:
    """只实现 archive / sync 用到的两个接口。"""

    def __init__(self, payload=None, statements=None, fail=()):
        self.payload = payload or make_payload()
        self.statements = statements or {}
        self.fail = set(fail)
        self.blocked = 0

    def contest_problems(self, contest_id):
        if str(contest_id) in self.fail:
            raise core.ApiError("模拟失败", 500)
        return self.payload

    def problem(self, problem_id, contest_id=None):
        if int(problem_id) in self.fail:
            raise core.ApiError("模拟题面失败", 500)
        return self.statements.get(int(problem_id), {"pdoc": {"content": ""}})

    def close(self):
        pass


class ArchiveTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.console, self.stream = make_console()
        self.root = os.path.join(self.tmp, "archive")
        self.statements = {
            37834: {"pdoc": {"content": json.dumps({"zh": "## 题目描述\nA 的题面"},
                                                   ensure_ascii=False)}},
            37795: {"pdoc": {"content": json.dumps({"zh": "B 的题面"}, ensure_ascii=False)}},
        }

    def test_archive_layout(self):
        client = FakeClient(statements=self.statements)
        result = hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                                        {"id": CID, "name": "NOI2026模拟赛63"}, self.root)
        self.assertEqual(result["problems"], 2)
        self.assertEqual(result["statements"], 2)
        contest_dir = os.path.join(self.root, "%s-NOI2026模拟赛63" % CID)
        self.assertTrue(os.path.isfile(os.path.join(contest_dir, "index.md")))
        self.assertTrue(os.path.isfile(os.path.join(contest_dir, "problems", "01-Don't-Look-Back-in-Anger.md")))
        entry = self.store.contest_entry("yangjinhua", CID)
        self.assertIn("archivedAt", entry)
        self.assertEqual(entry["archivedStatements"], 2)

    def test_archive_markdown_content(self):
        client = FakeClient(statements=self.statements)
        hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                               {"id": CID, "name": "NOI2026模拟赛63"}, self.root)
        contest_dir = os.path.join(self.root, "%s-NOI2026模拟赛63" % CID)
        with open(os.path.join(contest_dir, "index.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('generator: "hydrooj ', text)
        self.assertIn("# NOI2026模拟赛63", text)
        self.assertIn(hj.DEFAULT_API + "/contest/" + CID, text)
        self.assertIn("## 题目列表", text)

        with open(os.path.join(contest_dir, "problems", "01-Don't-Look-Back-in-Anger.md"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("A 的题面", body)
        self.assertIn("**时限**：1000 ms", body)
        self.assertIn("**内存**：256 MB", body)
        self.assertIn("/p/37834?tid=" + CID, body)

    def test_archive_idempotent(self):
        client = FakeClient(statements=self.statements)
        hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                               {"id": CID, "name": "c"}, self.root)
        time.sleep(1.1)          # 跨秒：证明不会因为时间戳变化而重写
        second = hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                                        {"id": CID, "name": "c"}, self.root)
        self.assertEqual(second["files_written"], 0)
        self.assertGreaterEqual(second["files_unchanged"], 3)

    def test_force_refreshes_timestamp(self):
        client = FakeClient(statements=self.statements)
        hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                               {"id": CID, "name": "c"}, self.root)
        index = os.path.join(self.root, "%s-NOI2026模拟赛63" % CID, "index.md")
        with open(index, encoding="utf-8") as handle:
            first = core.preserved_timestamp(index, force=True)
        time.sleep(1.1)
        forced = hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                                        {"id": CID, "name": "c"}, self.root, force=True)
        self.assertGreater(forced["files_written"], 0)
        with open(index, encoding="utf-8") as handle:
            second = core.preserved_timestamp(index, force=True)
        self.assertNotEqual(first, second)

    def test_archive_uses_cache_without_refetch(self):
        client = FakeClient(statements=self.statements)
        hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                               {"id": CID, "name": "c"}, self.root)
        cached = self.store.load_statement(37834, hj.STATEMENT_CACHE_TTL)
        self.assertIn("A 的题面", cached["statement"])
        # 第二次不带 force：不应再请求题面（用一个会抛异常的 client 验证）
        strict = FakeClient(statements={}, fail=[37834, 37795])
        result = hj.archive_one_contest(self.store, "yangjinhua", self.console, strict,
                                        {"id": CID, "name": "c"}, self.root)
        self.assertEqual(result["failed"], {})
        self.assertEqual(result["statements"], 2)

    def test_archive_statement_failure(self):
        client = FakeClient(statements=self.statements, fail=[37795])
        result = hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                                        {"id": CID, "name": "c"}, self.root)
        self.assertIn("37795", result["failed"])
        self.assertEqual(result["statements"], 1)
        path = os.path.join(self.root, "%s-NOI2026模拟赛63" % CID,
                            "problems", "02-献给空白的无败冠冕（T2）.md")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("未能获取题目正文", handle.read())

    def test_archive_without_statements_keeps_existing(self):
        client = FakeClient(statements=self.statements)
        contest_dir = os.path.join(self.root, "%s-NOI2026模拟赛63" % CID)
        hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                               {"id": CID, "name": "c"}, self.root)
        os.remove(os.path.join(contest_dir, "problems", "02-献给空白的无败冠冕（T2）.md"))
        result = hj.archive_one_contest(self.store, "yangjinhua", self.console, client,
                                        {"id": CID, "name": "c"}, self.root,
                                        with_statements=False)
        with open(os.path.join(contest_dir, "problems",
                               "01-Don't-Look-Back-in-Anger.md"), encoding="utf-8") as handle:
            self.assertIn("A 的题面", handle.read())     # 旧题面被保留
        self.assertEqual(result["statements"], 1)         # 只统计现存有效题面

    def test_archive_contests_writes_root_index(self):
        client = FakeClient(statements=self.statements)
        out = hj.archive_contests(self.store, "yangjinhua", self.console, client,
                                  [{"id": CID, "name": "NOI2026模拟赛63"}], self.root)
        self.assertEqual(len(out["results"]), 1)
        index = os.path.join(self.root, "index.md")
        with open(index, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# Hydro OJ 本地存档", text)
        self.assertIn("%s-NOI2026模拟赛63/index.md" % CID, text)

    def test_archive_contests_records_failure(self):
        client = FakeClient(statements=self.statements, fail=[CID])
        out = hj.archive_contests(self.store, "yangjinhua", self.console, client,
                                  [{"id": CID, "name": "c"}], self.root)
        self.assertEqual(out["results"], [])
        self.assertEqual(len(out["failures"]), 1)


class SyncTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.console, self.stream = make_console()

    def test_sync_only_ids(self):
        client = FakeClient()
        stats = hj.sync_contests(self.store, "u", self.console, client,
                                 only_ids=[CID], with_problems=True)
        self.assertEqual(stats["contests"], 1)
        self.assertEqual(stats["problems"], 2)
        entry = self.store.contest_entry("u", CID)
        self.assertEqual(entry["name"], "NOI2026模拟赛63")
        self.assertEqual(entry["mode"], "IOI")
        self.assertEqual(len(entry["problems"]), 2)

    def test_sync_skips_known(self):
        client = FakeClient()
        hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID])
        stats = hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID])
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["contests"], 1)

    def test_sync_force_refetches(self):
        client = FakeClient()
        hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID])
        stats = hj.sync_contests(self.store, "u", self.console, client,
                                 only_ids=[CID], force=True)
        self.assertEqual(stats["skipped"], 0)

    def test_sync_preserves_mark(self):
        client = FakeClient()
        hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID])
        self.store.set_state("u", CID, 37834, core.STATE_DONE)
        hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID], force=True)
        entry = self.store.contest_entry("u", CID)
        states = {p["problemId"]: p.get("state") for p in entry["problems"]}
        self.assertEqual(states[37834], core.STATE_DONE)

    def test_sync_records_failure(self):
        client = FakeClient(fail=[CID])
        stats = hj.sync_contests(self.store, "u", self.console, client, only_ids=[CID])
        self.assertEqual(len(stats["failed"]), 1)
        self.assertEqual(stats["contests"], 0)

    def test_sync_no_problems(self):
        client = FakeClient()
        stats = hj.sync_contests(self.store, "u", self.console, client,
                                 only_ids=[CID], with_problems=False)
        self.assertEqual(stats["contests"], 1)
        self.assertEqual(stats["problems"], 0)


class MarkFlowTests(TmpCase):
    """直接验证共享的标记逻辑对 Hydro 数据结构同样成立。"""

    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.store.upsert_contest("u", hj.contest_from_tdoc(make_payload()["tdoc"]),
                                  hj.extract_problems(make_payload()))

    def test_default_mark_is_done(self):
        self.assertEqual(hj.DEFAULT_MARK_STATE, core.STATE_DONE)
        specs, state = core._split_state_tokens(["1"])
        self.assertEqual((specs, state), (["1"], None))

    def test_resolve_problem_by_order_and_id(self):
        entry = self.store.contest_entry("u", CID)
        self.assertEqual(core.resolve_problem(entry, "1")["problemId"], 37834)
        self.assertEqual(core.resolve_problem(entry, "37795")["displayOrder"], 2)

    def test_stats_collect(self):
        entry = self.store.contest_entry("u", CID)
        self.store.set_state("u", CID, 37834, core.STATE_DONE)
        stats = core.collect_stats(self.store.all_contests("u"))
        self.assertEqual(stats["overall"]["problems"], 2)
        self.assertEqual(stats["overall"][core.STATE_DONE], 1)
        entry = self.store.contest_entry("u", CID)
        self.assertEqual(entry["name"], "NOI2026模拟赛63")


if __name__ == "__main__":
    unittest.main()
