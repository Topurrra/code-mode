#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""End-to-end suite: the real CLI, as a subprocess, and nothing mocked.

Run it from the project root:

    python tests/e2e.py

It writes ``docs/e2e-trace.txt``: every command it ran, the exit code and the
output. The commands in the trace are relative to the project root and use
the ``last`` alias instead of generated ids, so they can be pasted and run
again in order. Working files go to ``.e2e/``, which is wiped at the start.

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
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".e2e"
TRACE = ROOT / "docs" / "e2e-trace.txt"
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
    ) -> Result:
        argv = ["main.py"]
        if home:
            argv += ["--home", ".e2e/" + home]
        if config:
            argv += ["--config", ".e2e/%s.json" % config]
        argv += list(args)
        started = time.monotonic()
        proc = subprocess.run(
            [PY] + argv, cwd=str(ROOT), input=stdin, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=self.env(env),
        )
        result = Result(proc.returncode, proc.stdout, proc.stderr)
        result.seconds = time.monotonic() - started  # type: ignore[attr-defined]
        if not quiet:
            shown_env = "".join("%s=%s " % (k, v) for k, v in (env or {}).items())
            self.log()
            self.log("$ %spython %s" % (shown_env, " ".join(_quote(a) for a in argv)))
            if stdin:
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
    r = s.cli("prompt", config="chaos")
    s.check("P22", "the tool description is short and contains no tool schema",
            r.code == 0 and len(r.out) < 2600 and "read_note" not in r.out and "demo (6 tools)" in r.out, "%d chars" % len(r.out))


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
    r = s.cli("run", "--json", "return 5", config="chaos")
    s.check("X10", "top-level return is a SyntaxError with a hint about the trailing expression",
            r.code == 1 and r.get("error", "type") == "SyntaxError" and "expression" in r.get("error", "hint", default=""))
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
    candidates = [[name] for name in ("python3.9", "python3.10", "python3.11", "python3.12", "python3.13", "python3.14")]
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
    s.check("P4", "its signature is generated from the schema the server published",
            r.get(0, "signature") == "inner.execute(code: str = ..., executionId: str = ..., session: str = ...) -> Any", str(r.get(0, "signature")))
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


def policy_rules(s: Suite) -> None:
    s.section("O. Policy: server spellings, argument rules, approvals that need a terminal or expire")
    s.config("rules", chaos_config(policy={
        "default": "allow", "allow": [],
        "deny": ["chaos.dash-echo", {"tool": "chaos.echo", "args": {"value.kind": "secret*"}}],
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
    r = s.cli("get", "last", "--json", config="strict", home="strict")
    s.check("L13 I8", "the execution is still paused, waiting for a person", r.code == 3 and r.get("status") == "paused")
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


def log_invariants(s: Suite) -> None:
    s.section("M. Every execution log written by this run (I1)")
    files = sorted(WORK.glob("*/executions/*.jsonl"))
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
            if e["type"] == "call" and e["kind"] == "tool" and e["state"] == "applied":
                seq = e["seq"]
                asked = any(p["type"] == "call" and p["seq"] == seq and p["state"] == "pending" for p in entries)
                approved = any(p["type"] == "approval" and seq in p["seqs"] for p in entries[: entries.index(e)])
                if asked and not approved:
                    problems.append("seq %d applied without approval" % seq)
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
                 mcp_server, mcp_connector, snippets, agent_loop, entry_points, policy_rules, log_invariants]
    only = sys.argv[1:]
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
    if not only:
        TRACE.parent.mkdir(parents=True, exist_ok=True)
        TRACE.write_text("\n".join(suite.lines) + "\n", encoding="utf-8")
    print("\n" + summary + ("" if only else "    trace: docs/e2e-trace.txt"))
    return 0 if passed == len(suite.checks) else 1


if __name__ == "__main__":
    sys.exit(main())
