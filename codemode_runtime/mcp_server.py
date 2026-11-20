# SPDX-License-Identifier: MIT
"""The runtime as an MCP server over stdio.

This is how any agent, on any model, uses the runtime: its harness starts
``python main.py serve`` as an MCP server and sees exactly one tool,
``execute``. The tool takes either ``code`` (run a program) or
``executionId`` (fetch the outcome of an earlier one, for example after a
human approved it). There is deliberately no approve tool.

Written against the public MCP spec with the standard library: JSON-RPC 2.0,
one message per line on stdin and stdout. Nothing else may be written to
stdout, so all diagnostics go to stderr.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
from typing import Any, BinaryIO, Dict, Optional

from . import __version__
from .errors import CodemodeError
from .jsonutil import dumps_line
from .runtime import Runtime

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "The Python program to run."},
        "executionId": {
            "type": "string",
            "description": "Instead of code: return the current outcome of this earlier execution.",
        },
        "session": {
            "type": "string",
            "description": "Which state dict the program sees. Defaults to 'default'.",
        },
    },
    "additionalProperties": False,
}


class McpServer:
    def __init__(self, runtime: Runtime, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self._runtime = runtime
        self._stdin = stdin
        self._stdout = stdout
        self._write_lock = threading.Lock()
        self._inbox: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._current_id: Any = None
        self._cancel: Optional[threading.Event] = None
        self._state_lock = threading.Lock()

    # -- transport --------------------------------------------------------

    def _send(self, message: Dict[str, Any]) -> None:
        with self._write_lock:
            self._stdout.write(dumps_line(message))
            self._stdout.flush()

    def _result(self, request_id: Any, result: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _read(self) -> None:
        """Reader thread. It only queues, except for cancellation, which has
        to act while the worker is busy running the request being cancelled."""
        for line in self._stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._error(None, -32700, "parse error: each line must be one JSON-RPC message")
                continue
            if not isinstance(message, dict):
                self._error(None, -32600, "invalid request: batches are not supported")
                continue
            if message.get("method") == "notifications/cancelled":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                with self._state_lock:
                    if self._cancel is not None and params.get("requestId") == self._current_id:
                        self._cancel.set()
                continue
            self._inbox.put(message)
        self._inbox.put(None)

    def serve(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()
        while True:
            message = self._inbox.get()
            if message is None:
                return  # stdin closed: the client is gone
            self._handle(message)

    # -- methods ----------------------------------------------------------

    def _handle(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        is_request = "id" in message
        if not isinstance(method, str):
            return  # a response to something we never asked; ignore
        if not is_request:
            return  # notifications need no answer
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            if method == "initialize":
                wanted = params.get("protocolVersion")
                self._result(request_id, {
                    "protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codemode-runtime", "version": __version__},
                    "instructions": "One tool, execute: write a Python program that calls tools. "
                    "Use search() and describe() inside the program to find them.",
                })
            elif method == "ping":
                self._result(request_id, {})
            elif method == "tools/list":
                self._result(request_id, {"tools": [{
                    "name": "execute",
                    "description": self._runtime.tool_description(),
                    "inputSchema": _INPUT_SCHEMA,
                }]})
            elif method == "tools/call":
                self._call(request_id, params)
            else:
                self._error(request_id, -32601, "method not found: %s" % method)
        except Exception as exc:  # the server must outlive any single request
            print("codemode-runtime: %s failed: %r" % (method, exc), file=sys.stderr)
            self._error(request_id, -32603, "internal error: %s" % exc)

    def _call(self, request_id: Any, params: Dict[str, Any]) -> None:
        if params.get("name") != "execute":
            self._error(request_id, -32602, "unknown tool %r; the only tool is 'execute'" % (params.get("name"),))
            return
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        code, exec_id = args.get("code"), args.get("executionId")
        cancel = threading.Event()
        with self._state_lock:
            self._current_id, self._cancel = request_id, cancel
        try:
            if (code is None) == (exec_id is None):
                payload: Dict[str, Any] = {"status": "error", "error": {
                    "type": "UsageError",
                    "message": "pass exactly one of 'code' and 'executionId'",
                }}
            elif exec_id is not None:
                payload = self._runtime.get(str(exec_id))
            else:
                payload = self._runtime.execute(str(code), str(args.get("session", "default")), cancel_event=cancel)
        except CodemodeError as exc:
            # Usage problems are tool results the model can read and fix, not
            # protocol errors.
            payload = {"status": "error", "error": exc.to_dict()}
        finally:
            with self._state_lock:
                self._current_id, self._cancel = None, None
        if cancel.is_set():
            return  # the spec says a cancelled request gets no response
        self._result(request_id, {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=True)}],
            "structuredContent": payload,
            "isError": payload.get("status") in ("error", "rejected"),
        })


def serve(runtime: Runtime) -> None:
    McpServer(runtime, sys.stdin.buffer, sys.stdout.buffer).serve()
