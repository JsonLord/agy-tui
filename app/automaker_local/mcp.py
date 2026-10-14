"""stdio-only Automaker MCP adapter; Automaker stays authoritative.

Tool classification is declared once in policy.TOOL_CLASSES (READ vs CONTROL).
CONTROL tools mutate Automaker state and are explicit operator/agent actions;
a future ClickUp approval tier will gate them. No generic HTTP access exists.
"""

from __future__ import annotations

import asyncio
import uuid

from mcp.server import MCPServer
from pydantic import ValidationError

from .client import AutomakerClient
from .policy import AutomakerError, environment
from .schemas import (
    JobCreateInput,
    JobUpdateInput,
    autonomy_summary,
    backlog_summary,
    control_result,
    health_summary,
    job_summaries,
    job_summary,
    project_summaries,
    running_agent_summaries,
)

mcp = MCPServer("automaker-local")
_client: AutomakerClient | None = None


def client() -> AutomakerClient:
    global _client
    if _client is None:
        base_url, api_key = environment()
        _client = AutomakerClient(base_url, api_key)
    return _client


async def response(operation):
    key = uuid.uuid4().hex
    try:
        return {"correlation_id": key, **await operation()}
    except AutomakerError as error:
        return {"correlation_id": key, "status": "BLOCKED", "error_code": error.code}
    except ValidationError:
        return {
            "correlation_id": key,
            "status": "BLOCKED",
            "error_code": "AUTOMAKER_REQUEST_REJECTED",
        }
    except Exception:  # noqa: BLE001 -- redact errors at the external protocol boundary
        return {
            "correlation_id": key,
            "status": "BLOCKED",
            "error_code": "INTERNAL_ERROR",
        }


async def observe(call, *args):
    return await asyncio.to_thread(call, *args)


# --- READ tools (state-neutral observation; safe for monitoring loops) ------


@mcp.tool()
async def automaker_health() -> dict:
    """READ tool: Automaker server liveness, version and timestamp."""

    async def call():
        return health_summary(await observe(client().health))

    return await response(call)


@mcp.tool()
async def automaker_projects() -> dict:
    """READ tool: bounded normalized summaries of all registered projects."""

    async def call():
        return project_summaries(await observe(client().projects_overview))

    return await response(call)


@mcp.tool()
async def automaker_jobs(project_path: str) -> dict:
    """READ tool: normalized job (feature) summaries for one Automaker project."""

    async def call():
        payload = await observe(client().features_list, project_path)
        return job_summaries(payload)

    return await response(call)


@mcp.tool()
async def automaker_job_status(project_path: str, feature_id: str) -> dict:
    """READ tool: one bounded normalized job status including description/error."""

    async def call():
        payload = await observe(client().feature_get, project_path, feature_id)
        feature = payload.get("feature")
        if not isinstance(feature, dict):
            raise AutomakerError("AUTOMAKER_NOT_FOUND")
        return {"job": job_summary(feature, detailed=True)}

    return await response(call)


@mcp.tool()
async def automaker_running_agents() -> dict:
    """READ tool: currently active executor/worker executions across projects."""

    async def call():
        return running_agent_summaries(await observe(client().running_agents))

    return await response(call)


@mcp.tool()
async def automaker_autonomy_status(project_path: str = "") -> dict:
    """READ tool: auto-mode state. Automaker uses POST here but it mutates nothing."""

    async def call():
        return autonomy_summary(await observe(client().auto_mode_status, project_path))

    return await response(call)


@mcp.tool()
async def automaker_backlog_status(project_path: str = "") -> dict:
    """READ tool: backlog-plan generation state and saved-plan presence."""

    async def call():
        return backlog_summary(await observe(client().backlog_status, project_path))

    return await response(call)


# --- CONTROL tools (mutate Automaker state; explicit operator/agent actions) -


@mcp.tool()
async def automaker_job_create(project_path: str, job: dict) -> dict:
    """CONTROL tool: create one job; restricted typed schema, no model/provider fields."""

    async def call():
        feature = JobCreateInput.model_validate(job).to_feature()
        payload = await observe(client().feature_create, project_path, feature)
        created = payload.get("feature")
        if not isinstance(created, dict):
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE")
        return {"job": job_summary(created, detailed=True)}

    return await response(call)


@mcp.tool()
async def automaker_job_update(
    project_path: str, feature_id: str, updates: dict
) -> dict:
    """CONTROL tool: update whitelisted job fields; no delete/bulk access."""

    async def call():
        payload = JobUpdateInput.model_validate(updates).to_updates()
        result = await observe(
            client().feature_update, project_path, feature_id, payload
        )
        feature = result.get("feature")
        if not isinstance(feature, dict):
            raise AutomakerError("AUTOMAKER_BAD_RESPONSE")
        return {"job": job_summary(feature, detailed=True)}

    return await response(call)


@mcp.tool()
async def automaker_worker_start(session_id: str, working_directory: str = "") -> dict:
    """CONTROL tool: start one agent conversation session; no model selection."""

    async def call():
        if not session_id.strip():
            raise AutomakerError("AUTOMAKER_REQUEST_REJECTED")
        result = await observe(client().agent_start, session_id, working_directory)
        return {"result": control_result(result, keys=("success", "sessionId"))}

    return await response(call)


@mcp.tool()
async def automaker_worker_stop(session_id: str) -> dict:
    """CONTROL tool: stop one explicit session; never a broad stop-all action."""

    async def call():
        if not session_id.strip():
            raise AutomakerError("AUTOMAKER_REQUEST_REJECTED")
        result = await observe(client().agent_stop, session_id)
        return {"result": control_result(result, keys=("success",))}

    return await response(call)
