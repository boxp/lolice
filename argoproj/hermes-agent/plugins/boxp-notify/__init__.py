"""Send-only notification channel (platform plugin). BOXP-546 prototype, BOXP-553 stage 1.

Stage 1 deploys a copy of this file (lolice ``argoproj/hermes-agent/plugins/boxp-notify``) for the
Alertmanager relay only (``direct:alertmanager``). The Task Board and cron paths below are still lab-only;
they are meant to replace what the core patches ``fix-task-board-delivery`` / ``fix-cron-delivery-copy`` do:

* ``deliver: boxp_notify`` + ``deliver_extra.chat_id: "tb:{event_id}"`` on the
  webhook route, ``deliver: "boxp_notify:cron"`` on a cron job.
* ``send`` is the real delivery: it writes the home inbox file the Task Board
  dispatcher already reads, forwards to Telegram (off / fallback / both), and
  keeps a durable receipt so the same event is delivered once.
* Silence is granted only when the ticket Notes hold the helper receipt.

Only ``gateway.platforms.base`` and ``gateway.config`` are imported from Hermes.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

NAME = "boxp_notify"
NOTICE = "【Task Board 配送要確認】"
EVENT = re.compile(
    r"(BOXP-\d+)-(?:\d{8}T\d{6}Z-[a-f0-9-]+-(?:review|blocked|done)"
    r"|lane-(?:review|blocked|done)-\d{8}T\d{6}Z-[a-f0-9]+|digest-\d{8}T\d{6}Z-[a-f0-9]+)")
SILENT_RESULTS = {"silent", "deferred", "continued"}
SILENCE_MARKERS = {"[SILENT]", "SILENT", "NO_REPLY", "[NO_REPLY]"}
CRON_HEADER = re.compile(r"Cronjob Response: ([^\n]{1,200})\n\(job_id: ")
CRON_FAILURE = re.compile(r"^⚠️ Cron '[^\n]*' failed", re.M)
ALERT_HEADER = re.compile(r"発生中 (\d+) 件 / 解除 (\d+) 件")
ALERT_LINE = re.compile(r"^(?:🔴|✅) \[([A-Za-z0-9_.:/=?-]{1,80})\] ([A-Za-z0-9_.:/=?-]{1,80})", re.M)
TELEGRAM_LIMIT = 4000
DEFAULT_SILENCE = "⚠️ The model returned only a silence marker for a message that needed a reply. Try again or rephrase."
DEFAULT_STATUS = r"^⏳||retrying \(\d+/\d+\)||retrying automatically"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def settings() -> dict[str, Any]:
    home = Path(_env("HERMES_HOME", str(Path.home() / ".hermes")))
    state = Path(_env("BOXP_NOTIFY_STATE_DIR", str(home / "boxp-notify")))
    return {
        "state": state,
        "vault": Path(_env("BOXP_NOTIFY_VAULT", "/home/boxp/Documents/obsidian-headless/BOXP")),
        "telegram_mode": _env("BOXP_NOTIFY_TELEGRAM_MODE", "fallback"),  # off | fallback | both
        "telegram_base": _env("BOXP_NOTIFY_TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        # Falls back to the gateway's own Telegram settings (home channel and its topic), so that a
        # notification moved onto this channel lands where `deliver: telegram` put it before.
        "telegram_token": _env("BOXP_NOTIFY_TELEGRAM_TOKEN") or _env("TELEGRAM_BOT_TOKEN"),
        "telegram_chat": _env("BOXP_NOTIFY_TELEGRAM_CHAT_ID") or _env("TELEGRAM_HOME_CHANNEL"),
        "telegram_thread": _env("BOXP_NOTIFY_TELEGRAM_THREAD_ID") or _env("TELEGRAM_HOME_CHANNEL_THREAD_ID"),
        # A receipt that reached one egress but not the other stops being retried after this long.
        "pending_max_age": int(_env("BOXP_NOTIFY_PENDING_MAX_AGE_SECONDS", "86400") or 86400),
        # Texts upstream substitutes for a refused silence / a failed turn; measured in the lab and
        # supplied by configuration so that an upstream wording change is a config change, not a patch.
        "silence_fallbacks": [s for s in _env("BOXP_NOTIFY_SILENCE_FALLBACKS", DEFAULT_SILENCE).split("||") if s],
        # Transient progress lines ("retrying 1/3 ...") are never a result: dropped without closing the event.
        "status_patterns": [s for s in _env("BOXP_NOTIFY_STATUS_PATTERNS", DEFAULT_STATUS).split("||") if s],
        "cron_bucket_seconds": int(_env("BOXP_NOTIFY_CRON_BUCKET_SECONDS", "600") or 600),
        "home_fail": (state / "lab-fail-home").exists(),  # lab only: simulate an unwritable vault
        "no_recovery": (state / "lab-no-recovery").exists(),  # lab only: observe upstream redelivery alone
    }


def now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def journal(cfg: dict, kind: str, **fields: Any) -> None:
    """Append-only measurement log (lab evidence; metadata plus a short text preview)."""
    try:
        cfg["state"].mkdir(parents=True, exist_ok=True)
        with (cfg["state"] / "journal.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": now().isoformat(), "kind": kind, **fields}, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("boxp-notify journal unavailable")


# --- classification -------------------------------------------------------------------------

def parse_target(chat_id: str) -> dict:
    """``tb:<event>`` Task Board event, ``direct:<name>[:<id>]`` agent-less relay, anything else cron."""
    chat_id = str(chat_id or "")
    if chat_id.startswith("tb:"):
        event = chat_id[3:]
        match = EVENT.fullmatch(event)
        return {"kind": "task-board", "event": event, "ticket": match[1] if match else None}
    if chat_id.startswith("direct:"):
        # ``direct:alertmanager:<firing count>`` is the only agent-less relay with a home inbox contract
        # (BOXP-501: Projects/alertmanager-deliveries). Other names have none and go to Telegram.
        name, _, extra = chat_id[7:].partition(":")
        return {"kind": "direct", "name": name, "extra": extra}
    return {"kind": "cron", "name": chat_id}


def classify(cfg: dict, content: str, event: Optional[str] = None) -> str:
    """silence | empty | status | notice | report | unverified.

    For a Task Board event only a text that carries ``event=<id>`` (the route prompt requires it) is a
    formal report. Everything else is unverified: the channel does not depend on upstream's error wording.
    """
    text = (content or "").strip()
    if not text or text == "(empty)":
        return "empty"
    if text.upper() in SILENCE_MARKERS or any(text == s.strip() for s in cfg["silence_fallbacks"]):
        return "silence"
    if text.startswith(NOTICE):
        return "notice"
    if event is None or f"event={event}" in text:
        return "report"
    if any(re.search(p, text) for p in cfg["status_patterns"]):
        return "status"
    return "unverified"


def notes_receipt(cfg: dict, event: str, ticket: str) -> str:
    """Same rule as the current patch: only the exact helper-produced line counts."""
    pattern = re.compile(
        r"^.*\[kohako-event\]: \[kohako-event\] handled event=" + re.escape(event)
        + r" run=\S+ result=(notified|silent|question|deferred|continued) summary=.+$")
    results = set()
    try:
        with (cfg["vault"] / "Tickets" / f"{ticket}.md").open() as stream:
            for line in stream:
                match = pattern.fullmatch(line.rstrip("\n"))
                if match:
                    results.add(match[1])
    except (OSError, UnicodeError):
        return "unknown"
    return next(iter(results)) if len(results) == 1 else "unknown"


def notice(event: str, reason: str) -> str:
    return (f"{NOTICE} event={event} reason={reason}\n"
            "業務処理・通知の完了は未確認です。最新票Notesとeventの配送記録を照合し、"
            "必要な報告だけ取り次いでください。業務処理の再実行・自動再送はしません。")


def decide(cfg: dict, target: dict, content: str) -> dict:
    """Return {"action": deliver|suppress|drop, "slot": report|notice, "text", "classification"}."""
    if target["kind"] != "task-board":
        kind = classify(cfg, content)
        action = "suppress" if kind in {"silence", "empty"} else "deliver"
        return {"action": action, "slot": "report", "text": content, "classification": kind}
    event, ticket = target["event"], target["ticket"]
    kind = classify(cfg, content, event)
    if kind == "status":
        return {"action": "drop", "slot": "notice", "text": content, "classification": "status"}
    if not ticket:  # not a recognised event id: never silence, never guess a ticket
        return {"action": "deliver", "slot": "notice", "text": notice(event, "unknown_event"),
                "classification": "unknown_event"}
    proof = notes_receipt(cfg, event, ticket)
    if kind == "silence":
        if proof in SILENT_RESULTS:
            return {"action": "suppress", "slot": "report", "text": content, "classification": "proven_silent",
                    "proof": proof}
        return {"action": "deliver", "slot": "notice", "text": notice(event, "missing_reply"),
                "classification": "missing_reply", "proof": proof}
    if kind == "empty":
        return {"action": "deliver", "slot": "notice", "text": notice(event, "empty_response"),
                "classification": "empty_response", "proof": proof}
    if kind == "unverified":
        return {"action": "deliver", "slot": "notice",
                "text": content + "\n\n" + notice(event, "unverified_response"),
                "classification": "unverified_response", "proof": proof}
    if kind == "notice":
        return {"action": "deliver", "slot": "notice", "text": content, "classification": "notice", "proof": proof}
    return {"action": "deliver", "slot": "report", "text": content, "classification": "report", "proof": proof}


# --- delivery -------------------------------------------------------------------------------

def file_ident(key: str) -> str:
    """File-name-safe identity of a key: a readable prefix plus a hash of the whole key.

    The hash keeps two keys apart whatever their length; a plain truncation dropped the text digest and
    the time bucket of a long cron target, so a different notification could hit the same receipt.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:80] + "-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def receipt_path(cfg: dict, key: str) -> Path:
    return cfg["state"] / "receipts" / (file_ident(key) + ".json")


