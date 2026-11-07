"""Hermes -> in-cluster Langfuse observer (BOXP-428).

Scope: only delegation paths the Owner selected. A turn is buffered in memory and sent when it
ends, and only if it is a Kohako G2 session, a webhook (Task Board event) session, or it delegated,
was delegated (subagent_start, not the parent id, which session rotation also sets) or referred to a
ticket. Other conversation is dropped and only counted. Audio and images never
reach this module: no transcription hook is registered and data URIs are removed.

Bodies (the observation contract the Owner settled, see THREAT_MODEL.md next to this file): the
request, every model reply, the input and output of every tool and the delegation texts are sent,
so that a trace can be read. Using a tool is not a reason to hide anything. What is guaranteed to
stay out, and how:

K0 known sources: a call whose arguments name a credential file, a secret-printing command or an
environment dump (_SENSITIVE_PATH, _SENSITIVE_COMMAND, _is_env_dump) sends neither its arguments nor
its result, and the session and its delegating parent and children send no later body. The tables
are fixed lists and end here; a spelling of the same command that escapes them is an accepted gap.
K1 known formats: every string goes through redact() (_PATTERNS, element, assignment, the whole line
around a secret word and the lines next to it) before it is queued, and the serialized batch is
scanned again (_unsafe) before sending; a hit there drops the span. When a tool result carried a
credential-shaped known format (_EXPOSURE_KINDS: private key, header, URL credentials, JWT, bearer,
provider/cloud keys, this process's environment values), later bodies of the session are withheld
too. A name-based hit (assignment, env entry, flag, cli password) does not withhold: the value it
names (4..256 characters, _named_values) is pinned to the session lineage and masked in every field
of its turns, escapes included, and the final guard rejects a span that still holds it, so a bare
restatement in a later reply is not sent. A value that cannot be pinned (shorter, longer, or more
than 64 in one text) in a tool result withholds the lineage; over 256 pinned values per lineage
withholds it too (pinned_values_over_limit).
Not guaranteed: a plain value without a secret word or known format near it, a transformed or partial
restatement (base64, reversal...), a restatement in an unrelated session, and a short named value in a
prompt or reply. The Owner accepted these for a self-hosted Langfuse behind Owner-only Access with
short retention.

State: a session's withholding reason and pinned values are kept in memory (_sessions, _pins, bounded,
oldest evicted). A session reads them through its recorded parents (_parents), and a reason or a value
found in a child reaches its ancestors at once. History that
this process did not observe is scanned for the same K0 evidence and K1 formats in tool results; a
tool name in it, or the fact that it was not observed, withholds nothing.

Key: read from an optional Secret volume, polled by the worker. It can appear or rotate while the
Pod runs. Until then withholding state is still tracked but nothing is buffered or sent. Payloads
are built and masked inside on_session_end, so the queue only ever holds masked batches.

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
import shlex
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
_WITHHELD_SESSION = "[withheld: the session saw a known source or a known format]"
# A tool result longer than this is not scanned; its body is withheld and the session with it (fail-closed).
_MAX_SCAN_BYTES = 1024 * 1024
_MAX_SCANNED_HASHES = 2048

# A value can sit inside serialized text at any depth, so a quote can carry escapes. A value ends at
# a quote of its own depth, and one that never closes runs to the end of the line.
_QUOTE = r"""(?:\\*["']|\\+u00(?:22|27)|&quot;|&apos;|&\#0*(?:34|39);|&\#x0*(?:22|27);)"""
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
    # A self-closing opening tag (within 512 characters) starts no element, so <password/><password>x</password>
    # yields the second one; a longer opening tag is treated as an element (fail closed).
    + r"(?!(?:(?!" + _GT + r").){0,512}?[/\\]\s*" + _GT + r")"
    + r".*?(?:" + _LT + r"\\*/(?P=tag)\s*" + _GT + r"|\Z)")


def _value(group, limit="", bare=r"[^\s,;}\]]+"):
    # limit bounds a value that has to be followed by more of the pattern.
    scan = limit or "*?"
    return (r"(?:(?P<" + group + ">" + _QUOTE + r")(?:[^\n]" + scan + r"(?<!\\)(?P=" + group + r")"
            + ("" if limit else r"|[^\n]*") + r")|" + bare + ")")


# A key may follow a \u0022 quote escape (\u0022value\u0022:), whose last character is a word character.
_KEY_START = r"(?:(?<![\w-])|(?<=\\u00(?:22|27)))"


def _entry_key(name):
    return _KEY_START + name + _QUOTE + r"?" + _GAP + r"[:=]" + _GAP



# One unquoted shell word, whose spaces may be escaped (apple\ orchard, apple\\ orchard in JSON). A backslash run
# is tried as an escape only from its start, so a long run stays linear.
_SHELL_WORD = r"(?:(?<!\\)\\++[ \t]|\S)++"

_SECRET_NAME = re.compile(
    r"(?i)(password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|credential|cookie|auth(?!ors?(?![a-z]))|(?<![a-z])pass(?![a-z]))")
_NOT_A_CREDENTIAL_NAME = re.compile(r"PWD|OLDPWD")
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
    ("flag", re.compile(r"(?i)(?<![\w-])--?" + _SECRET_KEY + r"[ \t]+(?!-)" + _value("quote", bare=_SHELL_WORD))),
    # A CLI password is one shell word: a quoted value runs to its matching quote (_value), so
    # sshpass -p 'apple orchard' masks and pins the whole value, not just the part before the space. A quote that
    # never closes (-u 'admin:apple orchard) runs to the end of the line.
    ("cli_password", re.compile(
        r"(?i)(?<![\w-])(?:-u|--user)[ \t=]*(?P<uq>" + _QUOTE + r")(?!\d+:\d+\b)[^\s:]*:(?:[^\n]*?(?<!\\)(?P=uq)|[^\n]*)")),
    ("cli_password", re.compile(
        r"(?i)(?<![\w-])(?:-u|--user)[ \t=]*(?!" + _QUOTE + r")(?!\d+:\d+\b)[^\s:]+:" + _value("quote", bare=_SHELL_WORD))),
    ("cli_password", re.compile(
        r"(?i)\b(?:mysql\w*|mariadb\w*)\b[^\n|;&]{0,512}?\s-p" + _value("quote", bare=_SHELL_WORD))),
    ("cli_password", re.compile(
        r"(?i)\bsshpass\b[^\n|;&]{0,512}?\s-p[ \t]*" + _value("quote", bare=_SHELL_WORD))),
    ("assignment", re.compile(
        r"(?i)[\"']?" + _SECRET_KEY + _QUOTE + r"?" + _GAP + _SEPARATOR + _GAP
        # A YAML block scalar carries its value on the indented lines below the key.
        + r"(?:[|>][-+]?\d?(?:(?:\n|\\+n)[ \t]+(?:(?!\\+n)[^\n])*)+|" + _value("quote") + r")")),
    ("data_uri", re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.I)),
    ("entropy", re.compile(r"(?<![A-Za-z0-9_/+=-])[A-Za-z0-9_/+=-]{40,}(?![A-Za-z0-9_/+=-])")),
]
_FIELD_ONLY = {"assignment", "entropy", "data_uri", "env_entry", "flag", "cli_password"}
# A hit of one of these in a tool result means the model saw a credential: later bodies of the session are withheld.
# A secret word alone (secret_line), a high-entropy string or an image is not such evidence.
# Only shapes that are credentials by themselves count. Name-based kinds (assignment, env entry, flag, cli password,
# XML element) mask the value in place and pin it (_named_values) but are no evidence: they match ordinary code and logs such as
# TOKEN_FILE=/work/x or credentials: 'same-origin', which withheld whole sessions in production (BOXP-428).
_EXPOSURE_KINDS = {"private_key", "header", "url_credentials", "jwt", "provider_key", "aws_key", "google_key",
                   "telegram", "bearer", "env"}
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
# K0 table, part 1: files that hold credentials. The list is fixed; THREAT_MODEL.md names what it does not cover.
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|[/\\\s\"'=])(?:"
    r"auth\.json|\.credentials[^/\s\"']*|\.env(?:\.[\w-]+)?|\.ssh(?:[/\\]|\b)|\.aws(?:[/\\]|\b)|kubeconfig|\.kube[/\\]config"
    # id_*: every SSH-style key name as a whole path element (id_rsa, id_ed25519_sk, id_xmss ...). user_id_map / id_12345 are not.
    r"|\.git-credentials|\.netrc|id_[a-z][\w-]*(?![\w-])|\.gnupg(?:[/\\]|\b)|secrets?(?:[/\\])"
    # token files: tokens.json, refresh_token.txt, /var/lib/app/tokens. A bare word such as max_output_tokens is not a file.
    r"|[\w.-]{0,64}tokens?[\w.-]{0,64}\.[a-z0-9]{1,8}(?![\w.-])"
    r"|[\w.-]{0,64}\.(?:key|pem|p8|p12|pfx|jks|keystore|ppk)(?![\w.-])"
    # per-tool credential files of common CLIs
    r"|\.npmrc|\.pypirc|\.pgpass|\.my\.cnf|\.docker[/\\]config\.json|\.boto|\.s3cfg|\.vault-token|\.terraformrc|\.htpasswd"
    r"|\.config[/\\]gh[/\\]hosts\.yml|\.config[/\\]gcloud(?:[/\\]|\b)|\.azure(?:[/\\]|\b)|\.cargo[/\\]credentials[^/\s\"']*|\.gem[/\\]credentials"
    r")|[/\\][\w.-]{0,64}tokens?[\w.-]{0,64}(?=[/\\\s\"']|$)")
