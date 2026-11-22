# SPDX-License-Identifier: MIT
"""A small MCP server over HTTP, for the end-to-end suite. Standard library
only. It is not used by the application.

It speaks both remote transports the MCP spec defines, on one port:

  POST   /mcp        streamable HTTP. Answers with JSON, or with an event
                     stream for the tools that report progress.
  DELETE /mcp        ends a session.
  GET    /sse        HTTP+SSE (2024-11-05): opens the stream, names the
                     message endpoint in its first event.
  POST   /messages   messages for an SSE session; answers come on the stream.
  GET    /state      what the server has seen (for assertions).

Usage: python http_mcp_server.py [TOKEN]

Prints the port it listens on as the first line of stdout. With TOKEN, every
request must carry ``Authorization: Bearer TOKEN``.
"""

import json
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
SESSIONS = {}      # streamable HTTP: session id -> True
STREAMS = {}       # SSE: session id -> queue of messages to send
SEEN = {"initialize": 0, "deleted": [], "protocol_headers": [], "cancelled": [], "calls": 0}

TOOLS = [
    {"name": "add", "description": "Add two integers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
     "outputSchema": {"type": "object", "properties": {"sum": {"type": "integer"}}}},
    {"name": "count", "description": "Count to n, reporting progress on an event stream, then return n.",
     "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}},
    {"name": "whoami", "description": "Return the session id and the headers the server saw.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "echo_auth", "description": "Return the Authorization header the server received.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "drop_sessions", "description": "Forget every session, as a server does when it restarts.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Wait before answering.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]}},
]


def respond(request_id, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    return message


def run_tool(name, args, headers, session, emit):
    """Return a tools/call result. ``emit`` sends a notification on the
    stream of the current request, when there is one."""
    SEEN["calls"] += 1
    if name == "add":
        return {"content": [{"type": "text", "text": str(args["a"] + args["b"])}], "structuredContent": {"sum": args["a"] + args["b"]}}
    if name == "count":
        return {"content": [], "structuredContent": {"counted": int(args["n"])}}
    if name == "whoami":
        return {"content": [], "structuredContent": {
            "session": session, "protocol": headers.get("MCP-Protocol-Version"), "accept": headers.get("Accept"), "initialized": SEEN["initialize"]}}
    if name == "echo_auth":
        return {"content": [{"type": "text", "text": "the server received: %s" % headers.get("Authorization")}]}
    if name == "drop_sessions":
        SESSIONS.clear()
        return {"content": [{"type": "text", "text": "dropped"}]}
    if name == "slow":
        time.sleep(float(args.get("seconds", 0)))
        return {"content": [{"type": "text", "text": "slept"}]}
    return {"content": [{"type": "text", "text": "unknown tool %s" % name}], "isError": True}


def handle_rpc(message, headers, session, emit):
    """One JSON-RPC message in, a response out (None for a notification)."""
    method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "notifications/cancelled":
        SEEN["cancelled"].append(params.get("requestId"))
        return None
    if request_id is None:
        return None
    if method == "initialize":
        SEEN["initialize"] += 1
        return respond(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                                    "serverInfo": {"name": "http-fixture", "version": "1"}})
    if method == "tools/list":
        return respond(request_id, {"tools": TOOLS})
    if method == "tools/call":
        token = (params.get("_meta") or {}).get("progressToken")
        args = params.get("arguments") or {}
        if params.get("name") == "count" and emit is not None:
            for i in range(int(args.get("n", 0))):
                emit({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": i + 1, "total": int(args["n"]), "message": "counting"}})
                time.sleep(0.05)
        return respond(request_id, run_tool(params.get("name"), args, headers, session, emit))
    if method == "ping":
        return respond(request_id, {})
    return respond(request_id, error={"code": -32601, "message": "method not found: %s" % method})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b"", content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if TOKEN and self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._reply(401, b'{"error": "missing or wrong bearer token"}')
            return False
        return True

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/state":
            self._reply(200, json.dumps(SEEN).encode())
            return
        if url.path != "/sse":
            self._reply(404, b"not found", "text/plain")
            return
        if not self._authorized():
            return
        session = uuid.uuid4().hex
        STREAMS[session] = queue.Queue()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()  # no Content-Length: the stream ends when the connection does
        self.close_connection = True
        try:
            self.wfile.write(b": a comment line, which a client must ignore\n\n")
            self.wfile.write(("event: endpoint\ndata: /messages?session=%s\n\n" % session).encode())
            self.wfile.flush()
            while True:
                try:
                    message = STREAMS[session].get(timeout=0.5)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if message is None:
                    return
                # The data is split over two lines on purpose: a client must
                # join them with a line break, which JSON reads as a space.
                text = json.dumps(message)
                self.wfile.write(("event: message\ndata: %s\ndata: %s\n\n" % (text[:1], text[1:])).encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            STREAMS.pop(session, None)

    def do_DELETE(self):
        session = self.headers.get("Mcp-Session-Id")
        SESSIONS.pop(session, None)
        SEEN["deleted"].append(session)
        self._reply(200, b"{}")

    def do_POST(self):
        url = urlsplit(self.path)
        if not self._authorized():
            return
        message = self._body()
        if url.path == "/messages":
            session = (parse_qs(url.query).get("session") or [""])[0]
            stream = STREAMS.get(session)
            if stream is None:
                self._reply(404, b'{"error": "no such session"}')
                return
            self._reply(202, b"")

            def work():
                reply = handle_rpc(message, self.headers, session, stream.put)
                if reply is not None:
                    stream.put(reply)

            threading.Thread(target=work, daemon=True).start()
            return
        if url.path != "/mcp":
            self._reply(404, b"<html>this is a web page, not an MCP endpoint</html>", "text/html")
            return
        method = message.get("method") if isinstance(message, dict) else None
        session = self.headers.get("Mcp-Session-Id")
        if method == "initialize":
            session = uuid.uuid4().hex
            SESSIONS[session] = True
        elif session not in SESSIONS:
            self._reply(404, b'{"error": "unknown session"}')
            return
        if method != "initialize":
            SEEN["protocol_headers"].append(self.headers.get("MCP-Protocol-Version"))
        streaming = method == "tools/call" and (message.get("params") or {}).get("name") == "count"
        if not streaming:
            reply = handle_rpc(message, self.headers, session, None)
            if reply is None:
                self._reply(202, b"")
            else:
                self._reply(200, json.dumps(reply).encode(), extra={"Mcp-Session-Id": session})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.close_connection = True

        def emit(notification):
            self.wfile.write(("event: message\ndata: %s\n\n" % json.dumps(notification)).encode())
            self.wfile.flush()

        reply = handle_rpc(message, self.headers, session, emit)
        emit(reply)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(server.server_address[1])
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
