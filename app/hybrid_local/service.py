from __future__ import annotations

import asyncio
import hashlib
import math
from pathlib import Path

from .acp import OpenCodeACP
from .policy import ROLES, HybridError, WorkspaceRegistry
from .store import RunStore


class HybridLocal:
    def __init__(
        self,
        registry,
        state,
        binary="opencode",
        *,
        environment=None,
        adapter=OpenCodeACP,
    ):
        self.registry = WorkspaceRegistry(Path(registry))
        self.store = RunStore(Path(state) / "hybrid-local.sqlite3")
        self.binary, self.environment, self.adapter = binary, environment, adapter

    def client(self, workspace, role, run_id=""):
        return self.adapter(
            self.binary,
            workspace,
            role,
            environment=self.environment,
            audit=lambda kind, data: self.store.audit(run_id, kind, data),
        )

    async def workspace_status(self, workspace_id):
        workspace = self.registry.get(workspace_id)
        async with self.client(workspace, "reviewer") as client:
            session = await client.session()
        return {
            "workspace_id": workspace_id,
            **session,
            "capabilities": sorted(workspace.capabilities),
            "provider_invocation_verified": False,
            "desktop_session_shared": False,
        }

    async def run(
        self,
        workspace_id,
        task,
        *,
        role="engineer",
        session_id="",
        timeout=300,
        parent_id="",
    ):
        workspace = self.registry.get(workspace_id)
        if (
            role not in ROLES
            or not isinstance(task, str)
            or not task.strip()
            or len(task) > 131072
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 1 <= timeout <= 28800
        ):
            raise HybridError("INVALID_RUN_REQUEST")
        if role == "engineer" and "edit" not in workspace.capabilities:
            raise HybridError("CAPABILITY_DENIED")
        old = (
            self.store.verify_session(session_id, workspace, role)
            if session_id
            else None
        )
        key = self.store.begin(workspace, role, timeout, parent_id)
        self.store.audit(
            key, "task", {"sha256": hashlib.sha256(task.encode()).hexdigest()}
        )
        client = self.client(workspace, role, key)
        try:
            async with asyncio.timeout(timeout), client:
                session = await client.session(session_id, old["model"] if old else "")
                self.store.bind(key, workspace, session)
                result = await client.prompt(task, timeout)
            self.store.finish(key, "COMPLETED_AWAITING_VALIDATION")
            return {**self.store.status(key), **result}
        except BaseException as error:
            code = (
                error.code
                if isinstance(error, HybridError)
                else "TURN_TIMEOUT"
                if isinstance(error, TimeoutError)
                else "LOCAL_EXECUTION_ERROR"
            )
            self.store.finish(
                key, "REMOTE_STOP_UNCONFIRMED" if client.prompting else "FAILED", code
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            return self.store.status(key)

    async def assist(self, run_id, advice, timeout=300):
        parent = self.store.status(run_id)
        return await self.run(
            parent["workspace_id"],
            advice,
            role="supervisor",
            parent_id=run_id,
            timeout=timeout,
        )
