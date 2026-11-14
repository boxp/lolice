"""Send-only notification channel (platform plugin). BOXP-546 prototype, BOXP-553 stage 1, BOXP-554 stage 2,
BOXP-556 stage 3 (the Hermes image carries no core patch any more).

A copy of this file is deployed from lolice ``argoproj/hermes-agent/plugins/boxp-notify``. Stage 1 moved the
Alertmanager relay (``direct:alertmanager``) here, stage 2 the Task Board and cron notifications. Since
stage 3 the former core patches ``fix-task-board-delivery`` / ``fix-cron-delivery-copy`` are gone, so this
channel is the only place that keeps the home inbox copy, the silence proof and the de-duplication:

* ``deliver: boxp_notify`` + ``deliver_extra.chat_id: "tb:{event_id}"`` on the webhook route
  ``task-board-channel``, ``deliver: "boxp_notify:cron"`` on a cron job.
* ``send`` is the real delivery: it writes the home inbox file the Task Board dispatcher already reads,
  sends to Telegram, and keeps a durable receipt so the same event is delivered once.
* Task Board: the gate the patch implemented lives here, with the same files the dispatcher reads
  (``Projects/task-board-events``): silence only on the Notes receipt, quiet hours and digest claims hold the
  send (``quiet-held``), ``delivery-receipts`` tell the dispatcher what happened, ``delivery-routing.md`` plus
  the reminder readiness decide between home-primary and Telegram-primary. Reconciliation notices go to
  Telegram as before.
* cron: home and Telegram (``BOXP_NOTIFY_TELEGRAM_MODE_CRON=both``), as before the switch.
* New cron jobs: a ``pre_tool_call`` hook rewrites ``deliver`` / ``failure_deliver`` of the agent's
  ``cronjob_manage`` create/update calls from Telegram (or the creating chat) to ``boxp_notify:cron``. Without
  the cron patch a job addressed to Telegram would reach Telegram only and never the home inbox.
* Stored cron jobs still addressed to Telegram (saved earlier, or created with ``hermes cron create``) are
  moved the same way by the recovery loop, through ``hermes cron edit``.

Only ``gateway.platforms.base`` and ``gateway.config`` are imported from Hermes.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

NAME = "boxp_notify"
NOTICE = "【Task Board 配送要確認】"
EVENT = re.compile(
    r"(BOXP-\d+)-(?:\d{8}T\d{6}Z-[a-f0-9-]+-(?:review|blocked|done)"
    r"|lane-(?:review|blocked|done)-\d{8}T\d{6}Z-[a-f0-9]+|digest-\d{8}T\d{6}Z-[a-f0-9]+)")
SILENT_RESULTS = {"silent", "deferred", "continued"}
REPORT_RESULTS = {"notified", "question"}
TB = "Projects/task-board-events"
REMINDER_ROUTE = "kohako-home-reminder"
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
        # cron hands over no run id: the same text of the same job counts as a retry of one run only while
        # it is that job's latest notification and this recent. Keep it below the shortest job interval.
        "cron_dedup_seconds": int(_env("BOXP_NOTIFY_CRON_DEDUP_SECONDS", "300") or 300),
        "hermes_home": home,
        # Task Board gate inputs, the same ones the dispatcher and the core patch use.
        "quiet_spec": _env("CODEX_TASK_BOARD_EVENT_QUIET_HOURS", "2-9"),
        "quiet_zone": _env("CODEX_TASK_BOARD_EVENT_TIMEZONE", "Asia/Tokyo"),
        "ready_max_age": int(_env("BOXP_TASK_BOARD_HOME_READY_MAX_AGE_SECONDS", "3600") or 3600),
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


# --- Task Board gate (the contract of the core patch, on the files the dispatcher reads) -----------

def quiet_now(cfg: dict) -> bool:
    """The dispatcher publishes its effective window; before its first tick the same environment applies.
    Same semantics as the dispatcher: an unparsable window means no quiet hours, a bad timezone holds."""
    spec, zone = cfg["quiet_spec"], cfg["quiet_zone"]
    policy = read_json(cfg["vault"] / TB / "quiet-policy.md") or {}
    if isinstance(policy.get("quiet_hours"), str) and isinstance(policy.get("timezone"), str):
        spec, zone = policy["quiet_hours"], policy["timezone"]
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", spec)
    if not match:
        return False
    start, end = map(int, match.groups())
    try:
        hour = now().astimezone(ZoneInfo(zone)).hour
    except Exception:
        logger.warning("boxp-notify quiet policy has an invalid timezone")
        return True  # hold rather than risk a night send; the held event stays recoverable
    return start <= hour < end if start <= end else hour >= start or hour < end


def held(cfg: dict, event: str, proof: Optional[str]) -> bool:
    """Quiet hours, an event the dispatcher moved into the morning digest, or an event the agent deferred:
    the report is not sent now. The digest carries it."""
    return (quiet_now(cfg) or (cfg["vault"] / TB / "delivery-claims" / f"{event}.md").exists()
            or (proof == "deferred" and "-digest-" not in event))


def vault_receipt(cfg: dict, event: str, state: str, classification: Optional[str]) -> bool:
    """What the dispatcher reads to close, hold or stop redelivering an event (metadata only)."""
    if cfg["home_fail"]:
        return False
    try:
        atomic_json(cfg["vault"] / TB / "delivery-receipts" / f"{event}.md",
                    {"event": event, "state": state, "classification": classification,
                     "updated_at": now().isoformat()})
        return True
    except Exception as error:
        logger.warning("boxp-notify vault receipt unavailable: %s", type(error).__name__)
        return False


def silence_receipt(cfg: dict, event: str, proof: str) -> bool:
    """A digest ends without a send, and a deferred event enters the digest, only on this proof of an
    intentional silence. It never replaces the evidence of an earlier send.

    False only when the receipt this silence needs could not be written: the dispatcher would then see
    neither the hold nor the end of the event, so the caller must not report the silence as done."""
    expected = "silent" if "-digest-" in event else "deferred"
    if proof != expected:
        return True
    if (read_json(cfg["vault"] / TB / "delivery-receipts" / f"{event}.md") or {}).get("state") in {"sent", "uncertain"}:
        return True
    for slot in ("report", "notice"):
        if (read_json(receipt_path(cfg, f"tb-{event}.{slot}")) or {}).get("state") in {"delivered", "pending"}:
            return True
    return vault_receipt(cfg, event, "silent", proof)


def home_primary(cfg: dict) -> bool:
    """Home is the primary egress of a formal report only while ``delivery-routing.md`` asks for it and the
    unread reminder / Telegram fallback can run: the reminder route is registered and the dispatcher's
    heartbeat is fresh. Otherwise Telegram stays primary (fail safe)."""
    requested = "telegram"
    try:
        for line in (cfg["vault"] / TB / "delivery-routing.md").read_text().splitlines():
            match = re.fullmatch(r"\s*task-board:\s*(\S+)\s*", line)
            if match:
                requested = match[1]
                break
    except (OSError, UnicodeError):
        pass
    if requested != "home":
        return False
    route = (read_json(cfg["hermes_home"] / "webhook_subscriptions.json") or {}).get(REMINDER_ROUTE)
    ready = read_json(cfg["vault"] / TB / "home-reminder-ready.md") or {}
    try:
        age = (now() - datetime.fromisoformat(str(ready["checked_at"]).replace("Z", "+00:00"))).total_seconds()
        fresh = ready.get("reminder_ready") is True and -300 <= age <= cfg["ready_max_age"]
    except Exception:
        fresh = False
    if isinstance(route, dict) and route.get("enabled", True) is not False and fresh:
        return True
    logger.warning("boxp-notify home routing requested but reminder/fallback is not ready; using telegram")
    return False


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
    base = {"proof": proof, "receipt_class": "unresolved"}
    if kind == "silence":
        if proof in SILENT_RESULTS:
            return {"action": "suppress", "slot": "report", "text": content, "classification": "proven_silent",
                    **base, "receipt_class": proof}
        return {"action": "deliver", "slot": "notice", "text": notice(event, "missing_reply"),
                "classification": "missing_reply", **base}
    if kind == "empty":
        return {"action": "deliver", "slot": "notice", "text": notice(event, "empty_response"),
                "classification": "empty_response", **base}
    if kind == "unverified":
        return {"action": "deliver", "slot": "notice",
                "text": content + "\n\n" + notice(event, "unverified_response"),
                "classification": "unverified_response", **base}
    if kind == "notice":
        return {"action": "deliver", "slot": "notice", "text": content, "classification": "notice", **base}
    if NOTICE in content:
        return {"action": "deliver", "slot": "report", "text": content, "classification": "report", **base}
    if proof not in REPORT_RESULTS:
        # Keep a potentially necessary report, but do not claim its work succeeded (same as the patch).
        return {"action": "deliver", "slot": "report",
                "text": content + "\n\n" + notice(event, "missing_or_conflicting_receipt"),
                "classification": "missing_or_conflicting_receipt", **base}
    return {"action": "deliver", "slot": "report", "text": content, "classification": "report",
            **base, "receipt_class": proof}


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
    if target["kind"] == "cron" and _env("BOXP_NOTIFY_TELEGRAM_MODE_CRON"):
        return _env("BOXP_NOTIFY_TELEGRAM_MODE_CRON")
    return cfg["telegram_mode"]


def task_board_plan(cfg: dict, target: dict, decision: dict) -> dict:
    """Egress of one Task Board notification, fixed when it is first accepted.

    A formal report goes to the home inbox alone while home is primary (the dispatcher then reminds on
    Telegram when it stays unread, and sends the text there when home never stored it); Telegram is used
    here only when the inbox file cannot be written. Everything else - reconciliation notices, unresolved
    reports, Telegram-primary routing - goes to Telegram and leaves a home copy, as the patch did."""
    primary = (bool(target.get("ticket")) and decision["slot"] == "report" and NOTICE not in decision["text"]
               and home_primary(cfg))
    return {"mode": "fallback" if primary else "both", "primary": primary}


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


def direct_key(cfg: dict, name: str, digest: str, superseded: Optional[list] = None,
               window: Optional[int] = None, prefix: str = "direct") -> str:
    """Key of one agent-less relay notification: the same text counts as a retry only while nothing else
    from that relay was accepted in between.

    Nothing but the text reaches the channel, and an alert that resolves and fires again with the same
    start minute produces the same text as its first notification. A key made of text and time alone would
    drop that second firing as a duplicate. So each accepted notification gets a sequence number, and the
    last ones are remembered per relay: the same text again is a retry only when it is the latest
    notification of the relay, inside two time buckets. After a different notification (the resolve) it is
    a new one and goes to both egresses.

    What happens to the earlier one with the same text depends on whether it reached the Owner. Half
    delivered: upstream was told success and does not retry it, so recovery finishes it separately. Reached
    neither egress (both were down, the webhook was told failure): the new text may be the webhook's retry
    of it (another alert group came in between) or the alert firing again after its resolve. The two cannot
    be told apart, and they need not be: the new notification takes the place of the unreached one
    (``superseded`` collects its key), so the Owner gets that text once, after whatever came in between.
    Treating it as the old receipt instead would lose a second firing; keeping both would send a retry twice.
    """
    path = cfg["state"] / "direct-recent" / (file_ident(name if prefix == "direct" else f"{prefix}-{name}") + ".json")
    recent = read_json(path) or {}
    window = window or 2 * max(1, cfg["cron_bucket_seconds"])
    stamp = now().timestamp()
    entries = [e for e in recent.get("entries", []) if isinstance(e, dict) and stamp - float(e.get("at", 0)) < window]
    for index in range(len(entries) - 1, -1, -1):
        entry = entries[index]
        if entry.get("digest") != digest:
            continue
        if index == len(entries) - 1:
            return entry["key"]
        if superseded is not None:
            superseded.append(entry["key"])  # replaced below, if it turns out to have reached nothing
        break  # something else came after it: a new notification
    seq = int(recent.get("seq", 0)) + 1
    key = f"{prefix}-{name}-{digest}-{seq}"
    atomic_json(path, {"seq": seq, "entries": (entries + [{"digest": digest, "key": key, "at": stamp}])[-64:]})
    return key


def dedup_key(cfg: dict, target: dict, decision: dict, metadata: Optional[dict],
              superseded: Optional[list] = None) -> str:
    if target["kind"] == "task-board":
        return f"tb-{target['event']}.{decision['slot']}"
    digest = hashlib.sha256(decision["text"].encode()).hexdigest()[:16]
    bucket = int(now().timestamp()) // max(1, cfg["cron_bucket_seconds"])
    if target["kind"] == "direct":
        if decision["action"] != "deliver":
            return f"direct-{target['name']}-{digest}-{bucket}"
        return direct_key(cfg, str(target["name"]), digest, superseded)
    # cron: no run id reaches the channel. The same text of the same job is a retry of one run only while
    # it is that job's latest notification and recent (see ``direct_key``); otherwise it is a new run.
    job = str((metadata or {}).get("job_id") or target["name"] or "unknown")
    if decision["action"] != "deliver":
        return f"cron-{job}-{digest}-{bucket}"
    if not (metadata or {}).get("job_id"):
        # No job id either (the out-of-process sender): every job shares the target name, so the same text
        # from two jobs could not be told from a retry and the later one would be dropped. Losing a
        # notification is worse than a repeated one: without a job id nothing is treated as a retry.
        return f"cron-{job}-{digest}-{uuid.uuid4().hex[:16]}"
    return direct_key(cfg, job, digest, superseded, window=max(1, cfg["cron_dedup_seconds"]), prefix="cron")


def cron_job_name(cfg: dict, job_id: str, text: str, fallback: str) -> str:
    """Job name for the home inbox title. Nothing but the job id reaches the channel, and production runs
    cron with ``wrap_response: false`` (no "Cronjob Response: <name>" header), so the name is read from the
    scheduler's own job file; the header and the target name are fallbacks."""
    try:
        data = json.loads((cfg["hermes_home"] / "cron/jobs.json").read_text())
        for job in (data.get("jobs") if isinstance(data, dict) else data) or []:
            if isinstance(job, dict) and str(job.get("id")) == job_id and str(job.get("name") or "").strip():
                return re.sub(r"[\x00-\x1f\x7f]", " ", str(job["name"])).strip()[:200]
    except Exception:
        pass
    header = CRON_HEADER.match(text)
    return (header[1].strip() if header else "") or fallback


