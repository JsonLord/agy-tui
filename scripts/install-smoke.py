#!/usr/bin/env python3
"""Local stdio check with no OpenCode session or provider invocation."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("hybrid-local.py"))],
        env=dict(os.environ),
    )
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {
            "hybrid_run",
            "hybrid_review",
            "hybrid_status",
            "hybrid_workspace_status",
            "hybrid_supervisor_assist",
            "hybrid_start",
            "hybrid_resume",
        } <= {t.name for t in tools.tools}
        status = await session.call_tool("hybrid_status", {})
        assert not status.is_error
        assert json.loads(status.content[0].text).get("status") != "BLOCKED"
    print("STDIO_SMOKE_OK")


if __name__ == "__main__":
    asyncio.run(main())
