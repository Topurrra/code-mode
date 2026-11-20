# SPDX-License-Identifier: MIT
"""The sandbox side: the API a model's program sees.

This file is not imported by the host. Its source is sent to the sandbox
process as the first frame and executed there, so it must stand alone: the
standard library only, and syntax that old interpreters accept, because the
sandbox may be a different Python than the host (a container image, say).

Protocol: newline-delimited JSON. The program's requests (``call``,
``search``, ``describe``, ``step``, ``step_set``, ``snippet``) each get one
reply with the same ``id``. ``log`` frames carry printed output and get no
reply. One ``done`` frame ends the run.

The program never sees credentials, connectors or policy. Every capability it
has is a request to the host, which decides.
"""

import ast
import builtins
import io
import json
import linecache
import os
import sys
import threading
import traceback

_RUNNER_FILE = "codemode-runner"
_REPR_CAP = 1000000
_LOG_FLUSH_AT = 8192
_LOG_FRAME = 32768


class ToolError(Exception):
    """Raised in the program when a call is denied, fails, or is unknown.

    ``kind`` is a short machine-readable reason (``PolicyDenied``,
    ``UnknownTool``, ``ToolTimeout``, ...); ``message`` is for reading.
    """

    def __init__(self, kind, message):
        Exception.__init__(self, "%s: %s" % (kind, message))
        self.kind = kind
        self.message = message


