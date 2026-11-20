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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class CallContext:
    """What the gateway knows about a call. Passed to connectors so they can
    log or scope by it; ``agent`` is the host-stamped identity."""

    execution_id: str
    agent: str
    seq: int
    timeout_s: float


class Connector:
    """Base class. Subclasses raise ``errors.ToolFailure`` for expected
    failures; any other exception is reported as ``ToolCrashed``."""

    name = ""

    def list_tools(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def call(self, tool: str, args: Dict[str, Any], ctx: CallContext) -> Any:
        """``tool`` is the connector's own tool name, not the sanitized one."""
        raise NotImplementedError

    def close(self) -> None:
        """Release processes and handles. Must be safe to call twice."""


def build_connector(name: str, spec: Dict[str, Any], home: Path, startup_timeout_s: float) -> Connector:
    kind = spec["type"]  # validated by config.py
    if kind == "demo":
        from .demo import DemoConnector

        return DemoConnector(name, home)
    if kind == "chaos":
        from .chaos import ChaosConnector

        return ChaosConnector(name, home)
    from .mcp_stdio import McpStdioConnector

    return McpStdioConnector(name, spec, startup_timeout_s)