def mode_for(cfg: dict, target: dict) -> str:
    """Telegram mode of one target. An agent-less relay can override the default per name, e.g.
    ``BOXP_NOTIFY_TELEGRAM_MODE_ALERTMANAGER=both`` keeps alerts on Telegram while home is added."""
    if target["kind"] == "direct":
        override = _env("BOXP_NOTIFY_TELEGRAM_MODE_" + re.sub(r"[^A-Za-z0-9]", "_", str(target.get("name") or "")).upper())
        if override:
            return override
    return cfg["telegram_mode"]


def alert_summary(text: str, extra: str) -> tuple[str, int]:
    """Title and firing count for the home inbox, read back from the relay's own text.

    The webhook hands the channel nothing but the chat id and the text. The relay writes
    "発生中 N 件 / 解除 M 件" and one "🔴 [SEVERITY] alertname ..." line per alert; anything else falls
    back to the first line and to the count in the chat id (``direct:alertmanager:<firing>``), else 1.
    """
    header, line = ALERT_HEADER.search(text), ALERT_LINE.search(text)
    firing = int(header[1]) if header else int(extra) if str(extra).isdigit() else 1
    if not (header and line):
        return (text.strip().splitlines() or ["Alertmanager 通知"])[0][:200], firing
    total = firing + int(header[2])
    title = (f"[{line[1].upper()}] " if firing else "解除: ") + line[2] + (f" ほか {total - 1} 件" if total > 1 else "")
    return title[:200], firing


