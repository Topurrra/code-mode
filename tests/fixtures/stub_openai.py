# SPDX-License-Identifier: MIT
"""A scripted OpenAI-compatible endpoint, for tests.

Usage: python stub_openai.py SCRIPT.json LOG.jsonl

SCRIPT.json is a list of responses, served in order to POST requests:
``{"status": 200, "headers": {...}, "body": {...}}``. Every request is
appended to LOG.jsonl so the test can assert what the agent loop sent. The
port is chosen by the OS and printed on the first line of stdout.

It stands in for a model so the loop can be tested end to end without one:
same wire format, fixed answers.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def main() -> None:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        script = json.load(handle)
    log_path = sys.argv[2]
    served = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (name fixed by http.server)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(json.dumps({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(raw or b"{}"),
                }) + "\n")
            index = served["count"]
            served["count"] += 1
            step = script[index] if index < len(script) else {"status": 500, "body": {"error": "script exhausted"}}
            data = json.dumps(step.get("body", {})).encode("utf-8")
            self.send_response(step.get("status", 200))
            for key, value in step.get("headers", {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass  # keep the test output clean

    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
