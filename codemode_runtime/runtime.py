# SPDX-License-Identifier: MIT
"""The runtime: the public API every surface uses.

The CLI, the MCP server and the agent loop all call the same few methods
here, so they cannot drift apart: ``execute``, ``get``, ``approve``,
``reject``, ``verify``. Each returns a plain dict (the payload) whose
``status`` is one of ``completed``, ``paused``, ``error``, ``rejected`` or
``running``.

``approve`` and ``reject`` are reachable from the CLI only. They are not
sandbox functions and not MCP tools: an agent must not be able to approve its
own request.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .catalog import Catalog
from .config import Config
from .connectors import build_connector
from .engine import Approver, Pass, PassOutcome
from .errors import StateError, UsageError
from .executor import SubprocessExecutor
from .jsonutil import byte_len, canonical, sha256_hex
from .records import ExecutionStore, ExecutionView
from .sessions import SessionStore, SnippetStore, check_session_name
from .truncate import budget_result, truncate_text

_ARGS_PREVIEW_CHARS = 2000

_TOOL_DESCRIPTION = """\
Run a Python program that calls tools. Write one program per step, not one call per step.

Inside the program:
- tools.<namespace>.<tool>(**arguments) calls a tool and returns a plain Python value. \
call("namespace.tool", {...}) does the same by name.
- parallel([("namespace.tool", {...}), ...]) runs independent calls at the same time and \
returns their results in order.
- search("words") finds tools; describe("namespace.tool") returns the signature and schema. \
Look a tool up before you call it for the first time.
- state is a dict that persists between programs in this session. Keep what you read or \
computed there instead of fetching it again.
- step("name", fn) records the value of fn(). Use it for the time, randomness and anything \
else that would differ if the program ran again.
- run("snippet", **args) runs a saved snippet.
- print() goes to the logs. The value of the last expression is the result. Return only what \
you need: filter and aggregate in the program.

Calls are synchronous (no await). A failed or denied call raises ToolError(kind, message), \
which the program can catch.

