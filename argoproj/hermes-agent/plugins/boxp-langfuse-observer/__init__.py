"""Hermes -> in-cluster Langfuse observer (BOXP-428).

Scope: only delegation paths the Owner selected. A turn is buffered in memory and sent when it
ends, and only if it is a Kohako G2 session, a webhook (Task Board event) session, or it delegated,
was delegated (subagent_start, not the parent id, which session rotation also sets) or referred to a
ticket. Other conversation is dropped and only counted. Audio and images never
reach this module: no transcription hook is registered and data URIs are removed.

Secrets: arguments and output of tool calls on sensitive paths and commands are withheld, every string is masked, and the
serialized batch is scanned again before sending. Any hit or error drops the observation and keeps
a reason without body. Nothing is written to disk, so there is no spool to clean. Request history
goes through an allow-list that never keeps tool results; those appear only in tool spans. After a
sensitive tool ran, later bodies of that session and its delegating parent are withheld too, because
the model has seen the output and can repeat a value no pattern recognises.

Key: read from an optional Secret volume, polled by the worker. It can appear or rotate while the
Pod runs. Until then the hooks return at once and nothing is buffered.

Failure: hooks only touch memory and never raise. A daemon worker sends with short timeouts, at
most two attempts per batch, and counts every loss. Langfuse down means missing traces only.
"""
import base64
import collections
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)

VERSION = "0.1.0"
_URL = os.environ.get("LANGFUSE_URL", "http://langfuse-web.langfuse.svc.cluster.local:3000")
_KEY_DIR = os.environ.get("LANGFUSE_INGEST_KEY_DIR", "/etc/langfuse-ingest")
_KEY_POLL_SECONDS = 30
_MAX_FIELD = int(os.environ.get("LANGFUSE_MAX_FIELD_BYTES", str(32 * 1024)))
_MAX_TURN_SPANS = 300
_MAX_TURN_BYTES = 4 * 1024 * 1024
_MAX_TURNS = 32
_MAX_SESSIONS = 1024
_QUEUE_MAX = 64
_BATCH_BYTES = 2 * 1024 * 1024
_TIMEOUT = 5
_LAST_MESSAGES = 2
_G2_PREFIX = "kohako-g2-"
_WEBHOOK_PLATFORMS = {"webhook"}
_TICKET = re.compile(r"\bBOXP-\d{1,6}\b")
_DELEGATING_TOOLS = {"delegate_task"}
_CHAT_ROLES = {"system", "developer", "user", "assistant"}
_TEXT_BLOCKS = {"text", "input_text", "output_text"}
_WITHHELD_TOOL = "[withheld: sensitive path or command]"
_WITHHELD_SESSION = "[withheld: session ran a sensitive tool]"

# A value can sit inside serialized text at any depth, so a quote can carry escapes. A value ends at
# a quote of its own depth, and one that never closes runs to the end of the line.
_QUOTE = r"""(?:\\*["']|\\+u00(?:22|27)|&quot;|&\#0*(?:34|39);|&\#x0*(?:22|27);)"""
_GAP = r"""(?:\s|\\+[nrt])*"""
_SEPARATOR = r"""(?:[:=]|\\+u00(?:3[aA]|3[dD]))"""
_SECRET_WORD = (
    r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret"
    r"|credential|cookie|authorization|(?<![a-z])pass(?![a-z])|(?<![a-z])auth(?![a-z]))")
# Bounded so that a long word without a secret name stays linear to scan.
_SECRET_KEY = r"[\w.-]{0,64}" + _SECRET_WORD + r"[\w.-]{0,64}"
_ENTRY_GAP = r"""(?:\s|\\+[nrt]|,)*"""


def _value(group, limit="", bare=r"[^\s,;}\]]+"):
    # limit bounds a value that has to be followed by more of the pattern.
    scan = limit or "*?"
    return (r"(?:(?P<" + group + ">" + _QUOTE + r")(?:[^\n]" + scan + r"(?<!\\)(?P=" + group + r")"
            + ("" if limit else r"|[^\n]*") + r")|" + bare + ")")


def _entry_key(name):
    return r"(?<![\w-])" + name + _QUOTE + r"?" + _GAP + r"[:=]" + _GAP



