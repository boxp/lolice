"""BOXP-360: read-only /metrics for the AI ops ledger exposition file.

Serves only the low-cardinality metrics.prom written by the ledger writer (no IDs, no bodies).
A missing or stale file is reported by ai_ops_metrics_file_present / _age_seconds instead of
failing the scrape, so collector problems stay visible without touching the workload.

BOXP-372: also serves the subscription usage exposition (rate-limit windows and extra-usage
state, no IDs or bodies) with its own ai_subscription_metrics_file_present / _age_seconds.
"""
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = os.environ.get("AI_OPS_METRICS_FILE", "/data/ai-ops/metrics.prom")
SUBSCRIPTION_PATH = os.environ.get("AI_SUBSCRIPTION_METRICS_FILE", "")
PORT = int(os.environ.get("PORT", "9464"))
MAX_BYTES = 1024 * 1024


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