def direct_key(cfg: dict, name: str, digest: str) -> str:
    """Key of one agent-less relay notification: the same text counts as a retry only while nothing else
    from that relay was accepted in between (or while its own delivery is still unfinished).

    Nothing but the text reaches the channel, and an alert that resolves and fires again with the same
    start minute produces the same text as its first notification. A key made of text and time alone would
    drop that second firing as a duplicate. So each accepted notification gets a sequence number, and the
    last ones are remembered per relay: the same text again is a retry when it is the latest notification
    of the relay, or when its receipt is still pending (a webhook retry after both egresses failed), inside
    two time buckets. After a different notification (the resolve) it is a new one. When unsure this sends
    twice rather than not at all.
    """
    path = cfg["state"] / "direct-recent" / (file_ident(name) + ".json")
    recent = read_json(path) or {}
    window = 2 * max(1, cfg["cron_bucket_seconds"])
    stamp = now().timestamp()
    entries = [e for e in recent.get("entries", []) if isinstance(e, dict) and stamp - float(e.get("at", 0)) < window]
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.get("digest") != digest:
            continue
        latest = index == len(entries) - 1
        if latest or (read_json(receipt_path(cfg, entry["key"])) or {}).get("state") == "pending":
            return entry["key"]
        break  # delivered, and something else came after it: this is the alert firing again
    seq = int(recent.get("seq", 0)) + 1
    key = f"direct-{name}-{digest}-{seq}"
    atomic_json(path, {"seq": seq, "entries": (entries + [{"digest": digest, "key": key, "at": stamp}])[-64:]})
    return key


