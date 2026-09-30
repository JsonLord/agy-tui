from pathlib import Path

from google.antigravity import (
    Agent,
    LocalOpenAIAgentConfig,
    types,
)
from google.antigravity.hooks import policy

from app.config import Settings
from app.schemas import WorkPlan
from app import runtime_config

REMOTE_GX10 = types.McpStdioServer(
    name="remote-gx10",
    command=str(runtime_config.gx10_bin()),
    args=[],
    enabled_tools=[
        "list_allowed_directories",
        "list_directory",
        "list_directory_with_sizes",
        "read_text_file",
        "read_multiple_files",
        "get_file_info",
        "search_files",
        "directory_tree",
        "write_file",
        "edit_file",
        "create_directory",
        "move_file",
    ],
)



WORKER_SYSTEM = """
You are the execution layer of a software-engineering system.

A separate reasoning model has already produced the implementation plan.

Your responsibilities:
- inspect the repository
- implement the requested changes
- run appropriate commands and tests
- fix issues you encounter
- leave the working tree in the best completed state possible

Do the work directly.
Do not merely explain how to perform it.

Operate inside the supplied workspace.
Do not create unrelated files.
Do not commit or push unless explicitly instructed.
"""


async def execute_plan(
    settings: Settings,
    repo: Path,
    task: str,
    plan: WorkPlan,
    repair_context: str | None = None,
) -> str:

    repo = repo.resolve()
    workspace = str(repo)

    config = LocalOpenAIAgentConfig(
        model=settings.worker_model,
        base_url=settings.worker_base_url,
        workspaces=[workspace],
        system_instructions=WORKER_SYSTEM,
        capabilities=types.CapabilitiesConfig(
            enabled_tools=types.BuiltinTools.minimal(),
        ),
        mcp_servers=[REMOTE_GX10],

        policies=[
            policy.workspace_only([workspace]),
            policy.allow_all(),
        ],
    )

    repair = ""

    if repair_context:
        repair = f"""
A previous attempt failed validation.

VALIDATION FAILURE:

{repair_context}

Inspect the current repository state and repair the implementation.
"""

    prompt = f"""
ORIGINAL TASK:

{task}

IMPLEMENTATION PLAN:

{plan.model_dump_json(indent=2)}

{repair}

Execute the work now.

Before finishing:
1. inspect your changes
2. run appropriate tests
3. fix any failures you can
4. summarize what you changed
"""

    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        return await response.text()
