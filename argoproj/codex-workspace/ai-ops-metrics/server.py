"""BOXP-360: read-only /metrics for the AI ops ledger exposition file.

Serves only the low-cardinality metrics.prom written by the ledger writer (no IDs, no bodies).
A missing or stale file is reported by ai_ops_metrics_file_present / _age_seconds instead of
failing the scrape, so collector problems stay visible without touching the workload.

BOXP-372: also serves the subscription usage exposition (rate-limit windows and extra-usage
state, no IDs or bodies) with its own ai_subscription_metrics_file_present / _age_seconds.

BOXP-204: also serves the agent CLI auth expiry exposition (non-secret expiry timestamps and
enums written by agent_auth_expiry.bb, never a token) with ai_agent_auth_metrics_file_present /
_age_seconds.

BOXP-570: also serves ai_ops_ticket_title_info{ticket,title} so the authenticated Grafana
dashboard can show each ticket ID with its current title. Every ticket file and every ticket
with a run directory is listed (so no ticket in the Langfuse usage drops out of the selector),
at most TICKET_TITLE_LIMIT (newest IDs first), with the title from the ticket file's
"# BOXP-N: title" heading. A missing file or heading gives title="" (the dashboard then shows the ID only);
nothing is guessed. Titles stay inside the cluster (Prometheus, Grafana); no body is read
beyond the heading.

BOXP-769: also serves lolice_incident_* from the incident records (Incidents/Tickets/INC-N.md,
Incidents/Synthetic/SYN-N.md frontmatter, schema incident/v1). Labels are IDs and enums only;
titles and evidence text are never exported.
"""
import os
import json
import math
import re
from datetime import datetime, timezone
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = os.environ.get("AI_OPS_METRICS_FILE", "/data/ai-ops/metrics.prom")
SUBSCRIPTION_PATH = os.environ.get("AI_SUBSCRIPTION_METRICS_FILE", "")
AGENT_AUTH_PATH = os.environ.get("AI_AGENT_AUTH_METRICS_FILE", "")
PREPAID_DIR = os.environ.get("AI_PREPAID_DIR", "")
PORT = int(os.environ.get("PORT", "9464"))
MAX_BYTES = 1024 * 1024
TICKET_RUNS_DIR = os.environ.get("TICKET_RUNS_DIR", "")
TICKETS_DIR = os.environ.get("TICKETS_DIR", "")
TICKET_TITLE_LIMIT = 5000
TICKET_TITLE_CHARS = 60
TICKET_TITLE_TTL = 300
TICKET_ID = re.compile(r"BOXP-\d{1,6}")
_title_cache = {"at": 0.0, "body": b""}


def read_exposition(path):
    body, present, age = b"", 0, -1
    try:
        with open(path, "rb") as fh:
            body = fh.read(MAX_BYTES)
        present, age = 1, int(time.time() - os.path.getmtime(path))
    except OSError:
        pass
    if body and not body.endswith(b"\n"):
        body += b"\n"
    return body, present, age


def file_metrics(prefix, what, present, age):
    return (f"# HELP {prefix}_file_present Whether the {what} exposition file was readable.\n"
            f"# TYPE {prefix}_file_present gauge\n"
            f"{prefix}_file_present {present}\n"
            f"# HELP {prefix}_file_age_seconds Seconds since the {what} exposition file was written (-1 if missing).\n"
            f"# TYPE {prefix}_file_age_seconds gauge\n"
            f"{prefix}_file_age_seconds {age}\n").encode()


