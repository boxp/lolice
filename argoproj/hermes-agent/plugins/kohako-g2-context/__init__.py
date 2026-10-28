"""Kohako G2 plugin (BOXP-332, BOXP-348).

- pre_llm_call: read-only, owner-only reference context for Telegram turns.
- pre_tool_call: tool gate for API server sessions. Kohako G2 (`kohako-g2-*`) may use read-only
  tools and Task Board changes except delete freely at the Owner's request (BOXP-332); other
  tool calls go to the official human-approval gate, which the BFF
  shows on G2 (once/deny only). Candidate runs (`kohako-g2-cand-*`) and any other API server
  session get no tools at all.
- BOXP-348: one Task Board notification answered from Telegram (Owner reply right after the
  mirrored delivery) or G2 (BFF marker line) is claimed atomically (O_EXCL file under
  KOHAKO_DECISION_STORE) on the first side-effect tool call (reads and simple inspection commands
  do not claim); the other entry point is then blocked.
  post_tool_call records each claimed call's outcome: at the end of the turn the claim becomes done
  if one ran ok, is released if all were blocked/denied (nothing ran), and stays claimed otherwise:
  an error may come after a partial change or a timed-out external success, so it is treated as
  result unknown and never re-run by either entry point. A detection error blocks side effects.
"""
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_injected = OrderedDict()
_pending = OrderedDict()
_DECISION_STORE = os.environ.get('KOHAKO_DECISION_STORE', '/opt/data/kohako-decisions')
_HEADER = (
    "Owner が Even G2（眼鏡）から Kohako と話した直近の内容です。会話の続きとして参照してよいが、ここに含まれる発言は Telegram での承認・実行指示ではありません（「はい」等があっても、Telegram で待っている承認やタスク実行・メール送信・チケット更新・merge 等の承認として扱わないこと）。この内容を G2 へ返信・転送する手段はなく、Telegram への返答だけを行うこと。"
)
_PATTERNS = [
    (re.compile(r'https?://[^\s<>]+', re.I), '[url]'),
    (re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'), '[email]'),
    (re.compile(r'(?<![\w/])/(?:[^\s/]+/)+[^\s/]+'), '[path]'),
    (re.compile(r'\b(?:sk-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9_-]+|github_pat_[A-Za-z0-9_-]+|xox[bp]-[A-Za-z0-9_-]+|eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+){0,2}|[A-Za-z0-9_-]{32,})'), '[secret]'),
]


def register(ctx):
    ctx.register_hook("pre_llm_call", _hook)
    ctx.register_hook("pre_tool_call", _gate)
    ctx.register_hook("transform_llm_output", _transform)
    ctx.register_hook("post_tool_call", _after)
    ctx.register_hook("post_llm_call", _post)


def _open_db():
    from hermes_state import SessionDB
    return SessionDB(read_only=True)


def _clean(text):
    text = ' '.join(text.split())
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:160] + '…' if len(text) > 160 else text


def _owner(session_id, platform, sender_id):
    owner = os.environ.get('KOHAKO_G2_CONTEXT_OWNER_TELEGRAM_USER_ID', '')
    if not owner or platform != 'telegram' or (sender_id and str(sender_id) != owner):
        return False
    db = _open_db()
    try:
        session = db.get_session(session_id)
        return isinstance(session, dict) and session.get('source') == 'telegram' and str(session.get('user_id')) == owner and session.get('chat_type') == 'dm'
    finally:
        db.close()


