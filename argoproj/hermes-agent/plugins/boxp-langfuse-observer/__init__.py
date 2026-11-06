"""Hermes -> in-cluster Langfuse observer (BOXP-428).

Scope: only delegation paths the Owner selected. A turn is buffered in memory and sent when it
ends, and only if it is a Kohako G2 session, a webhook (Task Board event) session, or it delegated,
was delegated (subagent_start, not the parent id, which session rotation also sets) or referred to a
ticket. Other conversation is dropped and only counted. Audio and images never
reach this module: no transcription hook is registered and data URIs are removed.

Bodies (BOXP-428 TD-05): prompts, replies, delegation and tool input/output are sent so the Owner can
read them. Every string is masked first, including the whole line around a secret word and the lines
next to it, and the serialized batch is scanned again before sending. Any hit or error drops the
observation and keeps a reason without body. Nothing is written to disk, so there is no spool to clean.

Withholding needs evidence, because the model can repeat what a tool showed it as a bare value:
- a sensitive source (credential paths, environment dumps, cluster secrets, ...): the call and its
  output are never sent, and later bodies of the session, its parent and its children are withheld;
- a secret value seen by the model (a known format, a secret-named assignment, element or entry, a
  value of this process's environment) in tool input/output or in request history: the field is
  masked and later bodies of the same sessions are withheld;
- a secret word next to a value in prose ("the password is X", "X is my password"): the line is masked and
  X-like tokens of that line and the next are masked in every later field of the same sessions, in every
  encoding. A secret word in an ordinary sentence ("パスワードを設定してください") is a mention, not evidence;
- output too large to scan (_SCAN_LIMIT), a scan error or too many such tokens: that body and later bodies
  are withheld as above.
The user request, model replies and child summaries are scanned the same way before they are kept, so a
value they name is masked where they repeat it; prose there pins values but does not withhold the reply.
History this process did not observe, as after a restart or a rewrite by compaction (tracked by message
content, not count), is scanned the same way instead of being
withheld outright. Residual: a value with no secret word, no known format and no sensitive source,
which the model repeats on its own, is sent. Langfuse is in-cluster, Owner-only and short-lived.
THREAT_MODEL.md records the Owner-approved contract: what is protected, what must stay readable and the
accepted residual (unmarked plain-text values, free phrasings and transformations not listed there).

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

VERSION = "0.2.0"
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
_PATH_ARGS = ("path", "file_path", "filepath", "filename")
_COMMAND_ARGS = ("command", "cmd")
_COMMAND_HEAD = re.compile(r"""(?:[A-Za-z_]\w*=\S*\s+)*(?:[^\s'"]*/)?([\w.+-]{1,64})(?=\s|$)""")
_CHAT_ROLES = {"system", "developer", "user", "assistant"}
_TEXT_BLOCKS = {"text", "input_text", "output_text"}
_AUTHORED_ROLES = _CHAT_ROLES
_WITHHELD_TOOL = "[withheld: sensitive path or command]"
_WITHHELD_SESSION = "[withheld: a sensitive value reached the model in this session]"
_WITHHELD_UNPINNED = "[withheld: this output could not be scanned or masked]"
_SCAN_LIMIT = 256 * 1024
_MAX_SESSION_VALUES = 2048
# A value next to a secret word in prose: printable ASCII without separators, not a short plain word.
_VALUE_TOKEN = re.compile(r"""[^\s"'`,;:=<>()\[\]{}\x00-\x1f\x7f-\uffff]{6,128}""")
# A plain lowercase word is prose, not a value: masking it would blank words such as "generation".
_PLAIN_WORD = re.compile(r"[a-z]+|[0-9]{1,5}")

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
# Any format: a word that names a secret takes its line and the lines next to it. Counts of tokens stay.
_LOOSE_WORD = (
    r"(?:password|passwd|passphrase|pwd|secret|token(?!s(?![a-z])|_?count)|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|cookie|authorization|(?<![a-z])pass(?![a-z])|(?<![a-z])auth(?![a-z])"
    r"|パスワード|暗証番号|秘密鍵|認証情報|認証トークン|トークン|シークレット|合言葉)")
_LOOSE_SECRET = re.compile(r"(?i)" + _LOOSE_WORD)
_SEGMENT_BREAK = re.compile(r"(\r?\n|\\+(?:r\\+)?n|[。！？])")
_LT = r"(?:<|&lt;|&\#0*60;|&\#x0*3[cC];|\\+u003[cC])"
_GT = r"(?:>|&gt;|&\#0*62;|&\#x0*3[eE];|\\+u003[eE])"
# An element takes everything up to its closing tag, or the rest of the text when there is none.
_SECRET_ELEMENT = re.compile(
    r"(?is)" + _LT + r"(?P<tag>[\w:.-]{0,64}" + _LOOSE_WORD + r"[\w:.-]{0,64})(?=[\s/>&\\])"
    + r".*?(?:" + _LT + r"\\*/(?P=tag)\s*" + _GT + r"|\Z)")


def _value(group, limit="", bare=r"[^\s,;}\]]+"):
    # limit bounds a value that has to be followed by more of the pattern.
    scan = limit or "*?"
    return (r"(?:(?P<" + group + ">" + _QUOTE + r")(?:[^\n]" + scan + r"(?<!\\)(?P=" + group + r")"
            + ("" if limit else r"|[^\n]*") + r")|" + bare + ")")


def _entry_key(name):
    return r"(?<![\w-])" + name + _QUOTE + r"?" + _GAP + r"[:=]" + _GAP



_SECRET_NAME = re.compile(
    r"(?i)(password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|cookie|auth(?!ors?(?![a-z]))|(?<![a-z])pass(?![a-z]))")
_NOT_A_CREDENTIAL_NAME = re.compile(r"PWD|OLDPWD")
_UNSET_VALUE = re.compile(r"(?i)none|null|nil")
_FLAG_VALUE = re.compile(r"(?i)true|false|yes|no|on|off|none|null|nil|enabled|disabled|0|1")
# Whole words of the name, at its ends: MINIO_ROOT_PASSWORD holds "min" and SUPPORT_SECRET holds "port".
_TOGGLE_LAST_WORD = re.compile(r"(?i)enabled?|disabled?|required?|parallelism|rotation")
_TOGGLE_FIRST_WORD = re.compile(r"(?i)use|enable|disable|no|skip|require")
_COUNTER_LAST_WORD = re.compile(r"(?i)tokens|count|limit|max|min|ttl|timeout|expiry|expires|interval|seconds|minutes|hours|days|age|size|port|bytes")
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
# A secret key whose value spans several tokens or lines: a list, a map, a shell array, a YAML block.
_STRUCTURED_KEY = re.compile(
    r"(?i)(?:" + _entry_key("name") + _QUOTE + r"?" + _SECRET_KEY + _QUOTE + r"?" + _ENTRY_GAP + _QUOTE + r"?" + _entry_key("value")
    + r"|(?<![\w-])--?" + _SECRET_KEY + r"[ \t]+"
    + r"|[\"']?" + _SECRET_KEY + _QUOTE + r"?" + _GAP + _SEPARATOR + _GAP + r")")
_ENTRY_VALUE = re.compile(r"(?i)" + _entry_key("value") + r"(?=[\[{(])")
_ENTRY_NAME_AFTER = re.compile(
    r"(?i)" + _QUOTE + r"?" + _ENTRY_GAP + _QUOTE + r"?" + _entry_key("name") + _QUOTE + r"?" + _SECRET_KEY)
_STRUCTURE_TOKEN = re.compile(_QUOTE + r"|[\[\]{}()]")
_CLOSER = {"[": "]", "{": "}", "(": ")"}
_LINE_BREAK = re.compile(r"\r?\n|\\+(?:r\\+)?n")
_BLOCK_SCALAR = re.compile(r"[|>][-+]?\d?[ \t]*(?:\r?\n|\\+(?:r\\+)?n)")
_GAP_AT_END = re.compile(_GAP + r"\Z")
_MAX_REVERSED_ENTRIES = 1000
_MAX_REVERSED_VALUE = 4096
_PLACEHOLDER = re.compile(r"\[REDACTED:[a-z_]+\]")
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|[/\\\s\"'=])(?:auth\.json|\.credentials[^/\s\"']*|\.env(?:\.[\w-]+)?|\.ssh(?:[/\\]|\b)|\.aws(?:[/\\]|\b)|kubeconfig|\.git-credentials|\.netrc|id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?|\.gnupg(?:[/\\]|\b)|secrets?(?:[/\\])"
    # A file or directory named for tokens (tokens.json, refresh_tokens.txt, /var/lib/app/tokens). A bare key such as
    # max_output_tokens has no extension and no path separator, so it is not a path; a bare token(s) file name is.
    # Bounded to a file name's length so a long line is scanned in linear time.
    r"|tokens?(?=[\s\"']|\Z)"
    # Private key and keystore files (private.key, server.pem, client.pfx): once a key body loses its header it has no
    # known format left to mask, so reading the file withholds its bodies. Bounded to a file name's length.
    r"|[^/\\\s\"'=]{0,255}?\.(?:key|pem|p8|p12|pfx|jks|keystore|ppk)(?=[\s\"';|&)]|\Z)"
    r")|[/\\][^/\\\s\"']{0,255}?token|token[^/\\\s\"']{0,255}?(?:\.[A-Za-z0-9]+(?![\w-])|[/\\])")
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
    r"|\bhelm\s+get\b|\bgh\s+auth\s+token\b|\bgit\s+credential\b|langfuse-ingest"
    # Cloud and CLI credential commands that print a token, key or secret value to stdout. The subcommand may follow
    # global flags, so up to 512 characters of the same command (no pipe or separator) are skipped.
    r"|\bgcloud\b[^|;&\n]{0,512}?\b(?:print-(?:access|identity)-token|config-helper|secrets\s+versions\s+access"
    r"|keys\s+create)\b"
    r"|\baz\b[^|;&\n]{0,512}?\b(?:get-access-token|keyvault\s+(?:secret|key|certificate)\s+(?:show|download)"
    r"|create-for-rbac|credential\s+reset|keys\s+list)\b|(?<!\w)--expose-token\b"
    r"|\baws\b[^|;&\n]{0,512}?\b(?:sts\s+(?:get-session-token|assume-role[\w-]*|get-federation-token)"
    r"|get-login-password|create-access-key|eks\s+get-token|get-authorization-token|get-role-credentials)\b"
    r"|\b(?:kubectl|oc)\b[^|;&\n]{0,512}?\bcreate\s+token\b|\boc\s+whoami\s+(?:-t|--show-token)\b"
    r"|\bvault\s+(?:--?\S+\s+(?:[^-\s]\S*\s+)?){0,8}(?:kv\s+get|read|token\s+create|login|print\s+token)\b"
    r"|\bop\s+(?:--?\S+\s+(?:[^-\s]\S*\s+)?){0,8}(?:read|item\s+get|inject)\b|\bbw\s+get\b|\bheroku\s+auth:token\b"
    r"|\bnpm\s+token\b|\bdoctl\s+auth\s+token\b|(?<!\w)--show-token\b|\bdocker-credential-[\w-]+")
_CLUSTER_CLI = re.compile(r"(?i)\b(?:kubectl|oc)\s")
_ENV_OUTPUT = [re.compile(r"(?:^|[^A-Za-z0-9_]|\\[nrt]|\\u0000)" + name + "=") for name in ("PATH", "HOME")]

_lock = threading.Lock()
_turns = {}
_sessions = collections.OrderedDict()  # session id -> reason its later bodies are withheld, or ""
_session_values = collections.OrderedDict()  # session id -> values named in prose next to a secret word
_scanned = collections.OrderedDict()  # session id -> fingerprints of request messages already scanned
_MAX_SCANNED_MESSAGES = 8192  # per session; past it the set restarts and the whole history is scanned again
_local = threading.local()
_delegated = collections.OrderedDict()  # session ids started by delegation
_parents = collections.OrderedDict()  # session id -> the session that delegated (or rotated) to it
# No depth limit on a lineage: cutting the walk short would keep a deep child's withholding from reaching the
# ancestors above the cut. The walk is bounded by _parents itself (at most _MAX_SESSIONS links, see _evict) and stops
# on a cycle.
# Sessions whose withholding reason, pinned values or parent link were evicted from the bounded maps above. A Bloom
# filter of fixed size (1 MiB) that is never cleared while the process runs: a false positive only withholds an
# unrelated session (fail-closed), and forgetting a session that saw a secret would send its later bodies unmasked.
# A restart clears it, as it clears everything else; history from before a restart is scanned again (_note_request).
_FORGOTTEN_BITS = 1 << 23
_forgotten = bytearray(_FORGOTTEN_BITS // 8)
_seen_prompts = {}
_stats = {}
_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_secret_values = None
_CONTROL_ESCAPES = {"\n": "n", "\r": "r", "\t": "t", "\b": "b", "\f": "f"}
_HTML_NAMES = {"&": "amp", "<": "lt", ">": "gt", '"': "quot", "'": "apos"}
_env_pattern_cache = (None, None)
_creds = None


def _count(key, n=1):
    kinds = getattr(_local, "kinds", None)
    if kinds is not None:  # an evidence scan, not an export: record the kind only
        kinds.add(key)
        return
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
        _log.info("boxp-langfuse-observer tracks exposure but exports nothing until the ingest key appears")
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

def _is_setting(name, value):
    """The value and the name have to agree: PASSWORD=disabled is a password, AUTH_ENABLED=disabled is a setting."""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", name) if word]
    if not words:
        return False
    if _FLAG_VALUE.fullmatch(value):
        if _UNSET_VALUE.fullmatch(value) and _COUNTER_LAST_WORD.fullmatch(words[-1]):
            return True  # max_output_tokens: null, token_ttl: none
        return bool(_TOGGLE_LAST_WORD.fullmatch(words[-1]) or _TOGGLE_FIRST_WORD.fullmatch(words[0]))
    return bool(re.fullmatch(r"[0-9]+", value) and _COUNTER_LAST_WORD.fullmatch(words[-1]))


def _is_secret_value(name, value):
    """Short values count too. Only a setting is left alone, since masking it would blank ordinary words and numbers."""
    return (isinstance(value, str) and value.strip() and _SECRET_NAME.search(name)
            and not _NOT_A_CREDENTIAL_NAME.fullmatch(name) and not _is_setting(name, value))


def _env_secrets():
    global _secret_values
    if _secret_values is None:
        found = {v for k, v in os.environ.items() if _is_secret_value(k, v)}
        # A value read from a file keeps its newline; the text shows it without.
        _secret_values = sorted(found | {v.strip() for v in found}, key=len, reverse=True)
    return _secret_values


def _encoded_char(ch):
    """Every way serialized text can carry ch, letters and digits included. Escapes can repeat, since JSON can sit inside JSON
    (bounded at 255 backslashes, depth 8, to keep the scan linear) and a URL can be encoded again (%2561, same depth)."""
    code = ord(ch)
    units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
    hex_forms = ["".join(r"\\{1,255}u%04x" % unit for unit in units),
                 "".join("%%(?:25){0,8}%02x" % byte for byte in ch.encode("utf-8", "surrogatepass")),
                 f"&#0*{code};", f"&#x0*{code:x};"]
    if code < 0x100:
        hex_forms.append(r"\\{1,255}x%02x" % code)
    if ch in _HTML_NAMES:
        hex_forms.append(f"&{_HTML_NAMES[ch]};")
    forms = ["(?i:" + "|".join(hex_forms) + ")"]
    if ch.isascii() and ch.isalnum():
        return "(?:" + "|".join(forms + [ch]) + ")"  # \\n is a newline, not an n
    if ch in _CONTROL_ESCAPES:
        forms.append(r"\\{1,255}" + _CONTROL_ESCAPES[ch])
    if ch == " ":
        forms.append(r"\+")
    forms += [r"\\{1,255}" + re.escape(ch), re.escape(ch)]
    return "(?:" + "|".join(forms) + ")"


def _value_pattern(value, special=None):
    """Every position can start a value, so its first character is only tried at the start of a run of backslashes: the scan stays linear."""
    chars = [(special or {}).get(ch) or _encoded_char(ch) for ch in value]
    chars[0] = chars[0].replace(r"\\{1,255}", r"(?<!\\)\\+")
    return "".join(chars)


def _encoded_forms(value):
    """The value as the model can hand it back re-encoded: base64 and base64url at each of the 3 byte alignments (so it
    is found inside a longer encoded run too, without the characters the neighbouring bytes share), hex in either case,
    and reversed. Each form is matched like the value itself, in every serialized encoding."""
    raw = value.encode("utf-8", "surrogatepass")
    forms = set()
    for offset in range(3):
        start, end = -(-offset * 8 // 6), (offset + len(raw)) * 8 // 6
        forms.add(base64.b64encode(b"\0" * offset + raw).decode("ascii")[start:end])
    # base64url differs only in two characters, so one pattern takes both alphabets.
    url_safe = {"+": "(?:" + _encoded_char("+") + "|-)", "/": "(?:" + _encoded_char("/") + "|_)"}
    patterns = [_value_pattern(form, url_safe) for form in sorted(forms) if len(form) >= _MIN_PINNED and form != value]
    # Reversed at every length: a short value pinned from a reply ("the password is qzv") is as easy to hand back
    # reversed ("vzq"). A short ASCII session value is matched as a word of its own, its reverse included.
    if value[::-1] != value:
        patterns.append(_value_pattern(value[::-1]))
    return patterns + ["(?i:" + _value_pattern(raw.hex()) + ")"]  # hex digits in either case


def _values_regex(values, bounded_short=False):
    """Longest first, each value followed by its encoded forms. With bounded_short (session values), a short ASCII
    value (pinned from a reply, "the password is qzv") is matched as a word of its own, so it does not blank every
    longer word that contains it. Environment values keep matching anywhere."""
    def patterns(value):
        alternatives = [_value_pattern(value)] + _encoded_forms(value)
        if bounded_short and value.isascii() and len(value) < _MIN_PINNED:
            return [r"(?<![A-Za-z0-9])(?:" + "|".join(alternatives) + r")(?![A-Za-z0-9])"]
        return alternatives
    return re.compile("|".join(p for value in sorted(values, key=len, reverse=True) for p in patterns(value)))


# A run that can be base64, base64url or hex. Encoding a longer text that contains a value ("xqzvy" -> eHF6dnk=) leaves
# no fixed encoded form of the value itself, so the run is decoded and the value looked for in the decoded text.
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_-]{3,}={0,2}")
_HEX_RUN = re.compile(r"[0-9a-fA-F]+")
_URL_SAFE = str.maketrans("-_", "+/")
_MAX_DECODED_RUN = 1 << 20


def _readable(data):
    """Decoded bytes that read as text. An ordinary word decodes to bytes that do not, so it is left alone."""
    if len(data) < 2:
        return None
    text = data.decode("utf-8", "replace")
    printable = sum(ch.isprintable() or ch in "\t\n\r" for ch in text if ch != "\ufffd")
    if text.count("\ufffd") > 2 or printable < 0.9 * len(text):
        return None
    return text


def _decodings(run):
    core = run.rstrip("=")[:_MAX_DECODED_RUN]
    found = []
    if _HEX_RUN.fullmatch(core):
        for part in (core, core[1:], core[:-1]):
            if len(part) % 2 == 0 and part:
                found.append(bytes.fromhex(part))
    for offset in range(4):
        part = core[offset:].translate(_URL_SAFE)
        if len(part) % 4 == 1:
            part = part[:-1]
        try:
            found.append(base64.b64decode(part + "=" * (-len(part) % 4)))
        except (ValueError, TypeError):
            continue
    return [text for text in map(_readable, found) if text]


class _ValuesMatcher:
    """The value regex (each value and its fixed encoded forms) plus a decode of every encoded run, so a value inside
    an encoded longer text is found too. Inside decoded text a value matches anywhere: a blanked encoded run costs less
    than a leak."""

    def __init__(self, values, bounded_short=False):
        self.regex = _values_regex(values, bounded_short)
        self.plain = re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True) if v))

    def _hides_value(self, run):
        return any(self.plain.search(text) for text in _decodings(run))

    def search(self, text):
        found = self.regex.search(text)
        if found:
            return found
        return next((m for m in _ENCODED_RUN.finditer(text) if self._hides_value(m.group(0))), None)

    def subn(self, repl, text):
        out, hits = self.regex.subn(repl, text)
        count = [0]

        def sub(match):
            if self._hides_value(match.group(0)):
                count[0] += 1
                return repl
            return match.group(0)
        out = _ENCODED_RUN.sub(sub, out)
        return out, hits + count[0]


