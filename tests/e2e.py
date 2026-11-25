#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""End-to-end suite: the real CLI, as a subprocess, and nothing mocked.

Run it from the project root:

    python tests/e2e.py
    python tests/e2e.py --update-trace

A run with no scenario names writes the trace to ``.e2e/e2e-trace.txt``: every
command it ran, the exit code and the output. With ``--update-trace`` it also
writes the tracked copy, ``docs/e2e-trace.txt``. A run with scenario names
writes no trace. The commands in the trace are relative to the project root
and use the ``last`` alias instead of generated ids, so they can be pasted and
run again in order. Working files go to ``.e2e/``, which is wiped at the start.

Every check quotes the invariant or failure mode it covers, from
``tests/FAILURE_MODES.md``. The suite exits 0 only if every check holds.

Optional: set CODEMODE_E2E_MCP_PYTHON to an interpreter that has the official
``mcp`` package to also run the interop scenarios against that SDK.
"""

from __future__ import annotations

import json
import os
import queue
import random
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".e2e"
TRACE = ROOT / "docs" / "e2e-trace.txt"
WORK_TRACE = WORK / "e2e-trace.txt"
PY = sys.executable
TRACE_OUT_CHARS = 1400


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err
        try:
            self.json: Any = json.loads(out) if out.strip() else None
        except ValueError:
            self.json = None

    def get(self, *path: Any, default: Any = None) -> Any:
        node = self.json
        for key in path:
            try:
                node = node[key]
            except (KeyError, IndexError, TypeError):
                return default
        return node


class Suite:
    def __init__(self) -> None:
        self.checks: List[Dict[str, Any]] = []
        self.lines: List[str] = []
        self.skipped: List[str] = []
        self.masks: Dict[str, str] = {}
        self.mask(PY, "<python>")
        self.mask(str(ROOT), "<project>")

    # -- trace ------------------------------------------------------------

    def mask(self, path: str, label: str) -> None:
        """Keep machine-specific paths out of the trace, in every spelling a
        command, a JSON string or a hint may give them."""
        for spelling in (path, path.replace("\\", "/")):
            for form in (spelling, json.dumps(spelling)[1:-1], json.dumps(json.dumps(spelling)[1:-1])[1:-1]):
                self.masks[form] = label

    def log(self, text: str = "") -> None:
        for path in sorted(self.masks, key=len, reverse=True):
            text = text.replace(path, self.masks[path])
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.log()
        self.log("=" * 72)
        self.log("## " + title)
        self.log("=" * 72)
        print("\n## " + title)

    def check(self, ids: str, name: str, ok: Any, detail: str = "") -> None:
        ok = bool(ok)
        self.checks.append({"ids": ids, "name": name, "ok": ok})
        line = "  [%s] %s: %s%s" % ("ok" if ok else "FAIL", ids, name, ("  -- " + detail) if (detail and not ok) else "")
        self.log(line)
        print(line)

    def skip(self, what: str) -> None:
        self.skipped.append(what)
        self.log("  [skipped] " + what)
        print("  [skipped] " + what)

    # -- files ------------------------------------------------------------

    def program(self, name: str, source: str) -> str:
        path = WORK / "programs" / (name + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        rel = ".e2e/programs/%s.py" % name
        self.log()
        self.log("--- %s" % rel)
        self.log(source.rstrip())
        self.log("---")
        return rel

    def config(self, name: str, value: Any, raw: Optional[str] = None) -> str:
        text = raw if raw is not None else json.dumps(value, indent=1)
        (WORK / (name + ".json")).write_text(text, encoding="utf-8")
        self.log()
        self.log("--- .e2e/%s.json" % name)
        self.log(text.rstrip())
        self.log("---")
        return name

    # -- running the real thing ------------------------------------------

    def env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEMODE_", "OPENAI_"))}
        env.update(extra or {})
        return env

    def cli(
        self,
        *args: str,
        config: Optional[str] = None,
        home: Optional[str] = "home",
        stdin: str = "",
        env: Optional[Dict[str, str]] = None,
        timeout: float = 90,
        quiet: bool = False,
        stdin_is: Optional[Tuple[str, int]] = None,
    ) -> Result:
        """``stdin`` is text sent down a pipe. ``stdin_is`` replaces the pipe
        with something else, as (what to call it in the trace, a descriptor
        or ``subprocess.DEVNULL``)."""
        argv = ["main.py"]
        if home:
            argv += ["--home", ".e2e/" + home]
        if config:
            argv += ["--config", ".e2e/%s.json" % config]
        argv += list(args)
        started = time.monotonic()
        feed: Dict[str, Any] = {"stdin": stdin_is[1]} if stdin_is else {"input": stdin}
        proc = subprocess.run(
            [PY] + argv, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=self.env(env), **feed,
        )
        result = Result(proc.returncode, proc.stdout, proc.stderr)
        result.seconds = time.monotonic() - started  # type: ignore[attr-defined]
        if not quiet:
            shown_env = "".join("%s=%s " % (k, v) for k, v in (env or {}).items())
            self.log()
            self.log("$ %spython %s" % (shown_env, " ".join(_quote(a) for a in argv)))
            if stdin_is:
                self.log("[stdin is %s]" % stdin_is[0])
            elif stdin:
                self.log("[stdin] " + stdin.strip())
            self.log("[exit %d]" % result.code)
            if result.out.strip():
                self.log(_clip(result.out))
            if result.err.strip():
                self.log("[stderr] " + _clip(result.err, 600))
        return result

    def raw(
        self,
        argv: List[str],
        cwd: Optional[Path] = None,
        stdin: str = "",
        env: Optional[Dict[str, str]] = None,
        timeout: float = 90,
    ) -> Result:
        """Run any command line as written, for the cases where the way the
        program is started is the thing under test."""
        started = time.monotonic()
        proc = subprocess.run(
            argv, cwd=str(cwd or ROOT), input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=self.env(env),
        )
        result = Result(proc.returncode, proc.stdout, proc.stderr)
        result.seconds = time.monotonic() - started  # type: ignore[attr-defined]
        self.log()
        self.log("$ %s%s" % (" ".join(_quote(a) for a in argv), ("    (in %s)" % cwd) if cwd else ""))
        self.log("[exit %d]" % result.code)
        if result.out.strip():
            self.log(_clip(result.out))
        if result.err.strip():
            self.log("[stderr] " + _clip(result.err, 600))
        return result


def _quote(arg: str) -> str:
    if arg and all(c.isalnum() or c in "-_./=:" for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _clip(text: str, limit: int = TRACE_OUT_CHARS) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... %d more chars not shown in the trace ...]" % (len(text) - limit)


def poll(probe: Any, seconds: float = 8.0, every: float = 0.25) -> Any:
    """Call ``probe`` until it returns something true or the time is up, and
    return what it returned last. For checks about a program that is still
    running: how soon it gets somewhere depends on how busy the machine is,
    and a fixed sleep would make the check a bet."""
    deadline = time.monotonic() + seconds
    while True:
        value = probe()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(every)


def counter(home: str, name: str) -> int:
    path = WORK / home / "chaos" / ("counter-%s.txt" % name)
    return int(path.read_text()) if path.is_file() else 0


LIMITS = {
    "timeout_s": 6, "call_timeout_s": 2, "startup_timeout_s": 5, "max_calls": 20,
    "max_value_bytes": 20000, "max_result_chars": 2000, "max_log_chars": 1000,
}


def chaos_config(**changes: Any) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "connectors": {"demo": {"type": "demo"}, "chaos": {"type": "chaos"}, "gated": {"type": "chaos"}},
        "policy": {
            "default": "ask",
            "allow": ["demo.list_notes", "demo.read_note", "demo.search_notes", "chaos.*"],
            "deny": ["gated.fail"],
        },
        # The suite approves from subprocesses, which have no terminal. The
        # default (a terminal is required) has its own scenario.
        "approval": {"require_terminal": False},
        "limits": dict(LIMITS),
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = dict(config[key], **value)
        else:
            config[key] = value
    return config


# =========================================================================
# Scenarios
# =========================================================================


def basics(s: Suite) -> None:
    s.section("A. Zero-config demo, help, configuration")
    r = s.cli(home=None)
    s.check("demo", "python main.py with no arguments runs the demo and every check in it holds", r.code == 0 and "13 of 13 checks passed" in r.out)
    r = s.cli("--help", home=None)
    s.check("I7", "--help exits 0 and documents the exit codes", r.code == 0 and "exit codes" in r.out)
    r = s.cli("run", "--json")
    s.check("I7", "an empty program is a usage error (exit 2)", r.code == 2 and r.get("error", "type") == "UsageError")
    r = s.cli("frobnicate", home=None)
    s.check("I7", "an unknown command is a usage error (exit 2)", r.code == 2)
    r = s.cli("config", "--json")
    s.check("G", "with no config file the built-in demo config is used and says so", r.get("source") == "built-in demo config")
    r = s.cli("config", "--config", "codemode.example.json", "--json")
    s.check("G", "the example config shipped with the project is valid", r.code == 0 and sorted(r.get("connectors", default={})) == ["demo", "files"])

    # The commands the README's quick start shows, as written there.
    r = s.cli("run", "notes = tools.demo.list_notes(); len(notes)", home="readme")
    s.check("demo", "README: a one-line program runs and prints its result", r.code == 0 and "\n10\n" in r.out)
    r = s.cli("run", "tools.demo.create_note(title='Hi', body='There')", home="readme")
    s.check("demo", "README: a write pauses with exit 3", r.code == 3 and "status: paused" in r.out)
    r = s.cli("approve", "last", home="readme")
    s.check("demo", "README: approve last completes it", r.code == 0 and '"id": 11' in r.out)
    r = s.cli("show", "last", home="readme")
    s.check("demo", "README: show last prints the audit log", r.code == 0 and "approval of step(s) 1 by human" in r.out)
    r = s.cli("verify", "last", home="readme")
    s.check("demo", "README: verify last reports replay-ok", r.code == 0 and r.out.startswith("replay-ok"))

    bad = {
        "G1 unknown top-level key": ({"connectors": {"demo": {"type": "demo"}}, "polcy": {}}, "polcy"),
        "G1 unknown key in a section": ({"connectors": {"demo": {"type": "demo", "colour": 1}}}, "colour"),
        "G2 reserved connector name": ({"connectors": {"tools": {"type": "demo"}}}, "tools"),
        "G2 connector name not an identifier": ({"connectors": {"my-notes": {"type": "demo"}}}, "my-notes"),
        "G3 unknown connector type": ({"connectors": {"x": {"type": "grpc"}}}, "grpc"),
        "G4 zero limit": ({"connectors": {"demo": {"type": "demo"}}, "limits": {"timeout_s": 0}}, "timeout_s"),
        "G4 non-numeric limit": ({"connectors": {"demo": {"type": "demo"}}, "limits": {"max_calls": "many"}}, "max_calls"),
        "L5 misspelt policy default": ({"connectors": {"demo": {"type": "demo"}}, "policy": {"default": "alow"}}, "alow"),
        "L6 pattern that is not a string": ({"connectors": {"demo": {"type": "demo"}}, "policy": {"allow": [7]}}, "allow"),
        "G7 executor without {bootstrap}": ({"connectors": {"demo": {"type": "demo"}}, "executor": {"command": ["python"]}}, "bootstrap"),
    }
    for n, (label, (value, needle)) in enumerate(bad.items()):
        s.config("bad%d" % n, value)
        r = s.cli("config", "--json", config="bad%d" % n)
        s.check(label.split()[0], "%s is rejected, exit 2, and the message names it" % label,
                r.code == 2 and needle in json.dumps(r.json), r.out + r.err)
    s.config("notjson", None, raw="{connectors: oops")
    r = s.cli("config", config="notjson")
    s.check("G6", "a config that is not JSON is rejected with exit 2", r.code == 2 and "not readable JSON" in r.err)
    r = s.cli("config", "--config", ".e2e/does-not-exist.json")
    s.check("G6", "a missing explicit config file is an error, not a silent fallback", r.code == 2)


def discovery(s: Suite) -> None:
    s.section("B. Discovery and the tool description (P3, P4, P22)")
    r = s.cli("search", "notes", "--json", config="chaos")
    paths = [hit["path"] for hit in (r.json or [])]
    s.check("P3", "search finds the note tools, including read_note for the query 'notes'",
            r.code == 0 and {"demo.read_note", "demo.list_notes"} <= set(paths), str(paths))
    r = s.cli("describe", "demo.create_note", "--json", config="chaos")
    s.check("P4", "describe gives a signature with required parameters first and the policy",
            r.get("signature") == "demo.create_note(title: str, body: str, tags: list[str] = ...) -> dict" and r.get("policy") == "ask",
            str(r.get("signature")))
    r = s.cli("describe", "demo.red_note", "--json", config="chaos")
    s.check("I7", "describing an unknown tool exits 5 and suggests the close name",
            r.code == 5 and "demo.read_note" in r.get("error", "message", default=""))
    s.check("H20", "that error carries the close names in its hint, and the message still has them",
            "demo.read_note" in r.get("error", "hint", default="") and "Did you mean: demo.read_note" in r.get("error", "message", default=""), r.out[:300])
    r = s.cli("run", "--json", "try:\n    tools.demo.red_note(id=1)\nexcept ToolError as e:\n    out = [e.kind, e.hint]\nout", config="chaos")
    s.check("H20", "a program that calls a tool that does not exist gets the close names in e.hint",
            r.get("result", 0) == "UnknownTool" and "demo.read_note" in str(r.get("result", 1, default="")), r.out[:300])
    r = s.cli("prompt", config="chaos")
    s.check("P22", "the tool description is short and contains no tool schema",
            r.code == 0 and len(r.out) < 2600 and "read_note" not in r.out and "demo (6 tools)" in r.out, "%d chars" % len(r.out))

    # The shape of the last result of a tool that publishes no result schema.
    r = s.cli("describe", "chaos.sleep", "--json", config="chaos", home="shapes")
    s.check("H21", "a tool that never ran has no result shape in its description", r.code == 0 and "lastResultShape" not in r.json, r.out[:300])
    r = s.cli("run", "--json", "[tools.chaos.echo(value={'name': 'content-that-must-not-be-kept', 'count': 3, 'rows': [{'id': 1}]}), tools.chaos.sleep(seconds=0)]",
              config="chaos", home="shapes")
    r = s.cli("describe", "chaos.echo", "--json", config="chaos", home="shapes")
    shape = r.get("lastResultShape", default={})
    s.check("H21", "after a call, describe shows the shape of its result in a later process: keys and types, with a note that it is not a promise",
            isinstance(shape, dict) and sorted(shape) == ["count", "name", "rows"] and shape.get("count") == "int" and str(shape.get("name")).startswith("str")
            and "not a promise" in r.get("lastResultNote", default=""), r.out[:400])
    s.check("H21", "the shape holds no content of the result", "content-that-must-not-be-kept" not in r.out, r.out[:400])
    r = s.cli("describe", "chaos.sleep", "--json", config="chaos", home="shapes")
    s.check("H21", "each tool keeps its own shape", r.get("lastResultShape") == "float", r.out[:300])
    r = s.cli("search", "echo", "--json", config="chaos", home="shapes")
    s.check("H21", "search does not carry the shape", r.code == 0 and "lastResultShape" not in json.dumps(r.json), r.out[:300])
    s.cli("run", "--json", "tools.demo.list_notes()", config="chaos", home="shapes")
    r = s.cli("describe", "demo.list_notes", "--json", config="chaos", home="shapes")
    s.check("H21", "a tool with an outputSchema gets no shape: its schema is its answer", "outputSchema" in r.json and "lastResultShape" not in r.json, r.out[:300])
    s.cli("run", "--json", "tools.chaos.echo(value={'k%d' % n: {'a': [{'b': n}], 'c': 'x' * n} for n in range(300)})", config="chaos", home="shapes")
    kept = list((WORK / "shapes" / "cache").glob("shape-chaos.echo-*.json"))
    s.check("H21", "the shape of a very large result stays small on disk", len(kept) == 1 and kept[0].stat().st_size <= 4000, str([k.stat().st_size for k in kept]))

    (WORK / "shapes_blocked").mkdir(parents=True, exist_ok=True)
    (WORK / "shapes_blocked" / "cache").write_text("a file where the cache folder would be", encoding="utf-8")
    r = s.cli("run", "--json", "tools.chaos.echo(value=[1])", config="chaos", home="shapes_blocked")
    s.check("H21", "a cache that cannot be written does not fail the call", r.code == 0 and r.get("result") == [1], r.out[:300])

    s.cli("describe", "chaos.dash_echo", "--json", config="chaos", home="shapes", quiet=True)
    r = s.cli("run", "--json", "before = describe('chaos.dash_echo')\ntools.chaos.dash_echo(value={'seen': 1})\ntools.demo.create_note(title='Shape', body='replay')\n"
              "['lastResultShape' in before, 'lastResultShape' in describe('chaos.dash_echo')]", config="chaos", home="shapes")
    s.check("H21", "a program that pauses has called a tool, and so changed what describe would say now", r.code == 3, r.out[:300])
    r = s.cli("approve", "last", "--json", config="chaos", home="shapes")
    s.check("H21 I3", "resumed, its first describe() gives what it gave on the first pass (no shape), and its second one, made after the call, the shape",
            r.code == 0 and r.get("result") == [False, True], r.out[:300])
    r = s.cli("verify", "last", "--json", config="chaos", home="shapes")
    s.check("H21 I3", "verify replays that execution from its log", r.code == 0 and r.get("verified") is True, r.out[:200])


def semantics(s: Suite) -> None:
    s.section("C. What a program can do (P1, P2, P5, P14)")
    compose = s.program("compose", '''
notes = tools.demo.list_notes()
bodies = parallel([("demo.read_note", {"id": n["id"]}) for n in notes])
print("read", len(bodies), "notes")
state["titles"] = [b["title"] for b in bodies]
{"notes": len(bodies), "words": sum(len(b["body"].split()) for b in bodies)}
''')
    r = s.cli("run", "-f", compose, "--json", config="chaos")
    s.check("P1 P5", "one program makes 11 tool calls and completes", r.code == 0 and r.get("stats", "toolCalls") == 11)
    s.check("I4", "note bodies stayed in the sandbox: only the two numbers came back",
            r.get("result", "notes") == 10 and sorted(r.get("result")) == ["notes", "words"]
            and "null currency" not in r.out and "retry key" not in r.out, r.out[:200])
    s.check("P2", "tool results were far larger than what was returned",
            r.get("stats", "toolResultBytes", default=0) > 20 * len(json.dumps(r.get("result"))))
    s.check("P2", "print output arrives as logs", "read 10 notes" in r.get("logs", default=""))

    r = s.cli("run", "--json", "len(state['titles'])", config="chaos")
    s.check("P14", "state written by one program is there for the next, with no tool call",
            r.get("result") == 10 and r.get("stats", "toolCalls") == 0)
    r = s.cli("run", "--json", "--session", "other", "sorted(state)", config="chaos")
    s.check("P14", "another session does not see that state", r.get("result") == [])

    r = s.cli("run", "--json", "x = 1\ny = 0\nx / y", config="chaos")
    s.check("I7", "a program that raises exits 1 with the exception type and a traceback of the program only",
            r.code == 1 and r.get("error", "type") == "ZeroDivisionError" and "line 3" in r.get("error", "traceback", default="")
            and "codemode-runner" not in r.get("error", "traceback", default=""), r.out[:300])
    r = s.cli("run", "--json", "class Config:\n    return 5", config="chaos")
    s.check("X10 H18", "a return in a class body is a SyntaxError with a hint that says where return works",
            r.code == 1 and r.get("error", "type") == "SyntaxError" and "class body" in r.get("error", "hint", default="")
            and "return VALUE" in r.get("error", "hint", default=""), r.out[:300])
    r = s.cli("run", "--json", "await tools.demo.list_notes()", config="chaos")
    s.check("X10", "top-level await is a SyntaxError with a hint that calls are synchronous",
            r.code == 1 and "synchronous" in r.get("error", "hint", default=""))
    r = s.cli("run", "--json", "def broken(:\n  pass", config="chaos")
    s.check("X10", "invalid Python is reported as an error payload that points at the program, not at the parser",
            r.code == 1 and r.get("error", "type") == "SyntaxError" and "<code>" in r.get("error", "traceback", default="")
            and "ast.py" not in r.get("error", "traceback", default=""), r.out[:400])
    r = s.cli("run", "--json", "tools.demo.read_notes(id=1)", config="chaos")
    s.check("P13", "an unknown tool fails with UnknownTool and suggests the right name",
            r.code == 1 and r.get("error", "type") == "UnknownTool" and "demo.read_note" in r.get("error", "message", default=""))
    r = s.cli("run", "--json", "try:\n    tools.nowhere.thing()\nexcept ToolError as e:\n    out = e.kind\nout", config="chaos")
    s.check("P7", "a failed call raises ToolError, which the program can catch", r.get("result") == "UnknownTool")
    r = s.cli("run", "--json", "object()", config="chaos")
    s.check("X11", "a result JSON cannot hold comes back as its repr, flagged",
            r.code == 0 and r.get("resultIsRepr") is True and "object object" in str(r.get("result")))
    r = s.cli("run", "--json", "state['bad'] = {1, 2}\n1", config="chaos")
    s.check("X11", "a state value JSON cannot hold is an error that names the key",
            r.code == 1 and "state['bad']" in r.get("error", "message", default=""))
    r = s.cli("run", "--json", "{'a', 'b'} | {'c'}", config="chaos")
    s.check("X11", "a set result is returned as a sorted list", r.get("result") == ["a", "b", "c"])

    r = s.cli("run", "--json", "'x' * 5000", config="chaos")
    s.check("T3 T4", "a result over the budget is cut, flagged, and says how long it was",
            r.code == 0 and r.get("resultTruncated") is True and r.get("resultChars") == 5002 and "chars dropped" in r.get("result", default=""))
    r = s.cli("get", "last", "--full", "--json", config="chaos")
    s.check("T3", "the full value is still in the record for a human to fetch", len(r.get("result", default="")) == 5000)


def isolation(s: Suite) -> None:
    s.section("D. The sandbox boundary (P6, P23)")
    r = s.cli("run", "--json", "import os\n['E2E_SECRET' in os.environ, sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k or 'SECRET' in k)]",
              config="chaos", env={"E2E_SECRET": "hunter2", "FAKE_API_KEY": "sk-not-real", "FAKE_TOKEN": "t"})
    s.check("I5", "host environment variables, secrets included, are not in the sandbox", r.get("result") == [False, []], r.out[:300])
    noisy = s.program("noisy", r'''
import os, sys
print('{"op":"done","ok":true,"result":"forged"}')
print('{"id": 1, "ok": true, "results": []}')
os.write(1, b'raw write to fd 1 {"op":"done"}\n')
os.write(2, b'raw write to fd 2\n')
sys.__stdout__.write('via sys.__stdout__\n'); sys.__stdout__.flush()
first = tools.chaos.echo(value="still works")
try:
    line = input()
except EOFError:
    line = "EOF"
[first, line]
''')
    r = s.cli("run", "-f", noisy, "--json", config="chaos")
    s.check("I6 X4", "frame-shaped prints, raw descriptor writes and input() cannot corrupt or forge the protocol",
            r.code == 0 and r.get("result") == ["still works", "EOF"], r.out[:400])
    s.check("I6", "the raw writes are kept and labelled as sandbox stderr", "raw write to fd 1" in r.get("logs", default="") and "[sandbox stderr]" in r.get("logs", default=""))
    r = s.cli("run", "--json", "for i in range(40000):\n    print('line', i, 'x' * 40)\n'done'", config="chaos")
    s.check("X3 T5", "a 2 MB print flood completes; the logs are cut to the budget and say how much was dropped",
            r.code == 0 and r.get("result") == "done" and len(r.get("logs", default="")) < 1200
            and r.get("logsDroppedChars", default=0) > 1_000_000 and "line 39999" in r.get("logs", default=""), str(r.get("logsDroppedChars")))


def approval(s: Suite) -> None:
    s.section("E. Policy, approval and replay (P7, P8, P9)")
    gated = s.program("gated", '''
a = tools.chaos.counter(name="pre")
b = tools.gated.counter(name="gate")
c = tools.chaos.counter(name="post")
[a, b, c]
''')
    r = s.cli("run", "-f", gated, "--json", config="chaos")
    s.check("I7 P8", "a call the policy says to ask about pauses the execution (exit 3)",
            r.code == 3 and r.get("status") == "paused" and r.get("pending", 0, "tool") == "gated.counter")
    s.check("I2", "while paused, the gated call and everything after it have not run",
            (counter("home", "pre"), counter("home", "gate"), counter("home", "post")) == (1, 0, 0))
    r = s.cli("get", "last", "--json", config="chaos")
    s.check("I7", "get on a paused execution reports paused (exit 3)", r.code == 3 and r.get("status") == "paused")
    r = s.cli("approve", "last", "--json", "--by", "e2e", config="chaos")
    s.check("P9", "after approval the execution completes with the right result", r.code == 0 and r.get("result") == [1, 1, 1], r.out[:300])
    s.check("I9", "the call before the pause was replayed, not run again; the approved call ran exactly once",
            (counter("home", "pre"), counter("home", "gate"), counter("home", "post")) == (1, 1, 1) and r.get("stats", "replayed") == 1)
    r = s.cli("approve", "last", "--json", config="chaos")
    s.check("I7", "approving an execution that is not paused is refused (exit 2)", r.code == 2 and r.get("error", "type") == "StateError")
    r = s.cli("verify", "last", "--json", config="chaos")
    s.check("I3", "verify replays the execution from its log and reproduces the result (exit 0)", r.code == 0 and r.get("verified") is True)
    s.check("I3", "verify called no tool: the counters did not move",
            (counter("home", "pre"), counter("home", "gate"), counter("home", "post")) == (1, 1, 1))
    r = s.cli("show", "last", config="chaos")
    s.check("P10", "the audit log shows pending, the approval and who gave it, then applied",
            "pending" in r.out and "approval of step(s) 2 by e2e" in r.out and "ask(default)" in r.out)
    r = s.cli("get", "last", "--json", config="chaos")
    s.check("P16", "the record now carries how it was verified", r.get("verification") == "replay-ok")

    r = s.cli("run", "--json", "tools.gated.counter(name='rejected')", config="chaos")
    r = s.cli("reject", "last", "--json", "--reason", "not today", config="chaos")
    s.check("P8", "reject ends the execution as rejected (exit 1) and the call never runs",
            r.code == 1 and r.get("status") == "rejected" and counter("home", "rejected") == 0)
    r = s.cli("approve", "last", "--json", config="chaos")
    s.check("I1", "a rejected execution cannot be approved afterwards", r.code == 2)

    r = s.cli("run", "--json", "try:\n    tools.gated.fail(message='x')\nexcept ToolError as e:\n    out = [e.kind, tools.chaos.echo(value='carried on')]\nout", config="chaos")
    s.check("L1 P7", "a denied call raises PolicyDenied in the program, which carries on", r.get("result") == ["PolicyDenied", "carried on"])

    batch = s.program("batch", '''
parallel([("chaos.counter", {"name": "batch_free"}), ("gated.counter", {"name": "batch_gated"})])
''')
    r = s.cli("run", "-f", batch, "--json", config="chaos")
    s.check("I2", "a parallel batch with one gated call pauses before any call in the batch runs",
            r.code == 3 and (counter("home", "batch_free"), counter("home", "batch_gated")) == (0, 0))
    r = s.cli("approve", "last", "--json", config="chaos")
    s.check("I9", "after approval both calls ran once", r.get("result") == [1, 1] and (counter("home", "batch_free"), counter("home", "batch_gated")) == (1, 1))

    r = s.cli("run", "--interactive", "--json", "tools.gated.counter(name='tty_yes')", config="chaos", stdin="y\n")
    s.check("P8", "with --interactive, answering y approves in place and the call runs once",
            r.code == 0 and r.get("result") == 1 and counter("home", "tty_yes") == 1, r.out[:300] + r.err[:300])
    r = s.cli("run", "--interactive", "--json", "tools.gated.counter(name='tty_no')", config="chaos", stdin="\n")
    s.check("P8", "anything but yes refuses: the default answer is the safe one",
            r.code == 1 and r.get("error", "type") == "PolicyDenied" and counter("home", "tty_no") == 0)

    s.config("observe", chaos_config(policy={"mode": "observe"}))
    r = s.cli("run", "--json", "tools.gated.counter(name='observed')", config="observe", home="observe")
    log = s.cli("show", "last", "--json", config="observe", home="observe")
    calls = [e for e in (log.json or []) if e.get("type") == "call" and e.get("state") == "applied"]
    s.check("L8 P18", "observe mode runs the call and logs the decision it would have enforced",
            r.code == 0 and calls and calls[0].get("decision") == "ask" and calls[0].get("enforced") is False)


def replay_faults(s: Suite) -> None:
    s.section("F. Replay under nondeterminism (P9, P19)")
    drift = s.program("drift", '''
import time
tools.chaos.echo(value=time.time())
tools.gated.counter(name="drift")
''')
    s.cli("run", "-f", drift, "--json", config="chaos")
    r = s.cli("approve", "last", "--json", config="chaos")
    s.check("X12", "a program that changes between passes is stopped with ReplayDivergence and a hint about step()",
            r.code == 1 and r.get("error", "type") == "ReplayDivergence" and "step(" in r.get("error", "hint", default="")
            and counter("home", "drift") == 0, r.out[:300])
    steady = s.program("steady", '''
import time
now = step("now", time.time)
echoed = tools.chaos.echo(value=now)
tools.gated.counter(name="steady")
echoed == now
''')
    s.cli("run", "-f", steady, "--json", config="chaos")
    r = s.cli("approve", "last", "--json", config="chaos")
    s.check("P9", "the same program with step() resumes cleanly", r.code == 0 and r.get("result") is True and counter("home", "steady") == 1)
    r = s.cli("run", "--json", "step('a', lambda: tools.chaos.echo(value=1))", config="chaos")
    s.check("P9", "a tool call inside step() is refused, because replay would skip it",
            r.code == 1 and r.get("error", "type") == "StepError" and "inside step" in r.get("error", "message", default=""), r.out[:300])
    s.cli("run", "--json", "import random\nrandom.random()", config="chaos")
    r = s.cli("verify", "last", "--json", config="chaos")
    s.check("I7 I3", "verify catches a result the log does not explain (exit 4)", r.code == 4 and r.get("verified") is False)

    rng = random.Random(20261120)
    failures = []
    total = 25
    for n in range(total):
        lines = ["acc = []"]
        for i in range(rng.randint(1, 6)):
            op = rng.choice(["echo", "read", "parallel", "step", "state", "search", "fail"])
            if op == "echo":
                lines.append("acc.append(tools.chaos.echo(value=%d))" % rng.randint(0, 999))
            elif op == "read":
                lines.append("acc.append(tools.demo.read_note(id=%d)['title'])" % rng.randint(1, 10))
            elif op == "parallel":
                ids = [rng.randint(1, 10) for _ in range(rng.randint(1, 4))]
                lines.append("acc.extend(n['id'] for n in parallel([('demo.read_note', {'id': i}) for i in %r]))" % ids)
            elif op == "step":
                lines.append("import random\nacc.append(step('r%d', random.random))" % i)
            elif op == "state":
                lines.append("state['k%d'] = len(acc)" % rng.randint(0, 3))
            elif op == "search":
                lines.append("acc.append(len(search(%r)))" % rng.choice(["note", "echo", "counter", "zzz"]))
            else:
                lines.append("try:\n    tools.chaos.fail(message='boom')\nexcept ToolError as e:\n    acc.append(e.kind)")
        lines.append("acc")
        source = "\n".join(lines)
        ran = s.cli("run", "--json", source, config="chaos", home="property", quiet=True)
        checked = s.cli("verify", "last", "--json", config="chaos", home="property", quiet=True)
        if ran.code != 0 or checked.code != 0:
            failures.append({"program": source, "run": ran.out[:300], "verify": checked.out[:300]})
        if n == 0:
            s.log()
            s.log("Property test, seed 20261120, %d generated programs; each is run, then verified by replay." % total)
            s.log("First generated program:")
            s.log(source)
    s.check("I3 P19", "property: %d random programs (calls, parallel, steps, state, search, failures) all replay to the same result" % total,
            not failures, json.dumps(failures[:1])[:600])


def faults(s: Suite) -> None:
    s.section("G. Injected faults (P12, P20)")
    started = time.monotonic()
    r = s.cli("run", "--json", "while True:\n    pass", config="chaos")
    s.check("X1", "an endless loop is killed at the timeout and reported as Timeout (exit 1)",
            r.code == 1 and r.get("error", "type") == "Timeout" and time.monotonic() - started < 15, "%.1fs" % (time.monotonic() - started))
    r = s.cli("run", "--json", "1 + 1", config="chaos")
    s.check("I8", "the next command works", r.code == 0 and r.get("result") == 2)

    started = time.monotonic()
    r = s.cli("run", "--json", "try:\n    tools.chaos.sleep(seconds=30)\nexcept ToolError as e:\n    out = e.kind\nout", config="chaos")
    s.check("X2", "a stalled tool fails that call with ToolTimeout; the program and the CLI do not wait for it",
            r.code == 0 and r.get("result") == "ToolTimeout" and time.monotonic() - started < 10, "%.1fs %s" % (time.monotonic() - started, r.out[:200]))
    r = s.cli("run", "--json", "import os\nprint('about to die')\nos._exit(3)", config="chaos")
    s.check("X5", "a sandbox that dies is reported as SandboxCrashed, with its exit code",
            r.code == 1 and r.get("error", "type") == "SandboxCrashed" and "code 3" in r.get("error", "message", default=""), r.out[:300])
    r = s.cli("run", "--json", "try:\n    tools.chaos.big(chars=50000)\nexcept ToolError as e:\n    out = e.kind\nout", config="chaos")
    s.check("X8", "a tool result over the size limit fails the call with ValueTooLarge", r.get("result") == "ValueTooLarge")
    r = s.cli("run", "--json", "n = 0\ntry:\n    for i in range(100):\n        tools.chaos.echo(value=i)\n        n += 1\nexcept ToolError as e:\n    out = [e.kind, n]\nout", config="chaos")
    s.check("X9", "the call limit stops the program at exactly the limit", r.get("result") == ["CallLimitExceeded", 20])
    r = s.cli("run", "--json", "tools.chaos.echo(value='x' * 30000)", config="chaos")
    s.check("X8", "arguments over the size limit are refused before the tool is called",
            r.code == 1 and r.get("error", "type") == "ValueTooLarge")

    executors = {
        "X6 garbage": (["{python}", "-c", "print('hello, I am not a frame')  # {bootstrap}"], "ProtocolError"),
        "X7 silent": (["{python}", "-c", "import time; time.sleep(60)  # {bootstrap}"], "ExecutorStartFailed"),
        "X7 exits at once": (["{python}", "-c", "raise SystemExit(7)  # {bootstrap}"], "ExecutorStartFailed"),
        "X7 missing binary": (["definitely-not-a-real-binary-xyz", "{bootstrap}"], "ExecutorStartFailed"),
    }
    for n, (label, (command, expected)) in enumerate(executors.items()):
        s.config("exec%d" % n, chaos_config(executor={"command": command}, limits=dict(LIMITS, startup_timeout_s=2)))
        started = time.monotonic()
        r = s.cli("run", "--json", "1", config="exec%d" % n)
        s.check(label.split()[0], "executor fault (%s) ends as %s, promptly" % (label, expected),
                r.code == 1 and r.get("error", "type") == expected and time.monotonic() - started < 12, r.out[:300])

    slow = s.program("slow", 'tools.chaos.sleep(seconds=1.5)\n"slept"\n')
    background = subprocess.Popen([PY, "main.py", "--home", ".e2e/home", "--config", ".e2e/chaos.json", "run", "-f", slow, "--json"],
                                  cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=s.env())
    running_id = None
    for _ in range(40):
        time.sleep(0.05)
        rows = s.cli("executions", "--json", "--limit", "1", config="chaos", quiet=True).json
        if isinstance(rows, list) and rows and rows[0]["status"] == "running":
            running_id = rows[0]["executionId"]
            break
    r = s.cli("reject", "last", "--json", config="chaos") if running_id else Result(-1, "", "never saw it running")
    out, _ = background.communicate(timeout=30)
    s.check("X13", "a second pass on an execution that is running is refused as Busy, and the first one finishes",
            r.code == 1 and r.get("error", "type") == "Busy" and json.loads(out).get("result") == "slept", r.out[:300] + r.err[:200])


def other_interpreter(s: Suite) -> None:
    s.section("H. A different sandbox than the host (P6)")
    host = "%d.%d" % sys.version_info[:2]
    # 3.8 first: it is the oldest interpreter the sandbox side is written for.
    candidates = [[name] for name in ("python3.8", "python3.9", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14")]
    if shutil.which("py"):
        # The Windows launcher lists every installed interpreter with its path.
        try:
            listing = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ""
        candidates += [[line.split()[-1]] for line in listing.splitlines() if line.strip().lower().endswith("python.exe")]
    for candidate in candidates:
        if not (shutil.which(candidate[0]) or os.path.isfile(candidate[0])):
            continue
        try:
            probe = subprocess.run(candidate + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        version = probe.stdout.strip()
        if probe.returncode == 0 and version and version != host:
            s.mask(candidate[0], "<other-python>")
            s.config("otherpy", chaos_config(executor={"command": candidate + ["-I", "-u", "-c", "{bootstrap}"]}))
            r = s.cli("run", "--json", "import sys\n['%d.%d' % sys.version_info[:2], tools.chaos.echo(value=7), len(parallel([('demo.read_note', {'id': 1})]))]", config="otherpy")
            s.check("P6", "executor.command runs the sandbox under Python %s while the host is %s; tools still work" % (version, host),
                    r.code == 0 and r.get("result") == [version, 7, 1], r.out[:300])
            return
    s.skip("no second Python interpreter found; the pluggable executor was only exercised by the fault cases")


class Mcp:
    """A raw stdio JSON-RPC peer, so the test owns every byte it sends."""

    def __init__(self, argv: List[str], env: Dict[str, str]) -> None:
        self.proc = subprocess.Popen([PY] + argv, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.inbox: "queue.Queue[Any]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.inbox.put({"unparseable": line.decode("utf-8", "replace")})

    def send(self, message: Any, raw: Optional[bytes] = None) -> None:
        self.proc.stdin.write(raw if raw is not None else (json.dumps(message) + "\n").encode())  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def recv(self, timeout: float = 20) -> Any:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def request(self, request_id: Any, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20) -> Any:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.recv(timeout)

    def close(self) -> int:
        self.proc.stdin.close()  # type: ignore[union-attr]
        try:
            return self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return -9


def mcp_server(s: Suite) -> None:
    s.section("I. The runtime as an MCP server (P1, P8, P17, P25)")
    s.log("$ python main.py --home .e2e/mcp --config .e2e/chaos.json serve    (driven over stdin/stdout by the suite)")
    peer = Mcp(["main.py", "--home", ".e2e/mcp", "--config", ".e2e/chaos.json", "serve"], s.env())

    def exchange(label: str, reply: Any) -> Any:
        s.log("<- %s: %s" % (label, _clip(json.dumps(reply), 500)))
        return reply or {}

    init = exchange("initialize", peer.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}))
    s.check("P1", "initialize negotiates the protocol version and announces tools", init.get("result", {}).get("protocolVersion") == "2025-06-18" and "tools" in init.get("result", {}).get("capabilities", {}))
    peer.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = exchange("tools/list", peer.request(2, "tools/list"))
    names = [t["name"] for t in listed.get("result", {}).get("tools", [])]
    s.check("P1 P8 P17", "the server lists exactly one tool, execute: no approve, no policy, no log access", names == ["execute"], str(names))
    done = exchange("tools/call code", peer.request(3, "tools/call", {"name": "execute", "arguments": {"code": "len(tools.demo.list_notes())"}}))
    structured = done.get("result", {}).get("structuredContent", {})
    s.check("P13", "execute returns the payload as structured content and as text", structured.get("status") == "completed" and structured.get("result") == 10
            and json.loads(done["result"]["content"][0]["text"]) == structured and done["result"]["isError"] is False)
    paused = exchange("tools/call gated", peer.request(4, "tools/call", {"name": "execute", "arguments": {"code": "tools.gated.counter(name='mcp')"}}))
    paused_id = paused.get("result", {}).get("structuredContent", {}).get("executionId")
    s.check("P8", "a gated call comes back as paused, not as an error", paused.get("result", {}).get("structuredContent", {}).get("status") == "paused" and paused["result"]["isError"] is False)
    approved = s.cli("approve", "last", "--json", config="chaos", home="mcp")
    s.check("P8", "a human approves from the CLI, outside the agent's reach", approved.code == 0 and counter("mcp", "mcp") == 1)
    fetched = exchange("tools/call executionId", peer.request(5, "tools/call", {"name": "execute", "arguments": {"executionId": paused_id}}))
    s.check("P8", "the agent then fetches the outcome by executionId", fetched.get("result", {}).get("structuredContent", {}).get("result") == 1)
    both = exchange("tools/call code+id", peer.request(6, "tools/call", {"name": "execute", "arguments": {"code": "1", "executionId": "x"}}))
    s.check("P13", "passing both code and executionId is a tool error the model can read", both.get("result", {}).get("isError") is True)
    failed = exchange("tools/call raising", peer.request(7, "tools/call", {"name": "execute", "arguments": {"code": "1/0"}}))
    s.check("P13", "a failing program is isError with the error payload", failed.get("result", {}).get("isError") is True and failed["result"]["structuredContent"]["error"]["type"] == "ZeroDivisionError")
    s.check("P13", "an unknown method gets -32601", exchange("unknown method", peer.request(8, "resources/list")).get("error", {}).get("code") == -32601)
    s.check("P13", "an unknown tool gets -32602", exchange("unknown tool", peer.request(9, "tools/call", {"name": "approve", "arguments": {}})).get("error", {}).get("code") == -32602)
    peer.send(None, raw=b"this is not json\n")
    s.check("I8", "a garbage line gets a parse error and the server keeps serving", exchange("garbage line", peer.recv()).get("error", {}).get("code") == -32700)

    peer.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "execute", "arguments": {"code": "import time\ntime.sleep(30)"}}})
    time.sleep(1.0)
    peer.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 10, "reason": "e2e"}})
    started = time.monotonic()
    pong = exchange("ping after cancel", peer.request(11, "ping", timeout=10))
    s.check("X15 P25", "a cancelled execution stops at once, sends no response, and the server answers the next request",
            pong.get("id") == 11 and pong.get("result") == {} and time.monotonic() - started < 5, json.dumps(pong)[:200])
    cancelled = s.cli("get", "last", "--json", config="chaos", home="mcp")
    s.check("X15 I1", "the cancelled execution is recorded as an error of type Cancelled", cancelled.get("error", "type") == "Cancelled")
    s.check("I8", "the server exits cleanly when stdin closes", peer.close() == 0)


def mcp_tool(s: Suite) -> None:
    s.section("Y. The execute tool: arguments, background runs, progress, and a client that does several things at once")
    s.config("mcp2", chaos_config(limits=dict(LIMITS, timeout_s=40, call_timeout_s=35)))
    s.log("$ python main.py --home .e2e/mcp2 --config .e2e/mcp2.json serve    (driven over stdin/stdout by the suite)")
    peer = Mcp(["main.py", "--home", ".e2e/mcp2", "--config", ".e2e/mcp2.json", "serve"], s.env())
    seen: List[Any] = []

    def answer(request_id: Any, timeout: float = 30) -> Dict[str, Any]:
        """The response to one request. Messages that arrive before it
        (notifications, answers to other requests) are kept in order."""
        for message in seen:
            if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                return message
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = peer.recv(max(0.1, deadline - time.monotonic()))
            if message is None:
                break
            seen.append(message)
            if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                return message
        return {}

    def execute(request_id: Any, arguments: Dict[str, Any], meta: Optional[Dict[str, Any]] = None, wait: bool = True) -> Dict[str, Any]:
        params: Dict[str, Any] = {"name": "execute", "arguments": arguments}
        if meta:
            params["_meta"] = meta
        peer.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})
        if not wait:
            return {}
        reply = answer(request_id)
        s.log("<- execute %s: %s" % (json.dumps(arguments)[:160], _clip(json.dumps(reply.get("result", {}).get("structuredContent")), 400)))
        return reply.get("result", {}).get("structuredContent") or {}

    try:
        peer.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
        peer.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = peer.request(2, "tools/list")
        tools = listed.get("result", {}).get("tools", [])
        properties = sorted(tools[0]["inputSchema"]["properties"]) if tools else []
        s.check("P1 P17", "still exactly one tool; its new parameters ask and stop, and none of them approves or changes policy",
                [t["name"] for t in tools] == ["execute"]
                and properties == ["args", "background", "cancel", "check", "code", "executionId", "reuse", "select", "session", "wait"], str(properties))

        out = execute(10, {"code": "args['n'] * 2", "args": {"n": 21}})
        s.check("H7", "arguments for the program travel as an object next to the code", out.get("result") == 42, json.dumps(out)[:200])
        out = execute(11, {"code": "tools.demo.red_note(id=1)\ntools.chaos.counter(name='mcp_checked')", "check": True})
        s.check("C8", "check compiles and looks tools up without running: the unknown name is found and nothing ran",
                out.get("error", {}).get("type") == "UnknownTool" and "demo.read_note" in out["error"].get("hint", "") and counter("mcp2", "mcp_checked") == 0, json.dumps(out)[:300])

        started = time.monotonic()
        out = execute(20, {"code": "print('working')\ntools.chaos.sleep(seconds=6)\n'background done'", "background": True})
        job = out.get("executionId")
        s.check("R1", "background returns the execution id at once, with status running",
                out.get("status") == "running" and bool(job) and time.monotonic() - started < 5, "%.1fs %s" % (time.monotonic() - started, json.dumps(out)[:200]))
        s.check("E2", "over MCP the hint names the tool call to make, not a command line",
                '"executionId"' in out.get("hint", "") and '"wait"' in out.get("hint", "") and "main.py" not in out.get("hint", ""), out.get("hint", "")[:200])
        counter_id = [200]

        def in_flight(job_id: Any) -> Any:
            counter_id[0] += 1
            now = execute(counter_id[0], {"executionId": job_id})
            return now if now.get("progress", {}).get("running") else None

        poll(lambda: in_flight(job))
        out = execute(21, {"executionId": job})
        s.check("R2", "asking by executionId while it runs shows the call in flight and the output so far",
                out.get("status") == "running" and (out.get("progress", {}).get("running") or [{}])[0].get("tool") == "chaos.sleep" and "working" in out.get("logs", ""), json.dumps(out)[:300])
        out = execute(22, {"executionId": job, "wait": 20})
        s.check("R3", "wait returns the outcome once the program has ended", out.get("status") == "completed" and out.get("result") == "background done", json.dumps(out)[:200])

        execute(30, {"code": "tools.chaos.sleep(seconds=3)\n'slow one'"}, wait=False)
        time.sleep(0.5)
        peer.send({"jsonrpc": "2.0", "id": 31, "method": "ping"})
        execute(32, {"code": "'quick one'"}, wait=False)
        quick, slow = answer(32, 20), answer(30, 20)
        order = [m.get("id") for m in seen if isinstance(m, dict) and m.get("id") in (30, 31, 32)]
        s.check("X15 R2", "a program that runs for seconds does not block the client: a ping and a second program are answered while it runs",
                order == [31, 32, 30] and quick.get("result", {}).get("structuredContent", {}).get("result") == "quick one"
                and slow.get("result", {}).get("structuredContent", {}).get("result") == "slow one", str(order))

        out = execute(40, {"code": "import time\ntools.chaos.counter(name='mcp_progress')\ntime.sleep(6)\n'reported'"}, meta={"progressToken": "tok-40"})
        notes = [m for m in seen if isinstance(m, dict) and m.get("method") == "notifications/progress" and m.get("params", {}).get("progressToken") == "tok-40"]
        s.check("M7", "a client that asks for progress gets it while a foreground program runs",
                out.get("result") == "reported" and len(notes) >= 1 and "1 call(s) done" in notes[0]["params"].get("message", ""), json.dumps(notes[:1])[:300])

        out = execute(50, {"code": "kept = {'rows': [1, 2, 3]}\ntools.chaos.fail(message='late')"})
        failed = out.get("executionId")
        s.check("H8", "a failed program's variables are named, and the hint says how to read one with this tool",
                out.get("variables") == {"kept": {"rows": {"list": 3, "of": "int"}}} and '"select": "variables.NAME"' in out.get("hint", "") and '"reuse"' in out.get("hint", "")
                or out.get("variables") == {"kept": {"rows": {"list": 3, "of": "int"}}} and '"select"' in out.get("hint", ""), json.dumps(out)[-400:])
        out = execute(51, {"executionId": failed, "select": "variables.kept.rows"})
        s.check("H12", "select returns one part of an execution", out.get("value") == [1, 2, 3], json.dumps(out)[:200])
        out = execute(52, {"code": "a = tools.chaos.counter(name='mcp_reuse')\ntools.chaos.fail(message='again')"})
        out = execute(53, {"code": "a = tools.chaos.counter(name='mcp_reuse')\n[a, 'continued']", "reuse": out.get("executionId")})
        s.check("C7", "reuse continues a fixed program without repeating the call that worked",
                out.get("result") == [1, "continued"] and out.get("stats", {}).get("reused") == 1 and counter("mcp2", "mcp_reuse") == 1, json.dumps(out)[:300])

        out = execute(60, {"code": "tools.chaos.counter(name='mcp_cancel')\ntools.chaos.sleep(seconds=30)\ntools.chaos.counter(name='mcp_cancel')", "background": True})
        poll(lambda: in_flight(out.get("executionId")))
        started = time.monotonic()
        out = execute(61, {"executionId": out.get("executionId"), "cancel": True})
        s.check("R4", "cancel stops a background program: Cancelled, promptly, with the first call applied and the last one never made",
                out.get("error", {}).get("type") == "Cancelled" and time.monotonic() - started < 10 and counter("mcp2", "mcp_cancel") == 1, json.dumps(out)[:300])
        out = execute(62, {"code": "1", "executionId": "x"})
        s.check("P13", "code and executionId together are still a usage error the model can read", out.get("error", {}).get("type") == "UsageError")
    finally:
        code = peer.close()
    s.check("I8", "the server exits cleanly when stdin closes", code == 0, "exit %s" % code)


def mcp_connector(s: Suite) -> None:
    s.section("J. MCP servers as tool sources (P3, P6)")
    s.config("mcp", {
        "connectors": {
            "demo": {"type": "demo"},
            "inner": {"type": "mcp-stdio", "command": [PY, "main.py", "--home", ".e2e/inner", "serve"]},
            "broken": {"type": "mcp-stdio", "command": [PY, "-c", "import sys; sys.stderr.write('boom at startup'); sys.exit(3)"]},
            "missing": {"type": "mcp-stdio", "command": ["definitely-not-a-real-binary-xyz"]},
            "needs_key": {"type": "mcp-stdio", "command": [PY, "-c", "pass"], "env": {"TOKEN": "${E2E_UNSET_TOKEN}"}},
        },
        "policy": {"default": "allow"},
        "limits": dict(LIMITS, timeout_s=30, call_timeout_s=20),
    })
    r = s.cli("run", "--json", "inner = tools.inner.execute(code=\"tools.demo.list_notes()[0]['title']\")\n[inner['status'], inner['result']]", config="mcp")
    s.check("P3", "a tool on an MCP server is called from a program (here the server is a second copy of this runtime)",
            r.code == 0 and r.get("result") == ["completed", "Kickoff: Lighthouse migration"], r.out[:400])
    r = s.cli("search", "execute", "--json", config="mcp")
    # The expected text follows the parameters of this runtime's own execute
    # tool, which the connector "inner" points at. The tool gained optional
    # parameters (args, background, reuse, check, wait, select, cancel), so
    # the expectation was extended with them; what is checked is unchanged.
    s.check("P4", "its signature is generated from the schema the server published",
            r.get(0, "signature") == "inner.execute(code: str = ..., executionId: str = ..., session: str = ..., args: dict = ..., "
            "background: bool = ..., reuse: str = ..., check: bool = ..., wait: float = ..., ...) -> Any", str(r.get(0, "signature")))
    r = s.cli("run", "--json", "out = {}\nfor ns in ('broken', 'missing', 'needs_key'):\n    try:\n        getattr(tools, ns).anything()\n    except ToolError as e:\n        out[ns] = [e.kind, e.message]\nout['demo'] = len(tools.demo.list_notes())\nout", config="mcp")
    result = r.get("result", default={})
    s.check("X14", "a server that exits at startup makes its namespace unavailable, with its stderr in the message",
            result.get("broken", [""])[0] == "ConnectorUnavailable" and "boom at startup" in result.get("broken", ["", ""])[1], json.dumps(result)[:400])
    s.check("X14", "a server whose binary is missing is unavailable too", result.get("missing", [""])[0] == "ConnectorUnavailable")
    s.check("G5", "a connector whose ${VAR} is not set says which variable is missing", "E2E_UNSET_TOKEN" in result.get("needs_key", ["", ""])[1])
    s.check("X14 I8", "the other namespaces keep working in the same program", result.get("demo") == 10)
    r = s.cli("run", "--json", "try:\n    tools.inner.execute()\nexcept ToolError as e:\n    out = e.kind\nout", config="mcp")
    s.check("X14", "a tool result the server marks isError raises ToolError in the program", r.get("result") == "ToolError")

    sdk_python = os.environ.get("CODEMODE_E2E_MCP_PYTHON")
    if not sdk_python:
        s.skip("interop with the official MCP SDK (set CODEMODE_E2E_MCP_PYTHON to an interpreter that has the mcp package)")
        return
    s.mask(sdk_python, "<mcp-python>")
    s.log()
    s.log("Interop: the official MCP Python SDK, from the interpreter in CODEMODE_E2E_MCP_PYTHON.")
    client = subprocess.run([sdk_python, "-I", "tests/fixtures/sdk_client.py", PY, "main.py", "--home", ".e2e/sdk", "serve"],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=90, env=s.env())
    s.log("$ <mcp-python> tests/fixtures/sdk_client.py python main.py --home .e2e/sdk serve")
    s.log("[exit %d]" % client.returncode)
    s.log(_clip(client.stdout + ("\n[stderr] " + client.stderr if client.stderr.strip() else "")))
    try:
        seen = json.loads(client.stdout)
    except ValueError:
        seen = {}
    s.check("P1", "the official SDK client lists the one tool and runs a program through this server",
            seen.get("tools") == ["execute"] and (seen.get("structured") or {}).get("result") == {"count": 10, "first": "Kickoff: Lighthouse migration"},
            (client.stdout + client.stderr)[:500])
    s.config("sdk", {
        "connectors": {"fixture": {"type": "mcp-stdio", "command": [sdk_python, "-I", "tests/fixtures/sdk_server.py"]}},
        "policy": {"default": "allow"},
        "limits": dict(LIMITS, timeout_s=30, call_timeout_s=20, startup_timeout_s=30),
    })
    r = s.cli("run", "--json", "out = [tools.fixture.add(a=2, b=40), tools.fixture.shout_text(text='hi', times=2)]\ntry:\n    tools.fixture.explode()\nexcept ToolError as e:\n    out.append(e.kind)\nout + [h['path'] for h in search('shout')]", config="sdk")
    # The SDK publishes an output schema of {"result": <value>} for tools that
    # return a scalar, and sends that object as structured content. The
    # program gets what the published schema describes, not a guess at what
    # the author meant.
    s.check("P3 N1", "this runtime's client calls a server built with the official SDK, including a tool with a dash in its name and one that raises",
            r.code == 0 and r.get("result") == [{"result": 42}, {"result": "HI HI"}, "ToolError", "fixture.shout_text"], r.out[:500])
    r = s.cli("describe", "fixture.add", "--json", config="sdk")
    s.check("P4", "the value a program receives matches the output schema the server published",
            r.get("signature") == "fixture.add(a: int, b: int) -> dict" and "result" in r.get("outputSchema", "properties", default={}), r.out[:300])


def snippets(s: Suite) -> None:
    s.section("K. Snippets (P15)")
    r = s.cli("run", "--json", "base = args.get('base', 0)\nbase + tools.chaos.echo(value=5)", config="chaos")
    s.check("P15", "a program that reads args runs on its own, with args empty", r.get("result") == 5)
    r = s.cli("snippet", "save", "add_five", "--from", "last", "--description", "Add five to base.", "--json", config="chaos")
    s.check("P15", "the host promotes a completed execution to a snippet", r.code == 0 and r.get("name") == "add_five")
    r = s.cli("run", "--json", "[run('add_five', base=10), [h['path'] for h in search('five')]]", config="chaos")
    s.check("P15", "a later program finds it with search and calls it with run(), with its own input",
            r.code == 0 and r.get("result") == [15, ["snippet:add_five"]], r.out[:300])
    s.cli("run", "--json", "tools.gated.counter(name='snip')", config="chaos", quiet=True)
    r = s.cli("snippet", "save", "unfinished", "--from", "last", "--json", config="chaos")
    s.check("P15 P17", "an execution that has not completed cannot be promoted", r.code == 2 and r.get("error", "type") == "StateError")
    s.cli("reject", "last", config="chaos", quiet=True)
    r = s.cli("snippet", "save", "Bad Name", "--from", "last", "--json", config="chaos")
    s.check("I7", "an invalid snippet name is a usage error", r.code == 2)
    r = s.cli("run", "--json", "try:\n    run('nope')\nexcept ToolError as e:\n    out = e.kind\nout", config="chaos")
    s.check("P15", "running a snippet that does not exist raises ToolError", r.get("result") == "NotFound")


def agent_loop(s: Suite) -> None:
    s.section("L. The optional model loop against a scripted OpenAI-compatible endpoint (P24, P26)")

    def tool_call(arguments: str, call_id: str = "call_1") -> Dict[str, Any]:
        return {"status": 200, "body": {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "execute", "arguments": arguments}}]}}]}}

    def final(text: str) -> Dict[str, Any]:
        return {"status": 200, "body": {"choices": [{"message": {"role": "assistant", "content": text}}]}}

    def with_stub(name: str, script: List[Dict[str, Any]], *args: str, env: Optional[Dict[str, str]] = None, timeout: float = 90) -> Any:
        script_path = WORK / ("stub-%s.json" % name)
        log_path = WORK / ("stub-%s.log" % name)
        script_path.write_text(json.dumps(script), encoding="utf-8")
        stub = subprocess.Popen([PY, "tests/fixtures/stub_openai.py", str(script_path), str(log_path)], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
        try:
            port = stub.stdout.readline().strip()  # type: ignore[union-attr]
            s.log()
            s.log("(scripted endpoint '%s' on 127.0.0.1, %d canned response(s); CODEMODE_BASE_URL points at it)" % (name, len(script)))
            extra = {"CODEMODE_BASE_URL": "http://127.0.0.1:%s/v1" % port, "CODEMODE_MODEL": "stub-model"}
            extra.update(env or {})
            result = s.cli("agent", *args, config="chaos", home="agent", env=extra, timeout=timeout)
        finally:
            stub.kill()
        requests = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.is_file() else []
        return result, requests

    r, sent = with_stub("happy", [tool_call(json.dumps({"code": "len(tools.demo.list_notes())"})), final("There are 10 notes.")],
                        "How many notes are there?", "--json", env={"CODEMODE_API_KEY": "test-key"})
    events = [json.loads(line) for line in r.out.splitlines() if line.strip()]
    s.check("P24", "the loop runs a task to a final answer against an endpoint chosen by environment variables",
            r.code == 0 and events and events[-1] == {"event": "final", "turn": 2, "text": "There are 10 notes."}, r.out[:300] + r.err[:300])
    first = sent[0]["body"] if sent else {}
    s.check("P1 P24", "the model was offered exactly one function, execute, and the configured model name and key were sent",
            [t["function"]["name"] for t in first.get("tools", [])] == ["execute"] and first.get("model") == "stub-model"
            and sent[0]["path"] == "/v1/chat/completions" and sent[0]["authorization"] == "Bearer test-key")
    tool_messages = [m for m in (sent[1]["body"]["messages"] if len(sent) > 1 else []) if m.get("role") == "tool"]
    s.check("P2", "the tool result sent back to the model is the small payload, tied to the call id",
            tool_messages and tool_messages[0]["tool_call_id"] == "call_1" and json.loads(tool_messages[0]["content"]).get("result") == 10)

    r, sent = with_stub("malformed", [tool_call("{not json"), final("Recovered.")], "Do something", "--json")
    tool_messages = [m for m in (sent[1]["body"]["messages"] if len(sent) > 1 else []) if m.get("role") == "tool"]
    s.check("X16", "malformed tool arguments cost one turn, not the task: the model is told and the loop goes on",
            r.code == 0 and tool_messages and json.loads(tool_messages[0]["content"])["error"]["type"] == "BadArguments")
    s.check("P24", "no Authorization header is sent when no key is configured", sent and sent[0]["authorization"] is None)

    r, sent = with_stub("retry", [{"status": 429, "headers": {"Retry-After": "0"}, "body": {"error": "slow down"}}, final("After a retry.")], "Hi")
    s.check("X16", "a 429 with Retry-After is retried and the task completes", r.code == 0 and "After a retry." in r.out and len(sent) == 2)
    r, sent = with_stub("down", [{"status": 401, "body": {"error": "bad key"}}], "Hi", "--json")
    s.check("X16", "a 401 is not retried and ends as ModelEndpointError with the status in the message",
            r.code == 1 and r.get("error", "type") == "ModelEndpointError" and "401" in r.get("error", "message", default="") and len(sent) == 1)
    looping = [tool_call(json.dumps({"code": "1"}), "call_%d" % i) for i in range(5)]
    r, sent = with_stub("loop", looping, "Never finish", "--max-turns", "3")
    s.check("P26", "a model that never answers is stopped at max-turns (exit 1) instead of looping", r.code == 1 and "stopped after 3 turns" in r.out and len(sent) == 3)
    r = s.cli("agent", "Hi", "--json", config="chaos", home="agent")
    s.check("P24 I7", "with no endpoint configured the loop refuses to start (exit 2) and names the variables to set",
            r.code == 2 and "CODEMODE_BASE_URL" in r.get("error", "hint", default=""))


def entry_points(s: Suite) -> None:
    s.section("N. Entry points, command hints and odd files")
    r = s.raw([PY, "-I", "main.py", "--home", ".e2e/entry", "run", "--json", "1 + 1"])
    s.check("E1", "python -I main.py (isolated mode) imports the package and runs a program", r.code == 0 and r.get("result") == 2, r.err[:300])
    elsewhere = WORK / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)
    r = s.raw([PY, "-m", "codemode_runtime", "--home", str(WORK / "entry"), "run", "--json", "2 + 2"])
    s.check("E6", "python -m codemode_runtime is the same CLI", r.code == 0 and r.get("result") == 4, r.err[:300])
    r = s.raw([PY, "-I", str(ROOT / "codemode_runtime" / "__main__.py"), "--home", str(WORK / "entry"), "run", "--json", "3 + 3"], cwd=elsewhere)
    s.check("E6 E1", "the package's __main__.py runs as a script, in isolated mode, from another folder", r.code == 0 and r.get("result") == 6, r.err[:300])

    r = s.cli("run", "--json", "tools.gated.counter(name='hinted')", config="chaos", home="hint")
    hint = r.get("hint", default="")
    command = hint.split("'")[1] if hint.count("'") >= 2 else ""
    s.check("E2", "the hint of a paused execution carries the --config and --home it was run with",
            "--home" in command and "--config" in command and command.endswith("approve " + str(r.get("executionId"))), hint[:300])
    r = s.raw(shlex.split(command) + ["--json"], cwd=elsewhere) if command else Result(-1, "", "no command in the hint")
    s.check("E2", "pasted as printed, from another folder, the approve command finds the execution and completes it",
            r.code == 0 and r.get("result") == 1 and counter("hint", "hinted") == 1, r.out[:300] + r.err[:300])
    r = s.cli("get", "ex_nope", "--json", config="chaos", home="hint")
    s.check("E2 I7", "an unknown execution exits 5, and its hint is a command with the same --home",
            r.code == 5 and "--home" in r.get("error", "hint", default="") and "python main.py" not in r.get("error", "hint", default=""))

    r = s.cli("run", "--json", "40 + 2", config="chaos", home="hint")
    made = str(r.get("executionId"))
    for label, given in (("file name", made + ".jsonl"), ("path", ".e2e/hint/executions/" + made + ".jsonl"),
                         ("path with backslashes", ".e2e\\hint\\executions\\" + made + ".jsonl")):
        for command in ("get", "wait", "show", "verify"):
            r = s.cli(command, given, "--json", config="chaos", home="hint", quiet=True)
            names_it = made in r.out and r.get("error", "type") != "NotFound"
            if command == "show":
                names_it = r.code == 0 and isinstance(r.json, list) and r.json and r.json[0].get("id") == made
            s.check("E9", "%s accepts the log file's name or path as the id (%s)" % (command, label), names_it, r.out[:200])
    r = s.cli("get", "ex_nope.jsonl", "--json", config="chaos", home="hint")
    s.check("E9 I7", "a file name that matches nothing is still NotFound (exit 5), naming the id", r.code == 5 and "ex_nope" in r.get("error", "message", default=""), r.out[:200])
    r = s.cli("get", made + ".lock", "--json", config="chaos", home="hint", quiet=True)
    s.check("E9", "only the log's own name is read this way; another file name is not an id", r.code == 5, r.out[:200])

    georgian = s.program("georgian", '''
print("log: \\u10d0\\u10d1\\u10d2")
{"text": "\\u10d0\\u10d1\\u10d2", "n": 1}
''')
    legacy = {"PYTHONIOENCODING": "cp1252"}
    ka = "აბგ"
    r = s.cli("run", "-f", georgian, config="chaos", home="enc", env=legacy)
    s.check("E10", "a result and a log with text the code page cannot hold print as text (exit 0), as escapes",
            r.code == 0 and "Traceback" not in r.err and "\\u10d0" in r.out and "completed" in r.out, r.out[:200] + r.err[:300])
    r = s.cli("run", "-f", georgian, "--json", config="chaos", home="enc", env=legacy)
    s.check("E10", "with --json", r.code == 0 and r.get("result", "text") == ka and "Traceback" not in r.err, r.out[:200] + r.err[:300])
    r = s.cli("get", "last", "--select", "result.text", config="chaos", home="enc", env=legacy)
    s.check("E10", "--select printing a text value", r.code == 0 and r.out.strip() == "\\u10d0\\u10d1\\u10d2" and "Traceback" not in r.err, r.out[:200] + r.err[:300])
    r = s.cli("get", "last", "--select", "logs", config="chaos", home="enc", env=legacy)
    s.check("E10", "--select printing the output of the program", r.code == 0 and "\\u10d0" in r.out and "Traceback" not in r.err, r.out[:200] + r.err[:300])
    r = s.cli("get", ka, config="chaos", home="enc", env=legacy)
    s.check("E10", "an error and its hint on stderr, with a name the code page cannot hold (exit 5)",
            r.code == 5 and "no execution" in r.err and "Traceback" not in r.err, r.err[:300])
    r = s.cli("run", "--arg", "name=" + ka, "-f", s.program("echo_arg", 'args["name"]'), config="chaos", home="enc", env=legacy)
    s.check("E10", "an argument with such text, back in the result", r.code == 0 and "\\u10d0" in r.out and "Traceback" not in r.err, r.out[:200] + r.err[:300])
    for command in (("executions",), ("show", "last"), ("search", ka), ("verify", "last")):
        r = s.cli(*command, config="chaos", home="enc", env=legacy, quiet=True)
        s.check("E10", "%s does not fail on it" % command[0], "Traceback" not in r.err and r.code in (0, 5), r.err[:300])

    own = {"CODEMODE_COMMAND": "mytool code"}
    r = s.cli("run", "--json", "tools.gated.counter(name='named')", config="chaos", home="hint", env=own)
    hint = r.get("hint", default="")
    s.check("E8", "with CODEMODE_COMMAND set, the hint of a pause is the project's own command and nothing is added to it",
            r.code == 3 and "'mytool code approve %s'" % r.get("executionId") in hint and "main.py" not in hint and "--home" not in hint, hint[:300])
    r = s.cli("get", "ex_nope", "--json", config="chaos", home="hint", env={"CODEMODE_COMMAND": "runner codemode {options}"})
    hint = r.get("error", "hint", default="")
    s.check("E8", "{options} in it is where --config and --home go",
            r.code == 5 and "runner codemode --config " in hint and " --home " in hint and hint.endswith(" executions") and "main.py" not in hint, hint[:300])
    r = s.cli("instructions", config="chaos", home="hint", env=own)
    usage = s.cli("--help", home=None, env=own)
    s.check("E8", "the instructions for agents and --help name it too",
            r.code == 0 and "mytool code run --json -f" in r.out and "main.py" not in r.out and usage.out.startswith("usage: mytool code "), usage.out[:80])
    r = s.cli("get", "ex_nope", "--json", config="chaos", home="hint", env={"CODEMODE_COMMAND": "   "})
    s.check("E8", "a blank value changes nothing", r.code == 5 and "main.py" in r.get("error", "hint", default=""), r.out[:300])

    plain = json.dumps(chaos_config())
    for name, data in (("bom8", b"\xef\xbb\xbf" + plain.encode("utf-8")), ("bom16", plain.encode("utf-16"))):
        (WORK / (name + ".json")).write_bytes(data)
        r = s.cli("config", "--json", config=name)
        s.check("E3", "a config file with a %s byte-order mark loads" % ("UTF-8" if name == "bom8" else "UTF-16"),
                r.code == 0 and "chaos" in r.get("connectors", default={}), r.out[:200] + r.err[:200])
    crlf = WORK / "programs" / "crlf.py"
    crlf.parent.mkdir(parents=True, exist_ok=True)
    crlf.write_bytes(b"\xef\xbb\xbftotal = 0\r\nfor n in (1, 2, 3):\r\n    total += n\r\ntotal\r\n")
    r = s.cli("run", "-f", ".e2e/programs/crlf.py", "--json", config="chaos")
    s.check("E3", "a program file with a BOM and CRLF line endings runs", r.code == 0 and r.get("result") == 6, r.out[:300])
    r = s.cli("run", "-f", ".e2e/programs/does-not-exist.py", "--json", config="chaos")
    s.check("E3 I7", "a program file that does not exist is a usage error with a hint (exit 2)",
            r.code == 2 and r.get("error", "type") == "UsageError" and r.get("error", "hint", default=""))

    started = time.monotonic()
    silent = subprocess.Popen([PY, "main.py", "--home", ".e2e/entry", "run", "--json"], cwd=str(ROOT), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=s.env())
    try:
        # Not communicate(): that would close stdin, and an open, silent
        # stdin is the case. The read ends when the process gives up.
        out = silent.stdout.read()  # type: ignore[union-attr]
        code = silent.wait(timeout=30)
    finally:
        silent.stdin.close()  # type: ignore[union-attr]
    s.log()
    s.log("$ python main.py --home .e2e/entry run --json    (stdin: a pipe left open with nothing on it)")
    s.log("[exit %d]\n%s" % (code, _clip(out)))
    s.check("E4", "run with no program and a silent, open stdin fails with a usage error in bounded time instead of hanging",
            code == 2 and "stdin" in out and time.monotonic() - started < 20, "%.1fs %s" % (time.monotonic() - started, out[:200]))
    r = s.cli("--json", "frobnicate", home=None)
    s.check("E5", "a usage error with --json is one JSON object on stdout", r.code == 2 and r.get("error", "type") == "UsageError", r.out[:200])
    r = s.cli("--version", home=None)
    s.check("E6", "--version prints the version", r.code == 0 and r.out.startswith("codemode-runtime "))

    if not os.environ.get("CODEMODE_E2E_INSTALL"):
        s.skip("installing the package into a virtual environment (set CODEMODE_E2E_INSTALL=1; needs the network for the build backend)")
        return
    venv = WORK / "venv"
    made = subprocess.run([PY, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=300)
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    s.mask(str(venv), "<venv>")
    pip = s.raw([str(bin_dir / "python"), "-m", "pip", "install", "--quiet", "--no-deps", "--disable-pip-version-check", str(ROOT)], timeout=600)
    s.check("E6", "pip installs the project into a fresh virtual environment", made.returncode == 0 and pip.code == 0, (pip.out + pip.err)[-400:])
    r = s.raw([str(bin_dir / "codemode"), "--home", str(WORK / "installed"), "run", "--json", "tools.demo.create_note(title='t', body='b')"], cwd=elsewhere)
    s.check("E6", "the installed codemode entry point runs a program from any folder, and pauses on a write",
            r.code == 3 and r.get("status") == "paused", r.out[:300] + r.err[:300])
    s.check("E2 E6", "its hint names the codemode command, not an interpreter and a script", "'codemode --home" in r.get("hint", default=""), r.get("hint", default="")[:200])
    listed = s.raw([str(bin_dir / "python"), "-m", "pip", "show", "-f", "codemode-runtime"])
    s.check("E6", "the installed package includes the sandbox runner source", "sandbox_runner.py" in listed.out)


def _alive(pid: int) -> bool:
    """Is a process with this id running? Written out here so the suite does
    not lean on the code under test."""
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    try:
        # Exited but not yet collected by a parent: the process is gone, and
        # only its entry is left. (Linux; elsewhere there is no such file.)
        with open("/proc/%d/stat" % int(pid), "rb") as handle:
            return handle.read().rpartition(b")")[2].split()[:1] != [b"Z"]
    except OSError:
        return True


def _command_line(pid: Any) -> str:
    """The command line of the process with this id; empty when there is no
    such process or it cannot be read. For a check that looks at an id some
    time after it wrote it down: the system gives an id out again, and the
    process under it may then be somebody else's."""
    try:
        if os.name == "nt":
            ask = "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % int(pid)
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ask],
                                 capture_output=True, text=True, timeout=60, creationflags=0x08000000)  # no window
            return out.stdout.strip()
        try:
            with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
                return handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "args="], capture_output=True, text=True, timeout=30)
            return out.stdout.strip()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def program_api(s: Suite) -> None:
    s.section("P. What a program is given: arguments, helpers, honest listings, variables kept on failure")
    r = s.cli("run", "--json", "found = search('', 5)\n[len(found), found.total, found.more, len(search(''))]", config="chaos", home="api")
    total = r.get("result", 1, default=0)
    s.check("H1", "search() carries the total: 5 shown, the rest counted, and an empty query lists everything by default",
            r.code == 0 and total > 10 and r.get("result") == [5, total, total - 5, total], r.out[:300])
    s.check("H1", "the cap is visible to a caller who reads only the outcome: a note in the logs says how many were left out",
            "showing 5 of %d matches" % total in r.get("logs", default=""), r.get("logs", default="")[:200])
    r = s.cli("search", "--limit", "3", "--json", config="chaos", home="api")
    s.check("H1", "the search command says so too, on stderr, and its JSON stays a plain list",
            isinstance(r.json, list) and len(r.json) == 3 and "showing 3 of %d" % total in r.err, r.err[:200])
    listing = s.program("one_namespace", '''
offered = search(namespace="demo")
matching = search("counter", namespace="gated")
everywhere = search("counter", limit=50)
try:
    search(namespace="nope")
    unknown = "no error"
except ToolError as error:
    unknown = [error.kind, error.message]
[[hit["path"] for hit in offered], offered.total, offered.more, {hit["path"]: hit["policy"] for hit in offered},
 sorted(hit["path"] for hit in matching), sorted(hit["path"] for hit in everywhere), unknown]
''')
    r = s.cli("run", "-f", listing, "--json", config="chaos", home="api")
    result = r.get("result", default=[None] * 7)
    notes = ["demo.list_notes", "demo.read_note", "demo.search_notes", "demo.create_note", "demo.update_note", "demo.delete_note"]
    s.check("H13", "search(namespace=...) with no query lists everything one server offers, in its own order, with the total and each policy",
            r.code == 0 and result[0] == notes and result[1:3] == [6, 0] and (result[3] or {}).get("demo.read_note") == "allow"
            and (result[3] or {}).get("demo.create_note") == "ask", r.out[:500])
    s.check("H13", "with a query it finds only that namespace's tools, where the same query without one finds them in every namespace",
            "gated.counter" in (result[4] or []) and all(path.startswith("gated.") for path in result[4] or [])
            and {"gated.counter", "chaos.counter"} <= set(result[5] or []), json.dumps(result[4:6])[:300])
    s.check("H13", "a namespace that does not exist is an error that names the ones that do",
            isinstance(result[6], list) and result[6][0] == "UnknownTool" and "demo" in result[6][1] and "gated" in result[6][1], json.dumps(result[6])[:300])
    log = s.cli("show", "last", "--json", config="chaos", home="api", quiet=True)
    searches = [entry for entry in (log.json or []) if entry.get("kind") == "search"]
    s.check("H13 I3", "a search with no namespace is recorded as it always was, so an execution from before the parameter still replays",
            len(searches) == 4 and searches[2].get("args") == {"limit": 50} and searches[0].get("args") == {"limit": 50, "namespace": "demo"}, json.dumps([e.get("args") for e in searches]))
    r = s.cli("search", "--namespace", "demo", "--json", config="chaos", home="api")
    s.check("H13", "the search command takes --namespace too, for a person or a script that wants one server's tools",
            r.code == 0 and [hit["path"] for hit in (r.json or [])] == notes and sorted((r.json or [{}])[0]) == ["description", "path", "policy", "signature"], r.out[:300])
    r = s.cli("search", "--namespace", "nope", "--json", config="chaos", home="api")
    s.check("H13 I7", "and an unknown one exits 5", r.code == 5 and r.get("error", "type") == "NotFound" and "demo" in r.get("error", "message", default=""), r.out[:300])

    one_option = s.program("one_option", '''
pair = [("chaos.echo", {"value": 1}), ("chaos.fail", {})]

def kinds(values):
    return [value.kind if isinstance(value, ToolError) else value for value in values]

new = kinds(parallel(pair, on_error="return"))
old = kinds(parallel(pair, return_errors=True))
try:
    parallel(pair, on_error="raise")
    raised = "no error"
except ToolError as error:
    raised = error.kind
refused = []
for options in ({"on_error": "retrun"}, {"on_error": "raise", "return_errors": True}):
    try:
        parallel(pair, **options)
        refused.append("no error")
    except ValueError as error:
        refused.append(str(error))
[new, old, raised, refused]
''')
    r = s.cli("run", "-f", one_option, "--json", config="chaos", home="api")
    result = r.get("result", default=[None, None, None, ["", ""]])
    s.check("H14", "parallel() takes on_error like call(): 'return' gives a failed call back as a ToolError in its place, 'raise' raises, and the older return_errors=True still works",
            r.code == 0 and result[0] == result[1] == [1, "ChaosFailure"] and result[2] == "ChaosFailure", r.out[:400])
    s.check("H14", "a misspelt mode, and the two spellings contradicting each other, are refused before any call is made",
            "must be 'raise' or 'return'" in result[3][0] and "contradict" in result[3][1] and r.get("stats", "toolCalls") == 6, json.dumps(result[3])[:300])

    files = s.program("project_file", '''
import os
try:
    open("README.md")
    relative = "opened"
except FileNotFoundError:
    relative = "not found"
with open(project_root + "/README.md", encoding="utf-8") as handle:
    first = handle.readline().strip()
[relative, first, os.path.isabs(project_root), "\\\\" in project_root, os.path.samefile(os.getcwd(), project_root)]
''')
    r = s.cli("run", "-f", files, "--json", config="chaos", home="api")
    s.check("H15", "a program runs in an empty folder of its own and reaches a file of the project through project_root, an absolute path with forward slashes",
            r.code == 0 and r.get("result") == ["not found", "# codemode-runtime", True, False, False], r.out[:300])
    r = s.cli("run", "--json", "open('./README.md').read()", config="chaos", home="api")
    s.check("H15 I7", "a relative path that is not there fails with what happened and what to write instead",
            r.code == 1 and r.get("error", "type") == "FileNotFoundError" and "empty folder" in r.get("error", "hint", default="")
            and "open(project_root + '/README.md')" in r.get("error", "hint", default=""), r.out[:400])
    r = s.cli("run", "--json", "tools.gated.counter(name='rooted')\nproject_root", config="chaos", home="api")
    hint = r.get("hint", default="")
    command = hint.split("'")[1] if hint.count("'") >= 2 else ""
    other_folder = WORK / "elsewhere"
    other_folder.mkdir(parents=True, exist_ok=True)
    r = s.raw(shlex.split(command) + ["--json"], cwd=other_folder) if command else Result(-1, "", "no command in the hint")
    s.check("H15 I3", "a program that paused and is resumed from another folder is told the project folder it started with",
            r.code == 0 and os.path.normcase(str(r.get("result")).replace("/", os.sep)) == os.path.normcase(str(ROOT)), r.out[:300])

    r = s.cli("run", "--json", "info = describe('demo.read_note')\ntry:\n    info[:40]\nexcept TypeError as error:\n    sliced = str(error)\n[str(info).splitlines()[0], info['signature'], sliced]", config="chaos", home="api")
    s.check("H2", "describe() is a dict that prints as short text, and slicing it says what to do instead",
            r.code == 0 and r.get("result", 0) == r.get("result", 1) == "demo.read_note(id: int) -> dict" and "str(...)" in r.get("result", 2, default=""), r.out[:400])

    flaky = s.program("flaky", '''
first = repeat("chaos.flaky", {"name": "rep", "every": 4}, n=10)
stopped = repeat("chaos.flaky", {"name": "rep2", "every": 3}, n=10, until_fail=True)
judged = repeat("chaos.echo", {"value": 3}, n=4, ok=lambda v: v > 5, keep=1)
{"first": first, "stopped": stopped, "judged": judged}
''')
    r = s.cli("run", "-f", flaky, "--json", config="chaos", home="api")
    first, stopped, judged = (r.get("result", key, default={}) for key in ("first", "stopped", "judged"))
    s.check("H5", "repeat() runs a call n times and returns only the failures, with the run numbers",
            r.code == 0 and (first.get("runs"), first.get("passed"), first.get("failed")) == (10, 8, 2)
            and [f.get("run") for f in first.get("failures", [])] == [4, 8] and first["failures"][0].get("error") == "ChaosFailure", r.out[:500])
    s.check("H5", "until_fail stops at the first failure and says it stopped early",
            (stopped.get("runs"), stopped.get("failed"), stopped.get("stopped_early")) == (3, 1, True) and counter("api", "rep2") == 3, json.dumps(stopped)[:300])
    s.check("H5", "a caller's check decides what failed, and keep bounds what is returned",
            (judged.get("failed"), len(judged.get("failures", [])), judged.get("failures_omitted")) == (4, 1, 3), json.dumps(judged)[:300])
    r = s.cli("verify", "last", "--json", config="chaos", home="api")
    s.check("H5 I3", "every run of repeat() is a recorded call: the execution replays to the same result", r.code == 0 and r.get("verified") is True, r.out[:300])
    r = s.cli("run", "--json", "failed = call('chaos.fail', {'message': 'boom'}, on_error='return')\nworked = call('chaos.echo', {'value': 7}, on_error='return')\n[isinstance(failed, ToolError), failed.kind, failed.message, worked]", config="chaos", home="api")
    s.check("H6", "call(..., on_error='return') hands a failure back as a value and the program carries on",
            r.code == 0 and r.get("result") == [True, "ChaosFailure", "boom", 7], r.out[:300])

    r = s.cli("run", "--json", "--args-json", '{"runs": 1, "app": "from-json"}', "--arg", "runs=3", "--arg", "app=web", "--arg", 'tags=["a", "b"]',
              "--arg", "expr=a=b", "--arg", "flag=true", "--arg", "zip=007", "args", config="chaos", home="api")
    s.check("H7", "--arg values are JSON when they parse and text when they do not; they override --args-json",
            r.code == 0 and r.get("result") == {"runs": 3, "app": "web", "tags": ["a", "b"], "expr": "a=b", "flag": True, "zip": "007"}, r.out[:300])
    r = s.cli("run", "--json", "--arg", "n=1", "--arg-json", "n=5", "--arg-text", "t=1", "--arg-json", 'j="web"', "--arg-text", "m=3", "--arg", "m=3",
              "--arg", "l=[1,2]", "--arg", 'o={"a":1}', "--arg-json", "z=null", "--arg-json", "f=true",
              "[args, {k: type(args[k]).__name__ for k in args}]", config="chaos", home="api")
    s.check("H17", "--arg-text always gives text, --arg-json always JSON, and the latest of the three wins for a name; lists, objects and null are what they were",
            r.code == 0 and r.get("result") == [{"n": 5, "t": "1", "j": "web", "m": 3, "l": [1, 2], "o": {"a": 1}, "z": None, "f": True},
                                                {"n": "int", "t": "str", "j": "str", "m": "ArgInt", "l": "list", "o": "dict", "z": "NoneType", "f": "bool"}]
            and "warnings" not in (r.json or {}), r.out[:400])
    r = s.cli("run", "--json", "--arg-json", "n=forty", "1", config="chaos", home="api")
    s.check("H17 I7", "--arg-json with a value that is not JSON is a usage error (exit 2) whose hint shows how to write it",
            r.code == 2 and r.get("error", "type") == "UsageError" and "--arg-json n=40" in r.get("error", "hint", default="")
            and "--arg-text n=" in r.get("error", "hint", default=""), r.out[:400])
    typed = s.program("typed_args", '''
echoed = tools.chaos.echo(value=[args["n"], args["f"], args["v"]])
state["kept"] = args["f"]
[args["n"] * 2, args["n"] + 1, list(range(args["n"])), args["n"] in {1, 3}, [x for x in args["l"]], "yes" if args["f"] else "no",
 args["f"] == False, args["v"] * 2, sorted([args["n"], 1]), {args["n"]: "key"}[3], echoed, args["f"],
 args["f"] is False, isinstance(args["f"], bool), shape(args["f"]), args["f"] == "false"]
''')
    expected = [6, 4, [0, 1, 2], True, [1, 2], "no", True, 2.2, [1, 3], "key", [3, False, 1.1], False, True, True, "bool", False]
    r = s.cli("run", "--session", "typed_args", "-f", typed, "--arg", "n=3", "--arg", "l=[1,2]", "--arg", "f=false", "--arg", "v=1.10", "--json", config="chaos", home="api")
    warnings = r.get("warnings", default=[])
    s.check("H17", "a program written for typed values is unchanged: numbers count, compare and index as numbers, false is the real False, a tool gets them as they are",
            r.code == 0 and r.get("result") == expected and r.get("result", 11) is False and r.get("result", 10, 1) is False, r.out[:400])
    s.check("H17", "false from --arg comes with one note: what was typed, that it was read as the boolean, and the two ways to say which is meant",
            len(warnings) == 1 and "--arg f=false" in warnings[0] and "the boolean False" in warnings[0] and "args['f'] == 'false'" in warnings[0]
            and "--arg-text f=false" in warnings[0] and "--arg-json f=false" in warnings[0], json.dumps(warnings)[:600])
    r = s.cli("run", "--session", "typed_args", "--json", "state['kept']", config="chaos", home="api")
    s.check("H17", "and false given with --arg is stored in state as false", r.code == 0 and r.get("result") is False, r.out[:200])
    r = s.cli("run", "--session", "typed_args2", "-f", typed, "--arg", "n=3", "--arg", "l=[1,2]", "--arg-json", "f=false", "--arg", "v=1.10", "--json",
              config="chaos", home="api")
    s.check("H17", "with --arg-json the same false comes without the note", r.code == 0 and r.get("result") == expected and "warnings" not in (r.json or {}), r.out[:400])
    r = s.cli("run", "--json", "--arg", "z=null", "[args['z'] is None, args['z'] == 'null']", config="chaos", home="api")
    s.check("H17", "null from --arg is None, with the same kind of note", r.code == 0 and r.get("result") == [True, False]
            and any("nothing (None)" in w and "--arg-text z=null" in w for w in r.get("warnings", default=[])), r.out[:400])
    texty = s.program("text_args", '''
[args["dry"] == "1", args["dry"] in ("1", "yes"), f"v{args['v']}", str(args["v"]), args["tag"].startswith("20"), len(args["tag"]),
 "release-" + args["tag"], args["v"].split("."), args["tag"][:2], repr(args["v"])]
''')
    r = s.cli("run", "-f", texty, "--arg", "dry=1", "--arg", "v=1.10", "--arg", "tag=2024", "--json", config="chaos", home="api")
    warnings = r.get("warnings", default=[])
    s.check("H17", "a program written for text gets the text as typed: == with text, an f-string, len, slicing, + with text and the methods of text",
            r.code == 0 and r.get("result") == [True, True, "v1.10", "1.10", True, 4, "release-2024", ["1", "10"], "20", "1.1"], r.out[:400])
    s.check("H17", "one warning for each name whose text was used, saying what was given, how it was read, and that --arg-text gives text",
            len(warnings) == 3 and all(any("args[%r]" % name in w and "--arg-text %s=" % name in w for w in warnings) for name in ("dry", "v", "tag"))
            and "--arg dry=1" in " ".join(warnings) and "the number 1" in " ".join(warnings), json.dumps(warnings)[:800])
    r = s.cli("run", "--json", "--arg", "l=[1,2]", "import json\njson.loads(args['l'])", config="chaos", home="api")
    s.check("H17 I7", "json.loads on a value --arg already read fails with a hint: use it as it is, or pass --arg-text",
            r.code == 1 and r.get("error", "type") == "TypeError" and "already read" in r.get("error", "hint", default="")
            and "--arg-text l=" in r.get("error", "hint", default=""), r.out[:400])
    r = s.cli("run", "--json", "--arg", "n=4", "args['n'] + None", config="chaos", home="api")
    s.check("H17", "another failure gets no such hint", r.code == 1 and "--arg-text" not in r.get("error", "hint", default=""), r.out[:300])
    r = s.cli("run", "--json", "--arg", "name=argt", "--arg", "k=2", "--arg-text", "t=2",
              "x = tools.gated.counter(name=args['name'])\n[x, args['k'] + 1, args['k'] == '2', str(args['k']), args['t'], type(args['k']).__name__]",
              config="chaos", home="api")
    paused = r.get("status") == "paused"
    r = s.cli("approve", "last", "--json", config="chaos", home="api")
    s.check("H17 I9", "after a pause and an approval the program gets the same values: a number that remembers its text, and text",
            paused and r.code == 0 and r.get("result") == [1, 3, True, "2", "2", "ArgInt"] and "args['k']" in " ".join(r.get("warnings", default=[])), r.out[:400])
    r = s.cli("verify", "last", "--json", config="chaos", home="api")
    s.check("H17 I3", "and the execution replays to the same result", r.code == 0 and r.get("verified") is True, r.out[:300])
    log = s.cli("show", "last", "--json", config="chaos", home="api", quiet=True)
    start = next((entry for entry in (log.json or []) if entry.get("type") == "start"), {})
    s.check("H17", "the start entry keeps args typed, and the text of a number given with --arg in args_text",
            start.get("args") == {"name": "argt", "k": 2, "t": "2"} and start.get("args_text") == {"k": "2"}, json.dumps(start)[:400])
    r = s.cli("run", "--json", "--arg", "novalue", "1", config="chaos", home="api")
    s.check("H7 I7", "an --arg with no = is a usage error (exit 2)", r.code == 2 and r.get("error", "type") == "UsageError")
    r = s.cli("run", "--json", "--args-json", "[1, 2]", "1", config="chaos", home="api")
    s.check("H7 I7", "--args-json that is not an object is a usage error (exit 2)", r.code == 2 and "object" in r.get("error", "message", default=""))
    r = s.cli("run", "--json", "--arg", "name=argp", "tools.gated.counter(name=args['name'])", config="chaos", home="api")
    r = s.cli("approve", "last", "--json", config="chaos", home="api")
    s.check("H7 I9", "a paused execution resumes with the arguments it was started with", r.code == 0 and r.get("result") == 1 and counter("api", "argp") == 1, r.out[:300])
    r = s.cli("run", "--arg", "n=4", "--select", "result.squares.2", "{'squares': [i * i for i in range(args['n'])]}", config="chaos", home="api")
    s.check("H12", "--select prints one part of the result and nothing else", r.code == 0 and r.out.strip() == "4", r.out[:200])
    s.cli("run", "--json", "{'a,b': 1, 'a': 2, 'b': 3}", config="chaos", home="api")
    r = s.cli("get", "last", "--select", "result.a,b", "--json", config="chaos", home="api")
    s.check("H12 R13", "a path whose key holds a comma still names that key, as it did before a comma could separate paths",
            r.code == 0 and r.get("value") == 1 and r.get("select") == "result.a,b", r.out[:200])

    lost = s.program("lost", '''
results = {"build": {"ok": True, "log": "MARKER-KEPT " * 30}, "tests": {"ok": True, "failed": []}}
count = len(results)
handle = object()
tools.chaos.fail(message="the last call failed")
results
''')
    r = s.cli("run", "-f", lost, "--json", config="chaos", home="api")
    s.check("H8", "a program that fails after its work reports the shapes of its variables, not their content",
            r.code == 1 and r.get("error", "type") == "ChaosFailure" and r.get("variables", "count") == "int"
            and r.get("variables", "results", "build", "log") == "str[360]" and "handle" not in r.get("variables", default={})
            and "MARKER-KEPT" not in r.out, r.out[:500])
    s.check("H8 H10", "the error says how to keep the work next time, and how to read a variable now",
            "on_error" in r.get("error", "hint", default="") and "--select variables." in r.get("hint", default=""), r.out[-400:])
    r = s.cli("get", "last", "--select", "variables.results.tests", "--json", config="chaos", home="api")
    s.check("H8 H12", "get --select returns one variable of the failed program", r.code == 1 and r.get("value") == {"ok": True, "failed": []}, r.out[:300])
    r = s.cli("get", "last", "--select", "variables.nothing", "--json", config="chaos", home="api")
    s.check("H12 I7", "a path that does not exist exits 5 and lists what is there",
            r.code == 5 and "count" in r.get("error", "hint", default="") and "results" in r.get("error", "hint", default=""), r.out[:300])
    r = s.cli("get", "last", "--select", "result", "--json", config="chaos", home="api")
    s.check("H12", "selecting the result of a failed execution returns the failure, not an empty value", r.code == 1 and r.get("error", "type") == "ChaosFailure")
    r = s.cli("run", "--json", "{'rows': [{'id': i, 'text': 'x' * 50} for i in range(100)], 'n': 100}", config="chaos", home="api")
    s.check("T3 H12", "a result over the budget comes with its shape and a command that reads a part of it",
            r.get("resultTruncated") is True and r.get("resultShape", "rows", "list") == 100 and "--select result." in r.get("hint", default=""), r.out[-500:])
    r = s.cli("get", "last", "--select", "result.rows.99.id", config="chaos", home="api")
    s.check("H12", "the part can then be read: a list index works as a path element", r.code == 0 and r.out.strip() == "99")
    r = s.cli("get", "last", "--select", "result.rows", "--json", config="chaos", home="api")
    s.check("H12 T3", "a selected value over the budget is cut and flagged, with its shape", r.get("valueTruncated") is True and r.get("valueShape", "list") == 100)

    r = s.cli("run", "--json", "for i in range(2):\n    {'answer': i}", config="chaos", home="api")
    s.check("H9", "a value inside a trailing loop is not the result, and the null result comes with a warning that says why",
            r.code == 0 and r.get("result") is None and "loop" in " ".join(r.get("warnings", default=[])), r.out[:300])
    r = s.cli("run", "--json", "ok = True\nif ok:\n    {'answer': 42}", config="chaos", home="api")
    s.check("H19", "a value at the end of a trailing if-block is the result, with no warning",
            r.code == 0 and r.get("result") == {"answer": 42} and "warnings" not in (r.json or {}), r.out[:300])
    branches = s.program("branches", '''
first = tools.chaos.echo(value=0)
try:
    first / 0
except ZeroDivisionError:
    if first:
        "not this"
    else:
        with open(project_root + "/README.md", encoding="utf-8") as handle:
            {"branch": "handler", "first": handle.readline().strip()}
''')
    r = s.cli("run", "-f", branches, "--json", config="chaos", home="api")
    s.check("H19", "after a trailing try the result is the value of the branch that ran, through an if and a with nested in it",
            r.code == 0 and r.get("result") == {"branch": "handler", "first": "# codemode-runtime"} and "warnings" not in (r.json or {}), r.out[:300])
    r = s.cli("run", "-f", branches, "--check", "--json", config="chaos", home="api")
    s.check("H19", "run --check accepts it and gives no warning about a lost value", r.code == 0 and "warnings" not in (r.json or {}), r.out[:300])
    returns = s.program("returns", '''
ran = []
for n in range(10):
    try:
        if n == 3:
            return {"stopped_at": n, "ran": ran}
    finally:
        ran.append(n)
"never reached"
''')
    r = s.cli("run", "-f", returns, "--json", config="chaos", home="api")
    s.check("H18", "return at the top level, inside a loop, ends the program with its value after the finally blocks ran",
            r.code == 0 and r.get("result") == {"stopped_at": 3, "ran": [0, 1, 2, 3]}, r.out[:300])
    r = s.cli("run", "--json", "try:\n    return 'early'\nexcept Exception:\n    pass\n'late'", config="chaos", home="api")
    s.check("H18", "except Exception does not stop a return", r.code == 0 and r.get("result") == "early", r.out[:300])
    r = s.cli("run", "--json", "import contextlib\nwith contextlib.suppress(Exception):\n    return 'kept'\n'lost'", config="chaos", home="api")
    s.check("H18", "nor does a with block that suppresses every Exception", r.code == 0 and r.get("result") == "kept", r.out[:300])
    r = s.cli("run", "--json", "try:\n    return 5\nexcept:\n    return None", config="chaos", home="api")
    caught = r.get("result")
    r = s.cli("run", "--json", "try:\n    try:\n        return 'inner'\n    except BaseException:\n        'swallowed'\nexcept:\n    'outer'", config="chaos", home="api")
    s.check("H18", "neither a bare except nor except BaseException sees a return",
            caught == 5 and r.code == 0 and r.get("result") == "inner", "%r %s" % (caught, r.out[:300]))
    r = s.cli("run", "-f", returns, "--check", "--json", config="chaos", home="api")
    s.check("H18", "run --check accepts a program with a top-level return", r.code == 0 and r.get("status") == "completed", r.out[:300])
    r = s.cli("run", "--json", "def f():\n    return 'f'\nx = 1\nif x:\n    return\n'not this'", config="chaos", home="api")
    s.check("H18", "a bare return gives null", r.code == 0 and r.get("result") is None and r.get("status") == "completed", r.out[:300])
    r = s.cli("run", "--json", "kept = 'mine'\nif kept:\n    tools.chaos.fail(message='after the work')\nreturn kept", config="chaos", home="api")
    s.check("H18 H8", "the names the runtime adds for return and the result are not among the variables of a failed program",
            r.code == 1 and r.get("variables", default={}) == {"kept": "str[4]"}, r.out[:400])
    paused = s.program("paused_return", '''
count = tools.gated.counter(name="ret19")
if count > 100:
    return "too many"
if count:
    {"count": count, "arg": args["k"]}
''')
    r = s.cli("run", "-f", paused, "--arg", "k=v", "--json", config="chaos", home="api")
    was_paused = r.get("status") == "paused"
    r = s.cli("approve", "last", "--json", config="chaos", home="api")
    s.check("H18 H19 I9", "a program that paused and is resumed gives the value of its trailing block",
            was_paused and r.code == 0 and r.get("result") == {"count": 1, "arg": "v"}, r.out[:300])
    r = s.cli("verify", "last", "--json", config="chaos", home="api")
    s.check("H19 I3", "and its replay gives the same result", r.code == 0 and r.get("verified") is True, r.out[:300])
    r = s.cli("run", "--json", "for i in range(2):\n    print(i)", config="chaos", home="api")
    s.check("H9", "a program that ends in a loop of calls gets no such warning", r.code == 0 and "warnings" not in (r.json or {}))
    broken = s.program("broken", "message = 'first line\nsecond line'\nmessage\n")
    r = s.cli("run", "-f", broken, "--json", config="chaos", home="api")
    s.check("H10", "a string broken by a real line break is a SyntaxError whose hint names the shell and the fix",
            r.code == 1 and r.get("error", "type") == "SyntaxError" and "shell" in r.get("error", "hint", default="") and "-f" in r.get("error", "hint", default=""), r.out[:300])
    winpath = s.program("winpath", "path = 'C:\\Users\\name\\notes.txt'\npath\n")
    r = s.cli("run", "-f", winpath, "--json", config="chaos", home="api")
    s.check("H10", "a Windows path that starts a \\U escape gets a hint about forward slashes and raw strings",
            r.code == 1 and "raw string" in r.get("error", "hint", default=""), r.out[:300])
    r = s.cli("run", "--json", "row = tools.demo.read_note(id=1)\nrow['content']", config="chaos", home="api")
    s.check("H10", "a KeyError on a tool result points at shape() and describe()",
            r.code == 1 and r.get("error", "type") == "KeyError" and "shape(" in r.get("error", "hint", default=""))

    pidfile = WORK / "orphan.pid"
    orphan = s.program("orphan", '''
import os, time
with open(args["pidfile"], "w") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(0.05)
''')
    host = subprocess.Popen([PY, "main.py", "--home", ".e2e/api", "--config", ".e2e/chaos.json", "run", "-f", orphan, "--arg", "pidfile=" + str(pidfile).replace("\\", "/")],
                            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=s.env())
    sandbox_pid = 0
    for _ in range(100):
        time.sleep(0.05)
        if pidfile.is_file() and pidfile.read_text().strip():
            sandbox_pid = int(pidfile.read_text())
            break
    was_alive = bool(sandbox_pid) and _alive(sandbox_pid)
    host.kill()  # the hard way: no chance to clean up
    host.wait(timeout=10)
    gone = False
    for _ in range(100):
        if not _alive(sandbox_pid):
            gone = True
            break
        time.sleep(0.05)
    s.log()
    s.log("(started a program that loops without making a request, then killed the host process outright)")
    s.check("H11", "when the host process is killed, the sandbox process exits by itself instead of looping for ever", was_alive and gone,
            "pid %s alive before: %s, gone after: %s" % (sandbox_pid, was_alive, gone))
    if sandbox_pid and not gone:  # do not leave the loop running on the machine
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(sandbox_pid)], capture_output=True)
        else:
            os.kill(sandbox_pid, 9)