Outcomes: {"status": "completed", "result": ...}, {"status": "error", "error": ...}, or \
{"status": "paused", "pending": [...]} when a call needs human approval. When paused, nothing \
pending has run: tell the user, then fetch the outcome later by calling this tool with \
{"executionId": "..."} instead of code."""


class Runtime:
    def __init__(self, config: Config, home: Path, agent: str = "anonymous") -> None:
        self.config = config
        self.home = Path(home)
        self.agent = agent
        self.limits = config.limits
        self.store = ExecutionStore(self.home)
        self.sessions = SessionStore(self.home)
        self.snippets = SnippetStore(self.home)
        self.executor = SubprocessExecutor(config.executor_command, config.executor_env_passthrough)
        self._catalog: Optional[Catalog] = None

    # -- wiring -----------------------------------------------------------

    @property
    def catalog(self) -> Catalog:
        """Built on first use: commands that only read the log start no
        connector and no MCP server."""
        if self._catalog is None:
            connectors = {
                name: build_connector(name, spec, self.home, float(self.limits["startup_timeout_s"]))
                for name, spec in self.config.connectors.items()
            }
            self._catalog = Catalog(connectors, self.config.policy, self.snippets.list)
        return self._catalog

    def close(self) -> None:
        if self._catalog is not None:
            self._catalog.close()
            self._catalog = None

    def tool_description(self) -> str:
        """The whole static prompt. Short on purpose: it names the namespaces
        and the sandbox functions, and leaves every tool to be discovered."""
        lines = []
        for name, info in self.catalog.namespaces().items():
            lines.append("%s (%s)" % (name, ("%d tools" % info) if isinstance(info, int) else info))
        return _TOOL_DESCRIPTION + "\n\nNamespaces: " + ", ".join(lines) + "."

    # -- the five operations ---------------------------------------------

    def execute(
        self,
        code: str,
        session: str = "default",
        approver: Optional[Approver] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        if not isinstance(code, str) or not code.strip():
            raise UsageError("the program is empty", hint="Pass Python source as the code.")
        if len(code.encode("utf-8", "replace")) > int(self.limits["max_value_bytes"]):
            raise UsageError("the program is larger than limits.max_value_bytes")
        check_session_name(session)
        exec_id = self.store.new_id()
        policy = self.config.policy
        self.store.append(exec_id, {
            "type": "start",
            "id": exec_id,
            "session": session,
            "agent": self.agent,  # stamped here, on the host; the program cannot set it
            "code": code,
            "code_sha256": sha256_hex(code),
            "state_in": self.sessions.load(session),
            "limits": self.limits,
            "policy_sha256": sha256_hex(canonical(policy.to_dict())),
            "policy_mode": policy.mode,
            "connectors": sorted(self.config.connectors),
            "runtime_version": __version__,
        })
        with self.store.lock(exec_id):
            return self._run_pass(exec_id, approver, cancel_event)

    def approve(
        self,
        ref: str,
        by: str = "human",
        approver: Optional[Approver] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        exec_id = self.store.resolve_id(ref)
        with self.store.lock(exec_id):
            view = self.store.view(exec_id)
            if view.status != "paused":
                raise StateError(
                    "execution %s is %s, not paused" % (exec_id, view.status),
                    hint="Only a paused execution has something to approve.",
                )
            self.store.append(exec_id, {"type": "approval", "seqs": [p["seq"] for p in view.pending], "by": by})
            return self._run_pass(exec_id, approver, cancel_event)

    def reject(self, ref: str, by: str = "human", reason: str = "") -> Dict[str, Any]:
        exec_id = self.store.resolve_id(ref)
        with self.store.lock(exec_id):
            view = self.store.view(exec_id)
            if view.status not in ("paused", "running"):
                raise StateError("execution %s is already %s" % (exec_id, view.status))
            # Calls that already ran stay applied. Rejecting stops the
            # execution; it does not pretend to undo it.
            self.store.append(exec_id, {
                "type": "end",
                "status": "rejected",
                "by": by,
                "reason": reason,
                "rejected_seqs": [p["seq"] for p in view.pending],
            })
        return self.get(exec_id)

    def get(self, ref: str, full: bool = False) -> Dict[str, Any]:
        exec_id = self.store.resolve_id(ref)
        return self._payload(self.store.view(exec_id), full=full)

    def verify(self, ref: str) -> Dict[str, Any]:
        """Replay a completed execution from its log, calling no tool, and
        compare the result. This is a differential test of the record against
        the program: it shows the stored result follows from the stored calls."""
        exec_id = self.store.resolve_id(ref)
        with self.store.lock(exec_id):
            view = self.store.view(exec_id)
            if view.status != "completed" or view.end is None:
                raise StateError(
                    "execution %s is %s; only a completed execution can be verified" % (exec_id, view.status)
                )
            outcome = self._pass(view, "verify", None, None).run(
                self.executor, view.start["code"], view.start.get("state_in") or {}
            )
            detail = ""
            if outcome.status != "completed":
                error = outcome.error or {}
                detail = "%s: %s" % (error.get("type", "error"), error.get("message", ""))
            elif sha256_hex(canonical(outcome.result)) != view.end.get("result_sha256"):
                detail = "the replayed result differs from the recorded one"
            elif sha256_hex(canonical(outcome.state_out)) != view.end.get("state_out_sha256"):
                detail = "the replayed state differs from the recorded one"
            ok = not detail
            self.store.append(exec_id, {"type": "verify", "ok": ok, "detail": detail, "replayed": outcome.stats.get("replayed")})
        return {
            "executionId": exec_id,
            "verified": ok,
            "verification": "replay-ok" if ok else "replay-failed",
            "replayedSteps": outcome.stats.get("replayed", 0),
            "detail": detail or "the replay reproduced the recorded result and state without calling any tool",
        }

    # -- discovery and listings (host side) ------------------------------

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self.catalog.search(query, limit)

    def describe(self, target: str) -> Dict[str, Any]:
        return self.catalog.describe(target)

    def list_executions(self, limit: int = 20) -> List[Dict[str, Any]]:
        ids = self.store.ids()[-max(1, int(limit)):]
        return [self.store.view(i).summary() for i in reversed(ids)]

    def save_snippet(self, name: str, ref: str, description: str = "") -> Dict[str, Any]:
        """Promote the program of a completed execution to a reusable snippet.
        Only code that has run to completion can be promoted, and only from
        the host."""
        exec_id = self.store.resolve_id(ref)
        view = self.store.view(exec_id)
        if view.status != "completed":
            raise StateError(
                "execution %s is %s; only a completed execution can become a snippet" % (exec_id, view.status)
            )
        return self.snippets.save(name, view.start["code"], description, exec_id)

    # -- one pass ---------------------------------------------------------

    def _pass(self, view: ExecutionView, mode: str, approver: Optional[Approver], cancel_event: Any) -> Pass:
        return Pass(
            store=self.store,
            exec_id=view.id,
            view=view,
            catalog=self.catalog,
            policy=self.config.policy,
            limits=self.limits,
            snippets=self.snippets,
            agent=view.start.get("agent", self.agent),
            pass_no=view.passes + 1,
            mode=mode,
            approver=approver,
            cancel_event=cancel_event,
        )

    def _run_pass(self, exec_id: str, approver: Optional[Approver], cancel_event: Any) -> Dict[str, Any]:
        """Caller holds the execution lock."""
        view = self.store.view(exec_id)
        start = view.start
        runner = self._pass(view, "live", approver, cancel_event)
        self.store.append(exec_id, {"type": "pass_start", "pass": runner.pass_no, "pid": os.getpid()})
        outcome = runner.run(self.executor, start["code"], start.get("state_in") or {})
        extras: Dict[str, Any] = {}
        if outcome.status == "paused":
            self.store.append(exec_id, {
                "type": "pass_end",
                "pass": runner.pass_no,
                "status": "paused",
                "pending": [p["seq"] for p in outcome.pending],
                "stats": outcome.stats,
            })
        elif outcome.status == "completed":
            delta, warnings = self._state_delta(start["session"], start.get("state_in") or {}, outcome.state_out)
            self.sessions.apply_delta(start["session"], delta["set"], delta["delete"])
            result_text = canonical(outcome.result)
            fits = len(result_text) <= int(self.limits["max_value_bytes"])
            self.store.append(exec_id, {"type": "pass_end", "pass": runner.pass_no, "status": "completed"})
            self.store.append(exec_id, {
                "type": "end",
                "status": "completed",
                # A result too large for the log is hashed, not stored; the
                # caller still receives it, within the output budget.
                "result": outcome.result if fits else None,
                "result_omitted": not fits,
                "result_chars": len(result_text),
                "result_sha256": sha256_hex(result_text),
                "result_repr": outcome.result_repr,
                "state_out_sha256": sha256_hex(canonical(outcome.state_out)),
                "state_delta": {"set": sorted(delta["set"]), "delete": delta["delete"]},
                "logs": outcome.logs,
                "logs_dropped": outcome.logs_dropped,
                "stats": outcome.stats,
                "warnings": warnings,
            })
            if not fits:
                extras["live_result"] = outcome.result
        else:
            self.store.append(exec_id, {"type": "pass_end", "pass": runner.pass_no, "status": "error"})
            self.store.append(exec_id, {
                "type": "end",
                "status": "error",
                "error": outcome.error,
                "logs": outcome.logs,
                "logs_dropped": outcome.logs_dropped,
                "stats": outcome.stats,
            })
        return self._payload(self.store.view(exec_id), outcome=outcome, **extras)

    def _state_delta(self, session: str, before: Dict[str, Any], after: Dict[str, Any]) -> Any:
        """What this execution changed in ``state``, cut down to the size
        limit. A value that does not fit is dropped and named, so the next
        program is told rather than finding a hole."""
        changed = {k: v for k, v in after.items() if k not in before or before[k] != v}
        deleted = sorted(k for k in before if k not in after)
        warnings: List[str] = []
        limit = int(self.limits["max_state_bytes"])
        current = self.sessions.load(session)
        for key in deleted:
            current.pop(key, None)
        merged = dict(current, **changed)
        while changed and byte_len(merged) > limit:
            biggest = max(changed, key=lambda k: byte_len(changed[k]))
            warnings.append(
                "state[%r] was not saved: the session state would exceed %d bytes (limits.max_state_bytes)"
                % (biggest, limit)
            )
            del changed[biggest]
            merged = dict(current, **changed)
        return {"set": changed, "delete": deleted}, warnings

    # -- payloads ---------------------------------------------------------

    def _payload(
        self,
        view: ExecutionView,
        outcome: Optional[PassOutcome] = None,
        full: bool = False,
        live_result: Any = None,
    ) -> Dict[str, Any]:
        """What the caller sees. Built from the log, so ``get`` on an old
        execution and the return of a fresh one have the same shape."""
        payload: Dict[str, Any] = {"status": view.status, "executionId": view.id}
        end = view.end or {}
        budget = int(self.limits["max_result_chars"])
        if view.status == "completed":
            result = live_result if end.get("result_omitted") and live_result is not None else end.get("result")
            if end.get("result_omitted") and live_result is None:
                payload["result"] = None
                payload["resultOmitted"] = "the result was %d chars, too large to keep in the log" % end.get("result_chars", 0)
            elif full:
                payload["result"] = result
            else:
                shown, truncated, total = budget_result(result, budget)
                payload["result"] = shown
                if truncated:
                    payload["resultTruncated"] = True
                    payload["resultChars"] = total
                    payload["hint"] = (
                        "The result was cut to %d chars. Return less: filter, slice or aggregate in the program." % budget
                    )
            if end.get("result_repr"):
                payload["resultIsRepr"] = True
            if end.get("warnings"):
                payload["warnings"] = end["warnings"]
            payload["verification"] = view.verification
        elif view.status == "paused":
            payload["pending"] = [self._pending_item(p) for p in view.pending]
            payload["hint"] = (
                "Paused before a call that needs approval; nothing pending has run. A human approves with "
                "'python main.py approve %s' or refuses with 'python main.py reject %s'. Fetch the outcome "
                "afterwards by passing this executionId instead of code." % (view.id, view.id)
            )
        elif view.status == "rejected":
            payload["error"] = {
                "type": "Rejected",
                "message": "a human rejected this execution%s" % ((": " + end["reason"]) if end.get("reason") else ""),
                "hint": "Calls that ran before the pause stay applied.",
            }
        elif view.status == "error":
            payload["error"] = end.get("error") or {"type": "Error", "message": "failed"}
        logs = outcome.logs if outcome is not None else end.get("logs", "")
        dropped = outcome.logs_dropped if outcome is not None else end.get("logs_dropped", 0)
        if logs:
            payload["logs"] = logs
        if dropped:
            payload["logsDroppedChars"] = dropped
        stats = dict(outcome.stats if outcome is not None else end.get("stats") or {})
        if stats:
            payload["stats"] = stats
        return payload

    @staticmethod
    def _pending_item(entry: Dict[str, Any]) -> Dict[str, Any]:
        shown, truncated, _ = budget_result(entry.get("args"), _ARGS_PREVIEW_CHARS)
        item = {"seq": entry["seq"], "tool": entry["name"], "args": shown, "rule": entry.get("rule")}
        if truncated:
            item["argsTruncated"] = True
        return item


def summarize_stats(stats: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """One line a human can read: what code mode saved on this execution."""
    returned = len(canonical(payload.get("result"))) + len(payload.get("logs", ""))
    text = "%d tool call(s), %d replayed step(s), %d bytes of tool results stayed in the sandbox, %d bytes returned, %d ms" % (
        stats.get("toolCalls", 0),
        stats.get("replayed", 0),
        stats.get("toolResultBytes", 0),
        returned,
        stats.get("durationMs", 0),
    )
    return truncate_text(text, 400)[0]