def write_home(cfg: dict, target: dict, decision: dict, key: str, metadata: Optional[dict],
               primary: bool = True) -> bool:
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
            # ``primary: home`` is what makes the dispatcher remind / fall back on Telegram: only on a
            # report that did not go to Telegram.
            atomic_json(path, {"event": target["event"], "classification": decision["classification"],
                               "text": decision["text"], "sent_at": sent_at,
                               **({"primary": "home"} if primary else {})})
        else:
            job = re.sub(r"[^A-Za-z0-9_-]", "_", str((metadata or {}).get("job_id") or target["name"] or "x"))[:64]
            ident = file_ident(key)
            path = cfg["vault"] / "Projects/cron-deliveries/delivered-copies" / f"{ident}.md"
            # Upstream reports a failed run as "⚠️ Cron '<job name>' failed: ..." (measured on v0.22.5 and
            # v0.23.1).
            atomic_json(path, {"id": ident, "job_id": job,
                               "job_name": cron_job_name(cfg, str((metadata or {}).get("job_id") or ""),
                                                         decision["text"], str(target.get("name") or job)),
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


def markdown_html(text: str) -> str:
    """The few Markdown forms the agent writes (bold, code, links, headings) as Telegram HTML. The gateway's
    own Telegram adapter renders them; a plain Bot API call would show the asterisks."""
    parts = re.split(r"(```[^\n]*\n.*?```|`[^`\n]+`)", html.escape(text, quote=False), flags=re.S)
    for index, part in enumerate(parts):
        if part.startswith("```"):
            parts[index] = "<pre>" + part.split("\n", 1)[1][:-3] + "</pre>"
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            parts[index] = "<code>" + part[1:-1] + "</code>"
        else:
            part = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)\"]+)\)", r'<a href="\2">\1</a>', part)
            part = re.sub(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*", r"<b>\1</b>", part)
            parts[index] = re.sub(r"^#{1,6} +(?!<b>)(.+)$", r"<b>\1</b>", part, flags=re.M)
    return "".join(parts)


def telegram_send(cfg: dict, text: str, progress: Optional[dict] = None, saved=None,
                  rich: bool = False) -> tuple[bool, str]:
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
        url = f"{cfg['telegram_base']}/bot{cfg['telegram_token']}/sendMessage"

        def call(payload: dict) -> dict:
            request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())

        try:
            reply = None
            if rich and len(text) <= TELEGRAM_LIMIT:
                # One message only: markup must not be cut at a chunk border. Telegram answers 400 to
                # markup it cannot parse; the plain text below is then sent instead, so nothing is lost.
                try:
                    reply = call({**message, "text": markdown_html(text), "parse_mode": "HTML"})
                except urllib.error.HTTPError as error:
                    if error.code != 400:
                        raise
                if reply is not None and not reply.get("ok"):
                    reply = None
            if reply is None:
                reply = call(message)
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
    mode = record.get("mode") or mode_for(cfg, target)
    home = record.get("home") or write_home(cfg, target, decision, key, metadata,
                                            primary=bool(record.get("primary", True)))
    if home and not record.get("home"):
        record["home"] = True
        atomic_json(path, record)  # durable before the next egress: a crash must not redo this one
    telegram, detail = record.get("telegram", False), record.get("telegram_detail", "")
    rich = target["kind"] in {"task-board", "cron"}  # agent-written Markdown; relay texts stay as they are
    if not telegram and (mode == "both" or (mode == "fallback" and not home)):
        progress = {"chunks": int(record.get("telegram_chunks", 0))}

        def saved(count: int) -> None:
            record["telegram_chunks"] = count
            atomic_json(path, record)

        worker = asyncio.ensure_future(asyncio.to_thread(telegram_send, cfg, decision["text"], progress, saved, rich))
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
                if settle(cfg, record, home, telegram):
                    record["state"] = "delivered"
                atomic_json(path, record)
            raise
        record["telegram_chunks"] = progress["chunks"]
    record.update(home=home, telegram=telegram, telegram_detail=detail,
                  attempts=int(record.get("attempts", 0)) + 1, updated_at=now().isoformat())
    if (home or telegram) and not record.get("reached_at"):
        record["reached_at"] = record["updated_at"]  # the age limit of the other egress counts from here
    record["state"] = "delivered" if settle(cfg, record, home, telegram) else "pending"
    atomic_json(path, record)
    return record


