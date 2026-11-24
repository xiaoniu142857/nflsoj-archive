"""批量原题查询的离线测试（origin_pick/statements/query/report + 跳过逻辑）。"""
import io
import json
import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from nflsoj_archive import nflsoj as core                     # noqa: E402

TMP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")

STATEMENT = ("给定 L, R，求区间内数位中至少有一位非零的整数个数。")


def make_tmp_dir(prefix="b") -> str:
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
        self.store = core.Store(os.path.join(self.tmp, "data"))
        self.store.load()
        self.stream = io.StringIO()
        self.console = core.Console(color=False, ascii_only=False, stream=self.stream,
                                    width=100)

    def tearDown(self):
        rmtree_force(self.tmp)

    def seed(self, contests):
        for cid, problems in contests.items():
            self.store.upsert_contest("u", {"id": cid, "name": "比赛 %s" % cid}, problems)


class FakeHelper:
    tool_name = "nflsoj"

    def __init__(self, statements=None, errors=None):
        self.statements = statements or {}
        self.errors = errors or {}
        self.calls = []
        self.closed = False

    def statement(self, problem, refresh):
        pid = str(problem.get("problemId"))
        self.calls.append(pid)
        if pid in self.errors:
            return "", self.errors[pid]
        return self.statements.get(pid) or STATEMENT, ""

    def close(self):
        self.closed = True


class FakeCpret:
    def __init__(self, score=0.9):
        self.score = score
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        return {"candidates": [{"title": "原题", "source": "Luogu",
                                "url": "https://www.luogu.com.cn/problem/P1000",
                                "score": self.score, "duplicate": False, "group": 0}],
                "total": 1, "elapsed": 0.1}

    def close(self):
        pass


class PickProblemsTests(unittest.TestCase):
    def test_all_problems(self):
        entry = {"problems": [{"displayOrder": 1, "problemId": 10},
                              {"displayOrder": 2, "problemId": 11}]}
        self.assertEqual(len(core.origin_pick_problems(entry)), 2)

    def test_by_display_order(self):
        entry = {"id": 7, "problems": [{"displayOrder": 1, "problemId": 10},
                                       {"displayOrder": 2, "problemId": 11}]}
        picked = core.origin_pick_problems(entry, ["2"])
        self.assertEqual(picked[0]["problemId"], 11)

    def test_by_problem_id(self):
        entry = {"id": 7, "problems": [{"displayOrder": 1, "problemId": 10}]}
        self.assertEqual(core.origin_pick_problems(entry, ["10"])[0]["displayOrder"], 1)

    def test_missing_specs(self):
        stream = io.StringIO()
        console = core.Console(color=False, stream=stream, width=100)
        entry = {"id": 7, "problems": [{"displayOrder": 1, "problemId": 10}]}
        self.assertEqual(core.origin_pick_problems(entry, ["99"], console=console), [])
        self.assertIn("找不到题号", stream.getvalue())

    def test_limit(self):
        entry = {"problems": [{"displayOrder": i, "problemId": i} for i in range(1, 6)]}
        self.assertEqual(len(core.origin_pick_problems(entry, None, limit=2)), 2)


class FetchStatementsTests(TmpCase):
    def test_uses_cache(self):
        self.seed({1: [{"displayOrder": 1, "problemId": 10}]})
        self.store.save_statement(10, {"statement": "缓存的题面"})
        helper = FakeHelper()
        statements, errors, cached = core.origin_fetch_statements(
            self.store, self.console, helper, [{"displayOrder": 1, "problemId": 10}])
        self.assertEqual(statements["10"], "缓存的题面")
        self.assertEqual(cached, 1)
        self.assertEqual(helper.calls, [])

    def test_fetches_when_missing(self):
        helper = FakeHelper({"10": STATEMENT})
        statements, _errors, cached = core.origin_fetch_statements(
            self.store, self.console, helper, [{"displayOrder": 1, "problemId": 10}])
        self.assertEqual(cached, 0)
        self.assertEqual(helper.calls, ["10"])
        self.assertIn("非零", statements["10"])

    def test_refresh_bypasses_cache(self):
        self.store.save_statement(10, {"statement": "旧题面"})
        helper = FakeHelper({"10": "新题面"})
        statements, _errors, cached = core.origin_fetch_statements(
            self.store, self.console, helper, [{"displayOrder": 1, "problemId": 10}],
            refresh=True)
        self.assertEqual(cached, 0)
        self.assertEqual(statements["10"], "新题面")

    def test_errors_collected(self):
        helper = FakeHelper(errors={"10": "没有权限"})
        statements, errors, _cached = core.origin_fetch_statements(
            self.store, self.console, helper, [{"displayOrder": 1, "problemId": 10}])
        self.assertEqual(statements, {})
        self.assertEqual(errors["10"], "没有权限")

    def test_missing_problem_id_skipped(self):
        statements, errors, _cached = core.origin_fetch_statements(
            self.store, self.console, FakeHelper(), [{"displayOrder": 1}])
        self.assertEqual(statements, {})
        self.assertEqual(errors, {})