class _Channel(object):
    """Frames to and from the host, on descriptors the program cannot reach
    by accident.

    The protocol arrives on fd 0 and leaves on fd 1. Before any program code
    runs, both are moved to private descriptors, fd 0 is pointed at the null
    device and fd 1 at stderr. After that a ``print``, an ``input()``, a
    subprocess or a C extension writing to stdout cannot corrupt a frame or
    swallow one.
    """

    def __init__(self):
        fd_in = os.dup(0)
        fd_out = os.dup(1)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        os.dup2(2, 1)
        self._in = os.fdopen(fd_in, "rb")
        self._out = os.fdopen(fd_out, "wb", 0)
        self._lock = threading.RLock()
        self._next = 0
        self.in_step = False

    def send(self, frame):
        data = (json.dumps(frame, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
        view = memoryview(data)
        with self._lock:
            while len(view):
                written = self._out.write(view)
                view = view[written:]

    def recv(self):
        line = self._in.readline()
        if not line:
            os._exit(70)  # the host is gone; there is nobody to report to
        return json.loads(line.decode("ascii"))

    def request(self, frame):
        if self.in_step and frame.get("op") != "step_set":
            # On replay the step returns its recorded value and fn never runs,
            # so a request made inside fn would be missing from the sequence.
            raise RuntimeError("no tool calls, search or step inside step(); do them outside and pass the values in")
        with self._lock:
            self._next += 1
            frame["id"] = self._next
            self.send(frame)
            reply = self.recv()
        if reply.get("id") != frame["id"]:
            raise RuntimeError("protocol error: reply does not match request")
        if not reply.get("ok"):
            error = reply.get("error") or {}
            raise ToolError(error.get("type", "ToolError"), error.get("message", ""))
        return reply


class _LogStream(io.TextIOBase):
    """``sys.stdout`` and ``sys.stderr`` for the program: text becomes
    ``log`` frames. Buffered by line so a loop of prints is not a frame per
    character."""

    def __init__(self, channel, name):
        io.TextIOBase.__init__(self)
        self._channel = channel
        self._name = name
        self._parts = []
        self._size = 0

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() argument must be str, not %s" % type(text).__name__)
        self._parts.append(text)
        self._size += len(text)
        if "\n" in text or self._size >= _LOG_FLUSH_AT:
            self.flush()
        return len(text)

    def flush(self):
        if not self._parts:
            return
        text = "".join(self._parts)
        self._parts = []
        self._size = 0
        for start in range(0, len(text), _LOG_FRAME):
            self._channel.send({"op": "log", "stream": self._name, "text": text[start:start + _LOG_FRAME]})


_channel = None


def _jsonable(value, what):
    """Round-trip through JSON so the program holds exactly what the host
    recorded. A replayed pass then sees the same types as the first one."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TypeError("%s must be JSON-serializable: %s" % (what, exc))


def parallel(calls, return_errors=False):
    """Run independent tool calls at the same time.

    ``calls`` is a list of ``("namespace.tool", {arguments})`` pairs. Results
    come back in the same order. With ``return_errors=True`` a failed call
    yields its ``ToolError`` in the list instead of raising.
    """
    batch = []
    for entry in calls:
        if isinstance(entry, str):
            path, args = entry, {}
        else:
            entry = tuple(entry)
            if len(entry) == 1:
                path, args = entry[0], {}
            elif len(entry) == 2:
                path, args = entry
            else:
                raise TypeError("parallel() takes (path, arguments) pairs")
        if not isinstance(path, str):
            raise TypeError("a tool path must be a string like 'namespace.tool'")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise TypeError("arguments of %s must be a dict" % path)
        batch.append({"tool": path, "args": _jsonable(args, "arguments of %s" % path)})
    if not batch:
        return []
    reply = _channel.request({"op": "call", "calls": batch})
    out = []
    for item in reply["results"]:
        if item.get("ok"):
            out.append(item.get("value"))
            continue
        error = item.get("error") or {}
        failure = ToolError(error.get("type", "ToolError"), error.get("message", ""))
        if not return_errors:
            raise failure
        out.append(failure)
    return out


def call(path, args=None, **kwargs):
    """Call one tool by path: ``call("demo.read_note", {"id": 1})``."""
    merged = dict(args or {})
    merged.update(kwargs)
    return parallel([(path, merged)])[0]


class _Tool(object):
    __slots__ = ("_path",)

    def __init__(self, path):
        self._path = path

    def __call__(self, *args, **kwargs):
        if args:
            if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                return call(self._path, args[0])
            raise TypeError(
                "%s takes keyword arguments; see describe(%r)" % (self._path, self._path)
            )
        return call(self._path, kwargs)

    def __repr__(self):
        return "<tool %s>" % self._path


class _Namespace(object):
    """``tools.demo``. Any attribute is a tool; whether it exists is the
    host's call, so that an unknown name is recorded like any other outcome
    and a replayed pass behaves the same."""

    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __getattr__(self, tool):
        if tool.startswith("_"):
            raise AttributeError(tool)
        return _Tool("%s.%s" % (self._name, tool))

    def __repr__(self):
        return "<namespace %s; use describe(%r)>" % (self._name, self._name)


class _Tools(object):
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Namespace(name)

    def __repr__(self):
        return "<tools; use search('words') to find one>"


def search(query="", limit=10):
    """Find tools and snippets. Returns a short list of signatures."""
    return _channel.request({"op": "search", "query": str(query), "limit": int(limit)})["value"]


def describe(target):
    """Full detail for ``"namespace.tool"``, a namespace, or ``"snippet:name"``."""
    return _channel.request({"op": "describe", "target": str(target)})["value"]


def step(name, fn):
    """Record the value of ``fn()`` under ``name``.

    Use it for anything that would differ if the program ran again: the time,
    a random choice, a file listing. When an execution resumes after a pause
    the program runs again from the top, and ``step`` returns the recorded
    value instead of calling ``fn``.
    """
    if not callable(fn):
        raise TypeError("step(name, fn): fn must be callable")
    reply = _channel.request({"op": "step", "name": str(name)})
    if reply.get("hit"):
        return reply.get("value")
    _channel.in_step = True
    try:
        value = _jsonable(fn(), "the value of step %r" % (name,))
    except Exception as exc:
        # Recorded as a failure so that a replayed pass fails the same way.
        message = "%s: %s" % (type(exc).__name__, exc)
        _channel.request({"op": "step_set", "seq": reply["seq"], "error": {"type": "StepError", "message": message}})
        raise ToolError("StepError", message)
    finally:
        _channel.in_step = False
    _channel.request({"op": "step_set", "seq": reply["seq"], "value": value})
    return value


def _exec_block(code, namespace, filename):
    """Run a block; the value of a trailing expression is the result."""
    linecache.cache[filename] = (len(code), None, code.splitlines(True), filename)
    tree = ast.parse(code, filename, "exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        exec(compile(tree, filename, "exec"), namespace)
        return eval(compile(ast.Expression(last.value), filename, "eval"), namespace)
    exec(compile(tree, filename, "exec"), namespace)
    return None


_api = {}


def run(name, **args):
    """Run a saved snippet. The snippet reads its arguments from ``args``.

    ``args`` is also defined, empty, in an ordinary program, so a program
    written as ``base = args.get("base", 0)`` runs unchanged before and after
    it is promoted to a snippet.
    """
    reply = _channel.request({"op": "snippet", "name": str(name)})
    namespace = dict(_api)
    namespace["args"] = _jsonable(args, "snippet arguments")
    return _exec_block(reply["value"]["code"], namespace, "<snippet:%s>" % name)


def _result_default(value):
    if isinstance(value, (set, frozenset)):
        try:
            return sorted(value)
        except TypeError:
            return list(value)
    raise TypeError("not JSON-serializable")


def _encode_result(value):
    """(json value, was_repr). A value JSON cannot hold comes back as its
    repr, flagged, rather than failing a program that already did its work."""
    try:
        return json.loads(json.dumps(value, default=_result_default, allow_nan=False)), False
    except (TypeError, ValueError):
        text = repr(value)
        if len(text) > _REPR_CAP:
            text = text[:_REPR_CAP] + "... [repr cut at %d chars]" % _REPR_CAP
        return text, True


def _encode_state(state):
    if not isinstance(state, dict):
        raise TypeError("state must be a dict, not %s" % type(state).__name__)
    out = {}
    for key, value in state.items():
        if not isinstance(key, str):
            raise TypeError("state keys must be strings, got %r" % (key,))
        out[key] = _jsonable(value, "state[%r]" % key)
    return out


_HINTS = (
    ("'return' outside function", "End the program with an expression; its value is the result."),
    ("'await' outside", "Calls are synchronous here: remove 'await'. Use parallel([...]) to run calls at the same time."),
)


def _describe_error(exc):
    kind = type(exc).__name__
    message = str(exc)
    hint = ""
    if isinstance(exc, SyntaxError):
        message = "%s (line %s)" % (exc.msg, exc.lineno)
        for needle, text in _HINTS:
            if needle in (exc.msg or ""):
                hint = text
    elif isinstance(exc, SystemExit):
        hint = "Do not call exit(); end the program with an expression."
    elif isinstance(exc, ToolError):
        kind = exc.kind
        message = exc.message
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename != _RUNNER_FILE]
    if isinstance(exc, SyntaxError) and str(exc.filename or "").startswith("<"):
        # The program itself did not parse. The frames are the parser's, not
        # the program's; the exception line already shows the place.
        frames = []
    lines = ["Traceback (most recent call last):\n"] if frames else []
    lines += traceback.format_list(frames) + traceback.format_exception_only(type(exc), exc)
    error = {"type": kind, "message": message, "traceback": "".join(lines)[-6000:]}
    if hint:
        error["hint"] = hint
    return error


def _main():
    global _channel
    channel = _Channel()
    _channel = channel
    channel.send({"op": "ready", "python": "%d.%d.%d" % sys.version_info[:3]})
    job = channel.recv()
    out = _LogStream(channel, "stdout")
    err = _LogStream(channel, "stderr")
    sys.stdout, sys.stderr = out, err
    sys.stdin = open(os.devnull)
    namespace = {
        "__name__": "__codemode__",
        "__builtins__": builtins,
        "tools": _Tools(),
        "call": call,
        "parallel": parallel,
        "search": search,
        "describe": describe,
        "step": step,
        "run": run,
        "state": job.get("state") or {},
        "args": {},
        "ToolError": ToolError,
    }
    _api.update(namespace)
    try:
        value = _exec_block(job["code"], namespace, "<code>")
        result, was_repr = _encode_result(value)
        frame = {
            "op": "done",
            "ok": True,
            "result": result,
            "result_repr": was_repr,
            "state": _encode_state(namespace.get("state")),
        }
    except BaseException as exc:  # SystemExit and KeyboardInterrupt are outcomes too
        frame = {"op": "done", "ok": False, "error": _describe_error(exc)}
    try:
        out.flush()
        err.flush()
    except Exception:
        pass
    channel.send(frame)
    os._exit(0)  # do not wait for threads the program may have left running


if __name__ == "__main__":
    _main()
