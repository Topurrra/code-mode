# SPDX-License-Identifier: MIT
"""A connector for any MCP server that speaks the stdio transport.

MCP is the open way to hand tools to an agent, so this is the main route for
real capabilities. It is a small client written against the public spec with
the standard library: JSON-RPC 2.0, one message per line, ``initialize`` then
``tools/list`` and ``tools/call``. Only the tools part of the protocol is
used.

The server is a host-side process. Its credentials come from the connector's
``env`` in the config (``${NAME}`` references resolved here) and never reach
the sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any, Dict, List, Optional

from .. import __version__
from ..config import expand_env
from ..errors import CodemodeError, ToolFailure
from ..jsonutil import dumps_line
from . import CallContext, Connector

PROTOCOL_VERSION = "2025-06-18"

# The server starts with a short allowlist from the host environment plus what
# the config names. Passing the whole environment would hand every secret the
# host has to every server.
_BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMROOT",
    "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME",
)
_STDERR_TAIL = 4000


def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite number %s" % name)


def unwrap_result(result: Any) -> Any:
    """Turn a ``tools/call`` result into a plain value for the program.

    Structured content wins. A single text block that is JSON is parsed,
    because many servers return JSON as text and the program wants the value,
    not a string to parse again. A result marked ``isError`` becomes a
    failure the program can catch.
    """
    if not isinstance(result, dict):
        return result
    content = result.get("content") if isinstance(result.get("content"), list) else []
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    if result.get("isError"):
        raise ToolFailure("ToolError", "\n".join(texts) or "the tool reported an error")
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    others = [
        {"type": b.get("type"), "mimeType": b.get("mimeType"), "chars": len(str(b.get("data", "")))}
        for b in content
        if isinstance(b, dict) and b.get("type") != "text"
    ]
    if others:
        # Binary content is described, not inlined: the program gets what it
        # can use, and megabytes of base64 stay out of the log.
        return {"text": texts, "other": others}
    if len(texts) == 1:
        try:
            return json.loads(texts[0], parse_constant=_reject_constant)
        except ValueError:
            return texts[0]
    return texts if texts else None


class McpStdioConnector(Connector):
    def __init__(self, name: str, spec: Dict[str, Any], startup_timeout_s: float) -> None:
        self.name = name
        self._spec = spec
        self._startup_timeout = float(startup_timeout_s)
        self._proc: Optional[subprocess.Popen] = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._waiting: Dict[int, Dict[str, Any]] = {}
        self._stderr = bytearray()
        self._tools: Optional[List[Dict[str, Any]]] = None

    # -- process ----------------------------------------------------------

    def _ensure(self) -> None:
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._start()

    def _start(self) -> None:
        command = list(self._spec["command"])
        env = {k: os.environ[k] for k in _BASE_ENV if k in os.environ}
        try:
            for key, value in self._spec.get("env", {}).items():
                env[key] = expand_env(value, "connectors.%s.env.%s" % (self.name, key))
        except CodemodeError as exc:
            raise ToolFailure("ConnectorUnavailable", exc.message)
        # shutil.which honours PATHEXT, so "npx" finds npx.cmd on Windows.
        command[0] = shutil.which(command[0]) or command[0]
        try:
            self._proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self._spec.get("cwd"),
            )
        except OSError as exc:
            raise ToolFailure(
                "ConnectorUnavailable",
                "could not start MCP server %r (%s): %s" % (self.name, " ".join(self._spec["command"]), exc),
            )
        self._stderr = bytearray()
        proc = self._proc
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "codemode-runtime", "version": __version__},
                },
                self._startup_timeout,
            )
        except ToolFailure as exc:
            self.close()
            raise ToolFailure(
                "ConnectorUnavailable",
                "MCP server %r did not complete the handshake: %s%s" % (self.name, exc.message, self._stderr_note()),
            )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _stderr_note(self) -> str:
        tail = bytes(self._stderr[-_STDERR_TAIL:]).decode("utf-8", "replace").strip()
        return ("; server stderr: " + tail) if tail else ""

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for chunk in iter(lambda: proc.stderr.read1(4096), b""):  # type: ignore[union-attr]
            self._stderr.extend(chunk)
            del self._stderr[: max(0, len(self._stderr) - 4 * _STDERR_TAIL)]

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                message = json.loads(line)
            except ValueError:
                continue  # a server that logs to stdout must not take the client down
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                # The server is asking the client something. Only ping is
                # supported; saying so beats leaving it waiting.
                if message["method"] == "ping":
                    self._send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
                else:
                    self._send({
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported by this client"},
                    })
            elif "id" in message:
                slot = self._waiting.pop(message["id"], None)
                if slot is not None:
                    slot["message"] = message
                    slot["event"].set()
        # EOF: the server is gone. Wake everyone who is waiting on it.
        for slot in list(self._waiting.values()):
            slot["event"].set()
        self._waiting.clear()

    def _send(self, message: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise ToolFailure("ConnectorUnavailable", "MCP server %r is not running" % self.name)
        try:
            with self._write_lock:
                proc.stdin.write(dumps_line(message))
                proc.stdin.flush()
        except (OSError, ValueError):
            raise ToolFailure(
                "ConnectorUnavailable", "MCP server %r closed its input%s" % (self.name, self._stderr_note())
            )

    def _request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
        slot: Dict[str, Any] = {"event": threading.Event(), "message": None}
        self._waiting[request_id] = slot
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self._waiting.pop(request_id, None)
            try:
                self._send({
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": "timeout"},
                })
            except ToolFailure:
                pass
            raise ToolFailure("ToolTimeout", "%s on MCP server %r did not answer within %gs" % (method, self.name, timeout))
        message = slot["message"]
        if message is None:
            raise ToolFailure(
                "ConnectorUnavailable", "MCP server %r exited during %s%s" % (self.name, method, self._stderr_note())
            )
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise ToolFailure("ToolError", "%s (code %s)" % (error.get("message", "error"), error.get("code")))
        return message.get("result")

    # -- connector API ----------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        self._ensure()
        if self._tools is None:
            tools: List[Dict[str, Any]] = []
            cursor = None
            for _ in range(100):  # a server that pages forever must not hang the catalog
                params = {"cursor": cursor} if cursor else {}
                result = self._request("tools/list", params, self._startup_timeout) or {}
                tools.extend(t for t in result.get("tools", []) if isinstance(t, dict) and isinstance(t.get("name"), str))
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self._tools = tools
        return self._tools

    def call(self, tool: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        self._ensure()
        return unwrap_result(self._request("tools/call", {"name": tool, "arguments": args}, ctx.timeout_s))

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the polite way to stop a stdio server
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