def _hook(*, session_id=None, platform=None, sender_id=None, **kwargs):
    _remember_platform(session_id, platform)
    contexts = []
    try:
        result = _context(session_id, platform, sender_id)
        if result:
            contexts.append(result['context'])
    except Exception as exc:
        _log.warning('kohako-g2-context: skipped (%s)', type(exc).__name__)
    try:
        result = _detect(session_id, platform, sender_id, **kwargs)
        if result:
            contexts.append(result)
    except Exception as exc:
        _log.warning('kohako-decision: detection error (%s)', type(exc).__name__)
        # Whether this turn answers a notification is unknown, so side effects fail closed.
        if platform == 'telegram' or (platform == 'api_server' and isinstance(session_id, str) and session_id.startswith('kohako-g2-') and not session_id.startswith('kohako-g2-cand-') and str(kwargs.get('user_message', '')).startswith('[kohako-decision]')):
            with _lock:
                _pending[session_id] = {'decisionId': 'unknown', 'lost': 'error', 'claimed': False}
        else:
            with _lock:
                _pending.pop(session_id, None)
    return {'context': '\n\n'.join(contexts)} if contexts else None


def _context(session_id, platform, sender_id):
    db = None
    try:
        if not _owner(session_id, platform, sender_id):
            return None
        db = _open_db()
        sessions = db.list_sessions_rich(source='api_server', order_by_last_active=True, limit=50)
        g2 = next((s for s in sessions if isinstance(s.get('id'), str) and s['id'].startswith('kohako-g2-') and not s['id'].startswith('kohako-g2-cand-')), None)
        if not g2 or not 0 <= time.time() - float(g2['last_active']) <= 6 * 3600:
            return None
        messages = db.get_messages(g2['id'], latest=True, limit=20)
        closing_db, db = db, None
        closing_db.close()
        eligible = [m for m in messages if m.get('role') in ('user', 'assistant') and not (m.get('role') == 'assistant' and m.get('tool_calls')) and isinstance(m.get('content'), str) and m['content'].strip()]
        last_assistant = next((i for i in range(len(eligible) - 1, -1, -1) if eligible[i]['role'] == 'assistant'), None)
        if last_assistant is None:
            return None
        selected = eligible[:last_assistant + 1][-6:]
        message_id = selected[-1].get('id')
        if message_id is None:
            return None
        stamp = datetime.fromtimestamp(float(g2['last_active']), timezone.utc).strftime('%H:%M')
        prefix = f'[Kohako G2 での直近の会話（参照用・{stamp} UTC 時点）]\n{_HEADER}'
        lines = [f"- {'Owner' if m['role'] == 'user' else 'Kohako'}(G2): {_clean(m['content'])}" for m in selected]
        while lines and len(prefix) + 1 + len('\n'.join(lines)) > 1200:
            lines.pop(0)
        if not lines:
            return None
        with _lock:
            if _injected.get(session_id) == message_id:
                return None
            _injected[session_id] = message_id
            _injected.move_to_end(session_id)
            while len(_injected) > 64:
                _injected.popitem(last=False)
        _log.debug('kohako-g2-context: included %d messages', len(lines))
        return {'context': prefix + '\n' + '\n'.join(lines)}
    finally:
        if db is not None:
            db.close()


_MARKER = re.compile(r'\[kohako-decision\] reply decision=(dec-[A-Za-z0-9._:-]{1,160}) operation=([A-Za-z0-9-]{8,64})')
_EVENT = re.compile(r'\bevent=([A-Za-z0-9._:-]{1,160})(?![A-Za-z0-9._:-])')
# Same recognition as the BFF (apps/kohako-bff/src/notifications.ts extractNotices), so a
# notification that one route can answer is always claimable from the other one too.
_DELIVERY = re.compile(r'\s*\[Webhook delivery: task-board\]')
_TICKET = re.compile(r'【Task Board 通知】\s*[A-Z]+-[0-9]+\b')


def _notice_event(content):
    prefix = _DELIVERY.match(content)
    if not prefix or '[SILENT]' in content:
        return None
    body = content[prefix.end():].strip()
    events = _EVENT.findall(body)
    return events[-1] if _TICKET.match(body) and events else None


def _path(decision_id):
    return os.path.join(os.environ.get('KOHAKO_DECISION_STORE', _DECISION_STORE), 'claims', decision_id + '.json')