def prepaid_timestamp(value, utc_only=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("invalid timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if utc_only and dt.utcoffset().total_seconds() != 0:
        raise ValueError("baseline must be UTC")
    return dt.timestamp()


def prepaid_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("invalid number")
    return value


def prepaid_read(path):
    try:
        with open(path, "rb") as fh:
            oversized = os.fstat(fh.fileno()).st_size > MAX_BYTES
            data = fh.read(MAX_BYTES)
        return data, oversized, True
    except OSError:
        return b"", False, False


def prepaid_metrics(directory):
    provider = 'provider="gemini_api"'
    scope = provider + ',scope="billing_account"'
    lines = []
    def metric(name, help_text, samples):
        lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} gauge"))
        lines.extend(f"{name}{{{labels}}} {value}" for labels, value in samples)
    def label(value, allowed):
        return value if value in allowed else "other"

    data, oversized, readable = prepaid_read(os.path.join(directory, "baseline.json"))
    baseline = None
    try:
        if not readable or oversized:
            raise ValueError("unreadable baseline")
        obj = json.loads(data)
        if not isinstance(obj, dict) or obj.get("schema") != "gemini-prepaid-baseline/v1":
            raise ValueError("invalid schema")
        amount = prepaid_number(obj.get("amount"))
        currency, source = obj.get("currency"), obj.get("source")
        if not isinstance(currency, str) or not currency or not isinstance(source, str):
            raise ValueError("invalid labels")
        observed = prepaid_timestamp(obj.get("observed_at"), utc_only=True)
        baseline = (amount, currency, source, observed)
    except (ValueError, TypeError, OverflowError, UnicodeError, OSError, RecursionError):
        pass
    # The ledger only covers usage if a producer attests that it records every call
    # since (at or before) the baseline. An empty ledger without that attestation is
    # "usage not tracked", never zero usage.
    data, oversized, coverage_readable = prepaid_read(os.path.join(directory, "coverage.json"))
    covered = False
    try:
        if coverage_readable and not oversized:
            obj = json.loads(data)
            if not isinstance(obj, dict) or obj.get("schema") != "gemini-prepaid-coverage/v1":
                raise ValueError("invalid schema")
            clients = obj.get("clients")
            if not isinstance(clients, list) or not clients or not all(isinstance(c, str) and c for c in clients):
                raise ValueError("invalid clients")
            since = prepaid_timestamp(obj.get("recording_since"), utc_only=True)
            covered = baseline is not None and since <= baseline[3]
    except (ValueError, TypeError, OverflowError, UnicodeError, OSError, RecursionError):
        covered = False
    data, oversized, ledger_present = prepaid_read(os.path.join(directory, "usage.jsonl"))
    invalid = int(oversized)
    totals = {"estimate": 0.0, "actual": 0.0}
    count, last, mismatch = 0, None, False
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("invalid event")
            ts = prepaid_timestamp(obj.get("ts"))
            cost = prepaid_number(obj.get("estimated_cost"))
            currency, basis = obj.get("currency"), obj.get("basis")
            if cost < 0 or not isinstance(currency, str) or not currency or basis not in totals or not isinstance(obj.get("client"), str) or not isinstance(obj.get("model"), str):
                raise ValueError("invalid event")
            last = ts if last is None else max(last, ts)
            if baseline and ts > baseline[3]:
                if currency != baseline[1]:
                    mismatch = True
                total = totals[basis] + cost
                if not math.isfinite(total):
                    raise ValueError("usage total overflow")
                totals[basis] = total
                count += 1
        except (ValueError, TypeError, OverflowError, UnicodeError, OSError, RecursionError):
            invalid += 1
    state = ("baseline_missing" if not readable and not os.path.exists(os.path.join(directory, "baseline.json")) else
             "baseline_invalid" if not baseline else
             # An absent/unreadable ledger is "unknown usage", never zero usage.
             "usage_ledger_missing" if not ledger_present else
             "usage_coverage_unconfirmed" if not covered else "currency_mismatch" if mismatch else
             "usage_ledger_invalid" if invalid else "ok")
    # Google provides no official API for the actual prepaid balance.
    metric("ai_prepaid_balance_actual_state", "Availability of the actual prepaid balance.", [(scope + ',state="unsupported"', 1)])
    metric("ai_prepaid_baseline_present", "Whether the baseline is readable and valid.", [(provider, int(baseline is not None))])
    currency_label = label(baseline[1], {"USD", "JPY", "EUR"}) if baseline else None
    metric("ai_prepaid_baseline_amount", "Owner observed baseline amount.", [(scope + f',currency="{currency_label}",source="{label(baseline[2], {"ai_studio_ui_owner"})}"', baseline[0])] if baseline else [])
    metric("ai_prepaid_baseline_observed_timestamp_seconds", "Baseline observation timestamp.", [(provider, baseline[3])] if baseline else [])
    metric("ai_prepaid_usage_ledger_present", "Whether the usage ledger is readable.", [(provider, int(ledger_present))])
    metric("ai_prepaid_usage_coverage_confirmed", "Whether a producer attests the ledger covers all usage since the baseline.", [(provider, int(covered))])
    metric("ai_prepaid_usage_ledger_invalid_lines", "Number of invalid usage ledger lines.", [(provider, invalid)])
    metric("ai_prepaid_tracked_usage_since_baseline", "Tracked usage after the baseline by basis.", [(provider + f',currency="{currency_label}",basis="{basis}"', total) for basis, total in totals.items()] if baseline and ledger_present else [])
    metric("ai_prepaid_tracked_usage_events_since_baseline", "Number of valid events after the baseline.", [(provider, count)] if baseline and ledger_present else [])
    metric("ai_prepaid_usage_last_event_timestamp_seconds", "Latest valid usage event timestamp.", [(provider, last)] if last is not None else [])
    metric("ai_prepaid_balance_estimated_remaining", "Estimated remaining balance.", [(scope + f',currency="{currency_label}"', baseline[0] - sum(totals.values()))] if state == "ok" else [])
    metric("ai_prepaid_estimate_state", "One-hot estimate state.", [(provider + f',state="{s}"', int(state == s)) for s in ("ok", "baseline_missing", "baseline_invalid", "usage_ledger_missing", "usage_ledger_invalid", "currency_mismatch", "usage_coverage_unconfirmed")])
    metric("ai_prepaid_metrics_generated_timestamp_seconds", "Collector generation timestamp.", [(provider, time.time())])
    return ("\n".join(lines) + "\n").encode()


def ticket_title(tickets_dir, ticket):
    try:
        with open(os.path.join(tickets_dir, ticket + ".md"), "rb") as fh:
            head = fh.read(16 * 1024).decode("utf-8", "replace")
    except OSError:
        return ""
    m = re.search(r"^# " + re.escape(ticket) + r":[ \t]*(.+)$", head, re.M)
    if not m:
        return ""
    # Prometheus label value and the Grafana variable regex: no quotes, backslashes or control chars.
    title = re.sub(r"[\x00-\x1f\x7f]", " ", m.group(1)).replace("\\", "＼").replace('"', "”").strip()
    title = re.sub(r"\s+", " ", title)
    return title[:TICKET_TITLE_CHARS - 1] + "…" if len(title) > TICKET_TITLE_CHARS else title


def ticket_title_metrics(runs_dir, tickets_dir, now=None):
    # Every ticket file (the Task Board's ticket set) plus every ticket with a run directory, so any
    # ticket that can appear in the Langfuse usage is selectable; a run without a file keeps title="".
    ids = set()
    for directory, suffix in ((tickets_dir, ".md"), (runs_dir, "")):
        if not directory:
            continue
        try:
            names = [e.name for e in os.scandir(directory)]
        except OSError:
            continue
        for name in names:
            tid = name[:-len(suffix)] if suffix and name.endswith(suffix) else (name if not suffix else "")
            if TICKET_ID.fullmatch(tid):
                ids.add(tid)
    ordered = sorted(ids, key=lambda t: int(t.split("-", 1)[1]), reverse=True)
    lines = ["# HELP ai_ops_ticket_title_info Current title of each Task Board ticket (empty if unknown).",
             "# TYPE ai_ops_ticket_title_info gauge"]
    lines += [f'ai_ops_ticket_title_info{{ticket="{t}",title="{ticket_title(tickets_dir, t)}"}} 1' for t in ordered[:TICKET_TITLE_LIMIT]]
    lines += ["# HELP ai_ops_ticket_title_truncated Whether tickets were left out by the series limit.",
              "# TYPE ai_ops_ticket_title_truncated gauge",
              f"ai_ops_ticket_title_truncated {int(len(ordered) > TICKET_TITLE_LIMIT)}"]
    return ("\n".join(lines) + "\n").encode()


def cached_ticket_titles():
    now = time.time()
    if now - _title_cache["at"] > TICKET_TITLE_TTL:
        _title_cache["body"] = ticket_title_metrics(TICKET_RUNS_DIR, TICKETS_DIR, now)
        _title_cache["at"] = now
    return _title_cache["body"]


# ---- BOXP-769: incident records -> lolice_incident_* (spec: Obsidian Projects/lolice-incident-recovery/spec.md) ----
INCIDENTS_DIR = os.environ.get("INCIDENTS_DIR", "")
INCIDENT_LIMIT = 2000
INCIDENT_TTL = 60
_incident_cache = {"at": 0.0, "body": b""}
INCIDENT_FILE = re.compile(r"(INC|SYN)-\d{1,6}")
INCIDENT_TIME_FIELDS = (("started", "started"), ("detected", "detected"), ("cause_deployed_at", "cause_deployed"),
                        ("recovery_at", "recovery_action"), ("impact_resolved", "impact_resolved"))
INCIDENT_ENUMS = {
    "cause": {"", "deployment", "non_deployment", "unknown"},
    "recovery": {"", "rollback", "revert", "hotfix", "forward_fix", "restart", "manual_op", "self_healed", "external",
                 "unknown"},
}
REDEPLOY = {"rollback", "revert", "hotfix", "forward_fix"}
NO_REDEPLOY = {"restart", "manual_op", "self_healed", "external"}
SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.:/-]")


