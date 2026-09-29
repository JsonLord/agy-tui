"""P13 — continuation admission: a validated P12 continuation becomes a
submitted canonical plan (admission only, never execution).

The P12 loop ends with a validated ``HumanPlannerInputRequest``
continuation persisted under ``<state_root>/plans/<plan_id>/
continuations/<request_id>.json`` with verbatim human provenance.
That continuation is AVAILABLE but NOT executable: a planner-produced
plan does not become canonical state merely because it validated.

This module is the ONLY seam between "continuation available" and
"canonical runtime state":

1. loads the continuation artifact (fail closed if missing/corrupt);
2. verifies provenance (source request present, CONSUMED, pointing at
   this artifact; feedback verbatim; plan/checkpoint ids consistent);
3. re-verifies schema + domain validity NOW (the consumer's earlier
   validation does not expire by trust, and the artifact may be edited);
4. rejects a stale planner G0 (continuation.g0 must be absent);
5. requires a canonical G0 APPROVE (a ``G0Approval`` matching the
   continuation revision — a human decision, never fabricated here);
6. admits by calling the EXISTING ``submit_checkpoint_plan`` (which owns
   persistence, gate-record seeding, provenance, idempotency);
7. records admission provenance
   (``continuations/admissions/<request_id>.json``): stable document
   hash, source request id, revision, next gate, approver, timestamp.

Fail-closed rejections (all raise :class:`ContinuationAdmissionError`
with every detected reason):

* missing continuation / missing source request;
* source request not CONSUMED;
* feedback provenance mismatch (artifact vs request, verbatim);
* stale revision (G0 approval revision != continuation revision);
* tampered/invalid continuation (schema or domain validation fails);
* planner-fabricated G0 on the continuation (g0 must be absent);
* duplicate admission whose continuation content no longer matches the
  recorded hash (conflicting duplicate).

Idempotent repeated admission of the identical continuation returns the
existing admission record (same ``accepted=True``, ``idempotent=True``)
without re-submitting — consistent with submission's own idempotent
document semantics.

Admission is STOP: the next gate is RETURNED but never dispatched, no
G1/G2/G3/G4 executor is invoked, and no model is called anywhere in
this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.orchestration.agy_runstate import safe_name
from app.orchestration.checkpoint_planning.schemas import (
    G0Approval,
    CheckpointPlan,
)
from app.orchestration.checkpoint_planning.submission import (
    RUNTIME_OWNER,
    seed_gate_records,
    submit_checkpoint_plan,
)
from app.orchestration.checkpoint_planning.validation import (
    PlanValidationError,
    validate_checkpoint_plan,
)

#: Request status required before a continuation may be admitted.
REQUEST_STATUS_CONSUMED = "CONSUMED"


class ContinuationAdmissionError(RuntimeError):
    """Fail-closed admission rejection; ``errors`` lists every reason."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass
class AdmissionResult:
    accepted: bool
    idempotent: bool = False
    plan_id: str | None = None
    revision: int | None = None
    next_gate: str | None = None
    runtime_owner: str | None = None
    approver: str | None = None
    admissions_record: Path | None = None
    gate_records: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Paths (must mirror the P12 consumer's layout exactly)
# ---------------------------------------------------------------------------


def continuations_dir(state_root: Path | str, plan_id: str) -> Path:
    return Path(state_root) / "plans" / safe_name(plan_id) / "continuations"


def request_dir(state_root: Path | str, plan_id: str, checkpoint_id: str) -> Path:
    return (
        Path(state_root)
        / "runs"
        / safe_name(plan_id)
        / "human"
        / safe_name(checkpoint_id)
        / "planner_inputs"
    )


def continuation_path(state_root: Path | str, plan_id: str, request_id: str) -> Path:
    return continuations_dir(state_root, plan_id) / f"{safe_name(request_id)}.json"


def admissions_dir(state_root: Path | str, plan_id: str) -> Path:
    return continuations_dir(state_root, plan_id) / "admissions"


def admission_record_path(
    state_root: Path | str, plan_id: str, request_id: str
) -> Path:
    return admissions_dir(state_root, plan_id) / f"{safe_name(request_id)}.json"


# ---------------------------------------------------------------------------
# Deterministic document hashing (admission content identity)
# ---------------------------------------------------------------------------


def _stable_continuation_doc(plan: CheckpointPlan) -> dict:
    """The continuation plan document minus the approval channel.

    ``g0`` is applied at admission time (human-owned); hashing the rest
    gives a content identity for duplicate detection.
    """
    doc = plan.model_dump(mode="json")
    doc.pop("g0", None)
    return doc


def continuation_hash(plan: CheckpointPlan) -> str:
    payload = json.dumps(
        _stable_continuation_doc(plan), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, default=str)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)
    return path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Loading + verification
