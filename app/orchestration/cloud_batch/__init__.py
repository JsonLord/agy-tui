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

Classification posture (P14 -> P17): P14 proved the native AGY-CLI
transport is classification B — operational tool schemas cannot be
proven hidden from the model; execution is gated by CLI permissions
(headless auto-deny of unapproved tools, optional --sandbox, MCP server
switches). DEFAULT WEBUI CUTOVER WAS BLOCKED until a true pre-execution
(schema-level) restriction was proven. P17 resolves the isolation: the
PRODUCTION planner transport is now the tool-free google-genai plain
completion (:func:`app.orchestration.cloud_batch.planner.
routed_tool_free_invoke` over ``TOOL_FREE_TIER_PRIORITY``) — a
non-agentic API with no tool parameter at all, so isolation is
structural (classification A). The CLI transport survives ONLY as the
explicit restricted/experimental ``transport="agy_cli"`` opt-in
(classification B, tool schemas visible). Cutover remains BLOCKED until
the tool-free transport is LIVE-PROVEN with configured Gemini/Vertex
credentials (currently absent on this host — BLOCKED-BY-CREDENTIALS,
not blocked-by-upstream). Interim defenses for any restricted use:
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
    TOOL_FREE_TIER_PRIORITY,
    agy_cli_invoke,
    build_batch_prompt,
    build_clarify_prompt,
    build_reentry_prompt,
    genai_tool_free_invoke,
    parse_cloud_action,
    routed_agy_invoke,
    routed_tool_free_invoke,
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
from app.orchestration.cloud_batch.authority import (
    ROUTED_STATUS,
    ROLE_CLASSIFICATION,
    LoweringError,
    MutationClass,
    MutationClassificationError,
    classify_delegation,
    dispatch_batch,
    lower_batch_to_checkpoint,
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
    "TOOL_FREE_TIER_PRIORITY",
    "agy_cli_invoke",
    "routed_agy_invoke",
    "genai_tool_free_invoke",
    "routed_tool_free_invoke",
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
    "MutationClass",
    "ROLE_CLASSIFICATION",
    "classify_delegation",
    "MutationClassificationError",
    "LoweringError",
    "ROUTED_STATUS",
    "lower_batch_to_checkpoint",
    "dispatch_batch",
]
