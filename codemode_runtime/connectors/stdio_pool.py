# SPDX-License-Identifier: MIT
"""Several copies of one stdio MCP server, for ``"processes": N``.

Many simple servers answer one request at a time: while a long build runs,
a quick read waits behind it. When the server cannot be changed, the runtime
can start more than one copy of it and give each copy one call at a time.

- Copies start lazily. The first call starts one; another is started only
  when every copy started so far is busy, up to N. Looking up the tools
  (``doctor``, ``init``, the catalog) therefore starts one server, not N.
- Each copy is an ordinary stdio connector: a copy that exits is started
  again on its next call, and its failed call says so.
- The tool list comes from one copy; ``cancel`` goes to the copy that has
  the call; ``secrets`` is what any copy holds; ``close`` stops them all,
  side by side.
- The engine lets at most N calls of this connector run at once, and a call
  that waits for a free copy starts its time limit only when it gets one.
- When the gateway gives up on a call, its place is free at once but its
  copy may still be busy. A call that then finds no free copy waits no
  longer than its own time limit, and fails as a timeout without starting.
- A copy whose last call timed out may still be at work on it (with the
  default ``on_abandon``, a server that ignores the cancel goes on). It is
  given a call only when no other copy is idle and no new one may be
  started; once a call on it gets an answer, it is trusted again.

The limit: each copy is a separate process with its own memory. A server
that keeps something between calls in memory (an open session, a cache that
must be shared, a counter) sees only the calls its own copy was given, so
such a server must not be given ``processes``. A server that keeps its
state in files or a database is fine, as long as two calls may touch it at
once; ``serial`` runs the tools that must not overlap one at a time, within
one execution or one ``serve`` process.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..errors import ToolFailure
from . import CallContext, Connector


def pick_copy(busy: List[bool], limit: int, timed_out: Optional[List[bool]] = None) -> Optional[int]:
    """Which copy takes the next call. ``busy`` has one entry per copy made
    so far; ``timed_out`` says, for each, whether its last call ended in a
    timeout (the server may still be at work on it, behind its back).

    The first idle copy whose last call did not time out, in order, so that
    calls go to servers that are already running; else a new copy at the end
    while there are fewer than ``limit``; else the first idle copy whose last
    call timed out; else None: the caller waits for one to be free."""
    late = timed_out or []
    for index, taken in enumerate(busy):
        if not taken and not (index < len(late) and late[index]):
            return index
    if len(busy) < limit:
        return len(busy)
    for index, taken in enumerate(busy):
        if not taken:
            return index
    return None


class StdioPool(Connector):
    """Up to ``size`` copies of a stdio server, one call at a time each."""

    cacheable = True
    transport = "stdio"

    def __init__(self, name: str, size: int, make: Callable[[], Connector]) -> None:
        self.name = name
        self._size = max(1, int(size))
        self._make = make
        # Making a copy starts nothing: its server starts on its first call.
        self._copies: List[Connector] = [make()]
        self._busy: List[bool] = [False]
        # Whether the last call of each copy ended in a timeout. With
        # on_abandon "notify" a server that ignores the cancel is still at
        # work on it, and a call given to it would wait behind that work.
        self._timed_out: List[bool] = [False]
        self._cond = threading.Condition()
        self._holding: Dict[Tuple[str, int], Connector] = {}  # (execution, seq) -> the copy running it

    @property
    def copies(self) -> int:
        """How many copies were made so far (each starts its server on use)."""
        return len(self._copies)

    def _take(self, within: Optional[float] = None) -> Optional[int]:
        """A copy for one call, marked busy. Waits for one to be free: for
        ``within`` seconds at most, then None; with no limit, for as long
        as it takes."""
        deadline = None if within is None else time.monotonic() + within
        with self._cond:
            while True:
                index = pick_copy(self._busy, self._size, self._timed_out)
                if index is not None:
                    break
                if deadline is None:
                    self._cond.wait()
                    continue
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            if index == len(self._copies):
                self._copies.append(self._make())
                self._busy.append(False)
                self._timed_out.append(False)
            self._busy[index] = True
            return index

    def _give_back(self, index: int, timed_out: Optional[bool] = None) -> None:
        """The copy is free again. ``timed_out``: whether its call ended in
        a timeout; None when the call says nothing about the server."""
        with self._cond:
            self._busy[index] = False
            if timed_out is not None:
                self._timed_out[index] = timed_out
            self._cond.notify()

    def stamp(self) -> str:
        return self._copies[0].stamp()

    def list_tools(self) -> List[Dict[str, Any]]:
        index = self._take()
        try:
            return self._copies[index].list_tools()
        finally:
            self._give_back(index)

    def call(self, tool: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        # The gateway frees a call's place when it gives up on it, while
        # the copy may still be busy with it. A call that then finds no free
        # copy must not wait for ever and start once nobody wants it: it
        # waits as long as its own time limit, and fails without starting.
        index = self._take(within=ctx.timeout_s)
        if index is None:
            failure = ToolFailure(
                "ToolTimeout",
                "no copy of MCP server %r was free within %gs (all %d were busy); the call was not started"
                % (self.name, ctx.timeout_s, self._size),
                "It did not run. The copies were busy with earlier calls, some of which the gateway may have given up "
                "on. Try again later, or raise processes on this connector.",
            )
            failure.never_started = True  # type: ignore[attr-defined]  # the gateway keeps this message
            raise failure
        copy = self._copies[index]
        key = (ctx.execution_id, ctx.seq)
        with self._cond:
            self._holding[key] = copy
        timed_out: Optional[bool] = None
        try:
            result = copy.call(tool, args, ctx)
            timed_out = False
            return result
        except ToolFailure as failure:
            # A copy is trusted again once a call on it got an answer, an
            # error from the tool included. A copy that exited is started
            # again on its next call, so it is not behind anything either.
            timed_out = failure.kind == "ToolTimeout"
            raise
        finally:
            with self._cond:
                if self._holding.get(key) is copy:
                    del self._holding[key]
            self._give_back(index, timed_out)

    def cancel(self, ctx: CallContext) -> None:
        with self._cond:
            copy = self._holding.get((ctx.execution_id, ctx.seq))
        if copy is not None:
            copy.cancel(ctx)

    def secrets(self) -> List[str]:
        with self._cond:
            copies = list(self._copies)
        seen: List[str] = []
        for copy in copies:
            for value in copy.secrets():
                if value not in seen:
                    seen.append(value)
        return seen

    def close(self) -> None:
        with self._cond:
            to_stop = list(self._copies)

        def stop(copy: Connector) -> None:
            try:
                copy.close()
            except Exception:
                pass  # best effort, like every close

        threads = [threading.Thread(target=stop, args=(copy,), daemon=True) for copy in to_stop]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
