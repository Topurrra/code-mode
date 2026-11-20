# SPDX-License-Identifier: MIT
"""Drive the runtime's MCP server with the official Python SDK client.

Usage: python sdk_client.py SERVER_COMMAND [ARGS...]

Prints one JSON object: the tool names the server lists and the outcome of
one ``execute`` call. The end-to-end suite uses it to check that a client the
project did not write can use the server. It needs the ``mcp`` package, so it
runs only when the suite is given such an interpreter.
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROGRAM = "notes = tools.demo.list_notes()\n{'count': len(notes), 'first': notes[0]['title']}"


async def main() -> None:
    params = StdioServerParameters(command=sys.argv[1], args=sys.argv[2:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("execute", {"code": PROGRAM})
            print(json.dumps({
                "tools": [tool.name for tool in listed.tools],
                "isError": bool(getattr(result, "isError", False) or getattr(result, "is_error", False)),
                "structured": getattr(result, "structuredContent", None) or getattr(result, "structured_content", None),
            }))


if __name__ == "__main__":
    asyncio.run(main())