def calls(s: Suite) -> None:
    s.section("Q. Calls: time limits per tool, queues, large results, continuing a failed program")
    s.config("timing", {
        "connectors": {"chaos": {"type": "chaos"}, "quick": {"type": "chaos", "call_timeout_s": 0.5}, "serial": {"type": "chaos", "max_concurrency": 1}},
        "policy": {"default": "allow"},
        "limits": dict(LIMITS, timeout_s=20, call_timeout_s=1, call_timeouts={"chaos.sleep": 6}),
    })
    limits = s.program("limits", '''
def attempt(path, seconds):
    try:
        return call(path, {"seconds": seconds})
    except ToolError as error:
        return [error.kind, error.message, error.hint]
[attempt("chaos.sleep", 1.5), attempt("quick.sleep", 1.5), attempt("serial.sleep", 3)]
''')
    r = s.cli("run", "-f", limits, "--json", config="timing", home="calls")
    result = r.get("result", default=[None, None, None])
    s.check("C1", "a time limit set for one tool applies to that tool: the long call finishes, under a global limit it would not survive",
            r.code == 0 and result[0] == 1.5, r.out[:300])
    s.check("C1", "a connector's own limit ends its calls, and the error names the time that was allowed",
            isinstance(result[1], list) and result[1][0] == "ToolTimeout" and "0.5s" in result[1][1], json.dumps(result[1])[:300])
    s.check("C1", "with no pattern and no connector limit the global one applies; the hint says how to give this tool more time",
            isinstance(result[2], list) and result[2][0] == "ToolTimeout" and "1s" in result[2][1] and "call_timeouts" in result[2][2] and "serial.sleep" in result[2][2],
            json.dumps(result[2])[:400])
    r = s.cli("run", "--json", "parallel([('serial.sleep', {'seconds': 0.6})] * 3)", config="timing", home="calls")
    s.check("C2", "max_concurrency 1 runs a batch one call at a time, and a queued call's clock starts when it does: three 0.6 s calls pass a 1 s limit",
            r.code == 0 and r.get("result") == [0.6, 0.6, 0.6] and r.get("stats", "durationMs", default=0) >= 1700, r.out[:300])
    r = s.cli("run", "--json", "out = parallel([('chaos.echo', {'value': 'first'}), ('quick.sleep', {'seconds': 3}), ('chaos.echo', {'value': 'last'})], return_errors=True)\n[out[0], out[1].kind, out[2]]",
              config="timing", home="calls")
    s.check("C4", "one call that times out does not take the rest of its batch with it",
            r.code == 0 and r.get("result") == ["first", "ToolTimeout", "last"] and r.get("stats", "durationMs", default=9999) < 2500, r.out[:300])

    s.config("deadline", {"connectors": {"chaos": {"type": "chaos"}}, "policy": {"default": "allow"},
                          "limits": dict(LIMITS, timeout_s=2, call_timeout_s=30)})
    cut = s.program("cut", '''
done = [tools.chaos.echo(value="kept")]
try:
    tools.chaos.sleep(seconds=20)
except ToolError as error:
    done.append(error.message)
try:
    tools.chaos.echo(value="too late")
except ToolError as error:
    done.append(error.kind)
done
''')
    started = time.monotonic()
    r = s.cli("run", "-f", cut, "--json", config="deadline", home="calls")
    result = r.get("result", default=[])
    s.check("C3", "a call ended by the program's time limit says so, and names that limit, not its own",
            len(result) == 3 and "limits.timeout_s" in str(result[1]) and "program" in str(result[1]), r.out[:400])
    s.check("C3", "the program gets a moment to return what it has; no further call is started, and the outcome carries a warning",
            r.code == 0 and result[0] == "kept" and result[2] == "ToolTimeout" and "time limit" in " ".join(r.get("warnings", default=[]))
            and time.monotonic() - started < 8, "%.1fs %s" % (time.monotonic() - started, r.out[:300]))
    r = s.cli("run", "--json", "try:\n    tools.chaos.sleep(seconds=20)\nexcept ToolError:\n    pass\nwhile True:\n    pass", config="deadline", home="calls")
    s.check("C3 X1", "a program that does not finish in that moment is still stopped as Timeout", r.code == 1 and r.get("error", "type") == "Timeout", r.out[:300])

    r = s.cli("run", "--json", "try:\n    tools.chaos.big(chars=50000)\nexcept ToolError as error:\n    out = [error.kind, error.message, error.hint]\nout", config="chaos", home="calls")
    message = str(r.get("result", 1, default=""))
    saved = message.rpartition("saved on the host: ")[2].strip()
    s.mask(str(WORK), "<work>")
    s.check("C5", "a result over the size limit fails the call, and the error says where the result was saved and that the call ran",
            r.get("result", 0) == "ValueTooLarge" and saved and "it ran" in str(r.get("result", 2, default="")), r.out[:400])
    s.check("C5", "the saved file holds the whole result", bool(saved) and os.path.isfile(saved) and len(json.loads(Path(saved).read_text(encoding="ascii"))) == 50000, saved)
    r = s.cli("run", "--json", "try:\n    parallel([('chaos.counter', {'name': 'flood'})] * 50)\nexcept ToolError as error:\n    out = error.kind\nout", config="chaos", home="calls")
    log = s.cli("show", "last", "--json", config="chaos", home="calls", quiet=True)
    entries = [e for e in (log.json or []) if e.get("type") == "call"]
    s.check("C6", "a batch over the call limit is refused as one recorded step, and no call in it runs",
            r.get("result") == "CallLimitExceeded" and len(entries) == 1 and entries[0].get("kind") == "batch" and counter("calls", "flood") == 0, r.out[:300])

    first = s.program("late", '''
a = tools.chaos.counter(name="ra")
b = tools.chaos.counter(name="rb")
tools.chaos.fail(message="the last step failed")
[a, b]
''')
    r = s.cli("run", "-f", first, "--json", config="chaos", home="calls")
    failed_id = str(r.get("executionId"))
    s.check("C7", "a program that fails late is told how to continue: the calls that completed, and --reuse with its id",
            r.code == 1 and "2 tool call(s) completed" in r.get("hint", default="") and "--reuse " + failed_id in r.get("hint", default=""), r.out[-400:])
    fixed = s.program("late_fixed", '''
a = tools.chaos.counter(name="ra")
b = tools.chaos.counter(name="rb")
try:
    tools.chaos.fail(message="the last step failed")
    c = "worked"
except ToolError as error:
    c = error.kind
d = tools.chaos.counter(name="rd")
[a, b, c, d]
''')
    r = s.cli("run", "-f", fixed, "--reuse", failed_id, "--json", config="chaos", home="calls")
    s.check("C7", "the fixed program, run with --reuse, takes the two finished calls from the log and runs only what is new",
            r.code == 0 and r.get("result") == [1, 1, "ChaosFailure", 1] and r.get("stats", "reused") == 2
            and (counter("calls", "ra"), counter("calls", "rb"), counter("calls", "rd")) == (1, 1, 1), r.out[:400])
    r = s.cli("verify", "last", "--json", config="chaos", home="calls")
    s.check("C7 I3", "an execution that reused results replays from its own log", r.code == 0 and r.get("verified") is True, r.out[:200])
    changed = s.program("late_changed", '''
a = tools.chaos.counter(name="rz")
b = tools.chaos.counter(name="rb")
[a, b]
''')
    r = s.cli("run", "-f", changed, "--reuse", failed_id, "--json", config="chaos", home="calls")
    s.check("C7", "the first step that differs ends the reuse: it and everything after it run live",
            r.code == 0 and r.get("result") == [1, 2] and r.get("stats", "reused") == 0 and counter("calls", "rb") == 2, r.out[:300])
    s.config("tight", chaos_config(policy={"deny": ["gated.fail", "chaos.counter"]}))
    r = s.cli("run", "-f", fixed, "--reuse", failed_id, "--json", config="tight", home="calls")
    s.check("C7 L1", "a recorded result is not handed out for a tool the policy now denies",
            r.code == 1 and r.get("error", "type") == "PolicyDenied" and r.get("stats", "reused") == 0, r.out[:300])
    r = s.cli("run", "-f", fixed, "--reuse", "ex_does_not_exist", "--json", config="chaos", home="calls")
    s.check("C7 I7", "--reuse with an execution that does not exist exits 5 before anything runs", r.code == 5 and counter("calls", "rd") == 1)

    before = len(s.cli("executions", "--json", "--limit", "500", config="chaos", home="calls", quiet=True).json or [])
    r = s.cli("run", "--check", "--json", "a = tools.chaos.counter(name='checked')\nb = call('demo.read_note', {'id': 1})\nparallel([('gated.counter', {})])\n('not.a_tool', 1)",
              config="chaos", home="calls")
    tools_found = {t["tool"]: t["policy"] for t in r.get("result", "tools", default=[])}
    s.check("C8", "--check lists the tools a program names, with their policy, and does not run it",
            r.code == 0 and tools_found == {"chaos.counter": "allow", "demo.read_note": "allow", "gated.counter": "ask"} and counter("calls", "checked") == 0, r.out[:400])
    r = s.cli("run", "--check", "--json", "tools.demo.red_note(id=1)\ncall('chaos.ecko', {})\nparallel([('gated.countr', {})])", config="chaos", home="calls")
    s.check("C8", "--check finds tool names that do not exist, however they are written, and suggests the right ones (exit 1)",
            r.code == 1 and r.get("error", "type") == "UnknownTool" and len(r.get("result", "unknown", default=[])) == 3
            and "demo.read_note" in r.get("error", "hint", default=""), r.out[:500])
    r = s.cli("run", "--check", "--json", "value = 'broken\nstring'", config="chaos", home="calls")
    s.check("C8 H10", "--check reports a syntax error with its hint (exit 1)", r.code == 1 and r.get("error", "type") == "SyntaxError" and "shell" in r.get("error", "hint", default=""))
    answers = [s.cli("run", "--check", "--json", "tools.chaos.echo(value=%d)" % n, config="chaos", home="calls", quiet=True) for n in range(12)]
    s.check("C11", "--check answers every time: the program is sent only once the sandbox is ready for it (12 in a row)",
            all(a.code == 0 and a.get("result", "tools", 0, "tool") == "chaos.echo" for a in answers),
            str([a.get("error", "type") for a in answers if a.code != 0]))
    after = len(s.cli("executions", "--json", "--limit", "500", config="chaos", home="calls", quiet=True).json or [])
    s.check("C8", "--check records no execution", before == after, "%d -> %d" % (before, after))

    s.config("hinted", {
        "connectors": {"demo": {"type": "demo"}, "inner": {"type": "mcp-stdio", "command": [PY, "main.py", "--home", ".e2e/inner", "serve"]},
                       "chaos": {"type": "chaos"}},
        "policy": {"default": "allow", "deny": ["demo.create_note"]},
        "limits": dict(LIMITS, timeout_s=30, call_timeout_s=20),
    })
    r = s.cli("run", "--json", "out = []\nfor path, args in [('inner.execute', {}), ('chaos.fail', {'message': 'x'}), ('demo.create_note', {'title': 't', 'body': 'b'})]:\n"
              "    try:\n        call(path, args)\n    except ToolError as e:\n        out.append([e.kind, e.hint])\nout", config="hinted", home="calls")
    result = r.get("result", default=[[], [], []])
    s.check("H20", "a tool that failed with no hint of its own gets one from the gateway: its signature, and that describe() shows the schema",
            r.code == 0 and result[0][0] == "ToolError" and "inner.execute(" in result[0][1] and "describe('inner.execute')" in result[0][1], r.out[:400])
    s.check("H20", "a failure that has no hint but is not a failure of the tool (ChaosFailure) is left as it was, and a denial keeps its own hint",
            result[1] == ["ChaosFailure", ""] and result[2][0] == "PolicyDenied" and "signature" not in result[2][1] and "describe(" not in result[2][1], json.dumps(result[1:])[:300])
    s.config("literal", {"connectors": {"demo": {"type": "demo"}, "x": {"type": "mcp-stdio", "command": [PY, "-c", "pass"],
                                                                         "env": {"API_TOKEN": "literal-secret-value", "REF": "${E2E_SOME_VAR}"}}}})
    r = s.cli("config", "--json", config="literal", home="calls")
    s.check("C10", "config does not print a literal value from a connector's env; a ${NAME} reference is shown",
            r.code == 0 and "literal-secret-value" not in r.out and r.get("connectors", "x", "env") == {"API_TOKEN": "(hidden)", "REF": "${E2E_SOME_VAR}"}, r.out[:300])