# Arguments are matched in their JSON form, where a newline is a backslash and an n.
_WORD_START = r"""(?:^|[;&|\s"'(`/]|\\[nrt])"""
_WORD_END = r"""[ \t]*(?:$|[|;&<>"')`#]|\d+[<>]|\\[nrt"])"""
_ASSIGNMENT = r"""[ \t]+[A-Za-z_]\w*=(?:\\?"[^"]*"|'[^']*'|[^\s;&|)`"'\\]*)"""
# `env NAME=value` with no command to run, or `env` with an option, prints the environment.
_ENV_DUMP = _WORD_START + r"(?:env(?:[ \t]+-|(?:" + _ASSIGNMENT + r")*" + _WORD_END + r")|(?:set|export)" + _WORD_END + r")"
_SENSITIVE_COMMAND = re.compile(
    r"(?i)\baws\b[^|;&\n]{0,512}\b(?:ssm\s+get-parameter|secretsmanager\s+get-|configure\s+(?:get|export-credentials)"
    r"|sts\s+(?:get-session-token|assume-role\S*|get-federation-token)|ecr\s+get-login-password|iam\s+create-access-key)"
    # K0 table, part 2: commands that print a credential. The list is fixed; THREAT_MODEL.md names what it does not cover.
    r"|\bgcloud\b[^|;&\n]{0,512}\b(?:auth\s+(?:print-(?:access|identity)-token|application-default\s+print-access-token)|secrets\s+versions\s+access)"
    r"|\baz\b[^|;&\n]{0,512}\b(?:account\s+get-access-token|keyvault\s+secret\s+show)"
    r"|\bkubectl\b[^|;&\n]{0,512}\bcreate\s+token\b|\boc\s+whoami\s+-t\b|\bvault\s+(?:read|kv\s+get|token\s+(?:create|lookup)|login)\b"
    r"|\bop\s+(?:read|item\s+get)\b|\bdocker-credential-\w+"
    r"|\bhelm\s+get\b|\bgh\s+auth\s+token\b|\bgit\s+credential\b|langfuse-ingest")
_CLUSTER_CLI = re.compile(r"(?i)\b(?:kubectl|oc)\s")
# K0 table, part 1b: a token file named without an extension or a directory (tokens, mytoken, access_tokens_backup).
# Such a word is a file only in a reading position: the value of a path argument, or an operand of a program that
# reads its operands. Programs that only name files (ls, grep, git add ...) and options (--max-output-tokens) are not.
_TOKEN_WORD = re.compile(r"(?i)[\w.-]{0,64}tokens?[\w.-]{0,64}")
_TOKEN_HINT = re.compile(r"(?i)token")
_PATH_ARG = re.compile(r'"(?:file_?path|path|file|filename|target_file|notebook_path)"\s*:\s*("(?:[^"\\]|\\.)*")')
_COMMAND_ARG = re.compile(r'"(?:command|cmd|script|code)"\s*:\s*("(?:[^"\\]|\\.)*")')
_MENTION_PROGRAMS = {"echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "ack", "ls", "find", "fd", "rm", "mv", "cp", "mkdir",
                     "rmdir", "touch", "chmod", "chown", "test", "[", "stat", "file", "which", "type", "du", "tree", "basename", "dirname",
                     "realpath", "readlink", "cd", "pushd", "true", "false", ":", "wc", "ln"}
_PREFIX_PROGRAMS = {"sudo", "command", "time", "nice", "nohup", "exec", "xargs", "doas"}
# git / gh print file contents only through these subcommands; others (add, commit, log, status ...) name files.
_READING_SUBCOMMANDS = {"git": {"show", "cat-file", "diff", "blame", "grep", "archive"}, "gh": {"api", "gist"}}  # git log only with -p
_SHELL_FLAG = re.compile(r"-[A-Za-z]*c$")
_MAX_TOKEN_SCAN = 4096
_ENV_OUTPUT = [re.compile(r"(?:^|[^A-Za-z0-9_]|\\[nrt]|\\u0000)" + name + "=") for name in ("PATH", "HOME")]

_lock = threading.Lock()
_turns = {}
_sessions = collections.OrderedDict()  # session id -> why its bodies are withheld, or None
_pins = collections.OrderedDict()  # session -> distinct named values
_parents = collections.OrderedDict()  # session -> parent
_delegated = collections.OrderedDict()  # session ids started by delegation
_scanned = collections.OrderedDict()  # session id -> digests of history tool messages already scanned
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

def _is_setting(name, value):
    """The value and the name have to agree: PASSWORD=disabled is a password, AUTH_ENABLED=disabled is a setting."""
    words = [word for word in re.split(r"[^A-Za-z0-9]+", name) if word]
    if not words:
        return False
    if _FLAG_VALUE.fullmatch(value):
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


def _value_pattern(value):
    """Every position can start a value, so its first character is only tried at the start of a run of backslashes: the scan stays linear."""
    chars = [_encoded_char(ch) for ch in value]
    chars[0] = chars[0].replace(r"\\{1,255}", r"(?<!\\)\\+")
    return "".join(chars)


def _env_pattern():
    """One alternation, longest first, so a later value cannot rewrite the mark of an earlier one.
    Any length matches inside a longer word: the value can be part of it, and a blanked word costs less than a leak."""
    global _env_pattern_cache
    values = _env_secrets()
    if _env_pattern_cache[0] is not values:
        pattern = re.compile("|".join(map(_value_pattern, values))) if values else None
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


_PIN_KINDS = {"assignment", "env_entry", "flag", "cli_password"}
# A redaction placeholder is never a value. A $VAR / ${VAR} form may be a variable reference or a real password
# that starts with $ (password: $hunterhorse): it is pinned like any value (masking a later ${VAR} is a harmless
# over-mask), but when it is too short to pin it does not withhold, since it is most likely a reference.
_REFERENCE = re.compile(r"\[REDACTED:[a-z_-]+\]", re.I)
_VARIABLE_LIKE = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_NAME_SEPARATOR = re.compile(r"(?s)^(.*?)(?:[:=]|\\+u00(?:3[aA]|3[dD]))")
# The value key takes every serialized quote form (&quot;value&quot;:, \u0022value\u0022:), as _entry_key does.
_ENTRY_VALUE_START = re.compile(r"(?i)" + _entry_key("value"))


