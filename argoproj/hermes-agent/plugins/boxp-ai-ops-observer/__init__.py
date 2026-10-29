"""AI ops observer (BOXP-360 Plan A stage 1).

Metadata-only ledger of Hermes turns and approvals in the shared ai-ops-event/v0 schema
(see boxp/arch docs/project_docs/BOXP-360/tools/ai_ops_sli.bb):

- pre_llm_call -> run.started (once per session turn)
- on_session_end -> run.finished (succeeded / failed / interrupted)
- pre_approval_request -> approval.requested, post_approval_response -> approval.resolved
  (approval_key is a salted hash of the pattern key, so repeated requests of the same approval
  in one session can be counted without storing the command)
- api_request_error -> counter only

Privacy: every callback declares only the identifier / status arguments it needs, so the hook
dispatcher never passes message bodies, history, tool args or commands to this module. Session,
turn and pattern identifiers are reduced to salted SHA-256 hashes (local random salt under the
ledger dir, never exported); model names, platform user IDs and text are not recorded.

Observation must never slow or stop the agent: callbacks only enqueue a small dict
(put_nowait on a bounded queue) and a daemon worker writes the JSONL ledger. Queue overflow,
day-file cap and write errors are counted and exported in metrics.prom. Ledger counts and
per-process loss statistics are aggregated across processes without exporting identifiers.
"""
import atexit
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta

_log = logging.getLogger(__name__)

SCHEMA = "ai-ops-event/v0"
DEFINITION = "hermes-observer/v1"
_DIR = os.environ.get("BOXP_AI_OPS_DIR", "/opt/data/ai-ops")
_MAX_BYTES = int(os.environ.get("BOXP_AI_OPS_LEDGER_MAX_BYTES", str(8 * 1024 * 1024)))
_RETENTION_DAYS = int(os.environ.get("BOXP_AI_OPS_LEDGER_RETENTION_DAYS", "90"))
_QUEUE_MAX = 1000
_TURNS_MAX = 512

_lock = threading.Lock()
_write_lock = threading.Lock()  # worker and exit flush both append
_save_lock = threading.Lock()  # serializes proc-stats saves (worker vs exit flush)
_exiting = False  # set by the exit flush: later worker saves must not replace its snapshot
_queue = queue.Queue(maxsize=_QUEUE_MAX)
_turns = OrderedDict()  # (session hash, turn hash) -> run_id
_stats = {"emitted": 0, "dropped_queue_full": 0, "dropped_file_full": 0, "write_errors": 0,
          "api_request_errors": 0, "by_type": {}, "run_finished": {}, "last_emit": None}
_worker = None
_salt = None
_metrics_lock = None  # open fd holding the metrics writer flock
_ledger_offsets = {}  # file name -> (inode, byte offset)
_ledger_counts = {"by_type": {}, "run_finished": {}, "malformed": 0, "last_emit": 0}
_ledger_state_loaded = False
_EVENT_TYPES = ("run.started", "run.finished", "approval.requested", "approval.resolved")
_STATUSES = ("succeeded", "failed", "interrupted", "unknown")
_LOSS_KEYS = ("dropped_queue_full", "dropped_file_full", "write_errors", "api_request_errors")


def register(ctx):
    ctx.register_hook("pre_llm_call", _turn_started)
    ctx.register_hook("on_session_end", _turn_finished)
    ctx.register_hook("pre_approval_request", _approval_requested)
    ctx.register_hook("post_approval_response", _approval_resolved)
    ctx.register_hook("api_request_error", _api_error)
    _start_worker()


# ---------------------------------------------------------------------------
# identifiers

