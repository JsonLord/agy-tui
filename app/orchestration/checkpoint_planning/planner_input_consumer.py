"""P12 — planner-input consumer: human feedback -> validated continuation.

Closes the loop P11 opened: ``G4Executor.apply_human_decision(NEEDS_CHANGES)``
parked the gate in REPAIR and persisted a ``HumanPlannerInputRequest``
(status ``PENDING``, verbatim feedback). This module is the ONLY
consumer of those requests:

* ``consume_pending(plan_id, checkpoint_id)`` claims the first PENDING
  request atomically (PENDING -> PROCESSING via ``_atomic_write_json``,
  preserving every original field), loads the CANONICAL plan document
  (fail closed — never fabricates plan state), asks the injected
  ``CheckpointPlanner`` (P8 ``revise_plan`` surface — no second planner
  API) for one tiny continuation, and validates the result through the
  EXISTING pipeline only: ``CheckpointPlan.model_validate`` +
  ``validate_checkpoint_plan`` (the same functions submission uses).
  ``submit_checkpoint_plan`` is deliberately NOT called — validation
  only; P12 never auto-executes a continuation.
* Success: request -> CONSUMED (feedback kept verbatim), continuation
  persisted under ``<state_root>/plans/<plan_id>/continuations/
  <request_id>.json`` with human provenance (source_request_id,
  verbatim feedback, actor). "Resume" simply means that validated
  continuation exists (see :func:`pending_continuations`).
* Planner error OR validation failure: request -> FAILED with the
  error detail — the verbatim human feedback is NEVER lost.

Safety rules encoded here:

* One request is consumed at most once (PROCESSING/CONSUMED/FAILED
  files are skipped; a stale PROCESSING claim older than the injectable
  TTL may be reclaimed — idempotent retry, never double-consumption).
* Human text is QUOTED DATA inside the planner evidence, never an
  instruction channel; no worker dispatch seam exists in this module.
* Synchronous + deterministic (injected clock/planner): NO sleeps, NO
  polling loop anywhere (P7 philosophy — park is deterministic, resume
  is an event a human/operator triggers, never a wait).
* No model/network import at module top level (the planner is injected;
  the P8 surface is imported under TYPE_CHECKING only).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.checkpoint_planning.g4_executor import G4_GATE_ID
from app.orchestration.checkpoint_planning.learnings import read_learnings
from app.orchestration.checkpoint_planning.schemas import (
    GATE_KINDS,
    Checkpoint,
    CheckpointPlan,
)
from app.orchestration.checkpoint_planning.validation import (
    validate_checkpoint_plan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; planner is injected
    from app.orchestration.checkpoint_planning.planner import CheckpointPlanner

#: Claim statuses on the request file (PENDING comes from P11).
STATUS_PENDING = "PENDING"
STATUS_PROCESSING = "PROCESSING"
STATUS_CONSUMED = "CONSUMED"
STATUS_FAILED = "FAILED"

#: A PROCESSING claim older than this may be reclaimed (idempotent retry).
DEFAULT_CLAIM_TTL_SECONDS = 3600.0


class PlannerInputConsumerError(RuntimeError):
    """Base typed error for the consumer's fail-closed conditions."""


class CanonicalPlanMissingError(PlannerInputConsumerError):
    """The canonical plan document is missing/malformed — the consumer
    refuses to fabricate plan state and fails closed."""