def settle(cfg: dict, record: dict, home: bool, telegram: bool) -> bool:
    """Whether the receipt is complete after this egress pass. Updates the plan and, for a Task Board event,
    the dispatcher-facing receipt."""
    target = record["target"]
    mode = record.get("mode") or mode_for(cfg, target)
    if record.get("primary") and telegram and not home:
        # The inbox file could not be written and Telegram carried the report. It is now a Telegram
        # delivery: the home copy follows without the reminder mark, as the patch's copy did.
        record.update(mode="both", primary=False)
        mode = "both"
    complete = egress_complete(mode, target, home, telegram)
    if target["kind"] == "task-board" and target.get("ticket"):
        # "sent" lets the dispatcher close the event; "uncertain" tells it this event must not be sent
        # again by anyone else (recovery here finishes it and then reports "sent").
        want = "sent" if (home or telegram) else "uncertain"
        if record.get("vault_receipt") != want and vault_receipt(
                cfg, target["event"], want, record["decision"].get("receipt_class")):
            record["vault_receipt"] = want
        complete = complete and record.get("vault_receipt") == "sent"
    return complete


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
    superseded: list = []
    key = dedup_key(cfg, target, decision, metadata, superseded)
    for old in superseded:
        # An earlier notification with this text that reached neither egress is replaced by this one. Under
        # its own claim, and only if recovery has not reached the Owner with it meanwhile. Should the gateway
        # die right here, the webhook was not told success and retries: that retry is this new key again.
        old_path = receipt_path(cfg, old)
        async with claim(old_path):
            record = read_json(old_path)
            if (record and record.get("state") == "pending"
                    and not (record.get("home") or record.get("telegram"))):
                record.update(state="superseded", superseded_by=key, updated_at=now().isoformat())
                atomic_json(old_path, record)
                journal(cfg, "superseded", key=old, by=key)
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
    tracked = target["kind"] == "task-board" and bool(target.get("ticket"))
    if existing and existing.get("state") in {"suppressed", "held"} and decision["action"] == "deliver":
        existing = None  # nothing was sent under this key: a silence or a hold never consumes the report
    if tracked and existing and existing.get("state") == "suppressed" and decision["action"] == "suppress":
        existing = None  # the same silence again: write its receipt again, the dispatcher wants a fresh one
    if existing and existing.get("state") in {"delivered", "suppressed", "expired", "superseded"}:
        if tracked and existing["state"] in {"delivered", "expired"}:
            # The dispatcher only trusts a receipt newer than its own delivery attempt. If that receipt
            # cannot be written now, this redelivery is not confirmed: fail so it is retried (nothing is
            # sent again, the stored state stays as it is).
            if not vault_receipt(cfg, target["event"], "sent", existing["decision"].get("receipt_class")):
                journal(cfg, "duplicate_unrecorded", **base, first_state=existing["state"])
                return SendResult(success=False, error="boxp-notify: delivery receipt unrecorded",
                                  retryable=True)
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
        if tracked and not silence_receipt(cfg, target["event"], decision["proof"]):
            # Same rule as the hold below: without the receipt the dispatcher can neither fold the event
            # into the digest nor close it, so this is a retryable failure and nothing is stored as done.
            journal(cfg, "silence_unrecorded", **base, proof=decision.get("proof"))
            return SendResult(success=False, error="boxp-notify: silence unrecorded", retryable=True)
        if not existing:
            atomic_json(path, {"state": "suppressed", "target": target, "decision": decision,
                               "created_at": now().isoformat()})
        journal(cfg, "suppressed", **base, proof=decision.get("proof"))
        return SendResult(success=True, message_id=key)
    if tracked and held(cfg, target["event"], decision.get("proof")):
        if existing and (existing.get("home") or existing.get("telegram")):
            journal(cfg, "duplicate_dropped", **base, first_state=existing["state"])
            return SendResult(success=True, message_id=key)  # already with the Owner; recovery finishes it
        # The receipt is the only evidence of the hold: without it this is not a success.
        if not vault_receipt(cfg, target["event"], "quiet-held", decision.get("receipt_class")):
            journal(cfg, "quiet_hold_unrecorded", **base)
            return SendResult(success=False, error="boxp-notify: quiet hold unrecorded", retryable=True)
        atomic_json(path, {"state": "held", "key": key, "target": target, "decision": decision,
                           "created_at": now().isoformat()})
        journal(cfg, "quiet_held", **base, proof=decision.get("proof"))
        return SendResult(success=True, message_id=key)
    record = existing or {"state": "pending", "key": key, "target": target, "decision": decision,
                          "metadata": {k: v for k, v in (metadata or {}).items()
                                       if isinstance(v, (str, int, float, bool))},
                          "created_at": now().isoformat(),
                          **(task_board_plan(cfg, target, decision) if target["kind"] == "task-board" else {})}
    if not existing:
        atomic_json(path, record)  # durable intent before any egress
    elif tracked:
        # A redelivery of an event still pending here: the dispatcher needs a receipt newer than this
        # attempt, so the one written before no longer counts. If it cannot be written now the record
        # stays pending and recovery writes it.
        record.pop("vault_receipt", None)
    record = await deliver(cfg, path, record)
    reached = record["home"] or record["telegram"]
    journal(cfg, "delivered" if record["state"] == "delivered" else
            "delivered_partial" if reached else "delivery_failed", **base,
            home=record["home"], telegram=record["telegram"], proof=decision.get("proof"),
            mode=record.get("mode"), primary=record.get("primary"))
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
    pending = [(str(record.get("created_at") or ""), path)
               for path in (directory.glob("*.json") if directory.is_dir() else [])
               for record in [read_json(path)] if record and record.get("state") == "pending"]
    for _, path in sorted(pending):  # oldest first: a resolve and a second firing arrive in their order
        async with claim(path):
            record = read_json(path)  # a send that held the claim may have finished it meanwhile
            if not record or record.get("state") != "pending":
                continue
            target = record["target"]
            if (target["kind"] == "task-board" and target.get("ticket")
                    and held(cfg, target["event"], record["decision"].get("proof"))):
                # No Task Board send during quiet hours or once the digest owns the event. What reached
                # nothing becomes a hold, so the morning digest carries it; the rest waits.
                if not (record.get("home") or record.get("telegram")) and vault_receipt(
                        cfg, target["event"], "quiet-held", record["decision"].get("receipt_class")):
                    record.update(state="held", updated_at=now().isoformat())
                    atomic_json(path, record)
                    journal(cfg, "quiet_held", key=record.get("key") or path.stem, recovered=True)
                continue
            unconfirmed = (target["kind"] == "task-board" and bool(target.get("ticket"))
                           and record.get("vault_receipt") != "sent")
            # A Task Board event whose "sent" receipt is not in the vault yet never expires: the
            # dispatcher can only close the event from that receipt, so it is retried until the vault
            # is writable again, however long that takes (nothing already sent is sent again).
            if (not unconfirmed and (record.get("home") or record.get("telegram"))
                    and _age(record) > cfg["pending_max_age"]):
                # Reached the Owner through one egress; the other has failed for this long since then (a
                # rejected message would otherwise be retried every pass, forever). The limit counts from
                # the first egress that succeeded, not from creation: after an outage of both that outlasts
                # the limit, the egress that comes back later still gets the full window. Nothing unreached
                # is ever dropped.
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
    """Seconds since the receipt first reached the Owner (0 when that time is unknown: keep retrying)."""
    try:
        return (now() - datetime.fromisoformat(str(record.get("reached_at")))).total_seconds()
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
        journal(cfg, "connect", is_reconnect=is_reconnect, problems=problems, pending=pending_count(cfg),
                task_board_home_primary=home_primary(cfg), quiet=quiet_now(cfg))
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
            try:
                await asyncio.to_thread(migrate_cron_jobs, settings())
            except Exception:
                logger.exception("boxp-notify cron job migration failed")
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

    Same decision, receipt and egress as ``send``. No job id reaches this path, so jobs cannot be told
    apart here and nothing is treated as a retry of an earlier send (see ``dedup_key``). Pending receipts
    are finished by the gateway's recovery loop.
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