def long_runs(s: Suite) -> None:
    s.section("R. Long runs: detach, progress, wait, cancel, and a host that is killed")
    s.config("long", {"connectors": {"chaos": {"type": "chaos"}, "gated": {"type": "chaos"}},
                      "policy": {"default": "ask", "allow": ["chaos.*"]}, "approval": {"require_terminal": False},
                      "limits": dict(LIMITS, timeout_s=60, call_timeout_s=40, max_calls=400, max_log_chars=4000)})
    slow = s.program("slow_job", '''
print("phase one: counting")
tools.chaos.counter(name="lr_before")
print("phase two: the long call")
tools.chaos.sleep(seconds=args.get("seconds", 3))
tools.chaos.counter(name="lr_after")
"finished"
''')
    started = time.monotonic()
    r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=6", "--json", config="long", home="long")
    job = str(r.get("executionId"))
    s.check("R1", "run --detach returns at once with the execution id and status running (exit 0)",
            r.code == 0 and r.get("status") == "running" and job.startswith("ex_") and time.monotonic() - started < 5,
            "%.1fs %s" % (time.monotonic() - started, r.out[:200]))

    def in_flight(job_id: str) -> Any:
        seen = s.cli("get", job_id, "--json", config="long", home="long", quiet=True)
        return seen if seen.get("progress", "callsDone") == 1 and seen.get("progress", "running") else None

    poll(lambda: in_flight(job))
    r = s.cli("get", job, "--json", config="long", home="long")
    running = r.get("progress", "running", default=[])
    s.check("R2", "get on a running execution shows the call that finished, the call in flight and for how long",
            r.code == 0 and r.get("status") == "running" and r.get("progress", "callsDone") == 1
            and running and running[0].get("tool") == "chaos.sleep" and running[0].get("forS", -1) >= 0, r.out[:400])
    s.check("R2", "and what the program has printed so far", "phase two: the long call" in r.get("logs", default=""), r.get("logs", default="")[:200])
    r = s.cli("wait", job, "--timeout", "0.3", "--json", config="long", home="long")
    s.check("R3", "wait with less time than the run needs returns running, with exit 6", r.code == 6 and r.get("status") == "running", "exit %d" % r.code)
    r = s.cli("wait", job, "--timeout", "30", "--json", config="long", home="long")
    s.check("R1 R3", "the program kept running after the command that started it had exited; wait returns its outcome (exit 0)",
            r.code == 0 and r.get("result") == "finished" and (counter("long", "lr_before"), counter("long", "lr_after")) == (1, 1), r.out[:300])
    started = time.monotonic()
    r = s.cli("wait", job, "--timeout", "30", "--json", config="long", home="long")
    s.check("R3", "wait on an execution that has finished returns at once", r.code == 0 and r.get("status") == "completed" and time.monotonic() - started < 2)

    r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=30", "--json", config="long", home="long")
    job = str(r.get("executionId"))
    poll(lambda: in_flight(job))
    started = time.monotonic()
    r = s.cli("cancel", job, "--json", config="long", home="long")
    s.check("R4", "cancel from another process stops a running execution: Cancelled, promptly (exit 1)",
            r.code == 1 and r.get("error", "type") == "Cancelled" and time.monotonic() - started < 8, "%.1fs %s" % (time.monotonic() - started, r.out[:300]))
    s.check("R4", "what ran before the cancel stays applied; what came after never runs",
            (counter("long", "lr_before"), counter("long", "lr_after")) == (2, 1))
    r = s.cli("cancel", job, "--json", config="long", home="long")
    s.check("R4 I7", "cancelling an execution that has ended is refused (exit 2)", r.code == 2 and r.get("error", "type") == "StateError")
    r = s.cli("run", "--detach", "--json", "tools.gated.counter(name='lr_gated')", config="long", home="long")
    r = s.cli("wait", "last", "--timeout", "20", "--json", config="long", home="long")
    s.check("R1 I2", "a detached run that needs approval pauses like any other (exit 3)", r.code == 3 and r.get("status") == "paused" and counter("long", "lr_gated") == 0, r.out[:300])
    r = s.cli("cancel", "last", "--json", config="long", home="long")
    s.check("R4", "cancelling a paused execution rejects it", r.code == 1 and r.get("status") == "rejected" and counter("long", "lr_gated") == 0)

    killed = subprocess.Popen([PY, "main.py", "--home", ".e2e/long", "--config", ".e2e/long.json", "run", "-f", slow, "--arg", "seconds=30", "--json"],
                              cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=s.env())
    victim = ""
    for _ in range(100):
        time.sleep(0.05)
        rows = s.cli("executions", "--json", "--limit", "1", config="long", home="long", quiet=True).json or []
        if rows and rows[0]["status"] == "running" and rows[0]["toolCalls"] >= 1:
            victim = rows[0]["executionId"]
            break
    killed.kill()  # what a shell does to a command that ran past its time limit
    killed.wait(timeout=10)
    s.log()
    s.log("(started a foreground run, waited for its first call to finish, then killed the process outright)")
    r = s.cli("get", victim or "last", "--json", config="long", home="long")
    s.check("R5", "an execution whose process was killed does not stay running: get reports Interrupted (exit 1)",
            bool(victim) and r.code == 1 and r.get("error", "type") == "Interrupted", r.out[:400])
    s.check("R5 C7", "and says how to go on: start long programs detached, continue this one with --reuse",
            "--detach" in r.get("error", "hint", default="") and "--reuse " + victim in r.get("hint", default=""), r.out[-400:])
    r = s.cli("run", "-f", slow, "--arg", "seconds=0.1", "--reuse", victim or "last", "--json", config="long", home="long")
    s.check("R5 I8", "the dead process's lock blocks nothing: the program is run again, reusing the call that had finished",
            r.code == 0 and r.get("result") == "finished" and r.get("stats", "reused") == 1 and counter("long", "lr_before") == 3, r.out[:300])

    noisy = s.program("noisy_job", '''
for i in range(60):
    print("line %03d " % i + "x" * 2000)
    tools.chaos.echo(value=i)
"done"
''')
    r = s.cli("run", "-f", noisy, "--json", config="long", home="long")
    log = s.cli("show", "last", "--json", config="long", home="long", quiet=True)
    persisted = sum(len(e.get("text", "")) for e in (log.json or []) if e.get("type") == "log")
    s.check("R9", "output persisted while a program runs is bounded, however much it prints (120 kB printed)",
            r.code == 0 and r.get("result") == "done" and 0 < persisted <= 64100, "%d chars persisted" % persisted)

    jobs: List[str] = []
    try:
        r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=30", "--select", "result", "--select", "logs", "--json", config="long", home="long")
        job = str(r.get("executionId"))
        jobs.append(job)
        hint = r.get("hint", default="")
        s.check("R13", "run --detach with --select says nothing can be selected yet, and names the wait command that does it",
                r.code == 0 and r.get("status") == "running" and "--select" in hint and "wait %s --until-done --select result --select logs" % job in hint, hint[:400])
        poll(lambda: in_flight(job))
        r = s.cli("get", job, "--select", "logs", config="long", home="long")
        s.check("R14", "get --select logs on a running execution returns the output so far (exit 0)", r.code == 0 and "phase two: the long call" in r.out, r.out[:300] + r.err[:200])
        r = s.cli("get", job, "--select", "progress", "--json", config="long", home="long")
        s.check("R14", "so does progress", r.code == 0 and r.get("status") == "running" and r.get("value", "callsDone") == 1, r.out[:300])
        r = s.cli("get", job, "--select", "calls", "--json", config="long", home="long")
        names = [(c.get("name"), c.get("state")) for c in r.get("value", default=[]) if isinstance(c, dict)]
        s.check("R14", "so do the calls, with the state of each", ("chaos.counter", "applied") in names and ("chaos.sleep", "executing") in names, r.out[:300])
        r = s.cli("get", job, "--select", "logs", "--select", "args", "--select", "progress.callsDone", "--json", config="long", home="long")
        s.check("R13 R14", "several paths on a running execution: select is the list, values are by path",
                r.code == 0 and r.get("select") == ["logs", "args", "progress.callsDone"] and r.get("values", "progress.callsDone") == 1
                and "phase one" in r.get("values", "logs", default="") and r.get("values", "args", "seconds") == 30 and "value" not in (r.json or {}), r.out[:400])
        for part in ("result", "result.rows.0", "variables.x"):
            r = s.cli("get", job, "--select", part, "--json", config="long", home="long")
            fix = r.get("hint", default="")
            s.check("R14", "%s while it runs is the running outcome, not an error (exit 0), with a hint that says when it exists and what to read meanwhile" % part,
                    r.code == 0 and r.get("status") == "running" and "select" not in (r.json or {}) and "error" not in (r.json or {})
                    and "no " + part.split(".")[0] + " yet" in fix and "--until-done" in fix and "--select logs" in fix, r.out[:400])
        r = s.cli("cancel", job, "--json", config="long", home="long")
        s.check("R14", "(the job is stopped)", r.get("error", "type") == "Cancelled", r.out[:200])

        r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=4", "--json", config="long", home="long")
        job = str(r.get("executionId"))
        jobs.append(job)
        r = s.cli("get", "last", "--select", "code", "--json", config="long", home="long")
        s.check("R12 E9", "last names an execution that is still running", r.code == 0 and r.get("executionId") == job and r.get("status") == "running", r.out[:200])
        r = s.cli("wait", job, "--timeout", "0.3", "--json", config="long", home="long")
        s.check("R12", "a plain wait that runs out of time names --until-done in its hint (exit 6)",
                r.code == 6 and "wait %s --until-done" % job in r.get("hint", default=""), r.out[:400])
        r = s.cli("wait", job, "--until-done", "--every", "0.5", "--json", config="long", home="long", timeout=120)
        beats = [line for line in r.err.splitlines() if line.startswith("waiting:")]
        s.check("R12", "wait --until-done has no time limit: it returns the outcome once the run has ended (exit 0), as a plain wait would",
                r.code == 0 and r.get("status") == "completed" and r.get("result") == "finished" and r.out.count("\n") == 1, r.out[:300])
        s.check("R12", "while it waited it wrote short lines to stderr: how long, calls done, the call in flight; stdout has only the outcome",
                len(beats) >= 1 and "call(s) done" in beats[0] and "chaos.sleep" in " ".join(beats) and "waiting" not in r.out, r.err[:300])
        started = time.monotonic()
        r = s.cli("wait", job, "--until-done", "--json", config="long", home="long")
        s.check("R12", "on an execution that has ended it returns at once", r.code == 0 and time.monotonic() - started < 3)
        r = s.cli("wait", job, "--until-done", "--timeout", "5", "--json", config="long", home="long")
        s.check("R12 I7", "--until-done and --timeout together are a usage error (exit 2)", r.code == 2 and r.get("error", "type") == "UsageError", r.out[:200])

        quick = s.program("quick_parts", '''
print("quick output")
{"a": 1, "rows": [{"id": 7}, {"id": 8}], "text": "hello"}
''')
        r = s.cli("run", "--detach", "-f", quick, "--json", config="long", home="long")
        job = str(r.get("executionId"))
        r = s.cli("wait", job, "--until-done", "--select", "result.rows.1.id", config="long", home="long")
        s.check("R13", "wait --select with one path prints that value alone, as get does", r.code == 0 and r.out.strip() == "8", r.out[:200])
        r = s.cli("wait", job, "--select", "result.text", "--select", "result.a", "--json", config="long", home="long")
        s.check("R13", "wait --select with several paths: select is the list, values are by path",
                r.code == 0 and r.get("select") == ["result.text", "result.a"] and r.get("values") == {"result.text": "hello", "result.a": 1} and r.get("status") == "completed", r.out[:300])
        r = s.cli("wait", job, "--select", "result.a,logs", "--json", config="long", home="long")
        s.check("R13", "a,b in one value means two paths", r.get("select") == ["result.a", "logs"] and r.get("values", "logs") == "quick output\n", r.out[:300])
        r = s.cli("wait", job, "--select", "result.text", "--json", config="long", home="long")
        s.check("R13 I7", "one path keeps the shape it had: select and value",
                r.get("select") == "result.text" and r.get("value") == "hello" and "values" not in (r.json or {}), r.out[:300])
        r = s.cli("get", job, "--select", "result.text", "--select", "result.a", config="long", home="long")
        s.check("R13", "several paths without --json print each under its name", r.code == 0 and r.out.split() == ["result.text:", "hello", "result.a:", "1"], r.out[:200])
        r = s.cli("wait", job, "--select", "result.nothing", "--select", "result.a", "--json", config="long", home="long")
        s.check("R13 I7", "a path that is not there is NotFound (exit 5), with what is available", r.code == 5 and r.get("error", "type") == "NotFound" and r.get("error", "hint"), r.out[:300])
        r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=30", "--json", config="long", home="long")
        job = str(r.get("executionId"))
        jobs.append(job)
        poll(lambda: in_flight(job))
        r = s.cli("wait", job, "--timeout", "0.3", "--select", "result", "--json", config="long", home="long")
        s.check("R13", "wait --select that runs out of time shows the running outcome (exit 6), not an error", r.code == 6 and r.get("status") == "running", r.out[:300])

        keep = s.program("state_keeper", '''
if state.get("released"):
    result = "already released"
else:
    tools.chaos.echo(value="releasing")
    state["released"] = True
    result = "released"
result
''')
        only_state = s.program("state_only", '''
"already released" if state.get("released") else "nothing to do"
''')
        note = "No tool was called"
        r = s.cli("run", "-f", only_state, "--session", "r15", "--json", config="long", home="long")
        s.check("R15", "no note when state was empty at the start, whatever the program did",
                r.code == 0 and not any(note in w for w in r.get("warnings", default=[])), r.out[:300])
        r = s.cli("run", "-f", keep, "--session", "r15", "--json", config="long", home="long")
        s.check("R15", "no note for a program that calls a tool, though state was empty",
                r.code == 0 and r.get("result") == "released" and r.get("stats", "toolCalls") == 1 and not any(note in w for w in r.get("warnings", default=[])), r.out[:300])
        r = s.cli("run", "-f", keep, "--session", "r15", "--json", config="long", home="long")
        s.check("R15", "the second 'release' calls nothing and returns the old result: the outcome says that no tool was called and state held a value",
                r.code == 0 and r.get("result") == "already released" and any(note in w and "state" in w for w in r.get("warnings", default=[])), r.out[:400])
        r = s.cli("run", "-f", keep, "--session", "r15x", "--json", config="long", home="long")
        s.check("R15", "(a new session runs the tool again, with no note)",
                r.code == 0 and r.get("result") == "released" and not any(note in w for w in r.get("warnings", default=[])), r.out[:300])
        text = s.cli("run", "-f", only_state, "--session", "r15", config="long", home="long").out
        s.check("R15", "text output shows it as a warning line", "warning: No tool was called" in text, text[:300])
    finally:
        for left in jobs:
            s.cli("cancel", left, "--json", config="long", home="long", quiet=True)

    if os.name == "nt":
        s.skip("interrupting a foreground run from the keyboard (R10): a console control event cannot be sent to one child process on Windows")
        return
    import signal

    front = subprocess.Popen([PY, "main.py", "--home", ".e2e/long", "--config", ".e2e/long.json", "run", "-f", slow, "--arg", "seconds=30", "--json"],
                             cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=s.env())
    time.sleep(1.5)
    front.send_signal(signal.SIGINT)
    code = front.wait(timeout=20)
    r = s.cli("get", "last", "--json", config="long", home="long")
    s.check("R10", "a foreground run interrupted from the keyboard ends as Interrupted (exit 130 from the command, 1 from get)",
            code == 130 and r.code == 1 and r.get("error", "type") == "Interrupted", "exit %d %s" % (code, r.out[:300]))

    r = s.cli("run", "--detach", "-f", slow, "--arg", "seconds=30", "--json", config="long", home="long")
    job = str(r.get("executionId"))
    waiter = None
    try:
        poll(lambda: in_flight(job))
        waiter = subprocess.Popen([PY, "main.py", "--home", ".e2e/long", "--config", ".e2e/long.json", "wait", job, "--until-done", "--every", "0.3"],
                                  cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=s.env())
        waiter.stderr.readline()  # type: ignore[union-attr]  # its first line: it is inside the wait now
        waiter.send_signal(signal.SIGINT)
        code = waiter.wait(timeout=20)
        r = s.cli("get", job, "--json", config="long", home="long")
        s.check("R12", "Ctrl-C on wait --until-done stops the waiting (exit 130) and leaves the execution running",
                code == 130 and r.get("status") == "running", "exit %d %s" % (code, r.out[:200]))
    finally:
        if waiter is not None and waiter.poll() is None:
            waiter.kill()
            waiter.wait(timeout=10)
        s.cli("cancel", job, "--json", config="long", home="long", quiet=True)


