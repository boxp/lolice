import importlib.util
import inspect
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
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
        plugin._exiting = False
        plugin._inflight = 0
        plugin._abandoned = False
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
        with patch.object(plugin, "_write_unlocked", side_effect=OSError("disk")):
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

    def test_final_snapshot_saved_during_aggregation_is_retired(self):
        directory = Path(TMP, "proc-stats")
        directory.mkdir(exist_ok=True)
        dying = directory / "999998-7.json"
        dying.write_text(json.dumps({"pid": 999998, "start": 7, "write_errors": 1}))

        def exits_after_final_save(pid, start):
            if pid == 999998:  # saves its final snapshot and exits while the owner aggregates
                dying.write_text(json.dumps({"pid": 999998, "start": 7, "write_errors": 4}))
                return False
            return True

        with patch.object(plugin, "_pid_alive", side_effect=exits_after_final_save):
            totals, _, _ = plugin._process_totals()
        self.assertEqual(totals["write_errors"], 4)
        self.assertEqual(json.loads((directory / "retired.json").read_text())["write_errors"], 4)

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
        self.assertFalse(old.exists())  # counted, then pruned by the owner itself
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
        # the absorbed value is exported right away, not only after the deletion succeeds
        self.assertIn('ai_ops_hermes_lost_events_total{reason="write_error"} 4',
                      Path(TMP, "metrics.prom").read_text())
        with patch.object(plugin, "_pid_alive", side_effect=alive):
            plugin._write_metrics()
            plugin._write_metrics()
        self.assertFalse(dead.exists())
        retired = json.loads((directory / "retired.json").read_text())
        self.assertEqual(retired["write_errors"], 4)
        self.assertEqual(retired["absorbed"], [])
        self.assertIn('ai_ops_hermes_lost_events_total{reason="write_error"} 4',
                      Path(TMP, "metrics.prom").read_text())

    def test_expired_ledger_is_counted_before_prune(self):
        d = Path(TMP, "ledger")
        d.mkdir(exist_ok=True)
        old = d / "events-2000-01-02.jsonl"
        old.write_text('{"event_type":"run.started"}\n' * 3)
        plugin._prune()  # not yet counted (owner down / first start): kept
        self.assertTrue(old.exists())
        with patch.object(plugin, "_metrics_owner", return_value=False):
            plugin._write_metrics()  # non-owner neither counts nor prunes
        self.assertTrue(old.exists())
        plugin._write_metrics()
        self.assertFalse(old.exists())
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 3',
                      Path(TMP, "metrics.prom").read_text())
        new_owner()
        plugin._write_metrics()
        self.assertIn('ai_ops_hermes_events_total{event_type="run.started"} 3',
                      Path(TMP, "metrics.prom").read_text())

    def test_partially_counted_expired_ledger_is_kept(self):
        d = Path(TMP, "ledger")
        d.mkdir(exist_ok=True)
        old = d / "events-2000-01-03.jsonl"
        old.write_text('{"event_type":"run.started"}\n')
        plugin._read_ledger()
        with old.open("a") as fh:
            fh.write('{"event_type":"run.started"}\n')
        plugin._prune()
        self.assertTrue(old.exists())

    def test_exit_flush_saves_late_losses_and_queued_events(self):
        plugin._write_proc_stats()
        plugin._turn_started(session_id="s-exit", turn_id="t1")
        plugin._count("api_request_errors")  # after the last periodic save
        plugin._flush_at_exit()
        self.assertTrue(any(json.loads(l)["event_type"] == "run.started" for l in ledger_lines()))
        pid = os.getpid()
        saved = json.loads(Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json").read_text())
        self.assertEqual(saved["api_request_errors"], 1)
        self.assertEqual(saved["queue_depth"], 0)

    def _sigterm_child(self, prelude, main="time.sleep(30)\n"):
        """Run the plugin in a child process whose periodic worker never writes, so only the
        SIGTERM path can save the queued event and the late loss, then SIGTERM it."""
        d = tempfile.mkdtemp()
        code = (
            "import importlib.util, signal, sys, threading, time\n"
            + prelude
            + "spec = importlib.util.spec_from_file_location('obs', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "m._run = lambda: threading.Event().wait()\n"
            "class C:\n    def register_hook(self, n, cb): pass\n"
            "m.register(C())\n"
            "m._turn_started(session_id='s-term', turn_id='t1')\n"
            "m._count('api_request_errors')\n"
            "print('ready', flush=True)\n"
            + main)
        env = dict(os.environ, BOXP_AI_OPS_DIR=d)
        child = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).with_name("__init__.py"))],
                                 env=env, stdout=subprocess.PIPE, text=True)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        child.send_signal(signal.SIGTERM)
        rc = child.wait(15)
        lines = [json.loads(l) for f in Path(d, "ledger").glob("events-*.jsonl")
                 for l in f.read_text().splitlines() if l]
        stats = [json.loads(f.read_text()) for f in Path(d, "proc-stats").glob("*-*.json")]
        return rc, d, lines, stats

    def test_sigterm_flushes_before_default_termination(self):
        rc, _, lines, stats = self._sigterm_child("")
        self.assertEqual(rc, -signal.SIGTERM)  # still terminated by SIGTERM
        self.assertTrue(any(e["event_type"] == "run.started" for e in lines))
        self.assertEqual([s["api_request_errors"] for s in stats], [1])

    def test_sigterm_flush_chains_previous_handler(self):
        prelude = ("def prev(s, f):\n"
                   "    import os; open(os.environ['BOXP_AI_OPS_DIR'] + '/prev', 'w').close(); sys.exit(3)\n"
                   "signal.signal(signal.SIGTERM, prev)\n")
        rc, d, lines, stats = self._sigterm_child(prelude)
        self.assertEqual(rc, 3)
        self.assertTrue(Path(d, "prev").exists())
        self.assertTrue(any(e["event_type"] == "run.started" for e in lines))
        self.assertEqual([s["api_request_errors"] for s in stats], [1])

    def test_sigterm_while_main_thread_holds_stats_lock_does_not_deadlock(self):
        # The handler runs on the main thread inside `with _lock`; it must not wait on the lock.
        rc, _, lines, stats = self._sigterm_child("", "with m._lock:\n    time.sleep(2)\ntime.sleep(30)\n")
        self.assertEqual(rc, -signal.SIGTERM)
        self.assertTrue(any(e["event_type"] == "run.started" for e in lines))
        self.assertEqual([s["api_request_errors"] for s in stats], [1])

    def test_sigterm_previous_handler_that_returns_keeps_later_events(self):
        # A graceful handler only requests shutdown; events and losses during the shutdown
        # must still reach the ledger and the final snapshot at exit.
        prelude = ("stop = threading.Event()\n"
                   "signal.signal(signal.SIGTERM, lambda s, f: stop.set())\n")
        main = ("while not stop.is_set():\n    time.sleep(0.05)\n"
                "m._turn_started(session_id='s-late', turn_id='t2')\n"
                "m._count('api_request_errors')\n"
                "sys.exit(0)\n")
        rc, _, lines, stats = self._sigterm_child(prelude, main)
        self.assertEqual(rc, 0)
        self.assertEqual(sum(e["event_type"] == "run.started" for e in lines), 2)
        self.assertEqual([s["api_request_errors"] for s in stats], [2])

    def test_exit_flush_is_bounded_when_worker_holds_write_lock(self):
        plugin._turn_started(session_id="s-stuck", turn_id="t1")
        plugin._write_lock.acquire()  # worker stuck in storage I/O while holding the lock
        try:
            began = time.monotonic()
            plugin._flush_at_exit(0.3)
            self.assertLess(time.monotonic() - began, 1.5)
        finally:
            plugin._write_lock.release()
        pid = os.getpid()
        saved = json.loads(Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json").read_text())
        self.assertEqual(saved["dropped_queue_full"], 1)  # unwritten event is counted

    def test_exit_flush_counts_event_taken_by_worker_waiting_for_lock(self):
        plugin._turn_started(session_id="s-race", turn_id="t1")
        plugin._write_lock.acquire()  # the exit flush (or a slow write) holds the lock
        worker = threading.Thread(target=plugin._drain, args=(1.0,))
        try:
            worker.start()
            time.sleep(0.2)
            # the worker must not hold the event outside the lock, where the exit flush cannot see it
            self.assertEqual(plugin._queue.qsize(), 1)
            plugin._flush_at_exit(0.3)
        finally:
            plugin._write_lock.release()
            worker.join(2)
        pid = os.getpid()
        saved = json.loads(Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json").read_text())
        written = sum(1 for l in ledger_lines() if json.loads(l)["event_type"] == "run.started")
        self.assertEqual(saved["dropped_queue_full"] + written, 1)  # neither written twice nor lost

    def test_exit_flush_counts_in_flight_write_that_fails_after_deadline(self):
        plugin._turn_started(session_id="s-late", turn_id="t1")
        started, release = threading.Event(), threading.Event()

        def slow_fail(e):
            started.set()
            release.wait(5)
            raise OSError("late write failure")

        with patch.object(plugin, "_write_unlocked", side_effect=slow_fail):
            worker = threading.Thread(target=plugin._drain, args=(1.0,))
            worker.start()
            self.assertTrue(started.wait(2))  # the worker holds the event and the write lock
            plugin._flush_at_exit(0.3)
            release.set()
            worker.join(2)
        pid = os.getpid()
        saved = json.loads(Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json").read_text())
        self.assertEqual(saved["write_errors"], 1)  # the late failure is in the final snapshot
        self.assertEqual(plugin._stats["write_errors"], 1)  # and not counted twice

    def test_in_flight_write_error_is_counted_once_without_exit(self):
        plugin._turn_started(session_id="s-err", turn_id="t1")
        with patch.object(plugin, "_write_unlocked", side_effect=OSError("boom")):
            plugin._drain(0.1)
        self.assertEqual(plugin._stats["write_errors"], 1)
        self.assertEqual(plugin._inflight, 0)

    def test_exit_flush_is_bounded_when_storage_io_hangs(self):
        plugin._turn_started(session_id="s-hang", turn_id="t1")
        with patch.object(plugin, "_write_unlocked", side_effect=lambda e: time.sleep(5)):
            began = time.monotonic()
            plugin._flush_at_exit(0.3)
            self.assertLess(time.monotonic() - began, 1.5)

    def test_exit_flush_counts_losses_when_its_own_write_hangs(self):
        plugin._turn_started(session_id="s-own", turn_id="t1")
        plugin._turn_started(session_id="s-own", turn_id="t2")
        release = threading.Event()
        with patch.object(plugin, "_write_unlocked", side_effect=lambda e: release.wait(5)):
            began = time.monotonic()
            plugin._flush_at_exit(0.3)
            self.assertLess(time.monotonic() - began, 2.5)
            release.set()
        pid = os.getpid()
        saved = json.loads(Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json").read_text())
        # the stuck in-flight event and the one still queued are both in the final snapshot
        self.assertEqual(saved["write_errors"] + saved["dropped_queue_full"], 2)

    def test_final_exit_snapshot_is_not_replaced_by_worker(self):
        plugin._count("write_errors")
        plugin._flush_at_exit()
        pid = os.getpid()
        path = Path(TMP, "proc-stats", f"{pid}-{plugin._start_ticks(pid)}.json")
        before = path.read_text()
        plugin._count("write_errors")
        self.assertFalse(plugin._write_proc_stats())  # late worker save is skipped
        self.assertEqual(path.read_text(), before)

    def test_concurrent_stats_saves_do_not_collide(self):
        errors = []

        def save():
            for _ in range(200):
                try:
                    plugin._write_proc_stats()
                except Exception as exc:  # fixed tmp path: replace of a vanished tmp
                    errors.append(exc)

        threads = [threading.Thread(target=save) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(list(Path(TMP, "proc-stats").glob("*.tmp")), [])

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