CRON_TARGET = f"{NAME}:cron"
CRON_TOOL = "cronjob_manage"  # the tool of the ``cronjob`` toolset (same name on v0.22.5 and v0.23.1)


def _cron_targets(raw: Any) -> tuple[list[str], str, bool]:
    """``(elements as written, the value with Telegram elements replaced, whether anything changed)``."""
    items = raw if isinstance(raw, (list, tuple)) else ([] if raw is None else str(raw).split(","))
    parts = [part for part in (str(item).strip() for item in items) if part]
    out: list[str] = []
    changed = False
    for part in parts:
        low = part.lower()
        if low in ("origin", "all", "telegram") or low.startswith("telegram:"):
            part, changed = CRON_TARGET, True
        if part not in out:
            out.append(part)
    return parts, ",".join(out), changed


def cron_deliver_rewrite(tool_name: Any, args: Any) -> Optional[dict]:
    """``pre_tool_call`` directive that sends a new cron job's output through this channel.

    The ``cronjob_manage`` tool addresses a job to the chat it was created from when ``deliver`` is omitted
    (``origin``), which for the Owner is Telegram. Those elements, ``all`` (every home channel, i.e.
    Telegram) and explicit ``telegram[:chat[:thread]]`` targets become ``boxp_notify:cron``; anything else
    (``local``, another platform, an existing ``boxp_notify`` target) is left as written. An update is
    touched only for the fields it sets. ``failure_deliver`` follows the same rule when given (an omitted
    one falls back to ``deliver``).
    """
    if tool_name != CRON_TOOL or not isinstance(args, dict):
        return None
    action = str(args.get("action") or "").strip().lower()
    if action not in ("create", "update"):
        return None
    modified: dict[str, str] = {}
    parts, value, changed = _cron_targets(args.get("deliver"))
    if not parts and action == "create":
        value, changed = CRON_TARGET, True
    if changed:
        modified["deliver"] = value
    parts, value, changed = _cron_targets(args.get("failure_deliver"))
    if changed:
        modified["failure_deliver"] = value
    return {"action": "modify", "args": modified} if modified else None


