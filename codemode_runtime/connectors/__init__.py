# SPDX-License-Identifier: MIT
"""Connectors: where tools come from.

A connector is two methods. ``list_tools`` returns tool descriptors in the
shape MCP uses (``name``, ``description``, ``inputSchema``, optionally
``outputSchema``), and ``call`` runs one. Connectors live on the host: they
may hold credentials, and nothing they hold is ever sent to the sandbox.

To add a capability without touching this code, write an MCP server and point
an ``mcp-stdio`` connector at it. To add a connector type, write a class with
these two methods, register it in ``build_connector`` and add its allowed
keys to ``config.CONNECTOR_KEYS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REDACTED = "[redacted]"
_MIN_SECRET_CHARS = 6
_SECRET_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|BEARER|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY", re.I
)


@dataclass(frozen=True)
class CallContext:
    """What the gateway knows about a call. Passed to connectors so they can
    log or scope by it; ``agent`` is the host-stamped identity. ``progress``,
    when set, takes a short text about a call that is still running."""

    execution_id: str
    agent: str
    seq: int
    timeout_s: float
    progress: Optional[Callable[[str], None]] = None


class Connector:
    """Base class. Subclasses raise ``errors.ToolFailure`` for expected
    failures; any other exception is reported as ``ToolCrashed``."""

    name = ""

    def list_tools(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def call(self, tool: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        """``tool`` is the connector's own tool name, not the sanitized one."""
        raise NotImplementedError

    def cancel(self, ctx: CallContext) -> None:
        """Best effort: tell the tool source to stop the call ``ctx`` names.
        The gateway calls this when it gives up on a call (a timeout, a
        cancelled execution). The default does nothing."""

    def secrets(self) -> List[str]:
        """Values this connector was given as credentials. The gateway
        removes them from results and error messages before they are
        recorded or reach the sandbox."""
        return []

    def close(self) -> None:
        """Release processes and handles. Must be safe to call twice."""


def looks_secret(name: str) -> bool:
    """Does a variable or header name say it holds a credential?"""
    return bool(_SECRET_NAME.search(name))


def redact(value: Any, secrets: List[str]) -> Tuple[Any, bool]:
    """Replace every occurrence of a secret in the strings of a JSON value.
    Returns (value, changed). Short values are not treated as secrets: a
    two-character "secret" would shred unrelated text."""
    wanted = [s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_CHARS]
    if not wanted:
        return value, False
    changed = [False]

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            out = node
            for secret in wanted:
                if secret in out:
                    out = out.replace(secret, REDACTED)
            if out is not node and out != node:
                changed[0] = True
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {walk(key) if isinstance(key, str) else key: walk(item) for key, item in node.items()}
        return node

    return walk(value), changed[0]


def build_connector(
    name: str, spec: Dict[str, Any], home: Path, startup_timeout_s: float, base_dir: Optional[Path] = None
) -> Connector:
    kind = spec["type"]  # validated by config.py
    if kind == "demo":
        from .demo import DemoConnector

        return DemoConnector(name, home)
    if kind == "chaos":
        from .chaos import ChaosConnector

        return ChaosConnector(name, home)
    from .mcp_stdio import McpStdioConnector

    return McpStdioConnector(name, spec, float(spec.get("startup_timeout_s") or startup_timeout_s))