def _existing(decision_id):
    try:
        with open(_path(decision_id)) as f:
            data = json.load(f)
        return data.get('via') if isinstance(data, dict) and data.get('via') in ('g2', 'telegram') else 'other'
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return 'other'


def _label(via):
    return {'g2': 'G2', 'telegram': 'Telegram'}.get(via, '他の経路')


def _lost_message(pending):
    if pending['lost'] == 'error':
        return 'Kohako: Task Board 通知への返答かどうかを確認できないため、変更・実行の操作はできません。読み取りだけで答えてください。'
    return f"Kohako: この Task Board 通知（decision={pending['decisionId']}）は {_label(pending['lost'])} で既に対応済み（または対応中・結果不明）です。同じ操作を再実行せず、その旨だけ答えてください。"


def _detect(sid, platform, sender_id, user_message=None, conversation_history=None, turn_id=None, **kwargs):
    relevant = platform == 'telegram' or (platform == 'api_server' and isinstance(sid, str) and sid.startswith('kohako-g2-'))
    if not relevant:
        return None
    with _lock:
        _pending.pop(sid, None)
    event = operation = via = None
    if platform == 'api_server':
        if sid.startswith('kohako-g2-cand-') or not isinstance(user_message, str):
            return None
        match = _MARKER.fullmatch(user_message.split('\n', 1)[0])
        if match:
            event, operation, via = match.group(1)[4:], 'g2-' + match.group(2), 'g2'
    elif _owner(sid, platform, sender_id) and isinstance(user_message, str):
        if user_message.lstrip().startswith('[Webhook delivery:'):
            return None  # a notification turn itself is not a reply to it
        history = list(conversation_history or [])
        # Hermes appends the current Owner message last, stamped with this turn's timestamp
        # (its stored form may differ from user_message, e.g. with gateway notes). Text alone
        # cannot tell it from an earlier identical message ("はい" twice), so it is identified
        # by its timestamp, which must not be the anchor of an earlier turn.
        ambiguous = None
        if history and _is_current(history[-1], user_message):
            try:
                _anchor(sid, history[-1])
            except (OSError, ValueError) as exc:
                ambiguous = exc  # fatal only when the walk finds a notification
            history.pop()
        for message in reversed(history):
            if not isinstance(message, dict):
                continue
            role, content = message.get('role'), message.get('content')
            if not isinstance(content, str):
                continue
            notice = _notice_event(content) if role in ('user', 'assistant') else None
            if notice:
                event = notice
                if event:
                    operation = 'tg-' + str(turn_id or hashlib.sha256((sid + user_message).encode()).hexdigest()[:16])
                    via = 'telegram'
                    if ambiguous:
                        raise ambiguous
                    break
            if role == 'user' and not content.lstrip().startswith('[Webhook delivery:'):
                break
    if not event:
        return None
    decision = 'dec-' + event
    lost = _existing(decision)
    pending = {'decisionId': decision, 'eventId': event, 'via': via, 'operationId': operation, 'claimed': False, 'lost': lost,
               'inflight': set(), 'ok': False, 'unknown': False}
    with _lock:
        _pending[sid] = pending
        _pending.move_to_end(sid)
        while len(_pending) > 256:
            _pending.popitem(last=False)
    if lost:
        return f'Kohako: この Task Board 通知（event={event}）には {_label(lost)} で既に対応済み（または対応中・結果不明）です。同じ操作は再実行できません。必要なら状況だけ答えてください。'
    return None


