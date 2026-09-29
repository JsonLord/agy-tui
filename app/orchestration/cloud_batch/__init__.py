"""P14 — Interrupt-Driven Cloud Batch Planner.

Sits ABOVE the accepted P13 delegation policy. The project-level cloud
planner (Gemini inside the AGY CLI) is reduced to four cloud modes:

    CLARIFY | BATCH | NEEDS_HUMAN | FINAL

NEW project -> ONE CLARIFY turn (user-intent questions only) -> user
answer -> ONE BATCH turn producing a validated BatchPlan -> the cloud
model is DORMANT while the deterministic BatchDispatcher runs bounded
workers under the P13 delegation policy -> a deterministic reentry gate
marks REENTRY_READY -> a higher supervisor invokes the cloud at most
ONCE per reentry generation (next BATCH / NEEDS_HUMAN / FINAL).

The cloud model never manages Todo state, never owns wait loops, and is
never invoked by worker completion, progress events, or logs.

Classification B interim posture (see state/autopilot/status.md,
2026-09-29 P14 REASSIGNED): native AGY-CLI operational tool schemas
cannot be proven hidden from the model; execution is gated by CLI
permissions (headless auto-deny of unapproved tools, optional --sandbox,
MCP server switches). DEFAULT WEBUI CUTOVER REMAINS BLOCKED until a true
pre-execution (schema-level) restriction is proven. Interim defenses:
structured CloudAction envelope + fail-closed tool-event detection
(:func:`app.orchestration.cloud_batch.protocol.scan_for_tool_events`).
"""

from app.orchestration.cloud_batch.schemas import (
    BATCH_OUTCOME_MAX_BYTES,
    BATCH_OUTCOME_SOFT_BYTES,
    BATCH_PROMPT_MAX_BYTES,
    CLARIFY_MAX_QUESTIONS,
    CORE_RULES_MAX_BYTES,
    MAX_REENTRY_CALLS_PER_GENERATION,
    REENTRY_REASONS,
    ROLE_MANIFEST_MAX_BYTES,
    WORKER_CONCLUSION_MAX_CHARS,
    WORKER_EVIDENCE_REF_MAX,
    WORKER_RISK_MAX,
    WORKER_UNKNOWN_MAX,
    BatchOutcome,
    BatchPlan,
    ClarificationRequest,
    CloudAction,
    CloudMode,
    CloudProjectState,
    DelegationSpec,
    FinalResult,
    HumanResponse,
    NeedsHuman,
    ProjectContract,
    WorkerOutcome,
)
from app.orchestration.cloud_batch.protocol import (
    CloudProtocol,
    CloudProtocolError,
    CloudProtocolViolation,
    scan_for_tool_events,
)
from app.orchestration.cloud_batch.planner import (
    CloudPlanner,
    CloudPlannerUnavailable,
    CloudTierCapacityError,
    QuotaRouter,
    TIER_PRIORITY,
    agy_cli_invoke,
    build_batch_prompt,
    build_clarify_prompt,
    build_reentry_prompt,
    parse_cloud_action,
    routed_agy_invoke,
)
from app.orchestration.cloud_batch.state import ProjectStore
from app.orchestration.cloud_batch.dispatcher import (
    BatchDispatcher,
    DispatchError,
    OpenCodeWorkerRunner,
    P13_CONCURRENCY_CAP,
    WorkerRunResult,
    WorkerRunner,
    build_delegation_plan,
)
from app.orchestration.cloud_batch.human_bridge import (
    DuplicateHumanResponse,
    HumanBridgeError,
    NotHumanOrigin,
    ParkNotAwaiting,
    park_view,
    record_human_response,
)

__all__ = [
    "BATCH_OUTCOME_MAX_BYTES",
    "BATCH_OUTCOME_SOFT_BYTES",
    "BATCH_PROMPT_MAX_BYTES",
    "CLARIFY_MAX_QUESTIONS",
    "CORE_RULES_MAX_BYTES",
    "MAX_REENTRY_CALLS_PER_GENERATION",
    "REENTRY_REASONS",
    "ROLE_MANIFEST_MAX_BYTES",
    "WORKER_CONCLUSION_MAX_CHARS",
    "WORKER_EVIDENCE_REF_MAX",
    "WORKER_RISK_MAX",
    "WORKER_UNKNOWN_MAX",
    "BatchOutcome",
    "BatchPlan",
    "ClarificationRequest",
    "CloudAction",
    "CloudMode",
    "CloudProjectState",
    "DelegationSpec",
    "FinalResult",
    "HumanResponse",
    "NeedsHuman",
    "ProjectContract",
    "WorkerOutcome",
    "CloudProtocol",
    "CloudProtocolError",
    "CloudProtocolViolation",
    "scan_for_tool_events",
    "CloudPlanner",
    "CloudPlannerUnavailable",
    "CloudTierCapacityError",
    "QuotaRouter",
    "TIER_PRIORITY",
    "agy_cli_invoke",
    "routed_agy_invoke",
    "build_batch_prompt",
    "build_clarify_prompt",
    "build_reentry_prompt",
    "parse_cloud_action",
    "ProjectStore",
    "BatchDispatcher",
    "DispatchError",
    "P13_CONCURRENCY_CAP",
    "WorkerRunResult",
    "OpenCodeWorkerRunner",
    "WorkerRunner",
    "build_delegation_plan",
    "HumanBridgeError",
    "NotHumanOrigin",
    "ParkNotAwaiting",
    "DuplicateHumanResponse",
    "park_view",
    "record_human_response",
]