def _env_pattern():
    """One alternation, longest first, so a later value cannot rewrite the mark of an earlier one.
    Any length matches inside a longer word: the value can be part of it, and a blanked word costs less than a leak."""
    global _env_pattern_cache
    values = _env_secrets()
    if _env_pattern_cache[0] is not values:
        pattern = _ValuesMatcher(values) if values else None
        _env_pattern_cache = (values, pattern)
    return _env_pattern_cache[1]


def _is_entropy(s):
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", s) or "/" in s.strip("/") and s.count("/") >= 2:
        return False
    classes = sum(bool(re.search(p, s)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    return classes == 3


def _closes_quote(token, opening):
    """A quote closes only at its own depth: the depths use 0, 1, 3, 7... backslashes, plus escaped backslashes."""
    if token[-1] != opening[-1] or token.lstrip("\\") != opening.lstrip("\\"):
        return False
    depth = len(opening) - len(opening.lstrip("\\"))
    return (len(token) - len(token.lstrip("\\")) - depth) % (2 * (depth + 1)) == 0


def _bracket_end(text, start, stop=None):
    """End of the bracket opened at start, or of the scanned text when it never closes."""
    stop = len(text) if stop is None else min(stop, len(text))
    opener, closer = text[start], _CLOSER[text[start]]
    depth, quote = 0, None
    for match in _STRUCTURE_TOKEN.finditer(text, start, stop):
        token = match.group(0)
        if quote:
            if _closes_quote(token, quote):
                quote = None
        elif token == opener:
            depth += 1
        elif token == closer:
            depth -= 1
            if depth == 0:
                return match.end()
        elif token not in "[]{}()":
            quote = token
    return stop


def _block_end(text, key_end):
    """End of the YAML block under a key that ends at key_end, or key_end when no line belongs to it."""
    # A key further along a longer line than this counts as column 0, which only widens the block.
    line_start = max(0, key_end - 256)
    for found in _LINE_BREAK.finditer(text, line_start, key_end):
        line_start = found.end()
    head = text[line_start:key_end]
    indent = len(head) - len(head.lstrip(" \t"))
    end, position = key_end, key_end
    while True:
        line_break = _LINE_BREAK.search(text, position)
        if line_break is None:
            return end
        next_break = _LINE_BREAK.search(text, line_break.end())
        position = next_break.start() if next_break else len(text)
        line = text[line_break.end():position]
        body = line.lstrip(" \t")
        if not body.strip():
            if next_break is None:
                return end
            continue
        column = len(line) - len(body)
        if column < indent or (column == indent and not re.match(r"-(?:\s|$)", body)):
            return end
        end = position


def _structured_end(text, match):
    """End of the multi-token value after a secret key, or None when the value is a single token."""
    if match.end() >= len(text):
        return None
    if text[match.end()] in _CLOSER:
        return _bracket_end(text, match.end())
    gap = _GAP_AT_END.search(text, match.start(), match.end())
    key_end = gap.start() if gap else match.end()
    if not _LINE_BREAK.search(text, key_end, match.end()) and not _BLOCK_SCALAR.match(text, match.end()):
        return None
    end = _block_end(text, key_end)
    return end if end > key_end else None


def _mask_structured(text):
    """A pattern can only match the start of such a value, so these are cut out by their extent first."""
    pieces, position = [], 0
    while True:
        match = _STRUCTURED_KEY.search(text, position)
        if match is None:
            break
        end = _structured_end(text, match)
        if end is None:
            pieces.append(text[position:match.end()])
        else:
            pieces.append(text[position:match.start()] + "[REDACTED:assignment]")
            _count("redactions_assignment")
        position = end if end is not None else max(match.end(), position + 1)
    pieces.append(text[position:])
    return _mask_reversed_entries("".join(pieces))


def _mask_reversed_entries(text):
    # {"value": [...], "name": "DB_PASSWORD"}: only what follows the value says it is a secret.
    pieces, position = [], 0
    for scanned, match in enumerate(_ENTRY_VALUE.finditer(text)):
        if scanned >= _MAX_REVERSED_ENTRIES:  # scanning stays bounded; redact() drops the field instead
            raise ValueError("too many entries")
        if match.start() < position:
            continue
        end = _bracket_end(text, match.end(), match.end() + _MAX_REVERSED_VALUE)
        after = _ENTRY_NAME_AFTER.match(text, end)
        if after is None:
            continue
        pieces.append(text[position:match.start()] + "[REDACTED:env_entry]")
        position = after.start()
        _count("redactions_env_entry")
    pieces.append(text[position:])
    return "".join(pieces)


def _count_element(_match):
    _count("redactions_element")
    return "[REDACTED:element]"


def _mask_secret_lines(text):
    """The format-independent rule: nothing about a secret word's line and its neighbours is trusted."""
    if not _LOOSE_SECRET.search(_PLACEHOLDER.sub("", text)):
        return text
    parts = _SEGMENT_BREAK.split(text)  # text, break, text, break, ...
    segments = parts[::2]
    hits = [i for i, part in enumerate(segments) if _LOOSE_SECRET.search(_PLACEHOLDER.sub("", part))]
    filled = [i for i, part in enumerate(segments) if part.strip()]
    position = {segment: at for at, segment in enumerate(filled)}
    masked = set()
    for hit in hits:  # the nearest line with content on each side: blank lines do not separate a value
        at = position[hit]
        masked.update(filled[max(at - 1, 0):at + 2])
    _count("redactions_secret_line", len(masked))
    for i in masked:
        parts[i * 2] = "[REDACTED:secret_line]"
    return "".join(parts)


def redact(text, extra=None):
    """Return text without secrets. Raises nothing: failure drops the whole field."""
    try:
        out = str(text)
        if extra is not None:
            out, hits = extra.subn("[REDACTED:session_value]", out)
            if hits:
                _count("redactions_session_value")
        env_pattern = _env_pattern()
        if env_pattern:
            out, hits = env_pattern.subn("[REDACTED:env]", out)
            if hits:
                _count("redactions_env")
        out = _SECRET_ELEMENT.sub(_count_element, out)
        # Before the patterns, which would take the word with them and leave the rest of a value behind.
        out = _mask_secret_lines(_mask_structured(out))
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


# Attributes and span fields this code fills itself (no text of the session): a pinned word is matched against the rest.
_GENERATED_ATTRIBUTES = re.compile(r"langfuse\.trace\.metadata\.(?:status|coverage\.[\w.]+)|langfuse\.(?:trace\.name|release)|"
                                   r"langfuse\.observation\.(?:type|level|usage_details)|service\.name")
_GENERATED_FIELDS = frozenset({"key", "scope", "traceId", "spanId", "parentSpanId", "startTimeUnixNano", "endTimeUnixNano", "kind"})


def _payload_values(value, depth=0):
    """String values of the payload that can carry session text. A pinned word ("value", "none", "false") is masked in
    every field already; matching it against JSON keys, attribute names and the plugin's own metadata would only drop
    spans that hold nothing of the session."""
    if isinstance(value, str):
        yield value
    elif depth < 64 and isinstance(value, dict):
        if isinstance(value.get("key"), str) and _GENERATED_ATTRIBUTES.fullmatch(value["key"]):
            return
        for key, item in value.items():
            if key not in _GENERATED_FIELDS:
                yield from _payload_values(item, depth + 1)
    elif depth < 64 and isinstance(value, list):
        for item in value:
            yield from _payload_values(item, depth + 1)


def _unsafe(serialized, extra=None):
    """Final guard on the JSON text that is about to leave the process."""
    probe = _PLACEHOLDER.sub("", serialized)
    env_pattern = _env_pattern()
    if (env_pattern and env_pattern.search(probe)) or _LOOSE_SECRET.search(probe):
        return True
    if extra is not None:
        try:
            values = "\n".join(_payload_values(json.loads(serialized)))
        except ValueError:
            return True
        if extra.search(_PLACEHOLDER.sub("", values)) or extra.search(_PLACEHOLDER.sub("", json.dumps(values))):
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


# A value that only looks empty ("none", "true", "xxxxx", "****") can be the password itself, so it is not a reference.
_REFERENCE_VALUE = re.compile(r"""(?:\$\{?[\w.-]*\}?|<[^>]*>|\[REDACTED:[a-z_]+\]|\.\.\.|…|"{2}|'{2})""")
_ASSIGNED = re.compile(r"""^(?P<name>.*?)(?:[:=]|\\+u00(?:3[aA]|3[dD])|[ \t]+)(?P<value>.*)$""", re.S)
_MAX_EVIDENCE_MATCHES = 200


def _assigned_value(match_text):
    """A secret-named key with a real value: not a setting, a counter, a placeholder or a reference."""
    found = _ASSIGNED.match(match_text.strip())
    if not found:
        return True
    name = found.group("name").strip().strip("\"'\\-")
    value = found.group("value").strip().strip("\"'\\").strip()
    if not value or _REFERENCE_VALUE.fullmatch(value):
        return False
    return not _is_setting(name, value)


# A JSON escape (\\n, \\t) separates words like the character it stands for, so its letter is not left as a word.
_TOKEN_SPLIT = re.compile(r"""(?:[\s"'`,;:=<>()\[\]{}]|\\+[nrt]?)+""")
_STRIP = re.compile(r"""^(?:["'`]|\\+["'nrt]?)+|(?:["'`,;]|\\+["'nrt]?)+$""")
# Structural words (keys of a JSON/YAML secret reference, a URL scheme, a literal) are skipped beside a real value, but a
# value made of them alone is the value itself ("password: value"). Auth scheme words (Basic, Bearer) are never skipped:
# a password can be "bearer", and pinning the scheme word only masks that word later in the session.
_STOPWORD = re.compile(r"(?i)https?|user|username|name|value|key|ref|from|data|true|false|null|none")
# Only plain words stand alone as a value: "name:" (a key) and data["token"] (an expression) are structure.
_PLAIN_VALUE = re.compile(r"[\w.+/-]+(?:[ \t]+[\w.+/-]+)*")
# A literal (true, none) or a masked-looking run (xxxxx, ****) taken alone is the value, not "no value": a password can
# be any of them and cannot be told from a placeholder. Pinning masks only that word later in the session; a setting
# (PASSWORD_REQUIRED=true, max_output_tokens: null) is left alone by _is_setting.
_REFERENCE = re.compile(r"""(?i)\$\{?[\w.-]*\}?|<[^>]*>|\{\{[^}]*\}\}|\[REDACTED:[a-z_]+\]|\.\.\.|…|-|\|""")


def _strip(value):
    return _STRIP.sub("", str(value).strip()).strip()


# A secret word on its own ("password", "api_key", "secrets") names the value; a word that merely contains one
# ("my-secret-value") can be the value itself and is pinned like any other.
_BARE_SECRET = re.compile(r"(?i)[_.-]?(?:" + _LOOSE_WORD + r")s?[_.-]?")
# An env name (DB_PASSWORD) in an env entry names the value beside it.
_ENV_NAME = re.compile(r"[A-Z0-9_]*[A-Z][A-Z0-9_]*")
# A key in a structured value ({"token": ...}, token=...) is structure, not the value.
_KEY_TOKEN = re.compile(r"""([\w.-]+)["']?\s*[:=]""")


def _secret_word_only(token):
    """A token that is a secret name and not a value: a bare secret word, or any non-ASCII token with one (トークン数)."""
    return bool(_BARE_SECRET.fullmatch(token)) or (not token.isascii() and bool(_LOOSE_SECRET.search(token)))


def _reference(token):
    return not token or _secret_word_only(token) or _STOPWORD.fullmatch(token) or _REFERENCE.fullmatch(token)


_MIN_PINNED = 4


class _ShortValue(Exception):
    """A secret value too short to pin."""


class _UnpinnedValue(Exception):
    """A non-ASCII secret value: unspaced text has no word boundary, so the part repeated alone cannot be pinned."""


def _token_values(raw):
    """Values to mask for matched text. A single token is the value itself; a value of several words keeps the whole
    text and its secret-looking tokens. References (variables, placeholders, secret names, key words) are skipped,
    and a value under 4 characters raises _ShortValue: masking it everywhere would blank ordinary text, so the session
    is withheld instead."""
    found = set()
    for value in raw:
        value = _strip(value or "")
        if not value or _REFERENCE.fullmatch(value):
            continue
        # Tokens are checked one by one: a nested value such as {"token": "..."} names a secret and holds one.
        words = [t for t in (_strip(t) for t in _TOKEN_SPLIT.split(value)) if t]
        keys = {k for k in _KEY_TOKEN.findall(value) if _LOOSE_SECRET.search(k)}
        tokens = [t for t in words if not _reference(t) and t not in keys]
        if words and _PLAIN_VALUE.fullmatch(value):
            # Plain words carry no structure, so a structural word among them is part of the value ("password: value",
            # "password: value horsestaple"), not a key around one: it can be repeated alone.
            tokens = [t for t in words if not _secret_word_only(t) and not _REFERENCE.fullmatch(t)]
            if not tokens:
                # A value made of secret words alone ("password: secret") is the value itself.
                tokens = [t for t in words if t.isascii() and _BARE_SECRET.fullmatch(t)]
        # Every word is pinned, plain or not: the model can repeat any one of a several-word value alone.
        for token in tokens:
            if not token.isascii():
                raise _UnpinnedValue(token)
            if len(token) < _MIN_PINNED:
                # Too short to mask wherever it appears again, so the value cannot be pinned: the caller withholds.
                raise _ShortValue(token)
        found.update(tokens)
        if len(tokens) > 1 and (_PLAIN_VALUE.fullmatch(value) or not _LOOSE_SECRET.search(value)):
            found.add(value)
    return found


_AFTER_SEPARATOR = re.compile(r"(?s)^.*?(?:[:=]|\\+u00(?:3[aA]|3[dD]))(.*)$")


def _pinned_values(kind, text):
    """The values a pattern match shows, or None when it cannot be pinned (the caller withholds)."""
    if kind in ("private_key", "data_uri"):
        return None
    if kind == "header":
        raw = [text.split(":", 1)[1] if ":" in text else text]
    elif kind == "url_credentials":
        found = re.search(r"://[^:/@\s]*:([^@\s]*)@", text)
        raw = [found.group(1) if found else ""]
    elif kind == "cli_password":
        last = re.sub(r"^(?:-p|(?:-u|--user)=?)", "", text.split()[-1])
        raw = [re.sub(r"^[^:]*:(?=.)", "", last)]
    elif kind == "flag":
        raw = [re.split(r"[ \t]+", text)[-1]]
    elif kind == "assignment":
        found = _AFTER_SEPARATOR.match(text)
        raw = (found.group(1) if found else text).splitlines() or [""]
    elif kind == "bearer":
        raw = [re.sub(r"(?i)^bearer\s+", "", text)]
    elif kind == "env_entry":
        raw = [t for t in _TOKEN_SPLIT.split(text) if not (_ENV_NAME.fullmatch(_strip(t)) and _LOOSE_SECRET.search(t))]
    else:
        raw = [text]
    return _token_values(raw)


def _block_values(text):
    """Values of secret-named YAML/JSON blocks and XML elements, or None when one cannot be pinned."""
    if _ENTRY_VALUE.search(text):  # {"value": [...], "name": "DB_PASSWORD"}: the extent is only known by masking
        try:
            if _mask_reversed_entries(text) != text:
                return None
        except ValueError:
            return None
    found, position = set(), 0
    for n in range(_MAX_EVIDENCE_MATCHES + 1):
        match = _STRUCTURED_KEY.search(text, position)
        if match is None:
            break
        if n == _MAX_EVIDENCE_MATCHES:
            return None
        end = _structured_end(text, match)
        if end is None:
            position = max(match.end(), position + 1)
            continue
        found |= _token_values(_LINE_BREAK.split(text[match.end():end]))
        position = end
    for n, element in enumerate(_SECRET_ELEMENT.finditer(text)):
        if n >= _MAX_EVIDENCE_MATCHES:
            return None
        found |= _token_values([re.sub(r"(?s)^[^>]*>|<[^<]*$", "", element.group(0))])
    return found


def _evidence(text):
    """(reason, values): a reason when text shows the model a secret that cannot be pinned to a value, else the
    values to mask wherever they appear later. A value of this process's environment is already masked everywhere."""
    if len(text) > _SCAN_LIMIT:
        return "unscanned_large_output", set()
    try:
        values = _block_values(text)
        if values is None:
            return "sensitive_value", set()
        for kind, pattern in _PATTERNS:
            for n, match in enumerate(pattern.finditer(text)):
                if n >= _MAX_EVIDENCE_MATCHES:
                    return "unscanned_large_output", set()
                if kind == "entropy" and not _is_entropy(match.group(0)):
                    continue
                if kind in ("assignment", "flag") and not _assigned_value(match.group(0)):
                    continue
                pinned = _pinned_values(kind, match.group(0))
                if pinned is None:
                    return "sensitive_value", set()
                values |= pinned
    except _ShortValue:
        return "short_value", set()
    except _UnpinnedValue:
        return "unpinned_value", set()
    except Exception:
        return "scan_error", set()
    return None, values


# Words that join a secret word to its value in prose ("the password is ..."); they are not values themselves.
_CONNECTIVE = re.compile(
    r"(?i)a|an|the|is|are|was|were|be|been|being|to|for|of|on|in|at|by|as|it|its|this|that|these|those|and|or|not|no|"
    r"my|your|our|their|his|her|new|old|set|use|used|using|with|from|into|via|here|there|now|then|also|still|just|"
    r"if|so|do|does|did|has|have|had|will|can|may|must|should|would|could|me|you|we|they|ok|yes|now|again|below|above")
# ASCII runs and non-ASCII runs are separate tokens, so a particle written against a value (パスワードはabc) splits off.
_PROSE_TOKEN = re.compile(r"""[^\s"'`,;:=<>()\[\]{}\x00-\x1f\x7f-\U0010ffff]+|[^\x00-\x7f\s、。，．,「」『』（）【】［］〈〉《》：；！？・…]+""")
# Japanese particles and copulas that join a secret word to its value; any other non-ASCII candidate is a value.
_JA_CONNECTIVE = re.compile(r"は|が|を|も|の|に|で|と|へ|や|か|って|とは|には|では|です|でした|である|だ|だよ|ですよ|になります")
# トークン数, トークン上限 and the like count tokens; they do not name a secret.
_JA_COUNT = re.compile(r"数|量|上限|消費|使用|単位|長|化|ナイザ|課金|料金")
_JA_VALUE_EDGE = re.compile(r"^(?:は|って|とは|が|を|:|：|=|＝)+|(?:です|でした|である|だ|だよ|ですよ|になります)+$")
_MIN_REPLY_PINNED = 2


def _ja_pins(token):
    """A non-ASCII value named in a reply ("パスワードは 秘密のもじれつ"): the value without the separator or copula
    around it, and its parts split at の, so the whole or a part repeated alone is masked."""
    value = _JA_VALUE_EDGE.sub("", token)
    return {v for v in [value] + value.split("の") if len(v) >= _MIN_REPLY_PINNED}


_SEPARATOR_END = re.compile(r"(?:[:=：＝]|は|って|とは)\s*$")
_MAX_PROSE_VALUES = 64
_STRUCTURE_AFTER = re.compile(r"""\s*[:=\[({."'\\]""")
_NAME_REST = re.compile(r"[A-Za-z_.-]{0,16}")


def _secret_name(token):
    """A token that names a secret (password, DB_PASSWORD, apiKey), not one that holds a value."""
    return bool(_LOOSE_SECRET.search(token)) and bool(_NAME_REST.fullmatch(_LOOSE_SECRET.sub("", token)))


def _prose_candidates(line, cut):
    """Candidate values on line after position cut: tokens that are not connectives, names or references."""
    matches = [(m.group(0).strip(".-/+"), m.end()) for m in _PROSE_TOKEN.finditer(line, cut)]
    words = [(t, end) for t, end in matches if t and not _CONNECTIVE.fullmatch(t) and not _JA_CONNECTIVE.fullmatch(t)
             and not _secret_word_only(t) and not _REFERENCE.fullmatch(t)]
    # A structural word is a candidate like any other ("the password is value horsestaple": the model can repeat
    # "value" alone), unless it is a key or an expression ("name:", data["token"]), as in _token_values.
    found = [t for t, end in words if not (_STOPWORD.fullmatch(t) and _STRUCTURE_AFTER.match(line, end))]
    if not found:
        # Nothing but secret words after the secret word ("the password is secret"): the word is the value.
        found = [t for t, _ in matches if t.isascii() and _BARE_SECRET.fullmatch(t)]
    return found


_VALUE_SEPARATOR = re.compile(r"(?i)\s*(?:[:=：＝]|は|って|とは|(?:is|are|was|were)(?![a-z]))")
_COPULA = re.compile(r"(?i)is|are|was|were|=|:")
_QUESTION = re.compile(r"(?i)what|which|where|who|whose|how|why|when")


def _value_before(spans, cut):
    """The ASCII word joined to the secret word at cut by a copula and connectives only ("X is my password",
    "X was the token"), or None. Only the nearest word: earlier words of the sentence are not the value."""
    before = [t for start, end, t in spans if end <= cut and t and not _secret_name(t)]
    gap = []
    while before and (_CONNECTIVE.fullmatch(before[-1]) or _COPULA.fullmatch(before[-1])):
        gap.append(before.pop())
    if not before or not any(_COPULA.fullmatch(t) for t in gap):
        return None
    word = before[-1]
    if not word.isascii() or _REFERENCE.fullmatch(word) or _STOPWORD.fullmatch(word) or _QUESTION.fullmatch(word):
        return None
    return word


def _prose_values(text, reply=False):
    """Values a secret word names in prose. On the secret word's line, every word after it that is not a connective
    is a candidate, plain or not ("the password is correcthorsebattery"); when the line ends at the secret word and a
    separator ("password:", "パスワード："), the next line's words are; otherwise the next line's value-like tokens.
    Returns (values, reason). reason is "short_value" when a candidate is under 4 characters ("the password is alpha
    abc") or there are more candidates than are pinned, and "unpinned_value" when a candidate is non-ASCII: unspaced
    text has no word boundary, so the part the model may repeat alone ("秘密の文字列") cannot be pinned. Either way
    the caller withholds. With reply=True (text the model or the user wrote, which is kept rather than withheld,
    TD-07) such values are pinned instead: a short ASCII one of 2+ characters as a word of its own, a non-ASCII one
    whole and in parts (_ja_pins). Only a 1-character value still gives "short_value"."""
    lines = _LINE_BREAK.split(text[:_SCAN_LIMIT])
    found, reason = set(), None
    for i, line in enumerate(lines):
        line = _PLACEHOLDER.sub(" ", line)
        if not _LOOSE_SECRET.search(line):
            continue
        spans = [(m.start(), m.end(), m.group(0).strip(".-/+")) for m in _PROSE_TOKEN.finditer(line)]
        # A non-ASCII secret word (パスワードは...) need not be a token of its own: what follows it starts the value.
        cuts = [m.end() for m in _LOOSE_SECRET.finditer(line)
                if not m.group(0).isascii() and not _JA_COUNT.match(line, m.end())]
        # Whole tokens, so a word that merely contains a secret word ("app-secrets") does not start a value.
        cuts += [end for _, end, t in spans if _secret_name(t)]
        cut = min(cuts) if cuts else len(line)
        candidates = _prose_candidates(line, cut)
        names = [t for _, end, t in spans if end == cut and _secret_name(t)]
        if names and len(candidates) == 1 and _is_setting(names[0], candidates[0]):
            candidates = []  # PASSWORD_REQUIRED=true, token_ttl: none
        following = _PLACEHOLDER.sub(" ", lines[i + 1] if i + 1 < len(lines) else "")
        if cuts and not candidates and _SEPARATOR_END.search(line[cut:]):
            # "password:" then the value on the next line.
            candidates = _prose_candidates(following, 0)
        # A non-ASCII candidate withholds only when a separator names it as the value ("パスワードは X", "password: X").
        # A secret word in an ordinary sentence ("パスワードを設定してください") is a mention, not evidence (TD-07).
        named = not cuts or _VALUE_SEPARATOR.match(line, cut) or not line[cut:].strip()
        if any(not t.isascii() for t in candidates) and named:
            if reply:
                found.update(v for t in candidates[:_MAX_PROSE_VALUES] if not t.isascii() for v in _ja_pins(t))
            else:
                reason = reason or "unpinned_value"
        # Any candidate too short to pin ("password: alpha abc"), or more candidates than are pinned, leaves a value
        # that could be repeated alone, so the caller withholds.
        short = [t for t in candidates[:_MAX_PROSE_VALUES] if t.isascii() and len(t) < _MIN_PINNED]
        if reply:
            found.update(t for t in short if len(t) >= _MIN_REPLY_PINNED)
            short = [t for t in short if len(t) < _MIN_REPLY_PINNED]
        if short or len(candidates) > _MAX_PROSE_VALUES:
            reason = reason or "short_value"
        found.update(t for t in candidates[:_MAX_PROSE_VALUES] if len(t) >= _MIN_PINNED and t.isascii())
        for token in _VALUE_TOKEN.findall(_LOOSE_SECRET.sub(" ", following)):
            if not _PLAIN_WORD.fullmatch(token):
                found.add(token)
        # The line's value-like tokens before the word too ("Zq7... is my password").
        found.update(t for start, _, t in spans if start < cut and _VALUE_TOKEN.fullmatch(t) and not _PLAIN_WORD.fullmatch(t)
                     and not _secret_name(t) and not _REFERENCE.fullmatch(t))
        # A plain word joined to the secret word by a copula is its value too ("correcthorsebattery is my password").
        before = _value_before(spans, cut)
        if before is not None:
            if len(before) < (_MIN_REPLY_PINNED if reply else _MIN_PINNED):
                reason = reason or "short_value"
            else:
                found.add(before)
    return found, reason


def _strings(value, depth=0):
    """String leaves, so a scan sees text without its JSON escapes."""
    if isinstance(value, str):
        yield value
    elif depth < 16 and isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, depth + 1)
    elif depth < 16 and isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)
    elif value is not None and not isinstance(value, (dict, list, tuple)):
        yield str(value)