# ---------------------------------------------------------------------------


def _load_json(path: Path, what: str) -> dict:
    if not path.is_file():
        raise ContinuationAdmissionError([f"{what} is missing: {path}"])
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuationAdmissionError([f"{what} is unreadable: {path}: {exc}"]) from exc
    if not isinstance(doc, dict):
        raise ContinuationAdmissionError([f"{what} is not a JSON object: {path}"])
    return doc


def load_continuation(
    state_root: Path | str, plan_id: str, request_id: str
) -> tuple[CheckpointPlan, dict]:
    """Load ``(continuation plan, human_provenance)`` from the artifact.

    Raises :class:`ContinuationAdmissionError` on missing/corrupt shape
    (fail closed — an admission never guesses at its input).
    """
    doc = _load_json(continuation_path(state_root, plan_id, request_id), "continuation")
    provenance = doc.get("human_provenance")
    if not isinstance(provenance, dict):
        raise ContinuationAdmissionError(
            [f"continuation {request_id} is missing its human_provenance block"]
        )
    if str(provenance.get("source_request_id", "")) != request_id:
        raise ContinuationAdmissionError(
            [
                f"continuation provenance mismatch: source_request_id "
                f"{provenance.get('source_request_id')!r} != artifact id {request_id!r}"
            ]
        )
    try:
        plan = CheckpointPlan.model_validate(doc.get("continuation"))
    except ValidationError as exc:
        raise ContinuationAdmissionError(
            [f"continuation is not schema-valid: {exc.errors()[:3]}"]
        ) from exc
    return plan, provenance


def _verify_provenance(
    state_root: Path,
    plan: CheckpointPlan,
    request_id: str,
    provenance: dict,
) -> dict:
    """The source request must exist, be CONSUMED, point at this artifact,
    and carry the verbatim feedback. Returns the request doc."""
    plan_id = plan.plan_id
    checkpoint_id = str(provenance.get("checkpoint_id") or "")
    if not checkpoint_id:
        raise ContinuationAdmissionError(["continuation provenance lacks checkpoint_id"])

    errors: list[str] = []
    request_path = request_dir(state_root, plan_id, checkpoint_id) / f"{request_id}.json"
    request_doc = _load_json(request_path, f"source request {request_id}")

    if request_doc.get("status") != REQUEST_STATUS_CONSUMED:
        errors.append(
            f"source request {request_id} is {request_doc.get('status')!r}, "
            f"required {REQUEST_STATUS_CONSUMED}"
        )
    if str(request_doc.get("plan_id", "")) != plan_id:
        errors.append(
            f"source request plan_id {request_doc.get('plan_id')!r} != {plan_id!r}"
        )
    if str(request_doc.get("checkpoint_id", "")) != checkpoint_id:
        errors.append(
            f"source request checkpoint_id {request_doc.get('checkpoint_id')!r} "
            f"!= provenance {checkpoint_id!r}"
        )
    if str(request_doc.get("continuation_plan_id", "")) and (
        str(request_doc.get("continuation_plan_id")) != plan.plan_id
    ):
        errors.append(
            "source request continuation_plan_id does not match the "
            f"continuation plan id {plan.plan_id!r}"
        )
    if str(request_doc.get("continuation_revision", "")) and (
        int(request_doc.get("continuation_revision") or 0) != plan.revision
    ):
        errors.append(
            f"source request continuation_revision does not match the "
            f"continuation revision {plan.revision}"
        )
    feedback = str(provenance.get("feedback", ""))
    if str(request_doc.get("feedback", "")) != feedback:
        errors.append(
            "feedback provenance mismatch: artifact feedback is not the "
            "verbatim feedback recorded on the source request (tampered?)"
        )
    if errors:
        raise ContinuationAdmissionError(errors)
    return request_doc


def _revalidate(plan: CheckpointPlan) -> None:
    """Schema (already enforced at load) + domain validation, NOW."""
    try:
        verdict = validate_checkpoint_plan(plan)
    except PlanValidationError as exc:
        raise ContinuationAdmissionError([f"domain validation error: {exc}"]) from exc
    if not verdict.ok:
        raise ContinuationAdmissionError(
            ["continuation failed domain validation: " + "; ".join(verdict.errors)]
        )


# ---------------------------------------------------------------------------
# The admission
# ---------------------------------------------------------------------------


