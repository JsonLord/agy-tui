"""P8 checkpoint-planning domain schemas (worker A).

Pure pydantic v2 models for gate-preseeded checkpoint plans. No host
runtime (``google.antigravity``) import here: everything in this module
is stdlib + pydantic so the domain core is importable anywhere.

Design notes mandated by the milestone contract:

* The planner submits a COMPLETE gate-preseeded plan and then YIELDS to
  AGY-local; nothing in these schemas drives execution.
* Provider/model are provenance STRINGS (``gemini`` / ``mimo`` / model
  ids), never a hard-coded model-id enum.
* ``GateExpectations`` (what the plan promises) is deliberately separate
  from ``GateRecord`` (durable runtime state seeded at submission).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Iterator, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GATE_KINDS = ("G1_behavior", "G2_ui", "G3_adversarial", "G4_human")

CHECKPOINT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

PLAN_STATUSES = (
    "DRAFT",
    "VALIDATED",
    "APPROVED",
    "SUBMITTED",
    "EXECUTING",
    "COMPLETED",
    "REJECTED",
    "REPLANNED",
)

APPLICABILITY_REQUIRED = "REQUIRED"
APPLICABILITY_NOT_APPLICABLE = "NOT_APPLICABLE"
APPLICABILITY_VALUES = (APPLICABILITY_REQUIRED, APPLICABILITY_NOT_APPLICABLE)

G0_DECISIONS = ("APPROVE", "REQUEST_REVISION", "CANCEL")


class PlannerUnavailable(Exception):
    """The cloud planner (or fallback) could not produce a plan.

    Raised on ANY adapter failure: CLI unavailable/timeout/nonzero exit,
    provider error, or unparseable/non-schema-conformant output. The
    planner never fabricates a partial plan.
    """


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GateStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PASSED = "PASSED"
    REPAIR = "REPAIR"
    REJECTED = "REJECTED"


class CheckpointStatus(str, Enum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    ACCEPTED = "ACCEPTED"
    REPAIR = "REPAIR"
    BLOCKED = "BLOCKED"


class Complexity(str, Enum):
    TRIVIAL = "TRIVIAL"
    SMALL = "SMALL"
    FEATURE = "FEATURE"
    LARGE_FEATURE = "LARGE_FEATURE"


# ---------------------------------------------------------------------------
# Gate expectation / record models
# ---------------------------------------------------------------------------


class GateApplicability(BaseModel):
    """Explicit REQUIRED / NOT_APPLICABLE statement for one gate kind.

    ``required=False`` is a silent omission guard: a reason is mandatory.
    """

    required: bool
    reason: str | None = None

    @model_validator(mode="after")
    def _reason_when_not_required(self):
        if not self.required and not (self.reason or "").strip():
            raise ValueError(
                "GateApplicability.reason is mandatory when required=False"
            )
        return self


class GateExpectations(BaseModel):
    """What the plan promises each gate will enforce (pre-seeded)."""

    required: bool
    reason: str | None = None
    planned_tests: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)
    test_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reason_when_not_required(self):
        if not self.required and not (self.reason or "").strip():
            raise ValueError(
                "GateExpectations.reason is mandatory when required=False"
            )
        return self

    def applicability(self) -> GateApplicability:
        return GateApplicability(required=self.required, reason=self.reason)


class CheckpointGates(BaseModel):
    """All four gate kinds present, always explicit."""

    G1_behavior: GateExpectations
    G2_ui: GateExpectations
    G3_adversarial: GateExpectations
    G4_human: GateExpectations

    def iter_gates(self) -> Iterator[tuple[str, GateExpectations]]:
        for kind in GATE_KINDS:
            yield kind, getattr(self, kind)


class GateRecord(BaseModel):
    """Durable per-gate runtime state, seeded from ``GateExpectations``.

    Separate from expectations on purpose: planner claims never write
    status here (submission resets everything to PENDING/NOT_APPLICABLE).
    """

    gate: str
    status: GateStatus = GateStatus.PENDING
    applicability: str = APPLICABILITY_REQUIRED
    preseeded: GateExpectations
    evidence_refs: list[str] = Field(default_factory=list)
    closed_at: str | None = None
    closed_by: str | None = None
    repair_lineage: list[str] = Field(default_factory=list)
    red_exception: str | None = None

    @field_validator("applicability")
    @classmethod
    def _applicability_enum(cls, value: str) -> str:
        if value not in APPLICABILITY_VALUES:
            raise ValueError(
                f"applicability must be one of {APPLICABILITY_VALUES}, got {value!r}"
            )
        return value


# ---------------------------------------------------------------------------
# Checkpoints and plans
# ---------------------------------------------------------------------------


class Checkpoint(BaseModel):
    checkpoint_id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    implementation_context: str = ""
    relevant_files: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)
    gates: CheckpointGates
    status: CheckpointStatus = CheckpointStatus.PLANNED
    gate_records: dict[str, GateRecord] = Field(default_factory=dict)
    repair_lineage: list[str] = Field(default_factory=list)

    @field_validator("checkpoint_id")
    @classmethod
    def _checkpoint_id_format(cls, value: str) -> str:
        if not CHECKPOINT_ID_RE.match(value):
            raise ValueError(
                f"checkpoint_id must match {CHECKPOINT_ID_RE.pattern}, got {value!r}"
            )
        return value


class PlannerProvenance(BaseModel):
    role: str = "cloud"
    provider: str
    model: str | None = None
    fallback_used: bool = False
    planner_session: str | None = None
    gate_template_version: str = "1"


class G0Approval(BaseModel):
    decision: str
    approved_at: str | None = None
    approver: str | None = None
    revision: int


class AnalysisRef(BaseModel):
    analysis_id: str
    objective: str
    questions: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)
    analyst_session: str | None = None
    requested_at: str = ""
    evidence_ref: str = ""


class AnalysisRequest(BaseModel):
    objective: str
    questions: list[str] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)


class AnalysisEvidence(BaseModel):
    summary: str
    relevant_symbols: list[str] = Field(default_factory=list)
    dependency_paths: list[str] = Field(default_factory=list)
    existing_tests: list[str] = Field(default_factory=list)
    likely_files: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    source: str = "agy-planning-analyst"


class CheckpointPlan(BaseModel):
    plan_id: str
    feature_id: str
    objective: str
    planner: PlannerProvenance
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    status: str = "DRAFT"
    created_at: str = ""
    revision: int = 1
    design_context_refs: list[str] = Field(default_factory=list)
    learning_refs: list[str] = Field(default_factory=list)
    g0: G0Approval | None = None
    analyses: list[AnalysisRef] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def _status_enum(cls, value: str) -> str:
        if value not in PLAN_STATUSES:
            raise ValueError(f"status must be one of {PLAN_STATUSES}, got {value!r}")
        return value
