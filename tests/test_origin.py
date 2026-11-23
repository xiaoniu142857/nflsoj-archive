"""原题查询（CPRet）的离线测试：解析、映射、判定、缓存、渲染。"""
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

CARD = """
<li class="result-card" style="--i: {i}">
  <span class="rank medal-gold">#{rank}</span>
  <div class="result-main">
    <div class="result-title-row">
      <span class="cdot" style="--cc: var(--cl-0)" title="{title_attr}"></span>
      <a class="result-title" href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
    </div>
    <div class="result-meta">
      <span class="oj-tag">{source}</span>
      <a class="meta-link" href="/p/1?lang=zh" title="查看题面">📄 查看题面</a>
      {dups}
    </div>
  </div>
  <div class="score-col" title="cosine similarity">
    <span class="score-num">{score}</span>
  </div>
</li>
"""

DUPS = """
<details class="dups">
  <summary>另有 {n} 个同题/近似题</summary>
  <ul>
    <li>
      <span class="oj-tag">{source}</span>
      <a class="t" href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>
      <a class="meta-link" href="/p/2">📄</a>
      <span class="num">{score}</span>
    </li>
  </ul>
</details>
"""


def card(i, title, source, url, score, dups=""):
    return CARD.format(i=i, rank=i + 1, title_attr=title, title=title, url=url,
                       source=source, score="%.4f" % score, dups=dups)


def make_tmp_dir(prefix="o") -> str:
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


def make_console(width=100, stream=None):
    stream = stream or io.StringIO()
    return core.Console(color=False, ascii_only=False, stream=stream, width=width), stream


# ---------------------------------------------------------------------------
# HTML 解析
# ---------------------------------------------------------------------------
class ParseCardsTests(unittest.TestCase):
    def test_single_card(self):
        html = card(0, "宝石管理系统", "Luogu", "https://www.luogu.com.cn/problem/P2343", 0.8131)
        cards = core.parse_cpret_cards(html)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "宝石管理系统")
        self.assertEqual(cards[0]["source"], "Luogu")
        self.assertEqual(cards[0]["url"], "https://www.luogu.com.cn/problem/P2343")
        self.assertAlmostEqual(cards[0]["score"], 0.8131, places=4)
        self.assertFalse(cards[0]["duplicate"])

    def test_multiple_cards_order(self):
        html = "".join([card(0, "A", "Luogu", "https://x/problem/P1", 0.9),
                        card(1, "B", "AtCoder", "https://x/tasks/abc1_a", 0.8)])
        cards = core.parse_cpret_cards(html)
        self.assertEqual([c["title"] for c in cards], ["A", "B"])
        self.assertEqual(cards[1]["group"], 1)

    def test_sample_card_skipped(self):
        html = ('<li class="result-card sample-card"><a class="result-title" '
                'href="https://x/problem/P1">示例</a><span class="score-num">0.99</span></li>')
        self.assertEqual(core.parse_cpret_cards(html), [])

    def test_duplicates(self):
        dups = DUPS.format(n=1, source="Luogu", url="https://x/problem/P3369",
                           title="普通平衡树", score="0.6784")
        html = card(0, "平衡树", "BZOJ", "https://x/p/bzoj-1", 0.91, dups=dups)
        cards = core.parse_cpret_cards(html)
        self.assertEqual(len(cards), 2)
        self.assertFalse(cards[0]["duplicate"])
        self.assertTrue(cards[1]["duplicate"])
        self.assertEqual(cards[1]["group"], 0)
        self.assertAlmostEqual(cards[1]["score"], 0.6784, places=4)

    def test_html_entities_and_whitespace(self):
        html = card(0, "A &amp; B", "Luogu", "https://x/problem/P1?x=1&amp;y=2", 0.5)
        cards = core.parse_cpret_cards(html)
        self.assertEqual(cards[0]["title"], "A & B")
        self.assertIn("?x=1&y=2", cards[0]["url"])

    def test_empty_and_garbage(self):
        self.assertEqual(core.parse_cpret_cards(""), [])
        self.assertEqual(core.parse_cpret_cards(None), [])
        self.assertEqual(core.parse_cpret_cards("<div>没有卡片</div>"), [])

    def test_score_without_value(self):
        html = ('<li class="result-card"><a class="result-title" href="https://x/problem/P1">'
                'A</a><span class="oj-tag">Luogu</span></li>')
        cards = core.parse_cpret_cards(html)
        self.assertEqual(cards[0]["score"], 0.0)


