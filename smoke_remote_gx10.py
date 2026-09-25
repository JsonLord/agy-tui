import asyncio
import os
from pathlib import Path

from google.antigravity import Agent, LocalOpenAIAgentConfig, types
from google.antigravity.hooks import policy


LOCAL = str(Path.home() / "antigravity-hybrid")

REMOTE_GX10 = types.McpStdioServer(
    name="remote-gx10",
    command="/home/leon/bin/mcp-gx10-filesystem",
    args=[],
    enabled_tools=[
        "list_allowed_directories",
        "list_directory",
        "read_text_file",
    ],
)


async def main():
    config = LocalOpenAIAgentConfig(
        model=os.environ["WORKER_MODEL"],
        base_url=os.environ["WORKER_BASE_URL"],
        workspaces=[LOCAL],

        system_instructions=f"""
You have two separate filesystem domains.

LOCAL DEBIAN-DEVIL:
Use built-in filesystem tools.
Workspace:
{LOCAL}

REMOTE GX10:
Use remote-gx10 MCP tools only.

Exact remote tools:
- list_allowed_directories: {{}}
- list_directory: {{"path": "/absolute/path"}}
- read_text_file: {{"path": "/absolute/path"}}

Rules:
- never confuse local and remote paths
- use lowercase "path"
- never invent MCP tool aliases
- make remote MCP calls sequentially
- do not modify anything
""",

        capabilities=types.CapabilitiesConfig(
            enabled_tools=types.BuiltinTools.minimal(),
        ),

        mcp_servers=[REMOTE_GX10],

        policies=[
            policy.workspace_only([LOCAL]),
            policy.allow_all(),
        ],
    )

    async with Agent(config) as agent:
        response = await agent.chat(
            f"""
Perform these read-only checks:

1. LOCAL:
Inspect:
{LOCAL}/app/agents/worker.py

Report the first meaningful Python definition you see.

2. REMOTE:
List:
/root/vllm

Report whether docker-compose.yaml is present.

3. Clearly identify which information came from Debian-Devil
and which came from gx10-c65d.

Finish exactly with:
LOCAL_REMOTE_OK
"""
        )

        print(await response.text())


if __name__ == "__main__":
    asyncio.run(main())