_SECRET_NAME = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|cookie|auth)")
_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?(?:-----END (?:[A-Z ]+ )?PRIVATE KEY-----|\Z)", re.S)),
    ("header", re.compile(r"(?im)\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*[^\r\n]+")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
    ("provider_key", re.compile(r"\b(?:sk-lf-|pk-lf-|sk-|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|xox[baprs]-|npm_|glpat-)[A-Za-z0-9_-]{8,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("telegram", re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}=*")),
    # {"name": "DB_PASSWORD", "value": "..."} and its YAML form, as a pod spec prints an env entry.
    ("env_entry", re.compile(
        r"(?i)" + _entry_key("name") + _QUOTE + r"?" + _SECRET_KEY + _QUOTE + r"?" + _ENTRY_GAP + _QUOTE + r"?"
        + _entry_key("value") + _value("quote"))),
    ("env_entry", re.compile(
        r"(?i)" + _entry_key("value") + _value("quote", r"{0,4096}?") + _ENTRY_GAP + _QUOTE + r"?"
        + _entry_key("name") + _QUOTE + r"?" + _SECRET_KEY)),
    ("flag", re.compile(r"(?i)(?<![\w-])--?" + _SECRET_KEY + r"[ \t]+(?!-)" + _value("quote", bare=r"\S+"))),
    ("cli_password", re.compile(
        r"(?<![\w-])(?:-u|--user)[ \t=]*(?!\d+:\d+\b)[^\s:]+:\S+"
        r"|\b(?:mysql\w*|mariadb\w*)\b[^\n|;&]{0,512}?\s-p\S+"
        r"|\bsshpass\b[^\n|;&]{0,512}?\s-p[ \t]*\S+")),
    ("assignment", re.compile(
        r"(?i)[\"']?" + _SECRET_KEY + _QUOTE + r"?" + _GAP + _SEPARATOR + _GAP
        # A YAML block scalar carries its value on the indented lines below the key.
        + r"(?:[|>][-+]?\d?(?:(?:\n|\\+n)[ \t]+(?:(?!\\+n)[^\n])*)+|" + _value("quote") + r")")),
    ("data_uri", re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.I)),
    ("entropy", re.compile(r"(?<![A-Za-z0-9_/+=-])[A-Za-z0-9_/+=-]{40,}(?![A-Za-z0-9_/+=-])")),
]
_FIELD_ONLY = {"assignment", "entropy", "data_uri", "env_entry", "flag", "cli_password"}
_PLACEHOLDER = re.compile(r"\[REDACTED:[a-z_]+\]")
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|[/\\\s\"'=])(?:auth\.json|\.credentials[^/\s\"']*|\.env(?:\.[\w-]+)?|\.ssh(?:[/\\]|\b)|\.aws(?:[/\\]|\b)|kubeconfig|\.git-credentials|\.netrc|id_rsa|id_ed25519|secrets?(?:[/\\])|[^/\s\"']*token[^/\s\"']*)")
# Arguments are matched in their JSON form, where a newline is a backslash and an n.
_WORD_START = r"""(?:^|[;&|\s"'(`/]|\\[nrt])"""
_WORD_END = r"""[ \t]*(?:$|[|;&<>"')`#]|\d+[<>]|\\[nrt"])"""
_ASSIGNMENT = r"""[ \t]+[A-Za-z_]\w*=(?:\\?"[^"]*"|'[^']*'|[^\s;&|)`"'\\]*)"""
# `env NAME=value` with no command to run, or `env` with an option, prints the environment.
_ENV_DUMP = _WORD_START + r"(?:env(?:[ \t]+-|(?:" + _ASSIGNMENT + r")*" + _WORD_END + r")|(?:set|export)" + _WORD_END + r")"
_SENSITIVE_COMMAND = re.compile(
    r"(?i)" + _ENV_DUMP + r"|\bprintenv\b|\bexport\s+-p\b|\b(?:declare|typeset)(?:\s+-[a-z]*[px]|" + _WORD_END + r")"
    r"|\benviron\b|\bprocess\.env\b|\bgetenv\b|(?-i:[%$]ENV\b|\bENV(?:\.|\[))"
    r"|\baws\b[^|;&]*\b(?:ssm\s+get-parameter|secretsmanager\s+get-|configure\s+(?:get|export-credentials))"
    r"|\bhelm\s+get\b|\bgh\s+auth\s+token\b|\bgit\s+credential\b|langfuse-ingest")
_CLUSTER_CLI = re.compile(r"(?i)\b(?:kubectl|oc)\s")
_ENV_OUTPUT = [re.compile(r"(?:^|[^A-Za-z0-9_]|\\[nrt]|\\u0000)" + name + "=") for name in ("PATH", "HOME")]

