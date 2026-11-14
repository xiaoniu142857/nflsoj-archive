"""nflsoj 的离线单元测试（不访问网络）。

运行：
    python -m unittest discover -s tests -v
"""

import io
import errno
import json
import os
import shutil
import stat
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from nflsoj_archive import nflsoj as core  # noqa: E402

# 说明：本项目在受限沙箱里跑测试时，系统临时目录不允许 chmod，
# 因此临时目录统一建在仓库内的 .test-tmp/ 下。
TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")


def make_tmp_dir() -> str:
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, "t%d" % time.time_ns())
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


class TextWidthTests(unittest.TestCase):
    def test_char_width(self):
        self.assertEqual(core.char_width("a"), 1)
        self.assertEqual(core.char_width("中"), 2)
        self.assertEqual(core.char_width("\u0301"), 0)   # 组合字符

    def test_len_display_ignores_ansi(self):
        self.assertEqual(core.len_display("\x1b[31m中文\x1b[0m"), 4)
        self.assertEqual(core.len_display("abc"), 3)

    def test_truncate_keeps_within_width(self):
        text = "中文测试一二三"
        out = core.truncate(text, 5)
        self.assertLessEqual(core.len_display(out), 5)
        self.assertTrue(out.endswith("…"))

    def test_truncate_keeps_color_and_resets(self):
        out = core.truncate("\x1b[32mabcdef\x1b[0m", 4)
        self.assertLessEqual(core.len_display(out), 4)
        self.assertIn("\x1b[32m", out)

    def test_truncate_noop_when_fits(self):
        self.assertEqual(core.truncate("abc", 3), "abc")

    def test_pad_display(self):
        self.assertEqual(core.pad_display("中", 4), "中  ")
        self.assertEqual(core.pad_display("中", 4, "right"), "  中")

    def test_clean_text_strips_newlines(self):
        self.assertEqual(core.clean_text("a\nb\tc"), "a b c")

    def test_wrap_display(self):
        lines = core.wrap_display("abcdefgh", 3, "")
        self.assertTrue(all(core.len_display(line) <= 3 for line in lines))
        self.assertEqual("".join(lines), "abcdefgh")


class ConsoleTests(unittest.TestCase):
    def test_table_renders_and_fits_width(self):
        console, stream = make_console(width=60)
        console.table(["状态", "名称"], [[("已完成",), ("一个很长很长的题目名称" * 3,)]])
        text = stream.getvalue()
        self.assertIn("状态", text)
        for line in text.splitlines():
            self.assertLessEqual(core.len_display(line), 60)

    def test_fit_widths_respects_budget(self):
        console, _ = make_console()
        widths = console._fit_widths([50, 40, 30], 60)
        self.assertLessEqual(sum(widths), 60)

    def test_pill_without_color(self):
        console, _ = make_console()
        self.assertEqual(console.state_pill(core.STATE_DONE), "[已完成]")
        self.assertEqual(console.phase_pill("ended"), "[已结束]")

    def test_table_handles_short_rows(self):
        console, stream = make_console()
        console.table(["a", "b", "c"], [[("1",)]])
        self.assertIn("1", stream.getvalue())

    def test_progress_quiet_prints_nothing(self):
        console, stream = make_console()
        console.quiet = True
        progress = core.Progress(console, "测试", total=2).start_line()
        progress.update(1)
        progress.close()
        self.assertEqual(stream.getvalue(), "")