def dedup_key(cfg: dict, target: dict, decision: dict, metadata: Optional[dict]) -> str:
    if target["kind"] == "task-board":
        return f"tb-{target['event']}.{decision['slot']}"
    digest = hashlib.sha256(decision["text"].encode()).hexdigest()[:16]
    bucket = int(now().timestamp()) // max(1, cfg["cron_bucket_seconds"])
    if target["kind"] == "direct":
        if decision["action"] != "deliver":
            return f"direct-{target['name']}-{digest}-{bucket}"
        return direct_key(cfg, str(target["name"]), digest)
    # cron: no run id reaches the channel. job id + text + a coarse time bucket separates two runs
    # that produce the same text, while a retry of one run inside the bucket collapses.
    job = str((metadata or {}).get("job_id") or target["name"] or "unknown")
    bucket = int(now().timestamp()) // max(1, cfg["cron_bucket_seconds"])
    return f"cron-{job}-{digest}-{bucket}"


def write_home(cfg: dict, target: dict, decision: dict, key: str, metadata: Optional[dict]) -> bool:
    if cfg["home_fail"]:
        return False
    try:
        sent_at = now().isoformat()
        if target["kind"] == "direct":
            if target["name"] != "alertmanager":
                return False  # no home inbox contract for this relay: Telegram is its only egress
            ident = "alert-" + hashlib.sha256(key.encode()).hexdigest()[:32]
            title, firing = alert_summary(decision["text"], str(target.get("extra") or ""))
            atomic_json(cfg["vault"] / "Projects/alertmanager-deliveries/delivered-copies" / f"{ident}.md",
                        {"id": ident, "text": decision["text"], "title": title, "firing": firing, "sent_at": sent_at})
        elif target["kind"] == "task-board":
            if not target.get("ticket"):
                # Not a recognised event id: never used as a file name (it could point outside the vault),
                # and the dispatcher would not read it anyway. Telegram is the only egress.
                return False
            suffix = ".notice" if decision["slot"] == "notice" else ""
            path = (cfg["vault"] / "Projects/task-board-events/delivered-copies"
                    / f"{target['event']}{suffix}.md")
            atomic_json(path, {"event": target["event"], "classification": decision["classification"],
                               "text": decision["text"], "sent_at": sent_at, "primary": "home"})
        else:
            job = re.sub(r"[^A-Za-z0-9_-]", "_", str((metadata or {}).get("job_id") or target["name"] or "x"))[:64]
            ident = file_ident(key)
            path = cfg["vault"] / "Projects/cron-deliveries/delivered-copies" / f"{ident}.md"
            # Upstream wraps cron output as "Cronjob Response: <job name>\n(job_id: ...)" and reports a
            # failed run as "⚠️ Cron '<job name>' failed: ..." (measured on v0.22.5 and v0.23.1).
            header = CRON_HEADER.match(decision["text"])
            atomic_json(path, {"id": ident, "job_id": job,
                               "job_name": (header[1].strip() if header else "") or str(target.get("name") or job),
                               "failure": bool(CRON_FAILURE.search(decision["text"])), "text": decision["text"],
                               "sent_at": sent_at})
        return True
    except Exception as error:  # an unwritable vault must fall back, not raise into the gateway
        logger.warning("boxp-notify home inbox unavailable: %s", type(error).__name__)
        return False


def home_applicable(target: dict) -> bool:
    """False when the target has no home inbox contract, so Telegram is its only egress."""
    if target["kind"] == "direct":
        return target.get("name") == "alertmanager"
    if target["kind"] == "task-board":
        return bool(target.get("ticket"))
    return True


def egress_complete(mode: str, target: dict, home: bool, telegram: bool) -> bool:
    """Whether every egress the mode requires has succeeded (otherwise the receipt stays pending)."""
    if mode == "both":  # both means both: a home success alone must not close a failed Telegram send
        return telegram and (home or not home_applicable(target))
    if mode == "fallback":
        return home or telegram
    return home


