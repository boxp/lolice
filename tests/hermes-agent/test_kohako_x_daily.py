"""BOXP-735: @b0xp2 の新着公開投稿を毎日確かめる cron script の単体テスト。

期待値は vault `Projects/kohako-x-read-grok/daily-spec.md`（kohako-x-daily の現行仕様）の規則と例から出す。
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "argoproj/hermes-agent/scripts/kohako-x-daily/kohako_x_daily.py"
UTC = timezone.utc
EPOCH_MS = 1288834974657


def load():
    spec = importlib.util.spec_from_file_location("kohako_x_daily", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sid(dt):
    """その時刻に作られた X の投稿 ID（snowflake）。"""
    return (int(dt.timestamp() * 1000) - EPOCH_MS) << 22


def url(i, handle="b0xp2"):
    return f"https://x.com/{handle}/status/{i}"


NOW = datetime(2026, 11, 28, 9, 0, tzinfo=UTC)  # 18:00 JST


class RangeTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_after_success(self):
        self.assertEqual(self.mod.search_range(datetime(2026, 11, 27, 9, 0, tzinfo=UTC), NOW),
                         ("2026-11-26", "2026-11-29"))

    def test_first_run(self):
        self.assertEqual(self.mod.search_range(None, NOW), ("2026-11-27", "2026-11-29"))

    def test_capped_to_seven_days(self):
        self.assertEqual(self.mod.search_range(NOW - timedelta(days=20), NOW), ("2026-11-21", "2026-11-29"))


class CredentialTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = pathlib.Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_var(self):
        self.assertTrue(self.mod.api_key_configured({"XAI_API_KEY": "x"}, str(self.env_file)))

    def test_env_file_line(self):
        self.env_file.write_text("FOO=1\nexport XAI_API_KEY=abc\n")
        self.assertTrue(self.mod.api_key_configured({}, str(self.env_file)))

    def test_comment_only_is_not_configured(self):
        self.env_file.write_text("# XAI_API_KEY=abc\n  #export XAI_API_KEY=abc\n")
        self.assertFalse(self.mod.api_key_configured({}, str(self.env_file)))

    def test_empty_env_file_line_is_configured(self):
        # 規則 3: 値が空の代入行でも止める側に倒す
        self.env_file.write_text("XAI_API_KEY=\n")
        self.assertTrue(self.mod.api_key_configured({}, str(self.env_file)))

    def test_empty_env_var_is_configured(self):
        self.assertTrue(self.mod.api_key_configured({"XAI_API_KEY": ""}, str(self.env_file)))

    def test_missing_file(self):
        self.assertFalse(self.mod.api_key_configured({}, str(self.env_file)))


class AcceptTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def ok(self, **kw):
        base = {"success": True, "degraded": False, "credential_source": "xai-oauth",
                "citations": [], "inline_citations": [], "answer": ""}
        base.update(kw)
        return base

    def test_result_ok(self):
        self.assertTrue(self.mod.result_ok(self.ok())[0])
        self.assertFalse(self.mod.result_ok(self.ok(success=False, error="boom"))[0])
        self.assertFalse(self.mod.result_ok(self.ok(degraded=True))[0])
        self.assertFalse(self.mod.result_ok(self.ok(credential_source="xai"))[0])

    def test_only_cited_b0xp2_in_range(self):
        a = sid(datetime(2026, 11, 27, 8, 0, tzinfo=UTC))
        b = sid(datetime(2026, 11, 28, 1, 0, tzinfo=UTC))
        old = sid(datetime(2026, 11, 25, 23, 0, tzinfo=UTC))
        future = sid(NOW + timedelta(hours=1))
        other = sid(datetime(2026, 11, 27, 9, 0, tzinfo=UTC))
        only_answer = sid(datetime(2026, 11, 27, 10, 0, tzinfo=UTC))
        res = self.ok(
            citations=[url(a), f"https://twitter.com/B0xp2/status/{b}?s=20"],
            inline_citations=[{"url": url(a)}, {"url": url(old)}, {"url": url(future)},
                              {"url": url(other, "someone")}],
            answer=f"{url(only_answer)} | 本文だけ")
        self.assertEqual(self.mod.cited_ids(res, "2026-11-26", NOW), [a, b])

    def test_handleless_i_status_is_adopted(self):
        # 規則 6 / 例: 公式 x_search は出典を https://x.com/i/status/<ID> で返すことがある（2026-11-27 本番実呼出）。
        a = sid(datetime(2026, 11, 27, 8, 4, tzinfo=UTC))
        old = sid(datetime(2026, 11, 25, 23, 0, tzinfo=UTC))
        res = self.ok(inline_citations=[{"url": url(a, "i")}, {"url": url(old, "i")}],
                      answer=f"{url(a)} | RT の要約")
        self.assertEqual(self.mod.cited_ids(res, "2026-11-26", NOW), [a])

    def test_i_status_without_b0xp2_in_answer_is_dropped(self):
        # 規則 6 / 例: allowed_x_handles はスレッド取得に効かない。本文で b0xp2 の投稿と示されない i の出典は捨てる。
        a = sid(datetime(2026, 11, 27, 8, 4, tzinfo=UTC))
        parent = sid(datetime(2026, 11, 27, 7, 0, tzinfo=UTC))
        other = sid(datetime(2026, 11, 27, 7, 30, tzinfo=UTC))
        bare = sid(datetime(2026, 11, 27, 7, 45, tzinfo=UTC))
        res = self.ok(inline_citations=[{"url": url(parent, "i")}, {"url": url(other, "i")},
                                        {"url": url(bare, "i")}, {"url": url(a, "i")}],
                      answer=(f"{url(a)} | 本人の返信\n{url(other, 'someone')} | 親投稿\n"
                              f"{url(bare, 'i')} | どこかの投稿"))
        self.assertEqual(self.mod.cited_ids(res, "2026-11-26", NOW), [a])

    def test_query_excludes_other_accounts(self):
        # 規則 6/13: 検索に「本人が書いた投稿だけ・他アカウントの投稿・リポストは挙げない」と頼む。
        q = self.mod.QUERY
        self.assertIn("authored by @b0xp2", q)
        self.assertIn("other accounts", q)
        self.assertIn("reposts", q)

    def test_classify_counts_drops(self):
        # 規則 11/13 / 例: 出典 i が 7 件、本文に b0xp2 が 3 件 → 3 件採用・dropped_unconfirmed=4
        own = [sid(datetime(2026, 11, 27, 11, m, tzinfo=UTC)) for m in (28, 31, 33)]
        rts = [sid(datetime(2026, 11, 27, h, 4, tzinfo=UTC)) for h in (6, 8, 9, 10)]
        other = sid(datetime(2026, 11, 27, 7, 0, tzinfo=UTC))
        old = sid(datetime(2026, 11, 20, 0, 0, tzinfo=UTC))
        res = self.ok(inline_citations=[{"url": url(i, "i")} for i in rts + own]
                      + [{"url": url(other, "someone")}, {"url": url(old)}],
                      answer="\n".join(f"{url(i)} | 本人" for i in own))
        ids, stats = self.mod.classify(res, "2026-11-26", NOW)
        self.assertEqual(ids, own)
        self.assertEqual(stats, {"dropped_unconfirmed": 4, "dropped_other": 1, "dropped_range": 1})

    def test_new_ids_and_seen_cap(self):
        self.assertEqual(self.mod.new_ids([1, 2, 3], ["2"]), [1, 3])
        seen = self.mod.update_seen([str(i) for i in range(600)], [1000, 1001])
        self.assertEqual(len(seen), 500)
        self.assertEqual(seen[:2], ["1001", "1000"])


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_line_with_id(self):
        i = 2126225619818275095
        ans = f"冒頭\n- [{url(i)}]({url(i)}) | **ゲーム更新の告知をリポスト** [[1]]({url(i)})\n- 次"
        self.assertEqual(self.mod.summary_for(ans, i), "ゲーム更新の告知をリポスト")

    def test_missing(self):
        self.assertEqual(self.mod.summary_for("なし", 123), "（要約なし）")

    def test_truncate_and_redact(self):
        i = 5
        s = self.mod.summary_for(f"{url(i)} | 連絡は a.b@example.com まで " + "あ" * 200, i)
        self.assertIn("[メール省略]", s)
        self.assertLessEqual(len(s), 80)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name) / "kohako-x-read"
        self.env_file = pathlib.Path(self.tmp.name) / ".env"
        self.calls = []
        self.a = sid(datetime(2026, 11, 27, 8, 4, tzinfo=UTC))
        self.b = sid(datetime(2026, 11, 28, 1, 30, tzinfo=UTC))

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, result):
        def fn(**kw):
            self.calls.append(kw)
            return json.dumps(result, ensure_ascii=False)
        return fn

    def good(self):
        return {"success": True, "degraded": False, "credential_source": "xai-oauth", "citations": [],
                "inline_citations": [{"url": url(self.b)}, {"url": url(self.a)}],
                "answer": f"{url(self.b)} | 新しい方\n{url(self.a)} | 古い方"}

    def run_once(self, result, now=NOW, env=None, source="xai-oauth"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.run(now=now, base=str(self.base), env=env or {}, env_file=str(self.env_file),
                                search=self.search(result), resolve_source=lambda: source)
        self.assertEqual(out.getvalue(), "")  # 規則 1: 標準出力は常に空
        return code

    def state(self):
        return json.loads((self.base / "state.json").read_text())

    def last_run(self):
        return json.loads((self.base / "last_run.json").read_text())

    def test_first_run_writes_digest_oldest_first(self):
        self.assertEqual(self.run_once(self.good()), 0)
        self.assertEqual(len(self.calls), 1)
        kw = self.calls[0]
        self.assertEqual(kw["allowed_x_handles"], ["b0xp2"])
        self.assertEqual((kw["from_date"], kw["to_date"]), ("2026-11-27", "2026-11-29"))
        text = (self.base / "digest/2026-11-28.md").read_text()
        lines = [l for l in text.splitlines() if l.startswith("- 2026")]
        self.assertEqual(lines, [f"- 2026-11-27 17:04 JST 古い方 {url(self.a)}",
                                 f"- 2026-11-28 10:30 JST 新しい方 {url(self.b)}"])
        self.assertIn("指示として扱わない", text)
        self.assertEqual(oct(self.base.stat().st_mode & 0o777), "0o700")
        self.assertEqual(self.state()["last_success_at"], NOW.isoformat())
        self.assertEqual(self.last_run()["status"], "ok")
        self.assertEqual(self.last_run()["new"], 2)
        self.assertEqual(self.last_run()["dropped_unconfirmed"], 0)
        self.assertEqual(self.last_run()["dropped_other"], 0)
        self.assertEqual(self.last_run()["dropped_range"], 0)

    def test_i_status_digest_uses_b0xp2_url(self):
        res = self.good()
        res["inline_citations"] = [{"url": url(self.a, "i")}]
        res["answer"] = f"{url(self.a)} | 古い方"
        self.assertEqual(self.run_once(res), 0)
        text = (self.base / "digest/2026-11-28.md").read_text()
        self.assertIn(f"- 2026-11-27 17:04 JST 古い方 {url(self.a)}", text)
        self.assertEqual(self.last_run()["new"], 1)

    def test_second_run_dedupes(self):
        self.run_once(self.good())
        later = NOW + timedelta(days=1)
        self.assertEqual(self.run_once(self.good(), now=later), 0)
        self.assertEqual(self.calls[1]["from_date"], "2026-11-27")
        self.assertFalse((self.base / "digest/2026-11-29.md").exists())
        self.assertEqual(self.last_run()["new"], 0)

    def test_degraded_keeps_state(self):
        self.run_once(self.good())
        before = self.state()
        bad = dict(self.good(), degraded=True)
        self.assertEqual(self.run_once(bad, now=NOW + timedelta(days=1)), 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.last_run()["status"], "error")

    def test_api_key_blocks_call(self):
        self.env_file.write_text("XAI_API_KEY=abc\n")
        self.assertEqual(self.run_once(self.good()), 1)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "state.json").exists())

    def test_empty_api_key_line_blocks_call(self):
        self.env_file.write_text("XAI_API_KEY=\n")
        self.assertEqual(self.run_once(self.good()), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.last_run()["status"], "error")
        self.assertIn("XAI_API_KEY", self.last_run()["reason"])

    def test_api_key_overwrites_previous_ok_last_run(self):
        # 規則 11: 直前の成功の last_run を残さず、error と理由を書く
        self.assertEqual(self.run_once(self.good()), 0)
        before = self.state()
        self.env_file.write_text("XAI_API_KEY=abc\n")
        self.assertEqual(self.run_once(self.good(), now=NOW + timedelta(days=1)), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.last_run()["status"], "error")
        self.assertIn("XAI_API_KEY", self.last_run()["reason"])
        self.assertNotIn("abc", json.dumps(self.last_run()))
        self.assertEqual(self.state(), before)

    def test_non_oauth_source_blocks_call(self):
        self.assertEqual(self.run_once(self.good(), source="xai"), 1)
        self.assertEqual(self.calls, [])

    def test_stop_file(self):
        self.base.mkdir(parents=True)
        (self.base / "停止中").write_text("")
        self.assertEqual(self.run_once(self.good()), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.last_run()["status"], "stopped")

    def test_prunes_old_digests(self):
        d = self.base / "digest"
        d.mkdir(parents=True)
        (d / "2026-11-20.md").write_text("old")
        (d / "2026-11-21.md").write_text("keep")
        self.run_once(self.good())
        self.assertFalse((d / "2026-11-20.md").exists())
        self.assertTrue((d / "2026-11-21.md").exists())

    def _old_digest(self):
        d = self.base / "digest"
        d.mkdir(parents=True, exist_ok=True)
        (d / "2026-11-20.md").write_text("old")
        (d / "2026-11-21.md").write_text("keep")
        return d

    def _assert_pruned(self, d):
        self.assertFalse((d / "2026-11-20.md").exists())
        self.assertTrue((d / "2026-11-21.md").exists())

    def test_prunes_on_search_failure(self):
        # 規則 10: 検索が失敗し続けても 7 日を超えて控えを残さない
        d = self._old_digest()
        self.assertEqual(self.run_once(dict(self.good(), success=False, error="x")), 1)
        self._assert_pruned(d)

    def test_prunes_on_credential_error(self):
        d = self._old_digest()
        self.assertEqual(self.run_once(self.good(), source="xai"), 1)
        self._assert_pruned(d)

    def test_prunes_on_api_key(self):
        d = self._old_digest()
        self.env_file.write_text("XAI_API_KEY=abc\n")
        self.assertEqual(self.run_once(self.good()), 1)
        self._assert_pruned(d)

    def test_prunes_when_stopped(self):
        d = self._old_digest()
        (self.base / "停止中").write_text("")
        self.assertEqual(self.run_once(self.good()), 0)
        self._assert_pruned(d)


def ok_result(ids, answer_ids=None):
    """i の形の出典で ids を返し、回答本文には answer_ids（既定は ids）を b0xp2 の URL で挙げる。"""
    answer_ids = ids if answer_ids is None else answer_ids
    return {"success": True, "degraded": False, "credential_source": "xai-oauth", "citations": [],
            "inline_citations": [{"url": url(i, "i")} for i in ids],
            "answer": "\n".join(f"{url(i)} | 要約{n}" for n, i in enumerate(answer_ids))}


class InitialTest(unittest.TestCase):
    """仕様 §4 初回分。"""
    NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)

    def setUp(self):
        self.mod = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name) / "kohako-x-read"
        self.env_file = pathlib.Path(self.tmp.name) / ".env"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_initial(self, results, env=None, source="xai-oauth"):
        results = list(results)

        def search(**kw):
            self.calls.append(kw)
            r = results.pop(0)
            return json.dumps(r(kw) if callable(r) else r, ensure_ascii=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.mod.run_initial(now=self.NOW, base=str(self.base), env=env or {},
                                        env_file=str(self.env_file), search=search,
                                        resolve_source=lambda: source)
        self.assertEqual(out.getvalue(), "")
        return code

    def windows(self):
        return [(c["from_date"], c["to_date"]) for c in self.calls]

    def meta(self):
        return json.loads((self.base / "initial/meta.json").read_text())

    def test_first_window_and_unsaturated_steps(self):
        # 例: 1 回目 from=2026-10-29,to=2026-11-28。採用 5 件なら次は to=前の from。4 回で終わる。
        def five(kw):
            to = datetime.fromisoformat(kw["to_date"]).replace(tzinfo=UTC)
            return ok_result([sid(to - timedelta(days=2, hours=h)) for h in range(5)])
        self.assertEqual(self.run_initial([five] * 4), 0)
        self.assertEqual(self.windows(), [("2026-10-29", "2026-11-28"), ("2026-09-29", "2026-10-29"),
                                          ("2026-08-30", "2026-09-29"), ("2026-07-31", "2026-08-30")])
        m = self.meta()
        self.assertEqual(m["calls"], 4)
        self.assertEqual(m["count"], 20)
        self.assertEqual(m["stop"], "max_calls")
        self.assertEqual(len(m["windows"]), 4)
        self.assertEqual(m["windows"][0]["cited"], 5)

    def test_saturated_moves_to_oldest_date(self):
        # 例: 採用 25 件・最古 2026-11-10 → 2 回目 to=2026-11-10, from=2026-10-11
        first = [sid(datetime(2026, 11, 10, 3, 0, tzinfo=UTC) + timedelta(hours=10 * n)) for n in range(25)]
        self.assertEqual(self.run_initial([ok_result(first), dict(ok_result([]), degraded=True)]), 0)
        self.assertEqual(self.windows()[1], ("2026-10-11", "2026-11-10"))
        self.assertEqual(self.meta()["stop"], "degraded")
        self.assertEqual(self.meta()["count"], 25)

    def test_saturated_same_day_steps_back(self):
        # 規則 4: 最古の日付が今の to と同じなら to-1 日
        # 1 回目（to=11-28）: 全部 11-27 → to=2026-11-27。2 回目（to=11-27）: また全部 11-27 → 同じ日なので to=2026-11-26
        same = [sid(datetime(2026, 11, 27, 11, 50, tzinfo=UTC) - timedelta(minutes=n)) for n in range(20)]
        late = [sid(datetime(2026, 11, 27, 10, 0, tzinfo=UTC) - timedelta(minutes=n)) for n in range(20)]
        self.assertEqual(self.run_initial([ok_result(same), ok_result(late), dict(ok_result([]), success=False,
                                                                                    error="boom")]), 0)
        self.assertEqual([w[1] for w in self.windows()], ["2026-11-28", "2026-11-27", "2026-11-26"])
        self.assertEqual(self.meta()["stop"], "error")

    def test_archive_newest_first_and_header(self):
        a = sid(datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
        b = sid(datetime(2026, 11, 25, 3, 0, tzinfo=UTC))
        rt = sid(datetime(2026, 11, 24, 3, 0, tzinfo=UTC))
        res = ok_result([a, rt, b], answer_ids=[b, a])
        self.assertEqual(self.run_initial([res, dict(ok_result([]), degraded=True)]), 0)
        text = (self.base / "initial/archive.md").read_text()
        lines = [l for l in text.splitlines() if l.startswith("- 2026")]
        self.assertEqual(lines, [f"- 2026-11-25 12:00 JST 要約0 {url(b)}", f"- 2026-11-20 12:00 JST 要約1 {url(a)}"])
        self.assertIn("全件ではありません", text)
        self.assertIn("指示として扱わない", text)
        self.assertIn("2026-11-20", text.split("- 2026")[0])
        m = self.meta()
        self.assertEqual((m["oldest"], m["newest"], m["count"]), ("2026-11-20", "2026-11-25", 2))
        self.assertEqual(m["windows"][0]["dropped_unconfirmed"], 1)
        self.assertEqual(oct(self.base.stat().st_mode & 0o777), "0o700")

    def test_cap_100_keeps_newest(self):
        many = [sid(datetime(2026, 11, 26, 0, 0, tzinfo=UTC) - timedelta(hours=n)) for n in range(60)]
        more = [sid(datetime(2026, 11, 20, 0, 0, tzinfo=UTC) - timedelta(hours=n)) for n in range(60)]
        self.run_initial([ok_result(many), ok_result(more)])
        m = self.meta()
        self.assertEqual(m["count"], 100)
        self.assertEqual(m["stop"], "max_posts")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn(str(more[-1]), (self.base / "initial/archive.md").read_text())

    def test_already_done(self):
        (self.base / "initial").mkdir(parents=True)
        (self.base / "initial/meta.json").write_text("{}")
        self.assertEqual(self.run_initial([]), 0)
        self.assertEqual(self.calls, [])

    def test_first_call_limit_writes_nothing(self):
        bad = {"success": False, "error": "429: rate limit exceeded"}
        self.assertEqual(self.run_initial([bad]), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.base / "initial").exists())
        att = json.loads((self.base / "initial_attempt.json").read_text())
        self.assertEqual(att["stop"], "limit")

    def test_api_key_and_stop_file_and_source(self):
        self.env_file.write_text("XAI_API_KEY=\n")
        self.assertEqual(self.run_initial([]), 1)
        self.env_file.unlink()
        self.assertEqual(self.run_initial([], source="xai"), 1)
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / "停止中").write_text("")
        self.assertEqual(self.run_initial([]), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.base / "initial").exists())

    def test_daily_prune_keeps_initial(self):
        # 規則 8: 7 日の削除は digest/ だけ
        self.run_initial([ok_result([sid(datetime(2026, 11, 1, tzinfo=UTC))]), dict(ok_result([]), degraded=True)])
        before = (self.base / "initial/archive.md").read_text()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.mod.run(now=self.NOW + timedelta(days=30), base=str(self.base), env={},
                         env_file=str(self.env_file), search=lambda **kw: json.dumps(ok_result([])),
                         resolve_source=lambda: "xai-oauth")
        self.assertEqual((self.base / "initial/archive.md").read_text(), before)


if __name__ == "__main__":
    unittest.main()