_lock = threading.Lock()
_turns = {}
_sessions = collections.OrderedDict()  # session id -> ran a sensitive tool
_delegated = collections.OrderedDict()  # session ids started by delegation
_seen_prompts = {}
_stats = {}
_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_secret_values = None
_creds = None


def _count(key, n=1):
    with _lock:
        _stats[key] = _stats.get(key, 0) + n


def stats():
    with _lock:
        return dict(_stats)


def _read_credentials():
    try:
        values = []
        for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            with open(os.path.join(_KEY_DIR, name), encoding="utf-8") as fh:
                values.append(fh.read().strip())
    except (OSError, ValueError):
        return None
    return tuple(values) if all(values) else None


def _refresh_credentials():
    global _creds
    _creds = _read_credentials()
    return _creds


def register(ctx):
    if not _refresh_credentials():
        _log.info("boxp-langfuse-observer idle until the ingest key appears")
    ctx.register_hook("pre_llm_call", _on_turn_start)
    ctx.register_hook("pre_api_request", _on_api_request)
    ctx.register_hook("post_api_request", _on_api_response)
    ctx.register_hook("api_request_error", _on_api_error)
    ctx.register_hook("post_tool_call", _on_tool)
    ctx.register_hook("post_compaction", _on_compaction)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent)
    ctx.register_hook("post_llm_call", _on_turn_end)
    ctx.register_hook("on_session_end", _on_session_end)
    _start_worker()


# ---------------------------------------------------------------------------
# secret removal

def _env_secrets():
    global _secret_values
    if _secret_values is None:
        _secret_values = sorted(
            {v for k, v in os.environ.items() if _SECRET_NAME.search(k) and isinstance(v, str) and len(v) >= 8},
            key=len, reverse=True)
    return _secret_values


def _is_entropy(s):
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", s) or "/" in s.strip("/") and s.count("/") >= 2:
        return False
    classes = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes == 3


def redact(text):
    """Return text without secrets. Raises nothing: failure drops the whole field."""
    try:
        out = str(text)
        for value in _env_secrets():
            if value in out:
                out = out.replace(value, "[REDACTED:env]")
                _count("redactions_env")
        for kind, pattern in _PATTERNS:
            def sub(m, kind=kind):
                if kind == "entropy" and not _is_entropy(m.group(0)):
                    return m.group(0)
                _count("redactions_" + kind)
                return f"[REDACTED:{kind}]"
            out = pattern.sub(sub, out)
        return out
    except Exception:
        _count("redaction_errors")
        return "[dropped: redaction error]"


def _unsafe(serialized):
    """Final guard on the JSON text that is about to leave the process."""
    probe = _PLACEHOLDER.sub("", serialized)
    if any(v in probe for v in _env_secrets()):
        return True
    for kind, pattern in _PATTERNS:
        if kind in _FIELD_ONLY:
            continue  # JSON-escaped forms; these were applied per field
        if pattern.search(probe):
            return True
    return False


def _withhold(args_text):
    # A cluster CLI can name the resource after any flags, or through a variable, so word order is not checked.
    if "secret" in args_text.lower() and _CLUSTER_CLI.search(args_text):
        return True
    return bool(_SENSITIVE_PATH.search(args_text) or _SENSITIVE_COMMAND.search(args_text))


def _is_env_dump(result_text):
    # The command list cannot name every way to print the environment, so the output is checked too.
    return all(pattern.search(result_text) for pattern in _ENV_OUTPUT)


def _text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _field(value):
    out = redact(_text(value))
    raw = out.encode("utf-8", "ignore")
    if len(raw) > _MAX_FIELD:
        out = raw[:_MAX_FIELD].decode("utf-8", "ignore") + f"…[truncated {len(raw) - _MAX_FIELD} bytes]"
    return out


# ---------------------------------------------------------------------------
# turn buffer. Hooks only store references here; redaction runs in the worker.

def _key(session_id, turn_id):
    return (str(session_id or ""), str(turn_id or ""))


