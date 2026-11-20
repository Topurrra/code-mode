# SPDX-License-Identifier: MIT
"""The command line.

Built for two readers. A human gets short text. An agent passes ``--json`` and
gets one JSON value on stdout, plus an exit code that says which outcome
happened without parsing anything:

    0 completed      1 failed or rejected    2 usage or config error
    3 paused         4 verify mismatch       5 not found

``--json``, ``--config``, ``--home`` and ``--agent`` are accepted before or
after the command.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Dict, Optional

from . import __version__
from .config import load_config, resolve_agent, resolve_home
from .errors import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PAUSED,
    EXIT_VERIFY_MISMATCH,
    CodemodeError,
    UsageError,
)
from .jsonutil import pretty
from .runtime import Runtime, summarize_stats

STATUS_EXIT = {"completed": EXIT_OK, "running": EXIT_OK, "paused": EXIT_PAUSED, "error": EXIT_FAILED, "rejected": EXIT_FAILED}

_EPILOG = """\
exit codes: 0 completed, 1 failed or rejected, 2 usage or config error,
            3 paused for approval, 4 verify mismatch, 5 not found
with no command, the zero-config demo runs."""


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _print_json(value: Any) -> None:
    _out(json.dumps(value, ensure_ascii=True))


def _print_payload(payload: Dict[str, Any]) -> None:
    _out("status: %s    execution: %s" % (payload.get("status"), payload.get("executionId")))
    if "result" in payload:
        _out("result:")
        _out(pretty(payload["result"]) if not isinstance(payload["result"], str) else payload["result"])
    for key in ("resultTruncated", "resultOmitted", "resultIsRepr", "verification"):
        if payload.get(key) not in (None, False, "none"):
            _out("%s: %s" % (key, payload[key]))
    for item in payload.get("pending", []):
        _out("pending step %s: %s %s  (rule: %s)" % (item["seq"], item["tool"], json.dumps(item["args"], ensure_ascii=True), item.get("rule")))
    error = payload.get("error")
    if error:
        _out("error: %s: %s" % (error.get("type"), error.get("message")))
        if error.get("traceback"):
            _out(error["traceback"].rstrip())
        if error.get("hint"):
            _out("hint: %s" % error["hint"])
    for warning in payload.get("warnings", []):
        _out("warning: %s" % warning)
    if payload.get("logs"):
        _out("logs:")
        _out(payload["logs"].rstrip("\n"))
    if payload.get("hint"):
        _out("hint: %s" % payload["hint"])
    if payload.get("stats"):
        _out("stats: %s" % summarize_stats(payload["stats"], payload))


def _finish(payload: Dict[str, Any], as_json: bool) -> int:
    if as_json:
        _print_json(payload)
    else:
        _print_payload(payload)
    return STATUS_EXIT.get(payload.get("status", ""), EXIT_FAILED)


def _tty_approver(request: Dict[str, Any]) -> Optional[bool]:
    """Ask the person at the terminal. Anything but yes is a refusal: the
    safe answer must be the default one."""
    sys.stderr.write(
        "\napproval needed for step %s: %s\narguments: %s\napprove? [y/N] "
        % (request["seq"], request["tool"], json.dumps(request["args"], ensure_ascii=True)[:2000])
    )
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _read_code(args: argparse.Namespace) -> str:
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise UsageError("cannot read %s: %s" % (args.file, exc))
    if args.code and args.code != "-":
        return args.code
    if args.code == "-" or not sys.stdin.isatty():
        if getattr(args, "interactive", False):
            raise UsageError(
                "--interactive needs stdin for the approval prompt", hint="Pass the program as an argument or with -f."
            )
        return sys.stdin.read()
    raise UsageError("no program given", hint="python main.py run \"tools.demo.list_notes()\"  (or -f FILE, or - for stdin)")


# -- commands -------------------------------------------------------------


def cmd_run(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    code = _read_code(args)
    approver = _tty_approver if args.interactive else None
    return _finish(runtime.execute(code, args.session, approver=approver), as_json)


def cmd_get(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    return _finish(runtime.get(args.id, full=args.full), as_json)


def cmd_approve(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    return _finish(runtime.approve(args.id, by=args.by), as_json)


def cmd_reject(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    return _finish(runtime.reject(args.id, by=args.by, reason=args.reason), as_json)


def cmd_verify(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    report = runtime.verify(args.id)
    if as_json:
        _print_json(report)
    else:
        _out("%s: %s" % (report["verification"], report["detail"]))
    return EXIT_OK if report["verified"] else EXIT_VERIFY_MISMATCH


def cmd_executions(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    rows = runtime.list_executions(args.limit)
    if as_json:
        _print_json(rows)
    elif not rows:
        _out("no executions yet")
    else:
        for row in rows:
            _out("%(executionId)s  %(status)-9s  session=%(session)s  agent=%(agent)s  calls=%(toolCalls)d  verified=%(verification)s" % row)
    return EXIT_OK


def cmd_show(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    """The audit log of one execution, entry by entry."""
    entries = runtime.store.read(runtime.store.resolve_id(args.id))
    if as_json:
        _print_json(entries)
        return EXIT_OK
    for entry in entries:
        kind = entry.get("type")
        if kind == "start":
            _out("%s  session=%s  agent=%s  policy=%s" % (entry["id"], entry["session"], entry["agent"], entry["policy_mode"]))
        elif kind == "pass_start":
            _out("pass %s" % entry["pass"])
        elif kind == "call":
            detail = ""
            if entry.get("decision"):
                detail = "%s(%s)%s" % (entry["decision"], entry.get("rule"), "" if entry.get("enforced", True) else " not enforced")
            if entry.get("state") in ("error", "denied"):
                detail += "  %s" % (entry.get("error") or {}).get("type")
            if "duration_ms" in entry:
                detail += "  %d ms" % entry["duration_ms"]
            if entry.get("result_bytes"):
                detail += "  %d B" % entry["result_bytes"]
            _out("  %3d  %-8s %-28s %-9s %s" % (entry["seq"], entry["kind"], entry["name"][:28], entry["state"], detail))
        elif kind == "approval":
            _out("  approval of step(s) %s by %s" % (", ".join(str(s) for s in entry["seqs"]), entry["by"]))
        elif kind == "pass_end":
            _out("  pass ended: %s" % entry["status"])
        elif kind == "end":
            _out("end: %s" % entry["status"])
        elif kind == "verify":
            _out("verify: %s %s" % ("ok" if entry["ok"] else "FAILED", entry.get("detail", "")))
    return EXIT_OK


def cmd_search(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    hits = runtime.search(args.query, args.limit)
    if as_json:
        _print_json(hits)
    else:
        for hit in hits:
            _out("%s    [%s]" % (hit["signature"], hit["policy"]))
            if hit["description"]:
                _out("    %s" % hit["description"])
    return EXIT_OK


def cmd_describe(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    from .errors import NotFoundError, ToolFailure

    try:
        detail = runtime.describe(args.target)
    except ToolFailure as failure:
        raise NotFoundError(failure.message)
    _print_json(detail) if as_json else _out(pretty(detail))
    return EXIT_OK


def cmd_prompt(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    """Print the tool description, for a harness that wants to register the
    tool itself instead of going through MCP."""
    text = runtime.tool_description()
    _print_json({"name": "execute", "description": text}) if as_json else _out(text)
    return EXIT_OK


def cmd_config(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    detail = dict(runtime.config.describe(), home=str(runtime.home), agent=runtime.agent)
    _print_json(detail) if as_json else _out(pretty(detail))
    return EXIT_OK


def cmd_serve(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    from .mcp_server import serve

    serve(runtime)
    return EXIT_OK


def cmd_agent(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    from .llm_agent import run_agent

    def emit(event: Dict[str, Any]) -> None:
        if as_json:
            _print_json(event)
            sys.stdout.flush()
        elif event["event"] == "tool":
            sys.stderr.write("turn %s: execute -> %s %s\n" % (event["turn"], event["status"], event.get("error") or ""))

    approver = _tty_approver if args.interactive else None
    finished, text = run_agent(runtime, args.task, args.session, args.max_turns, emit, approver)
    if not as_json:
        _out(text)
    return EXIT_OK if finished else EXIT_FAILED


def cmd_snippet(runtime: Runtime, args: argparse.Namespace, as_json: bool) -> int:
    if args.action == "save":
        if not args.name or not args.source:
            raise UsageError("snippet save needs a name and --from", hint="python main.py snippet save NAME --from EXECUTION_ID")
        saved = runtime.save_snippet(args.name, args.source, args.description)
        _print_json(saved) if as_json else _out("saved snippet %s from %s" % (saved["name"], saved["sourceExecution"]))
    elif args.action == "delete":
        if not args.name:
            raise UsageError("snippet delete needs a name")
        runtime.snippets.delete(args.name)
        _print_json({"deleted": args.name}) if as_json else _out("deleted snippet %s" % args.name)
    else:
        rows = runtime.snippets.list()
        if as_json:
            _print_json(rows)
        else:
            for row in rows:
                _out("%s    %s  (from %s)" % (row["name"], row.get("description", ""), row.get("sourceExecution")))
            if not rows:
                _out("no snippets yet")
    return EXIT_OK


COMMANDS: Dict[str, Callable[[Runtime, argparse.Namespace, bool], int]] = {
    "run": cmd_run,
    "get": cmd_get,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "verify": cmd_verify,
    "executions": cmd_executions,
    "show": cmd_show,
    "search": cmd_search,
    "describe": cmd_describe,
    "prompt": cmd_prompt,
    "config": cmd_config,
    "serve": cmd_serve,
    "agent": cmd_agent,
    "snippet": cmd_snippet,
}


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS keeps a flag given before the command from being overwritten by
    # the default of the same flag on the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON value on stdout")
    common.add_argument("--config", default=argparse.SUPPRESS, metavar="FILE", help="config file (default: ./codemode.json, else the built-in demo config)")
    common.add_argument("--home", default=argparse.SUPPRESS, metavar="DIR", help="data directory (default: ./.codemode)")
    common.add_argument("--agent", default=argparse.SUPPRESS, metavar="NAME", help="who is acting; recorded in the audit log")

    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="codemode-runtime %s: one tool, execute(code), for any agent." % __version__,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=text, description=text, parents=[common])

    p = add("demo", "run the zero-config demo (also what runs with no command)")
    p.add_argument("--keep", action="store_true", help="keep the demo's data directory")

    p = add("run", "run a program")
    p.add_argument("code", nargs="?", help="the program, or - to read it from stdin")
    p.add_argument("-f", "--file", help="read the program from a file")
    p.add_argument("--session", default="default", help="which state dict the program sees")
    p.add_argument("--interactive", action="store_true", help="ask on the terminal when a call needs approval")

    p = add("get", "show the current outcome of an execution")
    p.add_argument("id", help="execution id, a unique prefix, or 'last'")
    p.add_argument("--full", action="store_true", help="do not cut the result to the output budget")

    p = add("approve", "approve the pending call(s) of a paused execution and resume it")
    p.add_argument("id")
    p.add_argument("--by", default="human", help="who approved; recorded in the audit log")

    p = add("reject", "refuse a paused execution; calls that already ran stay applied")
    p.add_argument("id")
    p.add_argument("--by", default="human")
    p.add_argument("--reason", default="")

    p = add("verify", "replay a completed execution from its log and compare the result")
    p.add_argument("id")

    p = add("executions", "list recent executions")
    p.add_argument("--limit", type=int, default=20)

    p = add("show", "print the audit log of an execution")
    p.add_argument("id")

    p = add("search", "find tools, as a program would with search()")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=10)

    p = add("describe", "describe a tool, a namespace or snippet:NAME")
    p.add_argument("target")

    add("prompt", "print the description of the execute tool")
    add("config", "print the resolved configuration")
    add("serve", "serve the execute tool over MCP (stdio)")

    p = add("agent", "run a task with a model on any OpenAI-compatible endpoint")
    p.add_argument("task")
    p.add_argument("--session", default="default")
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--interactive", action="store_true", help="ask on the terminal when a call needs approval")

    p = add("snippet", "save, list or delete reusable programs")
    p.add_argument("action", choices=["save", "list", "delete"])
    p.add_argument("name", nargs="?")
    p.add_argument("--from", dest="source", metavar="EXECUTION_ID", help="the completed execution to promote")
    p.add_argument("--description", default="")
    return parser


def main(argv: Optional[list] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            # A result may contain characters the console cannot encode;
            # printing it must degrade, not raise.
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    command = args.command or "demo"
    try:
        if command == "demo":
            from .demo import run_demo

            return run_demo(as_json, keep=bool(getattr(args, "keep", False)))
        config = load_config(getattr(args, "config", None))
        runtime = Runtime(config, resolve_home(getattr(args, "home", None)), resolve_agent(getattr(args, "agent", None), config))
        try:
            return COMMANDS[command](runtime, args, as_json)
        finally:
            runtime.close()
    except CodemodeError as exc:
        if as_json:
            _print_json({"status": "error", "error": exc.to_dict()})
        else:
            sys.stderr.write("error: %s\n" % exc.message)
            if exc.hint:
                sys.stderr.write("hint: %s\n" % exc.hint)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
