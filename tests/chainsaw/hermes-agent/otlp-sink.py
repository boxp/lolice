import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Same as Langfuse: a request asking for an h2c upgrade gets the connection closed, unanswered.
        if self.headers.get("Upgrade"):
            self.close_connection = True
            return
        body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
        record = {"authorization": self.headers.get("Authorization"), "body": body}
        line = json.dumps(record)
        with open(os.environ.get("SINK_FILE", "/tmp/requests.jsonl"), "a", encoding="utf-8") as output:
            output.write(line + "\n")
        print(line, flush=True)
        self.send_response(200)
        self.end_headers()

HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