MISBEHAVING = str(ROOT / "tests" / "fixtures" / "misbehaving_server.py")
FAKE_TOKEN = "fake-credential-for-tests-0001"  # not a real secret: the fixture echoes it so the suite can look for it
INHERITED_TOKEN = "inherited-value-for-tests-0002"  # another one, for a server that gets it from the host environment

# A complete MCP stdio server in a few lines, written to a file by the suite
# so that a test can edit "the project's own server" between two runs.
TINY_SERVER = '''import json, sys
VERSION = %r
TOOLS = [{"name": name, "description": "Return the version of this server script.", "inputSchema": {"type": "object", "properties": {}}} for name in %r]
for raw in sys.stdin.buffer:
    message = json.loads(raw)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "tiny", "version": VERSION}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": VERSION}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
'''


def _gone(pid: Any, seconds: float = 5.0) -> bool:
    """True once no process with this id is running, waiting a little."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def servers(s: Suite) -> None:
    s.section("S. MCP servers as they really are: slow, noisy, crashing, text-only, huge, leaking")
    state = WORK / "misbehave"
    state.mkdir(parents=True, exist_ok=True)
    s.mask(str(state), "<state>")

    def odd(**env: str) -> Dict[str, Any]:
        return {"type": "mcp-stdio", "command": [PY, MISBEHAVING], "env": dict({"MISBEHAVE_STATE": str(state)}, **env)}

    s.config("servers", {
        "connectors": {
            "demo": {"type": "demo"},
            "odd": odd(MISBEHAVE_TOKEN="${E2E_FIXTURE_TOKEN}"),
            "raw": dict(odd(), parse_json_text=False),
            "sluggish": dict(odd(MISBEHAVE_START_DELAY="4"), startup_timeout_s=15),
            "tooslow": odd(MISBEHAVE_START_DELAY="30"),
            "deaf": odd(MISBEHAVE_IGNORE_EOF="1"),
            "heir": dict(odd(), inherit_env=["MISBEHAVE_VER*", "MISBEHAVE_TOKEN"]),
            "picky": dict(odd(), inherit_env=["!MISBEHAVE_START_DELAY", "MISBEHAVE_*", "!MISBEHAVE_TOK*"]),
        },
        "policy": {"default": "allow"},
        "limits": {"timeout_s": 60, "call_timeout_s": 10, "startup_timeout_s": 3, "max_calls": 50, "max_result_chars": 4000,
                   "call_timeouts": {"odd.slow": 1}},
    })
    secret = {"E2E_FIXTURE_TOKEN": FAKE_TOKEN}

    shapes = s.program("shapes", '''
{"text": tools.odd.text(), "json": tools.odd.json_text(), "number": tools.odd.number_text(), "multi": tools.odd.multi_text(),
 "image": tools.odd.image(), "raw_json": tools.raw.json_text(), "raw_number": tools.raw.number_text()}
''')
    r = s.cli("run", "-f", shapes, "--json", config="servers", home="servers", env=secret)
    result = r.get("result", default={})
    s.check("M2", "a server that prints a banner on stdout and ends its lines with CRLF is still usable", r.code == 0 and result.get("text") == "just some text, with a { brace", r.out[:300])
    s.check("M4", "text that is JSON is parsed, other text stays text, several blocks are a list, an image is described and not inlined",
            result.get("json") == {"ok": True, "items": [1, 2, 3]} and result.get("number") == 42 and result.get("multi") == ["first block", "second block"]
            and result.get("image") == {"text": ["a caption"], "other": [{"type": "image", "mimeType": "image/png", "chars": 400}]}, json.dumps(result)[:400])
    s.check("M4", "with parse_json_text false on the connector, text is always text",
            result.get("raw_json") == '{"ok": true, "items": [1, 2, 3]}' and result.get("raw_number") == "42", json.dumps(result)[:400])

    crash = s.program("crash", '''
before = tools.odd.pid()
try:
    tools.odd.crash()
    crashed = "no error"
except ToolError as error:
    crashed = [error.kind, error.message, error.hint]
after = tools.odd.pid()
[before["pid"] != after["pid"], after["asked"], crashed]
''')
    r = s.cli("run", "-f", crash, "--json", config="servers", home="servers", env=secret)
    crashed = r.get("result", 2, default=["", "", ""])
    s.check("M3", "a server that exits in the middle of a call fails that call, with its stderr in the message",
            crashed[0] == "ConnectorUnavailable" and "exited during tools/call" in crashed[1] and "crashing on purpose" in crashed[1], json.dumps(crashed)[:400])
    s.check("M3", "the next call finds a new server process, and the error had said it would", r.get("result", 0) is True and r.get("result", 1) == 1 and "started again" in crashed[2], r.out[:300])

    huge = s.program("huge", '''
info = describe("odd.huge")
[info.get("schemaTruncated"), info["schemaChars"], len(str(dict(info))), len(info["inputSchema"]["properties"]),
 info["signature"][:40], len(info["description"])]
''')
    r = s.cli("run", "-f", huge, "--json", config="servers", home="servers", env=secret)
    result = r.get("result", default=[None, 0, 0, 0, "", 0])
    s.check("M5", "a tool with a schema of hundreds of kilobytes can still be described: the schema is shortened and says so",
            r.code == 0 and result[0] is True and result[1] > 300000 and result[2] < 80000 and result[3] == 400 and result[4].startswith("odd.huge(option_000: str")
            and result[5] < 4200, r.out[:400])
    r = s.cli("describe", "odd.huge", "--json", config="servers", home="servers", env=secret)
    s.check("M5", "the same from the command line, with a note that points at the signature", r.code == 0 and "signature" in r.get("schemaNote", default=""), r.out[-200:])

    leak = s.program("leak", '''
leaked = tools.odd.leak()
try:
    tools.odd.leak(as_error=True)
    failed = "no error"
except ToolError as error:
    failed = error.message
[leaked, failed]
''')
    r = s.cli("run", "-f", leak, "--json", config="servers", home="servers", env=secret)
    log = s.cli("show", "last", "--json", config="servers", home="servers", env=secret, quiet=True)
    s.check("M6 C9", "a credential the server echoes back never reaches the program or the outcome: not as a value, not as a key, not in an error",
            r.code == 0 and FAKE_TOKEN not in r.out and r.get("result", 0, "token") == "[redacted]" and "[redacted]" in r.get("result", 0, default={})
            and "Bearer [redacted]" in str(r.get("result", 1)), r.out[:400])
    s.check("M6 C9", "and it is not in the execution log either", log.code == 0 and FAKE_TOKEN not in log.out and "[redacted]" in log.out)

    host = dict(secret, MISBEHAVE_VERSION="set on the host", MISBEHAVE_TOKEN=INHERITED_TOKEN, MISBEHAVE_START_DELAY="30")
    r = s.cli("run", "--json", "[tools.odd.version(), tools.heir.version(), tools.heir.leak()]", config="servers", home="servers", env=host)
    s.check("M17", "a server gets no variable of the host that the config does not name: the same variable is unset in one server and set in the one that inherits it",
            r.code == 0 and r.get("result", 0) == "unset" and r.get("result", 1) == "set on the host", r.out[:300])
    s.check("M17", "inherit_env passes what it names and nothing else (a start delay set on the host did not reach the server)", r.code == 0 and r.seconds < 20, "%.1fs" % r.seconds)
    s.check("M18 C9", "a credential that reached the server through inherit_env and is echoed back does not reach the program",
            INHERITED_TOKEN not in r.out and r.get("result", 2, "token") == "[redacted]", r.out[:300])
    r = s.cli("run", "--json", "[tools.picky.version(), tools.picky.leak()['token']]", config="servers", home="servers", env=host)
    s.check("M19", "a name after ! in inherit_env is left out of a family that is passed on: the server got the version, not the start delay and not the credential",
            r.code == 0 and r.get("result") == ["set on the host", ""] and INHERITED_TOKEN not in r.out, r.out[:300])
    r = s.cli("search", "--namespace", "odd", "--json", config="servers", home="servers", env=secret)
    r2 = s.cli("search", "--namespace", "odd", "--full", "--json", config="servers", home="servers", env=secret)
    short = {hit["path"]: hit["description"] for hit in (r.json or [])}
    whole = {hit["path"]: hit["description"] for hit in (r2.json or [])}
    s.check("H16", "search --full gives whole descriptions where the listing gives one line of each, cut at 160 characters",
            r.code == 0 and r2.code == 0 and sorted(short) == sorted(whole) and len(short.get("odd.huge", "")) == 160 and short["odd.huge"].endswith("...")
            and whole.get("odd.huge", "").startswith("A tool with an enormous schema. This parameter is described") and len(whole["odd.huge"]) > 4000
            and whole.get("odd.leak") == short.get("odd.leak") == "Return this server's credential, in a result or in an error.", "%d and %d chars" % (len(short.get("odd.huge", "")), len(whole.get("odd.huge", ""))))
    s.check("H16 M5", "a description longer than describe keeps is cut there too, and says by how much",
            "more chars of description not shown" in whole.get("odd.huge", "") and len(whole.get("odd.huge", "")) < 4100, whole.get("odd.huge", "")[-80:])
    bad = json.loads((WORK / "servers.json").read_text(encoding="utf-8"))
    bad["connectors"]["heir"]["inherit_env"] = "MISBEHAVE_*"
    s.config("servers_bad", bad)
    r = s.cli("config", "--json", config="servers_bad", home="servers")
    s.check("M17 G1", "inherit_env that is not a list or true is a config error that says what it takes",
            r.code == 2 and "inherit_env" in r.get("error", "message", default="") and "list" in r.get("error", "message", default=""), r.out[:300])

    r = s.cli("run", "--detach", "--json", "tools.odd.progress(steps=12, pause=0.5)", config="servers", home="servers", env=secret)
    job = str(r.get("executionId"))

    def reporting() -> Any:
        seen = s.cli("get", job, "--json", config="servers", home="servers", env=secret, quiet=True)
        return seen if (seen.get("progress", "running", default=[{}]) or [{}])[0].get("reported") else None

    r = poll(reporting, seconds=5.0) or s.cli("get", job, "--json", config="servers", home="servers", env=secret)
    reported = str((r.get("progress", "running", default=[{}]) or [{}])[0].get("reported"))
    s.check("M7 R2", "progress a server reports during a long call is visible while the call runs", r.get("status") == "running" and "of 12" in reported, r.out[:400])
    r = s.cli("wait", job, "--timeout", "30", "--json", config="servers", home="servers", env=secret)
    s.check("M7", "and the call completes normally", r.code == 0 and r.get("result") == {"steps": 12}, r.out[:200])

    (state / "slow.log").unlink(missing_ok=True)
    giveup = s.program("giveup", '''
try:
    tools.odd.slow(seconds=20, label="abandoned")
    first = "no error"
except ToolError as error:
    first = error.kind
[first, tools.odd.slow(seconds=0.4, label="afterwards")]
''')
    r = s.cli("run", "-f", giveup, "--json", config="servers", home="servers", env=secret)
    notes = (state / "slow.log").read_text(encoding="utf-8") if (state / "slow.log").is_file() else ""
    s.check("M8", "when the gateway gives up on a call, the server is told and can stop the work",
            r.get("result") == ["ToolTimeout", "slept"] and "abandoned cancelled" in notes and "afterwards finished" in notes, r.out[:200] + " | " + notes)

    r = s.cli("run", "--json", "[tools.odd.do_it(), tools.odd.do_it_2(), tools.odd.do_it_3(), describe('odd.do_it_2')['originalName']]", config="servers", home="servers", env=secret)
    s.check("M10 N5", "three tool names that become one identifier are all callable, and each reaches its own tool",
            r.get("result") == ["this is do-it", "this is do.it", "this is do_it", "do.it"], r.out[:300])

    together = s.program("together", '''
out = parallel([("sluggish.text", {}), ("tooslow.text", {}), ("odd.number_text", {}), ("demo.list_notes", {})], return_errors=True)
[out[0], [out[1].kind, out[1].message, out[1].hint], out[2], len(out[3])]
''')
    started = time.monotonic()
    r = s.cli("run", "-f", together, "--json", config="servers", home="fresh", env=secret)
    took = time.monotonic() - started
    result = r.get("result", default=[None, ["", "", ""], None, None])
    s.check("M1", "a server that is slow to start works when its connector allows the time (startup_timeout_s)", result[0] == "just some text, with a { brace", r.out[:300])
    s.check("M1", "one that does not start in time is unavailable, and the error names the setting to raise",
            result[1][0] == "ConnectorUnavailable" and "handshake" in result[1][1] and "startup_timeout_s" in result[1][2], json.dumps(result[1])[:400])
    s.check("M11", "the others in the batch keep their results, and the servers were started side by side (4 s and 3 s of waiting took under 7)",
            result[2] == 42 and result[3] == 10 and took < 6.8, "%.1fs" % took)

    r = s.cli("run", "--json", "tools.deaf.spawn_child()", config="servers", home="servers", env=secret)
    pids = r.get("result", default={})
    s.check("M9", "a server that ignores the end of its input is stopped at the end of the run, together with the process it started",
            r.code == 0 and bool(pids.get("server")) and _gone(pids.get("server")) and _gone(pids.get("child")), json.dumps(pids))
    for pid in (pids.get("server"), pids.get("child")):  # never leave fixture processes behind
        if pid and _alive(pid):
            subprocess.run(["taskkill", "/F", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)], capture_output=True)
    _pooled_servers(s)
    _abandoned_calls(s, state, odd)


def _pooled_servers(s: Suite) -> None:
    """M20, M21: copies of a server that answers one request at a time
    ("processes"), and tools that must never overlap ("serial"). Every
    timing check compares what the servers wrote down, with wide margins."""
    pool = WORK / "pool"
    shutil.rmtree(pool, ignore_errors=True)
    pool.mkdir(parents=True, exist_ok=True)
    s.mask(str(pool), "<pool>")

    def server(**extra: Any) -> Dict[str, Any]:
        # These servers ignore the end of their input: one the pool forgot
        # to stop would stay running, and the last check would see it.
        env = {"MISBEHAVE_STATE": str(pool), "MISBEHAVE_ONE_AT_A_TIME": "1", "MISBEHAVE_IGNORE_EOF": "1", "MISBEHAVE_TOKEN": "${E2E_FIXTURE_TOKEN}"}
        return dict({"type": "mcp-stdio", "command": [PY, MISBEHAVING], "env": env}, **extra)

    s.config("pool", {
        "connectors": {
            "one": server(),
            "two": server(processes=2),
            "both": server(processes=2, serial=["build-*"]),
        },
        "policy": {"default": "allow"},
        "limits": {"timeout_s": 60, "call_timeout_s": 20, "startup_timeout_s": 10, "max_calls": 50, "catalog_cache_s": 0,
                   "call_timeouts": {"both.build_step": 4.5}},
    })
    secret = {"E2E_FIXTURE_TOKEN": FAKE_TOKEN}

    # Each server process writes files of its own: two that append to one
    # file at the same moment can lose a line, and a count would then be wrong.
    def started() -> List[int]:
        return sorted(int(line) for path in pool.glob("start-*.log") for line in path.read_text(encoding="utf-8").split())

    def worked() -> Dict[str, Dict[str, Any]]:
        lines = [line for path in sorted(pool.glob("work-*.log")) for line in path.read_text(encoding="utf-8").splitlines()]
        return {entry["label"]: entry for entry in (json.loads(line) for line in lines if line.strip())}

    def overlap(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[float]:
        """Seconds two recorded calls ran at the same time; None if one is missing."""
        if not a or not b:
            return None
        return min(a["end"], b["end"]) - max(a["start"], b["start"])

    try:
        r = s.cli("doctor", "--json", config="pool", home="pool", env=secret)
        s.check("M20", "doctor lists the tools of a connector with processes 2 from one server, not two (three connectors, three servers)",
                len(started()) == 3, "%d servers started; %s" % (len(started()), r.out[:200]))

        side = s.program("pool_side", '''
parallel([("two.work", {"seconds": 3, "label": "pool long"}), ("two.work", {"seconds": 0.3, "label": "pool short"})])
''')
        r = s.cli("run", "-f", side, "--json", config="pool", home="pool", env=secret)
        log = worked()
        both = overlap(log.get("pool long"), log.get("pool short"))
        s.check("M20", "with processes 2, a short call runs on a second copy while a long call holds the first: the servers saw them overlap",
                r.code == 0 and both is not None and both > 0.2 and log["pool long"]["pid"] != log["pool short"]["pid"], "%s %s" % (both, r.out[:300]))

        queue = s.program("pool_queue", '''
parallel([("one.work", {"seconds": 2, "label": "queue long"}), ("one.work", {"seconds": 0.3, "label": "queue short"})])
''')
        r = s.cli("run", "-f", queue, "--json", config="pool", home="pool", env=secret)
        log = worked()
        both = overlap(log.get("queue long"), log.get("queue short"))
        s.check("M20", "without it, the same two calls to a one-at-a-time server do not overlap: the second waits for the first",
                r.code == 0 and both is not None and both <= 0, "%s %s" % (both, r.out[:300]))

        builds = s.program("pool_serial", '''
parallel([("both.build_step", {"seconds": 2.5, "label": "build a"}), ("both.build_step", {"seconds": 2.5, "label": "build b"}),
          ("both.work", {"seconds": 0.5, "label": "read"})])
''')
        r = s.cli("run", "-f", builds, "--json", config="pool", home="pool", env=secret)
        log = worked()
        builds_overlap = overlap(log.get("build a"), log.get("build b"))
        read_overlap = max(overlap(log.get("build a"), log.get("read")) or -1, overlap(log.get("build b"), log.get("read")) or -1)
        s.check("M21", "two calls of a tool that serial names never overlap, though a second copy is free (the pattern is the server's spelling, build-*)",
                r.code == 0 and builds_overlap is not None and builds_overlap <= 0, "%s %s" % (builds_overlap, r.out[:300]))
        s.check("M21", "a build waiting for its turn holds no place: a read ran beside the first build",
                read_overlap > 0.2, str(read_overlap))
        s.check("M21", "and the waiting build's time limit (4.5 s) started when it did: 2.5 s of waiting and 2.5 s of work passed",
                r.code == 0 and [r.get("result", 0, "label"), r.get("result", 1, "label")] == ["build a", "build b"], r.out[:300])

        crash = s.program("pool_crash", '''
out = parallel([("two.work", {"seconds": 2, "label": "survivor"}), ("two.crash", {})], on_error="return")
[out[0], out[1].kind, tools.two.work(seconds=0.1, label="after")["label"]]
''')
        r = s.cli("run", "-f", crash, "--json", config="pool", home="pool", env=secret)
        s.check("M20 M3", "a copy that crashes mid-call fails that call only: the call on the other copy keeps its result, and the next call works",
                r.code == 0 and r.get("result", 0, "label") == "survivor" and r.get("result", 1) == "ConnectorUnavailable" and r.get("result", 2) == "after", r.out[:400])

        leak = s.program("pool_leak", '''
out = parallel([("two.leak", {"pause": 1.5}), ("two.leak", {"pause": 1.5, "as_error": True})], on_error="return")
[out[0], out[1].message]
''')
        r = s.cli("run", "-f", leak, "--json", config="pool", home="pool", env=secret)
        s.check("M20 C9", "a credential echoed back by either copy is removed: in a result and in an error",
                r.code == 0 and FAKE_TOKEN not in r.out and r.get("result", 0, "token") == "[redacted]"
                and "Bearer [redacted]" in str(r.get("result", 1)), r.out[:400])

        (pool / "slow.log").unlink(missing_ok=True)
        r = s.cli("run", "--detach", "--json", "parallel([('two.progress', {'steps': 40, 'pause': 0.3}), ('two.slow', {'seconds': 30, 'label': 'pooled'})])",
                  config="pool", home="pool", env=secret)
        job = str(r.get("executionId"))

        def reporting() -> Any:
            seen = s.cli("get", job, "--json", config="pool", home="pool", env=secret, quiet=True)
            running = seen.get("progress", "running", default=[]) or []
            return seen if len(running) == 2 and any(call.get("reported") for call in running) else None

        r = poll(reporting, seconds=15.0) or s.cli("get", job, "--json", config="pool", home="pool", env=secret)
        s.check("M20 M7", "progress a long call reports through a copy still reaches the caller, while another copy runs a second call",
                r.get("status") == "running" and "of 40" in json.dumps(r.get("progress", "running", default=[])), r.out[:400])
        s.cli("cancel", job, "--json", config="pool", home="pool", env=secret)
        notes = poll(lambda: (pool / "slow.log").read_text(encoding="utf-8") if (pool / "slow.log").is_file() else "", seconds=8.0) or ""
        s.check("M20 M8", "cancelling the run tells the copy that has the call, which stops the work",
                "pooled cancelled" in notes, notes)
        s.cli("wait", job, "--timeout", "30", "--json", config="pool", home="pool", env=secret, quiet=True)

        s.config("pool_late", {
            "connectors": {
                "three": server(processes=2),
                "holder": {"type": "chaos", "serial": ["hold"]},
            },
            "policy": {"default": "allow"},
            "limits": {"timeout_s": 60, "call_timeout_s": 20, "startup_timeout_s": 10, "catalog_cache_s": 0,
                       "call_timeouts": {"three.work": 1.5, "holder.hold": 1}},
        })
        late = s.program("pool_late", '''
first = tools.three.pid()["pid"]
try:
    tools.three.work(seconds=5, label="stuck")  # the server goes on after the timeout: it ignores "cancelled"
    stuck = "no error"
except ToolError as error:
    stuck = error.kind
[first, stuck, tools.three.work(seconds=0.2, label="next")]
''')
        r = s.cli("run", "-f", late, "--json", config="pool_late", home="pool", env=secret)
        log = worked()
        nxt = log.get("next") or {}
        s.check("M20", "a copy whose last call timed out is not given the next call while a new copy may be started: the next call ran at once, on another server",
                r.code == 0 and r.get("result", 1) == "ToolTimeout" and r.get("result", 2, "label") == "next"
                and bool(nxt) and nxt.get("pid") != r.get("result", 0), "%s %s" % (nxt, r.out[:300]))

        held = s.program("serial_held", '''
try:
    tools.holder.hold(seconds=4)  # given up on after its 1 s and the grace; the tool goes on for 4 s
    first = "no error"
except ToolError as error:
    first = error.kind
[first, tools.holder.hold(seconds=0.1)]
''')
        r = s.cli("run", "-f", held, "--json", config="pool_late", home="pool", env=secret)
        s.check("M21", "a serial call the gateway gave up on keeps its turn while it still runs: the next one started when it had ended (the tool saw nothing beside it)",
                r.code == 0 and r.get("result", 0) == "ToolTimeout" and r.get("result", 1) == {"seconds": 0.1, "beside": 0}, r.out[:400])
    finally:
        pids = started()
        # Some of these ids are a minute old, and an id is given out again
        # after its process ends: ask what runs under it before calling it a
        # leftover, and before stopping it.
        left = [pid for pid in pids if not _gone(pid) and "misbehaving_server" in _command_line(pid)]
        s.check("M20", "after every run, no copy of any server is left running (they ignore the end of their input, so each must be stopped)",
                bool(pids) and not left, "%d started, still running: %s" % (len(pids), left))
        for pid in left:  # never leave fixture processes behind
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)], capture_output=True)


def _abandoned_calls(s: Suite, state: Path, odd: Any) -> None:
    """A server that ignores "cancelled" and takes one request at a time:
    with on_abandon "restart" it is stopped with everything it started,
    without it nothing changes."""
    s.config("abandon", {
        "connectors": {"stub": dict(odd(), on_abandon="restart"), "stubn": odd()},
        "policy": {"default": "allow"},
        "limits": {"timeout_s": 60, "call_timeout_s": 10, "call_timeouts": {"stub.stubborn": 1, "stubn.stubborn": 1}},
    })
    records = [state / "stubborn-restart.json", state / "stubborn-notify.json", state / "left-behind.json"]
    for record in records:
        record.unlink(missing_ok=True)

    def started(record: Path) -> Dict[str, Any]:
        try:
            return json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    try:
        restart = s.program("abandon_restart", '''
try:
    tools.stub.stubborn(seconds=30, label="restart")
    first = ["no error", ""]
except ToolError as error:
    first = [error.kind, error.message]
[first, tools.stub.pid()["pid"], tools.stub.running(label="restart")]
''')
        r = s.cli("run", "-f", restart, "--json", config="abandon", home="abandon")
        old = started(records[0])
        first = r.get("result", 0, default=["", ""])
        s.check("M23", 'with on_abandon "restart", a call over its time limit on a server that ignores "cancelled" fails as a timeout that says the server is stopped',
                first[0] == "ToolTimeout" and "on_abandon is restart" in first[1] and "stopped" in first[1], r.out[:400])
        s.check("M23", "the next call is answered by a new server process, while the program still runs",
                bool(old.get("server")) and r.get("result", 1) not in (None, old.get("server")), "%s then %s" % (old.get("server"), r.get("result", 1)))
        s.check("M23 M24", "by then neither the old server nor the process it started through a wrapper that had exited is running",
                r.get("result", 2) == {"server": False, "grandchild": False} and _gone(old.get("grandchild"), 1.0), json.dumps(r.get("result", 2)))

        notify = s.program("abandon_notify", '''
try:
    tools.stubn.stubborn(seconds=3, label="notify")
    first = ["no error", ""]
except ToolError as error:
    first = [error.kind, error.message]
[first, tools.stubn.pid()["pid"]]
''')
        r = s.cli("run", "-f", notify, "--json", config="abandon", home="abandon")
        old = started(records[1])
        first = r.get("result", 0, default=["", ""])
        s.check("M23", "without the setting nothing changes: the server is told and kept, and the next call waits for it and reaches the same process",
                first[0] == "ToolTimeout" and "on_abandon" not in first[1] and bool(old.get("server")) and r.get("result", 1) == old.get("server"), r.out[:400])
        r = s.cli("run", "--json", "tools.stubn.spawn_child()", config="abandon", home="abandon")
        pids = r.get("result", default={})
        records[2].write_text(json.dumps(pids), encoding="utf-8")  # for the clean-up below
        time.sleep(1.0)
        s.check("M24", "a process that a server started and left behind is still running after the server exited by itself at the end of the run",
                r.code == 0 and bool(pids.get("child")) and _gone(pids.get("server")) and _alive(pids.get("child")), json.dumps(pids))
        bad = json.loads((WORK / "abandon.json").read_text(encoding="utf-8"))
        bad["connectors"]["stub"]["on_abandon"] = "kill"
        s.config("abandon_bad", bad)
        r = s.cli("config", "--json", config="abandon_bad", home="abandon")
        s.check("M23 G1", "an on_abandon that is not notify or restart is a config error that names both",
                r.code == 2 and "on_abandon" in r.get("error", "message", default="") and "restart" in r.get("error", "message", default=""), r.out[:300])
    finally:
        for record in records:  # never leave fixture processes behind, also when a check failed
            for pid in started(record).values():
                if pid and _alive(pid):
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)], capture_output=True)


def remote(s: Suite) -> None:
    s.section("T. MCP over HTTP: streamable HTTP and HTTP+SSE")
    fixture = subprocess.Popen([PY, "tests/fixtures/http_mcp_server.py", FAKE_TOKEN], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    try:
        port = fixture.stdout.readline().strip()  # type: ignore[union-attr]
        base = "http://127.0.0.1:%s" % port
        s.mask(port, "<port>")
        s.log()
        s.log("(an MCP server over HTTP on 127.0.0.1:<port>, from tests/fixtures/http_mcp_server.py; it wants a bearer token)")
        auth = {"Authorization": "Bearer ${E2E_HTTP_TOKEN}"}
        s.config("remote", {
            "connectors": {
                "web": {"type": "mcp-http", "url": base + "/mcp", "headers": auth},
                "legacy": {"type": "mcp-sse", "url": base + "/sse", "headers": auth},
                "nokey": {"type": "mcp-http", "url": base + "/mcp"},
                "page": {"type": "mcp-http", "url": base + "/index.html", "headers": auth},
                "down": {"type": "mcp-http", "url": "http://127.0.0.1:9/mcp"},
                "wrongkind": {"type": "mcp-sse", "url": base + "/state", "headers": auth},
            },
            "policy": {"default": "allow"},
            "limits": {"timeout_s": 40, "call_timeout_s": 10, "startup_timeout_s": 4, "call_timeouts": {"web.slow": 1}, "catalog_cache_s": 0},
        })
        env = {"E2E_HTTP_TOKEN": FAKE_TOKEN}

        def state() -> Dict[str, Any]:
            import urllib.request

            with urllib.request.urlopen(base + "/state", timeout=10) as response:
                return json.loads(response.read())

        basic = s.program("remote_basic", '''
who = tools.web.whoami()
[tools.web.add(a=2, b=40), tools.web.count(n=3), who["protocol"], bool(who["session"]), describe("web.add")["signature"],
 tools.legacy.add(a=1, b=2), [hit["path"] for hit in search("add")]]
''')
        r = s.cli("run", "-f", basic, "--json", config="remote", home="remote", env=env)
        result = r.get("result", default=[None] * 7)
        s.check("M12", "streamable HTTP: a tool is called and its structured answer comes back", r.code == 0 and result[0] == {"sum": 42}, r.out[:400])
        s.check("M12", "an answer sent as an event stream, with progress notifications before it, is read to its result", result[1] == {"counted": 3})
        s.check("M12", "later requests carry the session id and the protocol version the server chose",
                result[2] == "2025-06-18" and result[3] is True and result[4] == "web.add(a: int, b: int) -> dict", json.dumps(result[2:5]))
        s.check("M13", "HTTP+SSE: the endpoint event is followed, comment lines are skipped, and data spread over two lines is joined",
                result[5] == {"sum": 3} and sorted(result[6]) == ["legacy.add", "web.add"], json.dumps(result[5:]))
        seen = state()
        s.check("M12", "the session was ended when the run ended", len(seen["deleted"]) >= 1 and all(seen["deleted"]), json.dumps(seen)[:300])

        forgotten = s.program("forgotten", '''
before = tools.web.whoami()
tools.web.drop_sessions()
after = tools.web.whoami()
[before["session"] != after["session"], after["initialized"] - before["initialized"]]
''')
        r = s.cli("run", "-f", forgotten, "--json", config="remote", home="remote", env=env)
        s.check("M12", "a session the server has forgotten (404) is replaced by a new one and the call is sent again, once", r.code == 0 and r.get("result") == [True, 1], r.out[:300])
        r = s.cli("run", "--json", "tools.web.echo_auth()", config="remote", home="remote", env=env)
        s.check("M6 C9", "a header credential the server echoes back is removed from the result", r.code == 0 and FAKE_TOKEN not in r.out and "[redacted]" in str(r.get("result")), r.out[:300])

        broken = s.program("remote_broken", '''
out = {}
for namespace in ("nokey", "page", "down", "wrongkind"):
    try:
        getattr(tools, namespace).add(a=1, b=1)
        out[namespace] = "no error"
    except ToolError as error:
        out[namespace] = [error.kind, error.message, error.hint]
try:
    tools.web.slow(seconds=8)
    out["slow"] = "no error"
except ToolError as error:
    out["slow"] = [error.kind, error.message]
out["web"] = tools.web.add(a=1, b=1)
out
''')
        started = time.monotonic()
        r = s.cli("run", "-f", broken, "--json", config="remote", home="remote", env=env)
        result = r.get("result", default={})

        def got(name: str, index: int) -> str:
            value = result.get(name)
            return str(value[index]) if isinstance(value, list) and len(value) > index else ""

        s.check("M12", "missing credentials: the server's 401 becomes ConnectorUnavailable, and the hint shows how to set a header from a variable",
                got("nokey", 0) == "ConnectorUnavailable" and "401" in got("nokey", 1) and "headers" in got("nokey", 2) and "${" in got("nokey", 2), json.dumps(result.get("nokey"))[:400])
        s.check("M12", "a URL that is a web page, and a server that is not there, are reported as what they are",
                "404" in got("page", 1) and got("down", 0) == "ConnectorUnavailable" and "could not reach" in got("down", 1), json.dumps([result.get("page"), result.get("down")])[:500])
        s.check("M13", "an SSE connector pointed at something that never names its endpoint says so, and suggests the other transport",
                got("wrongkind", 0) == "ConnectorUnavailable" and "mcp-http" in got("wrongkind", 2), json.dumps(result.get("wrongkind"))[:400])
        s.check("M12 X2", "a call over its time limit fails as ToolTimeout in bounded time, and the other servers' failures did not stop this one working",
                got("slow", 0) == "ToolTimeout" and result.get("web") == {"sum": 2} and time.monotonic() - started < 25, "%.1fs %s" % (time.monotonic() - started, json.dumps(result.get("slow"))))
        s.check("M8", "the HTTP server was told that the caller gave up", len(state()["cancelled"]) >= 1)
    finally:
        fixture.kill()


def layout(s: Suite) -> None:
    s.section("U. A project's layout: the config found from a subfolder, cached tool lists, an edited server")
    project = WORK / "project"
    deep = project / "src" / "pkg"
    deep.mkdir(parents=True, exist_ok=True)
    (project / "tools").mkdir(exist_ok=True)
    s.mask(str(project), "<project-under-test>")

    def write_server(version: str, tools: List[str]) -> None:
        (project / "tools" / "server.py").write_text(TINY_SERVER % (version, tools), encoding="utf-8")

    def starts() -> int:
        return len((project / "starts.txt").read_text().splitlines()) if (project / "starts.txt").is_file() else 0

    # The server appends a line to starts.txt in its working directory each
    # time it is started: that is how the suite counts starts, and how it
    # sees which directory the server was started in.
    counted = TINY_SERVER.replace("for raw in sys.stdin.buffer:", "open('starts.txt', 'a').write('started\\n')\nfor raw in sys.stdin.buffer:")
    (project / "tools" / "server.py").write_text(counted % ("one", ["version"]), encoding="utf-8")
    (project / "codemode.json").write_text(json.dumps({
        "connectors": {"mine": {"type": "mcp-stdio", "command": [PY, "tools/server.py"]}},
        "policy": {"default": "allow"},
        "limits": {"startup_timeout_s": 10},
    }, indent=1), encoding="utf-8")
    main_py = str(ROOT / "main.py")

    r = s.raw([PY, main_py, "run", "--json", "tools.mine.version()"], cwd=deep)
    s.check("M15", "from a folder below the project root, with no --config, the config is found above and the server's relative path is read from the root",
            r.code == 0 and r.get("result") == "one", r.out[:300] + r.err[:300])
    s.check("M15", "the server ran in the project root, and the data directory is the root's, not one in the subfolder",
            starts() == 1 and (project / ".codemode" / "executions").is_dir() and not (deep / ".codemode").exists())
    r = s.raw([PY, main_py, "get", "ex_nope", "--json"], cwd=deep)
    s.check("M15 E2", "a hint from such a run names the config and the data directory it found, so the command can be pasted anywhere",
            r.code == 5 and "--config" in r.get("error", "hint", default="") and ".codemode" in r.get("error", "hint", default=""), r.out[:300])
    r = s.raw([PY, main_py, "--config", "../../codemode.json", "config", "--json"], cwd=deep)
    s.check("M15", "a config named with --config loads from anywhere", r.code == 0 and "mine" in r.get("connectors", default={}))
    # Changed on purpose: this check used to say that with --config the
    # server is NOT found from the subfolder. That was the limit people hit
    # (a "cwd" of "." worked from the project folder and failed from any
    # other), not a promise. The rule is now: the current folder first, as
    # before, and for a config that was named, the folder of the config next.
    named = ["--config", "../../codemode.json", "--home", "../../.codemode-explicit", "run", "--json"]
    ask = "try:\n    out = tools.mine.version()\nexcept ToolError as error:\n    out = [error.kind, error.hint]\nout"
    r = s.raw([PY, main_py] + named + [ask], cwd=deep)
    s.check("M15", "with --config, a relative path that is not in the current folder is read from the folder of the config: the server is found from the subfolder",
            r.get("result") == "one", r.out[:300])
    (deep / "tools").mkdir(exist_ok=True)
    (deep / "tools" / "server.py").write_text(counted % ("local", ["version"]), encoding="utf-8")
    r = s.raw([PY, main_py] + named + [ask], cwd=deep)
    s.check("M15", "and a file that is in the current folder still wins: the server next to the config is not the one that ran",
            r.get("result") == "local", r.out[:300])

    def named_config(name: str, value: Dict[str, Any]) -> List[str]:
        (project / (name + ".json")).write_text(json.dumps(dict(value, policy={"default": "allow"}), indent=1), encoding="utf-8")
        return ["--config", "../../%s.json" % name, "--home", "../../.codemode-explicit", "run", "--json"]

    cwd_run = named_config("cwd", {"connectors": {"mine": {"type": "mcp-stdio", "command": [PY, "server.py"], "cwd": "tools"}}, "limits": {"startup_timeout_s": 10}})
    r = s.raw([PY, main_py] + cwd_run + [ask], cwd=deep)
    s.check("M15", "a relative cwd is read from the current folder first: the server ran in the subfolder's tools folder",
            r.get("result") == "local" and (deep / "tools" / "starts.txt").is_file(), r.out[:300])
    shutil.rmtree(deep / "tools")
    r = s.raw([PY, main_py] + cwd_run + [ask], cwd=deep)
    s.check("M15", "with no such folder in the current one, the cwd is read from the folder of the config: the server ran in the project's tools folder",
            r.get("result") == "one" and (project / "tools" / "starts.txt").is_file(), r.out[:300])
    gone = named_config("nocwd", {"connectors": {"mine": {"type": "mcp-stdio", "command": [PY, "server.py"], "cwd": "nowhere"}}, "limits": {"startup_timeout_s": 10}})
    r = s.raw([PY, main_py] + gone + [ask], cwd=deep)
    said = str(r.get("result", 1, default=""))
    s.check("M15", "a folder that is in neither place is reported with both folders it looked in",
            r.get("result", 0) == "ConnectorUnavailable" and "(the current folder)" in said and "(the folder of the config)" in said
            and str(deep) in said and str(project) in said, r.out[:500])

    (project / "codemode_helpers").mkdir(exist_ok=True)
    (project / "codemode_helpers" / "hi.py").write_text("def twice(x):\n    return x * 2\n", encoding="utf-8")
    (project / "GUIDE.txt").write_text("Write programs in small steps. MARKER-OF-THE-GUIDE", encoding="utf-8")
    files = named_config("files", {"connectors": {"demo": {"type": "demo"}}, "helpers": "codemode_helpers", "guidance": {"file": "GUIDE.txt"}})
    r = s.raw([PY, main_py] + files + ["helpers.hi.twice(4)"], cwd=deep)
    s.check("M15", "the helpers folder is read from the folder of the config when it is not in the current one", r.get("result") == 8, r.out[:300])
    r = s.raw([PY, main_py, "--config", "../../files.json", "prompt"], cwd=deep)
    s.check("M15", "so is a guidance file", r.code == 0 and "MARKER-OF-THE-GUIDE" in r.out, r.out[:300] + r.err[:300])
    lost = named_config("lost", {"connectors": {"demo": {"type": "demo"}}, "helpers": "no_such_helpers", "guidance": {"file": "NO_SUCH_GUIDE.txt"}})
    r = s.raw([PY, main_py] + lost + ["1"], cwd=deep)
    s.check("M15", "a helpers folder that is in neither place names both folders in the error",
            r.code != 0 and "(the current folder)" in r.out and "(the folder of the config)" in r.out and "no_such_helpers" in r.out, r.out[:500])
    r = s.raw([PY, main_py, "--config", "../../lost.json", "prompt"], cwd=deep)
    s.check("M15", "and a guidance file that is in neither place does the same",
            "(the current folder)" in r.out + r.err and "(the folder of the config)" in r.out + r.err and "NO_SUCH_GUIDE" in r.out + r.err, r.out[:300] + r.err[:300])

    place = ("import os; print(os.path.basename(os.getcwd()))")
    shell = named_config("shell", {"connectors": {"ci": {"type": "commands", "cwd": "tools", "tools": {"where": {"command": [PY, "-c", place]}}}}})
    r = s.raw([PY, main_py] + shell + ["tools.ci.where()"], cwd=deep)
    s.check("M15", "the cwd of a command is read from the folder of the config too: it ran in the project's tools folder",
            r.code == 0 and "tools" in json.dumps(r.get("result")), r.out[:300])
    spec = named_config("spec", {"connectors": {"api": {"type": "openapi", "spec": "no_such_document.json"}}})
    r = s.raw([PY, main_py] + spec + ["tools.api.anything()"], cwd=deep)
    s.check("M15", "an OpenAPI document that is in neither place names both folders",
            "(the current folder)" in r.out and "(the folder of the config)" in r.out and "no_such_document.json" in r.out, r.out[:500])
    outside = str(WORK / "outside")
    r = s.raw([PY, main_py, "--home", outside, "run", "--json", "tools.demo.list_notes()"], cwd=ROOT / "tests")
    s.check("M15", "the search stops at the repository root: with no config in this repository, a run from tests/ uses the built-in demo config",
            r.code == 0 and len(r.get("result", default=[])) == 10, r.out[:200] + r.err[:200])
    r = s.raw([PY, main_py, "--home", outside, "run", "--json", "tools.mine.version()"], cwd=ROOT / "tests")
    s.check("M15", "and a namespace that is missing because no config was found is explained, with the command that writes one",
            r.code == 1 and "No codemode.json was found" in r.get("error", "hint", default="") and "init" in r.get("error", "hint", default=""), r.out[:400])

    before = starts()
    r = s.raw([PY, main_py, "search", "version", "--json"], cwd=project)
    r2 = s.raw([PY, main_py, "search", "version", "--json"], cwd=project)
    s.check("M14", "listing tools again does not start the server again: the list is cached on disk",
            r.code == 0 and r2.code == 0 and [h["path"] for h in (r2.json or [])] == ["mine.version"] and starts() == before, "starts %d -> %d" % (before, starts()))
    (project / "tools" / "server.py").write_text(counted % ("two!", ["version", "added_later"]), encoding="utf-8")
    r = s.raw([PY, main_py, "search", "", "--json"], cwd=project)
    s.check("M14", "editing the server's script drops the cached list at once: the new tool is listed",
            sorted(h["path"] for h in (r.json or [])) == ["mine.added_later", "mine.version"] and starts() == before + 1, r.out[:300])
    r = s.raw([PY, main_py, "run", "--json", "tools.mine.version()"], cwd=project)
    s.check("M14 M16", "and a program talks to the edited server", r.get("result") == "two!", r.out[:200])

    # The runtime as an MCP server, started in the project root as a client would start it.
    served = subprocess.Popen([PY, main_py, "serve"], cwd=str(project), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=s.env())

    def ask(request_id: int, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        served.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode())  # type: ignore[union-attr]
        served.stdin.flush()  # type: ignore[union-attr]
        return json.loads(served.stdout.readline() or b"{}")  # type: ignore[union-attr]

    try:
        ask(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}})
        first = ask(2, "tools/call", {"name": "execute", "arguments": {"code": "tools.mine.version()"}})
        (project / "tools" / "server.py").write_text(counted % ("three", ["version"]), encoding="utf-8")
        second = ask(3, "tools/call", {"name": "execute", "arguments": {"code": "tools.mine.version()"}})
        s.log()
        s.log("$ python main.py serve    (in the project folder; the server script is edited between two execute calls)")
        s.check("M16", "in a process that serves many executions, a server script edited between two of them is picked up by the second",
                first.get("result", {}).get("structuredContent", {}).get("result") == "two!"
                and second.get("result", {}).get("structuredContent", {}).get("result") == "three", json.dumps([first, second])[:500])
    finally:
        served.stdin.close()  # type: ignore[union-attr]
        try:
            served.wait(timeout=10)
        except subprocess.TimeoutExpired:
            served.kill()


def commands(s: Suite) -> None:
    s.section("V. A project's own commands as tools (the commands connector)")

    def py(code: str, *rest: Any) -> List[Any]:
        return [PY, "-c", code] + list(rest)

    s.config("cmds", {
        "connectors": {"ci": {
            "type": "commands",
            "env": {"FIXED": "from-config", "API_TOKEN": "${E2E_FIXTURE_TOKEN}"},
            "max_output_chars": 400,
            "tools": {
                "echo": {"description": "Print each argument on its own line.",
                         "command": py("import sys; print('\\n'.join(sys.argv[1:]))", "{text}", "{items}", {"if": "flag", "then": ["--flag", "{flag}"]}),
                         "params": {"text": {"type": "string"}, "items": {"type": "array", "default": []}, "flag": {"type": "string"}}},
                "env": {"description": "Print four environment variables as JSON.",
                        "command": py("import os, json; print(json.dumps({k: os.environ.get(k) for k in ('FIXED', 'CALLER_VAR', 'E2E_HOST_ONLY', 'API_TOKEN')}))"),
                        "allow_env": ["CALLER_*"]},
                "fail": {"command": py("import sys; print('some output'); sys.stderr.write('it broke\\n'); sys.exit(3)")},
                "lenient": {"command": py("import sys; sys.exit(3)"), "ok_exit_codes": [0, 3]},
                "noisy": {"command": py("for i in range(2000): print('line %04d' % i)")},
                "hang": {"command": py("import subprocess, sys, time; child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
                                       "print(child.pid, flush=True); time.sleep(120)"), "timeout_s": 1.5},
                "bytes": {"command": py("import sys; sys.stdout.buffer.write(b'one\\r\\ntwo\\r\\ncaf\\xe9 \\xff\\r\\n')")},
                "missing": {"command": ["definitely-not-a-real-binary-xyz", "--version"]},
            },
        }},
        "policy": {"default": "allow", "ask": ["ci.fail"]},
        "approval": {"require_terminal": False},
        "limits": {"timeout_s": 60, "call_timeout_s": 20},
    })
    env = {"E2E_FIXTURE_TOKEN": FAKE_TOKEN, "E2E_HOST_ONLY": "the host has this, the command must not"}
    nasty = 'a b; rm -rf "x" && echo $(id) | tee'

    args = s.program("cmd_args", '''
with_flag = tools.ci.echo(text=args["nasty"], items=["one", "two words", "-rf"], flag="-n")
without = tools.ci.echo(text="plain")
def refused(**arguments):
    try:
        tools.ci.echo(**arguments)
        return "no error"
    except ToolError as error:
        return [error.kind, error.message]
[with_flag["ok"], with_flag["stdout"].splitlines(), without["stdout"].splitlines(), refused(), refused(text=5), refused(text="x", shell=True),
 describe("ci.echo")["signature"], sorted(describe("ci.echo")["outputSchema"]["properties"])[:3]]
''')
    r = s.cli("run", "-f", args, "--arg", "nasty=" + nasty, "--json", config="cmds", home="cmds", env=env)
    result = r.get("result", default=[None] * 8)
    s.check("K2", "a value with spaces, quotes, ; and && reaches the command as one argument; a list is one argument per item",
            r.code == 0 and result[0] is True and result[1] == [nasty, "one", "two words", "-rf", "--flag", "-n"], r.out[:500])
    s.check("K2", "a conditional part is left out when its parameter is not given", result[2] == ["plain"])
    s.check("K3", "a missing, mistyped or undeclared argument is refused before anything runs",
            [x[0] if isinstance(x, list) else x for x in result[3:6]] == ["BadArguments"] * 3 and "text" in str(result[3]) and "shell" in str(result[5]), json.dumps(result[3:6])[:400])
    s.check("K1 P4", "each command is a tool with a signature from its declared parameters and a published result schema",
            result[6] == "ci.echo(text: str, items: list[str] = ..., flag: str = ...) -> dict" and result[7] == ["exit", "ok", "output_file"], json.dumps(result[6:]))

    envs = s.program("cmd_env", '''
import json
seen = json.loads(tools.ci.env(env={"CALLER_VAR": "set by the caller"})["stdout"])
try:
    tools.ci.env(env={"PATH": "/somewhere/else"})
    refused = "no error"
except ToolError as error:
    refused = [error.kind, error.hint]
[seen, refused]
''')
    r = s.cli("run", "-f", envs, "--json", config="cmds", home="cmds", env=env)
    seen = r.get("result", 0, default={})
    s.check("K4", "the command gets the config's variables and the ones the caller may set; a variable of the host is not passed along",
            seen.get("FIXED") == "from-config" and seen.get("CALLER_VAR") == "set by the caller" and seen.get("E2E_HOST_ONLY") is None, r.out[:400])
    s.check("K4", "a variable the project did not allow is refused, and the refusal says where that is decided",
            r.get("result", 1, 0) == "BadArguments" and "allow_env" in str(r.get("result", 1, 1)), json.dumps(r.get("result", 1))[:300])
    s.check("K10 C9", "a credential from the connector's environment that a command prints does not reach the program",
            FAKE_TOKEN not in r.out and seen.get("API_TOKEN") == "[redacted]", r.out[:300])

    r = s.cli("run", "--json", "tools.ci.fail()", config="cmds", home="cmds", env=env)
    s.check("K8 L1", "each command is its own name for the policy: one the policy asks about pauses", r.code == 3 and r.get("pending", 0, "tool") == "ci.fail")
    r = s.cli("approve", "last", "--json", config="cmds", home="cmds", env=env)
    result = r.get("result", default={})
    s.check("K8", "a command that exits with another code is a result, not an exception: ok false, the code, both outputs",
            r.code == 0 and (result.get("ok"), result.get("exit"), result.get("stdout"), result.get("stderr"), result.get("truncated"), result.get("output_file"))
            == (False, 3, "some output\n", "it broke\n", False, None), r.out[:400])
    r = s.cli("run", "--json", "out = tools.ci.lenient()\n[out['ok'], out['exit']]", config="cmds", home="cmds", env=env)
    s.check("K8", "a tool may declare other exit codes as fine", r.get("result") == [True, 3])

    r = s.cli("run", "--json", "out = tools.ci.noisy()\nfull = out['output_file']\n[out['truncated'], out['stdout_chars'], len(out['stdout']), "
              "out['stdout'].splitlines()[0], out['stdout'].splitlines()[-1], 'chars not shown' in out['stdout'], full]", config="cmds", home="cmds", env=env)
    result = r.get("result", default=[None] * 7)
    s.check("K5", "output over the budget keeps both ends, is marked as cut, and reports its real size",
            result[:6] == [True, 20000, result[2], "line 0000", "line 1999", True] and (result[2] or 9999) < 520, json.dumps(result)[:400])
    saved = str(result[6] or "")
    s.check("K5", "the whole output is in the file the result names",
            bool(saved) and os.path.isfile(saved) and Path(saved).read_bytes().count(b"line ") == 2000, saved)

    started = time.monotonic()
    r = s.cli("run", "--json", "out = tools.ci.hang()\n[out['ok'], out['timed_out'], out['exit'], out['stdout'].strip()]", config="cmds", home="cmds", env=env)
    result = r.get("result", default=[None, None, None, "0"])
    child = int(result[3]) if str(result[3]).isdigit() else 0
    s.check("K6", "a command over its time limit is stopped and says so, with the output it had produced",
            result[:3] == [False, True, None] and child > 0 and time.monotonic() - started < 10, "%.1fs %s" % (time.monotonic() - started, r.out[:300]))
    s.check("K6", "the process it had started is stopped with it", child > 0 and _gone(child, 5.0), "child pid %s" % child)
    if child and _alive(child):
        subprocess.run(["taskkill", "/F", "/PID", str(child)] if os.name == "nt" else ["kill", "-9", str(child)], capture_output=True)

    r = s.cli("run", "--json", "out = tools.ci.bytes()\ntry:\n    tools.ci.missing()\n    gone = 'no error'\nexcept ToolError as error:\n    gone = [error.kind, error.hint]\n[out['stdout'], gone]",
              config="cmds", home="cmds", env=env)
    text = str(r.get("result", 0, default=""))
    s.check("K9", "CRLF line ends are normalized and bytes that are not UTF-8 do not lose the output", text.startswith("one\ntwo\ncaf") and "\r" not in text, repr(text))
    s.check("K7", "a program that is not on PATH fails with what to check", r.get("result", 1, 0) == "CommandNotFound" and "PATH" in str(r.get("result", 1, 1)), r.out[:300])
    bad = dict(json.loads((WORK / "cmds.json").read_text(encoding="utf-8")))
    bad["connectors"] = {"ci": {"type": "commands", "tools": {"t": {"command": ["prog", "{undeclared}"]}}}}
    s.config("cmds_bad", bad)
    r = s.cli("config", "--json", config="cmds_bad", home="cmds")
    s.check("K1 G1", "a command that uses a parameter nobody declared is a config error (exit 2) that names it",
            r.code == 2 and "{undeclared}" in r.get("error", "message", default=""), r.out[:300])


def http_api(s: Suite) -> None:
    s.section("Z. An HTTP API as tools, from its OpenAPI document (the openapi connector)")
    import urllib.request

    fixture = subprocess.Popen([PY, "tests/fixtures/openapi_server.py", FAKE_TOKEN], cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    try:
        port = fixture.stdout.readline().strip()  # type: ignore[union-attr]
        base = "http://127.0.0.1:%s" % port
        s.mask(port, "<port>")
        s.log()
        s.log("(an HTTP API on 127.0.0.1:<port>, from tests/fixtures/openapi_server.py, with its description at /openapi.json;")
        s.log(" it wants a bearer token, or the same value as api_key in the query)")

        def fetch(path: str) -> Any:
            with urllib.request.urlopen(base + path, timeout=10) as response:
                return json.loads(response.read())

        def sent(route: str) -> int:
            return fetch("/state")["requests"].get(route, 0)

        # The same description as files: one as it is, one that is YAML, one that is Swagger 2.0.
        folder = WORK / "api"
        folder.mkdir(parents=True, exist_ok=True)
        document = fetch("/openapi.json")
        for name in ("notes.json", "nobase.json"):
            (folder / name).write_text(json.dumps(document), encoding="utf-8")
        (folder / "notes.yaml").write_text("openapi: 3.0.3\ninfo:\n  title: Notes\npaths: {}\n", encoding="utf-8")
        (folder / "old.json").write_text(json.dumps({"swagger": "2.0", "host": "127.0.0.1", "paths": {}}), encoding="utf-8")
        auth = {"Authorization": "Bearer ${E2E_API_TOKEN}"}
        s.config("api", {
            "connectors": {
                "api": {"type": "openapi", "spec": base + "/openapi.json", "headers": auth},
                "keyed": {"type": "openapi", "spec": base + "/openapi.json", "query": {"api_key": "${E2E_API_TOKEN}"}},
                "filed": {"type": "openapi", "spec": ".e2e/api/notes.json", "base_url": base + "/v1", "headers": auth},
                "nokey": {"type": "openapi", "spec": base + "/openapi.json"},
                "elsewhere": {"type": "openapi", "spec": base + "/elsewhere.json", "headers": auth},
                "yaml": {"type": "openapi", "spec": ".e2e/api/notes.yaml"},
                "old": {"type": "openapi", "spec": ".e2e/api/old.json"},
                "nobase": {"type": "openapi", "spec": ".e2e/api/nobase.json"},
                "nofile": {"type": "openapi", "spec": ".e2e/api/missing.json"},
                "down": {"type": "openapi", "spec": ".e2e/api/notes.json", "base_url": "http://127.0.0.1:9/v1"},
            },
            # The whole policy of an API in three lines, because the method is in the name.
            "policy": {"default": "ask", "allow": ["*.get_*", "api.post_form", "api.post_text", "api.post_tree", "api.post_upload", "api.post_fail"],
                       "deny": ["*.delete_*"]},
            "approval": {"require_terminal": False},
            "limits": {"timeout_s": 40, "call_timeout_s": 10, "startup_timeout_s": 5, "call_timeouts": {"api.get_slow": 1}},
        })
        env = {"E2E_API_TOKEN": FAKE_TOKEN}
        methods = ("get", "put", "post", "delete", "patch")

        reading = s.program("api_reading", '''
names = [hit["path"] for hit in search("", namespace="api", limit=100)]
echo = tools.api.get_echo_value(value="a/b?c=d#e f", q="x&y=z", tags=["one", "two"], flag=True, filter={"name": "n"}, **{"X-Trace": "t-1"})
[names, describe("api.get_notes_id"), tools.api.get_notes(limit=2), [note["id"] for note in tools.api.get_notes(tag=["a", "b"])],
 tools.api.get_notes_id(id=2), echo, [tools.api.get_a_b(), tools.api.get_a_b_2(), tools.api.get_root()],
 describe("api.post_tree")["inputSchema"], tools.filed.get_notes_id(id=3), describe("api.post_notes")["signature"]]
''')
        r = s.cli("run", "-f", reading, "--json", config="api", home="api", env=env)
        result = r.get("result", default=[None] * 10)
        names = [str(name).partition(".")[2] for name in (result[0] or [])]
        s.check("A1", "every operation is a tool whose name starts with its method, so a rule over names is a rule over methods",
                r.code == 0 and len(names) == 21 and all(name.partition("_")[0] in methods and "_" in name for name in names), r.out[:500])
        s.check("A11", "a path of the document that does not start with a slash is not a tool", bool(names) and not any("stolen" in name for name in names), json.dumps(names))
        described = result[1] or {}
        s.check("A2", "a parameter given once for a path, as a reference, is a parameter of each operation there, with its type and its description",
                described.get("signature") == "api.get_notes_id(id: int) -> dict"
                and described.get("inputSchema", {}).get("properties") == {"id": {"type": "integer", "description": "The number of the note."}}, json.dumps(described)[:500])
        s.check("A2", "the description of a tool starts with its route, and the answer's shape is outlined",
                str(described.get("description")).startswith("GET /notes/{id}: Read one note (operationId: readNote).")
                and sorted(described.get("outputSchema", {}).get("properties", {})) == ["id", "tags", "title"], json.dumps(described)[:500])
        s.check("A4", "a query value and a list of them reach the API: a limit is kept, and a list is sent as repeated values",
                [note["id"] for note in (result[2] or [])] == [1, 2] and result[3] == [3] and (result[4] or {}).get("title") == "second", json.dumps(result[2:5])[:300])
        echo = result[5] or {}
        s.check("A3", "a path value with a slash, a question mark, a hash and a space reaches the API as one segment",
                echo.get("path") == "/v1/echo/a%2Fb%3Fc%3Dd%23e%20f", json.dumps(echo))
        s.check("A4 A5", "a value with & and = stays one value; a joined list, a boolean and an object are sent as the document says; a declared header is sent",
                echo.get("query") == "q=x%26y%3Dz&tags=one%2Ctwo&flag=true&filter%5Bname%5D=n" and echo.get("trace") == "t-1", json.dumps(echo))
        s.check("A1", "two routes whose names come out the same are both callable and each reaches its own; the root path has a name",
                result[6] == [{"route": "/a-b"}, {"route": "/a_b"}, {"route": "root"}], json.dumps(result[6]))
        tree = (result[7] or {}).get("$defs", {}).get("Tree", {})
        s.check("A2", "a body whose schema refers to itself is described once, and the description ends",
                (result[7] or {}).get("properties", {}).get("body") == {"$ref": "#/$defs/Tree"}
                and tree.get("properties", {}).get("children", {}).get("items") == {"$ref": "#/$defs/Tree"}, json.dumps(result[7])[:400])
        s.check("A2", "a body that is a reference to a reference is a parameter with a type", result[9] == "api.post_notes(body: dict) -> dict", str(result[9]))
        s.check("A11", "a document in a file, with the address of the API in the config", (result[8] or {}).get("title") == "third", json.dumps(result[8]))
        seen = fetch("/state")
        s.check("A11", "the request for the document did not carry the connector's credentials",
                seen["document_requests"] >= 2 and seen["document_requests_with_credentials"] == 0, json.dumps(seen)[:300])

        refusing = s.program("api_refusing", '''
def attempt(function, **arguments):
    try:
        return function(**arguments)
    except ToolError as error:
        return [error.kind, error.message, error.hint]

{"dots": attempt(tools.api.get_echo_value, value=".."),
 "credential": attempt(tools.api.get_echo_value, value="x", Authorization="Bearer mine"),
 "unknown": attempt(tools.api.get_notes, nope=1),
 "missing": attempt(tools.api.get_notes_id),
 "no_body": attempt(tools.api.post_form),
 "upload": [describe("api.post_upload")["description"], attempt(tools.api.post_upload, body={"file": "x"})],
 "form": tools.api.post_form(body={"a": "1 2", "b": ["x", "y&z"]}),
 "text": tools.api.post_text(body="some words"),
 "tree": tools.api.post_tree(body={"name": "r", "children": [{"name": "c"}]}),
 "answers": [tools.api.get_text(), tools.api.get_bytes(), tools.api.get_nothing()],
 "404": attempt(tools.api.get_notes_id, id=99),
 "500": attempt(tools.api.get_fail),
 "500 after a write": attempt(tools.api.post_fail),
 "redirect": attempt(tools.api.get_redirect),
 "slow": attempt(tools.api.get_slow, seconds=6),
 "delete": attempt(tools.api.delete_notes_id, id=1)}
''')
        started = time.monotonic()
        r = s.cli("run", "-f", refusing, "--json", config="api", home="api", env=env)
        took = time.monotonic() - started
        result = r.get("result", default={})

        def got(name: str, index: int) -> str:
            value = result.get(name)
            return str(value[index]) if isinstance(value, list) and len(value) > index else ""

        s.check("A3", "a path value of .. is refused before anything is sent: it would name another route",
                r.code == 0 and got("dots", 0) == "BadArguments" and sent("GET /echo/..") == 0 and sent("GET /echo/") == 0, json.dumps(result.get("dots")))
        s.check("A5", "a header the config sets is not a parameter, though the document declares it: a program cannot replace the credential",
                got("credential", 0) == "BadArguments" and "Authorization" in got("credential", 1) and "Authorization" not in got("credential", 2), json.dumps(result.get("credential")))
        s.check("A7", "an argument the tool does not take, and a required one that is missing, fail before anything is sent and say what the tool takes",
                got("unknown", 0) == "BadArguments" and "limit, tag" in got("unknown", 2) and got("missing", 0) == "BadArguments" and "id" in got("missing", 1)
                and got("no_body", 0) == "BadArguments" and "body" in got("no_body", 1), json.dumps([result.get(k) for k in ("unknown", "missing", "no_body")])[:500])
        upload = result.get("upload") or ["", []]
        s.check("A6", "a body the connector cannot send: the tool says so in its description, and a call fails with nothing sent",
                "multipart/form-data" in str(upload[0]) and "cannot send" in str(upload[0]) and list(upload[1])[:1] == ["BadArguments"] and sent("POST /upload") == 0, json.dumps(upload)[:400])
        s.check("A6", "a form body, a text body and a JSON body each arrive as what they are",
                result.get("form") == {"type": "application/x-www-form-urlencoded", "fields": {"a": ["1 2"], "b": ["x", "y&z"]}}
                and result.get("text") == {"type": "text/plain", "text": "some words"} and result.get("tree") == {"depth": 2},
                json.dumps([result.get(k) for k in ("form", "text", "tree")])[:400])
        answers = result.get("answers") or [None, {}, "?"]
        s.check("A8", "text comes back as text, an empty answer as nothing, and bytes that are not text are described, not returned",
                answers[0] == "plain words\n" and answers[2] is None and (answers[1] or {}).get("bytes") == 4
                and (answers[1] or {}).get("content_type") == "application/octet-stream", json.dumps(answers))
        s.check("A9", "an error status is a failure the program catches, with the status, the route and the body",
                got("404", 0) == "ToolError" and got("404", 1) == 'HTTP 404 Not Found from GET /notes/{id}: {"error": "no note 99"}', json.dumps(result.get("404")))
        s.check("A9", "a long error body is cut and says by how much; after a write, the failure says the write may have happened",
                got("500", 1).endswith("[... 310 more chars not shown ...]") and got("500", 2).startswith("Call it as: ") and "describe(" in got("500", 2)
                and "check the target before you repeat" in got("500 after a write", 2), json.dumps([result.get("500"), result.get("500 after a write")])[-400:])
        s.check("A10", "a redirect is not followed, and the failure names the address",
                got("redirect", 0) == "ToolError" and "/v1/landing" in got("redirect", 1) and "not followed" in got("redirect", 1)
                and fetch("/state")["landed"] == 0 and sent("GET /landing") == 0, json.dumps(result.get("redirect")))
        s.check("A9 X2", "a call over its time limit fails as ToolTimeout in bounded time", got("slow", 0) == "ToolTimeout" and took < 25, "%.1fs %s" % (took, json.dumps(result.get("slow"))))
        s.check("A1 L1", "a deny rule over a method (*.delete_*) stops every delete, and nothing is sent",
                got("delete", 0) == "PolicyDenied" and "*.delete_*" in got("delete", 1) and sent("DELETE /notes/1") == 0, json.dumps(result.get("delete")))

        secrets = s.program("api_credentials", '''
def attempt(function, **arguments):
    try:
        return function(**arguments)
    except ToolError as error:
        return [error.kind, error.message, error.hint]

{"header": tools.api.get_whoami(), "query": tools.keyed.get_whoami(), "signature": describe("keyed.get_echo_value")["signature"],
 "replace": attempt(tools.keyed.get_echo_value, value="x", api_key="mine"), "nokey": attempt(tools.nokey.get_notes)}
''')
        r = s.cli("run", "-f", secrets, "--json", config="api", home="api", env=env)
        result = r.get("result", default={})
        s.check("A13 C9", "a credential the API echoes back, from a header or from the query, does not reach the program",
                r.code == 0 and FAKE_TOKEN not in r.out and (result.get("header") or {}).get("authorization") == "[redacted]"
                and (result.get("query") or {}).get("query") == "api_key=[redacted]", r.out[:400])
        s.check("A4", "a query value the config sets is not a parameter, though the document declares it, and a caller cannot give it",
                "api_key" not in str(result.get("signature")) and "value: str" in str(result.get("signature")) and got("replace", 0) == "BadArguments", json.dumps([result.get("signature"), result.get("replace")])[:400])
        s.check("A9", "missing credentials: the API's 401 is a failure whose hint shows how to set a header or a query value from a variable",
                got("nokey", 0) == "ToolError" and "HTTP 401" in got("nokey", 1) and "headers" in got("nokey", 2) and "${" in got("nokey", 2), json.dumps(result.get("nokey"))[:400])

        broken = s.program("api_documents", '''
out = {}
for namespace in ("elsewhere", "yaml", "old", "nobase", "nofile", "down"):
    try:
        getattr(tools, namespace).get_root()
        out[namespace] = "no error"
    except ToolError as error:
        out[namespace] = [error.kind, error.message, error.hint]
out["api"] = tools.api.get_root()
out
''')
        r = s.cli("run", "-f", broken, "--json", config="api", home="api", env=env)
        result = r.get("result", default={})
        s.check("A11", "a document fetched from one host that names another host for the API is refused until the config names that host",
                got("elsewhere", 0) == "ConnectorUnavailable" and "another host" in got("elsewhere", 1) and "127.0.0.1:9" in got("elsewhere", 1)
                and '"base_url"' in got("elsewhere", 2), json.dumps(result.get("elsewhere"))[:500])
        s.check("A12", "a document that is YAML, and one that is Swagger 2.0, are each reported as what they are, with what to do",
                "YAML" in got("yaml", 1) and "JSON" in got("yaml", 2) and "Swagger 2.0" in got("old", 1) and "OpenAPI 3" in got("old", 2),
                json.dumps([result.get("yaml"), result.get("old")])[:500])
        s.check("A11 A12", "a document in a file that names no address, and a file that is not there, each say which setting to change",
                got("nobase", 0) == "ConnectorUnavailable" and "base_url" in got("nobase", 2) and "could not be read" in got("nofile", 1)
                and "connectors.nofile.spec" in got("nofile", 2), json.dumps([result.get("nobase"), result.get("nofile")])[:500])
        s.check("A12 X2", "an API that is not there is ConnectorUnavailable, and none of these cost the working namespace anything",
                got("down", 0) == "ConnectorUnavailable" and "could not reach" in got("down", 1) and result.get("api") == {"route": "root"},
                json.dumps([result.get("down"), result.get("api")])[:400])

        writing = s.program("api_writing", '''
before = tools.api.get_notes(limit=50)
made = tools.api.post_notes(body={"title": "from a program", "tags": ["e2e"]})
[len(before), made["title"], made["id"]]
''')
        reads = sent("GET /notes")
        r = s.cli("run", "-f", writing, "--json", config="api", home="api", env=env)
        s.check("A15 L2", "a read is allowed by a rule over its method and a write is asked about: the program pauses and nothing was written",
                r.code == 3 and r.get("pending", 0, "tool") == "api.post_notes" and r.get("pending", 0, "args") == {"body": {"title": "from a program", "tags": ["e2e"]}}
                and sent("GET /notes") == reads + 1 and sent("POST /notes") == 0, r.out[:400])
        r = s.cli("approve", "last", "--json", config="api", home="api", env=env)
        s.check("A15 I2", "after approval the write is sent once, and the read before it is not sent to the API again",
                r.code == 0 and r.get("result") == [3, "from a program", 4] and sent("GET /notes") == reads + 1 and sent("POST /notes") == 1
                and fetch("/state")["created"] == [4], r.out[:300] + json.dumps(fetch("/state")["requests"])[:300])

        r = s.cli("search", "tree", "--namespace", "filed", "--json", config="api", home="api", env=env)
        cached = (WORK / "api" / "cache" / "tools-filed.json").is_file()
        edited = dict(document, paths=dict(document["paths"], **{"/added": {"get": {"summary": "Added to the document later.", "responses": {"200": {"description": "ok"}}}}}))
        (folder / "notes.json").write_text(json.dumps(edited), encoding="utf-8")
        r2 = s.cli("search", "added", "--namespace", "filed", "--json", config="api", home="api", env=env)
        s.check("A14", "the tool list of a document is kept on disk, and editing the document's file drops it at once: the new operation is listed",
                cached and [hit["path"] for hit in (r.json or [])] == ["filed.post_tree"] and [hit["path"] for hit in (r2.json or [])] == ["filed.get_added"],
                "cached=%s %s %s" % (cached, r.out[:200], r2.out[:200]))

        s.config("api_bad", {"connectors": {"api": {"type": "openapi", "base_url": base + "/v1"}}})
        r = s.cli("config", "--json", config="api_bad", home="api")
        s.check("A12", "an openapi connector that names no document is a config error (exit 2) with an example",
                r.code == 2 and "spec" in r.get("error", "message", default="") and "openapi" in r.get("error", "hint", default=""), r.out[:300])
    finally:
        fixture.kill()


def helper_modules(s: Suite) -> None:
    s.section("W. Project helpers: a project's own functions, in the same sandbox and under the same policy")
    project = WORK / "helpers_project"
    folder = project / "codemode_helpers"
    folder.mkdir(parents=True, exist_ok=True)
    s.mask(str(project), "<helpers-project>")

    def write(name: str, source: str) -> None:
        (folder / name).write_text(source, encoding="utf-8")
        s.log()
        s.log("--- codemode_helpers/%s" % name)
        s.log(source.rstrip())
        s.log("---")

    write("notes.py", '''"""Helpers over the notes store."""

def titles(limit=3):
    """The first titles, in order."""
    return [note["title"] for note in tools.demo.list_notes()][:limit]

def make(title):
    """Create a note. A write: the policy decides."""
    return tools.demo.create_note(title=title, body="made by a helper")

def remove(note_id):
    return tools.demo.delete_note(id=note_id)

def remember(key, value):
    state[key] = value
    return sorted(state)
''')
    report_source = '''from helpers import notes

VERSION = %r

def summary():
    """How many notes there are, and which version of this helper ran."""
    return {"count": len(notes.titles(100)), "version": VERSION}
'''
    write("report.py", report_source % "one")
    write("broken.py", "def half_written(:\n    pass\n")
    write("_private.py", 'raise RuntimeError("a private file must never be loaded")\n')
    (project / "codemode.json").write_text(json.dumps({
        "connectors": {"demo": {"type": "demo"}},
        "policy": {"default": "ask", "allow": ["demo.list_notes", "demo.read_note"], "deny": ["demo.delete_note"]},
        "approval": {"require_terminal": False},
        "helpers": "codemode_helpers",
    }, indent=1), encoding="utf-8")
    main_py = str(ROOT / "main.py")

    def run(*args: str) -> Result:
        return s.raw([PY, main_py] + list(args), cwd=project)

    r = run("run", "--json", "[helpers.notes.titles(2), helpers.report.summary()]")
    s.check("P5", "a program calls a project's helper, and helpers use each other",
            r.code == 0 and r.get("result", 0) == ["Kickoff: Lighthouse migration", "Incident 2291: duplicate invoices"]
            and r.get("result", 1) == {"count": 10, "version": "one"}, r.out[:400] + r.err[:300])
    s.check("P1", "the helper's tool calls crossed the gateway like any other: they are counted and logged", r.get("stats", "toolCalls") == 2)
    r = run("run", "--json", "try:\n    helpers.notes.remove(1)\n    out = 'removed'\nexcept ToolError as error:\n    out = error.kind\nout")
    s.check("P1 L1", "a call the policy denies is denied when a helper makes it", r.get("result") == "PolicyDenied", r.out[:300])
    r = run("run", "--json", "version = helpers.report.summary()['version']\ncreated = helpers.notes.make('from a helper')\n[version, created['id']]")
    s.check("P1 I2", "a call the policy asks about pauses the execution when a helper makes it, and nothing was written",
            r.code == 3 and r.get("pending", 0, "tool") == "demo.create_note" and not (project / ".codemode" / "demo" / "notes.json").exists(), r.out[:300])
    write("report.py", report_source % "two")
    r = run("approve", "last", "--json")
    s.check("P3", "a helper edited while the execution was paused does not change it: the resumed pass runs the helpers it started with",
            r.code == 0 and r.get("result") == ["one", 11], r.out[:300])
    r = run("run", "--json", "helpers.report.summary()['version']")
    s.check("P3", "the next program gets the edited helper", r.get("result") == "two")

    r = run("run", "--json", "1 + 1")
    s.check("P2", "a helper file that does not parse does not stop programs that do not use it", r.code == 0 and r.get("result") == 2)
    r = run("run", "--json", "helpers.broken.half_written()")
    s.check("P2", "a program that uses it gets a syntax error that names the helper file and the line",
            r.code == 1 and r.get("error", "type") == "SyntaxError" and "<helper:broken>" in r.get("error", "traceback", default="")
            and "line 1" in r.get("error", "message", default=""), r.out[:400])
    r = run("run", "--json", "try:\n    helpers.reports.summary()\nexcept AttributeError as error:\n    out = str(error)\nout")
    s.check("P7 P4", "asking for a module that does not exist lists the ones that do; a file that starts with an underscore is not one of them",
            "broken, notes, report" in str(r.get("result")) and "_private" not in str(r.get("result")), r.out[:300])
    r = run("run", "--json", "from helpers import notes\nnotes.remember('seen', 3)")
    r2 = run("run", "--json", "state['seen']")
    s.check("P5 P14", "a helper shares the program's state, and 'from helpers import name' works", r.get("result") == ["seen"] and r2.get("result") == 3, r.out[:200])

    r = run("run", "--json", "found = search('titles')\ninfo = describe('helpers.notes')\n[[h['signature'] for h in found], [h['policy'] for h in found], "
            "info['doc'], [f['name'] for f in info['functions']], describe('helpers.notes.make')['description']]")
    result = r.get("result", default=[[""], [""], "", [], ""])
    s.check("P6", "search() finds helper functions with their parameters, ranked by name like tools, and describe() shows a module and a function",
            result[0][:1] == ["helpers.notes.titles(limit=3)"] and result[1][:1] == ["helper"] and result[2] == "Helpers over the notes store."
            and result[3] == ["titles", "make", "remove", "remember"] and result[4].startswith("Create a note."), r.out[:500])
    r = run("run", "--json", "found = search(namespace='helpers')\n[sorted({hit['path'].split('.')[1] for hit in found}), sorted({hit['policy'] for hit in found}), found.more]")
    s.check("H13 P6", "search(namespace='helpers') lists the project's helper functions and nothing else, a file that does not parse included",
            r.get("result") == [["broken", "notes", "report"], ["helper"], 0], r.out[:300])
    r = run("instructions")
    listed = r.out.partition("### This project's helpers")[2]
    s.check("P8", "the instructions for this project's agents list its helper functions with their parameters and what each is for",
            r.code == 0 and "- `helpers.notes.titles(limit=3)`" in listed and "- `helpers.notes.make(title)`: Create a note." in listed, r.out[-600:])
    r = run("run", "--json", "import os\n[helpers.notes.project_root == project_root, os.path.isdir(project_root)]")
    s.check("H15 P6", "a helper is given the project's folder under the same name as the program", r.get("result") == [True, True], r.out[:300])
    r = run("search", "broken", "--json")
    s.check("P2 P6", "a helper file that does not parse is listed as such, not left out",
            r.code == 0 and any(h["path"] == "helpers.broken" and "does not parse" in h["description"] for h in (r.json or [])), r.out[:300])
    r = run("prompt")
    s.check("P22", "the tool description names the project's helper modules in one line, and stays short",
            "helpers.<module>.<function>()" in r.out and "notes" in r.out and len(r.out) < 2900, "%d chars" % len(r.out))

    (folder / "Bad-Name.py").write_text("x = 1\n", encoding="utf-8")
    r = run("run", "--json", "1")
    s.check("P4 G1", "a helper file whose name cannot be a module name is a config error (exit 2) that names it and suggests another",
            r.code == 2 and "Bad-Name.py" in r.get("error", "message", default="") and "bad_name.py" in r.get("error", "hint", default=""), r.out[:300])
    (folder / "Bad-Name.py").unlink()
    (project / "codemode.json").write_text(json.dumps({"connectors": {"demo": {"type": "demo"}}, "helpers": "no_such_folder"}), encoding="utf-8")
    r = run("run", "--json", "1")
    s.check("P4 G6", "a helpers folder that does not exist is a config error that says so", r.code == 2 and "does not exist" in r.get("error", "message", default=""), r.out[:300])


STORE_SERVER = '''import json, os, sys
# A small MCP server with a store of items in a file, and tools that say
# through annotations whether they read, write or destroy.
PATH = "items.json"
TOOLS = [
    {"name": "list_items", "description": "List the items.", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "count", "description": "How many items there are.", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}},
    {"name": "add_item", "description": "Add an item.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False}},
    {"name": "clear_all", "description": "Remove every item.", "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]
def items():
    return json.load(open(PATH)) if os.path.isfile(PATH) else []
for raw in sys.stdin.buffer:
    message = json.loads(raw)
    if "id" not in message:
        continue
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "store", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        current = items()
        if name == "add_item":
            current.append(args["name"])
            json.dump(current, open(PATH, "w"))
        elif name == "clear_all":
            json.dump([], open(PATH, "w"))
            current = []
        value = {"items": current, "total": len(current), "key_seen": bool(os.environ.get("STORE_API_KEY"))}
        result = {"content": [{"type": "text", "text": json.dumps(value)}], "structuredContent": value}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
'''


def new_project(s: Suite) -> None:
    s.section("X. A project that has never used the runtime: init, doctor, a first program")
    fresh = WORK / "fresh"
    (fresh / ".git").mkdir(parents=True, exist_ok=True)  # a repository, as far as the setup needs to know
    (fresh / "server").mkdir(exist_ok=True)
    (fresh / "server" / "store.py").write_text(STORE_SERVER, encoding="utf-8")
    (fresh / ".gitignore").write_bytes(b"build/\r\n")
    literal = "sk-written-into-the-server-list-0001"
    (fresh / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "item-store": {"command": PY, "args": ["server/store.py"], "env": {"STORE_API_KEY": literal, "STORE_MODE": "test"}},
        "codemode": {"command": "codemode", "args": ["serve"]},
    }}, indent=1), encoding="utf-8")
    s.mask(str(fresh), "<fresh-project>")
    main_py = str(ROOT / "main.py")
    key = {"ITEM_STORE_STORE_API_KEY": "exported-by-the-person-0001"}

    def run(*args: str, env: Optional[Dict[str, str]] = None, stdin: str = "") -> Result:
        return s.raw([PY, main_py] + list(args), cwd=fresh, env=env, stdin=stdin)

    r = run("init", "--json", env=key)
    written = (fresh / "codemode.json").read_text(encoding="utf-8") if (fresh / "codemode.json").is_file() else ""
    config = json.loads(written or "{}")
    s.check("U8 U1", "init writes codemode.json from the project's server list and checks the server it lists",
            r.code == 0 and r.get("connectors", "item_store", "tools") == 4 and config.get("connectors", {}).get("item_store", {}).get("command") == [PY, "server/store.py"],
            r.out[:500] + r.err[:300])
    s.check("U2", "a credential written out in the server list is not copied or printed: the config reads a variable, and init names it",
            literal not in written and literal not in r.out and config["connectors"]["item_store"]["env"] == {"STORE_API_KEY": "${ITEM_STORE_STORE_API_KEY}", "STORE_MODE": "test"}
            and r.get("export") == ["ITEM_STORE_STORE_API_KEY"], written[:400])
    s.check("U3", "the server's name becomes a namespace a program can write, and the runtime's own server is left out",
            sorted(config.get("connectors", {})) == ["item_store"] and any("own MCP server" in n for n in r.get("notes", default=[])))
    s.check("U4 L2", "the starting policy allows what the server marks read-only, asks about the rest and denies deletes",
            config.get("policy") == {"mode": "enforce", "default": "ask", "allow": ["item_store.list_items", "item_store.count"],
                                     "deny": ["*.delete_*"], "ask": ["item_store.clear_all"]}, json.dumps(config.get("policy")))
    ignore = (fresh / ".gitignore").read_bytes()
    s.check("U6", "the data folders are added to .gitignore, in the file's own line endings",
            ignore.startswith(b"build/\r\n") and b".codemode/\r\n" in ignore and b".codemode-work/\r\n" in ignore and b"\n\n" not in ignore.replace(b"\r\n", b""), repr(ignore))
    r = run("init", "--json", env=key)
    s.check("U5 I7", "init does not overwrite a config that exists (exit 2) and says how to check it or force it",
            r.code == 2 and "--force" in r.get("error", "hint", default="") and "doctor" in r.get("error", "hint", default=""))
    r = run("init", "--force", env=key)
    s.check("U5 U6", "init --force writes it again, and .gitignore is not touched twice",
            r.code == 0 and "already has" in r.out and (fresh / ".gitignore").read_bytes() == ignore, r.out[:300])

    r = run("doctor", "--json", env=key)
    checks = {c["check"]: c for c in r.get("checks", default=[])}
    s.check("U7 U8", "doctor passes on the fresh project: interpreter, config, data folder, sandbox, the server, the policy",
            r.code == 0 and r.get("ok") is True and checks.get("connector item_store", {}).get("status") == "ok"
            and checks.get("sandbox", {}).get("status") == "ok" and checks.get("policy", {}).get("status") == "ok", r.out[:600])
    r = run("doctor", "--json")
    failed = [c for c in r.get("checks", default=[]) if c["status"] == "fail"]
    s.check("U7 G5", "without the variable exported, doctor fails that connector (exit 1) and names the variable to export",
            r.code == 1 and len(failed) == 1 and "ITEM_STORE_STORE_API_KEY" in failed[0]["detail"] + failed[0].get("hint", ""), r.out[:500])

    r = run("run", "--json", "tools.item_store.count()", env=key)
    s.check("U8", "a first program runs: the allowed read goes through, and the server got its credential from the environment",
            r.code == 0 and r.get("result") == {"items": [], "total": 0, "key_seen": True}, r.out[:300])
    r = run("run", "--json", "tools.item_store.add_item(name='first')", env=key)
    s.check("U8 L2", "a write pauses: a project that was just set up runs nothing but reads without asking", r.code == 3 and not (fresh / "items.json").exists())
    r = run("approve", "last", "--json", env=key)
    s.check("U8 L13", "and the approval is for a person: with the config init wrote, approve needs a terminal",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and not (fresh / "items.json").exists(), r.out[:300])
    r = run("run", "--json", "try:\n    tools.item_store.delete_everything()\nexcept ToolError as error:\n    out = error.kind\nout", env=key)
    s.check("U8", "an unknown tool is still an unknown tool", r.get("result") == "UnknownTool")

    policy = dict(config["policy"], default="allow", allow=config["policy"]["allow"] + ["item_store.cuont"], deny=["*.delete_*", "item_store.clear-al"])
    (fresh / "codemode.json").write_text(json.dumps(dict(config, policy=policy, approval={"require_terminal": False})), encoding="utf-8")
    r = run("doctor", "--json", env=key)
    warnings = " | ".join(c["detail"] + " " + c.get("hint", "") for c in r.get("checks", default=[]) if c["status"] == "warn")
    s.check("U7", "doctor warns about rules that match no tool (a misspelt deny rule denies nothing), about default allow, and about approvals without a terminal",
            r.code == 0 and "item_store.cuont" in warnings and "item_store.clear-al" in warnings and "denies nothing" in warnings
            and "default allow" in warnings and "approve its own request" in warnings and "*.delete_*" not in warnings, warnings[:700])

    listed = len(run("executions", "--json", "--limit", "200", env=key).json or [])
    r = run("prune", "--max-executions", "1", "--dry-run", "--json", env=key)
    s.check("U9", "prune --dry-run says what it would remove and removes nothing",
            r.code == 0 and r.get("dryRun") is True and r.get("removed", default=0) >= 1 and len(run("executions", "--json", "--limit", "200", env=key).json or []) == listed, r.out[:300])
    r = run("prune", "--max-executions", "1", "--json", env=key)
    left = run("executions", "--json", "--limit", "200", env=key).json or []
    s.check("U9", "prune keeps the newest execution and the one that is paused, waiting for a person; the rest are gone",
            r.code == 0 and r.get("removed", default=0) >= 1 and sorted(row["status"] for row in left) == ["completed", "paused"], json.dumps([row["status"] for row in left]))

    r = run("instructions", env=key)
    s.check("U11", "instructions prints the block for agents with this project's own command line in it",
            r.code == 0 and "## Code mode" in r.out and "main.py run --json -f .codemode-work/" in r.out and "Never approve it yourself" in r.out, r.out[:300])
    shown = set()
    for line in r.out.splitlines():
        parts = line.split("main.py ", 1)
        if len(parts) == 2:
            words = parts[1].split()
            shown.add(words[0])
            shown.update(word for word in words if word.startswith("--"))
    usage = " ".join(s.raw([PY, main_py, command, "--help"], cwd=fresh).out for command in ("run", "wait", "get", "cancel"))
    s.check("U11", "every command and option the instructions show exists in the CLI",
            {"run", "wait", "get", "cancel"} <= shown and all(option in usage for option in shown if option.startswith("--")), str(sorted(shown)))
    r = run("instructions", "--mcp", env=key)
    s.check("U11", "instructions --mcp prints the form for an agent that uses the execute tool", r.code == 0 and '"background": true' in r.out and "main.py run" not in r.out)

    # A project that keeps such things in folders of its own.
    laid = WORK / "laid_out"
    (laid / ".git").mkdir(parents=True, exist_ok=True)
    (laid / "server").mkdir(exist_ok=True)
    (laid / "server" / "store.py").write_text(STORE_SERVER, encoding="utf-8")
    (laid / ".mcp.json").write_text(json.dumps({"mcpServers": {"store": {"command": PY, "args": ["server/store.py"]}}}), encoding="utf-8")
    s.mask(str(laid), "<laid-out-project>")
    where = ["--config", "tools/agents/codemode.json", "--home", "tools/agents/data"]
    r = s.raw([PY, main_py, "init", "--out", "tools/agents/codemode.json", "--home", "tools/agents/data", "--work-dir", "tools/agents/programs/", "--json"], cwd=laid)
    placed = laid / "tools" / "agents" / "codemode.json"
    laid_config = json.loads(placed.read_text(encoding="utf-8")) if placed.is_file() else {}
    s.check("U12", "init --out writes the config where the project keeps such things, creating the folder, and puts nothing in the project root",
            r.code == 0 and laid_config.get("work_dir") == "tools/agents/programs" and sorted(laid_config.get("connectors", {})) == ["store"]
            and not (laid / "codemode.json").exists() and not (laid / ".codemode").exists(), r.out[:400] + r.err[:300])
    lines = (laid / ".gitignore").read_text(encoding="utf-8").split() if (laid / ".gitignore").is_file() else []
    s.check("U12 U6", ".gitignore gets the folders this project uses, not the default names",
            "tools/agents/data/" in lines and "tools/agents/programs/" in lines and ".codemode/" not in lines and ".codemode-work/" not in lines, str(lines))
    printed = " ".join(r.get("next", default=[]))
    s.check("U12 E2", "the commands init prints name the config it wrote and the data folder, so that they work as pasted",
            printed.count("tools/agents/codemode.json") >= 2 and "--home" in printed and "run from the project root" in " ".join(r.get("notes", default=[])), printed[:400])
    r = s.raw([PY, main_py] + where + ["doctor", "--json"], cwd=laid)
    s.check("U12 U7", "doctor passes with that config and finds nothing missing from .gitignore",
            r.code == 0 and r.get("ok") is True and not any(c["check"] == "gitignore" for c in r.get("checks", default=[])), r.out[:500])
    r = s.raw([PY, main_py] + where + ["instructions"], cwd=laid)
    s.check("U12 U11", "the instructions name this project's folders: where programs go, and the folder to leave alone",
            r.code == 0 and "-f tools/agents/programs/<name>.py" in r.out and "`tools/agents/data/` folder" in r.out and ".codemode-work" not in r.out
            and "`.codemode/`" not in r.out, r.out[:300])
    r = s.raw([PY, main_py] + where + ["run", "--json", "tools.store.count()['total']"], cwd=laid)
    s.check("U12", "and a program runs against it", r.code == 0 and r.get("result") == 0, r.out[:300])
    r = s.raw([PY, main_py, "init", "--out", "elsewhere.json", "--home", str(WORK / "laid_out_data"), "--work-dir", str(WORK / "laid_out_programs"), "--json"], cwd=laid)
    s.check("U12 U6", "folders outside the project get no entry: there is nothing of theirs to keep out of the repository",
            r.code == 0 and "nothing to add" in r.get("gitignore", default="") and (laid / ".gitignore").read_text(encoding="utf-8").split() == lines, r.out[:300])

    extra = ["--home", "tools/agents/data", "--json"]
    r = s.raw([PY, main_py, "init", "--out", "tools/agents/second.json"] + extra, cwd=laid, env={"CODEMODE_COMMAND": "mytool code"})
    r2 = s.raw([PY, main_py, "init", "--out", "tools/agents/third.json"] + extra, cwd=laid, env={"CODEMODE_COMMAND": "runner codemode {options}"})
    told, told2 = " ".join(r.get("notes", default=[]) + r.get("next", default=[])), " ".join(r2.get("notes", default=[]) + r2.get("next", default=[]))
    s.check("U14 E8", "under a command of the project's own that finds the config by itself, init --out does not tell the person to pass --config; where the command takes it, it still does",
            r.code == 0 and "--config" not in told and "mytool code doctor" in told
            and r2.code == 0 and "Name it on every command with --config" in told2 and "runner codemode --config " in told2, told[:300] + " | " + told2[:200])

    # The same project has two things to tell its agents.
    plain = s.raw([PY, main_py] + where + ["prompt"], cwd=laid).out + s.raw([PY, main_py] + where + ["instructions"], cwd=laid).out
    (laid / "docs").mkdir(exist_ok=True)
    rule = "Never clear the store on a weekday."
    (laid / "docs" / "agents.md").write_text("Count the items before you add one.\n%s\n" % rule, encoding="utf-8")
    placed.write_text(json.dumps(dict(laid_config, guidance={"file": "docs/agents.md"}, limits={"max_guidance_chars": 300})), encoding="utf-8")
    r = s.raw([PY, main_py] + where + ["prompt"], cwd=laid)
    r2 = s.raw([PY, main_py] + where + ["instructions"], cwd=laid)
    r3 = s.raw([PY, main_py] + where + ["instructions", "--mcp"], cwd=laid)
    s.check("U13", "a project's own text for its agents reaches both places an agent reads: the tool description and the instructions, in both forms",
            r.code == 0 and "From this project:" in r.out and rule in r.out and "### In this project" in r2.out and rule in r2.out and rule in r3.out
            and "From this project:" not in plain and "### In this project" not in plain, r.out[-300:])
    (laid / "docs" / "agents.md").write_text("First line of a handbook.\n" + "Another rule that nobody needs before the first program. " * 20 + "\nTHE-LAST-LINE\n", encoding="utf-8")
    r = s.raw([PY, main_py] + where + ["prompt"], cwd=laid)
    r2 = s.raw([PY, main_py] + where + ["doctor", "--json"], cwd=laid)
    cut = {c["check"]: c for c in r2.get("checks", default=[])}.get("guidance", {})
    s.check("U13", "the file is read when it is needed, and a text over the limit is cut and says how much is missing and where the whole of it is",
            r.code == 0 and "First line of a handbook." in r.out and "THE-LAST-LINE" not in r.out and "more chars of this project's guidance are not shown" in r.out
            and "docs/agents.md" in r.out and "max_guidance_chars" in r.out, r.out[-400:])
    s.check("U13 U7", "doctor warns that it was cut", r2.code == 0 and cut.get("status") == "warn" and "cut to 300" in cut.get("detail", ""), json.dumps(cut)[:300])
    (laid / "docs" / "agents.md").unlink()
    r = s.raw([PY, main_py] + where + ["prompt"], cwd=laid)
    r2 = s.raw([PY, main_py] + where + ["instructions", "--json"], cwd=laid)
    r3 = s.raw([PY, main_py] + where + ["doctor", "--json"], cwd=laid)
    gone = {c["check"]: c for c in r3.get("checks", default=[])}.get("guidance", {})
    s.check("U13", "a guidance file that cannot be read does not cost an agent the tool: the description says so and tells it to tell the user",
            r.code == 0 and "could not be read" in r.out and "docs/agents.md" in r.out and "Tell the user" in r.out and "search(" in r.out, r.out[-300:])
    s.check("U13 I7", "the instructions command and doctor fail on it, each naming the file",
            r2.code == 2 and r2.get("error", "type") == "ConfigError" and "docs/agents.md" in r2.get("error", "message", default="")
            and r3.code == 1 and gone.get("status") == "fail" and "docs/agents.md" in gone.get("detail", ""), r2.out[:300] + json.dumps(gone)[:200])

    empty = WORK / "no_servers"
    empty.mkdir(exist_ok=True)
    r = s.raw([PY, main_py, "init", "--json"], cwd=empty)
    s.check("U5", "with no server list and no repository, init says where it looked and what to do instead (exit 2)",
            r.code == 2 and ".mcp.json" in r.get("error", "hint", default="") and "--from" in r.get("error", "hint", default=""), r.out[:300])
    if shutil.which("git"):
        made = subprocess.run(["git", "init", "-q", str(empty)], capture_output=True, text=True)
        r = s.raw([PY, main_py, "init", "--json"], cwd=empty)
        r2 = s.raw([PY, main_py, "run", "--json", "out = tools.repo.status()\n[out['ok'], out['exit']]"], cwd=empty)
        s.check("U5 K8", "in a repository with no server list, init writes a commands connector to start from, and its tools run",
                made.returncode == 0 and r.code == 0 and r.get("connectors", "repo", "tools") == 2 and r2.get("result") == [True, 0], r.out[:300] + r2.out[:300])
    else:
        s.skip("the commands starter that init writes in a repository (git is not installed)")

    modules = os.environ.get("CODEMODE_E2E_NODE_MODULES")
    entry = Path(modules or "") / "@modelcontextprotocol" / "server-filesystem" / "dist" / "index.js"
    if not modules or not shutil.which("node") or not entry.is_file():
        s.skip("a public MCP server (set CODEMODE_E2E_NODE_MODULES to a node_modules folder that has @modelcontextprotocol/server-filesystem)")
        return
    public = WORK / "public"
    data = public / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "hello.txt").write_text("hello from a file\n", encoding="utf-8")
    (public / ".mcp.json").write_text(json.dumps({"mcpServers": {"files": {"command": "node", "args": [str(entry), str(data)]}}}), encoding="utf-8")
    s.mask(str(entry), "<node_modules>/@modelcontextprotocol/server-filesystem/dist/index.js")
    s.mask(str(public), "<public-project>")
    r = s.raw([PY, main_py, "init", "--json"], cwd=public)
    allow = r.get("policy", "allow", default=[])
    s.check("U8 M11", "init on a project that lists the reference filesystem server: the server answers, and its read-only tools are allowed by name",
            r.code == 0 and r.get("connectors", "files", "tools", default=0) >= 8 and "files.list_directory" in allow and "files.write_file" not in allow, r.out[:600])
    program = "listing = tools.files.list_directory(path=args['data'])\ntext = tools.files.read_text_file(path=args['data'] + '/hello.txt')\n[shape(listing), shape(text), 'hello from a file' in str(text)]"
    r = s.raw([PY, main_py, "run", "--json", "--arg", "data=" + str(data).replace("\\", "/"), program], cwd=public)
    s.check("U8 M4", "a program lists a folder and reads a file through a server this project did not write", r.code == 0 and r.get("result", 2) is True, r.out[:500])
    r = s.raw([PY, main_py, "run", "--json", "--arg", "data=" + str(data).replace("\\", "/"), "tools.files.write_file(path=args['data'] + '/new.txt', content='x')"], cwd=public)
    s.check("U8 I2", "a write through that server pauses, and the file is not written", r.code == 3 and not (data / "new.txt").exists(), r.out[:300])


def policy_rules(s: Suite) -> None:
    s.section("O. Policy: server spellings, argument rules, approvals that need a terminal or expire")
    s.config("rules", chaos_config(policy={
        "default": "allow", "allow": [],
        "deny": ["chaos.dash_echo", {"tool": "chaos.echo", "args": {"value.kind": "secret*"}}],
        "ask": [{"tool": "chaos.counter", "args": {"name": "guard*"}}],
    }))
    tried = s.program("rules", '''
def attempt(path, args):
    try:
        return call(path, args)
    except ToolError as error:
        return error.kind
[attempt("chaos.dash_echo", {"value": 1}),
 attempt("chaos.echo", {"value": {"kind": "secret-plan"}}),
 attempt("chaos.echo", {"value": {"kind": "public"}}),
 attempt("chaos.echo", {"value": "no kind at all"}),
 attempt("chaos.counter", {"name": "free"})]
''')
    r = s.cli("run", "-f", tried, "--json", config="rules", home="rules")
    result = r.get("result", default=[None] * 5)
    s.check("L9", "a deny rule written with the server's spelling (dash-echo) stops the sanitized call (dash_echo)", result[0] == "PolicyDenied", r.out[:300])
    s.check("L11", "a rule with an argument pattern denies the matching call and lets the others through",
            result[1:4] == ["PolicyDenied", {"kind": "public"}, "no kind at all"] and result[4] == 1, r.out[:300])
    r = s.cli("run", "--json", "tools.chaos.counter(name='guarded')", config="rules", home="rules")
    s.check("L11 I2", "an ask rule with an argument pattern pauses the matching call, and names the rule",
            r.code == 3 and "guard*" in str(r.get("pending", 0, "rule")) and counter("rules", "guarded") == 0, r.out[:300])
    r = s.cli("describe", "chaos.echo", "--json", config="rules", home="rules")
    s.check("L15", "describe says when the decision for a tool depends on its arguments",
            r.get("policy") == "allow" and r.get("policyDependsOnArguments") is True, r.out[:200])

    s.config("flip", chaos_config())
    r = s.cli("run", "--json", "tools.gated.counter(name='flip')", config="flip", home="flip")
    s.config("flip", chaos_config(policy={"deny": ["gated.fail", "gated.counter"]}))
    r = s.cli("approve", "last", "--json", config="flip", home="flip")
    s.check("L10", "an approval given after the policy changed to deny the call does not run it",
            r.code == 1 and r.get("error", "type") == "PolicyDenied" and counter("flip", "flip") == 0, r.out[:300])
    s.check("L10", "the outcome says the policy changed and that the denied call must not be retried",
            "policy changed" in " ".join(r.get("warnings", default=[])) and "approval does not override" in r.get("error", "message", default=""), r.out[:400])

    s.config("strict", chaos_config(approval={"require_terminal": True}))
    r = s.cli("run", "--json", "tools.gated.counter(name='strict')", config="strict", home="strict")
    r = s.cli("approve", "last", "--json", config="strict", home="strict")
    s.check("L13", "approve from a process with no terminal is refused (exit 2) and the call does not run",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and counter("strict", "strict") == 0, r.out[:300])
    s.check("L13", "the refusal tells an agent to hand the command to the user", "do not approve your own request" in r.get("error", "hint", default=""))
    r = s.cli("approve", "last", "--json", config="strict", home="strict", stdin_is=("the null device", subprocess.DEVNULL))
    s.check("L16", "approve with the null device on stdin is refused like a pipe, though isatty() calls that device a terminal on Windows",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and counter("strict", "strict") == 0, r.out[:300])
    r = s.cli("get", "last", "--json", config="strict", home="strict")
    s.check("L13 I8", "the execution is still paused, waiting for a person", r.code == 3 and r.get("status") == "paused")
    if os.name == "nt":
        s.skip("approve at a real terminal (L16): a console cannot be given to a child process on Windows without opening a window")
    else:
        import pty

        controller, terminal = pty.openpty()
        try:
            r = s.cli("approve", "last", "--json", config="strict", home="strict", stdin_is=("a terminal", terminal))
        finally:
            os.close(controller)
            os.close(terminal)
        s.check("L16", "approve with a real terminal on stdin is accepted, and the call runs once",
                r.code == 0 and r.get("result") == 1 and counter("strict", "strict") == 1, r.out[:300])
    r = s.cli("run", "--interactive", "--json", "tools.gated.counter(name='strict_tty')", config="strict", home="strict", stdin="y\n")
    s.check("L13", "run --interactive with a piped 'y' is refused before anything runs",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and counter("strict", "strict_tty") == 0, r.out[:300])
    s.config("unset", {k: v for k, v in chaos_config().items() if k != "approval"})
    r = s.cli("run", "--json", "tools.gated.counter(name='unset')", config="unset", home="strict")
    r = s.cli("approve", "last", "--json", config="unset", home="strict")
    s.check("L13 L2", "a config that says nothing about approvals gets the safe default: a terminal is required",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and counter("strict", "unset") == 0)
    r = s.cli("reject", "last", "--json", config="unset", home="strict")
    s.check("L13", "reject needs no terminal: stopping is always allowed", r.code == 1 and r.get("status") == "rejected")

    s.config("ttl", chaos_config(approval={"require_terminal": False, "ttl_s": 1}))
    r = s.cli("run", "--json", "tools.gated.counter(name='old')", config="ttl", home="ttl")
    time.sleep(2.6)
    r = s.cli("approve", "last", "--json", config="ttl", home="ttl")
    s.check("L14", "a pause older than approval.ttl_s cannot be approved (exit 2), and the call does not run",
            r.code == 2 and r.get("error", "type") == "StateError" and "expire" in r.get("error", "message", default="") and counter("ttl", "old") == 0, r.out[:300])
    r = s.cli("approve", "last", "--stale", "--json", config="ttl", home="ttl")
    s.check("L14", "approve --stale is the person insisting: the call runs once", r.code == 0 and r.get("result") == 1 and counter("ttl", "old") == 1, r.out[:300])

    # Approvals given in advance, for one execution.
    s.config("standing", chaos_config(policy={"deny": ["gated.fail", {"tool": "gated.counter", "args": {"name": "forbidden"}}]}))
    many = s.program("many_gated", '''
first = tools.gated.counter(name="st")
second = call("gated.counter", {"name": "st"})
third = tools.gated.counter(name="st")
note = tools.demo.create_note(title="made after three counted calls", body="b")
[first, second, third, note["id"] > 0]
''')
    r = s.cli("run", "-f", many, "--json", config="standing", home="standing")
    r = s.cli("approve", "last", "--same-tool", "--json", config="standing", home="standing")
    grants = r.get("standingApprovals", default=[]) or [{}]
    s.check("L17", "approve --same-tool approves the pending call and, in advance, later calls to that tool in this execution: three ran on one approval",
            r.code == 3 and counter("standing", "st") == 3, r.out[:400])
    s.check("L17", "a call to another tool still pauses, and the outcome says what was approved in advance, by whom, until when and for how many calls",
            r.get("pending", 0, "tool") == "demo.create_note" and len(grants) == 1 and grants[0].get("tools") == ["gated.counter"]
            and grants[0].get("by") == "human" and grants[0].get("calls") == 2 and bool(grants[0].get("expires")), json.dumps(grants)[:300])
    r = s.cli("approve", "last", "--json", config="standing", home="standing")
    s.check("L17 I2", "approving the other call completes the program, and nothing ran twice",
            r.code == 0 and r.get("result") == [1, 2, 3, True] and counter("standing", "st") == 3, r.out[:300])
    log = s.cli("show", "last", "--json", config="standing", home="standing", quiet=True).json or []
    given = [e for e in log if e.get("type") == "approval"]
    under = [e for e in given if e.get("via") == "standing"]

    def before_its_call(entry: Dict[str, Any]) -> bool:
        started = [i for i, e in enumerate(log) if e.get("type") == "call" and e.get("seq") == entry["seqs"][0] and e.get("state") == "executing"]
        return bool(started) and log.index(entry) < started[0]

    s.check("L17 I2", "each call that ran under the approval in advance has an approval entry of its own, written before the call, naming the person and the approval",
            bool(given) and given[0].get("standing") == ["gated.counter"] and [e["seqs"] for e in under] == [[2], [3]]
            and all(e.get("by") == "human" and e.get("granted") == given[0].get("ts") and before_its_call(e) for e in under),
            json.dumps([{k: e.get(k) for k in ("seqs", "by", "standing", "via")} for e in given]))

    r = s.cli("run", "-f", many, "--json", config="standing", home="standing")
    s.check("L17", "the approval was for that execution: the same program run again pauses at its first call",
            r.code == 3 and r.get("pending", 0, "seq") == 1 and "standingApprovals" not in (r.json or {}) and counter("standing", "st") == 3, r.out[:300])
    r = s.cli("approve", "last", "--all-remaining", "--json", config="standing", home="standing")
    s.check("L17", "approve --all-remaining covers every later call the policy asks about: the program completes on one approval",
            r.code == 0 and r.get("result") == [4, 5, 6, True] and r.get("standingApprovals", 0, "tools") == ["*"]
            and r.get("standingApprovals", 0, "calls") == 3, r.out[:400])
    forbidden = s.program("gated_then_forbidden", '''
done = tools.gated.counter(name="st2")
try:
    tools.gated.counter(name="forbidden")
    refused = "it ran"
except ToolError as error:
    refused = error.kind
[done, refused]
''')
    r = s.cli("run", "-f", forbidden, "--json", config="standing", home="standing")
    r = s.cli("approve", "last", "--all-remaining", "--json", config="standing", home="standing")
    s.check("L17 L1", "an approval in advance never covers a call a deny rule forbids, not even one for every later call",
            r.code == 0 and r.get("result") == [1, "PolicyDenied"] and counter("standing", "forbidden") == 0, r.out[:300])

    s.config("standing_ttl", chaos_config(approval={"require_terminal": False, "ttl_s": 2}))
    slowly = s.program("gated_slowly", '''
import time
first = tools.gated.counter(name="exp")
time.sleep(3.3)
second = tools.gated.counter(name="exp")
[first, second]
''')
    r = s.cli("run", "-f", slowly, "--json", config="standing_ttl", home="standing")
    r = s.cli("approve", "last", "--same-tool", "--json", config="standing_ttl", home="standing")
    s.check("L17 L14", "an approval in advance is good for as long as any approval (approval.ttl_s): after that the next call pauses again",
            r.code == 3 and r.get("pending", 0, "seq") == 2 and counter("standing", "exp") == 1, r.out[:300])
    s.cli("reject", "last", "--json", config="standing_ttl", home="standing", quiet=True)

    s.config("standing_narrow", chaos_config(approval={"require_terminal": False, "standing": "same-tool"}))
    s.config("standing_off", chaos_config(approval={"require_terminal": False, "standing": "off"}))
    r = s.cli("run", "--json", "tools.gated.counter(name='narrow')", config="standing_narrow", home="standing")
    r = s.cli("approve", "last", "--all-remaining", "--json", config="standing_narrow", home="standing")
    r2 = s.cli("approve", "last", "--same-tool", "--json", config="standing_off", home="standing")
    s.check("L17", "a project can narrow approvals in advance, or turn them off: what it does not allow is refused (exit 2) before anything runs",
            r.code == 2 and r.get("error", "type") == "StateError" and "approval.standing" in r.get("error", "message", default="")
            and "--same-tool" in r.get("error", "hint", default="") and r2.code == 2 and r2.get("error", "type") == "StateError"
            and counter("standing", "narrow") == 0, r.out[:300] + r2.out[:300])
    r = s.cli("approve", "last", "--same-tool", "--json", config="standing_narrow", home="standing")
    s.check("L17", "and what it does allow works", r.code == 0 and r.get("result") == 1 and r.get("standingApprovals", 0, "tools") == ["gated.counter"], r.out[:300])
    r = s.cli("run", "--json", "tools.gated.counter(name='strict_all')", config="strict", home="strict")
    r = s.cli("approve", "last", "--all-remaining", "--json", config="strict", home="strict")
    s.check("L17 L13", "an approval in advance needs a person at a terminal, like any approval",
            r.code == 2 and r.get("error", "type") == "TerminalRequired" and counter("strict", "strict_all") == 0, r.out[:300])


def log_invariants(s: Suite) -> None:
    s.section("M. Every execution log written by this run (I1)")
    # Every data directory under the working folder, at any depth: some
    # scenarios run in a project folder of their own.
    files = sorted(WORK.rglob("executions/*.jsonl"))
    bad = []
    statuses: Dict[str, int] = {}
    for path in files:
        entries = [json.loads(line) for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
        kinds = [e["type"] for e in entries]
        problems = []
        if kinds.count("start") != 1 or kinds[0] != "start":
            problems.append("start")
        if kinds.count("end") > 1:
            problems.append("more than one end")
        if "end" in kinds and any(k in ("call", "pass_start", "approval") for k in kinds[kinds.index("end"):]):
            problems.append("activity after end")
        for e in entries:
            if e["type"] == "call" and e["kind"] == "tool" and e["state"] == "applied" and e.get("reused_from"):
                # A reused step did not run in this execution, so there is no
                # write-ahead entry to look for here. What must hold instead:
                # the execution it names really applied the same request.
                source = path.with_name(e["reused_from"] + ".jsonl")
                origin = [json.loads(line) for line in source.read_text(encoding="ascii").splitlines() if line.strip()] if source.is_file() else []
                if not any(p["type"] == "call" and p["seq"] == e["seq"] and p["state"] == "applied" and p["fp"] == e["fp"] for p in origin):
                    problems.append("seq %d reused from %s, which did not apply that request" % (e["seq"], e["reused_from"]))
                continue
            if e["type"] == "call" and e["kind"] == "tool" and e["state"] == "applied":
                seq = e["seq"]
                asked = any(p["type"] == "call" and p["seq"] == seq and p["state"] == "pending" for p in entries)
                approved = any(p["type"] == "approval" and seq in p["seqs"] for p in entries[: entries.index(e)])
                if asked and not approved:
                    problems.append("seq %d applied without approval" % seq)
                # A call can be asked about without ever being recorded as
                # pending: one approved in advance, or at the terminal as it
                # came. It needs an approval entry before it all the same.
                if e.get("decision") == "ask" and e.get("enforced", True) and not approved:
                    problems.append("seq %d was asked about and ran with no approval entry before it" % seq)
                intent = any(p["type"] == "call" and p["seq"] == seq and p["state"] == "executing" for p in entries[: entries.index(e)])
                if not intent:
                    problems.append("seq %d applied without a write-ahead entry" % seq)
        status = next((e["status"] for e in entries if e["type"] == "end"), "open")
        statuses[status] = statuses.get(status, 0) + 1
        if problems:
            bad.append("%s: %s" % (path.name, ", ".join(problems)))
    s.log("%d execution logs: %s" % (len(files), ", ".join("%s=%d" % kv for kv in sorted(statuses.items()))))
    s.check("I1", "%d logs: one start each, at most one end, nothing after the end" % len(files), files and not bad, "; ".join(bad[:3]))
    s.check("I2 P10", "in every log, a call that was asked about was approved before it ran, and every applied call has a write-ahead entry", not bad, "; ".join(bad[:3]))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    suite = Suite()
    suite.log("codemode-runtime end-to-end trace")
    suite.log("Generated by: python tests/e2e.py    (host Python %s, %s)" % (sys.version.split()[0], sys.platform))
    suite.log("Commands are relative to the project root. 'python' is the interpreter that ran the suite.")
    suite.log()
    suite.log("The config most scenarios use: two fault-injection namespaces, one allowed and one that asks,")
    suite.log("and limits small enough to hit on purpose.")
    suite.config("chaos", chaos_config())
    scenarios = [basics, discovery, semantics, isolation, approval, replay_faults, faults, other_interpreter,
                 mcp_server, mcp_tool, mcp_connector, snippets, agent_loop, entry_points, policy_rules, program_api, calls, long_runs,
                 servers, remote, layout, commands, http_api, helper_modules, new_project, log_invariants]
    update_trace = "--update-trace" in sys.argv[1:]
    only = [arg for arg in sys.argv[1:] if arg != "--update-trace"]
    for scenario in scenarios:
        if only and scenario.__name__ not in only and scenario is not log_invariants:
            continue
        try:
            scenario(suite)
        except Exception as exc:  # a crash in the suite is a failed check, with the trace kept
            import traceback

            suite.check("suite", "%s ran to the end" % scenario.__name__, False, traceback.format_exc()[-800:])
    passed = sum(1 for c in suite.checks if c["ok"])
    suite.log()
    suite.log("=" * 72)
    summary = "%d of %d checks passed, %d scenario(s) skipped" % (passed, len(suite.checks), len(suite.skipped))
    suite.log(summary)
    for what in suite.skipped:
        suite.log("skipped: " + what)
    for c in suite.checks:
        if not c["ok"]:
            suite.log("FAILED: %s %s" % (c["ids"], c["name"]))
    where = ""
    if not only:
        text = "\n".join(suite.lines) + "\n"
        WORK_TRACE.parent.mkdir(parents=True, exist_ok=True)
        WORK_TRACE.write_text(text, encoding="utf-8")
        where = ".e2e/e2e-trace.txt"
        if update_trace:
            TRACE.parent.mkdir(parents=True, exist_ok=True)
            TRACE.write_text(text, encoding="utf-8")
            where += " and docs/e2e-trace.txt"
    print("\n" + summary + ("" if not where else "    trace: " + where))
    return 0 if passed == len(suite.checks) else 1


if __name__ == "__main__":
    sys.exit(main())
