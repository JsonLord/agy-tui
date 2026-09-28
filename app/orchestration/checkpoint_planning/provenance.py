"""P8 plan provenance (worker A).

Answers, from the plan document alone: who planned (provider / model /
role / fallback), when (created_at, G0 approval time), which analyses
fed the plan (including analyst sessions), the gate template version,
the plan revision, and when control handed to AGY-local (submitted_at,
recorded into the persisted document by submission).

No raw prompts are ever persisted — provenance references, never
reproductions.
"""

from __future__ import annotations

from pathlib import Path

from app.orchestration.checkpoint_planning.schemas import CheckpointPlan

PROVENANCE_SCHEMA = "agy-checkpoint-plan-provenance/1"


def plan_provenance_record(
    plan: CheckpointPlan,
    plan_path: Path | str,
    *,
    submitted_at: str | None = None,
) -> dict:
    """Build the durable provenance record for one submitted plan.

    ``submitted_at`` is the moment control handed to AGY-local (the same
    timestamp submission stamps into the persisted plan document).
    """
    planner = plan.planner
    g0 = plan.g0

    return {
        "schema": PROVENANCE_SCHEMA,
        "plan_id": plan.plan_id,
        "feature_id": plan.feature_id,
        "revision": plan.revision,
        "plan_path": str(plan_path),
        "who": {
            "role": planner.role,
            "provider": planner.provider,
            "model": planner.model,
            "fallback_used": planner.fallback_used,
            "planner_session": planner.planner_session,
            "gate_template_version": planner.gate_template_version,
        },
        "when": {
            "created_at": plan.created_at,
            "g0_approved_at": g0.approved_at if g0 else None,
            "g0_approver": g0.approver if g0 else None,
            "g0_decision": g0.decision if g0 else None,
            "submitted_at": submitted_at,
        },
        "analyses": [
            {
                "analysis_id": ref.analysis_id,
                "objective": ref.objective,
                "analyst_session": ref.analyst_session,
                "evidence_ref": ref.evidence_ref,
                "requested_at": ref.requested_at,
            }
            for ref in plan.analyses
        ],
        "context_refs": {
            "design_context_refs": list(plan.design_context_refs),
            "learning_refs": list(plan.learning_refs),
        },
        "note": (
            "Provenance only: raw planner prompts are never persisted. "
            "Execution ownership belongs to AGY-local after submission."
        ),
    }