def _turn(session_id, turn_id):
    key = _key(session_id, turn_id)
    with _lock:
        turn = _turns.get(key)
        if turn is None:
            if len(_turns) >= _MAX_TURNS:
                oldest = min(_turns, key=lambda k: _turns[k]["start"])
                del _turns[oldest]
                _stats["turns_evicted"] = _stats.get("turns_evicted", 0) + 1
            turn = {"start": time.time(), "spans": [], "bytes": 0, "tickets": set(), "delegated": False,
                    "user_message": None, "model": None, "platform": "", "parent": None, "output": None,
                    "truncated": False, "session": str(session_id or ""), "turn": str(turn_id or "")}
            _turns[key] = turn
        return turn


def _add(turn, span):
    size = sum(len(_text(v)) for v in span.values())
    with _lock:
        if len(turn["spans"]) >= _MAX_TURN_SPANS or turn["bytes"] + size > _MAX_TURN_BYTES:
            turn["truncated"] = True
            _stats["spans_dropped_turn_limit"] = _stats.get("spans_dropped_turn_limit", 0) + 1
            return
        turn["spans"].append(span)
        turn["bytes"] += size


def _tainted(session):
    with _lock:
        return _sessions.get(str(session or ""), False)


def _taint(*sessions):
    with _lock:
        for session in sessions:
            if session:
                _sessions[str(session)] = True
                _sessions.move_to_end(str(session))
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)


def _tool_args(message):
    """Tool arguments in any provider shape, used to find sensitive calls in resumed history."""
    if not isinstance(message, dict):
        return
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            yield (call.get("function") or {}).get("arguments") if isinstance(call.get("function"), dict) else call
    if message.get("type") in ("function_call", "custom_tool_call"):
        yield message.get("arguments") or message.get("input")
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("input")


def _note_delegated(session):
    with _lock:
        _delegated[str(session)] = True
        _delegated.move_to_end(str(session))
        while len(_delegated) > _MAX_SESSIONS:
            _delegated.popitem(last=False)


def _note_session(session, messages):
    """A session first seen here may be resumed, so the taint is rebuilt from its history once."""
    with _lock:
        if session in _sessions:
            return
        _sessions[session] = False
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    if any(_withhold(_text(a)) for m in messages for a in _tool_args(m)):
        _taint(session)


