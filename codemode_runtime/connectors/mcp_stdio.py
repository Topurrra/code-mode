# SPDX-License-Identifier: MIT
"""A connector for any MCP server that speaks the stdio transport.

The server is a host-side process: one JSON-RPC message per line on its
stdin and stdout. Its credentials come from the connector's ``env`` in the
config (``${NAME}`` references resolved here) and never reach the sandbox.

What a real server does, and what this client does about it:

- It may be slow to start (a package runner that downloads first): the
  handshake has its own time limit, ``startup_timeout_s``.
- It may exit: the next call starts it again, and the failed call says the
  server exited and shows its stderr.
- It may print things that are not protocol messages to stdout: they are
  skipped, counted, and named in the error if a call then times out.
- It may ignore the end of its input: it is then stopped together with
  everything it started.
- It may ignore a cancelled call and keep working, so that the next caller
  waits behind work nobody wants. With ``"on_abandon": "restart"`` it gets a
  moment to end the call, and is then stopped with everything it started;
  the next call starts it again.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import PathBase, expand_env
from ..errors import CodemodeError, ConfigError, ToolFailure
from ..jsonutil import dumps_line
from ..procutil import IS_WINDOWS, child_options, contain, kill_tree, release
from . import CallContext, looks_secret
from .mcp_common import MAX_MESSAGE_BYTES, Mailbox, McpConnector, Progress, unwrap_result  # noqa: F401  (unwrap_result: public here since 0.1)

# The server starts with a short allowlist from the host environment plus what
# the config names. Passing the whole environment would hand every secret the
# host has to every server. The list is what a program needs to find its
# own folders and to know the machine it is on: who the user is, where
# programs, settings, caches and temporary files live, the locale and the
# time zone. None of it is a credential.
BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERNAME",
    "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
)
_STDERR_TAIL = 4000
_ABANDON_GRACE_S = 2.0  # with on_abandon "restart": how long a server has to end a call it was told to stop
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def check_inherit_env(value: Any, where: str) -> None:
    """Validate a connector's ``inherit_env`` at config load."""
    if value is not True and value is not False and not (isinstance(value, list) and all(isinstance(v, str) and v.lstrip("!") for v in value)):
        raise ConfigError(
            "%s must be true, false or a list of variable names (globs are allowed)" % where,
            hint='Example: {"inherit_env": ["JAVA_HOME", "MYTOOL_*", "!MYTOOL_DEBUG"]}. A name after ! is left out. '
            "true passes the whole host environment, credentials included.",
        )


def host_env(inherit: Any = None, environ: Optional[Mapping[str, str]] = None) -> "tuple[Dict[str, str], List[str]]":
    """What a child process gets from the host environment: the base list,
    plus what the connector's ``inherit_env`` names. ``True`` is everything;
    a list holds names and globs, and a name or a glob after ``!`` is left
    out whatever else matches it, even one of the base list:
    ``["MYTOOL_*", "!MYTOOL_DEBUG"]``, or ``["*", "!HOME"]``.

    Returns the variables, and the values of the inherited ones whose names
    say they hold a credential (TOKEN, KEY, SECRET, ...): the child was
    handed them, so what it prints back has to be cleaned of them.

    Names are compared the way the platform compares them: without regard
    to case on Windows, exactly elsewhere.
    """
    environ = os.environ if environ is None else environ
    fold = (lambda text: text.upper()) if IS_WINDOWS else (lambda text: text)
    base = {fold(name) for name in BASE_ENV}
    patterns = [fold(pattern) for pattern in inherit] if isinstance(inherit, list) else []
    wanted = [pattern for pattern in patterns if not pattern.startswith("!")]
    left_out = [pattern[1:] for pattern in patterns if pattern.startswith("!")]
    env: Dict[str, str] = {}
    secrets: List[str] = []
    for key, value in environ.items():
        name = fold(key)
        if any(fnmatchcase(name, pattern) for pattern in left_out):
            continue
        if name in base:
            env[key] = value
        elif inherit is True or any(fnmatchcase(name, pattern) for pattern in wanted):
            env[key] = value
            if looks_secret(key):
                secrets.append(value)
    return env, secrets


def resolve_env(name: str, spec: Dict[str, Any], section: str = "env") -> "tuple[Dict[str, str], List[str]]":
    """Expand a connector's ``env`` or ``headers``. Returns the values and
    the ones that are credentials: an entry is one if the config lists it
    under ``secrets``, or if it is a ``${NAME}`` reference and its own name or
    the referenced name says so (TOKEN, KEY, SECRET, AUTH, ...)."""
    values: Dict[str, str] = {}
    secrets: List[str] = []
    listed = set(spec.get("secrets") or [])
    try:
        for key, raw in (spec.get(section) or {}).items():
            values[key] = expand_env(raw, "connectors.%s.%s.%s" % (name, section, key))
            referenced = _REFERENCE.findall(raw)
            if key in listed or (referenced and (looks_secret(key) or any(looks_secret(ref) for ref in referenced))):
                secrets.append(values[key])
                # "Bearer <token>": the token alone must go too.
                secrets.extend(part for part in values[key].split() if len(part) >= 12)
    except CodemodeError as exc:
        raise ToolFailure("ConnectorUnavailable", exc.message, exc.hint)
    return values, secrets