class TimeTests(unittest.TestCase):
    def test_parse_time_iso_z(self):
        ts, dt = core.parse_time("2026-11-14T09:00:00.000Z")
        self.assertIsNotNone(ts)
        self.assertEqual(core.fmt_time("2026-11-14T09:00:00.000Z"), "2026-11-14 17:00")

    def test_parse_time_invalid(self):
        self.assertEqual(core.parse_time("下周"), (None, None))
        self.assertEqual(core.parse_time(None), (None, None))

    def test_phase_prefers_server_value(self):
        self.assertEqual(core.contest_phase(None, None, "ENDED"), "ended")
        self.assertEqual(core.contest_phase(None, None, "SUBMISSION"), "running")
        self.assertEqual(core.contest_phase(None, None, "PENDING"), "not_started")

    def test_phase_from_time(self):
        self.assertEqual(core.contest_phase("2020-01-01T00:00:00Z",
                                            "2020-01-02T00:00:00Z"), "ended")
        self.assertEqual(core.contest_phase("2999-01-01T00:00:00Z",
                                            "2999-01-02T00:00:00Z"), "not_started")

    def test_fmt_interval_same_day(self):
        self.assertEqual(core.fmt_interval("2026-11-14T01:00:00Z", "2026-11-14T05:00:00Z"),
                         "2026-11-14 09:00 ~ 13:00")

    def test_fmt_seconds(self):
        self.assertEqual(core.fmt_seconds(9), "9 秒")
        self.assertEqual(core.fmt_seconds(125), "2 分 5 秒")

    def test_fmt_local_iso_offset(self):
        text = core.fmt_local_iso(1700000000)
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")


class StateTests(unittest.TestCase):
    def test_aliases(self):
        self.assertIs(core.STATE_ALIASES["ac"], core.STATE_DONE)
        self.assertIs(core.STATE_ALIASES["正在做"], core.STATE_DOING)
        self.assertIsNone(core.STATE_ALIASES["clear"])

    def test_split_state_tokens(self):
        self.assertEqual(core._split_state_tokens(["1", "2"]), (["1", "2"], None))
        self.assertEqual(core._split_state_tokens(["1", "done"]), (["1"], "done"))
        self.assertEqual(core._split_state_tokens(["done"]), ([], "done"))
        self.assertEqual(core._split_state_tokens(["1", "2", "doing"]), (["1", "2"], "doing"))

    def test_numeric_token_is_problem_not_state(self):
        # 纯数字结尾一律当题号，避免把 "2" 误判成 done
        self.assertEqual(core._split_state_tokens(["3", "2"]), (["3", "2"], None))

    def test_default_mark_state_is_done(self):
        self.assertEqual(core.DEFAULT_MARK_STATE, core.STATE_DONE)


class SlugTests(unittest.TestCase):
    def test_invalid_chars(self):
        self.assertEqual(core.slugify("a/b:c*d?"), "a-b-c-d")

    def test_spaces_become_dash(self):
        self.assertEqual(core.slugify("  Hello   World  "), "Hello-World")

    def test_reserved_name(self):
        self.assertEqual(core.slugify("CON"), "_CON")

    def test_empty(self):
        self.assertEqual(core.slugify("///"), "untitled")

    def test_length_cap(self):
        self.assertLessEqual(len(core.slugify("x" * 200)), 48)

    def test_keeps_chinese(self):
        self.assertEqual(core.slugify("S+线段树"), "S+线段树")