_OPENING_QUOTE = re.compile(_QUOTE, re.I)
_UNQUOTED_CHARS = (
    r"""[^\s,;}\]"'\\&]|\\\\|\\(?![nrt"'\\]|u00(?:22|27))|&(?!quot|apos|\#0*(?:34|39)(?!\d)|\#x0*(?:22|27)(?![0-9a-f]))"""
)
_UNQUOTED_VALUE = re.compile(r"(?i)(?:" + _UNQUOTED_CHARS + r")+")
# A CLI password or flag value is a shell word: an escaped space (apple\\ orchard) does not end it.
_UNQUOTED_SHELL_VALUE = re.compile(r"(?i)(?:(?<!\\)\\++[ \t]|" + _UNQUOTED_CHARS + r")++")
_ESCAPED_SPACE = re.compile(r"(?<!\\)\\++([ \t])")


def _closing_quote(text, q):
    """Index in text where the value opened by quote token q ends, or None when it never closes.

    A quote is escaped by the backslash run before it. A plain quote (n backslashes in q, n=0 at the top level, 1 for
    \\" inside a JSON string, 3 a layer deeper) closes when that run is a multiple of 2*(n+1): each backslash of the
    value is written as n+1 backslashes at that depth, and an escaped quote adds an odd count of them. Other quote
    forms (\\u0022, &quot;) close after an even run. The value keeps the escaped backslashes: "hunterhorse\\\\"
    ends after hunterhorse\\\\ (decoded hunterhorse\\).
    """
    plain = re.fullmatch(r"(\\*)[\"']", q)
    unit = 2 * (len(plain.group(1)) + 1) if plain else 2
    for m in re.finditer(r"(?<!\\)(\\*)" + re.escape(q), text, re.I):
        if len(m.group(1)) % unit == 0:
            return m.start() + len(m.group(1))
    return None


def _shell_values(text):
    """A CLI password or flag value as written and, when it escapes spaces, as the shell decodes it (apple orchard)."""
    stripped = text.lstrip()
    if _OPENING_QUOTE.match(stripped) or _PLACEHOLDER.match(stripped):
        return [_first_value(text)]
    match = _UNQUOTED_SHELL_VALUE.match(stripped)
    word = match.group(0).strip() if match else ""
    decoded = _ESCAPED_SPACE.sub(r"\1", word)
    return [word, decoded] if decoded != word else [word]


def _first_value(text):
    text = text.lstrip()
    placeholder = _PLACEHOLDER.match(text)
    if placeholder:
        return placeholder.group(0)
    # Quotes include their serialized forms (\\", \\u0022, &quot;, &#34;, &#x22;, &apos;) as the in-place mask
    # (_QUOTE) matches them: the value ends at the same quote token.
    quote = _OPENING_QUOTE.match(text)
    if quote:
        q = quote.group(0)
        # A quote token preceded by an escaping backslash run does not end the value (password: "a\"b" captures a\"b
        # and its decoded form a"b), while one preceded by escaped backslashes does (password: "hunterhorse\\"
        # captures hunterhorse\\ and its decoded form hunterhorse\).
        end = _closing_quote(text[len(q):], q)
        return text[len(q):len(q) + end if end is not None else None].strip()
    # Unquoted: stop at a quote, an entity quote or a quote/line escape (\", \u0022, \n, \r, \t) too, so a value
    # inside serialized JSON or HTML does not take the closing quote or a \n escape with it. Any other backslash
    # (password: \mysecret123, its JSON form \\mysecret123, my\secret) is part of the value, not its end.
    match = _UNQUOTED_VALUE.match(text)
    return match.group(0).strip() if match else ""


_TAG_CLOSE = re.compile(_GT, re.I)
_ELEMENT_CLOSE = re.compile(r"(?is)^(.*)" + _LT + r"\\*/[\w:.-]+\s*" + _GT + r"$")
_ELEMENT_SPLIT = re.compile(r"(?i)" + _LT + r"[^<>&\\]{0,256}?" + _GT + r"|\r?\n|\\+[nrt]")


def _element_values(raw):
    # Text inside an element whose tag names a secret (<password>x</password>, encoded tags too). The element is
    # masked in place; its text is pinned like an assignment value so a bare restatement is masked as well. Child
    # tags and line breaks separate values; a self-closing tag names nothing.
    closer = _TAG_CLOSE.search(raw)
    if not closer or (closer.start() <= 512 and re.search(r"[/\\]\s*$", raw[:closer.start()])):
        return []
    inner = raw[closer.end():]
    closing = _ELEMENT_CLOSE.match(inner)
    if closing:
        inner = closing.group(1)
    values = []
    for segment in _ELEMENT_SPLIT.split(inner):
        value = re.sub(r"^(?:\\*[\"'])+|(?:\\*[\"'])+$", "", segment.strip()).strip()
        if value:
            values.append(value)
    return values