def parse_incident_time(value):
    """UTC. ISO-8601 ('...Z' / offset) or the older 'YYYY-MM-DD HH:mm' (taken as UTC). None if unreadable."""
    v = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(v, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def read_frontmatter(path):
    try:
        with open(path, "rb") as fh:
            text = fh.read(64 * 1024).decode("utf-8", "replace")
    except OSError:
        return None
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] in (" ", "\t", "-") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            try:
                v = json.loads(v)
            except ValueError:
                v = v.strip('"')
        out[k.strip()] = str(v).strip() if v is not None else ""
    return out


def classify_incident(iid, f):
    """One record -> dict (spec 2 章 1-6). Problems are a set of enum names."""
    g = lambda k: (f.get(k) or "").strip()
    r = {"id": iid, "service": SAFE_LABEL.sub("_", g("service"))[:60], "problems": set(), "times": {},
         "synthetic": iid.startswith("SYN-") or g("synthetic").lower() == "true",
         "cause": g("cause") or "unknown", "recovery": g("recovery") or "unknown",
         "key": g("dedupe_key") or g("service"), "app": SAFE_LABEL.sub("_", g("cause_app"))[:60]}
    if g("schema") != "incident/v1":
        r.update(state="legacy", recovery_state="legacy", cause_state="legacy")
        return r
    for field, point in INCIDENT_TIME_FIELDS:
        if g(field):
            t = parse_incident_time(g(field))
            if t is None:
                r["problems"].add("bad_time")
            else:
                r["times"][point] = t
    for field, allowed in INCIDENT_ENUMS.items():
        if g(field) not in allowed:
            r["problems"].add("bad_value")
            r[field] = "unknown"
    t = r["times"]
    # 4: recovered only with a confirmed time AND evidence; an alert resolved / recovery action is not the end
    if "impact_resolved" in t and g("impact_resolved_evidence"):
        rs = "recovered"
        if any(p in t and t["impact_resolved"] < t[p] for p in ("started", "cause_deployed")):
            r["problems"].add("time_order")
    elif "impact_resolved" in t:
        rs = "unconfirmed"
        r["problems"].add("resolved_without_evidence")
    else:
        rs = "unrecovered"
    # 5
    if r["cause"] == "deployment":
        if r["app"] and "cause_deployed" in t and g("cause_evidence"):
            cs = "deployment"
        else:
            cs = "deployment_unconfirmed"
            r["problems"].add("cause_incomplete")
    elif r["cause"] == "non_deployment":
        cs = "non_deployment"
    else:
        cs = "unknown"
    r.update(recovery_state=rs, cause_state=cs, duplicate_of=g("duplicate_of"))
    r["state"] = "invalid" if r["problems"] & {"bad_time", "bad_value", "time_order"} else "counted"
    return r