class MarkdownTests(unittest.TestCase):
    def test_normalize_crlf(self):
        self.assertEqual(core.normalize_markdown("a\r\nb"), "a\nb")

    def test_normalize_collapses_blank_lines(self):
        self.assertEqual(core.normalize_markdown("a\n\n\n\n\nb"), "a\n\nb")

    def test_normalize_closes_unclosed_fence(self):
        out = core.normalize_markdown("```cpp\nint main() {}\n")
        self.assertEqual(out.count("```"), 2)

    def test_normalize_keeps_code_indent(self):
        out = core.normalize_markdown("```\n  x\n```")
        self.assertIn("  x", out)

    def test_front_matter_quotes_strings(self):
        text = core.build_front_matter([("title", 'a "b"'), ("tags", ["x", "y"]),
                                        ("n", 3), ("ok", True), ("nothing", None)])
        self.assertIn('title: "a \\"b\\""', text)
        self.assertIn('tags: ["x", "y"]', text)
        self.assertIn("n: 3", text)
        self.assertIn("ok: true", text)
        self.assertIn("nothing: null", text)
        self.assertTrue(text.startswith("---\n") and text.endswith("---"))

    def test_md_cell_escapes_pipe(self):
        self.assertEqual(core.md_cell("a|b"), "a\\|b")
        self.assertEqual(core.md_cell("a\nb"), "a b")

    def test_absolutize_links(self):
        out = core.absolutize_links("[x](/judge/a.html)", "https://example.com/")
        self.assertEqual(out, "[x](https://example.com/judge/a.html)")

    def test_absolutize_links_ignores_external(self):
        text = "[x](https://a/b)"
        self.assertEqual(core.absolutize_links(text, "https://example.com"), text)

    def test_write_if_changed(self):
        tmp = make_tmp_dir()
        try:
            path = os.path.join(tmp, "sub", "a.md")
            self.assertEqual(core.write_if_changed(path, "hello"), "written")
            self.assertEqual(core.write_if_changed(path, "hello"), "unchanged")
            self.assertEqual(core.write_if_changed(path, "world"), "written")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "world")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NormalizeTests(unittest.TestCase):
    def test_normalize_contest(self):
        raw = {"id": "12", "title": "热身赛", "mode": "ACM",
               "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-02T00:00:00Z",
               "phase": "ENDED", "groups": [{"name": "S+"}, "2026"],
               "creator": {"username": "menci"}}
        contest = core.normalize_contest(raw)
        self.assertEqual(contest["id"], 12)
        self.assertEqual(contest["name"], "热身赛")
        self.assertEqual(contest["groups"], ["S+", "2026"])
        self.assertEqual(contest["creator"], "menci")
        self.assertEqual(contest["phase"], "ENDED")

    def test_extract_problems(self):
        detail = {"problemInfos": [
            {"meta": {"id": 7, "title": "B"}, "displayOrder": 2,
             "tags": [{"name": "dp"}], "scoreWeight": 100,
             "attendeeStatistics": {"numAcceptedUsers": 5, "numSubmittedUsers": 9},
             "bestSubmission": {"status": "Accepted"}},
            {"meta": {"id": 8, "title": "A"}, "displayOrder": 1, "tags": ["贪心"]},
        ]}
        problems = core.extract_problems(detail)
        self.assertEqual([p["displayOrder"] for p in problems], [1, 2])
        first = problems[0]
        self.assertEqual(first["problemId"], 8)
        self.assertEqual(first["tags"], ["贪心"])
        second = problems[1]
        self.assertTrue(second["submitted"])
        self.assertEqual(second["bestStatus"], "Accepted")
        self.assertEqual(second["numAcceptedUsers"], 5)
        self.assertEqual(second["scoreWeight"], 100)

    def test_extract_problems_fallback_map(self):
        problems = core.extract_problems({"problems": {"1": 100, "2": 101}})
        self.assertEqual([p["problemId"] for p in problems], [100, 101])
        self.assertEqual(problems[0]["displayOrder"], 1)

    def test_extract_announcements(self):
        out = core.extract_announcements({"announcements": [
            {"title": "T", "content": "C", "time": "2026-01-01T00:00:00Z"}, "裸文本"]})
        self.assertEqual(out[0]["title"], "T")
        self.assertEqual(out[1]["content"], "裸文本")

    def test_extract_token_variants(self):
        self.assertEqual(core.extract_token('{"token": "abc.def"}'), "abc.def")
        self.assertEqual(core.extract_token("session-swr=eyJhbGciOi.xx"), "eyJhbGciOi.xx")
        self.assertEqual(core.extract_token("Bearer token123"), "token123")


class StoreTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()

    def test_upsert_preserves_state(self):
        contest = {"id": 1, "name": "C1", "mode": "ACM"}
        problems = [{"problemId": 10, "displayOrder": 1, "title": "P1", "tags": []}]
        self.store.upsert_contest("u", contest, problems)
        self.store.set_state("u", 1, 10, core.STATE_DONE)
        # 再次同步（服务器数据不含 state）不应丢标记
        self.store.upsert_contest("u", {"id": 1, "name": "C1 改名"}, problems)
        entry = self.store.contest_entry("u", 1)
        self.assertEqual(entry["name"], "C1 改名")
        self.assertEqual(entry["problems"][0]["state"], core.STATE_DONE)
        self.assertIn("stateUpdatedAt", entry["problems"][0])

    def test_upsert_keeps_archivedAt_on_problem(self):
        problems = [{"problemId": 10, "displayOrder": 1, "title": "P1"}]
        self.store.upsert_contest("u", {"id": 1, "name": "C"}, problems)
        entry = self.store.contest_entry("u", 1)
        entry["problems"][0]["archivedAt"] = 123
        self.store.upsert_contest("u", {"id": 1, "name": "C"}, problems)
        self.assertEqual(self.store.contest_entry("u", 1)["problems"][0]["archivedAt"], 123)

    def test_set_state_clear(self):
        self.store.upsert_contest("u", {"id": 1}, [{"problemId": 5, "displayOrder": 1}])
        self.store.set_state("u", 1, 5, core.STATE_DOING)
        self.store.set_state("u", 1, 5, None)
        self.assertNotIn("state", self.store.contest_entry("u", 1)["problems"][0])

    def test_set_state_missing_problem(self):
        self.store.upsert_contest("u", {"id": 1}, [])
        with self.assertRaises(KeyError):
            self.store.set_state("u", 1, 999, core.STATE_DONE)

    def test_save_and_load_roundtrip(self):
        self.store.upsert_contest("u", {"id": 1, "name": "C"}, [])
        self.store.save_progress()
        other = core.Store(self.tmp)
        other.load()
        self.assertEqual(other.contest_entry("u", 1)["name"], "C")

    def test_all_contests_sorted_by_start_time(self):
        self.store.upsert_contest("u", {"id": 1, "startTime": "2025-01-01T00:00:00Z"}, [])
        self.store.upsert_contest("u", {"id": 2, "startTime": "2026-01-01T00:00:00Z"}, [])
        self.assertEqual([c["id"] for c in self.store.all_contests("u")], [2, 1])

    def test_cached_username(self):
        self.assertEqual(self.store.cached_username(), "")
        self.store.set_credentials(username="gaoshenghan")
        self.assertEqual(self.store.cached_username(), "gaoshenghan")

    def test_statement_cache_ttl(self):
        self.store.save_statement(77, {"id": 77, "statement": "内容"})
        self.assertIsNotNone(self.store.load_statement(77))
        self.assertIsNone(self.store.load_statement(77, ttl=-1))

    def test_mark_archived(self):
        self.store.upsert_contest("u", {"id": 1}, [])
        self.store.mark_archived("u", 1, "/tmp/x", 3, 2)
        entry = self.store.contest_entry("u", 1)
        self.assertEqual(entry["archivedProblems"], 3)
        self.assertEqual(entry["archivedStatements"], 2)
        self.assertIn("archivedAt", entry)

    def test_archive_dir_default_and_override(self):
        self.assertTrue(self.store.archive_dir.endswith("archive"))
        self.store.config["archiveDir"] = "/custom/path"
        self.assertEqual(self.store.archive_dir, "/custom/path")


class LookupTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.store.upsert_contest("u", {"id": 12, "name": "寒假集训", "startTime": "2026-01-01T00:00:00Z"},
                                  [{"problemId": 345, "displayOrder": 1, "title": "A题"}])

    def test_resolve_by_id(self):
        contest, matches = core.resolve_contest(self.store, "u", "12")
        self.assertEqual(contest["id"], 12)
        self.assertEqual(matches, [])

    def test_resolve_by_keyword(self):
        contest, _ = core.resolve_contest(self.store, "u", "寒假")
        self.assertEqual(contest["id"], 12)

    def test_resolve_ambiguous(self):
        self.store.upsert_contest("u", {"id": 13, "name": "寒假集训 2"}, [])
        contest, matches = core.resolve_contest(self.store, "u", "寒假")
        self.assertIsNone(contest)
        self.assertEqual(len(matches), 2)

    def test_resolve_problem_by_order_and_id(self):
        entry = self.store.contest_entry("u", 12)
        self.assertEqual(core.resolve_problem(entry, "1")["problemId"], 345)
        self.assertEqual(core.resolve_problem(entry, "345")["displayOrder"], 1)
        self.assertIsNone(core.resolve_problem(entry, "9"))

    def test_contest_link(self):
        self.assertEqual(core.contest_link("https://x/", 5), "https://x/c/5")
        self.assertEqual(core.contest_link("https://x", 5, 2), "https://x/c/5/p/2")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.contest = {"id": 2189, "name": "S+树上启发式合并", "mode": "ACM",
                        "startTime": "2025-03-11T09:00:00Z", "endTime": "2025-03-12T09:00:00Z",
                        "phase": "ENDED", "creator": "menci", "groups": ["S+"]}
        self.problems = [
            {"problemId": 4059, "displayOrder": 1, "title": "Boring counting",
             "tags": ["树上启发式合并", "树上"], "state": core.STATE_DONE, "submitted": True,
             "scoreWeight": 100, "numAcceptedUsers": 61, "numSubmittedUsers": 71},
            {"problemId": 4060, "displayOrder": 2, "title": "Another", "tags": []},
        ]

    def test_render_contest_markdown(self):
        text = core.render_contest_markdown({
            "contest": self.contest, "problems": self.problems,
            "detail": {"description": "看 [视频](/videoshow/x)", "announcements": []},
            "files": {"1": "problems/01-boring-counting.md", "2": "problems/02-another.md"},
            "has_statement": {"1": True, "2": False},
            "archived_at": "2026-11-14T21:00:00+08:00",
            "base_url": "https://nflsoi.cc:20035",
        })
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('title: "S+树上启发式合并"', text)
        self.assertIn("contest_id: 2189", text)
        self.assertIn("# S+树上启发式合并", text)
        self.assertIn("## 题目列表", text)
        self.assertIn("problems/01-boring-counting.md", text)
        self.assertIn("https://nflsoi.cc:20035/videoshow/x", text)
        self.assertIn("已完成", text)
        self.assertIn("已保存", text)       # 第 1 题有题面
        self.assertIn("缺失", text)         # 第 2 题文件在但没抓到题面
        self.assertIn("返回总目录", text)

    def test_render_contest_problem_without_file(self):
        text = core.render_contest_markdown({
            "contest": self.contest, "problems": self.problems, "detail": {},
            "files": {"1": "problems/01-boring-counting.md"},
            "has_statement": {"1": True}, "base_url": "https://x",
        })
        self.assertIn("未存档题面", text)
        self.assertIn("未归档", text)

    def test_render_contest_without_problems(self):
        text = core.render_contest_markdown({
            "contest": self.contest, "problems": [],
            "detail": {"permissions": {"canViewProblems": False}}, "files": {},
            "base_url": "https://x",
        })
        self.assertIn("canViewProblems = false", text)

    def test_render_problem_markdown(self):
        text = core.render_problem_markdown({
            "contest": self.contest, "problem": self.problems[0],
            "statement": "## 题目描述\r\n很短的题意。",
            "archived_at": "2026-11-14T21:00:00+08:00", "base_url": "https://x",
        })
        self.assertIn('problem_id: 4059', text)
        self.assertIn("# 1. Boring counting", text)
        self.assertIn("[S+树上启发式合并](../index.md)", text)
        self.assertIn("`树上启发式合并`", text)
        self.assertIn("## 题目描述", text)
        self.assertIn("很短的题意。", text)
        self.assertIn("https://x/c/2189/p/1", text)

    def test_render_problem_missing_statement(self):
        text = core.render_problem_markdown({
            "contest": self.contest, "problem": self.problems[1],
            "statement": "", "error": "权限不足", "base_url": "https://x",
        })
        self.assertIn("未能获取题目正文：权限不足", text)

    def test_render_root_index(self):
        text = core.render_root_index([
            {"contest": self.contest, "dir": "2189-S+树上启发式合并",
             "problems": 7, "statements": 7, "done": 3},
        ], "gaoshenghan")
        self.assertIn("2189-S+树上启发式合并/index.md", text)
        self.assertIn("gaoshenghan", text)
        self.assertIn("| 7 | 3 | 7 |", text)

    def test_render_root_index_empty(self):
        self.assertIn("还没有任何存档", core.render_root_index([], "u"))