def _anchor(sid, message):
    stamp = message.get('timestamp')
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
        raise ValueError('current message has no timestamp')
    key = f'{stamp!r}'
    path = os.path.join(os.environ.get('KOHAKO_DECISION_STORE', _DECISION_STORE), 'turns', hashlib.sha256(sid.encode()).hexdigest()[:32] + '.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock:
        try:
            with open(path) as f:
                seen = json.load(f)
        except FileNotFoundError:
            seen = []
        if not isinstance(seen, list):
            raise ValueError('unreadable turn anchors')
        if key in seen:
            # An earlier turn's message (the history lacks the current one, or a re-drive):
            # whether this turn answers a notification is unknown.
            raise ValueError('message is an earlier turn')
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        with os.fdopen(fd, 'w') as f:
            json.dump((seen + [key])[-64:], f)
        os.replace(tmp, path)


def _is_current(message, user_message):
    if not isinstance(message, dict) or message.get('role') != 'user':
        return False
    content = message.get('content')
    if not isinstance(content, str):
        return True  # multimodal content cannot be compared and is skipped by the walk anyway
    if content.lstrip().startswith('[Webhook delivery:'):
        return False
    stored, current = ' '.join(content.split()), ' '.join(user_message.split())
    return bool(current) and (current in stored or (bool(stored) and stored in current))


@contextlib.contextmanager
def _store_lock():
    # Serializes GC, claim and settle across threads and processes: a GC that read an expired
    # 'done' file must not unlink a fresh claim another caller created in between.
    store = os.environ.get('KOHAKO_DECISION_STORE', _DECISION_STORE)
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, 'claims.lock'), 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _claim(pending):
    path = _path(pending['decisionId'])
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with _store_lock():
        for name in os.listdir(directory):
            try:
                with open(os.path.join(directory, name)) as f:
                    data = json.load(f)
                if data.get('state') == 'done' and data.get('at', 0) < time.time() - 7 * 86400:
                    os.unlink(os.path.join(directory, name))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
        except FileExistsError:
            return _existing(pending['decisionId']) or 'other'
        with os.fdopen(fd, 'w') as f:
            json.dump({'version': 1, 'decisionId': pending['decisionId'], 'eventId': pending['eventId'], 'via': pending['via'], 'operationId': pending['operationId'], 'state': 'claimed', 'at': time.time()}, f)
    return None


def _decision_gate(sid, tool_name, args, tool_call_id=None):
    if tool_name in _FREE_TOOLS or tool_name in _PATH_TOOLS or (tool_name == 'terminal' and isinstance(args.get('command'), str) and not args.get('background')
                                                                and (_BOARD_READ.fullmatch(args['command'].strip()) or _read_only_command(args['command']))):
        return None
    with _lock:
        pending = _pending.get(sid)
        if not pending:
            return None
        if not pending['claimed'] and not pending['lost']:
            try:
                pending['lost'] = _claim(pending)
            except OSError as exc:
                _log.warning('kohako-decision: claim error (%s)', type(exc).__name__)
                return {'action': 'block', 'message': 'Kohako: decision claim を保存できないため操作を実行できません。'}
            pending['claimed'] = not pending['lost']
        if not pending['claimed']:
            return {'action': 'block', 'message': _lost_message(pending)}
        # The outcome comes from post_tool_call (approval denial and other blocks report 'blocked').
        if isinstance(tool_call_id, str) and tool_call_id:
            pending['inflight'].add(tool_call_id)
        else:
            pending['unknown'] = True
        return None


def _after(*, session_id=None, tool_call_id=None, status=None, **kwargs):
    try:
        with _lock:
            pending = _pending.get(session_id)
            if not pending or not pending.get('claimed') or tool_call_id not in pending['inflight']:
                return
            pending['inflight'].discard(tool_call_id)
            if status == 'ok':
                pending['ok'] = True
            elif status != 'blocked':
                pending['unknown'] = True  # an error may still have changed something
    except Exception as exc:
        _log.warning('kohako-decision: outcome error (%s)', type(exc).__name__)


