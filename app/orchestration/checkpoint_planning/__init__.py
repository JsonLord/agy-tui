"""AGY P8 — cloud-driven checkpoint planning package.

Public surface re-exported here: planning context/analysis/todo
projection + MCP tools (surface) and the domain core (schemas,
validation, planner, submission, provenance).
"""

from __future__ import annotations

from app.orchestration.checkpoint_planning.context import (
    agy_design_context,
    agy_learnings_read,
    agy_project_inspect,
)
from app.orchestration.checkpoint_planning.codebase_analysis import (
    ANALYST_AGENT,
    AnalystUnavailable,
    RepoMutationDetected,
    build_analyst_prompt,
    codebase_analyze,
    codebase_analyze_many,
    default_analyst,
    evidence_from_payload,
)
from app.orchestration.checkpoint_planning.todo_projection import (
    render_checkpoint_todos,
    sync_checkpoint_todos,
)
from app.orchestration.checkpoint_planning import mcp_tools
from app.orchestration.checkpoint_planning.schemas import (  # noqa: F401
    GATE_KINDS,
    AnalysisEvidence,
    AnalysisRequest,
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    CheckpointStatus,
    Complexity,
    G0Approval,
    GateApplicability,
    GateExpectations,
    GateRecord,
    GateStatus,
    PlannerProvenance,
    PlannerUnavailable,
)
from app.orchestration.checkpoint_planning.validation import (  # noqa: F401
    PlanValidationError,
    ValidationResult,
    checkpoint_execution_order,
    topological_order,
    validate_checkpoint_plan,
)
from app.orchestration.checkpoint_planning.planner import (  # noqa: F401
    MAX_REVIEW_FIX_CYCLES,
    GeminiCloudPlanner,
    MiMoProFallbackPlanner,
    CheckpointPlanner,
    classify_complexity,
    gate_templates,
    preview_checkpoint_plan,
    route_planner,
)
from app.orchestration.checkpoint_planning.submission import (  # noqa: F401
    SubmissionResult,
    submit_checkpoint_plan,
)
from app.orchestration.checkpoint_planning.provenance import (  # noqa: F401
    plan_provenance_record,
)
from app.orchestration.checkpoint_planning.gate_engine import (  # noqa: F401
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.gate_evidence import (  # noqa: F401
    GateExecutionRecord,
    evidence_is_sufficient,
    g1_evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.g1_executor import (  # noqa: F401
    CumulativeRegression,
    G1Executor,
    GateConfig,
    GateRuntimeError,
    GatekeeperDecision,
    GatekeeperUnavailable,
    GreenNotAchieved,
    MiMoProGatekeeper,
    RedNotDemonstrated,
    TestAuthorEscapedTestPaths,
    TestFileTampered,
)

__all__ = [
    # B surface
    "agy_project_inspect",
    "agy_design_context",
    "agy_learnings_read",
    "codebase_analyze",
    "codebase_analyze_many",
    "default_analyst",
    "build_analyst_prompt",
    "evidence_from_payload",
    "ANALYST_AGENT",
    "AnalystUnavailable",
    "RepoMutationDetected",
    "render_checkpoint_todos",
    "sync_checkpoint_todos",
    "mcp_tools",
    # A domain core
    "GATE_KINDS",
    "AnalysisEvidence",
    "AnalysisRequest",
    "Checkpoint",
    "CheckpointGates",
    "CheckpointPlan",
    "CheckpointStatus",
    "Complexity",
    "G0Approval",
    "GateApplicability",
    "GateExpectations",
    "GateRecord",
    "GateStatus",
    "PlannerProvenance",
    "PlannerUnavailable",
    "PlanValidationError",
    "ValidationResult",
    "checkpoint_execution_order",
    "topological_order",
    "validate_checkpoint_plan",
    "MAX_REVIEW_FIX_CYCLES",
    "GeminiCloudPlanner",
    "MiMoProFallbackPlanner",
    "CheckpointPlanner",
    "classify_complexity",
    "gate_templates",
    "preview_checkpoint_plan",
    "route_planner",
    "SubmissionResult",
    "submit_checkpoint_plan",
    "plan_provenance_record",
    # P9 gate runtime
    "GateEngine",
    "GateTransitionError",
    "GateExecutionRecord",
    "g1_evidence_is_sufficient",
    "evidence_is_sufficient",
    "G1Executor",
    "GateConfig",
    "GatekeeperDecision",
    "GateRuntimeError",
    "TestAuthorEscapedTestPaths",
    "TestFileTampered",
    "RedNotDemonstrated",
    "GreenNotAchieved",
    "CumulativeRegression",
    "GatekeeperUnavailable",
    "MiMoProGatekeeper",
]