class ArchiveFlowTests(TmpCase):
    class FakeClient:
        def __init__(self, detail, statements):
            self.detail = detail
            self.statements = statements

        def contest_detail(self, contest_id, problems=True, description=False, announcements=False):
            return self.detail

        def problem_detail(self, problem_id, statement=True):
            return self.statements[problem_id]

    def setUp(self):
        super().setUp()
        self.store = core.Store(self.tmp)
        self.store.load()
        self.console, self.stream = make_console()
        self.detail = {
            "meta": {"id": 7, "name": "Test Contest", "mode": "ACM",
                     "startTime": "2026-01-01T00:00:00Z", "endTime": "2026-01-02T00:00:00Z"},
            "description": "说明文本",
            "announcements": [{"title": "公告一", "content": "内容", "time": None}],
            "problemInfos": [
                {"meta": {"id": 101, "title": "Alpha"}, "displayOrder": 1,
                 "tags": [{"name": "dp"}]},
                {"meta": {"id": 102, "title": "Beta"}, "displayOrder": 2, "tags": []},
            ],
            "permissions": {"canViewProblems": True},
        }
        self.client = self.FakeClient(self.detail, {
            101: {"meta": {"id": 101}, "statement": "Alpha 题面"},
            102: {"meta": {"id": 102}, "statement": "Beta 题面"},
        })
        self.root = os.path.join(self.tmp, "archive")

    def test_archive_one_contest_layout(self):
        result = core.archive_one_contest(self.store, "u", self.console, self.client,
                                          {"id": 7, "name": "Test Contest"}, self.root)
        contest_dir = os.path.join(self.root, "7-Test-Contest")
        self.assertEqual(result["problems"], 2)
        self.assertEqual(result["statements"], 2)
        self.assertTrue(os.path.isfile(os.path.join(contest_dir, "index.md")))
        self.assertTrue(os.path.isfile(os.path.join(contest_dir, "problems", "01-Alpha.md")))
        self.assertTrue(os.path.isfile(os.path.join(contest_dir, "problems", "02-Beta.md")))
        with open(os.path.join(contest_dir, "problems", "01-Alpha.md"), encoding="utf-8") as handle:
            self.assertIn("Alpha 题面", handle.read())
        entry = self.store.contest_entry("u", 7)
        self.assertIn("archivedAt", entry)
        self.assertEqual(entry["archivedStatements"], 2)

    def test_archive_idempotent(self):
        core.archive_one_contest(self.store, "u", self.console, self.client,
                                 {"id": 7, "name": "Test Contest"}, self.root)
        second = core.archive_one_contest(self.store, "u", self.console, self.client,
                                          {"id": 7, "name": "Test Contest"}, self.root)
        self.assertEqual(second["files_written"], 0)

    def test_archive_statements_cached(self):
        core.archive_one_contest(self.store, "u", self.console, self.client,
                                 {"id": 7, "name": "Test Contest"}, self.root)
        cached = self.store.load_statement(101)
        self.assertEqual(cached["statement"], "Alpha 题面")

    def test_archive_without_statements(self):
        result = core.archive_one_contest(self.store, "u", self.console, self.client,
                                          {"id": 7, "name": "Test Contest"}, self.root,
                                          with_statements=False)
        self.assertEqual(result["statements"], 0)
        path = os.path.join(self.root, "7-Test-Contest", "problems", "01-Alpha.md")
        with open(path, encoding="utf-8") as handle:
            self.assertIn("未能获取题目正文", handle.read())

    def test_archive_statement_failure_recorded(self):
        client = self.FakeClient(self.detail, {101: {"statement": ""}})
        result = core.archive_one_contest(self.store, "u", self.console, client,
                                          {"id": 7, "name": "Test Contest"}, self.root)
        self.assertIn("101", result["failed"])
        self.assertEqual(result["statements"], 0)

    def test_archive_contests_writes_root_index(self):
        out = core.archive_contests(self.store, "u", self.console, self.client,
                                    [{"id": 7, "name": "Test Contest"}], self.root)
        self.assertEqual(len(out["results"]), 1)
        index = os.path.join(self.root, "index.md")
        self.assertTrue(os.path.isfile(index))
        with open(index, encoding="utf-8") as handle:
            self.assertIn("7-Test-Contest/index.md", handle.read())