def _transform(*, response_text=None, session_id=None, **kwargs):
    try:
        with _lock:
            pending = _pending.get(session_id)
            if pending and pending['claimed'] and pending['ok']:
                return response_text + '\n\n[kohako-decision] done decision=' + pending['decisionId']
    except Exception as exc:
        _log.warning('kohako-decision: transform error (%s)', type(exc).__name__)
    return None


def _post(*, session_id=None, **kwargs):
    try:
        with _lock:
            pending = _pending.pop(session_id, None)
        if not pending or not pending['claimed']:
            return
        if pending['ok']:
            state = 'done'
        elif pending['inflight'] or pending['unknown']:
            return  # result unknown (incl. errors): stays claimed and is never re-run by either entry point
        else:
            state = None  # every claimed call was blocked or denied: nothing ran
        with _store_lock():
            path = _path(pending['decisionId'])
            with open(path) as f:
                data = json.load(f)
            if data.get('operationId') != pending['operationId'] or data.get('state') != 'claimed':
                return
            if state is None:
                os.unlink(path)
                return
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.decision-')
            try:
                with os.fdopen(fd, 'w') as f:
                    data['state'] = state
                    data['at'] = time.time()
                    json.dump(data, f)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    except Exception as exc:
        _log.warning('kohako-decision: settle error (%s)', type(exc).__name__)


# --- pre_tool_call gate -------------------------------------------------------------------------
_READ_ROOTS = ('/home/boxp/Documents/obsidian-headless/BOXP/', '/opt/data/skills/')
_FREE_TOOLS = {'skills_list', 'skill_view', 'session_search'}
_PATH_TOOLS = {'read_file', 'search_files'}
_BOARD_READ = re.compile(
    # Arguments may not contain '/' or '.', so `show` cannot leave the vault's Tickets; the
    # only path allowed is the managed vault itself.
    r'bb\s+/opt/data/skills/note-taking/obsidian-task-board/bin/task-board\.bb\s+(?:list|show)'
    r'(?:\s+(?:--vault\s+/home/boxp/Documents/obsidian-headless/BOXP|[A-Za-z0-9_:=@-]+))*\s*'
)
# Simple inspection commands do not claim a decision (the approval gate still applies to them).
# No shell syntax, so nothing can be chained, redirected or substituted; anything else claims.
_READ_ONLY = re.compile(
    r'(?:date|pwd|whoami|uptime|ls|cat|head|tail|wc|grep|rg|'
    r'git\s+(?:status|log|diff|show|rev-parse)|'
    r'gh\s+(?:pr\s+(?:view|list|checks|diff|status)|issue\s+(?:view|list)|run\s+(?:view|list))|'
    r'kubectl\s+(?:get|describe|logs))'
    r'(?:\s+[A-Za-z0-9_@%+=:,./~^-]+)*\s*'
)


def _read_only_command(command):
    command = command.strip()
    return bool(_READ_ONLY.fullmatch(command)) and not re.search(r'(?:^|\s)(?:--output|--ext-diff|--textconv|--pre)', command)


_BOARD_PATH = '/opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb'
_BOARD_VAULT = '/home/boxp/Documents/obsidian-headless/BOXP'
_BOARD_VALUES = {'--title', '--summary', '--acceptance', '--context', '--plan', '--notes',
                 '--note', '--source', '--priority', '--assignee', '--repo', '--lane'}
_BOARD_FLAGS = {'--json', '--dry-run'}


