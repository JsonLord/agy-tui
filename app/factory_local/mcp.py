"""Narrow stdio import/approval/materialization surface; no execution tools."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from pathlib import Path

from mcp.server import MCPServer
from pydantic import ValidationError

from app.automaker_local.client import AutomakerClient
from app.automaker_local.policy import AutomakerError, environment

from .schemas import FactoryApproval, FactoryPlan
from .service import FactoryService
from .store import FactoryError, FactoryStore

mcp = MCPServer("factory-local")
TOOL_CLASSES = {
    "factory_status": "READ",
    "factory_plan_status": "READ",
    "factory_job_map": "READ",
    "factory_blockers": "READ",
    "factory_register_plan": "CONTROL",
    "factory_record_approval": "CONTROL",
    "factory_materialize_approved_plan": "CONTROL",
    "factory_argus_status": "READ",
    "factory_argus_plan_preview": "READ",
    "factory_argus_reviews": "READ",
    "factory_plan_goal": "CONTROL",
    "factory_import_argus_plan": "CONTROL",
    "factory_review_completion": "CONTROL",
}
_service = None


class LazyAutomaker:
    """No external configuration needed to import/register/read stored refs."""

    def __getattr__(self, name):
        if name not in {
            "projects_overview",
            "auto_mode_status",
            "feature_get",
            "features_list",
            "feature_create",
        }:
            raise AttributeError(name)
        base, key = environment()
        return getattr(AutomakerClient(base, key), name)


def service():
    global _service
    if _service is None:
        raw = os.environ.get("FACTORY_STATE_PATH")
        if not raw or not Path(raw).is_absolute():
            raise FactoryError("FACTORY_STATE_PATH_REQUIRED")
        _service = FactoryService(FactoryStore(Path(raw)), LazyAutomaker())
    return _service


async def response(call):
    correlation = uuid.uuid4().hex
    try:
        result = (
            await call()
            if inspect.iscoroutinefunction(call)
            else await asyncio.to_thread(call)
        )
        return {"correlation_id": correlation, **result}
    except (FactoryError, AutomakerError) as error:
        return {
            "correlation_id": correlation,
            "status": "BLOCKED",
            "error_code": error.code,
        }
    except ValidationError:
        return {
            "correlation_id": correlation,
            "status": "BLOCKED",
            "error_code": "FACTORY_INVALID_INPUT",
        }
    except Exception:  # noqa: BLE001 -- redact at the external boundary
        return {
            "correlation_id": correlation,
            "status": "BLOCKED",
            "error_code": "INTERNAL_ERROR",
        }


@mcp.tool()
async def factory_status() -> dict:
    """READ normalized goals and live canonical Automaker states."""
    return await response(lambda: service().status())


@mcp.tool()
async def factory_plan_status(plan_id: str) -> dict:
    """READ approval separately from execution and dependency readiness."""
    return await response(lambda: service().plan_status(plan_id))


@mcp.tool()
async def factory_job_map(plan_id: str) -> dict:
    """READ stored cross-system references; no external runtime files."""
    return await response(lambda: service().job_map(plan_id))


@mcp.tool()
async def factory_blockers(plan_id: str) -> dict:
    """READ unresolved approval, reconciliation and external state decisions."""
    return await response(
        lambda: {"blockers": service().plan_status(plan_id)["blockers"]}
    )


@mcp.tool()
async def factory_register_plan(plan: dict) -> dict:
    """CONTROL import AGY FactoryPlan (not official Argus output). Immutable ID/revision."""
    return await response(
        lambda: service().register_plan(FactoryPlan.model_validate(plan))
    )


@mcp.tool()
async def factory_record_approval(approval: dict) -> dict:
    """CONTROL trusted operator records revision-bound manual approval; no ClickUp verification."""
    return await response(
        lambda: service().record_approval(FactoryApproval.model_validate(approval))
    )


@mcp.tool()
async def factory_materialize_approved_plan(plan_id: str) -> dict:
    """CONTROL create/reconcile jobs only; never launch a worker or auto-mode."""
    return await response(lambda: service().materialize_approved_plan(plan_id))


_bridge = None


def bridge():
    global _bridge
    if _bridge is None:
        from app.argus_local.client import ArgusMcpClient
        from app.argus_local.config import load_registry
        from app.argus_local.service import ArgusFactoryBridge

        registry = load_registry(os.environ.get("FACTORY_PROJECT_REGISTRY"))
        _bridge = ArgusFactoryBridge(
            service().store,
            registry,
            ArgusMcpClient(
                os.environ.get("ARGUS_MCP_EXECUTABLE", "argus-plugin-server")
            ),
        )
    return _bridge


@mcp.tool()
async def factory_argus_status(workspace_id: str) -> dict:
    """READ registered Argus project status/artifact references and uncertain request IDs."""

    async def call():
        return await bridge().status(workspace_id)

    return await response(call)


@mcp.tool()
async def factory_argus_plan_preview(plan_id: str) -> dict:
    """READ imported AGY envelope and external reference; no new Argus generation."""
    return await response(lambda: bridge().store.preview(plan_id))


@mcp.tool()
async def factory_argus_reviews(plan_id: str) -> dict:
    """READ compact review verdicts; no transcripts or automatic replanning."""
    return await response(lambda: {"reviews": bridge().store.reviews(plan_id)})


@mcp.tool()
async def factory_plan_goal(
    workspace_id: str, goal_id: str, plan_id: str, objective: str, request_id: str
) -> dict:
    """CONTROL one bounded Manager /ask planning turn; register PROPOSED only."""

    async def call():
        return await bridge().plan_goal(
            workspace_id, goal_id, plan_id, objective, request_id
        )

    return await response(call)


@mcp.tool()
async def factory_import_argus_plan(
    argus_project_id: str, source_ref: str, payload: str
) -> dict:
    """CONTROL strictly validate/import explicit AGY_FACTORY_PLAN payload; no approval/jobs."""
    return await response(
        lambda: bridge().import_plan(argus_project_id, source_ref, payload)
    )


@mcp.tool()
async def factory_review_completion(
    plan_id: str, completion: dict, evidence: dict, request_id: str
) -> dict:
    """CONTROL separate Manager /ask review advice; no execution-state change or replan."""

    async def call():
        from app.argus_local.protocol import ReviewEvidence
        from app.factory_local.schemas import FactoryCompletion

        return await bridge().review_completion(
            plan_id,
            FactoryCompletion.model_validate(completion),
            ReviewEvidence.model_validate(evidence),
            request_id,
        )

    return await response(call)
