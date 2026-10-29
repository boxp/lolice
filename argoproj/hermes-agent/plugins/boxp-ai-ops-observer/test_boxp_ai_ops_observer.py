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


def new_owner():
    """Simulate a fresh metrics owner process (in-memory ledger state lost)."""
    plugin._ledger_offsets.clear()
    plugin._ledger_counts.update(by_type={}, run_finished={}, malformed=0, last_emit=0)
    plugin._ledger_state_loaded = False


class ObserverTests(unittest.TestCase):
    def setUp(self):
        for f in Path(TMP).glob("ledger/*.jsonl"):
            f.unlink()
        for f in Path(TMP).glob("proc-stats/*.json"):
            f.unlink()
        for name in ("ledger-state.json", "proc-stats/retired.json"):
            if Path(TMP, name).exists():
                Path(TMP, name).unlink()
        new_owner()
        for key in plugin._LOSS_KEYS:
            plugin._stats[key] = 0
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

    def test_ledger_incremental_and_partial_lines(self):
        path = Path(plugin._day_file(plugin._now()))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            fh.write(json.dumps({"event_type": "run.started", "event_id": "conv-private/run-private"}) + "\n")
        plugin._write_metrics()
        metrics = Path(TMP, "metrics.prom")
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 1', metrics.read_text())
        plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 1', metrics.read_text())
        with path.open("a") as fh:
            fh.write('{"event_type":"run.finished","status":"unexpected"}')
        plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.finished"} 0', metrics.read_text())
        with path.open("a") as fh:
            fh.write('\nnot json\n')
        plugin._write_metrics()
        text = metrics.read_text()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.finished"} 1', text)
        self.assertIn('ai_ops_hermes_turns_finished_total{status="unknown"} 1', text)
        self.assertIn('ai_ops_hermes_ledger_malformed_lines_total 1', text)
        self.assertNotIn('conv-', text)
        self.assertNotIn('run-private', text)
        path.unlink()
        plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 1', metrics.read_text())

    def test_dead_process_stats_are_retired(self):
        directory = Path(TMP, "proc-stats")
        directory.mkdir(exist_ok=True)
        dead = directory / "999999-5.json"
        dead.write_text(json.dumps({"pid": 999999, "start": 5, "dropped_queue_full": 3, "queue_depth": 9}))
        with patch.object(plugin, "_pid_alive", side_effect=lambda pid, start: pid != 999999):
            plugin._write_metrics()
            self.assertFalse(dead.exists())
            self.assertEqual(json.loads((directory / "retired.json").read_text())["dropped_queue_full"], 3)
            plugin._write_metrics()
        text = Path(TMP, "metrics.prom").read_text()
        self.assertIn('ai_ops_hermes_lost_events_total{reason="queue_full"} 3', text)
        self.assertIn('ai_ops_hermes_processes 1', text)
        self.assertIn('ai_ops_hermes_queue_depth 0', text)

    def test_owner_change_keeps_counters_monotonic(self):
        d = Path(TMP, "ledger")
        d.mkdir(exist_ok=True)
        old = d / "events-2000-01-01.jsonl"
        cur = Path(plugin._day_file(plugin._now()))
        old.write_text('{"event_type":"run.started"}\n' * 2)
        cur.write_text('{"event_type":"run.started"}\n')
        plugin._write_metrics()
        metrics = Path(TMP, "metrics.prom")
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 3', metrics.read_text())
        old.unlink()  # pruned while owned by the first process
        new_owner()
        with cur.open("a") as fh:
            fh.write('{"event_type":"run.started"}\n')
        plugin._write_metrics()
        # neither a reset (pruned 2 lost) nor a re-count of the retained line
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 4', metrics.read_text())
        new_owner()
        plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 4', metrics.read_text())
        state = Path(TMP, "ledger-state.json").read_text()
        self.assertNotIn(old.name, state)

    def test_corrupt_ledger_state_does_not_reset_counters(self):
        cur = Path(plugin._day_file(plugin._now()))
        cur.parent.mkdir(parents=True, exist_ok=True)
        cur.write_text('{"event_type":"run.started"}\n')
        plugin._write_metrics()
        Path(TMP, "ledger-state.json").write_text("{broken")
        new_owner()
        with self.assertRaises(ValueError):
            plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 1',
                      Path(TMP, "metrics.prom").read_text())

    def test_retire_is_idempotent_when_unlink_fails(self):
        directory = Path(TMP, "proc-stats")
        directory.mkdir(exist_ok=True)
        dead = directory / "999998-7.json"
        dead.write_text(json.dumps({"pid": 999998, "start": 7, "write_errors": 4}))
        real_unlink = os.unlink

        def failing_unlink(path):
            if str(path).endswith("999998-7.json"):
                raise OSError("unlink failed")
            return real_unlink(path)

        alive = lambda pid, start: pid != 999998
        with patch.object(plugin, "_pid_alive", side_effect=alive), \
                patch.object(plugin.os, "unlink", side_effect=failing_unlink):
            plugin._write_metrics()
        self.assertTrue(dead.exists())
        with patch.object(plugin, "_pid_alive", side_effect=alive):
            plugin._write_metrics()
            plugin._write_metrics()
        self.assertFalse(dead.exists())
        retired = json.loads((directory / "retired.json").read_text())
        self.assertEqual(retired["write_errors"], 4)
        self.assertEqual(retired["absorbed"], [])
        self.assertIn('ai_ops_hermes_lost_events_total{reason="write_error"} 4',
                      Path(TMP, "metrics.prom").read_text())

    def test_reused_pid_from_previous_pod_is_retired(self):
        # proc-stats/ lives on the PVC; after a pod restart a new process can get the same pid.
        directory = Path(TMP, "proc-stats")
        directory.mkdir(exist_ok=True)
        retired = directory / "retired.json"
        if retired.exists():
            retired.unlink()
        pid = os.getpid()
        stale = directory / f"{pid}-1.json"
        stale.write_text(json.dumps({"pid": pid, "start": 1, "write_errors": 2}))
        plugin._write_metrics()
        self.assertFalse(stale.exists())
        self.assertTrue((directory / f"{pid}-{plugin._start_ticks(pid)}.json").exists())
        text = Path(TMP, "metrics.prom").read_text()
        self.assertIn('ai_ops_hermes_lost_events_total{reason="write_error"} 2', text)
        self.assertIn('ai_ops_hermes_processes 1', text)

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