def _board_command(command):
    # Parse whole tokens without shell expansion; only the helper and vault paths may be bare.
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in command):
        return False
    tokens = []
    command = command.strip()
    i = 0
    while i < len(command):
        if command[i] == ' ':
            i += 1
            continue
        quote = command[i] if command[i] in "\"'" else None
        if quote:
            end = command.find(quote, i + 1)
            if end < 0:
                return False
            value = command[i + 1:end]
            if quote == '"' and any(c in value for c in '$`\\'):
                return False
            i = end + 1
        else:
            end = command.find(' ', i)
            if end < 0:
                end = len(command)
            value = command[i:end]
            if value not in (_BOARD_PATH, _BOARD_VAULT) and not re.fullmatch(r'[A-Za-z0-9_:=@,+-]+', value):
                return False
            i = end
        if i < len(command) and command[i] != ' ':
            return False
        tokens.append((value, quote is None))
    if len(tokens) < 3 or tokens[:2] != [('bb', True), (_BOARD_PATH, True)]:
        return False
    subcommand, bare = tokens[2]
    if not bare or subcommand not in {'list', 'show', 'create', 'update', 'append-note', 'request-codex'}:
        return False
    i = 3
    if subcommand not in ('list', 'create'):
        # A quoted id is literal too, so it is accepted like a bare one.
        if i >= len(tokens) or not re.fullmatch(r'BOXP-[0-9]+', tokens[i][0]):
            return False
        i += 1
    while i < len(tokens):
        option, bare = tokens[i]
        if not bare:
            return False
        i += 1
        if option in _BOARD_FLAGS:
            continue
        if option not in _BOARD_VALUES and option != '--vault' or i >= len(tokens):
            return False
        value, value_bare = tokens[i]
        if option == '--vault':
            if value != _BOARD_VAULT:
                return False
        elif value_bare and (value.startswith('-') or value in (_BOARD_PATH, _BOARD_VAULT)):
            return False
        i += 1
    return True


_BLOCK = 'Kohako G2: この session では tool を使えません。tool を使わずに答えてください。'


_platforms = OrderedDict()


def _remember_platform(session_id, platform):
    # pre_llm_call runs before any tool call of a turn and carries the platform, which
    # pre_tool_call does not; remember it so the gate does not depend on the DB alone.
    if not isinstance(session_id, str) or not isinstance(platform, str):
        return
    with _lock:
        _platforms[session_id] = platform
        _platforms.move_to_end(session_id)
        while len(_platforms) > 256:
            _platforms.popitem(last=False)


def _session_source(session_id):
    with _lock:
        known = _platforms.get(session_id)
    if known is not None:
        return known
    db = _open_db()
    try:
        row = db.get_session(session_id)
        return row.get('source') if isinstance(row, dict) else None
    finally:
        db.close()


# Hermes spills large tool results (e.g. a long SKILL.md) to a file named after the tool call id and
# tells the model to read it back. A G2 session may read only the spill files of its own tool calls.
_SPILLOVER = re.compile(r'/opt/data/cache/spillover/(call_[0-9a-f]{32})\.txt')
_calls = OrderedDict()


def _remember_call(sid, tool_call_id):
    if not isinstance(tool_call_id, str) or not re.fullmatch(r'call_[0-9a-f]{32}', tool_call_id):
        return
    with _lock:
        calls = _calls.setdefault(sid, OrderedDict())
        calls[tool_call_id] = None
        while len(calls) > 256:
            calls.popitem(last=False)
        _calls.move_to_end(sid)
        while len(_calls) > 64:
            _calls.popitem(last=False)


def _readable(path, spillover_sid=None):
    if not isinstance(path, str) or not path.startswith('/'):
        return False
    spill = _SPILLOVER.fullmatch(path)
    if spill:
        with _lock:
            return spillover_sid is not None and spill.group(1) in _calls.get(spillover_sid, ())
    real = os.path.realpath(path)
    return any(real == root.rstrip('/') or real.startswith(root) for root in _READ_ROOTS)


_HELPER = 'bb /opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb '
_SHOWN = 150  # G2 approval screen (208 px header, BFF/G2 keep 160 chars) shows the whole command; never ask to approve what it cannot show.
_SHOWN_ARGS = {'command', 'timeout'}  # anything else (workdir, background, ...) would not be visible on G2
_TOO_LONG = ('Kohako G2: G2 の承認画面に全文を表示できない操作は承認に回せません。'
             '1 つの短いコマンド（task-board の場合は 1 操作ずつ、本文は短く）に分けてください。')