def incident_metrics(root, now=None):
    now = int(time.time() if now is None else now)
    recs = []
    present = 0
    unreadable = {"false": 0, "true": 0}
    for sub in ("Tickets", "Synthetic"):
        d = os.path.join(root, sub)
        try:
            names = sorted(e.name for e in os.scandir(d))
            # 本番の Tickets が読めたときだけ収集成功（Synthetic だけでは障害ゼロと誤表示する）
            if sub == "Tickets":
                present = 1
        except OSError:
            continue
        for name in names:
            iid = name[:-3] if name.endswith(".md") else ""
            if not INCIDENT_FILE.fullmatch(iid):
                continue
            f = read_frontmatter(os.path.join(d, name))
            if f is None:
                # 読めない記録は黙って除外しない: 問題として出し、本番なら収集失敗（件数を「不明」にする）
                syn = sub == "Synthetic"
                unreadable["true" if syn else "false"] += 1
                if not syn:
                    present = 0
                recs.append({"id": iid, "service": "", "problems": {"unreadable"}, "times": {}, "synthetic": syn,
                             "cause": "unknown", "recovery": "unknown", "key": "", "app": "", "state": "invalid",
                             "recovery_state": "unknown", "cause_state": "unknown", "duplicate_of": ""})
                continue
            recs.append(classify_incident(iid, f))
    recs = recs[:INCIDENT_LIMIT]
    ids = {r["id"] for r in recs}
    # 3: duplicates
    for r in recs:
        dup = r.get("duplicate_of")
        if r["state"] == "counted" and dup:
            if dup in ids and dup != r["id"]:
                r["state"] = "duplicate"
            else:
                r["state"] = "invalid"
                r["problems"].add("duplicate_target_missing")
    # 7: overlapping open intervals with the same dedupe_key -> possible_duplicate on the later one
    counted = [r for r in recs if r["state"] == "counted"]
    def span(r):
        t = r["times"]
        s = t.get("started", t.get("detected"))
        e = t["impact_resolved"] if r["recovery_state"] == "recovered" else now
        return s, e
    for r in counted:
        s, e = span(r)
        if s is None or not r["key"]:
            continue
        for o in counted:
            if o is r or o["key"] != r["key"]:
                continue
            os_, oe = span(o)
            if os_ is None:
                continue
            if (os_, o["id"]) < (s, r["id"]) and os_ <= e and s <= oe:
                r["problems"].add("possible_duplicate")
    lines = []
    def metric(name, help_text, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, v in samples:
            lines.append(f"{name}{{{labels}}} {v}" if labels else f"{name} {v}")
    syn = lambda r: "true" if r["synthetic"] else "false"
    def redeploy(r):
        return "true" if r["recovery"] in REDEPLOY else "false" if r["recovery"] in NO_REDEPLOY else "unknown"
    metric("lolice_incident_info", "Incident record (state: counted, legacy, duplicate, invalid).",
           [(f'incident="{r["id"]}",service="{r["service"]}",state="{r["state"]}",recovery_state="{r["recovery_state"]}",'
             f'cause_state="{r["cause_state"]}",recovery="{r["recovery"]}",redeploy="{redeploy(r)}",synthetic="{syn(r)}"', 1)
            for r in recs])
    live = [r for r in recs if r["state"] in ("counted", "duplicate", "invalid")]
    metric("lolice_incident_timestamp_seconds", "Recorded incident time points (UTC epoch).",
           [(f'incident="{r["id"]}",point="{p}",synthetic="{syn(r)}"', t) for r in live for p, t in sorted(r["times"].items())])
    ok = [r for r in counted if r["state"] == "counted" and r["recovery_state"] == "recovered"]
    metric("lolice_incident_restore_seconds", "Impact start to confirmed impact resolution (all causes).",
           [(f'incident="{r["id"]}",service="{r["service"]}",synthetic="{syn(r)}"', r["times"]["impact_resolved"] - r["times"]["started"])
            for r in ok if "started" in r["times"]])
    metric("lolice_incident_failed_deployment_recovery_seconds",
           "DORA failed deployment recovery time: causing deployment deployedAt to confirmed impact resolution.",
           [(f'incident="{r["id"]}",app="{r["app"]}",synthetic="{syn(r)}"', r["times"]["impact_resolved"] - r["times"]["cause_deployed"])
            for r in ok if r["cause_state"] == "deployment"])
    metric("lolice_incident_record_problem", "Problems found in an incident record.",
           [(f'incident="{r["id"]}",problem="{p}",synthetic="{syn(r)}"', 1) for r in recs for p in sorted(r["problems"])])
    states = ("counted", "legacy", "duplicate", "invalid")
    metric("lolice_incident_records", "Incident records by state.",
           [(f'state="{s}"', sum(1 for r in recs if r["state"] == s)) for s in states])
    metric("lolice_incident_unreadable_records", "Incident record files that could not be read.",
           [(f'synthetic="{k}"', v) for k, v in sorted(unreadable.items())])
    metric("lolice_incident_dir_present", "1 when the production incident directory and every record in it could be read.",
           [("", present)])
    metric("lolice_incident_metrics_generated_timestamp_seconds", "When the incident metrics were generated.", [("", now)])
    return ("\n".join(lines) + "\n").encode()


def cached_incidents():
    now = time.time()
    if now - _incident_cache["at"] > INCIDENT_TTL:
        _incident_cache["body"] = incident_metrics(INCIDENTS_DIR, now)
        _incident_cache["at"] = now
    return _incident_cache["body"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n")
        if self.path != "/metrics":
            return self._send(404, b"not found\n")
        body, present, age = read_exposition(PATH)
        out = body + file_metrics("ai_ops_metrics", "ledger", present, age)
        if SUBSCRIPTION_PATH:
            sub_body, sub_present, sub_age = read_exposition(SUBSCRIPTION_PATH)
            out += sub_body + file_metrics("ai_subscription_metrics", "subscription usage", sub_present, sub_age)
        if AGENT_AUTH_PATH:
            auth_body, auth_present, auth_age = read_exposition(AGENT_AUTH_PATH)
            out += auth_body + file_metrics("ai_agent_auth_metrics", "agent auth expiry", auth_present, auth_age)
        if PREPAID_DIR:
            out += prepaid_metrics(PREPAID_DIR)
        if TICKETS_DIR:
            out += cached_ticket_titles()
        if INCIDENTS_DIR:
            out += cached_incidents()
        self._send(200, out, "text/plain; version=0.0.4")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