def telegram_send(cfg: dict, text: str, progress: Optional[dict] = None, saved=None) -> tuple[bool, str]:
    """Blocking Bot API call (run in a thread). Splits long text; no retry beyond the caller's.

    ``progress["chunks"]`` counts the chunks already accepted, so a retry resumes after them.
    ``saved(count)`` is called right after each accepted chunk so the count survives a crash mid-text.
    """
    if not (cfg["telegram_token"] and cfg["telegram_chat"]):
        return False, "telegram not configured"
    progress = progress if progress is not None else {}
    ids = []
    for index, start in enumerate(range(0, max(len(text), 1), TELEGRAM_LIMIT)):
        if index < int(progress.get("chunks", 0)):
            continue
        message = {"chat_id": cfg["telegram_chat"], "text": text[start:start + TELEGRAM_LIMIT]}
        if str(cfg.get("telegram_thread") or "").isdigit():
            message["message_thread_id"] = int(cfg["telegram_thread"])
        body = json.dumps(message).encode()
        request = urllib.request.Request(
            f"{cfg['telegram_base']}/bot{cfg['telegram_token']}/sendMessage", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                reply = json.loads(response.read())
            if not reply.get("ok"):
                return False, "telegram rejected"
            ids.append(str((reply.get("result") or {}).get("message_id", "")))
            progress["chunks"] = index + 1
            if saved:
                saved(index + 1)
        except (urllib.error.URLError, OSError, ValueError) as error:
            return False, f"telegram {type(error).__name__}"
    return True, ",".join(ids)


async def deliver(cfg: dict, path: Path, record: dict) -> dict:
    """Run the egress for one pending receipt (stored at ``path``) and return the updated record."""
    target, decision, metadata = record["target"], record["decision"], record.get("metadata")
    key = record.get("key") or path.stem
    home = record.get("home") or write_home(cfg, target, decision, key, metadata)
    if home and not record.get("home"):
        record["home"] = True
        atomic_json(path, record)  # durable before the next egress: a crash must not redo this one
    telegram, detail = record.get("telegram", False), record.get("telegram_detail", "")
    mode = mode_for(cfg, target)
    if not telegram and (mode == "both" or (mode == "fallback" and not home)):
        progress = {"chunks": int(record.get("telegram_chunks", 0))}

        def saved(count: int) -> None:
            record["telegram_chunks"] = count
            atomic_json(path, record)

        worker = asyncio.ensure_future(asyncio.to_thread(telegram_send, cfg, decision["text"], progress, saved))
        try:
            telegram, detail = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Shutdown cancelled this send while the HTTP call runs on in its thread. Keep the claim until
            # the call ends and its outcome is in the receipt, so nobody resends what is being accepted.
            done, _ = await asyncio.wait([worker])
            if not worker.cancelled() and worker.exception() is None:
                telegram, detail = worker.result()
                record.update(telegram=telegram, telegram_detail=detail, telegram_chunks=progress["chunks"],
                              updated_at=now().isoformat())
                if egress_complete(mode, target, home, telegram):
                    record["state"] = "delivered"
                atomic_json(path, record)
            raise
        record["telegram_chunks"] = progress["chunks"]
    record.update(home=home, telegram=telegram, telegram_detail=detail,
                  attempts=int(record.get("attempts", 0)) + 1, updated_at=now().isoformat())
    record["state"] = "delivered" if egress_complete(mode, target, home, telegram) else "pending"
    atomic_json(path, record)
    return record


_LOCKS: dict[str, asyncio.Lock] = {}


@contextlib.asynccontextmanager
async def claim(path: Path):
    """One claim per receipt file at a time, shared by ``send`` and ``recover``.

    The asyncio lock covers this process; the file lock covers the standalone cron process, which runs
    outside the gateway. Both are keyed by the receipt file.
    """
    name = path.stem
    async with _LOCKS.setdefault(name, asyncio.Lock()):
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.parent / f".{name}.lock", "w")
        try:
            await asyncio.to_thread(fcntl.flock, handle, fcntl.LOCK_EX)
            yield
        finally:
            handle.close()  # releases the file lock
            if len(_LOCKS) > 512:
                for stale in [k for k, lock in _LOCKS.items() if not lock.locked() and k != name]:
                    _LOCKS.pop(stale, None)