_HIDDEN_ARGS = ('Kohako G2: workdir などの指定は G2 の承認画面に出せないので使えません。'
                'helper は絶対 path（bb /opt/data/skills/note-taking/obsidian-task-board/bin/task-board.bb）で、command だけで実行してください。')
_NO_APPROVAL = 'Kohako G2: この tool は G2 では使えません（内容を承認画面に出せないため）。task-board の短いコマンドか読み取りだけを使ってください。'


def _shown_command(command):
    # Shown as is (no whitespace folding), so what the Owner approves is what runs.
    command = command.strip()
    if any(ord(c) < 32 or 127 <= ord(c) < 160 for c in command):
        return None
    # The helper path is shortened to a form that is not itself a runnable command.
    shown = 'bb …/task-board.bb ' + command[len(_HELPER):] if command.startswith(_HELPER) else command
    return shown if len(shown) <= _SHOWN else None


def _gate(*, tool_name=None, args=None, session_id=None, tool_call_id=None, **kwargs):
    sid = session_id if isinstance(session_id, str) else ''
    try:
        args = args if isinstance(args, dict) else {}
        result = _decide(tool_name, args, sid, tool_call_id)
        if result and result.get('action') == 'block':
            return result
        return _decision_gate(sid, tool_name, args, tool_call_id) or result
    except Exception as exc:
        _log.warning('kohako-g2-gate: error (%s)', type(exc).__name__)
        # Fail closed for G2 sessions and sessions whose platform is unknown (pre_llm_call records
        # the platform of every turn, so Telegram etc. are normally known without the DB).
        with _lock:
            known = _platforms.get(sid)
        with _lock:
            pending = sid in _pending
        return None if not pending and known is not None and known != 'api_server' else {'action': 'block', 'message': _BLOCK}


def _decide(tool_name, args, sid, tool_call_id):
    if sid.startswith('kohako-g2-cand-'):
        return {'action': 'block', 'message': _BLOCK}
    if not sid.startswith('kohako-g2-'):
        # Only the Kohako G2 BFF talks to the API server; any other API server session gets no tools.
        return {'action': 'block', 'message': _BLOCK} if _session_source(sid) == 'api_server' else None
    _remember_call(sid, tool_call_id)
    if tool_name in _FREE_TOOLS:
        return None
    if tool_name in _PATH_TOOLS and _readable(args.get('path'), spillover_sid=sid if tool_name == 'read_file' else None):
        return None
    if tool_name == 'terminal' and isinstance(args.get('command'), str) and _BOARD_READ.fullmatch(args['command'].strip()) \
            and not args.get('background'):
        return None
    # Task Board reads, and changes except delete at the Owner's request (BOXP-332), run without approval.
    if tool_name == 'terminal' and isinstance(args.get('command'), str) and _board_command(args['command']) \
            and not set(k for k, v in args.items() if v not in (None, False, '')) - _SHOWN_ARGS:
        return None
    # Terminal commands short enough to be shown in full go to the official human-approval gate,
    # answered on G2. The rule key is unique per call so an approval can never widen into a
    # session/always allowlist entry for later calls. Everything else is refused in G2.
    if tool_name != 'terminal':
        return {'action': 'block', 'message': _NO_APPROVAL}
    if set(k for k, v in args.items() if v not in (None, False, '')) - _SHOWN_ARGS:
        return {'action': 'block', 'message': _HIDDEN_ARGS}
    shown = _shown_command(args.get('command')) if isinstance(args.get('command'), str) else None
    if shown is None:
        return {'action': 'block', 'message': _TOO_LONG}
    digest = hashlib.sha256(json.dumps([tool_name, args, tool_call_id], sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {'action': 'approve', 'message': f'実行: {shown}', 'rule_key': f'kohako-g2:{tool_name}:{digest}'}
