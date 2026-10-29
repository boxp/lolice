"""BOXP-360: read-only /metrics for the AI ops ledger exposition file.

Serves only the low-cardinality metrics.prom written by the ledger writer (no IDs, no bodies).
A missing or stale file is reported by ai_ops_metrics_file_present / _age_seconds instead of
failing the scrape, so collector problems stay visible without touching the workload.
"""
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PATH = os.environ.get("AI_OPS_METRICS_FILE", "/data/ai-ops/metrics.prom")
PORT = int(os.environ.get("PORT", "9464"))
MAX_BYTES = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok\n")
        if self.path != "/metrics":
            return self._send(404, b"not found\n")
        body, present, age = b"", 0, -1
        try:
            with open(PATH, "rb") as fh:
                body = fh.read(MAX_BYTES)
            present, age = 1, int(time.time() - os.path.getmtime(PATH))
        except OSError:
            pass
        tail = ("# HELP ai_ops_metrics_file_present Whether the ledger exposition file was readable.\n"
                "# TYPE ai_ops_metrics_file_present gauge\n"
                f"ai_ops_metrics_file_present {present}\n"
                "# HELP ai_ops_metrics_file_age_seconds Seconds since the exposition file was written (-1 if missing).\n"
                "# TYPE ai_ops_metrics_file_age_seconds gauge\n"
                f"ai_ops_metrics_file_age_seconds {age}\n").encode()
        if body and not body.endswith(b"\n"):
            body += b"\n"
        self._send(200, body + tail, "text/plain; version=0.0.4")

    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