async def send_once(cfg: dict, chat_id: str, content: str, metadata: Optional[dict]) -> SendResult:
    target = parse_target(chat_id)
    decision = decide(cfg, target, content)
    key = dedup_key(cfg, target, decision, metadata)
    # One claim per key at a time: two concurrent redeliveries of one event, or a redelivery and the
    # recovery pass, must not both run the egress.
    async with claim(receipt_path(cfg, key)):
        return await _send_claimed(cfg, target, decision, key, chat_id, content, metadata)


async def _send_claimed(cfg: dict, target: dict, decision: dict, key: str, chat_id: str, content: str,
                        metadata: Optional[dict]) -> SendResult:
    path = receipt_path(cfg, key)
    existing = read_json(path)
    base = {"chat_id": chat_id, "key": key, "classification": decision["classification"],
            "metadata_keys": sorted((metadata or {}).keys()), "length": len(content or "")}
    if decision["action"] == "drop" or (metadata or {}).get("_interim_send"):
        # Progress lines and lifecycle broadcasts ("shutting down") are never a notification result.
        journal(cfg, "status_dropped", **base)
        return SendResult(success=True, message_id=key)
    if existing and existing.get("state") in {"delivered", "suppressed", "expired"}:
        journal(cfg, "duplicate_dropped", **base, first_state=existing["state"])
        return SendResult(success=True, message_id=key)
    if target["kind"] == "task-board" and decision["slot"] == "notice":
        # A formal report already delivered for this event makes a later notice redundant.
        report = read_json(receipt_path(cfg, f"tb-{target['event']}.report"))
        if report and report.get("state") in {"delivered", "pending"}:
            # Delivered: the notice is redundant. Pending: the channel holds the report and finishes it
            # itself, so upstream's "delivery failed" follow-up must not become a second notification.
            journal(cfg, "notice_after_report_dropped", **base, report_state=report["state"])
            return SendResult(success=True, message_id=key)
    if decision["action"] == "suppress":
        atomic_json(path, {"state": "suppressed", "target": target, "decision": decision,
                           "created_at": now().isoformat()})
        journal(cfg, "suppressed", **base, proof=decision.get("proof"))
        return SendResult(success=True, message_id=key)
    record = existing or {"state": "pending", "key": key, "target": target, "decision": decision,
                          "metadata": {k: v for k, v in (metadata or {}).items()
                                       if isinstance(v, (str, int, float, bool))},
                          "created_at": now().isoformat()}
    if not existing:
        atomic_json(path, record)  # durable intent before any egress
    record = await deliver(cfg, path, record)
    reached = record["home"] or record["telegram"]
    journal(cfg, "delivered" if record["state"] == "delivered" else
            "delivered_partial" if reached else "delivery_failed", **base,
            home=record["home"], telegram=record["telegram"], proof=decision.get("proof"))
    if reached:
        # Partial (mode both, one egress failed): the notification reached the Owner, so upstream is told
        # success and sends no failure follow-up; the receipt stays pending and recovery finishes the rest.
        return SendResult(success=True, message_id=key)
    return SendResult(success=False, error="boxp-notify: home inbox and telegram both unavailable",
                      retryable=True)


async def recover(cfg: dict) -> int:
    """Finish receipts left pending by a crash or by a failed egress."""
    done = 0
    if cfg["no_recovery"]:
        return done
    directory = cfg["state"] / "receipts"
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        record = read_json(path)
        if not record or record.get("state") != "pending":
            continue
        async with claim(path):
            record = read_json(path)  # a send that held the claim may have finished it meanwhile
            if not record or record.get("state") != "pending":
                continue
            if (record.get("home") or record.get("telegram")) and _age(record) > cfg["pending_max_age"]:
                # Reached the Owner through one egress; the other has failed for this long (a rejected
                # message would otherwise be retried every pass, forever). Nothing unreached is ever dropped.
                record.update(state="expired", updated_at=now().isoformat())
                atomic_json(path, record)
                journal(cfg, "expired", key=record.get("key") or path.stem, home=bool(record.get("home")),
                        telegram=bool(record.get("telegram")))
                continue
            record = await deliver(cfg, path, record)
        journal(cfg, "recovered" if record["state"] == "delivered" else "recovery_failed",
                key=record.get("key") or path.stem,
                home=record["home"], telegram=record["telegram"])
        done += record["state"] == "delivered"
    return done