def _message_view(message):
    """Allow-list: role, plain text and tool names. Tool results and any unknown shape stay out."""
    if not isinstance(message, dict) or message.get("role") not in _CHAT_ROLES:
        kind = (message.get("role") or message.get("type")) if isinstance(message, dict) else type(message).__name__
        return {"omitted": str(kind)}
    content = message.get("content")
    if isinstance(content, list):
        content = [b["text"] if isinstance(b, dict) and b.get("type") in _TEXT_BLOCKS and isinstance(b.get("text"), str)
                   else {"omitted": str(b.get("type")) if isinstance(b, dict) else type(b).__name__} for b in content]
    elif content is not None and not isinstance(content, str):
        content = {"omitted": type(content).__name__}
    view = {"role": message["role"], "content": content}
    calls = [(c.get("function") or {}).get("name") if isinstance(c.get("function"), dict) else c.get("name")
             for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    if calls:
        view["tool_calls"] = calls
    return view


def _note_tickets(turn, value):
    if value:
        found = _TICKET.findall(_text(value)[:_MAX_TURN_BYTES])
        if found:
            with _lock:
                turn["tickets"].update(found)


def _guarded(fn):
    def wrapper(*args, **kwargs):
        if _creds is None:
            return None
        try:
            fn(*args, **kwargs)
        except Exception:
            _count("hook_errors")
        return None
    wrapper.__signature__ = __import__("inspect").signature(fn)
    wrapper.__name__ = fn.__name__
    return wrapper


@_guarded
def _on_turn_start(session_id=None, turn_id=None, user_message=None, model=None, platform=None, parent_session_id=None):
    turn = _turn(session_id, turn_id)
    _note_tickets(turn, user_message)
    if str(parent_session_id or "") in _delegated:  # rotation of a delegated session keeps it delegated
        _note_delegated(turn["session"])
    if _tainted(parent_session_id):  # the delegated goal may repeat what the parent saw
        _taint(turn["session"])
    if _tainted(turn["session"]):  # the user can repeat a value no pattern recognises
        user_message = _WITHHELD_SESSION
    turn.update(user_message=user_message, model=model, platform=platform or "", parent=parent_session_id)


@_guarded
def _on_api_request(session_id=None, turn_id=None, api_request_id=None, system_prompt=None, request_messages=None,
                    message_count=None, retry_count=None, started_at=None):
    turn = _turn(session_id, turn_id)
    messages = list(request_messages or [])
    _note_session(turn["session"], messages)
    tainted = _tainted(turn["session"])
    if tainted and turn["user_message"] is not None:  # the taint of a resumed session is only known here
        turn["user_message"] = _WITHHELD_SESSION
    tail = _WITHHELD_SESSION if tainted else [_message_view(m) for m in messages[-_LAST_MESSAGES:]]
    with _lock:
        turn.setdefault("requests", {})[str(api_request_id)] = {
            "input": tail, "message_count": message_count, "retry_count": retry_count, "started_at": started_at}
    if system_prompt:
        digest = hashlib.sha256(_text(system_prompt).encode("utf-8", "ignore")).hexdigest()
        with _lock:
            if turn.get("system_digest") == digest:
                return
            turn["system_digest"] = digest
        _add(turn, {"key": "system:" + digest[:16], "name": "system-prompt", "type": "span",
                    "input": _WITHHELD_SESSION if tainted else system_prompt, "start": started_at or time.time(), "end": started_at or time.time(),
                    "metadata": {"source": "hermes_assembled"}})


@_guarded
def _on_api_response(session_id=None, turn_id=None, api_request_id=None, response=None, usage=None, model=None,
                     response_model=None, provider=None, base_url=None, api_call_count=None, finish_reason=None,
                     started_at=None, ended_at=None):
    turn = _turn(session_id, turn_id)
    with _lock:
        req = turn.get("requests", {}).pop(str(api_request_id), {})
    message = (response or {}).get("assistant_message") if isinstance(response, dict) else None
    output = _WITHHELD_SESSION if _tainted(turn["session"]) else _message_view({"role": "assistant", **message}) if isinstance(message, dict) else None
    location = "local" if "local-llm" in str(base_url or "") or "llama" in str(base_url or "") else "cloud"
    _add(turn, {"key": "api:" + str(api_request_id), "name": "llm-call", "type": "generation",
                "input": req.get("input"), "output": output, "model": response_model or model, "usage": usage,
                "start": started_at or req.get("started_at"), "end": ended_at,
                "metadata": {"input_scope": f"last {_LAST_MESSAGES} of {req.get('message_count')} messages",
                             "provider": provider, "location": location, "api_call": api_call_count,
                             "retry": req.get("retry_count"), "finish_reason": finish_reason}})
    _note_tickets(turn, message)


@_guarded
def _on_api_error(session_id=None, turn_id=None, api_request_id=None, status_code=None, retry_count=None,
                  retryable=None, reason=None):
    turn = _turn(session_id, turn_id)
    now = time.time()
    _add(turn, {"key": f"apierr:{api_request_id}:{retry_count}", "name": "llm-call-error", "type": "span", "level": "ERROR",
                "start": now, "end": now,
                "metadata": {"status_code": status_code, "retry": retry_count, "retryable": retryable, "reason": reason}})


@_guarded
def _on_tool(session_id=None, turn_id=None, tool_call_id=None, tool_name=None, args=None, result=None, duration_ms=None,
             status=None, error_type=None):
    turn = _turn(session_id, turn_id)
    if tool_name in _DELEGATING_TOOLS:
        turn["delegated"] = True
    _note_tickets(turn, args)
    _note_tickets(turn, result)
    args_text = _text(args)
    already = _tainted(turn["session"])
    withheld = _withhold(args_text) or _is_env_dump(_text(result))
    if withheld:
        _taint(turn["session"], turn["parent"])
    now = time.time()
    _add(turn, {"key": "tool:" + str(tool_call_id or now), "name": str(tool_name), "type": "tool",
                "input": _WITHHELD_TOOL if withheld else _WITHHELD_SESSION if already else args,
                "output": _WITHHELD_TOOL if withheld else _WITHHELD_SESSION if already else result,
                "start": now - (duration_ms or 0) / 1000, "end": now,
                "level": "ERROR" if error_type or status in ("error", "failed") else None,
                "metadata": {"status": status, "error_type": error_type, "withheld": withheld or already}})


@_guarded
def _on_compaction(session_id=None, turn_id=None, before_tokens=None, after_tokens=None, messages_removed=None):
    turn = _turn(session_id, turn_id)
    now = time.time()
    _add(turn, {"key": f"compaction:{now}", "name": "compaction", "type": "span", "start": now, "end": now,
                "metadata": {"before_tokens": before_tokens, "after_tokens": after_tokens, "messages_removed": messages_removed}})


@_guarded
def _on_subagent_start(parent_session_id=None, parent_turn_id=None, parent_subagent_id=None, child_session_id=None,
                       child_subagent_id=None, child_role=None, child_goal=None):
    if child_session_id:
        _note_delegated(child_session_id)


@_guarded
def _on_subagent(parent_session_id=None, parent_turn_id=None, child_session_id=None, child_role=None, child_summary=None,
                 child_status=None, duration_ms=None):
    turn = _turn(parent_session_id, parent_turn_id)
    turn["delegated"] = True
    _note_tickets(turn, child_summary)
    if _tainted(child_session_id):
        _taint(turn["session"], turn["parent"])
    if _tainted(turn["session"]):
        child_summary = _WITHHELD_SESSION
    now = time.time()
    _add(turn, {"key": "subagent:" + str(child_session_id), "name": "subagent", "type": "agent",
                "output": child_summary, "start": now - (duration_ms or 0) / 1000, "end": now,
                "metadata": {"child_session_id": child_session_id, "child_role": child_role, "status": child_status}})


@_guarded
def _on_turn_end(session_id=None, turn_id=None, assistant_response=None):
    turn = _turn(session_id, turn_id)
    _note_tickets(turn, assistant_response)
    turn["output"] = _WITHHELD_SESSION if _tainted(turn["session"]) else assistant_response


@_guarded
def _on_session_end(session_id=None, turn_id=None, completed=None, failed=None, interrupted=None, turn_exit_reason=None):
    with _lock:
        turn = _turns.pop(_key(session_id, turn_id), None)
    if turn is None:
        return
    turn["status"] = "interrupted" if interrupted else "failed" if failed else "succeeded" if completed else "unknown"
    turn["exit_reason"] = turn_exit_reason
    turn["tainted"] = _tainted(turn["session"])
    if not in_scope(turn):
        _count("turns_out_of_scope")
        return
    try:
        _queue.put_nowait(turn)
    except queue.Full:
        _count("turns_dropped_queue_full")


def in_scope(turn):
    return (turn["session"].startswith(_G2_PREFIX) or turn["platform"] in _WEBHOOK_PLATFORMS
            or turn["delegated"] or turn["session"] in _delegated or bool(turn["tickets"]))


# ---------------------------------------------------------------------------
# OTLP

def _attr(key, value):
    if isinstance(value, (list, tuple)):
        return {"key": key, "value": {"arrayValue": {"values": [{"stringValue": str(v)} for v in value]}}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _ns(ts):
    return str(int(float(ts) * 1_000_000_000))


def _id(trace, key):
    return hashlib.sha256(f"{trace}:{key}".encode()).hexdigest()[:16]


def _usage(usage):
    """Exclusive buckets: Hermes reports input/output plus cache and reasoning subsets when known."""
    if not isinstance(usage, dict):
        return None
    get = lambda *names: next((int(usage[n]) for n in names if isinstance(usage.get(n), (int, float))), 0)
    cached = get("cache_read_tokens", "cached_tokens", "cache_read_input_tokens")
    written = get("cache_write_tokens", "cache_creation_input_tokens")
    reasoning = get("reasoning_tokens")
    raw_input = get("input_tokens", "prompt_tokens")
    raw_output = get("output_tokens", "completion_tokens")
    details = {"input": max(raw_input - cached - written, 0), "output": max(raw_output - reasoning, 0)}
    for name, value in (("input_cached_tokens", cached), ("input_cache_creation", written), ("output_reasoning_tokens", reasoning)):
        if value:
            details[name] = value
    return details if raw_input or raw_output else None


def build_spans(turn):
    trace = hashlib.sha256(f"hermes:{turn['session']}:{turn['turn']}".encode()).hexdigest()[:32]
    root_id = _id(trace, "root")
    g2 = turn["session"].startswith(_G2_PREFIX)
    tags = ["hermes"] + (["g2"] if g2 else []) + [f"platform:{turn['platform'] or 'unknown'}"]
    tags += [f"ticket:{t}" for t in sorted(turn["tickets"])]
    trace_name = "g2-delegation" if g2 else "hermes-turn"
    common = [
        _attr("langfuse.session.id", _field(turn["session"])),
        _attr("langfuse.trace.name", trace_name),
        _attr("langfuse.trace.tags", tags),
        _attr("langfuse.release", VERSION),
        _attr("langfuse.trace.metadata.status", turn.get("status")),
        _attr("langfuse.trace.metadata.coverage.reasoning", "not_collected"),
        _attr("langfuse.trace.metadata.coverage.provider_payload", "system prompt and last messages of each attempt"),
        _attr("langfuse.trace.metadata.coverage.g2_realtime", "delegated text only; realtime voice session not observed"),
        _attr("langfuse.trace.metadata.coverage.truncated", str(turn["truncated"]).lower()),
        _attr("langfuse.trace.metadata.coverage.withheld_after_sensitive_tool", str(bool(turn.get("tainted"))).lower()),
    ]
    end = time.time()
    spans = [{
        "traceId": trace, "spanId": root_id, "name": trace_name, "kind": 1,
        "startTimeUnixNano": _ns(turn["start"]), "endTimeUnixNano": _ns(end),
        "attributes": common + [
            _attr("langfuse.observation.input", _field(turn["user_message"] or "")),
            _attr("langfuse.observation.output", _field(turn["output"] or "")),
            _attr("langfuse.observation.metadata.exit_reason", _field(turn.get("exit_reason") or "")),
            _attr("langfuse.observation.metadata.parent_session_id", _field(turn["parent"] or "")),
        ],
    }]
    for item in turn["spans"]:
        start = item.get("start") or turn["start"]
        attrs = common + [_attr("langfuse.observation.type", item["type"])]
        if item.get("input") is not None:
            attrs.append(_attr("langfuse.observation.input", _field(item["input"])))
        if item.get("output") is not None:
            attrs.append(_attr("langfuse.observation.output", _field(item["output"])))
        if item.get("model"):
            attrs.append(_attr("langfuse.observation.model.name", _field(item["model"])))
        details = _usage(item.get("usage"))
        if details:
            attrs.append(_attr("langfuse.observation.usage_details", json.dumps(details)))
        if item.get("level"):
            attrs.append(_attr("langfuse.observation.level", item["level"]))
        for key, value in (item.get("metadata") or {}).items():
            if value is not None:
                attrs.append(_attr(f"langfuse.observation.metadata.{key}", _field(value)))
        spans.append({"traceId": trace, "spanId": _id(trace, item["key"]), "parentSpanId": root_id,
                      "name": _field(item["name"])[:120], "kind": 1, "startTimeUnixNano": _ns(start),
                      "endTimeUnixNano": _ns(item.get("end") or start), "attributes": attrs})
    return spans


def _payload(spans):
    return json.dumps({"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "hermes-agent")]},
                                          "scopeSpans": [{"scope": {"name": "boxp-langfuse-observer", "version": VERSION},
                                                          "spans": spans}]}]}, ensure_ascii=False)


