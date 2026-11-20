# SPDX-License-Identifier: MIT
"""A tiny MCP server built with the official Python SDK, for the interop test.

It is not used by the application. The end-to-end suite points an
``mcp-stdio`` connector at it to check that the runtime's hand-written client
talks to an implementation it did not write. It runs only when the suite is
given an interpreter that has the ``mcp`` package (CODEMODE_E2E_MCP_PYTHON).
"""

try:  # SDK 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # SDK 1.x, where the same class was called FastMCP
    from mcp.server.fastmcp import FastMCP as Server

server = Server("interop-fixture")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool(name="shout-text")
def shout_text(text: str, times: int = 1) -> str:
    """Upper-case a text and repeat it. The dash in the name is on purpose."""
    return " ".join([text.upper()] * times)


@server.tool()
def explode() -> str:
    """Always raises."""
    raise RuntimeError("exploded on purpose")


if __name__ == "__main__":
    server.run()
