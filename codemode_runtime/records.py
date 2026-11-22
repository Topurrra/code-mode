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
from .procutil import pid_alive

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
        self.live_logs: List[str] = []  # output persisted while the pass runs
        self.progress: Dict[int, str] = {}  # seq -> the last progress text of that call
        self.pass_pid: Optional[int] = None
        last_pass_status = None
        for entry in entries:
            kind = entry.get("type")
            if kind == "start":
                self.start = entry
            elif kind == "log":
                self.live_logs.append(str(entry.get("text", "")))
            elif kind == "progress":
                self.progress[int(entry.get("seq", 0))] = str(entry.get("text", ""))
            elif kind == "call":
                self.calls[int(entry["seq"])] = entry
            elif kind == "approval":
                self.approved.update(int(s) for s in entry.get("seqs", []))
                self.policy_changed = self.policy_changed or bool(entry.get("policy_changed"))
            elif kind == "pass_start":
                self.passes += 1
                self.pass_pid = entry.get("pid")
                self.live_logs = []  # each pass runs the program from the top
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
        lines = path.read_bytes().split(b"\n")
        for number, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                # The last line may be half written: another process is
                # appending right now, or one died in the middle of a write.
                # Anything earlier that does not parse is real damage.
                if number == len(lines) - 1:
                    break
                raise NotFoundError(
                    "the log of execution %s is damaged at line %d" % (exec_id, number + 1),
                    hint="The file is %s. Start the program again; the entries before that line can still be read by hand." % path,
                )
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
        for attempt in range(6):
            if _create_exclusive(path):
                break
            owner = _read_pid(path)
            if (owner is not None and pid_alive(owner)) or attempt == 5:
                raise BusyError(
                    "execution %s is being run by another process (pid %s)" % (exec_id, owner or "?"),
                    hint="Wait for it: %s" % launcher.command("wait", exec_id),
                )
            # A lock whose owner is dead is left over from a process that was
            # killed. Clearing it is done by one process at a time, so two
            # that find it together cannot both end up holding the lock. The
            # loop is for the moments when the file is busy: being read by
            # another process, or half deleted.
            self._clear_if_stale(path)
            time.sleep(0.02 * attempt)
        try:
            yield
        finally:
            try:
                os.unlink(str(path))
            except OSError:
                pass

    def _clear_if_stale(self, path: Path) -> None:
        guard = path.with_name(path.name + ".clearing")
        try:
            if guard.is_file() and time.time() - guard.stat().st_mtime > 30:
                guard.unlink()  # a clearer that died; the guard is seconds of work, never 30
        except OSError:
            pass
        if not _create_exclusive(guard):
            return  # someone else is looking at it; they will decide
        try:
            pid = _read_pid(path)
            if path.is_file() and (pid is None or not pid_alive(pid)):
                path.unlink()
        except OSError:
            pass
        finally:
            try:
                guard.unlink()
            except OSError:
                pass

    def holder(self, exec_id: str) -> Optional[int]:
        """The pid of the live process running a pass of this execution."""
        pid = _read_pid(self._dir / (exec_id + ".lock"))
        return pid if pid is not None and pid_alive(pid) else None

    # -- cancellation across processes ------------------------------------

    def request_cancel(self, exec_id: str) -> None:
        """Ask the process running this execution to stop it. A file, because
        that process may be another one: a detached worker, an MCP server."""
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / (exec_id + ".cancel")).write_text(str(os.getpid()), encoding="ascii")

    def cancel_requested(self, exec_id: str) -> bool:
        return (self._dir / (exec_id + ".cancel")).is_file()

    def clear_cancel(self, exec_id: str) -> None:
        try:
            (self._dir / (exec_id + ".cancel")).unlink()
        except OSError:
            pass

    def worker_log(self, exec_id: str) -> Path:
        """Where a detached worker's own output goes: the place to look when
        it died before it could write to the execution log."""
        return self._dir / (exec_id + ".worker.log")


def _create_exclusive(path: Path) -> bool:
    """Create a lock file holding this process's id. False if it exists."""
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except PermissionError:
        return False  # Windows: the file is being deleted by its owner right now
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _read_pid(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