class ClientTests(unittest.TestCase):
    def test_offline_raises(self):
        client = core.Client(offline=True)
        with self.assertRaises(core.ApiError):
            client.session_info()

    def test_error_message_extracted(self):
        client = core.Client()
        client.http.request = lambda *a, **k: (403, {}, b'{"message": "not activated"}')
        with self.assertRaises(core.ApiError) as ctx:
            client.session_info()
        self.assertIn("未激活", ctx.exception.message)

    def test_validation_error_list_joined(self):
        client = core.Client()
        client.http.request = lambda *a, **k: (
            400, {}, json.dumps({"message": ["a 错误", "b 错误"]}).encode())
        with self.assertRaises(core.ApiError) as ctx:
            client.session_info()
        self.assertIn("a 错误", ctx.exception.message)
        self.assertIn("b 错误", ctx.exception.message)

    def test_non_json_response(self):
        client = core.Client()
        client.http.request = lambda *a, **k: (200, {}, b"<html>")
        with self.assertRaises(core.ApiError):
            client.session_info()

    def test_empty_body_returns_dict(self):
        client = core.Client()
        client.http.request = lambda *a, **k: (200, {}, b"")
        self.assertEqual(client.session_info(), {})

    def test_headers_include_auth(self):
        client = core.Client(token="abc", cookie="sid=1")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Bearer abc")
        self.assertEqual(headers["Cookie"], "sid=1")

    def test_query_bool_encoding(self):
        captured = {}

        def fake(method, path, query=None, body=None, headers=None):
            captured["query"] = query
            return 200, {}, b"{}"

        client = core.Client()
        client.http.request = fake
        client.contest_detail(12)
        self.assertIs(captured["query"]["problemInfos"], True)


class StatsTests(unittest.TestCase):
    def test_collect_stats(self):
        contests = [
            {"id": 1, "problems": [
                {"state": core.STATE_DONE, "stateUpdatedAt": 100},
                {"state": core.STATE_DOING},
                {"submitted": True},
            ]},
            {"id": 2, "problems": []},
        ]
        stats = core.collect_stats(contests)
        self.assertEqual(stats["overall"]["problems"], 3)
        self.assertEqual(stats["overall"][core.STATE_DONE], 1)
        self.assertEqual(stats["overall"][core.STATE_DOING], 1)
        self.assertEqual(stats["overall"][core.STATE_TODO], 1)
        self.assertEqual(stats["overall"]["submitted"], 1)
        self.assertEqual(stats["overall"]["synced"], 1)
        self.assertEqual(stats["recent"][0][0], 100)

    def test_activity_buckets(self):
        import time as _time
        buckets = core._activity_buckets(
            [{"problems": [{"stateUpdatedAt": int(_time.time())}]}], 7)
        self.assertEqual(len(buckets), 7)
        self.assertEqual(sum(b[1] for b in buckets), 1)

    def test_sparkline(self):
        self.assertEqual(core._sparkline([0, 0, 0]), "···")
        self.assertEqual(len(core._sparkline([1, 2, 3])), 3)


class ExporterTests(unittest.TestCase):
    def test_iter_rows(self):
        rows = list(core.iter_rows([
            {"id": 1, "name": "C", "problems": [
                {"problemId": 9, "displayOrder": 1, "title": "T",
                 "tags": ["a", "b"], "state": core.STATE_DONE}]}]))
        self.assertEqual(rows[0]["state_label"], "已完成")
        self.assertEqual(rows[0]["tags"], "a b")