def _get_salt():
    global _salt
    if _salt is None:
        path = os.path.join(_DIR, "salt")
        try:
            with open(path) as fh:
                _salt = fh.read().strip()
        except OSError:
            _salt = os.urandom(16).hex()
            try:
                os.makedirs(_DIR, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(_salt)
            except FileExistsError:
                with open(path) as fh:
                    _salt = fh.read().strip()
            except OSError:
                pass  # in-memory salt: hashes stay opaque, only cross-restart joins are lost
    return _salt


def _hex(kind, value, n):
    return hashlib.sha256(f"{_get_salt()}:{kind}:{value}".encode()).hexdigest()[:n]


def _conv(kind, value):
    h = _hex(kind, value, 32)
    return f"conv-{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _base(task_id, event_type):
    now = _iso(_now())
    return {"schema_version": SCHEMA, "source": "hermes-observer", "runtime": "hermes",
            "task_id": task_id, "request_id": task_id, "event_type": event_type,
            "occurred_at": now, "observed_at": now, "occurred_precision": "ns",
            "cost_source": "unknown"}


# ---------------------------------------------------------------------------
# hooks. Each callback declares only metadata parameters and no **kwargs: the dispatcher
# passes just the declared names, so bodies / history / commands never reach this module.

def _turn_started(session_id=None, turn_id=None):
    try:
        if not session_id:
            return None
        task = _conv("session", session_id)
        key = (task, _hex("turn", turn_id, 16))
        with _lock:
            if key in _turns:
                return None
            run_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-" + _hex("turn", turn_id, 8)
            _turns[key] = run_id
            while len(_turns) > _TURNS_MAX:
                _turns.popitem(last=False)
        e = _base(task, "run.started")
        e.update(run_id=run_id, event_id=f"{task}/{run_id}:started")
        _enqueue(e)
    except Exception:  # never affect the turn
        _count("write_errors")
    return None


def _turn_finished(session_id=None, turn_id=None, completed=None, failed=None, interrupted=None):
    try:
        if not session_id:
            return None
        task = _conv("session", session_id)
        with _lock:
            run_id = _turns.pop((task, _hex("turn", turn_id, 16)), None)
        status = "interrupted" if interrupted else "failed" if failed else "succeeded" if completed else None
        e = _base(task, "run.finished")
        if status:
            e["status"] = status
        if run_id:
            e.update(run_id=run_id, event_id=f"{task}/{run_id}:finished")
        else:
            # Start not seen (plugin restart / sampling): keep the fact without a run link.
            e["event_id"] = f"{task}/{_hex('fin', f'{turn_id}:{time.time_ns()}', 16)}:finished"
        _enqueue(e)
    except Exception:
        _count("write_errors")
    return None


def _approval_key(pattern_key, pattern_keys):
    keys = sorted(str(k) for k in (pattern_keys or []) if k) or ([str(pattern_key)] if pattern_key else [])
    return "approval:" + _hex("approval", "|".join(keys), 16) if keys else None


def _approval(event_type, word, session_key, pattern_key, pattern_keys):
    if not session_key:
        return
    task = _conv("session-key", session_key)
    e = _base(task, event_type)
    akey = _approval_key(pattern_key, pattern_keys)
    if akey:
        e["approval_key"] = akey
    e["event_id"] = f"{task}/{_hex('approval', f'{akey}:{time.time_ns()}', 16)}:approval:{word}"
    _enqueue(e)


def _approval_requested(session_key=None, pattern_key=None, pattern_keys=None):
    try:
        _approval("approval.requested", "requested", session_key, pattern_key, pattern_keys)
    except Exception:
        _count("write_errors")
    return None


def _approval_resolved(session_key=None, pattern_key=None, pattern_keys=None):
    try:
        _approval("approval.resolved", "resolved", session_key, pattern_key, pattern_keys)
    except Exception:
        _count("write_errors")
    return None


def _api_error(status_code=None):
    _count("api_request_errors")
    return None


# ---------------------------------------------------------------------------
# bounded delivery

def _count(key, sub=None):
    with _lock:
        if sub is None:
            _stats[key] = _stats.get(key, 0) + 1
        else:
            _stats[key][sub] = _stats[key].get(sub, 0) + 1


def _enqueue(e):
    try:
        _queue.put_nowait(e)
    except queue.Full:
        _count("dropped_queue_full")


def _day_file(now):
    return os.path.join(_DIR, "ledger", f"events-{now.strftime('%Y-%m-%d')}.jsonl")


def _write(e):
    with _write_lock:
        _write_unlocked(e)


def _write_unlocked(e):
    path = _day_file(_now())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) >= _MAX_BYTES:
            _count("dropped_file_full")
            return
    except FileNotFoundError:
        pass
    with open(path, "a") as fh:
        fh.write(json.dumps(e, separators=(",", ":")) + "\n")


def _ledger_state_path():
    return os.path.join(_DIR, "ledger-state.json")