# A value captured from a serialized body keeps its escapes (password: &quot;a&amp;b&quot; captures a&amp;b), while a
# later restatement can be the decoded text (a&b). Both are pinned: the decoded form is matched with its own escapes,
# so pinning it covers the plain restatement and every serialization of it.
_ENTITY_ESCAPE = re.compile(r"&(?:#0*(\d{1,7})|#[xX]0*([0-9a-fA-F]{1,6})|(amp|lt|gt|quot|apos));")
_JSON_ESCAPE = re.compile(r"""\\(?:u([0-9a-fA-F]{4})|(["'\\/bfnrt]))""")
_ENTITY_NAMES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}
_JSON_CHARS = {'"': '"', "'": "'", "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _code_point(n):
    return chr(n) if 1 <= n <= 0x10FFFF else None


def _decode_entity(m):
    if m.group(1):
        return _code_point(int(m.group(1))) or m.group(0)
    if m.group(2):
        return _code_point(int(m.group(2), 16)) or m.group(0)
    return _ENTITY_NAMES[m.group(3)]


def _decode_json(m):
    if m.group(1):
        return chr(int(m.group(1), 16))
    return _JSON_CHARS[m.group(2)]


def _join_surrogates(text):
    # JSON writes a non-BMP character as a surrogate pair (\ud83d\ude00); the decoded form joins the pair into the
    # character (U+1F600) a restatement uses. A lone surrogate stays as it is.
    if not any("\ud800" <= ch <= "\udfff" for ch in text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def _decoded_forms(value):
    """value and its decoded forms (HTML entities and JSON escapes, up to 3 nested layers)."""
    forms = [value]
    for _ in range(3):
        decoded = _join_surrogates(_JSON_ESCAPE.sub(_decode_json, _ENTITY_ESCAPE.sub(_decode_entity, value)))
        if decoded == value:
            break
        forms.append(decoded)
        value = decoded
    return forms


# A list or object value (password: [a, b], token: {primary: a}) is masked in place as a whole (_mask_structured);
# each element is pinned too. Quoted elements keep their commas; a key followed by : or = names, not a value.
_COLLECTION_GAP = re.compile(r"(?:[\s,;\[\]{}()]++|\\++[nrt](?![A-Za-z0-9]))++")
_KEY_SEPARATOR = re.compile(r"\s*(?:[:=]|\\+u00(?:3[aA]|3[dD]))")


def _collection_values(text):
    is_object = text.lstrip().startswith("{")
    values, tokens = [], 0
    while True:
        gap = _COLLECTION_GAP.match(text)
        if gap:
            text = text[gap.end():]
        if not text.strip() or len(values) > 64:
            return values
        if tokens > 256:
            # Past 256 tokens (mostly keys) the rest is one value, which unpins it when long (fail closed).
            values.append(text)
            return values
        tokens += 1
        quote = _OPENING_QUOTE.match(text)
        value = _first_value(text)
        if quote:
            q = quote.group(0)
            closed = _closing_quote(text[len(q):], q)
            consumed = len(q) + closed + len(q) if closed is not None else len(text)
        else:
            value = re.split(r"[\[\]{}()]", value, maxsplit=1)[0]
            consumed = len(value)
        consumed = max(1, min(consumed, len(text)))
        text = text[consumed:]
        separator = _KEY_SEPARATOR.match(text)
        if separator:
            text = text[separator.end():]
            continue
        if not quote and re.search(r"[:=]$", value):
            continue
        if not quote:
            # key:value with no space after the separator. Inside an object only the value is a value; in a list
            # ([{primary:a}], [a:b]) the token may be an object nested in the list or a literal, so pin both.
            pair = re.match(r"^[\w.-]+[:=](.+)$", value)
            if pair:
                if not is_object and value.strip():
                    values.append(value.strip())
                value = pair.group(1)
        if value.strip():
            values.append(value.strip())


def _named_values(text):
    found, unpinned = _named_values_raw(text)
    decoded = {form for value in found for form in _decoded_forms(value)}
    if len(decoded) > 64:
        return {v for v in decoded if 4 <= len(v) <= 256}, True
    return {v for v in decoded if 4 <= len(v) <= 256}, unpinned or any(
        not 4 <= len(v) <= 256 and not (len(v) < 4 and _VARIABLE_LIKE.fullmatch(v)) for v in decoded)


def _reversed_entry_values(text):
    """Elements of list/object values placed before a secret name ({"value": [a, b], "name": "API_SECRET"}), as
    _mask_reversed_entries masks them in place. Bounded by the same limits; past them the field is withheld."""
    values = []
    position = 0
    for scanned, match in enumerate(_ENTRY_VALUE.finditer(text)):
        if scanned >= _MAX_REVERSED_ENTRIES:
            break
        if match.start() < position:
            continue
        end = _bracket_end(text, match.end(), match.end() + _MAX_REVERSED_VALUE)
        if _ENTRY_NAME_AFTER.match(text, end) is None:
            continue
        values.extend(_collection_values(text[match.end():end]))
        position = end
    return values


def _named_values_raw(text):
    text = _PLACEHOLDER.sub("", text)
    found = set()
    for match in _SECRET_ELEMENT.finditer(text):
        for value in _element_values(match.group(0)):
            if not _REFERENCE.fullmatch(value):
                found.add(value)
                if len(found) > 64:
                    return found, True
    for value in _reversed_entry_values(text):
        if value and not _REFERENCE.fullmatch(value):
            found.add(value)
            if len(found) > 64:
                return found, True
    for kind, pattern in _PATTERNS:
        if kind not in _PIN_KINDS:
            continue
        covered = 0
        for match in pattern.finditer(text):
            raw = match.group(0)
            values = []
            if kind == "assignment":
                if match.start() < covered:
                    # Inside a list/object value already scanned below: its elements are pinned there, and skipping
                    # keeps unclosed brackets (password: [password: [ ...) linear.
                    continue
                sep = _NAME_SEPARATOR.match(raw)
                if not sep:
                    continue
                name = re.sub(r"[\"'\\\s]", "", sep.group(1))
                tail = raw[sep.end():]
                position = match.start() + sep.end()
                while position < len(text) and text[position] in " \t":
                    position += 1
                if position < len(text) and text[position] in "[{(":
                    # The value runs to the matching close (or the end of text, fail closed), as _mask_structured
                    # masks it.
                    covered = max(match.end(), _bracket_end(text, position))
                    values = _collection_values(text[position:covered])
                elif re.match(r"\s*[|>]", tail):
                    values = [line.strip() for line in _LINE_BREAK.split(tail)[1:]]
                    values = [_first_value(v) if re.match(r"\\*[\"']", v) else v for v in values]
                else:
                    value = _first_value(tail)
                    if not _is_setting(name, value):
                        values = [value]
            elif kind == "env_entry":
                if match.start() < covered:
                    continue
                entry = _ENTRY_VALUE_START.search(raw)
                if entry:
                    position = match.start() + entry.end()
                    while position < len(text) and text[position] in " \t":
                        position += 1
                    if position < len(text) and text[position] in "[{(":
                        # "value": ["a", "b"]: the value runs to the matching close (or the end of text, fail
                        # closed), as _mask_structured masks it, and every element is pinned.
                        covered = max(match.end(), _bracket_end(text, position))
                        values = _collection_values(text[position:covered])
                    else:
                        values = [_first_value(raw[entry.end():])]
            elif kind == "flag":
                if match.start() < covered:
                    continue
                head = re.match(r"^\S+[ \t]+", raw)
                tail = raw[head.end():] if head else raw
                position = match.start() + (head.end() if head else 0)
                if position < len(text) and text[position] in "[{(":
                    # --password [a, b]: like an assignment, the value runs to the matching close (or the end of
                    # text, fail closed), as _mask_structured masks it, and every element is pinned.
                    covered = max(match.end(), _bracket_end(text, position))
                    values = _collection_values(text[position:covered])
                else:
                    values = _shell_values(tail)
            else:
                if re.match(r"(?i)^(?:-u|--user)", raw):
                    rest = re.sub(r"(?i)^(?:-u|--user)[ \t=]*", "", raw)
                    if _OPENING_QUOTE.match(rest):
                        # -u 'user:pass word': the whole quoted word is user:password.
                        word = _first_value(rest)
                        values = [word.split(":", 1)[1]] if ":" in word else []
                    else:
                        values = _shell_values(re.sub(r"^[^\s:]+:", "", rest))
                else:
                    values = _shell_values(re.sub(r"(?is)^.*?\s-p[ \t]*", "", raw))
            for value in values:
                if value and not _REFERENCE.fullmatch(value):
                    found.add(value)
                    if len(found) > 64:
                        return found, True
    return found, False


def _lineage(session):
    seen = set()
    while session and session not in seen:
        seen.add(session)
        session = _parents.get(session)
    return seen


def _link_parent(session, parent):
    if session and parent:
        with _lock:
            _parents[str(session)] = str(parent)
            _parents.move_to_end(str(session))
            while len(_parents) > _MAX_SESSIONS:
                _parents.popitem(last=False)


_pinned_digests = collections.OrderedDict()  # (session, digest) of bodies already pinned: history is resent on every request


def _pin_body(session, parent, body):
    _link_parent(session, parent)
    text = _text(body)
    if not text:
        return False
    key = (str(session or ""), hashlib.sha256(text.encode("utf-8", "replace")).hexdigest())
    with _lock:
        if key in _pinned_digests:
            unpinned, pinned = _pinned_digests[key]
            if not pinned or key[0] in _pins:
                _pinned_digests.move_to_end(key)
                return unpinned
            del _pinned_digests[key]
    values, unpinned = _named_values(text)
    if values:
        overflow = False
        with _lock:
            for node in _lineage(str(session or "")):
                current = _pins.setdefault(node, set())
                current.update(values)
                if len(current) > 256:
                    overflow = True
                    current.clear()
                _pins.move_to_end(node)
            while len(_pins) > _MAX_SESSIONS:
                evicted, _ = _pins.popitem(last=False)
                # The digest cache says "already pinned" only while the pins exist: once a session's pins are evicted,
                # its cached digests are dropped so a resent history pins its values again.
                for cached in [k for k in _pinned_digests if k[0] == evicted]:
                    del _pinned_digests[cached]
        if overflow:
            _taint("pinned_values_over_limit", *_lineage(str(session or "")))
    with _lock:
        _pinned_digests[key] = (unpinned, bool(values))
        while len(_pinned_digests) > 8 * _MAX_SESSIONS:
            _pinned_digests.popitem(last=False)
    return unpinned


def _pin_pattern(session):
    with _lock:
        values = set().union(*(_pins.get(node, set()) for node in _lineage(str(session or ""))))
    return re.compile("|".join(map(_value_pattern, sorted(values, key=len, reverse=True)))) if values else None


def _mask_value(value, pattern):
    if isinstance(value, str):
        return pattern.sub("[REDACTED:pinned]", value)
    if isinstance(value, list):
        return [_mask_value(item, pattern) for item in value]
    if isinstance(value, dict):
        return {key: _mask_value(item, pattern) for key, item in value.items()}
    return value


def _mask_pins(spans, pattern):
    """Mask the span name and attribute values only: ids, times and attribute keys are the exporter's own."""
    return [{**span, "name": _mask_value(span.get("name"), pattern),
             "attributes": [{**a, "value": _mask_value(a.get("value"), pattern)} for a in span.get("attributes", [])]}
            for span in spans]


def _pinned_text(span):
    return _PLACEHOLDER.sub("", json.dumps([span.get("name"), [a.get("value") for a in span.get("attributes", [])]],
                                           ensure_ascii=False))


# Documentation placeholders (Authorization: Bearer <token>, Bearer YOUR_API_TOKEN, postgres://user:password@db) are masked
# in place but are no evidence that the model saw a credential (BOXP-428 TD-12).
_PLACEHOLDER_CREDENTIAL = re.compile(
    r"(?i)[$<{%*\[].*|x+|\.+|pass(?:word|wd|phrase)?\d?|pwd|secret|(?:api[_-]?|access[_-]?|auth[_-]?)?(?:key|token)|changeme"
    r"|your[\w-]*|my[\w-]*pass\w*|example\w*")


def _placeholder_credential(value):
    # Surrounding punctuation is prose ("a bearer token."); a short lower-case word ("bearer tokens") is a word.
    value = re.sub(r"""^["'`(\[{]+|["'`.,;:)\]}/]+$""", "", value.strip())
    return not value or bool(_PLACEHOLDER_CREDENTIAL.fullmatch(value) or re.fullmatch(r"[A-Z_]+", value)
                             or re.fullmatch(r"[a-z]{1,12}", value))


def _exposure(text):
    """Kinds of known format found in text, checked before the line mask so that a value next to a secret word counts."""
    kinds = set()
    env_pattern = _env_pattern()
    if env_pattern and env_pattern.search(text):
        kinds.add("env")
    for kind, pattern in _PATTERNS:
        if kind in _EXPOSURE_KINDS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if kind == "header":
                    credential = re.sub(r"(?i)^(?:bearer|basic|token|digest)\s+", "", value.split(":", 1)[-1].strip())
                elif kind == "bearer":
                    credential = re.sub(r"(?i)^bearer\s+", "", value)
                elif kind == "url_credentials":
                    password = re.search(r":([^:@]*)@$", value)
                    credential = password.group(1) if password else ""
                else:
                    credential = None
                if credential is not None and _placeholder_credential((credential.split() or [""])[0]):
                    continue
                kinds.add(kind)
                break
    return kinds


def _redact(text):
    """Return (text without secrets, kinds of known format it held). Raises nothing: failure drops the whole field."""
    try:
        out = str(text)
        kinds = _exposure(out)
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
        return out, kinds
    except Exception:
        _count("redaction_errors")
        return "[dropped: redaction error]", {"redaction_error"}


def redact(text):
    """Return text without secrets."""
    return _redact(text)[0]


def _unsafe(serialized):
    """Final guard on the JSON text that is about to leave the process."""
    probe = _PLACEHOLDER.sub("", serialized)
    env_pattern = _env_pattern()
    if (env_pattern and env_pattern.search(probe)) or _LOOSE_SECRET.search(probe):
        return True
    for kind, pattern in _PATTERNS:
        if kind in _FIELD_ONLY:
            continue  # JSON-escaped forms; these were applied per field
        if pattern.search(probe):
            return True
    return False


_PATH_KEYS = ('file_path', 'filePath', 'path', 'notebook_path', 'filename', 'file', 'target_file')
_COMMAND_KEYS = ('command', 'cmd', 'script', 'code')
_SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh', 'fish'}
_INTERPRETERS = _SHELLS | {'bb', 'node', 'deno', 'bun', 'ruby', 'perl', 'php'}
_PATTERN_PROGRAMS = {'grep', 'egrep', 'fgrep', 'rg', 'ag', 'ack'}
_CLUSTER_SECRET = re.compile(r'(?i)\b(?:kubectl|oc)\b[^|;&\n]{0,512}?(?<![\w.-])secrets?(?![\w-])')
_HEREDOC = re.compile(r'''(?<!<)<<(-?)[ \t]*(['"]?)([A-Za-z_][\w-]*)\2''')
_SUBSTITUTION = re.compile(r'\$\((?:[^()]|\([^()]*\))*\)|`[^`]*`')
_ENV_ACCESSOR = re.compile(r'(?i)\b(?:os\.)?environ\b|\bprocess\.env\b|\bgetenv\b|(?-i:[%$]ENV\b|\bENV(?=[.\[]))')
_ENV_SINGLE = re.compile(r'''^\s*(?:\.(?:get|setdefault|pop|fetch)\s*\(|\?\.|\.|\[|\()\s*['"]?([A-Za-z_][A-Za-z0-9_]*)''')
_ENV_SECRET = re.compile(r'(?i)token|secret|passw|passphrase|api_?key|private_?key|credential|cookie|session|access_?key|auth')
_PRINT_CALL = re.compile(r'(?i)\b(?:print|println|printf|puts|pp|echo|console\.(?:log|error|info|warn)|(?:sys\.)?std(?:out|err)\.write|prn|pprint)\b')


def _interpreter(word):
    name = _last_element(word)
    return name in _INTERPRETERS or bool(re.fullmatch(r'python[\d.]*', name))


def _shell_words(text):
    """Return shell words and their source offsets, preserving whitespace inside quotes."""
    words, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '\n':
            words.append((i, i + 1, '\n', False, True))
            i += 1
            continue
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = n if end < 0 else end
            continue
        if text[i] in '|;&()<>':
            words.append((i, i + 1, text[i], False, True))
            i += 1
            continue
        start, value, quote, quoted_ws = i, [], None, False
        while i < n:
            c = text[i]
            if quote:
                if c == quote:
                    quote = None
                elif c == '\\' and quote == '"' and i + 1 < n:
                    i += 1
                    value.append(text[i])
                else:
                    value.append(c)
                    quoted_ws |= c.isspace()
            elif c in "'\"":
                quote = c
            elif c == '\\' and i + 1 < n:
                i += 1
                value.append(text[i])
            elif c.isspace() or c in '|;&()<>':
                break
            else:
                value.append(c)
            i += 1
        words.append((start, i, ''.join(value), quoted_ws, False))
    return words


def _segments(words):
    segment = []
    for word in words:
        if word[4]:
            if segment:
                yield segment
                segment = []
        else:
            segment.append(word)
    if segment:
        yield segment


def _heredoc_operators(line, quote):
    """<<TAG operators of a command line that are outside quotes, comments and backslash escapes. A quoted or
    commented <<EOF (echo '<<EOF', echo a # <<EOF, echo \\<<EOF) is not an operator, so the lines after it are still
    judged as commands (BOXP-428 TD-12 P0). The quote state carries over lines; a missed operator only makes its
    body judged as commands (fail closed)."""
    matches, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if quote:
            if c == quote:
                quote = None
            elif c == '\\' and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c == '\\':
            i += 1
        elif c == '#' and (i == 0 or line[i - 1].isspace() or line[i - 1] in '|;&()<>'):
            break
        elif c == '<':
            match = _HEREDOC.match(line, i)
            if match:
                matches.append(match)
                i = match.end()
                continue
        i += 1
    return matches, quote


def _strip_heredocs(text, code):
    out, pending, quote = [], collections.deque(), None
    for line in text.split('\n'):
        if pending:
            tag, tabs, keep, expands = pending[0]
            done = (line.lstrip('\t') if tabs else line) == tag
            if keep or done:
                out.append(line)
            elif expands:
                # An unquoted delimiter (<<EOF) still runs $(...) and `...` inside the body, so cat <<EOF with
                # $(cat .env) or $(printenv) is judged like a command (BOXP-428 TD-12 P0). Quoted (<<'EOF') bodies
                # are literal data and stay out.
                out.append(' ; '.join(m[2:-1] if m.startswith('$(') else m[1:-1] for m in _SUBSTITUTION.findall(line)))
            else:
                out.append('')
            if done:
                pending.popleft()
            continue
        out.append(line)
        matches, quote = _heredoc_operators(line, quote)
        for match in matches:
            segments = list(_segments(_shell_words(line[:match.start()])))
            words = [w[2] for w in segments[-1]] if segments else []
            runner = next((w for i, w in enumerate(words) if _interpreter(w) and
                           all(x.startswith('-') for x in words[i + 1:])), None)
            pending.append((match.group(3), bool(match.group(1)), bool(runner and (code or _last_element(runner) in _SHELLS)),
                            not match.group(2)))
    return '\n'.join(out)


def _program_index(words):
    """Index of the program a segment runs. Assignments and prefix programs are skipped, and so is `env` with its
    options and assignments when a command follows it (env LANG=C printenv runs printenv; BOXP-428 TD-12 P1). A bare
    env, or env -S (its command is a split string), stays on env and is judged as the environment dump."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if re.fullmatch(r'[A-Za-z_]\w*=.*', word) or _last_element(word) in _PREFIX_PROGRAMS | {'!'}:
            i += 1
            continue
        if _last_element(word) == 'env':
            j = i + 1
            while j < n:
                if re.fullmatch(r'-[A-Za-z]*S.*|--split-string(?:=.*)?', words[j]):
                    return i
                if words[j] in {'-u', '--unset', '-C', '--chdir'}:
                    j += 2
                elif words[j].startswith('-') or re.fullmatch(r'[A-Za-z_]\w*=.*', words[j]):
                    j += 1
                else:
                    break
            if j >= n or (j == n - 1 and re.fullmatch(r'\d+', words[j])):  # env A=1 2>&1: a file descriptor, no command
                return i
            i = j
            continue
        return i
    return None


def _executed_text(text, depth=0, code=True):
    text = _strip_heredocs(text, code)
    edits = []
    for segment in _segments(_shell_words(text)):
        idx = _program_index([w[2] for w in segment])
        if idx is None:
            continue
        program = _last_element(segment[idx][2])
        pattern_idx = None
        if program in _PATTERN_PROGRAMS:
            consumes = {'-A', '-B', '-C', '-m', '-g', '-t', '--max-count', '--glob', '--type', '--include', '--exclude'}
            skip = False
            for j in range(idx + 1, len(segment)):
                word = segment[j][2]
                if skip:
                    skip = False
                    continue
                if re.fullmatch(r'-[A-Za-z]*e|--regexp', word):
                    pattern_idx = j + 1
                    break
                if word in consumes:
                    skip = True
                elif not word.startswith('-'):
                    pattern_idx = j
                    break
        for j in range(idx + 1, len(segment)):
            start, end, word, ws, _ = segment[j]
            prev = segment[j - 1][2]
            # bash -c 'printenv' / sh -lc env: a one-word script of an interpreter runs too (BOXP-428 TD-12 P1)
            if re.fullmatch(r'-[A-Za-z]*[ce]', prev) and depth < 2 and (_interpreter(program) or (ws and prev == '-c')):
                replacement = ' ( ' + _executed_text(word, depth + 1, code) + ' ) '
            elif program in {'echo', 'printf'} or j == pattern_idx or (ws and (prev.startswith('-') or re.match(r'--?[\w-]+=', word))):
                replacement = ' ' + ' '.join(_SUBSTITUTION.findall(text[start:end])) + ' '
            else:
                continue
            edits.append((start, end, replacement))
    out, pos = [], 0
    for start, end, replacement in edits:
        out.extend((text[pos:start], replacement))
        pos = end
    out.append(text[pos:])
    return ''.join(out)


def _env_access(text):
    for match in _ENV_ACCESSOR.finditer(text):
        after = text[match.end():match.end() + 200]
        if re.match(r'^\s*\.copy\s*\(\s*\)', after):
            # env = os.environ.copy() / run(cmd, env=os.environ.copy()) hands a copy to a child process without
            # printing it; print(os.environ.copy()) or any other use of the copy is a dump.
            line_start = text.rfind('\n', 0, match.start()) + 1
            before = text[line_start:match.start()]
            if (re.match(r'^\s*\.copy\s*\(\s*\)\s*(?:[,)\n;#]|$)', after)
                    and re.search(r'(?:^|[^=!<>])=\s*$', before) and not _PRINT_CALL.search(before)):
                continue
            return True
        single = _ENV_SINGLE.match(after)
        if not single:
            return True
        name = single.group(1)
        if name in {'items', 'keys', 'values', 'entries', 'toJSON', 'to_h', 'to_a', 'each'}:
            return True
        line_start = text.rfind('\n', 0, match.start()) + 1
        if _ENV_SECRET.search(name) and _PRINT_CALL.search(text[line_start:match.start()]):
            return True
    return False


def _segment_env(text):
    for seg in _segments(_shell_words(text)):
        words = [w[2] for w in seg]
        idx = _program_index(words)
        if idx is None:
            continue
        program, args = _last_element(words[idx]), words[idx + 1:]
        if program == 'printenv':
            return True
        if program in {'set', 'export', 'declare', 'typeset'} and (not args or any(re.fullmatch(r'-[A-Za-z]*[px]', x) for x in args)):
            return True
        if program in {'kubectl', 'oc'} and '--' in args:
            remote = args[args.index('--') + 1:]
            if remote and _last_element(remote[0]) == 'env':
                program, args = 'env', remote[1:]
        if program == 'env':
            if args and re.fullmatch(r'\d+', args[-1]):
                args = args[:-1]
            i = 0
            while i < len(args):
                if re.fullmatch(r'-[A-Za-z]*S.*|--split-string(?:=.*)?', args[i]):
                    return True
                if args[i] in {'-u', '--unset', '-C', '--chdir'}:
                    i += 2
                elif args[i].startswith('-') or re.fullmatch(r'[A-Za-z_]\w*=.*', args[i]):
                    i += 1
                else:
                    break
            if i >= len(args):
                return True
    return False


def _command_source(command):
    if len(command) > 256 * 1024 or command.count('<<') > 4096:
        return True
    text = _executed_text(command)
    shell = _executed_text(command, code=False)
    return bool(_SENSITIVE_PATH.search(text) or _SENSITIVE_COMMAND.search(text) or _CLUSTER_SECRET.search(text)
                or _env_access(text) or _segment_env(shell) or _reads_token_file(shell))


def _path_source(path):
    path = str(path)
    return bool(_SENSITIVE_PATH.search(' ' + path) or _TOKEN_WORD.fullmatch(_last_element(path)))


def _withhold(args_text):
    try:
        try:
            args = json.loads(args_text)
        except (ValueError, TypeError):
            if args_text.lstrip().startswith('{'):
                return bool(_SENSITIVE_PATH.search(args_text) or _SENSITIVE_COMMAND.search(args_text)
                            or ('secret' in args_text.lower() and _CLUSTER_CLI.search(args_text)) or _reads_token_file(args_text)
                            or re.search(r'(?i)\benviron\b|\bprocess\.env\b|\bgetenv\b|\bprintenv\b|(?-i:[%$]ENV\b|\bENV(?:\.|\[))', args_text)
                            or _env_access(args_text) or _segment_env(args_text))
            args = args_text
        if isinstance(args, dict):
            for key in _PATH_KEYS:
                if key in args and args[key] is not None and _path_source(args[key]):
                    return True
            for key in _COMMAND_KEYS:
                if key not in args or args[key] is None:
                    continue
                command = args[key]
                if isinstance(command, list):
                    command = [str(x) for x in command]
                    flag = next((i for i, w in enumerate(command) if re.fullmatch(r'-[A-Za-z]*[ce]', w)), None)
                    command = command[flag + 1] if command and _interpreter(command[0]) and flag is not None and flag + 1 < len(command) else ' '.join(shlex.quote(x) for x in command)
                if _command_source(str(command)):
                    return True
            return False
        text = str(args)
        return _command_source(text) or (not re.search(r'\s', text.strip()) and _path_source(text.strip()))
    except (ValueError, TypeError, RecursionError):
        return True


def _last_element(word):
    return word.strip("\"'").rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1]


def _reads_token_file(args_text):
    """K0 part 1b: a token-named file in a reading position. Unparseable shell text with such a word is fail-closed."""
    if not _TOKEN_HINT.search(args_text):
        return False
    try:
        if args_text.lstrip().startswith("{"):
            for match in _PATH_ARG.finditer(args_text):
                if _TOKEN_WORD.fullmatch(_last_element(json.loads(match.group(1)))):
                    return True
            return any(_command_reads_token_file(json.loads(m.group(1))) for m in _COMMAND_ARG.finditer(args_text))
        return _command_reads_token_file(args_text)
    except (ValueError, TypeError, RecursionError):
        return True


def _command_reads_token_file(command, depth=0):
    if not _TOKEN_HINT.search(command):
        return False
    if depth > 2 or len(command) > _MAX_TOKEN_SCAN:
        return True  # nested or long beyond what is parsed here: fail-closed
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    words = list(lexer)  # ValueError on an unclosed quote: fail-closed by the caller
    segments, current = [], []
    for word in words:
        if word in (";", "|", "||", "&&", "&", "(", ")", "\n") or set(word) <= set(";|&()"):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(word)
    if current:
        segments.append(current)
    for segment in segments:
        start = _program_index(segment)
        program = _last_element(segment[start]) if start is not None else None
        operands = [(index, segment[index]) for index in range(start + 1, len(segment))] if start is not None else []
        if program is None:
            continue
        # `< file` is read by any program, so it is checked before the program is classified.
        if any(segment[index - 1].endswith("<") and _TOKEN_WORD.fullmatch(_last_element(word)) for index, word in operands):
            return True
        subcommands = _READING_SUBCOMMANDS.get(program)
        if subcommands is not None:
            names = {w for _, w in operands}
            if not (names & subcommands or (program == "git" and "log" in names and names & {"-p", "--patch", "-u", "--cc"})):
                continue
        elif program in _MENTION_PROGRAMS:
            continue
        for index, word in operands:
            previous = segment[index - 1]
            if any(c in word for c in " \t\n"):
                if _SHELL_FLAG.fullmatch(previous) and _command_reads_token_file(word, depth + 1):
                    return True
                continue
            if word.startswith("-"):
                continue
            if "=" in word:
                word = word.rsplit("=", 1)[-1]
            name = _last_element(word.split(":", 1)[-1] if program == "git" else word)
            if _TOKEN_WORD.fullmatch(name):
                return True
    return False


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
# turn buffer. Hooks store bodies here; on_session_end masks them and queues only the masked batches.

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
    if _creds is None:  # nothing is sent without a key, so nothing is buffered; withholding state is still tracked
        return
    size = sum(len(_text(v)) for v in span.values())
    with _lock:
        if len(turn["spans"]) >= _MAX_TURN_SPANS or turn["bytes"] + size > _MAX_TURN_BYTES:
            turn["truncated"] = True
            _stats["spans_dropped_turn_limit"] = _stats.get("spans_dropped_turn_limit", 0) + 1
            return
        turn["spans"].append(span)
        turn["bytes"] += size


def _tainted(session):
    """The reason this session's bodies are withheld, or None."""
    with _lock:
        return next((_sessions[node] for node in _lineage(str(session or "")) if _sessions.get(node)), None)


def _taint(reason, *sessions):
    """Withhold later bodies of these sessions. The first reason is kept; it names the evidence, not the latest event."""
    with _lock:
        for session in sessions:
            for node in _lineage(str(session or "")):
                _sessions[node] = _sessions.get(node) or reason
                _sessions.move_to_end(node)
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)


def _note_delegated(session):
    with _lock:
        _delegated[str(session)] = True
        _delegated.move_to_end(str(session))
        while len(_delegated) > _MAX_SESSIONS:
            _delegated.popitem(last=False)


def _call_arguments(message):
    """Arguments of tool calls in any provider shape, as text."""
    if not isinstance(message, dict):
        return
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            yield _text((call.get("function") or {}).get("arguments") if isinstance(call.get("function"), dict) else call.get("arguments"))
    if message.get("type") in ("function_call", "custom_tool_call"):
        yield _text(message.get("arguments") or message.get("input"))
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield _text(block.get("input"))


def _tool_results(message):
    """Tool results in any provider shape, as text: anything that is not plain chat text."""
    if not isinstance(message, dict):
        yield _text(message)
        return
    if message.get("role") not in _CHAT_ROLES:
        yield _text(message)
        return
    content = message.get("content")
    for block in content if isinstance(content, list) else []:
        if not (isinstance(block, dict) and block.get("type") in _TEXT_BLOCKS | {"tool_use"}):
            yield _text(block)


def _evidence(args_text, result_text):
    """K0 or K1 evidence in one tool call: the reason to withhold, or None."""
    if _withhold(args_text):
        return "known_source"
    if len(result_text.encode("utf-8", "ignore")) > _MAX_SCAN_BYTES:
        return "unscanned_output"
    if _is_env_dump(result_text):
        return "env_dump"
    if _exposure(result_text) or _named_values(result_text)[1]:
        return "known_format_in_tool_output"
    return None


def _note_request(session, parent, messages):
    """History this process did not observe is scanned for the same evidence; each message once."""
    with _lock:
        digests = _scanned.setdefault(session, set())
        _scanned.move_to_end(session)
        while len(_scanned) > _MAX_SESSIONS:
            _scanned.popitem(last=False)
        if len(digests) > _MAX_SCANNED_HASHES:
            digests.clear()
    for message in messages:
        digest = hashlib.sha256(_text(message).encode("utf-8", "ignore")).digest()
        with _lock:
            if digest in digests:
                continue
            digests.add(digest)
        for args_text in _call_arguments(message):
            if _withhold(args_text):
                _taint("known_source", session, parent)
        for result_text in _tool_results(message):
            reason = _evidence("", result_text)
            if reason:
                _taint(reason, session, parent)


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
    _pin_body(turn["session"], parent_session_id, user_message)
    _note_tickets(turn, user_message)
    if str(parent_session_id or "") in _delegated:  # rotation of a delegated session keeps it delegated
        _note_delegated(turn["session"])
    reason = _tainted(parent_session_id)
    if reason:  # the delegated goal may repeat what the parent saw
        _taint(reason, turn["session"])
    if _tainted(turn["session"]):  # the user can quote a reply back
        user_message = _WITHHELD_SESSION
    turn.update(user_message=user_message, model=model, platform=platform or "", parent=parent_session_id)


@_guarded
def _on_api_request(session_id=None, turn_id=None, api_request_id=None, system_prompt=None, request_messages=None,
                    message_count=None, retry_count=None, started_at=None):
    turn = _turn(session_id, turn_id)
    messages = list(request_messages or [])
    _pin_body(turn["session"], turn["parent"], system_prompt)
    for message in messages:
        _pin_body(turn["session"], turn["parent"], message)
    _note_request(turn["session"], turn["parent"], messages)
    tainted = _tainted(turn["session"])
    first_request = not turn.get("requested")
    turn["requested"] = True
    if tainted and first_request and turn["user_message"] is not None:  # the evidence of a resumed session is only known here
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
    _pin_body(turn["session"], turn["parent"], response)
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
    args_text, result_text = _text(args), _text(result)
    _pin_body(turn["session"], turn["parent"], args)
    _pin_body(turn["session"], turn["parent"], result)
    already = _tainted(turn["session"])
    reason = _evidence(args_text, result_text)
    if reason:
        _taint(reason, turn["session"], turn["parent"])
    now = time.time()
    if reason == "known_format_in_tool_output" and not already:
        # The mask is the guarantee for this body; what the model may repeat later is withheld from here on.
        tool_input, tool_output, policy = args, result, "masked_known_format"
    elif reason:
        tool_input, tool_output, policy = _WITHHELD_TOOL, _WITHHELD_TOOL, "withheld_" + reason
    elif already:
        tool_input, tool_output, policy = _WITHHELD_SESSION, _WITHHELD_SESSION, "withheld_after_" + already
    else:
        tool_input, tool_output, policy = args, result, None  # masked by _field when the turn is built
    _add(turn, {"key": "tool:" + str(tool_call_id or now), "name": str(tool_name), "type": "tool",
                "input": tool_input, "output": tool_output,
                "start": now - (duration_ms or 0) / 1000, "end": now,
                "level": "ERROR" if error_type or status in ("error", "failed") else None,
                "metadata": {"status": status, "error_type": error_type, "body_policy": policy}})


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
        _link_parent(child_session_id, parent_session_id)
        _pin_body(child_session_id, parent_session_id, child_goal)
        _note_delegated(child_session_id)


@_guarded
def _on_subagent(parent_session_id=None, parent_turn_id=None, child_session_id=None, child_role=None, child_summary=None,
                 child_status=None, duration_ms=None):
    turn = _turn(parent_session_id, parent_turn_id)
    turn["delegated"] = True
    _pin_body(child_session_id or turn["session"], turn["session"] if child_session_id else turn["parent"], child_summary)
    _note_tickets(turn, child_summary)
    reason = _tainted(child_session_id)
    if reason:
        _taint(reason, turn["session"], turn["parent"])
    if _tainted(turn["session"]):
        child_summary = _WITHHELD_SESSION
    now = time.time()
    _add(turn, {"key": "subagent:" + str(child_session_id), "name": "subagent", "type": "agent",
                "output": child_summary, "start": now - (duration_ms or 0) / 1000, "end": now,
                "metadata": {"child_session_id": child_session_id, "child_role": child_role, "status": child_status}})


@_guarded
def _on_turn_end(session_id=None, turn_id=None, assistant_response=None):
    turn = _turn(session_id, turn_id)
    _pin_body(turn["session"], turn["parent"], assistant_response)
    _note_tickets(turn, assistant_response)
    turn["output"] = _WITHHELD_SESSION if _tainted(turn["session"]) else assistant_response


@_guarded
def _on_session_end(session_id=None, turn_id=None, completed=None, failed=None, interrupted=None, turn_exit_reason=None):
    with _lock:
        turn = _turns.pop(_key(session_id, turn_id), None)
    if turn is None:
        return
    if _creds is None:
        _count("turns_skipped_no_key")
        return
    turn["status"] = "interrupted" if interrupted else "failed" if failed else "succeeded" if completed else "unknown"
    turn["exit_reason"] = turn_exit_reason
    turn["withheld_reason"] = _tainted(turn["session"])
    if not in_scope(turn):
        _count("turns_out_of_scope")
        return
    # Masked here, in the hook: the queue never holds a body that has not been through redact() and the final guard.
    pattern = _pin_pattern(turn["session"])
    spans = build_spans(turn)
    if pattern:
        spans = _mask_pins(spans, pattern)
    bodies = list(guarded_payloads(spans, pattern))
    try:
        _queue.put_nowait(bodies)
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


def _body_policy(turn, item, *rendered):
    """Why an observation's bodies look the way they do. Names avoid secret words: the final guard scans metadata too."""
    policy = (item or {}).get("metadata", {}).get("body_policy")
    if policy is None:
        if any(text in (_WITHHELD_SESSION, _WITHHELD_TOOL) for text in rendered):
            policy = "withheld_after_" + (turn.get("withheld_reason") or "unknown")
        elif any(_PLACEHOLDER.search(text) or text.startswith("[dropped:") for text in rendered):
            policy = "masked_known_format"
        elif any("…[truncated " in text for text in rendered):
            policy = "truncated"
        else:
            policy = "sent"
    _count("bodies_" + policy)
    return policy


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
        _attr("langfuse.trace.metadata.coverage.tool_bodies", "sent through the known-format mask; withheld after a known source, an environment dump or a known format in a tool result (body_policy on each observation)"),
        _attr("langfuse.trace.metadata.coverage.truncated", str(turn["truncated"]).lower()),
        _attr("langfuse.trace.metadata.coverage.withheld_reason", turn.get("withheld_reason") or "none"),
    ]
    end = time.time()
    root_input, root_output = _field(turn["user_message"] or ""), _field(turn["output"] or "")
    spans = [{
        "traceId": trace, "spanId": root_id, "name": trace_name, "kind": 1,
        "startTimeUnixNano": _ns(turn["start"]), "endTimeUnixNano": _ns(end),
        "attributes": common + [
            _attr("langfuse.observation.input", root_input),
            _attr("langfuse.observation.output", root_output),
            _attr("langfuse.observation.metadata.body_policy", _body_policy(turn, None, root_input, root_output)),
            _attr("langfuse.observation.metadata.exit_reason", _field(turn.get("exit_reason") or "")),
            _attr("langfuse.observation.metadata.parent_session_id", _field(turn["parent"] or "")),
        ],
    }]
    for item in turn["spans"]:
        start = item.get("start") or turn["start"]
        attrs = common + [_attr("langfuse.observation.type", item["type"])]
        rendered = []
        for name in ("input", "output"):
            if item.get(name) is not None:
                rendered.append(_field(item[name]))
                attrs.append(_attr("langfuse.observation." + name, rendered[-1]))
        attrs.append(_attr("langfuse.observation.metadata.body_policy", _body_policy(turn, item, *rendered)))
        if item.get("model"):
            attrs.append(_attr("langfuse.observation.model.name", _field(item["model"])))
        details = _usage(item.get("usage"))
        if details:
            attrs.append(_attr("langfuse.observation.usage_details", json.dumps(details)))
        if item.get("level"):
            attrs.append(_attr("langfuse.observation.level", item["level"]))
        for key, value in (item.get("metadata") or {}).items():
            if value is not None and key != "body_policy":
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


def guarded_payloads(spans, pinned_pattern=None):
    """Yield only payloads that pass the final guard; a failing span is dropped alone."""
    def pinned(span):
        return bool(pinned_pattern and pinned_pattern.search(_pinned_text(span)))
    for batch in batches(spans):
        body = _payload(batch)
        if not _unsafe(body) and not any(pinned(s) for s in batch):
            yield body
            continue
        kept = [s for s in batch if not _unsafe(json.dumps(s, ensure_ascii=False)) and not pinned(s)]
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


def export_bodies(bodies):
    for body in bodies:
        _send(body)
    _count("turns_exported")


def _work():
    while True:
        _refresh_credentials()
        try:
            bodies = _queue.get(timeout=_KEY_POLL_SECONDS)
        except queue.Empty:
            continue
        try:
            export_bodies(bodies)
        except Exception:
            _count("export_errors")


def _start_worker():
    global _worker
    if _worker is None:
        _worker = threading.Thread(target=_work, name="boxp-langfuse-observer", daemon=True)
        _worker.start()
