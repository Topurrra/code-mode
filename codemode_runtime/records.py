# SPDX-License-Identifier: MIT
"""The execution log: one append-only JSONL file per execution.

The log is the source of truth. Status, pending approvals and replay data are
all derived from it by ``ExecutionView``; nothing is stored twice and nothing
is edited in place. That is what makes an execution resumable after a pause
and checkable afterwards: the file says what happened, in order.

Entry types: ``start``, ``pass_start``, ``call``, ``approval``, ``pass_end``,
``end``, ``verify``. A ``call`` entry with a given ``seq`` may appear more than
once (``executing`` then ``applied``, or ``pending`` then ``applied``); the
last one is the current state of that sequence number.
"""

from __future__ import annotations

import calendar
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

from . import launcher
from .errors import BusyError, NotFoundError
from .jsonutil import dumps_line

TERMINAL = ("completed", "error", "rejected")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_ts(text: Any) -> Optional[float]:
    """The ``ts`` of a log entry as seconds since the epoch, or None."""
    try:
        return float(calendar.timegm(time.strptime(str(text), _TS_FORMAT)))
    except (TypeError, ValueError):
        return None


class ExecutionView:
    """What a log means right now."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries
        self.start: Dict[str, Any] = {}
        self.calls: Dict[int, Dict[str, Any]] = {}
        self.approved: Set[int] = set()
        self.passes = 0
        self.end: Optional[Dict[str, Any]] = None
        self.verify: Optional[Dict[str, Any]] = None
        self.policy_changed = False
        self.paused_at: Optional[str] = None
        last_pass_status = None
        for entry in entries:
            kind = entry.get("type")
            if kind == "start":
                self.start = entry
            elif kind == "call":
                self.calls[int(entry["seq"])] = entry
            elif kind == "approval":
                self.approved.update(int(s) for s in entry.get("seqs", []))
                self.policy_changed = self.policy_changed or bool(entry.get("policy_changed"))
            elif kind == "pass_start":
                self.passes += 1
                last_pass_status = None
            elif kind == "pass_end":
                last_pass_status = entry.get("status")
                if last_pass_status == "paused":
                    self.paused_at = entry.get("ts")
            elif kind == "end":
                self.end = entry
            elif kind == "verify":
                self.verify = entry
        if self.end is not None:
            self.status = self.end.get("status", "error")
        elif last_pass_status == "paused":
            self.status = "paused"
        else:
            self.status = "running"

    @property
    def id(self) -> str:
        return self.start.get("id", "")

    @property
    def pending(self) -> List[Dict[str, Any]]:
        """Calls waiting for a human, in order."""
        return [
            entry
            for seq, entry in sorted(self.calls.items())
            if entry.get("state") == "pending" and seq not in self.approved
        ]

    def paused_for(self, now: Optional[float] = None) -> float:
        """Seconds since the execution paused; 0 if that cannot be told."""
        when = parse_ts(self.paused_at)
        if when is None:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - when)

    @property
    def verification(self) -> str:
        """How this execution was checked. ``none`` is an answer too: an
        unverified result should be findable, not indistinguishable."""
        if self.verify is None:
            return "none"
        return "replay-ok" if self.verify.get("ok") else "replay-failed"

    def summary(self) -> Dict[str, Any]:
        applied = sum(1 for c in self.calls.values() if c.get("kind") == "tool" and c.get("state") == "applied")
        return {
            "executionId": self.id,
            "status": self.status,
            "session": self.start.get("session"),
            "agent": self.start.get("agent"),
            "startedAt": self.start.get("ts"),
            "passes": self.passes,
            "toolCalls": applied,
            "verification": self.verification,
        }


class ExecutionStore:
    def __init__(self, home: Path) -> None:
        self._dir = Path(home) / "executions"

    def _path(self, exec_id: str) -> Path:
        return self._dir / (exec_id + ".jsonl")

    def new_id(self) -> str:
        """Sortable by start time, so ``last`` and listings need no index.

        Microseconds are part of the id: with whole seconds, two executions
        started in the same second would sort by their random suffix and
        ``last`` would name the wrong one.
        """
        now = time.time_ns()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now // 1_000_000_000))
        return "ex_%s%06d_%s" % (stamp, (now % 1_000_000_000) // 1000, secrets.token_hex(2))

    def append(self, exec_id: str, entry: Dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        entry = dict(entry, ts=time.strftime(_TS_FORMAT, time.gmtime()))
        with open(self._path(exec_id), "ab") as handle:
            handle.write(dumps_line(entry))

    def save_blob(self, exec_id: str, seq: int, value: Any) -> Optional[str]:
        """Keep a tool result that is too large for the log, so that the work
        of the call is not lost with it. Returns the file's path, or None if
        it could not be written; the caller reports either."""
        folder = self._dir.parent / "blobs"
        path = folder / ("%s-%d.json" % (exec_id, seq))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="ascii") as handle:
                json.dump(value, handle, ensure_ascii=True)
        except (OSError, TypeError, ValueError):
            return None
        return str(path)

    def read(self, exec_id: str) -> List[Dict[str, Any]]:
        path = self._path(exec_id)
        if not path.is_file():
            raise NotFoundError(
                "no execution %r" % exec_id, hint="List them with: %s" % launcher.command("executions")
            )
        entries = []
        for line in path.read_text(encoding="ascii").splitlines():
            if line.strip():
                entries.append(json.loads(line))
        return entries

    def view(self, exec_id: str) -> ExecutionView:
        return ExecutionView(self.read(exec_id))

    def ids(self) -> List[str]:
        if not self._dir.is_dir():
            return []
        return sorted(p.stem for p in self._dir.glob("ex_*.jsonl"))

    def resolve_id(self, ref: str) -> str:
        """Accept a full id, a unique prefix, or ``last``."""
        ids = self.ids()
        if ref == "last":
            if not ids:
                raise NotFoundError("there are no executions yet")
            return ids[-1]
        if ref in ids:
            return ref
        matches = [i for i in ids if i.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise NotFoundError("%r matches %d executions: %s" % (ref, len(matches), ", ".join(matches[:5])))
        raise NotFoundError("no execution %r" % ref, hint="List them with: %s" % launcher.command("executions"))

    @contextmanager
    def lock(self, exec_id: str) -> Iterator[None]:
        """One pass at a time per execution. Two concurrent passes could both
        run the same approved call, which is exactly the double side effect
        replay exists to prevent."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / (exec_id + ".lock")
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = path.read_text().strip()
            except OSError:
                owner = "?"
            raise BusyError(
                "execution %s is being run by another process (pid %s)" % (exec_id, owner),
                hint="Wait for it. If that process is gone, delete %s" % path,
            )
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(str(path))
            except OSError:
                pass