def batches(spans):
    current, size = [], 0
    for span in spans:
        span_size = len(json.dumps(span, ensure_ascii=False).encode("utf-8", "ignore"))
        if current and size + span_size > _BATCH_BYTES:
            yield current
            current, size = [], 0
        current.append(span)
        size += span_size
    if current:
        yield current


def guarded_payloads(spans):
    """Yield only payloads that pass the final guard; a failing span is dropped alone."""
    for batch in batches(spans):
        body = _payload(batch)
        if not _unsafe(body):
            yield body
            continue
        kept = [s for s in batch if not _unsafe(json.dumps(s, ensure_ascii=False))]
        _count("spans_dropped_secret_guard", len(batch) - len(kept))
        if kept:
            body = _payload(kept)
            if not _unsafe(body):
                yield body


def _post(body):
    if _creds is None:
        return False
    pk, sk = _creds
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    request = urllib.request.Request(
        _URL + "/api/public/otel/v1/traces", data=body.encode("utf-8"), method="POST",
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json", "x-langfuse-ingestion-version": "4"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return 200 <= response.status < 300


def _send(body):
    for attempt in range(2):
        try:
            if _post(body):
                _count("batches_sent")
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if attempt == 0:
            time.sleep(1)
    _count("batches_dropped")
    return False


def export_turn(turn):
    for body in guarded_payloads(build_spans(turn)):
        _send(body)
    _count("turns_exported")


def _work():
    while True:
        _refresh_credentials()
        try:
            turn = _queue.get(timeout=_KEY_POLL_SECONDS)
        except queue.Empty:
            continue
        try:
            export_turn(turn)
        except Exception:
            _count("export_errors")


def _start_worker():
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_work, name="boxp-langfuse-observer", daemon=True)
        _worker.start()