def _cron_rewrite_on() -> bool:
    return _env("BOXP_NOTIFY_ENABLED") == "1" and _env("BOXP_NOTIFY_CRON_REWRITE", "1") == "1"


def _cron_hook(*, tool_name=None, args=None, **_kwargs):
    try:
        if not _cron_rewrite_on():
            return None
        directive = cron_deliver_rewrite(tool_name, args)
        if directive is not None:
            journal(settings(), "cron_deliver_rewritten", action=str(args.get("action")),
                    before={k: str(args.get(k))[:200] for k in directive["args"]}, after=directive["args"])
        return directive
    except Exception:
        logger.exception("boxp-notify cron deliver rewrite failed")
        return None


_cron_edit_failed: dict[str, float] = {}


def cron_jobs_to_migrate(cfg: dict) -> list[tuple[str, dict[str, str]]]:
    """Stored cron jobs that would still run and deliver to Telegram: ``[(job id, {field: new value})]``.

    Jobs saved before the hook existed, or created outside the agent (``hermes cron create``), never pass
    ``pre_tool_call``. Finished or disabled jobs are left alone. A stored job with no ``deliver`` is local.
    """
    try:
        data = json.loads((cfg["hermes_home"] / "cron/jobs.json").read_text())
    except (OSError, ValueError):
        return []
    found = []
    for job in (data.get("jobs") if isinstance(data, dict) else data) or []:
        if not isinstance(job, dict) or job.get("enabled") is False or job.get("state") == "completed":
            continue
        job_id = str(job.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
            continue
        fields = {}
        for field in ("deliver", "failure_deliver"):
            _parts, value, changed = _cron_targets(job.get(field))
            if changed:
                fields[field] = value
        if fields:
            found.append((job_id, fields))
    return found


def migrate_cron_jobs(cfg: dict, run=None) -> int:
    """Point the stored jobs found by ``cron_jobs_to_migrate`` at this channel with the official
    ``hermes cron edit``; the job file itself is only read. A job whose edit failed is retried after an hour."""
    if not _cron_rewrite_on():
        return 0
    import shutil
    import subprocess
    import time
    binary = shutil.which("hermes") or "/opt/hermes/bin/hermes"
    done = 0
    for job_id, fields in cron_jobs_to_migrate(cfg):
        if time.monotonic() - _cron_edit_failed.get(job_id, -3600.0) < 3600:
            continue
        command = [binary, "cron", "edit", job_id]
        for field, value in fields.items():
            command += ["--" + field.replace("_", "-"), value]
        try:
            result = (run or subprocess.run)(command, capture_output=True, text=True, timeout=90)
            ok = result.returncode == 0
        except Exception as error:
            ok, result = False, error
        still = dict(cron_jobs_to_migrate(cfg)).get(job_id) if ok else fields
        if still:
            _cron_edit_failed[job_id] = time.monotonic()
            journal(cfg, "cron_job_migration_failed", job_id=job_id, fields=fields,
                    detail=(type(result).__name__ if isinstance(result, Exception) else f"exit={result.returncode}"))
            logger.warning("boxp-notify could not move cron job %s to the channel", job_id)
        else:
            done += 1
            journal(cfg, "cron_job_migrated", job_id=job_id, fields=fields)
    return done


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
    ctx.register_hook("pre_tool_call", _cron_hook)
