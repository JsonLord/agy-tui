"""Local stdio only; no official AGY compatibility or autonomous Argus claim."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from mcp.server import MCPServer

from app.schemas import WorkPlan

from .policy import HybridError
from .service import HybridLocal

mcp = MCPServer("hybrid-local")
_service = None


def service():
    global _service
    if _service is None:
        registry, state = (
            os.environ.get("HYBRID_WORKSPACES_FILE"),
            os.environ.get("HYBRID_STATE_ROOT"),
        )
        if not registry or not state:
            raise HybridError("LOCAL_CONFIGURATION_REQUIRED")
        _service = HybridLocal(
            Path(registry),
            Path(state),
            os.environ.get("HYBRID_OPENCODE_BIN", "opencode"),
        )
    return _service


async def response(operation):
    key = uuid.uuid4().hex
    try:
        return {"correlation_id": key, **await operation()}
    except HybridError as error:
        return {"correlation_id": key, "status": "BLOCKED", "error_code": error.code}
    except Exception:  # noqa: BLE001 -- redact errors at the external protocol boundary
        return {
            "correlation_id": key,
            "status": "BLOCKED",
            "error_code": "INTERNAL_ERROR",
        }


@mcp.tool()
async def hybrid_workspace_status(workspace_id: str) -> dict:
    """Observe native workspace model selection; creates a read-only ACP session, no inference."""
    return await response(lambda: service().workspace_status(workspace_id))


@mcp.tool()
async def hybrid_run(
    workspace_id: str, task: str, session_id: str = "", timeout: int = 300
) -> dict:
    """Authorized bounded work; no model/provider selection accepted."""
    return await response(
        lambda: service().run(
            workspace_id, task, session_id=session_id, timeout=timeout
        )
    )


@mcp.tool()
async def hybrid_review(workspace_id: str, task: str, timeout: int = 300) -> dict:
    """Independent read-only reviewer; successful turn is not an acceptance verdict."""
    return await response(
        lambda: service().run(workspace_id, task, role="reviewer", timeout=timeout)
    )


@mcp.tool()
async def hybrid_supervisor_assist(
    run_id: str, advice: str, timeout: int = 300
) -> dict:
    """At most two independent read-only assistance sessions per original run."""
    return await response(lambda: service().assist(run_id, advice, timeout))


@mcp.tool()
async def hybrid_status(run_id: str = "") -> dict:
    """Read persisted run/session evidence without opening OpenCode."""

    async def status():
        return service().store.status(run_id) if run_id else service().store.latest()

    return await response(status)


@mcp.tool()
async def hybrid_start(task: str, project_root: str, approved_plan: dict) -> dict:
    """Restricted legacy shape: root must already be registered; plan is user-supplied scope."""

    async def start():
        plan = WorkPlan.model_validate(approved_plan)
        runtime = service()
        matches = [
            w.id
            for w in runtime.registry.workspaces.values()
            if w.root == Path(project_root).resolve()
        ]
        if len(matches) != 1:
            raise HybridError("UNKNOWN_WORKSPACE")
        workspace = runtime.registry.get(matches[0])
        for path in plan.allowed_files:
            workspace.file(path, write=True)
        return await runtime.run(
            matches[0], task + "\nUser-authorized strategy:\n" + plan.model_dump_json()
        )

    return await response(start)


@mcp.tool()
async def hybrid_resume(run_id: str, cloud_advice: str) -> dict:
    """Resume only our engineer session, same root and observed model."""

    async def resume():
        runtime = service()
        previous = runtime.store.status(run_id)
        if previous["recovery_required"] or previous["status"] in {
            "STARTING",
            "RUNNING",
        }:
            raise HybridError("REMOTE_STOP_MUST_BE_VERIFIED")
        if previous["role"] != "engineer" or not previous["session_id"]:
            raise HybridError("NO_RESUMABLE_ENGINEER_SESSION")
        return await runtime.run(
            previous["workspace_id"], cloud_advice, session_id=previous["session_id"]
        )

    return await response(resume)
