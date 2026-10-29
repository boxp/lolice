import importlib.util
import inspect
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp()
os.environ["BOXP_AI_OPS_DIR"] = TMP
spec = importlib.util.spec_from_file_location("ai_ops_observer", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

CONV = re.compile(r"conv-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
RUN = r"\d{8}T\d{6}Z-[0-9a-f]{8}"


class Ctx:
    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, cb):
        self.hooks[name] = cb


def drain():
    while plugin._drain(0.01):
        pass


def ledger_lines():
    d = Path(TMP, "ledger")
    return [l for f in sorted(d.glob("events-*.jsonl")) for l in f.read_text().splitlines() if l]


class ObserverTests(unittest.TestCase):
    def setUp(self):
        for f in Path(TMP).glob("ledger/*.jsonl"):
            f.unlink()
        plugin._turns.clear()
        while not plugin._queue.empty():
            plugin._queue.get_nowait()

    def test_callbacks_declare_no_var_kwargs(self):
        ctx = Ctx()
        with patch.object(plugin, "_start_worker"):
            plugin.register(ctx)
        self.assertEqual(set(ctx.hooks), {"pre_llm_call", "on_session_end", "pre_approval_request",
                                          "post_approval_response", "api_request_error"})
        for name, cb in ctx.hooks.items():
            params = inspect.signature(cb).parameters.values()
            self.assertFalse(any(p.kind == p.VAR_KEYWORD for p in params), name)
            self.assertFalse({"user_message", "conversation_history", "command", "description", "args"}
                             & {p.name for p in params}, name)

    def test_turn_lifecycle_is_metadata_only(self):
        plugin._turn_started(session_id="telegram-123456", turn_id="t1")
        plugin._turn_started(session_id="telegram-123456", turn_id="t1")  # second iteration of the turn
        plugin._turn_finished(session_id="telegram-123456", turn_id="t1", completed=True)
        drain()
        events = [json.loads(l) for l in ledger_lines()]
        self.assertEqual([e["event_type"] for e in events], ["run.started", "run.finished"])
        start, fin = events
        self.assertRegex(start["task_id"], CONV)
        self.assertEqual(start["task_id"], start["request_id"])
        self.assertRegex(start["run_id"], RUN)
        self.assertEqual(start["run_id"], fin["run_id"])
        self.assertEqual(start["event_id"], f"{start['task_id']}/{start['run_id']}:started")
        self.assertEqual(fin["status"], "succeeded")
        self.assertEqual((start["source"], start["runtime"]), ("hermes-observer", "hermes"))
        raw = "\n".join(ledger_lines())
        self.assertNotIn("telegram", raw)
        self.assertNotIn("123456", raw)

    def test_finish_status_and_missing_start(self):
        plugin._turn_finished(session_id="s", turn_id="t9", failed=True)
        plugin._turn_finished(session_id="s", turn_id="t8", interrupted=True, failed=True)
        drain()
        events = [json.loads(l) for l in ledger_lines()]
        self.assertEqual([e["status"] for e in events], ["failed", "interrupted"])
        self.assertTrue(all("run_id" not in e and e["event_id"].endswith(":finished") for e in events))

    def test_repeated_approval_shares_key(self):
        plugin._approval_requested(session_key="k", pattern_key="rm -rf /tmp/x")
        plugin._approval_requested(session_key="k", pattern_key="rm -rf /tmp/x")
        plugin._approval_resolved(session_key="k", pattern_key="rm -rf /tmp/x")
        drain()
        events = [json.loads(l) for l in ledger_lines()]
        self.assertEqual([e["event_type"] for e in events],
                         ["approval.requested", "approval.requested", "approval.resolved"])
        self.assertEqual(len({e["approval_key"] for e in events}), 1)
        self.assertRegex(events[0]["approval_key"], r"approval:[0-9a-f]{16}")
        self.assertEqual(len({e["event_id"] for e in events}), 3)
        self.assertNotIn("rm -rf", "\n".join(ledger_lines()))

    def test_queue_overflow_is_counted_not_raised(self):
        before = plugin._stats["dropped_queue_full"]
        with patch.object(plugin, "_queue", plugin.queue.Queue(maxsize=1)):
            plugin._turn_started(session_id="s", turn_id="a")
            plugin._turn_started(session_id="s", turn_id="b")
        self.assertEqual(plugin._stats["dropped_queue_full"], before + 1)

    def test_file_cap_and_write_error_are_counted(self):
        with patch.object(plugin, "_MAX_BYTES", 1):
            plugin._turn_started(session_id="s", turn_id="c1")
            plugin._turn_started(session_id="s", turn_id="c2")
            before = plugin._stats["dropped_file_full"]
            drain()
        self.assertEqual(plugin._stats["dropped_file_full"], before + 1)
        before = plugin._stats["write_errors"]
        with patch.object(plugin, "_write", side_effect=OSError("disk")):
            plugin._turn_started(session_id="s", turn_id="c3")
            drain()
        self.assertEqual(plugin._stats["write_errors"], before + 1)

    def test_metrics_have_no_identifiers(self):
        plugin._turn_started(session_id="secret-session", turn_id="m1")
        plugin._turn_finished(session_id="secret-session", turn_id="m1", completed=True)
        drain()
        plugin._write_metrics()
        text = Path(TMP, "metrics.prom").read_text()
        self.assertIn('ai_ops_hermes_turns_finished_total{status="succeeded"}', text)
        self.assertIn('ai_ops_hermes_lost_events_total{reason="queue_full"}', text)
        self.assertIn('ai_ops_hermes_info{schema_version="ai-ops-event/v0"', text)
        self.assertNotRegex(text, r"conv-|secret")

    def test_only_lock_holder_writes_metrics(self):
        import fcntl
        plugin._write_metrics()
        self.assertIsNotNone(plugin._metrics_lock)
        fd = os.open(os.path.join(TMP, "metrics.lock"), os.O_WRONLY)
        try:
            with self.assertRaises(OSError):  # another process cannot take the writer lock
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_hashes_are_salted(self):
        a = plugin._conv("session", "x")
        with patch.object(plugin, "_salt", "other"):
            self.assertNotEqual(a, plugin._conv("session", "x"))


if __name__ == "__main__":
    unittest.main()