class QueryProblemsTests(TmpCase):
    def test_records_and_cache(self):
        self.seed({1: [{"displayOrder": 1, "problemId": 10}]})
        cpret = FakeCpret(0.9)
        records = core.origin_query_problems(
            self.store, self.console, "u", 1,
            [{"displayOrder": 1, "problemId": 10, "title": "非零数位"}],
            {"10": STATEMENT}, {}, 0.7, cpret)
        self.assertTrue(records["10"]["matched"])
        self.assertEqual(records["10"]["vjudgeId"], "洛谷-P1000")
        cached = self.store.problem_origin("u", 1, 10)
        self.assertEqual(cached["vjudgeId"], "洛谷-P1000")
        self.assertEqual(len(cpret.calls), 1)

    def test_no_statement_not_cached(self):
        self.seed({1: [{"displayOrder": 1, "problemId": 10}]})
        records = core.origin_query_problems(
            self.store, self.console, "u", 1,
            [{"displayOrder": 1, "problemId": 10, "title": "t"}],
            {}, {"10": "Not Found"}, 0.7, FakeCpret())
        self.assertIsNone(records["10"]["score"])
        self.assertFalse(records["10"]["cached"])
        self.assertIsNone(self.store.problem_origin("u", 1, 10))

    def test_threshold_miss_not_matched(self):
        self.seed({1: [{"displayOrder": 1, "problemId": 10}]})
        records = core.origin_query_problems(
            self.store, self.console, "u", 1,
            [{"displayOrder": 1, "problemId": 10, "title": "t"}],
            {"10": STATEMENT}, {}, 0.95, FakeCpret(0.9))
        self.assertFalse(records["10"]["matched"])
        self.assertTrue(records["10"]["matched"] is False)


class ReportTableTests(TmpCase):
    def test_table_rows(self):
        problems = [{"displayOrder": 1, "problemId": 10, "title": "非零数位"},
                    {"displayOrder": 2, "problemId": 11, "title": "A类B类数"}]
        records = {"10": {"matched": True, "score": 0.8847, "vjudgeId": "洛谷-P1000",
                          "source": "Luogu"},
                   "11": {"matched": False, "score": 0.42}}
        matched, cached = core.origin_report_table(self.console, problems, records)
        text = self.stream.getvalue()
        self.assertEqual(matched, 1)
        self.assertEqual(cached, 0)
        self.assertIn("非零数位", text)
        self.assertIn("洛谷-P1000", text)
        self.assertIn("0.8847", text)
        self.assertIn("未命中", text)


class BatchSkipLogicTests(TmpCase):
    """--all 的“跳过已缓存”只认真正查过的结果（score 为数字）。"""

    def test_skip_only_when_score_cached(self):
        self.seed({1: [
            {"displayOrder": 1, "problemId": 10, "origin": {"score": 0.9}},
            {"displayOrder": 2, "problemId": 11, "origin": {"score": None}},
            {"displayOrder": 3, "problemId": 12},
        ]})
        pending = []
        for problem in self.store.contest_entry("u", 1)["problems"]:
            cached = problem.get("origin")
            if isinstance(cached, dict) and isinstance(cached.get("score"), (int, float)):
                continue
            pending.append(problem)
        self.assertEqual([p["problemId"] for p in pending], [11, 12])


if __name__ == "__main__":
    unittest.main()
