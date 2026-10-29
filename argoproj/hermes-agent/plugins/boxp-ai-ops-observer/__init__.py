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
day-file cap and write errors are counted and exported in metrics.prom.
"""
import fcntl
import hashlib
import json
import logging
import os
import queue
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
_queue = queue.Queue(maxsize=_QUEUE_MAX)
_turns = OrderedDict()  # (session hash, turn hash) -> run_id
_stats = {"emitted": 0, "dropped_queue_full": 0, "dropped_file_full": 0, "write_errors": 0,
          "api_request_errors": 0, "by_type": {}, "run_finished": {}, "last_emit": None}
_worker = None
_salt = None
_metrics_lock = None  # open fd holding the metrics writer flock


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
    with _lock:
        _stats["emitted"] += 1
        _stats["by_type"][e["event_type"]] = _stats["by_type"].get(e["event_type"], 0) + 1
        if e["event_type"] == "run.finished":
            st = e.get("status", "unknown")
            _stats["run_finished"][st] = _stats["run_finished"].get(st, 0) + 1
        _stats["last_emit"] = time.time()


def _metrics_text():
    with _lock:
        s = json.loads(json.dumps(_stats))
    lines = [
        "# HELP ai_ops_hermes_info Hermes AI ops observer schema and definition versions.",
        "# TYPE ai_ops_hermes_info gauge",
        f'ai_ops_hermes_info{{schema_version="{SCHEMA}",definition_version="{DEFINITION}"}} 1',
        "# HELP ai_ops_hermes_events_total Ledger events written by event type (since plugin start).",
        "# TYPE ai_ops_hermes_events_total counter",
    ]
    for t in ("run.started", "run.finished", "approval.requested", "approval.resolved"):
        lines.append(f'ai_ops_hermes_events_total{{event_type="{t}"}} {s["by_type"].get(t, 0)}')
    lines += ["# HELP ai_ops_hermes_lost_events_total Events not written by reason (since plugin start).",
              "# TYPE ai_ops_hermes_lost_events_total counter"]
    for reason, key in (("queue_full", "dropped_queue_full"), ("file_full", "dropped_file_full"),
                        ("write_error", "write_errors")):
        lines.append(f'ai_ops_hermes_lost_events_total{{reason="{reason}"}} {s[key]}')
    lines += ["# HELP ai_ops_hermes_turns_finished_total Finished turns by status (since plugin start).",
              "# TYPE ai_ops_hermes_turns_finished_total counter"]
    for st in ("succeeded", "failed", "interrupted", "unknown"):
        lines.append(f'ai_ops_hermes_turns_finished_total{{status="{st}"}} {s["run_finished"].get(st, 0)}')
    lines += ["# HELP ai_ops_hermes_api_request_errors_total Provider API request errors (since plugin start).",
              "# TYPE ai_ops_hermes_api_request_errors_total counter",
              f"ai_ops_hermes_api_request_errors_total {s['api_request_errors']}",
              "# HELP ai_ops_hermes_queue_depth Events waiting in the bounded queue.",
              "# TYPE ai_ops_hermes_queue_depth gauge",
              f"ai_ops_hermes_queue_depth {_queue.qsize()}"]
    if s["last_emit"]:
        lines += ["# TYPE ai_ops_hermes_last_emit_timestamp_seconds gauge",
                  f"ai_ops_hermes_last_emit_timestamp_seconds {int(s['last_emit'])}"]
    lines += ["# TYPE ai_ops_hermes_metrics_generated_timestamp_seconds gauge",
              f"ai_ops_hermes_metrics_generated_timestamp_seconds {int(time.time())}"]
    return "\n".join(lines) + "\n"


def _metrics_owner():
    """Only one process exports metrics.prom: the counters are per process, so several
    Hermes processes loading the plugin would otherwise overwrite each other's values.
    Every process still appends to the ledger."""
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
    if not _metrics_owner():
        return
    path = os.path.join(_DIR, "metrics.prom")
    os.makedirs(_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(_metrics_text())
    os.replace(tmp, path)


def _prune():
    cutoff = (_now() - timedelta(days=_RETENTION_DAYS)).strftime("%Y-%m-%d")
    d = os.path.join(_DIR, "ledger")
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith("events-") and name.endswith(".jsonl") and name[7:17] < cutoff:
            try:
                os.remove(os.path.join(d, name))
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
        if time.time() - last >= 60:
            last = time.time()
            try:
                _prune()
                _write_metrics()
            except Exception:
                _log.debug("ai ops metrics write failed", exc_info=True)


def _start_worker():
    global _worker
    with _lock:
        if _worker is None:
            _worker = threading.Thread(target=_run, name="boxp-ai-ops-observer", daemon=True)
            _worker.start()
