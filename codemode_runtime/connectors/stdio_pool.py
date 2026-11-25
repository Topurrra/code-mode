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

The limit: each copy is a separate process with its own memory. A server
that keeps something between calls in memory (an open session, a cache that
must be shared, a counter) sees only the calls its own copy was given, so
such a server must not be given ``processes``. A server that keeps its
state in files or a database is fine, as long as two calls may touch it at
once; ``serial`` keeps the tools that must not overlap one at a time.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import CallContext, Connector


def pick_copy(busy: List[bool], limit: int) -> Optional[int]:
    """Which copy takes the next call. ``busy`` has one entry per copy made
    so far. The first idle copy, in order, so that calls go to servers that
    are already running; else a new copy at the end while there are fewer
    than ``limit``; else None: the caller waits for one to be free."""
    for index, taken in enumerate(busy):
        if not taken:
            return index
    if len(busy) < limit:
        return len(busy)
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
        self._cond = threading.Condition()
        self._holding: Dict[Tuple[str, int], Connector] = {}  # (execution, seq) -> the copy running it

    @property
    def copies(self) -> int:
        """How many copies were made so far (each starts its server on use)."""
        return len(self._copies)

    def _take(self) -> int:
        with self._cond:
            while True:
                index = pick_copy(self._busy, self._size)
                if index is not None:
                    break
                self._cond.wait()
            if index == len(self._copies):
                self._copies.append(self._make())
                self._busy.append(False)
            self._busy[index] = True
            return index

    def _give_back(self, index: int) -> None:
        with self._cond:
            self._busy[index] = False
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
        index = self._take()
        copy = self._copies[index]
        key = (ctx.execution_id, ctx.seq)
        with self._cond:
            self._holding[key] = copy
        try:
            return copy.call(tool, args, ctx)
        finally:
            with self._cond:
                if self._holding.get(key) is copy:
                    del self._holding[key]
            self._give_back(index)

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