class AtomicWriteTests(TmpCase):
    def test_writes_text(self):
        path = os.path.join(self.tmp, "sub", "a.txt")
        core.atomic_write_text(path, "hello\n")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello\n")

    def test_overwrites_readonly_file(self):
        path = os.path.join(self.tmp, "a.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("old")
        os.chmod(path, stat.S_IREAD)          # 设为只读
        core.atomic_write_text(path, "new")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "new")
        os.chmod(path, stat.S_IWRITE)

    def test_permission_error_becomes_store_error(self):
        path = os.path.join(self.tmp, "a.txt")
        original = os.replace

        def boom(src, dst):
            raise PermissionError(errno.EACCES, "拒绝访问", dst)

        os.replace = boom
        try:
            with self.assertRaises(core.StoreError) as ctx:
                core.atomic_write_text(path, "x")
        finally:
            os.replace = original
        self.assertIn(path, ctx.exception.message)
        self.assertIn("--data-dir", ctx.exception.hint)
        # 失败后不应留下临时文件
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_directory_creation_failure_is_store_error(self):
        file_path = os.path.join(self.tmp, "afile")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        with self.assertRaises(core.StoreError):
            core.atomic_write_text(os.path.join(file_path, "sub", "a.txt"), "x")

    def test_store_save_config_reports_store_error(self):
        store = core.Store(os.path.join(self.tmp, "data"))
        original = os.replace

        def boom(src, dst):
            raise PermissionError(errno.EACCES, "拒绝访问", dst)

        os.replace = boom
        try:
            with self.assertRaises(core.StoreError):
                store.save_config()
        finally:
            os.replace = original

    def test_store_writable_probe(self):
        store = core.Store(os.path.join(self.tmp, "data"))
        self.assertTrue(store.writable())
        # 探针不应留下文件
        self.assertEqual(os.listdir(self.tmp), [])

    def test_store_writable_when_parent_is_file(self):
        file_path = os.path.join(self.tmp, "afile")
        with open(file_path, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertFalse(core.Store(os.path.join(file_path, "data")).writable())


class DataDirTests(unittest.TestCase):
    def test_cli_value_wins(self):
        self.assertEqual(core.resolve_data_dir(os.path.join("x", "y")),
                         os.path.abspath(os.path.join("x", "y")))

    def test_env_var(self):
        old = os.environ.get(core.DATA_DIR_ENV)
        os.environ[core.DATA_DIR_ENV] = os.path.join(os.getcwd(), "envdata")
        try:
            self.assertEqual(core.resolve_data_dir(), os.path.abspath(os.path.join(os.getcwd(), "envdata")))
        finally:
            if old is None:
                os.environ.pop(core.DATA_DIR_ENV, None)
            else:
                os.environ[core.DATA_DIR_ENV] = old

    def test_default_when_unset(self):
        old = os.environ.pop(core.DATA_DIR_ENV, None)
        try:
            self.assertEqual(core.resolve_data_dir(), os.path.abspath(core.DEFAULT_DATA_DIR))
        finally:
            if old is not None:
                os.environ[core.DATA_DIR_ENV] = old


class WriteGuardTests(TmpCase):
    """不能写盘时，命令应给出友好提示而不是抛 traceback。"""

    API = "http://127.0.0.1:9"      # 不可达地址：若守卫失效也不会打到真实站点

    def _blocked(self) -> str:
        blocker = os.path.join(self.tmp, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        return os.path.join(blocker, "data")

    def test_login_refuses_unwritable_data_dir(self):
        code = core.main(["--api", self.API, "--data-dir", self._blocked(),
                          "login", "-u", "u", "-p", "p"])
        self.assertEqual(code, 1)

    def test_sync_refuses_unwritable_data_dir(self):
        code = core.main(["--api", self.API, "--data-dir", self._blocked(), "sync"])
        self.assertEqual(code, 1)

    def test_readonly_command_does_not_crash(self):
        # stats 不需要写盘：数据目录不可写时也应正常给出「没有账号」而不是 traceback
        code = core.main(["--api", self.API, "--data-dir", self._blocked(), "stats"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
