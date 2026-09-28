"""P8 checkpoint-plan submission (worker A).

The single canonical path from a planner-produced ``CheckpointPlan`` to
durable AGY state. Ten ordered steps:

 1. schema validation (reject on structural violations)
 2. checkpoint ids valid + unique
 3. dependency DAG acyclic and resolvable
 4. every gate explicitly accounted for (applicability)
 5. required G1 carries planned_tests
 6. G0 approval present, decision APPROVE, matching revision
 7. persist canonical ``state_root/plans/<plan_id>/checkpoint_plan.json``
    (+ ``provenance.json``)
 8. runtime_handoff — record ``runtime_owner="agy-local"`` and
    ``next_gate`` (first checkpoint's first REQUIRED gate)
 9. todo_sink — OpenCode Todo PROJECTION ONLY (canonical state never
    changes because of it; sink errors are recorded, never fatal)
10. mark plan SUBMITTED; planner has yielded.

Trust model: planner-claimed gate statuses are DISCARDED — every gate
record is re-seeded PENDING (NOT_APPLICABLE where required=False) by the
deterministic seed below. Idempotent: same plan_id+revision + identical
document = no-op accepted; different document = error.

No network. No host runtime import. Writes stay inside ``state_root``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from app.orchestration.checkpoint_planning.provenance import (
    plan_provenance_record,
)
from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_REQUIRED,
    GATE_KINDS,
    Checkpoint,
    CheckpointPlan,
    G0Approval,
    GateRecord,
    GateStatus,
)
from app.orchestration.checkpoint_planning.validation import (
    PlanValidationError,
    checkpoint_execution_order,
    validate_checkpoint_plan,
)

RUNTIME_OWNER = "agy-local"

# Fields stamped per submission attempt / owned by the runtime handoff;
# excluded from the idempotency document comparison so a re-submit of the
# same planner document is a no-op.
VOLATILE_DOC_FIELDS = ("submitted_at", "todo_projection")


def _stable_document(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if k not in VOLATILE_DOC_FIELDS}


@dataclass
class SubmissionResult:
    accepted: bool
    plan_id: str | None = None
    runtime_owner: str | None = None
    next_gate: str | None = None
    planner_session_complete: bool = False
    errors: list[str] = field(default_factory=list)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Deterministic gate-record seeding (planner claims are discarded)
# ---------------------------------------------------------------------------


def seed_gate_records(checkpoint: Checkpoint) -> dict[str, GateRecord]:
    """Re-seed durable gate state purely from the explicit expectations.

    Status is ALWAYS PENDING regardless of anything the planner wrote in
    ``gate_records``; applicability is REQUIRED/NOT_APPLICABLE from
    ``required``.
    """
    records: dict[str, GateRecord] = {}
    for kind, expectations in checkpoint.gates.iter_gates():
        records[kind] = GateRecord(
            gate=kind,
            status=GateStatus.PENDING,
            applicability=(
                APPLICABILITY_REQUIRED
                if expectations.required
                else APPLICABILITY_NOT_APPLICABLE
            ),
            preseeded=expectations,
        )
    return records


def next_required_gate(plan: CheckpointPlan) -> str | None:
    """``<checkpoint_id>:<gate_kind>`` of the first checkpoint (execution
    order) whose first REQUIRED gate it is."""
    by_id = {cp.checkpoint_id: cp for cp in plan.checkpoints}
    try:
        order = checkpoint_execution_order(plan)
    except PlanValidationError:
        return None
    for cid in order:
        cp = by_id.get(cid)
        if cp is None:  # pragma: no cover - defensive
            continue
        for kind, expectations in cp.gates.iter_gates():
            if expectations.required:
                return f"{cid}:{kind}"
    return None


# ---------------------------------------------------------------------------
# The submission pipeline
# ---------------------------------------------------------------------------


def submit_checkpoint_plan(
    plan: CheckpointPlan,
    *,
    state_root: Path,
    runtime_handoff: Callable[[CheckpointPlan], dict] | None = None,
    todo_sink: Callable[[CheckpointPlan, dict], dict] | None = None,
    g0_approval: G0Approval | None = None,
) -> SubmissionResult:
    """Run the ten ordered steps; see module docstring."""
    errors: list[str] = []

    # -- 1. schema ------------------------------------------------------------
    try:
        candidate = CheckpointPlan.model_validate(plan.model_dump(mode="json"))
    except ValidationError as exc:
        return SubmissionResult(
            accepted=False,
            plan_id=getattr(plan, "plan_id", None),
            errors=[f"schema: {err.get('loc')}: {err.get('msg')}" for err in exc.errors()],
        )

    # Apply an explicit approval argument (submission-owned, not planner-owned).
    if g0_approval is not None:
        candidate = candidate.model_copy(deep=True)
        candidate.g0 = g0_approval

    plan_id = candidate.plan_id

    # -- 2..5. domain validation (ids, DAG, gates, G1 data) -------------------
    verdict = validate_checkpoint_plan(candidate)
    if not verdict.ok:
        errors.extend(verdict.errors)

    # -- 6. G0 approval ---------------------------------------------------------
    g0 = candidate.g0
    if g0 is None:
        errors.append("G0: approval missing (APPROVE required before submission)")
    elif g0.decision != "APPROVE":
        errors.append(f"G0: decision must be APPROVE, got {g0.decision!r}")
    elif g0.revision != candidate.revision:
        errors.append(
            f"G0: approval revision {g0.revision} does not match plan revision "
            f"{candidate.revision}"
        )

    if errors:
        return SubmissionResult(accepted=False, plan_id=plan_id, errors=errors)

    # -- 7. canonical persistence (idempotent) ---------------------------------
    plan_dir = Path(state_root) / "plans" / plan_id
    plan_path = plan_dir / "checkpoint_plan.json"

    # Discard planner-claimed gate state; seed deterministically.
    candidate = candidate.model_copy(deep=True)
    for cp in candidate.checkpoints:
        cp.gate_records = seed_gate_records(cp)
    candidate.status = "SUBMITTED"
    if candidate.revision < 1:  # pragma: no cover - validated above
        errors.append("PLAN:revision: must be >= 1")
        return SubmissionResult(accepted=False, plan_id=plan_id, errors=errors)

    submitted_at = _utc_now()
    next_gate = next_required_gate(candidate)
    doc = candidate.model_dump(mode="json")
    doc["submitted_at"] = submitted_at
    doc["runtime_owner"] = RUNTIME_OWNER
    doc["next_gate"] = next_gate

    if plan_path.exists():
        try:
            existing_doc = json.loads(plan_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_doc = None
        if isinstance(existing_doc, dict) and (
            existing_doc.get("revision") == candidate.revision
        ):
            if _stable_document(existing_doc) == _stable_document(doc):
                # Idempotent no-op: identical plan_id+revision document.
                return SubmissionResult(
                    accepted=True,
                    plan_id=plan_id,
                    runtime_owner=existing_doc.get("runtime_owner", RUNTIME_OWNER),
                    next_gate=existing_doc.get("next_gate"),
                    planner_session_complete=True,
                )
            return SubmissionResult(
                accepted=False,
                plan_id=plan_id,
                errors=[
                    f"{plan_id}: revision {candidate.revision} already submitted "
                    "with a different document (supersede via a new revision)"
                ],
            )

    _atomic_write_json(plan_path, doc)
    _atomic_write_json(
        plan_dir / "provenance.json",
        plan_provenance_record(candidate, plan_path, submitted_at=submitted_at),
    )

    # -- 8. runtime handoff -------------------------------------------------------
    next_gate = next_required_gate(candidate)
    runtime_owner = RUNTIME_OWNER
    handoff_record: dict = {"runtime_owner": RUNTIME_OWNER, "next_gate": next_gate}
    if runtime_handoff is not None:
        try:
            returned = runtime_handoff(candidate)
            if isinstance(returned, dict):
                handoff_record.update(returned)
        except Exception as exc:  # noqa: BLE001 - handoff failure is an error
            errors.append(f"runtime_handoff failed: {exc}")

    runtime_owner = handoff_record.get("runtime_owner", RUNTIME_OWNER)
    next_gate = handoff_record.get("next_gate", next_gate)

    # -- 9. Todo projection (never authoritative) ----------------------------------
    todo_record: dict = {"projected": False}
    if todo_sink is not None and not errors:
        try:
            projected = todo_sink(candidate, handoff_record)
            if isinstance(projected, dict):
                todo_record = projected
        except Exception as exc:  # noqa: BLE001 - projection cannot alter state
            todo_record = {"projected": False, "error": str(exc)}

    # -- 10. SUBMITTED + yielded ------------------------------------------------------
    doc["runtime_owner"] = runtime_owner
    doc["next_gate"] = next_gate
    doc["todo_projection"] = todo_record
    _atomic_write_json(plan_path, doc)

    if errors:
        return SubmissionResult(
            accepted=False, plan_id=plan_id, errors=errors
        )

    return SubmissionResult(
        accepted=True,
        plan_id=plan_id,
        runtime_owner=runtime_owner,
        next_gate=next_gate,
        planner_session_complete=True,
    )
