import json
import re

from google.antigravity import (
    Agent,
    LocalOpenAIAgentConfig,
    types,
)

from app.config import Settings
from app.schemas import WorkPlan


PLANNER_SYSTEM = """
You are the planning and supervisory layer of a software-engineering system.

Another model will perform the actual repository modifications.

Your responsibilities:
- understand the requested change
- reason about repository structure
- identify likely relevant files
- produce a concise implementation plan
- define constraints
- define verification requirements
- produce a clear work order for the coding worker

Do not claim that files were changed.
Do not attempt implementation.

GROUNDING RULES:
- Base the plan only on repository information present in the supplied snapshot.
- Do not invent functions, tests, dependencies, APIs, docstrings, or conventions.
- If something is unknown, state the uncertainty instead of assuming it.
- Prefer repository-relative paths in the "files" field.
- Keep the plan implementation-focused and concise.
- Do not propose unnecessary changes beyond the user's task.


Return exactly one JSON object.

Required structure:

{
  "goal": "string",
  "files": ["string"],
  "steps": ["string"],
  "constraints": ["string"],
  "verification": ["string"],
  "risk": "low|medium|high",
  "worker_instruction": "string"
}
"""


def _extract_json(text: str) -> dict:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)

    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1:
            raise RuntimeError(
                f"Planner returned no JSON object:\n{text}"
            )

        return json.loads(text[start : end + 1])


async def create_plan(
    settings: Settings,
    task: str,
    snapshot: str,
    previous_plan: WorkPlan | None = None,
    failure: str | None = None,
) -> WorkPlan:

    config = LocalOpenAIAgentConfig(
        model=settings.planner_model,
        base_url=settings.planner_base_url,
        system_instructions=PLANNER_SYSTEM,
        capabilities=types.CapabilitiesConfig(
            enabled_tools=[],
        ),
    )

    extra = ""

    if previous_plan is not None:
        extra += (
            "\n\nPREVIOUS PLAN:\n"
            + previous_plan.model_dump_json(indent=2)
        )

    if failure:
        extra += (
            "\n\nLATEST VALIDATION FAILURE:\n"
            + failure
        )

    prompt = f"""
USER TASK:

{task}

REPOSITORY SNAPSHOT:

{snapshot}

{extra}

Produce the implementation plan now.
"""

    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        text = await response.text()

    data = _extract_json(text)

    return WorkPlan.model_validate(data)
