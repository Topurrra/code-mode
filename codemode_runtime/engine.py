# SPDX-License-Identifier: MIT
"""One pass of one execution: the gateway between the sandbox and the tools.

Every request the program makes arrives here as a frame and is given the next
sequence number. The gateway then does the same thing for every kind of
request:

1. Look at the log. If this sequence number already has an outcome, check that
   the request is the same one (by fingerprint) and return the recorded
   outcome. Nothing runs. This is replay.
2. Otherwise the request is live. For a tool call that means: resolve the
   name, apply the limits, ask the policy, write an ``executing`` entry, call
   the connector, record the outcome.

A call the policy says to ask about, and that no one has approved, is recorded
as pending and the pass ends as ``paused``. After approval the next pass runs
the same program from the top; steps 1 and 2 above make everything before the
approved call a replay and the approved call live. Nothing runs twice.

The pass only appends ``call`` entries. ``runtime.py`` owns the entries that
describe the execution as a whole.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .catalog import Catalog
from .connectors import CallContext
from .errors import CodemodeError, ToolFailure
from .executor import RUNNER_SOURCE, SandboxEOF, SandboxProtocolError
from .jsonutil import byte_len, fingerprint
from .policy import Policy
from .records import ExecutionStore, ExecutionView
from .sessions import SnippetStore
from .truncate import BoundedLog

Approver = Callable[[Dict[str, Any]], Optional[bool]]

_DIVERGENCE_HINT = (
    "A resumed execution runs the program again and must make the same requests in the same "
    "order. Wrap anything that changes between runs (time, randomness, file listings) in "
    "step(name, fn)."
)


class _Abort(Exception):
    """End the pass with an error outcome."""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


class _Paused(Exception):
    def __init__(self, pending: List[Dict[str, Any]]) -> None:
        super().__init__("paused")
        self.pending = pending


@dataclass
class PassOutcome:
    status: str  # completed | paused | error
    result: Any = None
    result_repr: bool = False
    state_out: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    pending: List[Dict[str, Any]] = field(default_factory=list)
    logs: str = ""
    logs_dropped: int = 0
    stats: Dict[str, Any] = field(default_factory=dict)


class Pass:
    def __init__(
        self,
        *,
        store: ExecutionStore,
        exec_id: str,
        view: ExecutionView,
        catalog: Catalog,
        policy: Policy,
        limits: Dict[str, float],
        snippets: SnippetStore,
        agent: str,
        pass_no: int,
        mode: str = "live",
        approver: Optional[Approver] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.store = store
        self.exec_id = exec_id
        self.view = view
        self.catalog = catalog
        self.policy = policy
        self.limits = limits
        self.snippets = snippets
        self.agent = agent
        self.pass_no = pass_no
        self.mode = mode  # "live", or "verify": replay only, never call a tool
        self.approver = approver
        self.cancel_event = cancel_event
        self.seq = 0
        self.tool_calls = 0
        self.replayed = 0
        self.tool_result_bytes = 0
        self.deadline = 0.0
        self.logs = BoundedLog(int(limits["max_log_chars"]))
        self._open_steps: Dict[int, Any] = {}
        self._handlers = {
            "call": self._on_call,
            "search": self._on_search,
            "describe": self._on_describe,
            "snippet": self._on_snippet,
            "step": self._on_step,
            "step_set": self._on_step_set,
        }

    # -- the loop ---------------------------------------------------------

    def run(self, executor: Any, code: str, state_in: Dict[str, Any]) -> PassOutcome:
        started = time.monotonic()
        self.deadline = started + float(self.limits["timeout_s"])
        try:
            proc = executor.start()
        except OSError as exc:
            return self._finish(
                PassOutcome(status="error", error={
                    "type": "ExecutorStartFailed",
                    "message": "could not start the sandbox: %s" % exc,
                    "hint": "Check executor.command in the config; the default needs no setup.",
                }),
                started,
            )
        try:
            # send() never blocks or raises; a sandbox that is gone or silent
            # shows up in _recv, as an exit, a bad frame or a timeout.
            proc.send({"op": "init", "runner": RUNNER_SOURCE})
            ready_by = min(self.deadline, started + float(self.limits["startup_timeout_s"]))
            if self._recv(proc, ready_by, starting=True).get("op") != "ready":
                raise _Abort("ProtocolError", "the sandbox did not start with a ready frame")
            proc.send({"op": "run", "code": code, "state": state_in})
            while True:
                frame = self._recv(proc, self.deadline)
                op = frame.get("op")
                if op == "log":
                    self.logs.add(str(frame.get("text", "")))
                    continue
                if op == "done":
                    outcome = self._on_done(frame)
                    proc.wait_exit(1.0)
                    break
                handler = self._handlers.get(op)
                if handler is None:
                    raise _Abort("ProtocolError", "the sandbox sent an unknown request %r" % (op,))
                reply = handler(frame)
                reply["id"] = frame.get("id")
                proc.send(reply)
        except _Paused as paused:
            outcome = PassOutcome(status="paused", pending=paused.pending)
        except _Abort as abort:
            error = {"type": abort.kind, "message": abort.message}
            if abort.hint:
                error["hint"] = abort.hint
            outcome = PassOutcome(status="error", error=error)
        finally:
            proc.kill()
            stderr = proc.stderr_tail().strip()
            if stderr:
                # Raw writes to the process's own stdout and stderr land here.
                self.logs.add("\n[sandbox stderr]\n" + stderr + "\n")
        return self._finish(outcome, started)

    def _finish(self, outcome: PassOutcome, started: float) -> PassOutcome:
        outcome.logs = self.logs.render()
        outcome.logs_dropped = self.logs.dropped
        outcome.stats = {
            "toolCalls": self.tool_calls,
            "replayed": self.replayed,
            "toolResultBytes": self.tool_result_bytes,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
        return outcome

    def _recv(self, proc: Any, deadline: float, starting: bool = False) -> Dict[str, Any]:
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise _Abort("Cancelled", "the execution was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if starting:
                    raise _Abort(
                        "ExecutorStartFailed",
                        "the sandbox did not report ready within %gs" % self.limits["startup_timeout_s"],
                        "The executor command must run the bootstrap: python -I -u -c {bootstrap}.",
                    )
                raise _Abort(
                    "Timeout",
                    "the program ran longer than %gs (limits.timeout_s)" % self.limits["timeout_s"],
                    "Do less in one program, or raise limits.timeout_s.",
                )
            try:
                return proc.recv(min(remaining, 0.2))
            except TimeoutError:
                continue
            except SandboxEOF:
                proc.wait_exit(0.5)
                raise _Abort(
                    "ExecutorStartFailed" if starting else "SandboxCrashed",
                    "the sandbox process exited (code %s) before it %s"
                    % (proc.exit_code(), "started" if starting else "finished"),
                    "See the logs for its stderr.",
                )
            except SandboxProtocolError as exc:
                raise _Abort(
                    "ProtocolError",
                    str(exc),
                    "The executor must run the bootstrap, and nothing else may write to its stdout.",
                )

    # -- replay and recording --------------------------------------------

    def _next(self, kind: str, name: str, args: Any) -> Any:
        """Give a request its sequence number and find its recorded outcome."""
        self.seq += 1
        fp = fingerprint(kind, name, args)
        rec = self.view.calls.get(self.seq)
        if rec is not None and rec.get("fp") != fp:
            same = rec.get("kind") == kind and rec.get("name") == name
            raise _Abort(
                "ReplayDivergence",
                "at step %d the log has %s %r but the program now asks for %s %r%s"
                % (self.seq, rec.get("kind"), rec.get("name"), kind, name, " with different arguments" if same else ""),
                _DIVERGENCE_HINT,
            )
        if rec is None and self.mode == "verify":
            raise _Abort(
                "VerifyMismatch",
                "the program asked for %s %r at step %d, which is not in the log" % (kind, name, self.seq),
                _DIVERGENCE_HINT,
            )
        return self.seq, fp, rec

    def _append(self, seq: int, kind: str, name: str, args: Any, fp: str, **extra: Any) -> Dict[str, Any]:
        assert self.mode == "live", "a verify pass must never write a call"
        entry = {"type": "call", "seq": seq, "pass": self.pass_no, "kind": kind, "name": name, "args": args, "fp": fp}
        entry.update(extra)
        self.store.append(self.exec_id, entry)
        self.view.calls[seq] = entry
        return entry

    @staticmethod
    def _reply(rec: Dict[str, Any]) -> Dict[str, Any]:
        if rec.get("state") == "applied":
            return {"ok": True, "value": rec.get("result")}
        return {"ok": False, "error": rec.get("error") or {"type": "ToolError", "message": "failed"}}

    def _size(self, value: Any, what: str) -> int:
        try:
            size = byte_len(value)
        except (TypeError, ValueError) as exc:
            raise ToolFailure("BadValue", "%s is not valid JSON: %s" % (what, exc))
        limit = int(self.limits["max_value_bytes"])
        if size > limit:
            raise ToolFailure(
                "ValueTooLarge",
                "%s is %d bytes; the limit is %d (limits.max_value_bytes). "
                "Ask for less: filter, paginate or summarise at the source." % (what, size, limit),
            )
        return size

    # -- discovery, snippets, steps --------------------------------------

    def _simple(self, kind: str, name: str, args: Any, compute: Callable[[], Any]) -> Dict[str, Any]:
        """Search, describe and snippet lookups are recorded like tool calls:
        the catalog may change between passes, and replay must not."""
        seq, fp, rec = self._next(kind, name, args)
        if rec is not None:
            self.replayed += 1
            return self._reply(rec)
        try:
            value = compute()
            self._size(value, "the %s result" % kind)
            return self._reply(self._append(seq, kind, name, args, fp, state="applied", result=value))
        except ToolFailure as failure:
            error = failure.to_dict()
        except CodemodeError as exc:
            error = {"type": exc.kind, "message": exc.message}
        return self._reply(self._append(seq, kind, name, args, fp, state="error", error=error))

    def _on_search(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        query = str(frame.get("query", ""))
        try:
            limit = int(frame.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        return self._simple("search", query, {"limit": limit}, lambda: self.catalog.search(query, limit))

    def _on_describe(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        target = str(frame.get("target", ""))
        return self._simple("describe", target, None, lambda: self.catalog.describe(target))

    def _on_snippet(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        name = str(frame.get("name", ""))
        return self._simple("snippet", name, None, lambda: {"code": self.snippets.get(name)["code"]})

    def _on_step(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        name = str(frame.get("name", ""))
        seq, fp, rec = self._next("step", name, None)
        if rec is not None:
            self.replayed += 1
            if rec.get("state") == "applied":
                return {"ok": True, "hit": True, "value": rec.get("result")}
            return self._reply(rec)
        self._open_steps[seq] = (name, fp)
        return {"ok": True, "hit": False, "seq": seq}

    def _on_step_set(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        seq = frame.get("seq")
        if seq not in self._open_steps:
            raise _Abort("ProtocolError", "the sandbox closed a step that is not open")
        name, fp = self._open_steps.pop(seq)
        if "error" in frame:
            raw = frame["error"] if isinstance(frame["error"], dict) else {}
            error = {"type": "StepError", "message": str(raw.get("message", ""))}
            self._append(seq, "step", name, None, fp, state="error", error=error)
            return {"ok": True}
        value = frame.get("value")
        try:
            self._size(value, "the value of step %r" % name)
        except ToolFailure as failure:
            return self._reply(self._append(seq, "step", name, None, fp, state="error", error=failure.to_dict()))
        self._append(seq, "step", name, None, fp, state="applied", result=value)
        return {"ok": True}

    # -- tool calls -------------------------------------------------------

    def _on_call(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        calls = frame.get("calls")
        if not isinstance(calls, list) or not calls:
            raise _Abort("ProtocolError", "the sandbox sent a call request with no calls")
        plan: List[Dict[str, Any]] = []
        for raw in calls:
            raw = raw if isinstance(raw, dict) else {}
            path = str(raw.get("tool", ""))
            args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
            seq, fp, rec = self._next("tool", path, args)
            self.tool_calls += 1
            item: Dict[str, Any] = {"seq": seq, "path": path, "args": args, "fp": fp}
            state = rec.get("state") if rec else None
            if state in ("applied", "error", "denied"):
                item["rec"] = rec
                self.replayed += 1
            elif self.mode == "verify":
                raise _Abort("VerifyMismatch", "step %d (%s) has no recorded outcome" % (seq, path))
            elif state == "executing":
                # An earlier pass started this call and died before recording
                # how it ended. Running it again could repeat a side effect.
                raise _Abort(
                    "IndeterminateCall",
                    "step %d (%s) was started earlier and its outcome was never recorded; it will not be run again"
                    % (seq, path),
                    "Check the target system by hand, then reject this execution and start a new one.",
                )
            elif state == "pending" and seq not in self.view.approved:
                item["pending"] = rec
            else:
                item.update(self._classify(seq, path, args, approved=(state == "pending")))
            plan.append(item)

        waiting = [i for i in plan if "pending" in i]
        if waiting:
            # Pause before anything in this batch runs: a batch is either
            # fully decided or not started.
            pending = []
            for i in waiting:
                rec = i["pending"]
                if rec is None:
                    rec = self._append(i["seq"], "tool", i["path"], i["args"], i["fp"], state="pending", **i["info"])
                pending.append({"seq": i["seq"], "tool": i["path"], "args": i["args"], "rule": rec.get("rule")})
            raise _Paused(pending)

        live = [i for i in plan if "entry" in i]
        for i in live:
            # Write-ahead: the log names every side effect before it happens.
            self._append(i["seq"], "tool", i["path"], i["args"], i["fp"], state="executing", **i["info"])
        timeout = max(0.1, min(float(self.limits["call_timeout_s"]), self.deadline - time.monotonic()))
        # Daemon threads, not a pool: a stalled tool must not be able to keep
        # the process alive after the pass has given up on it.
        threads = [threading.Thread(target=self._invoke, args=(i, timeout), daemon=True) for i in live]
        for thread in threads:
            thread.start()
        join_by = time.monotonic() + timeout + 0.5  # connectors report their own timeouts first
        for i, thread in zip(live, threads):
            while thread.is_alive() and time.monotonic() < join_by:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    # The entry stays "executing": the log says the call was
                    # started and that nobody saw how it ended.
                    raise _Abort("Cancelled", "cancelled while %s was running; its outcome is unknown" % i["path"])
                thread.join(min(0.2, max(0.0, join_by - time.monotonic())))
            slot = i.get("slot")
            if slot is None:
                slot = ("error", {
                    "type": "ToolTimeout",
                    "message": "%s did not answer within %gs (limits.call_timeout_s); it may still be running"
                    % (i["path"], timeout),
                }, 0)
            state, payload, size = slot
            extra = dict(i["info"], state=state, duration_ms=i.get("duration_ms", int(timeout * 1000)))
            if state == "applied":
                extra.update(result=payload, result_bytes=size)
            else:
                extra.update(error=payload)
            i["rec"] = self._append(i["seq"], "tool", i["path"], i["args"], i["fp"], **extra)

        results = []
        for i in plan:
            if "fail" in i:
                state, error = i["fail"]
                i["rec"] = self._append(
                    i["seq"], "tool", i["path"], i["args"], i["fp"], state=state, error=error, **i.get("info", {})
                )
            reply = self._reply(i["rec"])
            if reply["ok"]:
                self.tool_result_bytes += i["rec"].get("result_bytes") or 0
            results.append(reply)
        return {"ok": True, "results": results}

    def _classify(self, seq: int, path: str, args: Dict[str, Any], approved: bool) -> Dict[str, Any]:
        """Decide what happens to a call that has no recorded outcome."""
        if self.tool_calls > int(self.limits["max_calls"]):
            return {"fail": ("error", {
                "type": "CallLimitExceeded",
                "message": "this execution made more than %d tool calls (limits.max_calls)" % self.limits["max_calls"],
            })}
        try:
            self._size(args, "the arguments of %s" % path)
            entry = self.catalog.resolve(path)
        except ToolFailure as failure:
            return {"fail": ("error", failure.to_dict())}
        decision = self.policy.decide(entry.path, args, alias=entry.original_path)
        info = {"decision": decision.action, "rule": decision.rule, "enforced": decision.enforced}
        # Deny is checked before the approval: a human said yes to the call
        # as the policy stood, and a deny rule added since then still wins.
        if decision.effective == "deny":
            return {"info": info, "fail": ("denied", {
                "type": "PolicyDenied",
                "message": "%s is denied by policy (rule %r)%s"
                % (entry.path, decision.rule, "; the earlier approval does not override a deny rule" if approved else ""),
                "hint": "Do not retry or look for another way to make this call. Tell the user it was denied and by which rule.",
            })}
        if approved:
            return {"entry": entry, "info": dict(info, approved=True)}
        if decision.effective == "ask":
            verdict = None
            if self.approver is not None:
                asked = time.monotonic()
                verdict = self.approver({"seq": seq, "tool": entry.path, "args": args, "rule": decision.rule})
                self.deadline += time.monotonic() - asked  # time spent waiting on a human is not the program's
            if verdict is True:
                self.store.append(self.exec_id, {"type": "approval", "seqs": [seq], "by": "interactive"})
                self.view.approved.add(seq)
                return {"entry": entry, "info": dict(info, approved=True)}
            if verdict is False:
                return {"info": info, "fail": ("denied", {
                    "type": "PolicyDenied",
                    "message": "%s was refused by the human" % entry.path,
                })}
            return {"pending": None, "info": info}
        return {"entry": entry, "info": info}

    def _invoke(self, item: Dict[str, Any], timeout: float) -> None:
        """Runs on a worker thread. Writes one slot; the pass reads it once."""
        entry = item["entry"]
        started = time.monotonic()
        try:
            ctx = CallContext(self.exec_id, self.agent, item["seq"], timeout)
            value = self.catalog.connector_for(entry).call(entry.original, item["args"], ctx)
            slot = ("applied", value, self._size(value, "the result of %s" % entry.path))
        except ToolFailure as failure:
            slot = ("error", failure.to_dict(), 0)
        except Exception as exc:  # a connector bug is a failed call, not a crashed runtime
            slot = ("error", {"type": "ToolCrashed", "message": "%s: %s" % (type(exc).__name__, exc)}, 0)
        item["duration_ms"] = int((time.monotonic() - started) * 1000)
        item["slot"] = slot

    # -- the end ----------------------------------------------------------

    def _on_done(self, frame: Dict[str, Any]) -> PassOutcome:
        if not frame.get("ok"):
            error = frame.get("error") if isinstance(frame.get("error"), dict) else {}
            error.setdefault("type", "ProgramError")
            error.setdefault("message", "the program failed")
            return PassOutcome(status="error", error=error)
        if self.mode == "verify":
            recorded = max(self.view.calls, default=0)
            if self.seq != recorded:
                raise _Abort(
                    "VerifyMismatch",
                    "the log has %d steps but the replay made %d" % (recorded, self.seq),
                    _DIVERGENCE_HINT,
                )
        state = frame.get("state") if isinstance(frame.get("state"), dict) else {}
        return PassOutcome(
            status="completed",
            result=frame.get("result"),
            result_repr=bool(frame.get("result_repr")),
            state_out=state,
        )
