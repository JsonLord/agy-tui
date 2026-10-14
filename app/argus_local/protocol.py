"""Strict AGY envelopes, distinct from Argus-native messages/artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum

from pydantic import Field, model_validator

from app.factory_local.schemas import Contract, FactoryEvidenceRef, FactoryPlan
from app.factory_local.store import FactoryError

from .security import ensure_sanitized

MAX_PAYLOAD = 250_000


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def envelope(text: str, sentinel: str, error: str):
    start, end = f"<{sentinel}>", f"</{sentinel}>"
    if not isinstance(text, str) or len(text.encode()) > MAX_PAYLOAD:
        raise FactoryError(error)
    value = text.strip()
    if (
        not value.startswith(start)
        or not value.endswith(end)
        or value.count(start) != 1
        or value.count(end) != 1
    ):
        raise FactoryError(error)
    try:
        parsed = json.loads(
            value[len(start) : -len(end)],
            object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(parsed, dict):
            raise TypeError()
        return parsed
    except (ValueError, TypeError, RecursionError):
        raise FactoryError(error) from None


class KnownProject(Contract):
    workspace_id: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=300)
    project_path: str = Field(min_length=1, max_length=1000)
    argus_project_id: str = Field(min_length=1, max_length=200)


class ProjectRegistry(Contract):
    projects: tuple[KnownProject, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self):
        if len({p.workspace_id for p in self.projects}) != len(self.projects):
            raise ValueError("duplicate workspace")
        if len({p.argus_project_id for p in self.projects}) != len(self.projects):
            raise ValueError("duplicate Argus project")
        return self

    def project(self, workspace_id):
        project = next(
            (p for p in self.projects if p.workspace_id == workspace_id), None
        )
        if project is None:
            raise FactoryError("ARGUS_UNKNOWN_WORKSPACE")
        return project


class ArgusPlanImporter:
    def __init__(self, registry: ProjectRegistry):
        self.registry = registry

    def parse(self, text, project_id):
        from pydantic import ValidationError

        try:
            plan = FactoryPlan.model_validate(
                envelope(text, "AGY_FACTORY_PLAN", "ARGUS_PLAN_INVALID")
            )
        except ValidationError:
            raise FactoryError("ARGUS_PLAN_INVALID") from None
        self.validate(plan, project_id)
        return plan

    def validate(self, plan, project_id):
        ensure_sanitized(plan.model_dump(mode="json"))
        if plan.goal.argus_project_id != project_id:
            raise FactoryError("ARGUS_PROJECT_MISMATCH")
        ids = [task.argus_task_id for task in plan.tasks]
        # /ask creates no native task IDs. Null is honest; provided native IDs survive unchanged.
        if len({i for i in ids if i is not None}) != sum(i is not None for i in ids):
            raise FactoryError("ARGUS_TASK_ID_CONFLICT")
        for task in plan.tasks:
            registered = self.registry.project(task.workspace_id)
            if (task.repository, task.project_path, project_id) != (
                registered.repository,
                registered.project_path,
                registered.argus_project_id,
            ):
                raise FactoryError("ARGUS_PROJECT_MISMATCH")
        return plan


class ReviewVerdict(str, Enum):
    ACCEPTED = "ACCEPTED"
    MORE_WORK_REQUIRED = "MORE_WORK_REQUIRED"
    BLOCKED = "BLOCKED"
    ESCALATE = "ESCALATE"


class FactoryReviewResult(Contract):
    review_id: str = Field(min_length=1, max_length=100)
    plan_id: str = Field(min_length=1, max_length=100)
    plan_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(min_length=1, max_length=100)
    argus_project_id: str = Field(min_length=1, max_length=200)
    status: ReviewVerdict
    summary: str = Field(min_length=1, max_length=2000)
    remaining_gaps: tuple[str, ...] = Field(default=(), max_length=10)
    recommended_followup: str = Field(default="", max_length=1000)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=30)
    replan_recommended: bool = False
    reviewed_at: datetime

    @model_validator(mode="after")
    def coherent(self):
        if self.reviewed_at.tzinfo is None or any(
            not 1 <= len(gap) <= 500 for gap in self.remaining_gaps
        ):
            raise ValueError("invalid timestamp/gaps")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("duplicate evidence")
        if self.status == ReviewVerdict.ACCEPTED and (
            self.remaining_gaps or self.replan_recommended
        ):
            raise ValueError("incoherent acceptance")
        return self

    @property
    def accepted(self):
        return self.status == ReviewVerdict.ACCEPTED


class ReviewEvidence(Contract):
    refs: tuple[FactoryEvidenceRef, ...] = Field(min_length=1, max_length=30)
    sanitized_summary: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def unique(self):
        if len({r.evidence_id for r in self.refs}) != len(self.refs):
            raise ValueError("duplicate evidence")
        return self


def parse_review(text, plan, task_id, evidence: ReviewEvidence):
    from pydantic import ValidationError

    try:
        result = FactoryReviewResult.model_validate(
            envelope(text, "AGY_FACTORY_REVIEW", "ARGUS_REVIEW_INVALID")
        )
    except ValidationError:
        raise FactoryError("ARGUS_REVIEW_INVALID") from None
    if (
        result.plan_id,
        result.plan_revision,
        result.task_id,
        result.argus_project_id,
    ) != (plan.plan_id, plan.revision(), task_id, plan.goal.argus_project_id):
        raise FactoryError("ARGUS_REVIEW_INVALID")
    if not set(result.evidence_refs).issubset({r.evidence_id for r in evidence.refs}):
        raise FactoryError("ARGUS_REVIEW_INVALID")
    ensure_sanitized(result.model_dump(mode="json"))
    return result