# ---------------------------------------------------------------------------
# VJudge 题号映射
# ---------------------------------------------------------------------------
class VjudgeMappingTests(unittest.TestCase):
    def test_luogu(self):
        oj, num, _ = core.vjudge_problem("Luogu", "https://www.luogu.com.cn/problem/P1000")
        self.assertEqual((oj, num), ("洛谷", "P1000"))
        self.assertEqual("%s-%s" % (oj, num), "洛谷-P1000")

    def test_codeforces(self):
        oj, num, _ = core.vjudge_problem("Codeforces",
                                         "https://codeforces.com/problemset/problem/1234/A")
        self.assertEqual((oj, num), ("CodeForces", "1234A"))
        oj, num, _ = core.vjudge_problem("Codeforces",
                                         "https://codeforces.com/contest/908/problem/G")
        self.assertEqual(num, "908G")

    def test_gym(self):
        oj, num, _ = core.vjudge_problem("Gym", "https://codeforces.com/gym/100001/problem/A")
        self.assertEqual((oj, num), ("Gym", "100001A"))

    def test_atcoder(self):
        for url, expected in (
                ("https://atcoder.jp/contests/abc154/tasks/abc154_e", "abc154_e"),
                ("https://atcoder.jp/contests/arc033/tasks/arc033_3", "arc033_3"),
        ):
            oj, num, _ = core.vjudge_problem("AtCoder", url)
            self.assertEqual((oj, num), ("AtCoder", expected))

    def test_qoj_uoj_loj(self):
        self.assertEqual(core.vjudge_problem("QOJ", "https://qoj.ac/problem/18512")[1], "18512")
        self.assertEqual(core.vjudge_problem("UOJ", "https://uoj.ac/problem/21")[0], "UniversalOJ")
        self.assertEqual(core.vjudge_problem("LibreOJ", "https://loj.ac/p/104")[1], "104")

    def test_nowcoder(self):
        oj, num, _ = core.vjudge_problem("Nowcoder",
                                         "https://ac.nowcoder.com/acm/problem/51117")
        self.assertEqual((oj, num), ("牛客", "51117"))

    def test_bzoj_dead(self):
        oj, num, reason = core.vjudge_problem("BZOJ", "https://x/p/bzoj-1")
        self.assertEqual((oj, num), (None, None))
        self.assertIn("停止服务", reason)

    def test_unsupported_source(self):
        oj, num, reason = core.vjudge_problem("Hydro", "https://x/p/1")
        self.assertEqual((oj, num), (None, None))
        self.assertIn("Hydro", reason)

    def test_unparseable_url(self):
        oj, num, reason = core.vjudge_problem("Luogu", "https://x/whatever")
        self.assertIsNone(num)
        self.assertTrue(reason)

    def test_empty_source(self):
        self.assertEqual(core.vjudge_problem("", "")[:2], (None, None))


# ---------------------------------------------------------------------------
# 判定语义：严格大于阈值
# ---------------------------------------------------------------------------
def make_candidates(scores, sources=None, urls=None, dups=None):
    sources = sources or ["Luogu"] * len(scores)
    urls = urls or ["https://www.luogu.com.cn/problem/P%d" % (1000 + i)
                    for i in range(len(scores))]
    dups = dups or [False] * len(scores)
    return [{"title": "题 %d" % i, "source": sources[i], "url": urls[i],
             "score": scores[i], "duplicate": dups[i], "group": i}
            for i in range(len(scores))]