def _utc(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Prompt seam
# ---------------------------------------------------------------------------


def default_planner_input_prompt(request: dict, plan: CheckpointPlan) -> tuple[str, dict]:
    """Build ``(reason, evidence)`` for ``planner.revise_plan``.

    Compact, deterministic context. The human feedback travels inside
    ``evidence`` as QUOTED DATA — the ``reason`` (the instruction
    channel) describes the task without ever embedding human text.
    """
    checkpoint = next(
        (cp for cp in plan.checkpoints
         if cp.checkpoint_id == request.get("checkpoint_id")),
        None,
    )
    gate_statuses: dict[str, str] = {}
    if checkpoint is not None and checkpoint.gate_records:
        for gate_id in GATE_KINDS:
            record = checkpoint.gate_records.get(gate_id)
            if record is not None:
                gate_statuses[gate_id] = record.status.value

    reason = (
        "A human rejected the current state of checkpoint "
        f"{request.get('checkpoint_id')!r} at the G4 human gate "
        f"(request {request.get('request_id')!r}). Produce ONE tiny "
        "continuation/amendment for this plan that addresses the "
        "quoted human feedback in the evidence. Return the full "
        "revised plan as exactly one valid CheckpointPlan JSON object "
        "(schema unchanged) with 'revision' incremented by 1. Do not "
        "execute anything; the continuation only becomes eligible for "
        "the normal submission/gates flow later."
    )
    evidence: dict[str, Any] = {
        "source": "g4_human_planner_input",
        "original_plan": {
            "plan_id": plan.plan_id,
            "revision": plan.revision,
            "objective": plan.objective,
        },
        "checkpoint": {
            "checkpoint_id": request.get("checkpoint_id"),
            "title": checkpoint.title if checkpoint else "",
            "objective": checkpoint.objective if checkpoint else "",
            "relevant_files": list(checkpoint.relevant_files) if checkpoint else [],
        },
        "gate_statuses": gate_statuses,
        "human_feedback_verbatim": str(request.get("feedback", "")),
    }
    learnings = _learnings_context(plan)
    if learnings:
        evidence["learnings"] = learnings
    return reason, evidence


def _learnings_context(plan: CheckpointPlan) -> list[dict]:
    """Curated learnings carried into the plan, if any refs exist."""
    return [
        {"ref": ref, "topics": read_learnings(ref)[:10]}
        for ref in plan.learning_refs
        if Path(ref).is_file()
    ]


# ---------------------------------------------------------------------------
# Paths (must mirror G4Executor._human_dir / submission's plans layout)
# ---------------------------------------------------------------------------


def requests_dir(state_root: Path | str, plan_id: str, checkpoint_id: str) -> Path:
    return (
        Path(state_root)
        / "runs"
        / safe_name(plan_id)
        / "human"
        / safe_name(checkpoint_id)
        / "planner_inputs"
    )


def continuations_dir(state_root: Path | str, plan_id: str) -> Path:
    return Path(state_root) / "plans" / plan_id / "continuations"


# ---------------------------------------------------------------------------
# The consumer
# ---------------------------------------------------------------------------


class PlannerInputConsumer:
    """Claims PENDING planner-input requests and turns them into
    validated continuations through the injected planner.

    ``planner`` must satisfy the P8 :class:`CheckpointPlanner` surface
    (tests inject a fake; the live MiMoProFallbackPlanner is wired only
    in the smoke run). ``clock`` is injectable for deterministic
    claim-TTL behaviour; nothing here ever sleeps or polls.
    """

    def __init__(
        self,
        state_root: Path | str,
        *,
        planner: "CheckpointPlanner",
        planner_prompt_builder: Callable[[dict, CheckpointPlan], tuple[str, dict]]
        | None = None,
        clock: Callable[[], float] = time.time,
        claim_ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
    ) -> None:
        self.state_root = Path(state_root)
        self._planner = planner
        self._build_prompt = planner_prompt_builder or default_planner_input_prompt
        self._clock = clock
        self._ttl = float(claim_ttl_seconds)

    # -- request file plumbing -------------------------------------------------

    def _request_files(self, plan_id: str, checkpoint_id: str) -> list[Path]:
        directory = requests_dir(self.state_root, plan_id, checkpoint_id)
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def _load_json(self, path: Path) -> dict:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CanonicalPlanMissingError(
                f"planner-input request is unreadable: {path}: {exc}"
            ) from exc
        if not isinstance(doc, dict):
            raise CanonicalPlanMissingError(
                f"planner-input request is not a JSON object: {path}"
            )
        return doc

    def _claimable(self, doc: dict) -> bool:
        """PENDING is claimable; a PROCESSING claim older than the TTL may
        be reclaimed; CONSUMED/FAILED are terminal for consumption."""
        status = doc.get("status")
        if status == STATUS_PENDING:
            return True
        if status == STATUS_PROCESSING:
            claimed = str(doc.get("claimed_at") or "")
            try:
                age = self._clock() - datetime.fromisoformat(claimed).timestamp()
            except ValueError:
                return True  # unparseable claim stamp -> treat as stale
            return age > self._ttl
        return False

    def _claim(self, path: Path, doc: dict) -> dict:
        """Atomically rewrite the request with status PROCESSING, keeping
        ALL original fields and stamping claimed_at."""
        claimed = dict(doc)
        claimed["status"] = STATUS_PROCESSING
        claimed["claimed_at"] = _utc(self._clock)
        _atomic_write_json(path, claimed)
        return claimed

    def _finalize(self, path: Path, doc: dict, **fields: Any) -> dict:
        final = dict(doc)
        final.update(fields)
        _atomic_write_json(path, final)
        return final

    # -- canonical plan ----------------------------------------------------------

    def _load_canonical_plan(self, plan_id: str) -> CheckpointPlan:
        path = self.state_root / "plans" / plan_id / "checkpoint_plan.json"
        if not path.is_file():
            raise CanonicalPlanMissingError(
                f"no canonical plan document at {path} — the consumer never "
                "fabricates plan state"
            )
        doc = self._load_json(path)
        try:
            return CheckpointPlan.model_validate(doc)
        except ValidationError as exc:
            raise CanonicalPlanMissingError(
                f"canonical plan document is malformed: {path}: {exc}"
            ) from exc

    # -- the one public operation --------------------------------------------------

    def consume_pending(self, plan_id: str, checkpoint_id: str) -> dict:
        """Consume the first claimable planner-input request (idempotent).

        Returns ``{"consumed": None, ...}`` when nothing is pending,
        otherwise a result dict with ``status`` (CONSUMED/FAILED) and a
        human-readable ``reason``. Never dispatches a worker, never
        submits anything, never sleeps.
        """
        pending: tuple[Path, dict] | None = None
        for path in self._request_files(plan_id, checkpoint_id):
            doc = self._load_json(path)
            if self._claimable(doc):
                pending = (path, doc)
                break
        if pending is None:
            return {
                "consumed": None,
                "status": "IDLE",
                "reason": "no pending planner-input request",
            }
        path, doc = pending

        # Fail closed on canonical state BEFORE claiming (no orphan claims).
        plan = self._load_canonical_plan(plan_id)

        request = self._claim(path, doc)
        return self._process(path, request, plan, plan_id, checkpoint_id)

    # -- internals ------------------------------------------------------------------

    def _process(
        self,
        path: Path,
        request: dict,
        plan: CheckpointPlan,
        plan_id: str,
        checkpoint_id: str,
    ) -> dict:
        base = {
            "consumed": request.get("request_id"),
            "plan_id": plan_id,
            "checkpoint_id": checkpoint_id,
        }
        try:
            reason, evidence = self._build_prompt(request, plan)
            output = self._planner.revise_plan(plan=plan, evidence=evidence, reason=reason)
            continuation = self._validate_continuation(output)
        except Exception as exc:  # noqa: BLE001 - ANY failure => FAILED, feedback kept
            final = self._finalize(
                path,
                request,
                status=STATUS_FAILED,
                failed_at=_utc(self._clock),
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                **base,
                "status": STATUS_FAILED,
                "reason": str(exc),
                "request": final,
            }

        actor = self._human_actor(plan, checkpoint_id)
        continuation_path = self._persist_continuation(
            request, continuation, plan_id, checkpoint_id, actor
        )
        final = self._finalize(
            path,
            request,
            status=STATUS_CONSUMED,
            consumed_at=_utc(self._clock),
            continuation_plan_id=continuation.plan_id,
            continuation_revision=continuation.revision,
        )
        return {
            **base,
            "status": STATUS_CONSUMED,
            "reason": "continuation validated through the existing plan pipeline",
            "continuation_plan_id": continuation.plan_id,
            "continuation_revision": continuation.revision,
            "continuation_path": str(continuation_path),
            "request": final,
        }

    @staticmethod
    def _validate_continuation(output: Any) -> CheckpointPlan:
        """The ONLY validation seam: schema re-validate + the existing
        domain validator (exactly what submission runs before persisting).
        Validation-only — submit_checkpoint_plan is never called here."""
        if isinstance(output, CheckpointPlan):
            payload = output.model_dump(mode="json")
        else:
            payload = output
        plan = CheckpointPlan.model_validate(payload)
        verdict = validate_checkpoint_plan(plan)
        if not verdict.ok:
            raise ValueError(
                "planner continuation failed plan validation: "
                + "; ".join(verdict.errors)
            )
        return plan

    @staticmethod
    def _human_actor(plan: CheckpointPlan, checkpoint_id: str) -> str:
        """Provenance of the human whose feedback produced the request:
        the G4 gate record's ``closed_by`` on the canonical plan."""
        for cp in plan.checkpoints:
            if cp.checkpoint_id != checkpoint_id:
                continue
            record = cp.gate_records.get(G4_GATE_ID)
            if record is not None and record.closed_by:
                return str(record.closed_by)
        return "human:unknown"

    def _persist_continuation(
        self,
        request: dict,
        continuation: CheckpointPlan,
        plan_id: str,
        checkpoint_id: str,
        actor: str,
    ) -> Path:
        request_id = str(request.get("request_id") or "unknown")
        doc = {
            "continuation": continuation.model_dump(mode="json"),
            "human_provenance": {
                "source_request_id": request_id,
                "plan_id": plan_id,
                "checkpoint_id": checkpoint_id,
                "gate_id": request.get("gate_id", G4_GATE_ID),
                "feedback": request.get("feedback", ""),  # VERBATIM, never rewritten
                "actor": actor,
                "created_at": request.get("created_at", ""),
                "consumed_at": _utc(self._clock),
            },
        }
        return _atomic_write_json(
            continuations_dir(self.state_root, plan_id) / f"{safe_name(request_id)}.json",
            doc,
        )


# ---------------------------------------------------------------------------
# Resume surface (read-only; the orchestrator picks these up via the normal
# submission/gates flow — P12 never auto-submits or auto-executes)
# ---------------------------------------------------------------------------


def pending_continuations(state_root: Path | str, plan_id: str) -> list[dict]:
    """Validated continuations awaiting pickup, oldest request first.

    A continuation "exists to resume" only once the consumer marked its
    request CONSUMED and persisted the validated doc — so this is simply
    the continuation directory, read. Unreadable entries are skipped
    (display/resume surface, never authoritative).
    """
    directory = continuations_dir(state_root, plan_id)
    if not directory.is_dir():
        return []
    found: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict):
            doc.setdefault("_path", str(path))
            found.append(doc)
    return found
