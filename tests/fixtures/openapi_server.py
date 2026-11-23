# SPDX-License-Identifier: MIT
"""A small HTTP API with an OpenAPI document, for the end-to-end suite.
Standard library only. It is not used by the application.

  GET /openapi.json     the description of the API (no credential needed)
  GET /elsewhere.json   the same description, naming a server on another host
  GET /state            what the server has seen (for assertions)
  /v1/...               the API

Usage: python openapi_server.py [TOKEN]

Prints the port it listens on as the first line of stdout. With TOKEN, every
request under /v1 must carry ``Authorization: Bearer TOKEN`` or the query
value ``api_key=TOKEN``.
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = sys.argv[1] if len(sys.argv) > 1 else ""
LOCK = threading.Lock()
NOTES = {1: {"id": 1, "title": "first", "tags": ["a"]}, 2: {"id": 2, "title": "second", "tags": []}, 3: {"id": 3, "title": "third", "tags": ["a", "b"]}}
SEEN = {"requests": {}, "document_requests_with_credentials": 0, "document_requests": 0, "landed": 0, "created": []}


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


ECHO = {"type": "object", "properties": {"path": {"type": "string"}, "query": {"type": "string"}, "trace": {"type": "string"}}}

DOCUMENT = {
    "openapi": "3.0.3",
    "info": {"title": "Notes", "version": "1"},
    "servers": [{"url": "/{version}", "variables": {"version": {"default": "v1"}}}],
    "paths": {
        "/": {"get": {"summary": "The root of the API.", "responses": {"200": {"description": "ok"}}}},
        "/notes": {
            "get": {
                "operationId": "listNotes", "summary": "List notes.",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 10}},
                               {"name": "tag", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}}],
                "responses": {"200": dict(_json({"type": "array", "items": {"$ref": "#/components/schemas/Note"}}), description="ok")},
            },
            "post": {
                "operationId": "createNote", "summary": "Create a note.",
                "requestBody": {"$ref": "#/components/requestBodies/NewNote"},
                "responses": {"201": dict(_json({"$ref": "#/components/schemas/Note"}), description="created")},
            },
        },
        "/notes/{id}": {
            # Declared once for the path: both operations below take it.
            "parameters": [{"$ref": "#/components/parameters/NoteId"}],
            "get": {"operationId": "readNote", "summary": "Read one note.",
                    "responses": {"200": dict(_json({"$ref": "#/components/schemas/Note"}), description="ok")}},
            "delete": {"summary": "Delete one note.", "responses": {"204": {"description": "gone"}}},
        },
        "/echo/{value}": {"get": {
            "summary": "Say what arrived: the path and the query as they were sent, and one header.",
            "parameters": [
                {"name": "value", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "q", "in": "query", "schema": {"type": "string"}},
                {"name": "tags", "in": "query", "explode": False, "schema": {"type": "array", "items": {"type": "string"}}},
                {"name": "flag", "in": "query", "schema": {"type": "boolean"}},
                {"name": "filter", "in": "query", "style": "deepObject", "schema": {"type": "object"}},
                {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                # Two that a careless document declares and a caller must never get to set.
                {"name": "Authorization", "in": "header", "schema": {"type": "string"}},
                {"name": "api_key", "in": "query", "schema": {"type": "string"}},
            ],
            "responses": {"200": dict(_json(ECHO), description="ok")},
        }},
        "/form": {"post": {"summary": "Take a form.",
                           "requestBody": {"required": True, "content": {"application/x-www-form-urlencoded": {"schema": {"type": "object"}}}},
                           "responses": {"200": {"description": "ok"}}}},
        "/text": {
            "get": {"summary": "Answer with plain text.", "responses": {"200": {"description": "ok"}}},
            "post": {"summary": "Take plain text.", "requestBody": {"content": {"text/plain": {"schema": {"type": "string"}}}},
                     "responses": {"200": {"description": "ok"}}},
        },
        "/upload": {"post": {"summary": "Take a file.", "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                             "responses": {"200": {"description": "ok"}}}},
        "/tree": {"post": {"summary": "Take a tree.", "requestBody": _json({"$ref": "#/components/schemas/Tree"}),
                           "responses": {"200": {"description": "ok"}}}},
        "/redirect": {"get": {"summary": "Answer with a redirect.", "responses": {"302": {"description": "elsewhere"}}}},
        "/landing": {"get": {"summary": "Where the redirect points.", "responses": {"200": {"description": "ok"}}}},
        "/slow": {"get": {"summary": "Wait before answering.", "parameters": [{"name": "seconds", "in": "query", "schema": {"type": "number"}}],
                          "responses": {"200": {"description": "ok"}}}},
        "/bytes": {"get": {"summary": "Answer with bytes that are not text.", "responses": {"200": {"description": "ok"}}}},
        "/nothing": {"get": {"summary": "Answer with no body.", "responses": {"204": {"description": "nothing"}}}},
        "/whoami": {"get": {"summary": "Say which credentials arrived.", "responses": {"200": {"description": "ok"}}}},
        "/fail": {
            "get": {"summary": "Fail with a long body.", "responses": {"500": {"description": "broken"}}},
            "post": {"summary": "Fail after a write.", "responses": {"500": {"description": "broken"}}},
        },
        "/a-b": {"get": {"summary": "One of two routes whose names come out the same.", "responses": {"200": {"description": "ok"}}}},
        "/a_b": {"get": {"summary": "The other one.", "responses": {"200": {"description": "ok"}}}},
        # Not a path: joined to the address of the API it would name another host.
        "@127.0.0.1:9/stolen": {"get": {"summary": "Not an operation.", "responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "parameters": {"NoteId": {"name": "id", "in": "path", "required": True, "description": "The number of the note.", "schema": {"type": "integer"}}},
        "requestBodies": {"NewNote": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/NewNote"}}}}},
        "schemas": {
            "Note": {"type": "object", "properties": {"id": {"type": "integer"}, "title": {"type": "string"},
                                                      "tags": {"type": "array", "items": {"type": "string"}}}},
            "NewNote": {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}},
            "Tree": {"type": "object", "properties": {"name": {"type": "string"}, "children": {"type": "array", "items": {"$ref": "#/components/schemas/Tree"}}}},
        },
    },
}


def depth_of(tree):
    return 1 + max([depth_of(child) for child in tree.get("children") or []] or [0])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body=b"", content_type="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        query = parse_qs(url.query, keep_blank_values=True)
        if url.path in ("/openapi.json", "/elsewhere.json"):
            with LOCK:
                SEEN["document_requests"] += 1
                SEEN["document_requests_with_credentials"] += 1 if self.headers.get("Authorization") else 0
            document = DOCUMENT if url.path == "/openapi.json" else dict(DOCUMENT, servers=[{"url": "http://127.0.0.1:9/v1"}])
            return self._reply(200, document)
        if url.path == "/state":
            with LOCK:
                return self._reply(200, SEEN)
        if not url.path.startswith("/v1"):
            return self._reply(404, {"error": "not found"})
        route = "%s %s" % (method, url.path[3:] or "/")
        with LOCK:
            SEEN["requests"][route] = SEEN["requests"].get(route, 0) + 1
        by_header = self.headers.get("Authorization") == "Bearer " + TOKEN
        by_query = query.get("api_key") == [TOKEN]
        if TOKEN and not (by_header or by_query):
            return self._reply(401, {"error": "missing or wrong credentials"})
        path = url.path[3:] or "/"
        if route == "GET /":
            return self._reply(200, {"route": "root"})
        if route == "GET /notes":
            limit = int((query.get("limit") or ["10"])[0])
            tags = query.get("tag") or []
            with LOCK:
                notes = [note for note in NOTES.values() if all(tag in note["tags"] for tag in tags)]
            return self._reply(200, notes[:limit])
        if route == "POST /notes":
            new = json.loads(body or b"{}")
            with LOCK:
                note = {"id": max(NOTES) + 1, "title": new.get("title"), "tags": new.get("tags") or []}
                NOTES[note["id"]] = note
                SEEN["created"].append(note["id"])
            return self._reply(201, note)
        if path.startswith("/notes/"):
            key = path[len("/notes/"):]
            with LOCK:
                note = NOTES.get(int(key)) if key.isdigit() else None
                if note is not None and method == "DELETE":
                    del NOTES[int(key)]
            if note is None:
                return self._reply(404, {"error": "no note %s" % key})
            return self._reply(204) if method == "DELETE" else self._reply(200, note)
        if path.startswith("/echo/"):
            # As they were sent, before any decoding: the suite looks at the encoding.
            return self._reply(200, {"path": url.path, "query": url.query, "trace": self.headers.get("X-Trace"),
                                     "agent": self.headers.get("User-Agent"), "accept": self.headers.get("Accept")})
        if route == "POST /form":
            return self._reply(200, {"type": self.headers.get("Content-Type"), "fields": parse_qs(body.decode(), keep_blank_values=True)})
        if route == "POST /text":
            return self._reply(200, {"type": self.headers.get("Content-Type"), "text": body.decode()})
        if route == "GET /text":
            return self._reply(200, b"plain words\n", "text/plain; charset=utf-8")
        if route == "POST /tree":
            return self._reply(200, {"depth": depth_of(json.loads(body))})
        if route == "GET /redirect":
            return self._reply(302, b"", extra={"Location": "/v1/landing"})
        if route == "GET /landing":
            with LOCK:
                SEEN["landed"] += 1
            return self._reply(200, {"landed": True})
        if route == "GET /slow":
            time.sleep(float((query.get("seconds") or ["0"])[0]))
            return self._reply(200, {"slept": True})
        if route == "GET /bytes":
            return self._reply(200, b"\x00\xff\x10\x80", "application/octet-stream")
        if route == "GET /nothing":
            return self._reply(204)
        if route == "GET /whoami":
            return self._reply(200, {"authorization": self.headers.get("Authorization"), "query": url.query})
        if path == "/fail":
            return self._reply(500, ("it broke: " + "x" * 600).encode(), "text/plain")
        if route in ("GET /a-b", "GET /a_b"):
            return self._reply(200, {"route": path})
        return self._reply(404, {"error": "no route %s" % route})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(server.server_address[1])
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
