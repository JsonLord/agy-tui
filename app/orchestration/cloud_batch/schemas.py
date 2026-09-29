"""P14 — Cloud Batch Planner schemas (Pydantic v2, fail-closed).

The cloud model emits EXACTLY ONE bounded :class:`CloudAction` per
invocation. ``CloudAction`` is a discriminated union over
:class:`CloudMode` (CLARIFY / BATCH / NEEDS_HUMAN / FINAL). Any shape
violation raises a validation error -> the planner adapter treats it as
``PlannerUnavailable``-equivalent (fail closed; a plan is never
fabricated).

Batch validation reuses the P13 delegation policy (``DelegationPlan``)
for the DAG/parallelism/shared-file rules; the P14-specific checks here
add the role/parallelism/reentry-gate constraints and never duplicate
the P2 stage-DAG logic (P13 already delegates to ``DevelopmentPlan``).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Prompt / artifact byte budgets (protect against prompt bloat replacing
# tool bloat). Hard, tested constants.
# ---------------------------------------------------------------------------
CORE_RULES_MAX_BYTES = 8192          # core system prompt
ROLE_MANIFEST_MAX_BYTES = 4096       # compact role manifest
BATCH_PROMPT_MAX_BYTES = 48 * 1024   # one full BATCH prompt (all sections)
BATCH_OUTCOME_MAX_BYTES = 16 * 1024  # whole BatchOutcome (hard reject)
BATCH_OUTCOME_SOFT_BYTES = 12 * 1024  # advisory: prefer below this

# WorkerOutcome bounds
WORKER_CONCLUSION_MAX_CHARS = 2048
WORKER_EVIDENCE_REF_MAX = 20
WORKER_RISK_MAX = 10
WORKER_UNKNOWN_MAX = 10

# Protocol bounds
CLARIFY_MAX_QUESTIONS = 8
MAX_REENTRY_CALLS_PER_GENERATION = 1

# Parallelism. P13 ``DelegationPlan.concurrency_limit`` is the authoritative
# cap (ge=1, le=4) and is reused unchanged — P14 does NOT weaken it. These
# are only the P14-side guardrails for the planner's ``parallelism`` hint.
PARALLELISM_HARD_MAX = 8
PARALLELISM_DEFAULT_TARGET = 4

#: Reentry reasons the deterministic supervisor may raise.
REENTRY_REASONS = (
    "ALL_REQUIRED_DELEGATIONS_COMPLETE",
    "BATCH_FAILED_WITH_ESCALATION",
    "REPLAN_REQUIRED",
    "HUMAN_FEEDBACK_CONTINUATION_READY",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class CloudMode(str, Enum):
    """The ONLY four project-level cloud actions. No operational mode."""

    CLARIFY = "CLARIFY"
    BATCH = "BATCH"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FINAL = "FINAL"


class CloudProjectState(str, Enum):
    """Project-level lifecycle. Cloud may be invoked only from NEW,
    READY_TO_PLAN and REENTRY_READY (enforced by the protocol, not here)."""

    NEW = "NEW"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    READY_TO_PLAN = "READY_TO_PLAN"
    BATCH_RUNNING = "BATCH_RUNNING"
    REENTRY_READY = "REENTRY_READY"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Cloud action leaf payloads
# ---------------------------------------------------------------------------


class ClarificationRequest(BaseModel):
    """Exactly one clarification turn for a NEW project.

    Questions may concern ONLY user intent (result, scope, priority,
    architecture preference, tradeoffs, autonomy, deployment, preserve,
    do-not-change, acceptance). They must NOT ask for facts workers can
    investigate, and NO worker dispatch precedes the answer.
    """

    mode: Literal[CloudMode.CLARIFY] = CloudMode.CLARIFY
    questions: list[str] = Field(min_length=1, max_length=CLARIFY_MAX_QUESTIONS)
    reasoning: str = ""

    @field_validator("questions")
    @classmethod
    def _questions_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [q.strip() for q in value if q.strip()]
        if not cleaned:
            raise ValueError("questions must not be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("questions must be unique")
        return cleaned


class DelegationSpec(BaseModel):
    """One bounded delegation the cloud planner assigns.

    Maps 1:1 onto a P13 ``DelegatedTask`` (see
    :func:`app.orchestration.cloud_batch.dispatcher.build_delegation_plan`).
    ``role`` must be a known role from the role manifest; ``deps`` are
    delegation ids.
    """

    id: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=400)
    owned_files: list[str] = Field(min_length=1, max_length=3)
    deps: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        value = value.strip()
        if not _ID_RE.fullmatch(value):
            raise ValueError(
                f"id must match {_ID_RE.pattern}, got {value!r}"
            )
        return value

    @field_validator("role", "objective")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("owned_files", "reads", "invariants", "verification", "deps")
    @classmethod
    def _no_blanks(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if "owned_files" in cls.model_fields and len(set(cleaned)) != len(cleaned):
            raise ValueError("owned_files must be unique")
        return cleaned


class ReentryGate(BaseModel):
    """Deterministic reentry condition the cloud declares for a batch.

    The gate is evaluated by the DETERMINISTIC dispatcher — the cloud
    never evaluates it itself. ``required_ids`` are the delegations that
    MUST reach a terminal state before
    ALL_REQUIRED_DELEGATIONS_COMPLETE fires; an empty set means "every
    delegation in the plan".
    """

    reason: Literal[
        "ALL_REQUIRED_DELEGATIONS_COMPLETE",
        "BATCH_FAILED_WITH_ESCALATION",
        "REPLAN_REQUIRED",
        "HUMAN_FEEDBACK_CONTINUATION_READY",
    ] = REENTRY_REASONS[0]
    required_ids: list[str] = Field(default_factory=list)

    @field_validator("required_ids")
    @classmethod
    def _ids_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [i.strip() for i in value if i.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("required_ids must be unique")
        return cleaned


class BatchPlan(BaseModel):
    """One bounded batch: a set of delegations under a known role set, a
    parallelism hint, and a deterministic reentry gate.

    P14-specific structural checks live here; the shared DAG /
    shared-file / concurrency rules are re-applied by lowering to a P13
    ``DelegationPlan`` in :mod:`dispatcher` (no duplicated DAG logic).
    """

    mode: Literal[CloudMode.BATCH] = CloudMode.BATCH
    batch_id: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=400)
    delegations: list[DelegationSpec] = Field(min_length=1)
    parallelism: int = Field(default=PARALLELISM_DEFAULT_TARGET, ge=1, le=PARALLELISM_HARD_MAX)
    reentry: ReentryGate = Field(default_factory=ReentryGate)

    @field_validator("batch_id")
    @classmethod
    def _batch_id_shape(cls, value: str) -> str:
        value = value.strip()
        if not _ID_RE.fullmatch(value):
            raise ValueError(f"batch_id must match {_ID_RE.pattern}, got {value!r}")
        return value

    @field_validator("objective")
    @classmethod
    def _objective_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _validate_delegations(self) -> "BatchPlan":
        ids = [d.id for d in self.delegations]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate delegation ids")
        known = set(ids)
        for d in self.delegations:
            for dep in d.deps:
                if dep not in known:
                    raise ValueError(
                        f"delegation {d.id!r} depends on unknown id {dep!r}"
                    )
            # self-dependency is a trivial cycle
            if d.id in d.deps:
                raise ValueError(f"delegation {d.id!r} depends on itself")
        for rid in self.reentry.required_ids:
            if rid not in known:
                raise ValueError(
                    f"reentry.required_id {rid!r} is not a delegation id"
                )
        # Full cycle detection (multi-node a->b->a): fail closed at the
        # schema, before anything is persisted. P13 re-checks the DAG on
        # lowering (dispatch_waves) — this just makes the P14 surface
        # honest on its own.
        graph = {d.id: list(d.deps) for d in self.delegations}
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in graph}
        for root in graph:
            if color[root] != WHITE:
                continue
            stack = [(root, iter(graph[root]))]
            color[root] = GRAY
            while stack:
                node, it = stack[-1]
                advanced = False
                for dep in it:
                    if color[dep] == GRAY:
                        raise ValueError(
                            f"dependency cycle detected at delegation "
                            f"{dep!r}"
                        )
                    if color[dep] == WHITE:
                        color[dep] = GRAY
                        stack.append((dep, iter(graph[dep])))
                        advanced = True
                        break
                if not advanced:
                    color[node] = BLACK
                    stack.pop()
        return self


class WorkerOutcome(BaseModel):
    """Compact, bounded, cloud-facing worker result.

    Raw evidence (transcripts / logs / diffs / Serena / Graphify output)
    is NEVER copied here — it stays on disk and is referenced by
    ``raw_evidence_ref``. Bounds are enforced by validators.
    """

    delegation_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    status: str = Field(min_length=1)  # P13 TaskStatus value
    conclusion: str = Field(default="", max_length=WORKER_CONCLUSION_MAX_CHARS)
    evidence_refs: list[str] = Field(
        default_factory=list, max_length=WORKER_EVIDENCE_REF_MAX
    )
    risks: list[str] = Field(default_factory=list, max_length=WORKER_RISK_MAX)
    unknowns: list[str] = Field(default_factory=list, max_length=WORKER_UNKNOWN_MAX)
    truncated: bool = False
    raw_evidence_ref: str = ""

    @field_validator("status")
    @classmethod
    def _status_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("status must not be blank")
        return value.strip()


class BatchOutcome(BaseModel):
    """Compact, bounded summary of a whole batch for the cloud.

    Hard size guard: ``model_dump_json()`` must be <=
    :data:`BATCH_OUTCOME_MAX_BYTES`. Deterministic compression happens in
    :mod:`outcome` before this is ever sent to the cloud.
    """

    batch_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=400)
    workers: list[WorkerOutcome] = Field(default_factory=list)
    reentry_reason: Literal[
        "ALL_REQUIRED_DELEGATIONS_COMPLETE",
        "BATCH_FAILED_WITH_ESCALATION",
        "REPLAN_REQUIRED",
        "HUMAN_FEEDBACK_CONTINUATION_READY",
    ] = REENTRY_REASONS[0]
    summary: str = Field(default="", max_length=WORKER_CONCLUSION_MAX_CHARS)

    @model_validator(mode="after")
    def _size_guard(self) -> "BatchOutcome":
        size = len(self.model_dump_json().encode("utf-8"))
        if size > BATCH_OUTCOME_MAX_BYTES:
            raise ValueError(
                f"BatchOutcome is {size} bytes, exceeds hard limit "
                f"{BATCH_OUTCOME_MAX_BYTES} — compress before sending"
            )
        return self


class NeedsHuman(BaseModel):
    """Cloud escalates to a human. Parks; no worker dispatch from here."""

    mode: Literal[CloudMode.NEEDS_HUMAN] = CloudMode.NEEDS_HUMAN
    question: str = Field(min_length=1)
    context: str = ""
    options: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value.strip()


class FinalResult(BaseModel):
    """Terminal cloud action: project is COMPLETE. Carries the compact
    final summary (bounded). No further cloud invocation from COMPLETE."""

    mode: Literal[CloudMode.FINAL] = CloudMode.FINAL
    success: bool = True
    summary: str = Field(default="", max_length=WORKER_CONCLUSION_MAX_CHARS)
    artifacts: list[str] = Field(default_factory=list, max_length=32)


# ---------------------------------------------------------------------------
# The discriminated CloudAction envelope — ONE per invocation
# ---------------------------------------------------------------------------
CloudAction = Annotated[
    Union[
        ClarificationRequest,
        BatchPlan,
        NeedsHuman,
        FinalResult,
    ],
    Field(discriminator="mode"),
]


# ---------------------------------------------------------------------------
# Project-level state + contract
# ---------------------------------------------------------------------------


class ProjectContract(BaseModel):
    """Durable project record. Combines the original prompt + the
    clarification answer + durable state + a compact worker capability
    manifest. This is the input the single BATCH turn plans from.
    """

    project_id: str = Field(min_length=1, max_length=64)
    initial_prompt: str = Field(min_length=1)
    clarification_answer: str = Field(default="")
    objective: str = Field(min_length=1, max_length=400)
    created_at: str = ""
    state: CloudProjectState = CloudProjectState.NEW
    generation: int = Field(default=0, ge=0)
    base_sha: str = ""

    @field_validator("initial_prompt", "objective")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _shape_project_id(self) -> "ProjectContract":
        if not _ID_RE.fullmatch(self.project_id):
            raise ValueError(
                f"project_id must match {_ID_RE.pattern}, got {self.project_id!r}"
            )
        return self


__all__ = [
    "CORE_RULES_MAX_BYTES",
    "ROLE_MANIFEST_MAX_BYTES",
    "BATCH_PROMPT_MAX_BYTES",
    "BATCH_OUTCOME_MAX_BYTES",
    "BATCH_OUTCOME_SOFT_BYTES",
    "WORKER_CONCLUSION_MAX_CHARS",
    "WORKER_EVIDENCE_REF_MAX",
    "WORKER_RISK_MAX",
    "WORKER_UNKNOWN_MAX",
    "CLARIFY_MAX_QUESTIONS",
    "MAX_REENTRY_CALLS_PER_GENERATION",
    "REENTRY_REASONS",
    "PARALLELISM_HARD_MAX",
    "PARALLELISM_DEFAULT_TARGET",
    "CloudMode",
    "CloudProjectState",
    "ClarificationRequest",
    "DelegationSpec",
    "ReentryGate",
    "BatchPlan",
    "WorkerOutcome",
    "BatchOutcome",
    "NeedsHuman",
    "FinalResult",
    "CloudAction",
    "ProjectContract",
]
