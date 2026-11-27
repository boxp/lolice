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
        # 規則 6: 検索に「本人が書いた投稿だけ・スレッド等の他アカウントの投稿は挙げない」と頼む。
        q = self.mod.QUERY
        self.assertIn("authored by @b0xp2", q)
        self.assertIn("other accounts", q)

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


if __name__ == "__main__":
    unittest.main()