def _load_ledger_state():
    """Resume counts and offsets persisted by a previous metrics owner, so an owner change
    (process exit / pod restart) neither re-counts retained lines nor loses pruned counts."""
    global _ledger_state_loaded
    if _ledger_state_loaded:
        return
    try:
        with open(_ledger_state_path()) as fh:
            state = json.load(fh)
        offsets = {str(k): (int(v[0]), int(v[1])) for k, v in state["offsets"].items()}
        counts = state["counts"]
        loaded = {"by_type": {k: int(v) for k, v in counts["by_type"].items() if k in _EVENT_TYPES},
                  "run_finished": {k: int(v) for k, v in counts["run_finished"].items() if k in _STATUSES},
                  "malformed": int(counts["malformed"]), "last_emit": float(counts["last_emit"])}
    except FileNotFoundError:
        offsets, loaded = {}, None
    # Other errors propagate: exporting from zero would look like a counter reset and
    # re-count every retained line, so the owner keeps the previous metrics.prom instead.
    _ledger_offsets.clear()
    _ledger_offsets.update(offsets)
    if loaded:
        _ledger_counts.update(loaded)
    _ledger_state_loaded = True


def _save_ledger_state():
    _atomic_json(_ledger_state_path(), {
        "offsets": {k: list(v) for k, v in _ledger_offsets.items()},
        "counts": _ledger_counts})