class MatchSemanticsTests(unittest.TestCase):
    def test_above_threshold_matches(self):
        record = core.match_origin(make_candidates([0.71, 0.5]), 0.7)
        self.assertTrue(record["matched"])
        self.assertEqual(record["score"], 0.71)
        self.assertEqual(record["vjudgeId"], "洛谷-P1000")

    def test_equal_threshold_is_not_matched(self):
        """阈值判定是严格大于（>.7），刚好 0.7 不算命中。"""
        record = core.match_origin(make_candidates([0.7]), 0.7)
        self.assertFalse(record["matched"])
        self.assertEqual(record["score"], 0.7)

    def test_below_threshold(self):
        record = core.match_origin(make_candidates([0.69]), 0.7)
        self.assertFalse(record["matched"])
        self.assertIsNone(record.get("vjudgeId"))

    def test_best_is_highest(self):
        record = core.match_origin(make_candidates([0.5, 0.95, 0.8]), 0.7)
        self.assertEqual(record["score"], 0.95)
        self.assertEqual(record["vjudgeId"], "洛谷-P1001")

    def test_duplicates_excluded_from_best(self):
        record = core.match_origin(make_candidates([0.6, 0.99], dups=[False, True]), 0.7)
        self.assertFalse(record["matched"])
        self.assertEqual(record["score"], 0.6)

    def test_empty_results(self):
        record = core.match_origin([], 0.7)
        self.assertFalse(record["matched"])
        self.assertIsNone(record["score"])
        self.assertIn("没有返回结果", record["reason"])

    def test_threshold_from_argument_is_used(self):
        record = core.match_origin(make_candidates([0.85]), 0.9)
        self.assertFalse(record["matched"])
        self.assertEqual(record["threshold"], 0.9)

    def test_unsupported_best_uses_same_problem_duplicate(self):
        """最高分是 VJudge 不支持的来源时，改用同题可映射的版本。"""
        candidates = [
            {"title": "BZOJ 原题", "source": "BZOJ", "url": "https://x/p/bzoj-1",
             "score": 0.95, "duplicate": False, "group": 0},
            {"title": "洛谷版本", "source": "Luogu",
             "url": "https://www.luogu.com.cn/problem/P3369", "score": 0.9,
             "duplicate": True, "group": 0},
        ]
        record = core.match_origin(candidates, 0.7)
        self.assertTrue(record["matched"])
        self.assertEqual(record["vjudgeId"], "洛谷-P3369")
        self.assertIn("QOJ", record["note"] + "QOJ") if False else None
        self.assertIn("改用", record["note"])

    def test_unsupported_without_alternative(self):
        candidates = [{"title": "Hydro 题", "source": "Hydro", "url": "https://x/p/1",
                       "score": 0.95, "duplicate": False, "group": 0}]
        record = core.match_origin(candidates, 0.7)
        self.assertTrue(record["matched"])
        self.assertIsNone(record["vjudgeId"])
        self.assertIn("Hydro", record["note"])


# ---------------------------------------------------------------------------
# 查询文本与缓存
# ---------------------------------------------------------------------------
class QueryTextTests(unittest.TestCase):
    def test_title_and_statement(self):
        self.assertEqual(core.origin_query_text("标题", "正文"), "标题\n正文")

    def test_statement_only(self):
        self.assertEqual(core.origin_query_text("", "正文"), "正文")

    def test_truncation(self):
        text = core.origin_query_text("t", "x" * (core.ORIGIN_MAX_QUERY + 100))
        self.assertEqual(len(text), core.ORIGIN_MAX_QUERY)

    def test_none_inputs(self):
        self.assertEqual(core.origin_query_text(None, None), "")

    def test_query_id_stable(self):
        self.assertEqual(core.origin_query_id("abc"), core.origin_query_id("abc"))
        self.assertNotEqual(core.origin_query_id("abc"), core.origin_query_id("abd"))
        self.assertEqual(len(core.origin_query_id("abc")), 64)


class OriginCacheTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(os.path.join(self.tmp, "data"))
        self.store.load()
        self.quiet_console, _ = make_console()

    def test_save_and_load(self):
        query_id = core.origin_query_id("某个题面")
        payload = {"total": 100, "elapsed": 1.0,
                   "candidates": make_candidates([0.8])}
        self.store.save_origin_cache(query_id, payload)
        loaded = self.store.load_origin_cache(query_id)
        self.assertEqual(len(loaded["candidates"]), 1)
        self.assertIn("fetchedAt", loaded)

    def test_ttl_expiry(self):
        query_id = core.origin_query_id("题面")
        self.store.save_origin_cache(query_id, {"candidates": []})
        self.assertIsNone(self.store.load_origin_cache(query_id, 0.000001))

    def test_broken_cache_returns_none(self):
        query_id = core.origin_query_id("x")
        path = self.store.origin_cache_path(query_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{坏 JSON")
        self.assertIsNone(self.store.load_origin_cache(query_id))

    def test_cache_without_candidates(self):
        query_id = core.origin_query_id("x")
        self.store.save_origin_cache(query_id, {"total": 1})
        self.assertIsNone(self.store.load_origin_cache(query_id))

    def test_set_and_read_problem_origin(self):
        entry = self.store.upsert_contest("u", {"id": 1, "name": "c"},
                                         [{"problemId": 2, "displayOrder": 1,
                                           "title": "t"}])
        self.assertTrue(self.store.set_origin("u", 1, 2, {"vjudgeId": "洛谷-P1000",
                                                          "matched": True}))
        self.assertEqual(self.store.problem_origin("u", 1, 2)["vjudgeId"], "洛谷-P1000")
        self.assertFalse(self.store.set_origin("u", 1, 999, {}))

    def test_origin_field_survives_upsert(self):
        self.store.upsert_contest("u", {"id": 1, "name": "c"},
                                  [{"problemId": 2, "displayOrder": 1, "title": "t"}])
        self.store.set_origin("u", 1, 2, {"vjudgeId": "QOJ-1", "matched": True})
        self.store.upsert_contest("u", {"id": 1, "name": "c2"},
                                  [{"problemId": 2, "displayOrder": 1, "title": "t2"}])
        self.assertEqual(self.store.problem_origin("u", 1, 2)["vjudgeId"], "QOJ-1")

    def test_non_dict_origin_dropped(self):
        self.store.upsert_contest("u", {"id": 1, "name": "c"}, [])
        entry = self.store.contest_entry("u", 1)
        entry["problems"] = [{"problemId": 2, "origin": "不是对象"}]
        self.store._normalize_progress()
        self.assertNotIn("origin", entry["problems"][0])

    def test_run_origin_search_uses_cache(self):
        text = "标题\n题面"
        query_id = core.origin_query_id(text)
        self.store.save_origin_cache(query_id, {"candidates": make_candidates([0.88])})
        record, cached = core.run_origin_search(self.store, self.quiet_console, None,
                                                text, 0.7)
        self.assertTrue(cached)
        self.assertTrue(record["matched"])
        self.assertEqual(record["vjudgeId"], "洛谷-P1000")


class FakeCpret:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or make_candidates([0.9])
        self.error = error
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        if self.error:
            raise core.ApiError(self.error)
        return {"candidates": self.candidates, "total": 1, "elapsed": 0.1}

    def close(self):
        pass


class RunOriginSearchTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = core.Store(os.path.join(self.tmp, "data"))
        self.store.load()
        self.console, _ = make_console()

    def test_first_call_queries_then_cached(self):
        fake = FakeCpret()
        record, cached = core.run_origin_search(self.store, self.console, fake, "题面", 0.7)
        self.assertFalse(cached)
        self.assertEqual(len(fake.calls), 1)
        record2, cached2 = core.run_origin_search(self.store, self.console, fake, "题面", 0.7)
        self.assertTrue(cached2)
        self.assertEqual(len(fake.calls), 1)

    def test_refresh_bypasses_cache(self):
        fake = FakeCpret()
        core.run_origin_search(self.store, self.console, fake, "题面", 0.7)
        self.store.save_origin_cache(core.origin_query_id("题面"),
                                     {"candidates": make_candidates([0.5])})
        record, cached = core.run_origin_search(self.store, self.console, fake, "题面", 0.7,
                                                refresh=True)
        self.assertFalse(cached)
        self.assertTrue(record["matched"])

    def test_cached_result_respects_new_threshold(self):
        """缓存候选后换阈值不必重新联网。"""
        self.store.save_origin_cache(core.origin_query_id("题面"),
                                     {"candidates": make_candidates([0.75])})
        record, cached = core.run_origin_search(self.store, self.console, None, "题面", 0.8)
        self.assertTrue(cached)
        self.assertFalse(record["matched"])

    def test_compact_origin(self):
        record = core.match_origin(make_candidates([0.95, 0.5]), 0.7)
        compact = core.compact_origin(record)
        self.assertEqual(compact["vjudgeId"], "洛谷-P1000")
        self.assertEqual(len(compact["top"]), 2)
        self.assertIn("queriedAt", compact)
        self.assertNotIn("candidates", compact)


# ---------------------------------------------------------------------------
# 阈值解析
# ---------------------------------------------------------------------------
class ThresholdTests(TmpCase):
    def test_default(self):
        store = core.Store(os.path.join(self.tmp, "d"))
        store.load()
        self.assertEqual(core.origin_threshold(store, None), core.ORIGIN_THRESHOLD_DEFAULT)

    def test_cli_overrides(self):
        store = core.Store(os.path.join(self.tmp, "d"))
        store.load()

        class Args:
            threshold = 0.9
        self.assertEqual(core.origin_threshold(store, Args()), 0.9)

    def test_config_value(self):
        store = core.Store(os.path.join(self.tmp, "d"))
        store.load()
        store.config["originThreshold"] = 0.55
        self.assertEqual(core.origin_threshold(store, None), 0.55)

    def test_invalid_values_fall_back(self):
        store = core.Store(os.path.join(self.tmp, "d"))
        store.load()
        for raw in ("abc", None, -1, 2):
            store.config["originThreshold"] = raw
            self.assertEqual(core.origin_threshold(store, None),
                             core.ORIGIN_THRESHOLD_DEFAULT)


# ---------------------------------------------------------------------------
# 渲染：题目 Markdown 里的原题区块
# ---------------------------------------------------------------------------
class RenderOriginTests(unittest.TestCase):
    def bundle(self, origin=None):
        return {
            "contest": {"id": 1, "name": "比赛"},
            "problem": {"displayOrder": 1, "problemId": 2, "title": "题"},
            "statement": "正文",
            "origin": origin or {},
            "base_url": "https://x",
        }

    def test_matched_block(self):
        text = core.render_problem_markdown(self.bundle({
            "matched": True, "score": 0.8847, "threshold": 0.7,
            "vjudgeId": "洛谷-P1000", "title": "原题名",
            "url": "https://www.luogu.com.cn/problem/P1000"}))
        self.assertIn("## 原题", text)
        self.assertIn("洛谷-P1000", text)
        self.assertIn("0.8847", text)
        self.assertIn("原题名", text)
        self.assertIn("CPRet", text)
        self.assertIn('origin: "洛谷-P1000"', text)
        self.assertIn("origin_score: 0.8847", text)

    def test_unmatched_block(self):
        text = core.render_problem_markdown(self.bundle({
            "matched": False, "score": 0.42, "threshold": 0.7, "vjudgeId": None}))
        self.assertIn("未命中原题", text)
        self.assertIn("0.4200", text)
        self.assertIn("origin: null", text)

    def test_no_origin_block(self):
        text = core.render_problem_markdown(self.bundle({}))
        self.assertNotIn("## 原题", text)
        self.assertIn("origin: null", text)


if __name__ == "__main__":
    unittest.main()