def _scan_text(value):
    """Text for secret detection. String leaves are scanned without their JSON escapes, and a structured value is also
    scanned as JSON so a secret name kept only as a key ({"token": "..."}) still pins its value."""
    leaves = "\n".join(_strings(value))
    if isinstance(value, (dict, list, tuple)):
        return leaves + "\n" + _text(value)
    return leaves


def _text(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _field(value, extra=None):
    out = redact(_text(value), extra)
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


def _note_parent(session, parent):
    if not session or not parent or str(session) == str(parent):
        return
    with _lock:
        _parents[str(session)] = str(parent)
        _parents.move_to_end(str(session))
        _evict(_parents)  # a child cut from its parent would no longer see the parent's later withholding


def _forgotten_slots(session):
    digest = hashlib.sha256(str(session).encode("utf-8", "replace")).digest()
    return [int.from_bytes(digest[i:i + 4], "big") % _FORGOTTEN_BITS for i in range(0, 16, 4)]


def _forget(session):
    """Called with _lock held, for a session whose state is evicted."""
    for slot in _forgotten_slots(session):
        _forgotten[slot >> 3] |= 1 << (slot & 7)
    _stats["sessions_state_evicted"] = _stats.get("sessions_state_evicted", 0) + 1


def _was_forgotten(session):
    return all(_forgotten[slot >> 3] >> (slot & 7) & 1 for slot in _forgotten_slots(session))


def _evict(table, keep=lambda value: bool(value)):
    """Bounds a per-session map, called with _lock held. An evicted entry that still mattered is remembered as
    forgotten, so the session (and the sessions below it) are withheld instead of released."""
    while len(table) > _MAX_SESSIONS:
        session, value = table.popitem(last=False)
        if keep(value):
            _forget(session)


def _touch(session):
    """Keeps a session in use at the young end of the maps, called with _lock held: only sessions idle while
    _MAX_SESSIONS others were active are evicted."""
    for table in (_sessions, _session_values, _parents):
        if session in table:
            table.move_to_end(session)


def _lineage(session):
    """The session and the sessions above it. Looked up on every use, not copied at start: a parent can see a secret
    while its child is already running and hand the value to it."""
    chain, seen, current = [], set(), str(session or "")
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = _parents.get(current, "")
    return chain


def _tainted(session):
    """The reason later bodies of the session are withheld, or ""."""
    with _lock:
        for current in _lineage(session):
            _touch(current)
            if _sessions.get(current):
                return _sessions[current]
            if _was_forgotten(current):
                return "state_evicted"
        return ""


def _taint(reason, *sessions):
    """Withholds the sessions and every session above them: a value seen by a grandchild can reach the grandparent
    (a progress note, a relayed summary) before the session between them finishes."""
    with _lock:
        sessions = list(dict.fromkeys(s for given in sessions if given for s in _lineage(given)))
        for session in sessions:
            if session and not _sessions.get(str(session)):
                _sessions[str(session)] = reason
            if session:
                _sessions.move_to_end(str(session))
        _evict(_sessions)  # "" (observed, not withheld) is dropped; a reason is remembered
    _count("sessions_withheld_" + reason)


def _add_values(values, *sessions):
    """False once a session holds too many, so the caller withholds instead."""
    ok = True
    with _lock:
        for session in list(dict.fromkeys(s for given in sessions if given for s in _lineage(given))):
            if not session or not values:
                continue
            held = _session_values.setdefault(str(session), set())
            held.update(values)
            _session_values.move_to_end(str(session))
            if len(held) > _MAX_SESSION_VALUES:
                ok = False
        _evict(_session_values)
    return ok


def _values_of(*sessions):
    with _lock:
        lineage = [a for s in sessions if s for a in _lineage(s)]
        for current in lineage:
            _touch(current)
        return frozenset(v for a in lineage for v in _session_values.get(a, ()))


_values_pattern_cache = (None, None)


def _values_pattern(values):
    global _values_pattern_cache
    if not values:
        return None
    if _values_pattern_cache[0] != values:
        _values_pattern_cache = (values, _ValuesMatcher(values, bounded_short=True))
    return _values_pattern_cache[1]


def _note_evidence(session, parent, args_text, result_text):
    """Withhold on evidence only. Returns the reason found here, or None."""
    reason_in, pinned_in = _evidence(args_text)
    reason_out, pinned_out = _evidence(result_text)
    reason = reason_in or reason_out
    prose_in, prose_reason_in = _prose_values(args_text)
    prose_out, prose_reason_out = _prose_values(result_text)
    reason = reason or prose_reason_in or prose_reason_out
    values = pinned_in | pinned_out | prose_in | prose_out
    if values and not _add_values(values, session, parent):
        reason = reason or "too_many_session_values"
    if reason:
        _taint(reason, session, parent)
    return reason


def _note_body(turn, value, parent=None):
    """Scans text the model or the user wrote (a reply, a summary, a request) before it is kept: a value it names is
    pinned so a later bare repeat in any field is masked, and a body that cannot be pinned is withheld."""
    if value is None:
        return None
    return _note_written(turn["session"], parent if parent is not None else turn["parent"], value)


def _note_written(session, parent, value):
    text = _scan_text(value)
    reason, values = _evidence(text)
    # Prose ("the token endpoint returned 200") pins what it can but does not withhold a reply: a mention is not
    # evidence (TD-07), and an unmarked plain secret is the accepted residual. A value prose names by a separator is
    # pinned even when short or non-ASCII ("the password is qzv", "パスワードは 秘密のもじれつ"), so its bare repeat
    # is masked; only a 1-character one withholds. Assignments and known forms still withhold as before.
    prose, prose_reason = _prose_values(text, reply=True)
    reason = reason or prose_reason
    values = values | prose
    if values and not _add_values(values, session, parent):
        reason = reason or "too_many_session_values"
    if reason:
        _taint(reason, session, parent)
    return reason


def _tool_calls(message):
    """(name, arguments) in any provider shape."""
    if not isinstance(message, dict):
        return
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            yield function.get("name"), function.get("arguments")
    if message.get("type") in ("function_call", "custom_tool_call"):
        yield message.get("name"), message.get("arguments", message.get("input"))
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name"), block.get("input")


def _note_delegated(session):
    with _lock:
        _delegated[str(session)] = True
        _delegated.move_to_end(str(session))
        while len(_delegated) > _MAX_SESSIONS:
            _delegated.popitem(last=False)


def _fingerprint(message):
    try:
        data = json.dumps(message, sort_keys=True, default=repr, ensure_ascii=False)
    except Exception:
        data = repr(message)
    return hashlib.sha256(data.encode("utf-8", "replace")).digest()


def _note_request(session, parent, messages):
    """Scans the messages not scanned yet: tools whose hook did not fire, and history from before a restart."""
    # By content, not by count: compaction can rewrite earlier messages and keep (or grow) the length.
    prints = [_fingerprint(m) for m in messages]
    with _lock:
        seen = _scanned.get(session)
        if seen is None or len(seen) + len(prints) > _MAX_SCANNED_MESSAGES:
            seen = set()
        fresh = [m for m, fp in zip(messages, prints) if fp not in seen]
        seen.update(prints)
        _scanned[session] = seen
        _scanned.move_to_end(session)
        while len(_scanned) > _MAX_SESSIONS:
            _scanned.popitem(last=False)
        if session not in _sessions:
            _sessions[session] = ""
        _touch(session)
        _evict(_sessions)
    for message in fresh:
        # Instructions reach the model like any other text, so a value written there is pinned too. They are not tool
        # output, so describing an environment ("Set PATH=... and HOME=...") is not a dump of it.
        instructions = isinstance(message, dict) and message.get("role") in ("system", "developer")
        if any(_withhold(_text(args)) for _, args in _tool_calls(message)):
            _taint("sensitive_source", session, parent)
        text = _scan_text(message)
        if not instructions and _is_env_dump(text):
            _taint("sensitive_source", session, parent)
        authored, rest = _authored_parts(message)
        # A request, a reply or an instruction is scanned the way _note_body scans it live, so an ordinary sentence
        # ("Your password is too short.") in unobserved history pins what it names but does not withhold the session.
        for part in authored:
            _note_written(session, parent, part)
        if rest is not None:
            _note_evidence(session, parent, "", _scan_text(rest))


def _authored_parts(message):
    """(texts the user, the model or the instructions wrote, everything else). Tool output, tool call arguments and
    unknown shapes stay in the second part, which keeps the stricter tool scan."""
    if not isinstance(message, dict) or message.get("type") or message.get("role") not in _AUTHORED_ROLES:
        return [], message
    content = message.get("content")
    rest = {k: v for k, v in message.items() if k not in ("role", "content")}
    if isinstance(content, str):
        return [content], rest or None
    if not isinstance(content, list):
        return [], message
    authored, other = [], []
    for block in content:
        if isinstance(block, dict) and block.get("type") in _TEXT_BLOCKS and isinstance(block.get("text"), str) \
                and set(block) <= {"type", "text", "cache_control"}:
            authored.append(block["text"])
        else:
            other.append(block)
    if other:
        rest["content"] = other
    return authored, rest or None


def _block_view(block):
    if not isinstance(block, dict):
        return {"omitted": type(block).__name__}
    kind = block.get("type")
    if kind in _TEXT_BLOCKS and isinstance(block.get("text"), str):
        return block["text"]
    if kind == "tool_result":
        return {"tool_result": "\n".join(_strings(block.get("content")))}
    if kind == "tool_use":
        return {"tool_use": block.get("name"), "input": block.get("input")}
    return {"omitted": str(kind)}  # reasoning, images, audio and unknown shapes


def _message_view(message):
    """Role, text, tool calls and tool results (masked later). Reasoning and unknown shapes stay out."""
    if not isinstance(message, dict):
        return {"omitted": type(message).__name__}
    if message.get("type") == "function_call_output":
        return {"role": "tool", "content": "\n".join(_strings(message.get("output")))}
    if message.get("type") in ("function_call", "custom_tool_call"):
        return {"role": "assistant", "tool_calls": [{"name": n, "arguments": a} for n, a in _tool_calls(message)]}
    role = message.get("role")
    if role not in _CHAT_ROLES and role != "tool":
        return {"omitted": str(role or message.get("type"))}
    content = message.get("content")
    if isinstance(content, list):
        content = [_block_view(b) for b in content]
    elif content is not None and not isinstance(content, str):
        content = {"omitted": type(content).__name__}
    view = {"role": role, "content": content}
    calls = [{"name": n, "arguments": a} for n, a in _tool_calls({"tool_calls": message.get("tool_calls")})]
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
    # Hooks run even while the ingest key is absent: the exposure state (taint, pinned values, lineage) has to be
    # tracked from the first turn, or a session that read a secret before the key appeared would send a later
    # verbatim repeat of it unmasked. Only the export is skipped until the key exists (see _on_session_end).
    def wrapper(*args, **kwargs):
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
    _note_parent(turn["session"], parent_session_id)
    if str(parent_session_id or "") in _delegated:  # rotation of a delegated session keeps it delegated
        _note_delegated(turn["session"])
    if _tainted(parent_session_id):  # the delegated goal may repeat what the parent saw
        _taint(_tainted(parent_session_id), turn["session"])
    _add_values(_values_of(parent_session_id), turn["session"])
    if _tainted(turn["session"]):  # the user can quote a reply back
        user_message = _WITHHELD_SESSION
    elif _note_body(turn, user_message, parent_session_id):
        user_message = _WITHHELD_UNPINNED
    turn.update(user_message=user_message, model=model, platform=platform or "", parent=parent_session_id)


@_guarded
def _on_api_request(session_id=None, turn_id=None, api_request_id=None, system_prompt=None, request_messages=None,
                    message_count=None, retry_count=None, started_at=None):
    turn = _turn(session_id, turn_id)
    messages = list(request_messages or [])
    # The assembled system prompt is scanned as an instruction message, once per session while it stays the same.
    _note_request(turn["session"], turn["parent"], ([{"role": "system", "content": system_prompt}] if system_prompt else []) + messages)
    tainted = _tainted(turn["session"])
    first_request = not turn.get("requested")
    turn["requested"] = True
    if tainted and first_request and turn["user_message"] is not None:  # the taint of a resumed session is only known here
        turn["user_message"] = _WITHHELD_SESSION
    tail = _WITHHELD_SESSION if tainted else [_message_view(m) for m in messages[-_LAST_MESSAGES:]]
    _count("bodies_withheld_after_exposure" if tainted else "bodies_sent")
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
    # The reply is kept before the tool hook fires, so a call it makes to a sensitive path (writing .env, reading a
    # key) is checked here too: the call's arguments are the sensitive body.
    sensitive_call = isinstance(message, dict) and any(_withhold(_text(args)) for _, args in _tool_calls(message))
    if sensitive_call:
        _taint("sensitive_source", turn["session"], turn["parent"])
    already = not sensitive_call and _tainted(turn["session"])
    unpinned = not sensitive_call and not already and isinstance(message, dict) and _note_body(turn, _message_view({"role": "assistant", **message}))
    output = (_WITHHELD_TOOL if sensitive_call else _WITHHELD_SESSION if already else _WITHHELD_UNPINNED if unpinned else
              _message_view({"role": "assistant", **message}) if isinstance(message, dict) else None)
    location = "local" if "local-llm" in str(base_url or "") or "llama" in str(base_url or "") else "cloud"
    _add(turn, {"key": "api:" + str(api_request_id), "name": "llm-call", "type": "generation",
                "input": req.get("input"), "output": output, "model": response_model or model, "usage": usage,
                "start": started_at or req.get("started_at"), "end": ended_at,
                "metadata": {"input_scope": f"last {_LAST_MESSAGES} of {req.get('message_count')} messages",
                             "body_policy": ("withheld_sensitive_source" if sensitive_call else "withheld_after_exposure" if already
                                             else "withheld_unpinned" if unpinned else "sent"),
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


def _size(value):
    text = _text(value)
    return {"chars": len(text), "lines": text.count("\n") + 1 if text else 0}


def _tool_input_view(args):
    """What was done without the content: argument names, a path, and the program a command runs."""
    if not isinstance(args, dict):
        return {"omitted": type(args).__name__, **_size(args)}
    view = {"arg_names": sorted(str(k) for k in args)}
    for name in _PATH_ARGS:
        if isinstance(args.get(name), str):
            view[name] = args[name]
    for name in _COMMAND_ARGS:
        found = _COMMAND_HEAD.match(args[name].strip()) if isinstance(args.get(name), str) else None
        if found:
            view["program"] = found.group(1)
    return view


@_guarded
def _on_tool(session_id=None, turn_id=None, tool_call_id=None, tool_name=None, args=None, result=None, duration_ms=None,
             status=None, error_type=None):
    turn = _turn(session_id, turn_id)
    if tool_name in _DELEGATING_TOOLS:
        turn["delegated"] = True
    _note_tickets(turn, args)
    _note_tickets(turn, result)
    args_text = _scan_text(args)
    result_text = _scan_text(result)
    already = _tainted(turn["session"])
    withheld = _withhold(_text(args)) or _is_env_dump(result_text)
    exposure = None
    if withheld:
        _taint("sensitive_source", turn["session"], turn["parent"])
        policy = "withheld_sensitive_source"
    else:
        exposure = _note_evidence(turn["session"], turn["parent"], args_text, result_text)
        # A reason means this body holds something that could not be pinned to a value (too large to scan, a short
        # value, a private key, ...): masking cannot cover the rest of it, so this body is withheld too.
        policy = "withheld_after_exposure" if already else "withheld_unpinned" if exposure else "sent"
    _count("bodies_" + policy)
    now = time.time()
    hidden = (_WITHHELD_TOOL if withheld else _WITHHELD_SESSION if already else
              _WITHHELD_UNPINNED if exposure else None)
    _add(turn, {"key": "tool:" + str(tool_call_id or now), "name": str(tool_name), "type": "tool",
                "input": hidden or args, "output": hidden or result,
                "start": now - (duration_ms or 0) / 1000, "end": now,
                "level": "ERROR" if error_type or status in ("error", "failed") else None,
                "metadata": {"status": status, "error_type": error_type, "withheld": bool(hidden), "body_policy": policy,
                             "exposure": exposure, "view": _tool_input_view(args) if hidden in (_WITHHELD_SESSION, _WITHHELD_UNPINNED) else None,
                             "size": _size(result)}})


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
        _note_parent(child_session_id, parent_session_id)


@_guarded
def _on_subagent(parent_session_id=None, parent_turn_id=None, child_session_id=None, child_role=None, child_summary=None,
                 child_status=None, duration_ms=None):
    turn = _turn(parent_session_id, parent_turn_id)
    turn["delegated"] = True
    _note_tickets(turn, child_summary)
    if _tainted(child_session_id):
        _taint(_tainted(child_session_id), turn["session"], turn["parent"])
    _add_values(_values_of(child_session_id), turn["session"], turn["parent"])
    if _tainted(turn["session"]):
        child_summary = _WITHHELD_SESSION
    elif _note_body(turn, child_summary):
        child_summary = _WITHHELD_UNPINNED
    now = time.time()
    _add(turn, {"key": "subagent:" + str(child_session_id), "name": "subagent", "type": "agent",
                "output": child_summary, "start": now - (duration_ms or 0) / 1000, "end": now,
                "metadata": {"child_session_id": child_session_id, "child_role": child_role, "status": child_status}})


@_guarded
def _on_turn_end(session_id=None, turn_id=None, assistant_response=None):
    turn = _turn(session_id, turn_id)
    _note_tickets(turn, assistant_response)
    if _tainted(turn["session"]):
        assistant_response = _WITHHELD_SESSION
    elif _note_body(turn, assistant_response):
        assistant_response = _WITHHELD_UNPINNED
    turn["output"] = assistant_response


@_guarded
def _on_session_end(session_id=None, turn_id=None, completed=None, failed=None, interrupted=None, turn_exit_reason=None):
    with _lock:
        turn = _turns.pop(_key(session_id, turn_id), None)
    if turn is None:
        return
    turn["status"] = "interrupted" if interrupted else "failed" if failed else "succeeded" if completed else "unknown"
    turn["exit_reason"] = turn_exit_reason
    turn["tainted"] = _tainted(turn["session"])
    turn["values"] = _values_of(turn["session"], turn["parent"])
    if not in_scope(turn):
        _count("turns_out_of_scope")
        return
    if _creds is None:  # nothing to send yet; the state above is kept so later turns are masked/withheld
        _count("turns_skipped_no_key")
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
    extra = _values_pattern(turn.get("values"))

    def _field(value):
        return globals()["_field"](value, extra)

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
        _attr("langfuse.trace.metadata.coverage.tool_bodies", "all tools, masked and truncated; withheld only for a sensitive source or after a sensitive value reached the model (body_policy)"),
        _attr("langfuse.trace.metadata.coverage.withheld_reason", turn.get("tainted") or "not_withheld"),
        _attr("langfuse.trace.metadata.coverage.session_values_masked", str(len(turn.get("values") or ()))),
        _attr("langfuse.trace.metadata.coverage.truncated", str(turn["truncated"]).lower()),
        _attr("langfuse.trace.metadata.coverage.withheld_after_tool_output", str(bool(turn.get("tainted"))).lower()),
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


def guarded_payloads(spans, extra=None):
    """Yield only payloads that pass the final guard; a failing span is dropped alone."""
    for batch in batches(spans):
        body = _payload(batch)
        if not _unsafe(body, extra):
            yield body
            continue
        kept = [s for s in batch if not _unsafe(json.dumps(s, ensure_ascii=False), extra)]
        _count("spans_dropped_secret_guard", len(batch) - len(kept))
        if kept:
            body = _payload(kept)
            if not _unsafe(body, extra):
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
    for body in guarded_payloads(build_spans(turn), _values_pattern(turn.get("values"))):
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