def _read_ledger():
    """Count complete appended lines, retaining counts when old files are pruned.
    The state is persisted before metrics.prom is written, so exported counters never go
    backwards across owner changes."""
    _load_ledger_state()
    directory = os.path.join(_DIR, "ledger")
    names = sorted(os.listdir(directory) if os.path.isdir(directory) else [])
    for name in names:
        if not (name.startswith("events-") and name.endswith(".jsonl")
                and len(name) == len("events-YYYY-MM-DD.jsonl")):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as fh:
                stat = os.fstat(fh.fileno())
                # inode only: the device number of the PVC can change between pods.
                previous = _ledger_offsets.get(name)
                offset = previous[1] if previous and previous[0] == stat.st_ino and stat.st_size >= previous[1] else 0
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    offset = fh.tell()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("ledger entry is not an object")
                    except (ValueError, UnicodeDecodeError):
                        _ledger_counts["malformed"] += 1
                        continue
                    kind = event.get("event_type")
                    if kind in _EVENT_TYPES:
                        counts = _ledger_counts["by_type"]
                        counts[kind] = counts.get(kind, 0) + 1
                        if kind == "run.finished":
                            status = event.get("status")
                            if status not in _STATUSES:
                                status = "unknown"
                            counts = _ledger_counts["run_finished"]
                            counts[status] = counts.get(status, 0) + 1
                _ledger_offsets[name] = (stat.st_ino, offset)
                _ledger_counts["last_emit"] = max(_ledger_counts["last_emit"], stat.st_mtime)
        except FileNotFoundError:
            continue  # retention may remove a file while it is being scanned
    for name in [n for n in _ledger_offsets if n not in names]:
        del _ledger_offsets[name]  # pruned file: its counts stay in _ledger_counts
    _save_ledger_state()


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"  # unique per writer thread
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _start_ticks(pid):
    """Process start time (clock ticks since boot) from /proc, or 0 if unavailable.
    Combined with the pid it identifies a process: pids are reused after a pod restart
    while proc-stats/ lives on the PVC."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return 0


def _write_proc_stats(final=False, timeout=None):
    """Save this process's loss counters. Saves are serialized and the snapshot is taken under
    the save lock, so a later save never holds smaller counts than an earlier one. Once the
    exit flush has saved (final), worker saves are skipped so the final snapshot stays last."""
    global _exiting
    if not _save_lock.acquire(timeout=-1 if timeout is None else max(0.0, timeout)):
        return False
    try:
        if _exiting and not final:
            return False
        if final:
            _exiting = True
        with _lock:
            data = {key: _stats[key] for key in _LOSS_KEYS}
        pid = os.getpid()
        start = _start_ticks(pid)
        data.update(pid=pid, start=start, queue_depth=_queue.qsize())
        _atomic_json(os.path.join(_DIR, "proc-stats", f"{pid}-{start}.json"), data)
        return True
    finally:
        _save_lock.release()


def _pid_alive(pid, start):
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass
    except ProcessLookupError:
        return False
    return _start_ticks(pid) == start


def _process_totals():
    """Sum loss counters of live processes and of exited ones (retired.json).
    retired.json records which stats files it already absorbed, so a crash or unlink failure
    between updating it and deleting the file does not count that process twice."""
    directory = os.path.join(_DIR, "proc-stats")
    os.makedirs(directory, exist_ok=True)
    retired_path = os.path.join(directory, "retired.json")
    try:
        with open(retired_path) as fh:
            retired = json.load(fh)
    except FileNotFoundError:
        retired = {}
    names = os.listdir(directory)
    # Ids whose file is gone can never be absorbed again; drop them to keep the file small.
    absorbed = {n for n in retired.get("absorbed", []) if n in names}
    if absorbed != set(retired.get("absorbed", [])):
        retired["absorbed"] = sorted(absorbed)
        _atomic_json(retired_path, retired)
    totals = {key: int(retired.get(key, 0)) for key in _LOSS_KEYS}
    depth = processes = 0
    for name in names:
        m = re.fullmatch(r"(\d+)-(\d+)\.json", name)
        if not m:
            continue
        path = os.path.join(directory, name)
        try:
            if name in absorbed:
                os.unlink(path)  # already in retired totals; only the deletion was left
                continue
            with open(path) as fh:
                data = json.load(fh)
            pid, start = int(m.group(1)), int(m.group(2))
            if pid <= 0 or data.get("pid") != pid or data.get("start") != start:
                continue
            values = {key: max(0, int(data.get(key, 0))) for key in _LOSS_KEYS}
            if _pid_alive(pid, start):
                processes += 1
                depth += max(0, int(data.get("queue_depth", 0)))
            else:
                for key in _LOSS_KEYS:
                    retired[key] = int(retired.get(key, 0)) + values[key]
                absorbed.add(name)
                retired["absorbed"] = sorted(absorbed)
                _atomic_json(retired_path, retired)
                try:
                    os.unlink(path)
                except OSError:
                    pass  # absorbed: deleted on a later pass; still counted below (no dip)
            for key in _LOSS_KEYS:
                totals[key] += values[key]
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return totals, depth, processes


def _metrics_text():
    _read_ledger()
    s, depth, processes = _process_totals()
    ledger = _ledger_counts
    lines = [
        "# HELP ai_ops_hermes_info Hermes AI ops observer schema and definition versions.",
        "# TYPE ai_ops_hermes_info gauge",
        f'ai_ops_hermes_info{{schema_version="{SCHEMA}",definition_version="{DEFINITION}"}} 1',
        "# HELP ai_ops_hermes_events_total Ledger events by event type (counted from retained ledger files, all processes).",
        "# TYPE ai_ops_hermes_events_total counter",
    ]
    for t in ("run.started", "run.finished", "approval.requested", "approval.resolved"):
        lines.append(f'ai_ops_hermes_events_total{{event_type="{t}"}} {ledger["by_type"].get(t, 0)}')
    lines += ["# HELP ai_ops_hermes_lost_events_total Events not written by reason (all processes, including exited ones).",
              "# TYPE ai_ops_hermes_lost_events_total counter"]
    for reason, key in (("queue_full", "dropped_queue_full"), ("file_full", "dropped_file_full"),
                        ("write_error", "write_errors")):
        lines.append(f'ai_ops_hermes_lost_events_total{{reason="{reason}"}} {s[key]}')
    lines += ["# HELP ai_ops_hermes_turns_finished_total Finished turns by status (counted from retained ledger files, all processes).",
              "# TYPE ai_ops_hermes_turns_finished_total counter"]
    for st in ("succeeded", "failed", "interrupted", "unknown"):
        lines.append(f'ai_ops_hermes_turns_finished_total{{status="{st}"}} {ledger["run_finished"].get(st, 0)}')
    lines += ["# HELP ai_ops_hermes_api_request_errors_total Provider API request errors (all processes, including exited ones).",
              "# TYPE ai_ops_hermes_api_request_errors_total counter",
              f"ai_ops_hermes_api_request_errors_total {s['api_request_errors']}",
              "# HELP ai_ops_hermes_queue_depth Events waiting in the bounded queue.",
              "# TYPE ai_ops_hermes_queue_depth gauge",
              f"ai_ops_hermes_queue_depth {depth}",
              "# HELP ai_ops_hermes_processes Live processes reporting statistics.",
              "# TYPE ai_ops_hermes_processes gauge",
              f"ai_ops_hermes_processes {processes}",
              "# HELP ai_ops_hermes_ledger_malformed_lines_total Malformed ledger lines.",
              "# TYPE ai_ops_hermes_ledger_malformed_lines_total counter",
              f"ai_ops_hermes_ledger_malformed_lines_total {ledger['malformed']}"]
    if ledger["last_emit"]:
        lines += ["# TYPE ai_ops_hermes_last_emit_timestamp_seconds gauge",
                  f"ai_ops_hermes_last_emit_timestamp_seconds {int(ledger['last_emit'])}"]
    lines += ["# TYPE ai_ops_hermes_metrics_generated_timestamp_seconds gauge",
              f"ai_ops_hermes_metrics_generated_timestamp_seconds {int(time.time())}"]
    return "\n".join(lines) + "\n"


def _metrics_owner():
    """Only one process aggregates and exports metrics.prom."""
    global _metrics_lock
    if _metrics_lock is None:
        os.makedirs(_DIR, exist_ok=True)
        fd = os.open(os.path.join(_DIR, "metrics.lock"), os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        _metrics_lock = fd
    return True


def _write_metrics():
    _write_proc_stats()
    if not _metrics_owner():
        return
    path = os.path.join(_DIR, "metrics.prom")
    os.makedirs(_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(_metrics_text())
    os.replace(tmp, path)
    _prune()


def _prune():
    """Remove expired day files, only by the metrics owner and only once every complete line
    is counted in the persisted ledger state (same inode, offset at end of file). A file that
    was never counted (owner down, first start) is kept until a later pass has read it."""
    cutoff = (_now() - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")
    d = os.path.join(_DIR, "ledger")
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith("events-") and name.endswith(".jsonl") and name[7:17] < cutoff:
            path = os.path.join(d, name)
            try:
                stat = os.stat(path)
                counted = _ledger_offsets.get(name)
                if not counted or counted[0] != stat.st_ino or counted[1] != stat.st_size:
                    continue
                os.remove(path)
            except OSError:
                pass


def _drain(timeout):
    try:
        e = _queue.get(timeout=timeout)
    except queue.Empty:
        return False
    try:
        _write(e)
    except Exception:
        _count("write_errors")
    return True


def _run():
    last = 0.0
    while True:
        _drain(5.0)
        try:
            _write_proc_stats()
            if time.time() - last >= 60:
                last = time.time()
                _write_metrics()  # the owner prunes after counting
        except Exception:
            _log.debug("ai ops metrics write failed", exc_info=True)


def _flush_at_exit(deadline_seconds=2.0):
    """On interpreter exit, write what is still queued and save this process's loss counters,
    so losses after the last periodic save are retired instead of lost. The whole flush
    (lock waits and storage I/O) runs in a daemon thread bounded by the deadline, so a stuck
    worker or storage never blocks agent exit. Events not written in time count as
    queue_full losses; if even the final save cannot finish, the last periodic save remains."""
    end = time.monotonic() + deadline_seconds
    t = threading.Thread(target=_flush_body, args=(end,), name="boxp-ai-ops-exit", daemon=True)
    t.start()
    t.join(max(0.0, end - time.monotonic()) + 0.5)


def _flush_body(end):
    try:
        while time.monotonic() < end:
            if not _write_lock.acquire(timeout=max(0.0, end - time.monotonic())):
                break  # the worker is stuck holding the lock: give up writing
            try:
                try:
                    e = _queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    _write_unlocked(e)
                except Exception:
                    _count("write_errors")
            finally:
                _write_lock.release()
        while True:
            try:
                _queue.get_nowait()
            except queue.Empty:
                break
            _count("dropped_queue_full")
        _write_proc_stats(final=True, timeout=max(0.0, end - time.monotonic()))
    except Exception:
        _log.debug("ai ops exit flush failed", exc_info=True)


def _start_worker():
    global _worker
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_run, name="boxp-ai-ops-observer", daemon=True)
            _worker.start()
            atexit.register(_flush_at_exit)