def _age(record: dict) -> float:
    try:
        return (now() - datetime.fromisoformat(str(record.get("created_at")))).total_seconds()
    except ValueError:
        return 0.0


def pending_count(cfg: dict) -> int:
    directory = cfg["state"] / "receipts"
    return sum(1 for path in (directory.glob("*.json") if directory.is_dir() else [])
               if (read_json(path) or {}).get("state") == "pending")


def self_check(cfg: dict) -> list[str]:
    problems = []
    if not (cfg["vault"] / "Tickets").is_dir():
        problems.append("vault Tickets directory missing")
    modes = {cfg["telegram_mode"]} | {value.strip() for name, value in os.environ.items()
                                      if name.startswith("BOXP_NOTIFY_TELEGRAM_MODE_") and value.strip()}
    if not modes <= {"off", "fallback", "both"}:
        problems.append("unknown telegram mode")
    if modes != {"off"} and not (cfg["telegram_token"] and cfg["telegram_chat"]):
        problems.append("telegram fallback not configured")
    return problems


class BoxpNotifyAdapter(BasePlatformAdapter):
    """Send-only: nothing is ever handed to ``handle_message``."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(NAME))
        self._recovery: Optional[asyncio.Task] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        cfg = settings()
        problems = self_check(cfg)
        journal(cfg, "connect", is_reconnect=is_reconnect, problems=problems, pending=pending_count(cfg))
        if problems:
            logger.warning("boxp-notify self check: %s", "; ".join(problems))
        self._mark_connected()
        self._recovery = asyncio.create_task(self._recover_loop())
        return True

    async def _recover_loop(self) -> None:
        while True:
            try:
                await recover(settings())
            except Exception:
                logger.exception("boxp-notify recovery pass failed")
            await asyncio.sleep(int(_env("BOXP_NOTIFY_RECOVERY_SECONDS", "60") or 60))

    async def disconnect(self) -> None:
        if self._recovery:
            self._recovery.cancel()
            self._recovery = None
        self._mark_disconnected()
        journal(settings(), "disconnect")

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        cfg = settings()
        journal(cfg, "send_called", chat_id=chat_id, reply_to=reply_to, length=len(content or ""),
                preview=(content or "")[:300],
                metadata={k: (v if isinstance(v, (str, int, float, bool, type(None))) else type(v).__name__)
                          for k, v in (metadata or {}).items()})
        try:
            return await send_once(cfg, chat_id, content, metadata)
        except Exception as error:
            logger.exception("boxp-notify send failed")
            journal(cfg, "send_exception", chat_id=chat_id, error=type(error).__name__)
            return SendResult(success=False, error=f"boxp-notify: {type(error).__name__}", retryable=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": str(chat_id), "type": "channel"}


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, **_kwargs) -> dict:
    """Out-of-process cron delivery (a cron run outside the gateway has no live adapter).

    Same decision, receipt and egress as ``send``. No job id reaches this path, so the cron key falls back
    to the target name. Pending receipts are finished by the gateway's recovery loop.
    """
    cfg = settings()
    journal(cfg, "standalone_send_called", chat_id=str(chat_id), length=len(message or ""),
            preview=(message or "")[:300])
    try:
        result = await send_once(cfg, str(chat_id), message or "", None)
    except Exception as error:
        logger.exception("boxp-notify standalone send failed")
        return {"error": f"boxp-notify: {type(error).__name__}"}
    return {"success": True, "message_id": result.message_id} if result.success else {"error": result.error}


def _enabled() -> Optional[dict]:
    # No home channel on purpose: lifecycle broadcasts go to home channels, and every target must be explicit.
    return {} if _env("BOXP_NOTIFY_ENABLED") == "1" else None


def register(ctx) -> None:
    ctx.register_platform(
        name=NAME, label="BOXP notify", adapter_factory=lambda cfg: BoxpNotifyAdapter(cfg),
        check_fn=lambda: True, validate_config=lambda config: True, required_env=[],
        env_enablement_fn=_enabled, emoji="📮", pii_safe=True, allow_update_command=False,
        # Cron accepts a plugin platform as a `deliver` target only when it names this variable. It is left
        # unset: targets are always explicit (`boxp_notify:<name>`), there is no default home channel.
        cron_deliver_env_var="BOXP_NOTIFY_HOME_CHANNEL", standalone_sender_fn=_standalone_send)
