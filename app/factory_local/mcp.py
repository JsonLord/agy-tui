"""Narrow stdio import/approval/materialization surface; no execution tools."""

from __future__ import annotations

import asyncio
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
        return {"correlation_id": correlation, **await asyncio.to_thread(call)}
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
