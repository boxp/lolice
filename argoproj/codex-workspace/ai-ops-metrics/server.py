"""BOXP-360: read-only /metrics for the AI ops ledger exposition file.

Serves only the low-cardinality metrics.prom written by the ledger writer (no IDs, no bodies).
A missing or stale file is reported by ai_ops_metrics_file_present / _age_seconds instead of
failing the scrape, so collector problems stay visible without touching the workload.

BOXP-372: also serves the subscription usage exposition (rate-limit windows and extra-usage
state, no IDs or bodies) with its own ai_subscription_metrics_file_present / _age_seconds.

BOXP-570: also serves ai_ops_ticket_title_info{ticket,title} so the authenticated Grafana
dashboard can show each ticket ID with its current title. Every ticket file and every ticket
with a run directory is listed (so no ticket in the Langfuse usage drops out of the selector),
at most TICKET_TITLE_LIMIT (newest IDs first), with the title from the ticket file's
"# BOXP-N: title" heading. A missing file or heading gives title="" (the dashboard then shows the ID only);
nothing is guessed. Titles stay inside the cluster (Prometheus, Grafana); no body is read
beyond the heading.
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
        if PREPAID_DIR:
            out += prepaid_metrics(PREPAID_DIR)
        if TICKETS_DIR:
            out += cached_ticket_titles()
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
