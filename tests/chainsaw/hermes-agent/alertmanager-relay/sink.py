# BOXP-462 fixture: stands in for the Hermes webhook port. Records every POST and
# answers 401 unless the route path and the X-Gitlab-Token header match.
import http.server
import json
import os

TOKEN = os.environ["EXPECTED_TOKEN"]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        ok = self.path == "/webhooks/alertmanager" and self.headers.get("X-Gitlab-Token") == TOKEN
        with open("/tmp/requests.jsonl", "a") as fh:
            fh.write(json.dumps({"ok": ok, "path": self.path, "body": body.decode()}) + "\n")
        self.send_response(200 if ok else 401)
        self.end_headers()
        self.wfile.write(b"{}")


http.server.HTTPServer(("", 8644), Handler).serve_forever()
