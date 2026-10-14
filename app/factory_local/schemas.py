"""AGY interchange types, not an Argus-native output schema."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactoryApprovalStatus(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FactoryExecutionState(str, Enum):
    NOT_MATERIALIZED = "NOT_MATERIALIZED"
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    BLOCKED = "BLOCKED"


class FactoryGoalRef(Contract):
    goal_id: str = Field(min_length=1, max_length=100)
    argus_project_id: str | None = Field(default=None, max_length=200)


class FactoryPlanTask(Contract):
    task_id: str = Field(min_length=1, max_length=100)
    argus_task_id: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=18000)
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=300)
    project_path: str = Field(min_length=1, max_length=1000)


class FactoryDependency(Contract):
    task_id: str = Field(min_length=1, max_length=100)
    requires_task_id: str = Field(min_length=1, max_length=100)


class FactoryPlan(Contract):
    schema_version: int = Field(default=1, ge=1, le=1)
    plan_id: str = Field(min_length=1, max_length=100)
    goal: FactoryGoalRef
    tasks: tuple[FactoryPlanTask, ...] = Field(min_length=1, max_length=100)
    dependencies: tuple[FactoryDependency, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = {t.task_id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate task IDs")
        edges = {(d.task_id, d.requires_task_id) for d in self.dependencies}
        if len(edges) != len(self.dependencies):
            raise ValueError("duplicate dependencies")
        by_id = {t.task_id: t for t in self.tasks}
        for a, b in edges:
            if a not in ids or b not in ids or a == b:
                raise ValueError("invalid dependency")
            if by_id[a].project_path != by_id[b].project_path:
                raise ValueError("cross-project dependency unsupported")
        self.ordered_tasks()
        return self

    def ordered_tasks(self):
        remaining = list(self.tasks)
        done = set()
        ordered = []
        while remaining:
            ready = [
                t
                for t in remaining
                if all(
                    d.requires_task_id in done
                    for d in self.dependencies
                    if d.task_id == t.task_id
                )
            ]
            if not ready:
                raise ValueError("dependency cycle")
            for task in ready:
                ordered.append(task)
                done.add(task.task_id)
                remaining.remove(task)
        return ordered

    def revision(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


class FactoryApproval(Contract):
    approval_id: str = Field(min_length=1, max_length=100)
    plan_id: str = Field(min_length=1, max_length=100)
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str = Field(pattern=r"^manual$")
    status: FactoryApprovalStatus
    timestamp: datetime
    actor_ref: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def timezone_required(self):
        if self.timestamp.tzinfo is None:
            raise ValueError("timezone required")
        return self


class FactoryJobRef(Contract):
    task_id: str
    automaker_job_id: str | None = None
    approval_ref: str | None = None
    idempotency_key: str
    foreman_run_id: str | None = None


class FactoryWorkerRef(Contract):
    worker_id: str
    workspace_id: str


class FactoryAttemptRef(Contract):
    attempt_id: str
    foreman_run_id: str | None = None


class FactoryEvidenceRef(Contract):
    evidence_id: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=100)
    artifact_ref: str = Field(min_length=1, max_length=500)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class FactoryCompletion(Contract):
    task_id: str
    reviewer_ref: str
    accepted: bool
    evidence: tuple[FactoryEvidenceRef, ...] = ()


class FactoryBlocker(Contract):
    code: str
    task_id: str | None = None


class FactoryExecutor(Protocol):
    """Future assigned-attempt seam. Never called by this phase."""

    def start(self, job: FactoryJobRef, workspace: str) -> FactoryAttemptRef: ...
    def status(self, run: FactoryAttemptRef) -> FactoryExecutionState: ...
    def cancel(self, run: FactoryAttemptRef) -> None: ...


RAW_STATES = {
    "pending": FactoryExecutionState.PENDING,
    "running": FactoryExecutionState.RUNNING,
    "completed": FactoryExecutionState.VERIFYING,
    "verified": FactoryExecutionState.COMPLETED,
    "failed": FactoryExecutionState.FAILED,
}


def normalize_execution(raw):
    return RAW_STATES.get(raw, FactoryExecutionState.BLOCKED)