def admit_continuation(
    state_root: Path | str,
    plan_id: str,
    request_id: str,
    *,
    g0_approval: G0Approval | None,
) -> AdmissionResult:
    """Admit one validated continuation through the canonical G0 path.

    ``g0_approval`` is the human decision (a ``G0Approval`` with
    ``decision == "APPROVE"`` and ``revision == continuation.revision``).
    The service never fabricates it. Admission calls the existing
    ``submit_checkpoint_plan`` — no duplicated persistence, seeding, or
    provenance code.
    """
    root = Path(state_root)

    plan, provenance = load_continuation(root, plan_id, request_id)
    _verify_provenance(root, plan, request_id, provenance)
    _revalidate(plan)

    # A planner-fabricated G0 on the continuation is never honored.
    if plan.g0 is not None:
        raise ContinuationAdmissionError(
            [
                "continuation carries a g0 block; approval is human-owned and "
                "must be supplied at admission, not embedded in the plan"
            ]
        )

    # The canonical G0 approval (human-owned; never fabricated here).
    # Each condition fails closed with exactly one reason.
    if g0_approval is None:
        raise ContinuationAdmissionError(
            ["G0: approval missing (APPROVE required before admission)"]
        )
    if g0_approval.decision != "APPROVE":
        raise ContinuationAdmissionError(
            [f"G0: decision must be APPROVE, got {g0_approval.decision!r}"]
        )
    if g0_approval.revision != plan.revision:
        raise ContinuationAdmissionError(
            [
                f"G0: approval revision {g0_approval.revision} does not match the "
                f"continuation revision {plan.revision} (stale approval)"
            ]
        )

    # -- duplicate detection (conflicting duplicate => fail closed) ------------
    record_path = admission_record_path(root, plan_id, request_id)
    content_hash = continuation_hash(plan)
    if record_path.is_file():
        prior = _load_json(record_path, "prior admission record")
        if prior.get("continuation_hash") == content_hash:
            # Idempotent no-op: byte/semantically identical accepted input.
            existing_plan_doc = _load_json(
                root / "plans" / plan_id / "checkpoint_plan.json",
                "canonical plan document",
            )
            return AdmissionResult(
                accepted=True,
                idempotent=True,
                plan_id=plan_id,
                revision=plan.revision,
                next_gate=existing_plan_doc.get("next_gate"),
                runtime_owner=existing_plan_doc.get("runtime_owner", RUNTIME_OWNER),
                approver=prior.get("approver"),
                admissions_record=record_path,
                gate_records=_seeded_view(plan),
            )
        raise ContinuationAdmissionError(
            [
                f"conflicting duplicate admission for {request_id}: the "
                "continuation content does not match the already-admitted "
                "document (hash mismatch) — supersede via a new request"
            ]
        )

    # -- canonical submission (existing pipeline; owns persist + seed) --------
    submission = submit_checkpoint_plan(
        plan, state_root=root, g0_approval=g0_approval
    )
    if not submission.accepted:
        raise ContinuationAdmissionError(submission.errors)

    # -- admission provenance ---------------------------------------------------
    record_path = _atomic_write_json(
        record_path,
        {
            "request_id": request_id,
            "status": "ADMITTED",
            "plan_id": plan_id,
            "revision": plan.revision,
            "continuation_hash": content_hash,
            "source_request_id": request_id,
            "source_feedback": str(provenance.get("feedback", "")),
            "actor": str(provenance.get("actor", "")),
            "approver": g0_approval.approver,
            "approved_at": g0_approval.approved_at,
            "admitted_at": _utc_now(),
            "next_gate": submission.next_gate,
            "runtime_owner": submission.runtime_owner,
        },
    )

    return AdmissionResult(
        accepted=True,
        idempotent=False,
        plan_id=submission.plan_id,
        revision=plan.revision,
        next_gate=submission.next_gate,
        runtime_owner=submission.runtime_owner,
        approver=g0_approval.approver,
        admissions_record=record_path,
        gate_records=_seeded_view(plan),
    )


def _seeded_view(plan: CheckpointPlan) -> dict[str, dict]:
    """Deterministic gate records as submission will seed them (read-only
    view — the authoritative records are on the canonical plan document)."""
    view: dict[str, dict] = {}
    for cp in plan.checkpoints:
        for kind, record in seed_gate_records(cp).items():
            view[f"{cp.checkpoint_id}:{kind}"] = {
                "status": record.status.value,
                "applicability": record.applicability,
            }
    return view


__all__ = [
    "AdmissionResult",
    "ContinuationAdmissionError",
    "REQUEST_STATUS_CONSUMED",
    "admission_record_path",
    "admissions_dir",
    "admit_continuation",
    "continuation_hash",
    "continuation_path",
    "continuations_dir",
    "load_continuation",
    "request_dir",
]