class McpStdioConnector(McpConnector):
    transport = "stdio"

    def __init__(self, name: str, spec: Dict[str, Any], startup_timeout_s: float, base_dir: Optional[Path] = None) -> None:
        super().__init__(name, spec, startup_timeout_s)
        self._base = PathBase.of(base_dir)
        self._proc: Optional[subprocess.Popen] = None
        self._box = Mailbox()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stderr = bytearray()
        self.starts = 0
        self._restart_on_abandon = spec.get("on_abandon") == "restart"
        self._calls: Dict[Any, Any] = {}  # request id -> (slot, box, process) of a call in flight
        self._abandons: List[threading.Event] = []  # each set once a given-up call has ended or its server was stopped

    def _file_stamps(self) -> List[Any]:
        """Every part of the command that is a file here: the server script
        of a project is one of them, and editing it must be noticed."""
        base = Path(self._cwd() or ".")
        stamps: List[Any] = []
        for part in self._command():
            try:
                path = Path(part) if os.path.isabs(part) else base / part
                if path.is_file():
                    info = path.stat()
                    stamps.append([str(part), info.st_size, info.st_mtime_ns])
            except (OSError, ValueError):
                continue
        return stamps

    def _command(self) -> List[str]:
        """The command, with a file part that is not found from where the
        server runs but is next to a config that was named explicitly made
        into that file's full path."""
        parts = [str(part) for part in self._spec.get("command", [])]
        if self._base.config_dir is None:
            return parts
        here = Path(self._cwd() or ".")
        for index, part in enumerate(parts):
            try:
                if os.path.isabs(part) or (here / part).is_file():
                    continue
                other = self._base.config_dir / part
                if other.is_file():
                    parts[index] = str(other)
            except (OSError, ValueError):
                continue
        return parts

    # -- process ----------------------------------------------------------

    def _ensure(self) -> None:
        with self._state_lock:
            # A call that was given up on a moment ago may be about to cost
            # the server its life (on_abandon "restart"). Wait for that to be
            # decided: sent now, this call would queue behind the abandoned
            # work and then fail with the server.
            for done in list(self._abandons):
                done.wait(_ABANDON_GRACE_S + 15)
            proc = self._proc
            # The end of its output is the first sign that a server is gone;
            # the process itself may take a moment longer to be reported as
            # exited. Either one means: start it again.
            if proc is not None and proc.poll() is None and not self._box.closed:
                return
            if proc is not None:
                try:
                    proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                kill_tree(proc)  # if it has exited, this only lets go of it
            self._start()

    def _ensure_after_failure(self) -> None:
        """A write to the server failed, so it is gone whatever ``poll``
        says. Mark the connection dead and connect again."""
        self._box.close()
        self._ensure()

    def _cwd(self) -> Optional[str]:
        """Where the server runs: ``cwd`` from the config, relative to the
        config file's folder, which is also the default. A project's servers
        are written to be started from the project root. With a config that
        was named explicitly, ``cwd`` is read from the current folder, and
        from the config's folder when it is not there."""
        cwd = self._spec.get("cwd")
        if self._base.root is not None:
            return str((self._base.root / cwd) if cwd else self._base.root)
        return str(self._base.resolve(cwd)) if cwd else None

    def _start(self) -> None:
        command = self._command()
        env, inherited = host_env(self._spec.get("inherit_env"))
        extra, named = resolve_env(self.name, self._spec)
        self._secrets = named + inherited
        env.update(extra)
        cwd = self._cwd()
        # shutil.which honours PATHEXT, so "npx" finds npx.cmd on Windows.
        command[0] = shutil.which(command[0], path=env.get("PATH")) or shutil.which(command[0]) or command[0]
        try:
            self._proc = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                **child_options(),
            )
        except OSError as exc:
            raise ToolFailure(
                "ConnectorUnavailable",
                "could not start MCP server %r (%s): %s" % (self.name, " ".join(self._spec["command"]), exc),
                "Check connectors.%s.command and cwd (%s); the command must be on PATH or a path that exists.%s"
                % (self.name, cwd or ".", (" Relative paths: %s." % self._base.looked_in(self._spec.get("cwd"))) if self._base.config_dir is not None else ""),
            )
        contain(self._proc)  # so that stopping it reaches everything it started
        self.starts += 1
        self._stderr = bytearray()
        self._box = Mailbox()
        proc, box = self._proc, self._box
        threading.Thread(target=self._read_stdout, args=(proc, box), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        try:
            self._handshake()
        except ToolFailure as exc:
            note = self._stderr_note() + box.noise_note()
            self.close()
            hint = "Run the command by hand to see what it prints."
            if exc.kind == "ToolTimeout":
                hint = (
                    "The server did not answer the handshake in %gs. If it is slow to start (a package runner that "
                    "downloads first), raise startup_timeout_s on this connector." % self._startup_timeout
                )
            raise ToolFailure(
                "ConnectorUnavailable", "MCP server %r did not complete the handshake: %s%s" % (self.name, exc.message, note), hint
            )

    def _stderr_note(self) -> str:
        tail = bytes(self._stderr[-_STDERR_TAIL:]).decode("utf-8", "replace").strip()
        return ("; server stderr: " + tail) if tail else ""

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for chunk in iter(lambda: proc.stderr.read1(4096), b""):  # type: ignore[union-attr]
            self._stderr.extend(chunk)
            del self._stderr[: max(0, len(self._stderr) - 4 * _STDERR_TAIL)]

    def _read_stdout(self, proc: subprocess.Popen, box: Mailbox) -> None:
        stream = proc.stdout
        assert stream is not None
        while True:
            line = stream.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                box.note_noise(line)  # a server that logs to stdout must not take the client down
                continue
            reply = box.deliver(message)
            if reply is not None:
                try:
                    self._send(reply, proc)
                except ToolFailure:
                    pass
        box.close()  # end of output: the server is gone

    def _send(self, message: Dict[str, Any], proc: Optional[subprocess.Popen] = None) -> None:
        proc = proc or self._proc
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

    # -- the transport ----------------------------------------------------

    def _exchange(self, request: Dict[str, Any], timeout: float, on_progress: Progress) -> Dict[str, Any]:
        method = request["method"]
        for attempt in (1, 2):
            box = self._box
            slot = box.expect(request, on_progress)
            try:
                self._send(request)
                break
            except ToolFailure:
                box.forget(request["id"])
                if attempt == 2 or method == "initialize":
                    raise
                # The request never reached the server: it died between two
                # calls. Nothing ran, so start it again and send once more.
                self._ensure_after_failure()
        self._calls[request["id"]] = (slot, box, self._proc)
        try:
            answered = slot["event"].wait(timeout)
        finally:
            call = self._calls.pop(request["id"], None)
        if not answered:
            if not self._restart_on_abandon:
                box.forget(request["id"])
            try:
                self._notify("notifications/cancelled", {"requestId": request["id"], "reason": "timeout"})
            except ToolFailure:
                pass
            note = self._watch_abandoned(request["id"], call)
            failure = ToolFailure(
                "ToolTimeout", "%s on MCP server %r did not answer within %gs%s%s" % (method, self.name, timeout, box.noise_note(), note)
            )
            failure.note = note.lstrip("; ")  # type: ignore[attr-defined]  # kept by the gateway, which words its own timeout message
            raise failure
        message = slot["message"]
        if message is None:
            raise ToolFailure(
                "ConnectorUnavailable",
                "MCP server %r exited during %s%s%s" % (self.name, method, self._stderr_note(), box.noise_note()),
                "The server process is gone; it is started again on the next call. If the call changed something, "
                "check the target before you repeat it.",
            )
        return message

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def cancel(self, ctx: CallContext) -> None:
        request_id = self._in_flight.get(ctx.seq)
        super().cancel(ctx)
        if request_id is not None:
            self._watch_abandoned(request_id, self._calls.pop(request_id, None))

    def _watch_abandoned(self, request_id: Any, call: Any) -> str:
        """The gateway gave up on a call and the server has been told. With
        on_abandon "restart", give the server a moment to end the call (to
        answer it); if it does not, stop it with everything it started. A
        server that ignores "cancelled" would otherwise keep working, and a
        server that takes one request at a time would make the next caller
        wait behind work nobody wants.

        The watch runs on a thread of its own: the caller has its answer
        already. Returns a note for the caller's error, or "" when there is
        nothing to watch."""
        if not self._restart_on_abandon or call is None:
            return ""
        slot, box, proc = call
        done = threading.Event()
        self._abandons.append(done)

        def watch() -> None:
            try:
                answered = slot["event"].wait(_ABANDON_GRACE_S) and slot["message"] is not None
                box.forget(request_id)
                if not answered and proc is not None and proc.poll() is None:
                    kill_tree(proc)  # the server and everything it started
                    try:
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    # Calls still in flight on it fail as when a server
                    # exits; the next call starts a new one.
                    box.close()
            finally:
                done.set()
                try:
                    self._abandons.remove(done)
                except ValueError:
                    pass

        threading.Thread(target=watch, daemon=True).start()
        return (
            "; on_abandon is restart: the server is stopped, with everything it started, if it has not ended the call "
            "within %gs, and the next call starts a new one" % _ABANDON_GRACE_S
        )

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the polite way to stop a stdio server
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            # It ignored the end of its input. Take everything it started
            # with it: a wrapper script would otherwise leave the real
            # server running.
            kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        release(proc)  # its job handle; what a server that exited politely left running stays
        if proc.poll() is not None:
            # Only once it has exited: the reader threads are then at the end
            # of their streams, and closing cannot wait on a read in progress.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except (OSError, ValueError):
                    pass
