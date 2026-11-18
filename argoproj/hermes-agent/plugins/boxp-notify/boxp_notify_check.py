"""BOXP-546 lab: channel checks that need no Hermes image (gateway imports are stubbed).

Usage: python3 boxp_notify_check.py   (local HTTP stub only; nothing leaves the machine)
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

base = types.ModuleType("gateway.platforms.base")
base.BasePlatformAdapter = object
base.SendResult = lambda **kw: types.SimpleNamespace(**{"success": False, "error": None, **kw})
config = types.ModuleType("gateway.config")
config.Platform = config.PlatformConfig = object
sys.modules.update({"gateway": types.ModuleType("gateway"), "gateway.platforms": types.ModuleType("gateway.platforms"),
                    "gateway.platforms.base": base, "gateway.config": config})
spec = importlib.util.spec_from_file_location(
    "boxp_notify", Path(__file__).resolve().with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
# CI (Renovate の朝の run など) は 02:00-09:00 JST の静音時間帯にも走る。実時刻のままだと静音判定で
# 配送系の check が時刻次第で FAIL するため、既定の時計を日中 (12:00 JST) 起点で実時間どおり進める。
# 静音時間帯の check は従来どおり個別に plugin.now を差し替える。
_wall_now = plugin.now
_day_offset = plugin.datetime(2026, 11, 14, 3, 0, tzinfo=plugin.timezone.utc) - _wall_now()
plugin.now = lambda: _wall_now() + _day_offset

telegram = {"fail": False, "delay": 0.0, "sent": [], "bodies": [], "reject_html": False}


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        time.sleep(telegram["delay"])
        if telegram["reject_html"] and body.get("parse_mode"):
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not telegram["fail"]:
            telegram["sent"].append(body["text"])
            telegram["bodies"].append(body)
        reply = json.dumps({"ok": not telegram["fail"], "result": {"message_id": len(telegram["sent"])}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


LIB = os.environ.get("CHECK_LIB") == "1"  # loaded by the child of check 4: plugin only, parent's environment
if not LIB:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(tempfile.mkdtemp(prefix="boxp-notify-check."))
    (root / "vault/Tickets").mkdir(parents=True)
    # Events 1-9 of the delivery checks are formal reports: the Notes hold the helper's marker for each.
    (root / "vault/Tickets/BOXP-9001.md").write_text("".join(
        f"- t [kohako-event]: [kohako-event] handled event=BOXP-9001-20261114T00020{n}Z-cccc000{n}-done"
        f" run=r result=notified summary=s\n" for n in range(1, 10)))
    os.environ["HERMES_HOME"] = str(root / "hermes")
    os.environ.update(BOXP_NOTIFY_STATE_DIR=str(root / "state"), BOXP_NOTIFY_VAULT=str(root / "vault"),
        BOXP_NOTIFY_TELEGRAM_MODE="both", BOXP_NOTIFY_TELEGRAM_TOKEN="lab", BOXP_NOTIFY_TELEGRAM_CHAT_ID="1",
        BOXP_NOTIFY_TELEGRAM_API_BASE=f"http://127.0.0.1:{server.server_port}")
failures = []


def check(label, ok):
    print(("ok   " if ok else "FAIL ") + label)
    ok or failures.append(label)


def event(n):
    return f"BOXP-9001-20261114T00020{n}Z-cccc000{n}-done"


def state(ev):
    return plugin.read_json(plugin.receipt_path(plugin.settings(), f"tb-{ev}.report"))["state"]


def inbox(ev):
    return (root / "vault/Projects/task-board-events/delivered-copies" / f"{ev}.md").exists()


TBE = "vault/Projects/task-board-events"


def home_copy(ev, kind=""):
    return plugin.read_json(root / TBE / "delivered-copies" / f"{ev}{kind}.md") or {}


def receipt(ev):
    value = plugin.read_json(root / TBE / "delivery-receipts" / f"{ev}.md") or {}
    return value.get("state"), value.get("classification")


def home_ready(ready, age=0):
    """delivery-routing.md asks for home; the reminder route and the dispatcher heartbeat make it effective."""
    (root / TBE).mkdir(parents=True, exist_ok=True)
    (root / TBE / "delivery-routing.md").write_text("# routing\n\ntask-board: home\n")
    (root / "hermes").mkdir(exist_ok=True)
    (root / "hermes/webhook_subscriptions.json").write_text(json.dumps({"kohako-home-reminder": {"enabled": True}}))
    stamp = plugin.datetime.fromtimestamp(plugin.now().timestamp() - age, plugin.timezone.utc).isoformat().replace("+00:00", "Z")
    (root / TBE / "home-reminder-ready.md").write_text(json.dumps({"reminder_ready": ready, "checked_at": stamp}))


def note(ticket, ev, result):
    with (root / f"vault/Tickets/{ticket}.md").open("a") as stream:
        stream.write(f"- t [kohako-event]: [kohako-event] handled event={ev} run=r result={result} summary=s\n")


async def tb(ev, text):
    return await plugin.send_once(plugin.settings(), f"tb:{ev}", text, None)


async def stage2():
    """BOXP-554: the Task Board gate (same contract as the core patch) and the cron path."""
    def ev(n, kind="done"):
        return f"BOXP-9002-20261114T0003{n:02d}Z-dddd{n:04d}-{kind}"
    silence = "[SILENT]"
    telegram.update(sent=[], bodies=[])

    e = ev(1)  # silence without the Notes marker is never granted: a notice, to Telegram, with a home copy
    await tb(e, silence)
    check("gate: unproven silence becomes one notice on Telegram",
          len(telegram["sent"]) == 1 and telegram["sent"][0].startswith(plugin.NOTICE) and "missing_reply" in telegram["sent"][0])
    check("gate: the notice has its own home copy without the reminder mark, receipt sent/unresolved",
          home_copy(e, ".notice").get("text", "").startswith(plugin.NOTICE) and "primary" not in home_copy(e, ".notice")
          and not home_copy(e) and receipt(e) == ("sent", "unresolved"))
    note("BOXP-9002", e, "notified")
    await tb(e, f"【Task Board 通知】BOXP-9002 **完了** event={e}")
    check("gate: the formal report still arrives after the notice (home, primary)",
          home_copy(e).get("primary") == "home" and len(telegram["sent"]) == 1 and receipt(e) == ("sent", "notified"))
    await tb(e, f"【Task Board 通知】BOXP-9002 **完了** event={e}")
    check("gate: the same report again adds nothing", len(telegram["sent"]) == 1)

    e = ev(2)  # proven silence
    note("BOXP-9002", e, "silent")
    await tb(e, silence)
    check("gate: silence with the Notes marker sends nothing",
          len(telegram["sent"]) == 1 and not home_copy(e) and receipt(e) == (None, None))
    e = ev(3)  # deferred at night: silent, and the receipt lets the dispatcher fold it into the digest
    note("BOXP-9002", e, "deferred")
    await tb(e, silence)
    check("gate: a deferred silence leaves the silent/deferred receipt", receipt(e) == ("silent", "deferred"))
    await tb(e, f"遅い報告 event={e}")
    check("gate: a late report of a deferred event is held for the digest",
          receipt(e) == ("quiet-held", "unresolved") and not home_copy(e) and len(telegram["sent"]) == 1)
    digest = "BOXP-9002-digest-20261114T000400Z-abcdef012345"
    note("BOXP-9002", digest, "silent")
    await tb(digest, silence)
    check("gate: a digest ends silently only with the silent receipt", receipt(digest) == ("silent", "silent"))

    e = ev(13)  # the receipt of a deferred silence cannot be written: not a success, nothing stored as done
    note("BOXP-9002", e, "deferred")
    fail = root / "state/lab-fail-home"
    fail.touch()
    result = await tb(e, silence)
    check("gate: a deferred silence whose receipt cannot be written is a retryable failure, not stored",
          not result.success and getattr(result, "retryable", False) and receipt(e) == (None, None)
          and plugin.read_json(plugin.receipt_path(plugin.settings(), f"tb-{e}.report")) is None)
    fail.unlink()
    result = await tb(e, silence)
    check("gate: the retry writes the receipt and succeeds",
          result.success and receipt(e) == ("silent", "deferred") and state(e) == "suppressed")
    (root / TBE / "delivery-receipts" / f"{e}.md").unlink()
    result = await tb(e, silence)
    check("gate: the same silence delivered again writes its receipt again",
          result.success and receipt(e) == ("silent", "deferred") and len(telegram["sent"]) == 1)
    e = ev(4)  # a report whose marker is missing: delivered with the notice attached, to Telegram
    await tb(e, f"報告 event={e}")
    check("gate: a report without the marker goes to Telegram with the notice attached, receipt unresolved",
          len(telegram["sent"]) == 2 and "missing_or_conflicting_receipt" in telegram["sent"][1]
          and "primary" not in home_copy(e) and home_copy(e).get("text") == telegram["sent"][1] and receipt(e) == ("sent", "unresolved"))

    e = ev(5)  # quiet hours hold everything; the hold does not consume the report
    note("BOXP-9002", e, "notified")
    real_now = plugin.now
    plugin.now = lambda: plugin.datetime(2026, 11, 14, 18, 30, tzinfo=plugin.timezone.utc)  # 03:30 JST
    await tb(e, f"夜の報告 event={e}")
    held = (receipt(e), bool(home_copy(e)), len(telegram["sent"]))
    plugin.now = real_now
    check("quiet: nothing is sent during quiet hours, receipt quiet-held", held == (("quiet-held", "notified"), False, 2))
    (root / TBE / "quiet-policy.md").write_text(json.dumps({"quiet_hours": "0-24", "timezone": "Asia/Tokyo"}))
    await tb(e, f"夜の報告 event={e}")
    check("quiet: the dispatcher's published window is honoured", not home_copy(e))
    (root / TBE / "quiet-policy.md").unlink()
    await tb(e, f"夜の報告 event={e}")
    check("quiet: after the window the same event is delivered", home_copy(e).get("primary") == "home" and receipt(e) == ("sent", "notified"))

    e = ev(6)  # the dispatcher claimed the event for the digest
    note("BOXP-9002", e, "notified")
    (root / TBE / "delivery-claims").mkdir(exist_ok=True)
    (root / TBE / "delivery-claims" / f"{e}.md").write_text("{}")
    await tb(e, f"報告 event={e}")
    check("digest claim: a late report is held", receipt(e) == ("quiet-held", "notified") and not home_copy(e))

    e = ev(7)  # home requested but the reminder is not ready: Telegram stays primary
    note("BOXP-9002", e, "question")
    home_ready(True, age=7200)
    await tb(e, f"質問 event={e}")
    home_ready(True)
    check("routing: a stale reminder heartbeat keeps Telegram primary (copy without the reminder mark)",
          len(telegram["sent"]) == 3 and "primary" not in home_copy(e) and receipt(e) == ("sent", "question"))

    e = ev(8)  # the inbox cannot be written: Telegram carries the report, the home copy follows unmarked
    note("BOXP-9002", e, "notified")
    (root / "state/lab-fail-home").touch()
    result = await tb(e, f"報告 event={e}")
    check("home down: Telegram carries the report, the receipt stays pending",
          result.success and len(telegram["sent"]) == 4 and plugin.pending_count(plugin.settings()) == 1)
    (root / "state/lab-fail-home").unlink()
    await plugin.recover(plugin.settings())
    check("home back: the copy follows without the reminder mark, receipt sent, Telegram not repeated",
          len(telegram["sent"]) == 4 and home_copy(e).get("text") and "primary" not in home_copy(e)
          and receipt(e) == ("sent", "notified") and plugin.pending_count(plugin.settings()) == 0)

    e = ev(14)  # Telegram carried it, the vault stays unwritable past the age limit: never expired unrecorded
    note("BOXP-9002", e, "notified")
    (root / "state/lab-fail-home").touch()
    sent = len(telegram["sent"])
    await tb(e, f"報告 event={e}")
    os.environ["BOXP_NOTIFY_PENDING_MAX_AGE_SECONDS"] = "1"
    await asyncio.sleep(1.2)
    await plugin.recover(plugin.settings())
    check("vault down past the age limit: the event stays pending, no receipt, Telegram not repeated",
          state(e) == "pending" and receipt(e) == (None, None) and len(telegram["sent"]) == sent + 1)
    (root / "state/lab-fail-home").unlink()
    await plugin.recover(plugin.settings())
    os.environ["BOXP_NOTIFY_PENDING_MAX_AGE_SECONDS"] = ""
    check("vault back after the age limit: receipt sent so the dispatcher can close, Telegram not repeated",
          state(e) == "delivered" and receipt(e) == ("sent", "notified") and home_copy(e).get("text")
          and len(telegram["sent"]) == sent + 1)
    telegram["sent"] = telegram["sent"][:sent]

    e = ev(9)  # nothing reachable, then quiet hours begin: recovery turns it into a hold, not a night send
    note("BOXP-9002", e, "notified")
    (root / "state/lab-fail-home").touch()
    telegram["fail"] = True
    result = await tb(e, f"報告 event={e}")
    (root / "state/lab-fail-home").unlink()
    telegram["fail"] = False
    plugin.now = lambda: plugin.datetime(2026, 11, 14, 18, 30, tzinfo=plugin.timezone.utc)
    await plugin.recover(plugin.settings())
    plugin.now = real_now
    check("recovery in quiet hours: an unreached report becomes a hold",
          not result.success and receipt(e) == ("quiet-held", "notified") and not home_copy(e) and len(telegram["sent"]) == 4
          and plugin.pending_count(plugin.settings()) == 0)

    e = ev(10)  # a text that is not the report of this event: a notice, and the real report still follows
    note("BOXP-9002", e, "notified")
    await tb(e, "Sorry, I encountered an error")
    await tb(e, f"報告 event={e}")
    check("unverified: delivered as a notice, the report keeps its own slot",
          "unverified_response" in home_copy(e, ".notice").get("text", "") and home_copy(e).get("primary") == "home")
    before = receipt(e)
    (root / TBE / "delivery-receipts" / f"{e}.md").unlink()
    await tb(e, f"報告 event={e}")
    check("duplicate: the receipt is written again for the dispatcher", receipt(e) == before)
    (root / TBE / "delivery-receipts" / f"{e}.md").unlink()
    (root / "state/lab-fail-home").write_text("1")
    sent, copy = len(telegram["sent"]), home_copy(e)
    result = await tb(e, f"報告 event={e}")
    check("duplicate, receipt unwritable: a retryable failure, nothing sent again",
          not result.success and result.retryable and receipt(e) == (None, None)
          and len(telegram["sent"]) == sent and home_copy(e) == copy)
    (root / "state/lab-fail-home").unlink()
    result = await tb(e, f"報告 event={e}")
    check("duplicate, retried after the store is back: the receipt is written, still nothing sent",
          result.success and receipt(e) == before and len(telegram["sent"]) == sent and home_copy(e) == copy)
    await tb(e, "⏳ retrying (1/3)")
    check("status lines are never a result", len(telegram["sent"]) == 5)

    # cron: home and Telegram, name from the scheduler's job file, Markdown rendered on Telegram
    os.environ["BOXP_NOTIFY_TELEGRAM_MODE_CRON"] = "both"
    (root / "hermes/cron").mkdir()
    (root / "hermes/cron/jobs.json").write_text(json.dumps({"jobs": [{"id": "74a03bb016d3", "name": "Morning Report cronjob"}]}))
    telegram.update(sent=[], bodies=[])
    cron_dir = root / "vault/Projects/cron-deliveries/delivered-copies"
    seen = set(cron_dir.glob("*.md"))
    meta = {"job_id": "74a03bb016d3"}

    async def cron(text):
        return await plugin.send_once(plugin.settings(), "cron", text, meta)

    def cron_copies():
        return [json.loads(c.read_text()) for c in sorted(set(cron_dir.glob("*.md")) - seen, key=lambda c: c.stat().st_mtime_ns)]
    text = "ご主人さま、**朝の報告**です。\n- `a<b` [票](https://example.com/x?a=1&b=2)"
    await cron(text)
    made = cron_copies()
    check("cron: home copy holds the raw text, the job name and id",
          len(made) == 1 and made[0]["text"] == text and made[0]["job_name"] == "Morning Report cronjob"
          and made[0]["job_id"] == "74a03bb016d3" and made[0]["failure"] is False)
    check("cron: Telegram gets the same report once, Markdown rendered as HTML",
          len(telegram["bodies"]) == 1 and telegram["bodies"][0].get("parse_mode") == "HTML"
          and telegram["bodies"][0]["text"] == 'ご主人さま、<b>朝の報告</b>です。\n- <code>a&lt;b</code> <a href="https://example.com/x?a=1&amp;b=2">票</a>')
    await cron(text)
    check("cron: the same text again right away is a retry of that run", len(cron_copies()) == 1 and len(telegram["sent"]) == 1)
    await cron("別の報告")
    await cron(text)
    check("cron: the same text after another report is a new run", len(cron_copies()) == 3 and len(telegram["sent"]) == 3)
    os.environ["BOXP_NOTIFY_CRON_DEDUP_SECONDS"] = "1"
    await asyncio.sleep(1.2)
    await cron(text)
    os.environ["BOXP_NOTIFY_CRON_DEDUP_SECONDS"] = ""
    check("cron: the same text after the retry window is a new run", len(cron_copies()) == 4 and len(telegram["sent"]) == 4)
    await cron("⚠️ Cron 'Morning Report cronjob' failed: boom")
    check("cron: a failed run is marked", cron_copies()[-1]["failure"] is True)
    await cron(silence)
    check("cron: a silence marker sends nothing", len(cron_copies()) == 5 and len(telegram["sent"]) == 5)
    telegram["reject_html"] = True
    await cron("**壊れた <markup")
    telegram["reject_html"] = False
    check("cron: Telegram refusing the markup gets the plain text instead",
          telegram["sent"][-1] == "**壊れた <markup" and "parse_mode" not in telegram["bodies"][-1])
    # two jobs on the one cron target saying the same thing within the retry window are two notifications
    before, sent = len(cron_copies()), len(telegram["sent"])
    await plugin.send_once(plugin.settings(), "cron", "同じ本文の報告", {"job_id": "aaaa00000001"})
    await plugin.send_once(plugin.settings(), "cron", "同じ本文の報告", {"job_id": "bbbb00000002"})
    check("cron: the same text from another job is not a retry",
          len(cron_copies()) == before + 2 and len(telegram["sent"]) == sent + 2)
    # the out-of-process sender gets no job id: jobs cannot be told apart, so nothing is dropped as a retry
    before, sent = len(cron_copies()), len(telegram["sent"])
    first = await plugin._standalone_send(None, "cron", "job を区別できない報告")
    second = await plugin._standalone_send(None, "cron", "job を区別できない報告")
    check("cron: without a job id the same text again is delivered, not dropped",
          first.get("success") and second.get("success")
          and len(cron_copies()) == before + 2 and len(telegram["sent"]) == sent + 2)
    os.environ["BOXP_NOTIFY_TELEGRAM_MODE_CRON"] = ""
    telegram.update(sent=[], bodies=[])


def cron_rewrite():
    """BOXP-556: a cron target moves to the channel only when it means exactly the home Telegram chat and
    topic (here chat 42, topic 9, the gateway's only home channel)."""
    session = {}
    context = types.ModuleType("gateway.session_context")
    context.get_session_env = lambda name, default="": session.get(name, default)
    sys.modules["gateway.session_context"] = context

    def deliver(args, tool="cronjob_manage", origin=("telegram", "42", "9")):
        session.clear()
        if origin:
            session.update(HERMES_SESSION_PLATFORM=origin[0], HERMES_SESSION_CHAT_ID=origin[1],
                           HERMES_SESSION_THREAD_ID=origin[2])
        directive = plugin.cron_deliver_rewrite(tool, args)
        return None if directive is None else (directive["action"], directive["args"])
    to_channel = ("modify", {"deliver": "boxp_notify:cron"})
    home_ui, discord, cli = ("api_server", "s1", ""), ("discord", "5", ""), None
    check("cron rewrite: a create without deliver goes to the channel when created from the home Telegram chat",
          deliver({"action": "create", "schedule": "every 1h", "prompt": "x"}) == to_channel
          and deliver({"action": "create", "deliver": ""}) == to_channel
          and deliver({"action": "Create", "deliver": "origin"}) == to_channel
          and deliver({"action": "create"}, origin=home_ui) == to_channel)
    check("cron rewrite: a create from another chat, topic or platform, or from the CLI, stays as written",
          deliver({"action": "create"}, origin=("telegram", "42", "8")) is None
          and deliver({"action": "create"}, origin=("telegram", "43", "9")) is None
          and deliver({"action": "create", "deliver": "origin"}, origin=discord) is None
          and deliver({"action": "create"}, origin=cli) is None
          and deliver({"action": "create", "deliver": "origin"}, origin=cli) is None)
    check("cron rewrite: the home Telegram chat and all go to the channel, once",
          deliver({"action": "create", "deliver": "telegram"}, origin=discord) == to_channel
          and deliver({"action": "create", "deliver": "telegram:42:9"}, origin=cli) == to_channel
          and deliver({"action": "create", "deliver": ["telegram", "origin"]}) == to_channel
          and deliver({"action": "create", "deliver": "origin,all"}) == to_channel)
    check("cron rewrite: another Telegram chat or topic keeps its target",
          deliver({"action": "create", "deliver": "telegram:-100123:17"}) is None
          and deliver({"action": "create", "deliver": "telegram:42"}) is None
          and deliver({"action": "create", "deliver": "telegram:42:8"}) is None
          and deliver({"action": "update", "job_id": "a", "deliver": "telegram:1"}) is None
          and deliver({"action": "create", "deliver": "telegram:1,telegram"})
          == ("modify", {"deliver": "telegram:1,boxp_notify:cron"}))
    os.environ["TELEGRAM_CRON_THREAD_ID"] = "8"
    check("cron rewrite: a cron topic other than the channel's keeps telegram, all and a home UI job as written",
          deliver({"action": "create", "deliver": "telegram"}) is None
          and deliver({"action": "create", "deliver": "all"}) is None
          and deliver({"action": "create"}, origin=home_ui) is None
          and deliver({"action": "update", "job_id": "a", "failure_deliver": "telegram"}) is None
          and deliver({"action": "create", "deliver": "telegram:42:9"}) == to_channel
          and deliver({"action": "create"}) == to_channel)
    del os.environ["TELEGRAM_CRON_THREAD_ID"]
    os.environ["DISCORD_HOME_CHANNEL"] = "5"
    check("cron rewrite: with another home channel, all and a home UI job stay as written",
          deliver({"action": "create", "deliver": "all"}) is None
          and deliver({"action": "create"}, origin=home_ui) is None
          and deliver({"action": "create"}) == to_channel)
    del os.environ["DISCORD_HOME_CHANNEL"]
    config_file = plugin.settings()["hermes_home"] / "config.yaml"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    kept = config_file.read_text() if config_file.exists() else None
    config_file.write_text("platforms:\n  slack:\n    home_channel: C1\n")
    check("cron rewrite: a home_channel in config.yaml keeps all as written",
          deliver({"action": "create", "deliver": "all"}) is None)
    config_file.write_text(kept) if kept is not None else config_file.unlink()
    check("cron rewrite: other elements are kept in order",
          deliver({"action": "create", "deliver": "local,telegram"}) == ("modify", {"deliver": "local,boxp_notify:cron"})
          and deliver({"action": "update", "job_id": "a", "deliver": "telegram:42:9"}) == to_channel)
    check("cron rewrite: local, the channel itself, other platforms and non-delivery updates are untouched",
          deliver({"action": "create", "deliver": "local"}) is None
          and deliver({"action": "create", "deliver": "boxp_notify:cron"}) is None
          and deliver({"action": "create", "deliver": "discord:1"}) is None
          and deliver({"action": "update", "job_id": "a", "prompt": "y"}) is None
          and deliver({"action": "update", "job_id": "a", "deliver": "origin"}) is None
          and deliver({"action": "update", "job_id": "a", "failure_deliver": "local"}) is None)
    check("cron rewrite: a failure target for the home Telegram chat goes to the channel too",
          deliver({"action": "update", "job_id": "a", "failure_deliver": "telegram"})
          == ("modify", {"failure_deliver": "boxp_notify:cron"})
          and deliver({"action": "create", "deliver": "boxp_notify:cron", "failure_deliver": "telegram:42:9"})
          == ("modify", {"failure_deliver": "boxp_notify:cron"})
          and deliver({"action": "create", "failure_deliver": "telegram"})
          == ("modify", {"deliver": "boxp_notify:cron", "failure_deliver": "boxp_notify:cron"})
          and deliver({"action": "create", "deliver": "local", "failure_deliver": "telegram:1"}) is None)
    check("cron rewrite: other actions, other tools and malformed calls are untouched",
          deliver({"action": "list"}) is None and deliver({"action": "run", "job_id": "a"}) is None
          and deliver({"action": "create"}, tool="terminal") is None and deliver({"action": "create"}, tool="cronjob") is None and deliver(None) is None
          and deliver({"deliver": "telegram"}) is None)
    session.update(HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="42", HERMES_SESSION_THREAD_ID="9")
    os.environ["BOXP_NOTIFY_ENABLED"] = "1"
    check("cron rewrite: the hook follows the switches",
          plugin._cron_hook(tool_name="cronjob_manage", args={"action": "create"}, session_id="s") is not None
          and not os.environ.update(BOXP_NOTIFY_CRON_REWRITE="0")
          and plugin._cron_hook(tool_name="cronjob_manage", args={"action": "create"}) is None
          and not os.environ.update(BOXP_NOTIFY_CRON_REWRITE="1", BOXP_NOTIFY_ENABLED="")
          and plugin._cron_hook(tool_name="cronjob_manage", args={"action": "create"}) is None)
    os.environ.pop("BOXP_NOTIFY_CRON_REWRITE", None)
    # stored jobs: only jobs that will still run and still name Telegram are edited, through the CLI
    os.environ["BOXP_NOTIFY_ENABLED"] = "1"
    cfg = plugin.settings()
    jobs_file = cfg["hermes_home"] / "cron/jobs.json"
    jobs_file.parent.mkdir(parents=True, exist_ok=True)
    home, other = {"platform": "telegram", "chat_id": "42", "thread_id": "9"}, {"platform": "discord", "chat_id": "5"}
    jobs = [{"id": "aaaa00000001", "enabled": True, "state": "scheduled", "deliver": "boxp_notify:cron"},
            {"id": "aaaa00000002", "enabled": True, "state": "scheduled", "deliver": "boxp_notify:cron",
             "failure_deliver": "telegram"},
            {"id": "aaaa00000003", "enabled": True, "state": "scheduled", "deliver": "origin", "origin": home},
            {"id": "aaaa00000004", "enabled": False, "state": "completed", "deliver": "telegram"},
            {"id": "aaaa00000005", "enabled": True, "state": "scheduled", "deliver": "local"},
            {"id": "aaaa00000006", "enabled": True, "state": "scheduled", "deliver": None},
            {"id": "bad id; rm", "enabled": True, "state": "scheduled", "deliver": "telegram"},
            {"id": "aaaa00000008", "enabled": True, "state": "scheduled", "deliver": "origin", "origin": other},
            {"id": "aaaa00000009", "enabled": True, "state": "scheduled", "deliver": "telegram:1"},
            {"id": "aaaa0000000a", "enabled": True, "state": "scheduled", "deliver": "origin",
             "origin": {"platform": "telegram", "chat_id": "42", "thread_id": "8"}},
            {"id": "aaaa00000007", "enabled": True, "state": "scheduled", "deliver": "telegram:42:9"}]
    jobs_file.write_text(json.dumps({"jobs": jobs}))
    check("cron migration: only live jobs meaning the home Telegram chat are selected",
          plugin.cron_jobs_to_migrate(cfg) == [("aaaa00000002", {"failure_deliver": "boxp_notify:cron"}),
                                               ("aaaa00000003", {"deliver": "boxp_notify:cron"}),
                                               ("aaaa00000007", {"deliver": "boxp_notify:cron"})])
    calls = []

    def fake_cli(command, **_kw):
        calls.append(command[1:])
        if command[3] == "aaaa00000007":
            return types.SimpleNamespace(returncode=1)
        for job in jobs:
            if job["id"] == command[3]:
                job[command[4][2:].replace("-", "_")] = command[5]
        jobs_file.write_text(json.dumps({"jobs": jobs}))
        return types.SimpleNamespace(returncode=0)
    moved = plugin.migrate_cron_jobs(cfg, run=fake_cli)
    check("cron migration: edits go through `hermes cron edit`, a failed one is reported and not retried at once",
          moved == 2 and calls == [["cron", "edit", "aaaa00000002", "--failure-deliver", "boxp_notify:cron"],
                                   ["cron", "edit", "aaaa00000003", "--deliver", "boxp_notify:cron"],
                                   ["cron", "edit", "aaaa00000007", "--deliver", "boxp_notify:cron"]]
          and plugin.migrate_cron_jobs(cfg, run=fake_cli) == 0 and len(calls) == 3)
    os.environ["BOXP_NOTIFY_CRON_REWRITE"] = "0"
    plugin._cron_edit_failed.clear()
    check("cron migration: follows the switch", plugin.migrate_cron_jobs(cfg, run=fake_cli) == 0 and len(calls) == 3)
    os.environ.pop("BOXP_NOTIFY_CRON_REWRITE", None)
    jobs_file.unlink()


async def send(ev):
    return await plugin.send_once(plugin.settings(), f"tb:{ev}", f"報告 event={ev}", None)


async def main():
    # 1. mode both, Telegram down: home alone must not close the receipt; recovery sends Telegram once.
    ev = event(1)
    telegram["fail"] = True
    result = await send(ev)
    check("both/telegram down: send reports success (home reached, no upstream failure follow-up)", result.success)
    check("both/telegram down: home inbox written", inbox(ev))
    check("both/telegram down: receipt stays pending", state(ev) == "pending")
    telegram["fail"] = False
    check("both/telegram back: recovery finishes 1 receipt", await plugin.recover(plugin.settings()) == 1)
    check("both/telegram back: delivered, Telegram exactly once", state(ev) == "delivered" and len(telegram["sent"]) == 1)
    await send(ev)
    await plugin.recover(plugin.settings())
    check("both: redelivery and second recovery add nothing", len(telegram["sent"]) == 1)

    # 2. recovery passes run while a send is inside a slow Telegram call: one message, not two.
    ev = event(2)
    telegram.update(delay=1.0, sent=[])
    sending = asyncio.create_task(send(ev))
    await asyncio.sleep(0.3)
    check("race: receipt is pending while Telegram is in flight", state(ev) == "pending")
    await asyncio.gather(plugin.recover(plugin.settings()), plugin.recover(plugin.settings()), sending)
    check("race: recovery during an in-flight send -> Telegram exactly once", len(telegram["sent"]) == 1)
    check("race: delivered", state(ev) == "delivered")

    # 3. same race against another process (the standalone cron path): the file lock holds recovery back.
    ev = event(3)
    telegram.update(delay=0.0, fail=True, sent=[])
    await send(ev)
    telegram["fail"] = False
    cfg = plugin.settings()
    stem = plugin.receipt_path(cfg, f"tb-{ev}.report").stem
    holder = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); time.sleep(1.5)",
        str(cfg["state"] / "receipts" / f".{stem}.lock"), stdout=asyncio.subprocess.PIPE)
    await holder.stdout.readline()
    recovering = asyncio.create_task(plugin.recover(cfg))
    await asyncio.sleep(0.5)
    check("cross-process: recovery waits while another process holds the claim", not telegram["sent"])
    await holder.wait()
    check("cross-process: recovery proceeds after release, once", await recovering == 1 and len(telegram["sent"]) == 1)

    # 4. long text, process dies after Telegram accepted the first chunk: recovery resumes at the second.
    ev = event(5)
    telegram["sent"] = []
    text = f"報告 event={ev} " + "あ" * 4000 + "い" * 100
    code = ("import asyncio,os,runpy; m=runpy.run_path(os.environ['CHECK'],run_name='lib');"
            "p=m['plugin']; real=p.atomic_json\n"
            "def dying(path,payload):\n"
            "    real(path,payload)\n"
            "    if payload.get('telegram_chunks')==1: os._exit(9)\n"
            "p.atomic_json=dying\n"
            "asyncio.run(p.send_once(p.settings(),'tb:'+os.environ['EV'],os.environ['TEXT'],None))")
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, env={**os.environ, "CHECK": __file__, "EV": ev, "TEXT": text, "CHECK_LIB": "1"})
    check("chunks: sender process died after the first chunk", await child.wait() == 9 and len(telegram["sent"]) == 1)
    await plugin.recover(plugin.settings())
    check("chunks: recovery sends only the remaining chunk",
          state(ev) == "delivered" and len(telegram["sent"]) == 2 and "".join(telegram["sent"]) == text)

    # 5. send cancelled (shutdown) while Telegram is in flight: claim held to the end, outcome recorded.
    ev = event(7)
    telegram.update(delay=1.0, sent=[])
    sending = asyncio.create_task(send(ev))
    await asyncio.sleep(0.3)
    sending.cancel()
    recovering = asyncio.create_task(plugin.recover(plugin.settings()))
    await asyncio.gather(sending, recovering, return_exceptions=True)
    await plugin.recover(plugin.settings())
    check("cancel: cancelled in-flight send plus recovery -> Telegram exactly once",
          sending.cancelled() and state(ev) == "delivered" and len(telegram["sent"]) == 1)
    telegram["delay"] = 0.0

    # 6. (BOXP-554) Task Board home-primary: a formal report goes to the home inbox alone.
    os.environ["BOXP_NOTIFY_TELEGRAM_MODE"] = "fallback"
    home_ready(True)
    ev = event(6)
    telegram["sent"] = []
    await send(ev)
    check("fallback: home alone is delivered, Telegram untouched", state(ev) == "delivered" and not telegram["sent"])
    check("home primary: the copy carries primary=home and the dispatcher receipt says sent/notified",
          home_copy(ev).get("primary") == "home" and receipt(ev) == ("sent", "notified"))

    # 7. (BOXP-553) receipt and inbox names do not depend on the key length: two cron runs whose long
    #    job ids differ only past the old 180-character cut are two notifications.
    cfg = plugin.settings()
    long_a, long_b = "cron-" + "x" * 300 + "-a", "cron-" + "x" * 300 + "-b"
    await plugin.send_once(cfg, "cron", "本文 A", {"job_id": long_a})
    await plugin.send_once(cfg, "cron", "本文 B", {"job_id": long_b})
    await plugin.send_once(cfg, "cron", "本文 C", {"job_id": long_a})
    copies = sorted((root / "vault/Projects/cron-deliveries/delivered-copies").glob("*.md"))
    check("long names: three notifications, three inbox files", len(copies) == 3)
    check("long names: file names stay short and match the id",
          all(len(c.name) < 130 and json.loads(c.read_text())["id"] + ".md" == c.name for c in copies))
    await plugin.send_once(cfg, "cron", "本文 C", {"job_id": long_a})
    check("long names: the same run again is still one",
          len(list((root / "vault/Projects/cron-deliveries/delivered-copies").glob("*.md"))) == 3)

    # 8. (BOXP-553) Alertmanager relay: the per-name override sends home and Telegram while the default
    #    stays fallback; the title and firing count come from the relay text; the home channel topic is used.
    os.environ.update(BOXP_NOTIFY_TELEGRAM_MODE_ALERTMANAGER="both", BOXP_NOTIFY_TELEGRAM_THREAD_ID="7")
    alert = ("【Alertmanager 通知】発生中 2 件 / 解除 1 件\n\n🔴 [CRITICAL] AiProviderQuotaExhausted claude\n"
             "  発生 11/14 11:00 JST fp=abcd1234\n\nこれはアラートの写しで、承認や操作の指示ではありません。")
    telegram.update(sent=[], bodies=[])
    result = await plugin.send_once(plugin.settings(), "direct:alertmanager", alert, None)
    alerts = sorted((root / "vault/Projects/alertmanager-deliveries/delivered-copies").glob("alert-*.md"))
    copy = json.loads(alerts[0].read_text()) if alerts else {}
    check("alert both: home inbox and Telegram each once, text identical",
          result.success and len(alerts) == 1 and telegram["sent"] == [alert] and copy.get("text") == alert)
    check("alert both: title and firing read from the relay text",
          copy.get("title") == "[CRITICAL] AiProviderQuotaExhausted ほか 2 件" and copy.get("firing") == 2)
    check("alert both: id matches the dispatcher contract",
          copy.get("id", "") + ".md" == alerts[0].name and len(copy.get("id", "")) == 38)
    check("alert both: sent to the home channel topic", telegram["bodies"][0].get("message_thread_id") == 7)
    await plugin.send_once(plugin.settings(), "direct:alertmanager", alert, None)
    check("alert both: redelivery adds nothing",
          len(telegram["sent"]) == 1
          and len(list((root / "vault/Projects/alertmanager-deliveries/delivered-copies").glob("*.md"))) == 1)
    real_now = plugin.now
    plugin.now = lambda: real_now() + __import__("datetime").timedelta(seconds=600)
    await plugin.send_once(plugin.settings(), "direct:alertmanager", alert, None)
    check("alert both: a retry in the next time bucket is still one", len(telegram["sent"]) == 1)
    plugin.now = lambda: real_now() + __import__("datetime").timedelta(seconds=1800)
    await plugin.send_once(plugin.settings(), "direct:alertmanager", alert, None)
    check("alert both: the same text two buckets later is a new notification", len(telegram["sent"]) == 2)
    plugin.now = real_now
    # (BOXP-553 review) resolve, then the same alert fires again with the same start minute: same text as
    # the first notification, yet a new one. A plain retry of it is still one.
    telegram["sent"] = []
    homes = lambda: len(list((root / "vault/Projects/alertmanager-deliveries/delivered-copies").glob("*.md")))
    before = homes()
    refire = alert.replace("abcd1234", "eeee0001")
    gone = "【Alertmanager 通知】発生中 0 件 / 解除 1 件\n\n✅ [CRITICAL] AiProviderQuotaExhausted claude\n  解除 11/14 11:00 JST fp=eeee0001"
    for text in (refire, refire, gone, refire, refire):
        await plugin.send_once(plugin.settings(), "direct:alertmanager", text, None)
    check("alert refire: fire, resolve, fire again in the same minute are three notifications",
          telegram["sent"] == [refire, gone, refire] and homes() == before + 3)
    # A webhook retry of a notification that reached nothing stays one, even after another notification.
    telegram.update(sent=[], fail=True)
    (root / "state/lab-fail-home").touch()
    lost = alert.replace("abcd1234", "eeee0002")
    result = await plugin.send_once(plugin.settings(), "direct:alertmanager", lost, None)
    (root / "state/lab-fail-home").unlink()
    telegram["fail"] = False
    await plugin.send_once(plugin.settings(), "direct:alertmanager", gone.replace("eeee0001", "eeee0003"), None)
    await plugin.send_once(plugin.settings(), "direct:alertmanager", lost, None)
    await plugin.recover(plugin.settings())
    check("alert refire: a retry of an unfinished notification is not a second one",
          not result.success and telegram["sent"].count(lost) == 1 and plugin.pending_count(plugin.settings()) == 0)
    # (BOXP-553 review) both egresses are down while the alert fires, resolves and fires again with the same
    # text (each one retried by the webhook): after they come back the Owner gets fire, resolve... and the
    # second firing last, not a resolve as the final word. The unreached first firing is replaced by it.
    telegram.update(sent=[], fail=True)
    (root / "state/lab-fail-home").touch()
    down, down_gone = alert.replace("abcd1234", "eeee0005"), gone.replace("eeee0001", "eeee0005")
    before = homes()
    results = [await plugin.send_once(plugin.settings(), "direct:alertmanager", text, None)
               for text in (down, down, down_gone, down_gone, down, down)]
    (root / "state/lab-fail-home").unlink()
    telegram["fail"] = False
    await plugin.recover(plugin.settings())
    await plugin.recover(plugin.settings())
    check("alert refire while both are down: the second firing is delivered after the resolve, once",
          not any(r.success for r in results) and telegram["sent"] == [down_gone, down]
          and homes() == before + 2 and plugin.pending_count(plugin.settings()) == 0)
    # (BOXP-553 review) the first firing is half delivered (home yes, Telegram down) when the alert resolves
    # and fires again with the same text: the second firing is a new notification for the home inbox too,
    # and recovery still finishes the first one on Telegram.
    telegram.update(sent=[], fail=True)
    half = alert.replace("abcd1234", "eeee0004")
    half_gone = gone.replace("eeee0001", "eeee0004")
    before = homes()
    results = [await plugin.send_once(plugin.settings(), "direct:alertmanager", text, None)
               for text in (half, half_gone, half)]
    check("alert refire while half delivered: home gets fire, resolve and the second fire",
          all(r.success for r in results) and homes() == before + 3 and results[0].message_id != results[2].message_id)
    telegram["fail"] = False
    await plugin.recover(plugin.settings())
    await plugin.recover(plugin.settings())
    check("alert refire while half delivered: Telegram recovers all three, once each",
          sorted(telegram["sent"]) == sorted([half, half_gone, half]) and homes() == before + 3
          and plugin.pending_count(plugin.settings()) == 0)
    telegram["sent"] = []
    resolved = "【Alertmanager 通知】発生中 0 件 / 解除 1 件\n\n✅ [WARNING] AiOpsOutcomeBurnSlow\n  解除 11/14 11:05 JST fp=abcd1234"
    check("alert: resolved-only title", plugin.alert_summary(resolved, "") == ("解除: AiOpsOutcomeBurnSlow", 0))
    check("alert: unknown text falls back to the first line", plugin.alert_summary("x\ny", "3") == ("x", 3))

    # 9. (BOXP-553) alert both, Telegram down: home is written, Telegram is recovered once; with the vault
    #    unwritable Telegram still goes out and home is recovered once.
    telegram.update(sent=[], fail=True)
    other = alert.replace("abcd1234", "ffff0001")
    result = await plugin.send_once(plugin.settings(), "direct:alertmanager", other, None)
    check("alert/telegram down: success to upstream, pending", result.success and plugin.pending_count(plugin.settings()) == 1)
    telegram["fail"] = False
    await plugin.recover(plugin.settings())
    await plugin.recover(plugin.settings())
    check("alert/telegram back: recovered exactly once",
          telegram["sent"] == [other] and plugin.pending_count(plugin.settings()) == 0)
    telegram["sent"] = []
    third = alert.replace("abcd1234", "ffff0002")
    (root / "state/lab-fail-home").touch()
    result = await plugin.send_once(plugin.settings(), "direct:alertmanager", third, None)
    before = len(list((root / "vault/Projects/alertmanager-deliveries/delivered-copies").glob("*.md")))
    check("alert/home down: Telegram still delivered, pending", result.success and telegram["sent"] == [third]
          and plugin.pending_count(plugin.settings()) == 1)
    (root / "state/lab-fail-home").unlink()
    await plugin.recover(plugin.settings())
    await plugin.recover(plugin.settings())
    after = len(list((root / "vault/Projects/alertmanager-deliveries/delivered-copies").glob("*.md")))
    check("alert/home back: home recovered once, Telegram not repeated",
          after == before + 1 and telegram["sent"] == [third] and plugin.pending_count(plugin.settings()) == 0)

    # 10. (BOXP-553) a half-delivered receipt stops being retried after the age limit; an unreached one never.
    telegram.update(sent=[], fail=True)
    fourth = alert.replace("abcd1234", "ffff0003")
    await plugin.send_once(plugin.settings(), "direct:alertmanager", fourth, None)
    (root / "state/lab-fail-home").touch()
    await plugin.send_once(plugin.settings(), "direct:alertmanager", alert.replace("abcd1234", "ffff0004"), None)
    os.environ["BOXP_NOTIFY_PENDING_MAX_AGE_SECONDS"] = "1"
    await asyncio.sleep(1.2)
    await plugin.recover(plugin.settings())
    states = sorted(json.loads(p.read_text())["state"]
                    for p in (root / "state/receipts").glob("direct-alertmanager-*.json"))
    check("expiry: half-delivered expires, unreached stays pending",
          states.count("expired") == 1 and states.count("pending") == 1)
    (root / "state/lab-fail-home").unlink()
    telegram["fail"] = False
    await plugin.recover(plugin.settings())
    check("expiry: the unreached one is delivered once both egresses are back",
          plugin.pending_count(plugin.settings()) == 0 and len(telegram["sent"]) == 1)
    await plugin.recover(plugin.settings())
    check("expiry: an expired receipt is not delivered again", len(telegram["sent"]) == 1)
    # 10b. both egresses down for longer than the age limit, then only the home inbox comes back: the limit
    # counts from that first success, so Telegram is still recovered when it comes back later.
    telegram.update(sent=[], fail=True)
    (root / "state/lab-fail-home").touch()
    fifth = alert.replace("abcd1234", "ffff0005")
    result = await plugin.send_once(plugin.settings(), "direct:alertmanager", fifth, None)
    await asyncio.sleep(1.2)  # older than the 1 s limit, never reached
    (root / "state/lab-fail-home").unlink()
    await plugin.recover(plugin.settings())  # home only
    await plugin.recover(plugin.settings())  # must not expire: reached just now
    check("long outage: one egress back does not expire the other",
          not result.success and plugin.pending_count(plugin.settings()) == 1 and telegram["sent"] == [])
    telegram["fail"] = False
    await plugin.recover(plugin.settings())
    check("long outage: the other egress is recovered once it is back",
          plugin.pending_count(plugin.settings()) == 0 and telegram["sent"] == [fifth])
    telegram.update(sent=[], fail=True)
    await plugin.send_once(plugin.settings(), "direct:alertmanager", alert.replace("abcd1234", "ffff0006"), None)
    await asyncio.sleep(1.2)
    await plugin.recover(plugin.settings())
    check("long outage: the limit still ends retries counted from the first success",
          plugin.pending_count(plugin.settings()) == 0 and telegram["sent"] == [])
    telegram["fail"] = False
    os.environ["BOXP_NOTIFY_TELEGRAM_CHAT_ID"] = os.environ["BOXP_NOTIFY_TELEGRAM_TOKEN"] = ""
    os.environ.update(TELEGRAM_BOT_TOKEN="gw", TELEGRAM_HOME_CHANNEL="42", TELEGRAM_HOME_CHANNEL_THREAD_ID="9",
                      BOXP_NOTIFY_TELEGRAM_THREAD_ID="")
    cfg = plugin.settings()
    check("settings: falls back to the gateway's Telegram home channel",
          (cfg["telegram_token"], cfg["telegram_chat"], cfg["telegram_thread"]) == ("gw", "42", "9")
          and plugin.self_check(cfg) == [])
    await stage2()
    cron_rewrite()

if not LIB:
    asyncio.run(main())
    sys.exit(1 if failures else 0)
