"""P11 — the G4 human-approval executor (NO model, NO network).

G4 is HUMAN AUTHORITY: unlike G1–G3 there is no reviewer dispatch, no
gatekeeper call, no ``run_supervised`` anywhere in this path — the
human IS the gatekeeper, and only a real human APPROVE may close G4
PASSED (the engine hard-blocks ``advance(G4_human, PASSED)``).

Sequence for one checkpoint (mirrors the g2/g3 executor shape on top of
the same :class:`GateEngine`):

* ``open(checkpoint)``    — PENDING -> ACTIVE;
* ``wait(checkpoint)``    — ACTIVE -> WAITING_FOR_HUMAN and returns the
  pending-human-action payload (what the checkpoint viewer renders);
* ``apply_human_decision(checkpoint, record, request_sink)`` — the
  engine's ``submit_human_decision``; NEEDS_CHANGES additionally builds
  a :class:`HumanPlannerInputRequest` carrying the VERBATIM feedback and
  hands it to the ``request_sink`` persistence seam (default: atomic
  JSON under ``<state_root>/runs/<plan_id>/human/<checkpoint_id>/``).
  The request is CREATED only — no worker is dispatched, no planner is
  invoked here; the planner interprets human text later through the
  normal gates (human text never goes directly into a worker prompt).

Fail-closed: an engine ``GateTransitionError`` (wrong status, stale
revision, malformed decision, missing feedback) propagates with nothing
persisted; the decision file is written only after the engine accepted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_REQUIRED,
    Checkpoint,
    GateStatus,
    HumanDecision,
    HumanGateRecord,
    HumanPlannerInputRequest,
)

G4_GATE_ID = "G4_human"

#: Constraint text shipped inside the pending payload — the viewer (and
#: any future frontend) renders these, never a free model of its own.
_PENDING_DECISIONS = (
    {
        "decision": HumanDecision.APPROVE.value,
        "effect": "G4 -> PASSED (terminal; the ONLY path to PASSED)",
    },
    {
        "decision": HumanDecision.NEEDS_CHANGES.value,
        "effect": "G4 -> REPAIR; verbatim feedback becomes a "
                  "HumanPlannerInputRequest for the planner (bounded "
                  "repair through the normal gates afterwards)",
    },
    {
        "decision": HumanDecision.CANCEL.value,
        "effect": "G4 -> REJECTED (terminal, deterministic)",
    },
)


@dataclass
class GateConfig4:
    """Static configuration of one G4 lane (pure data, no behavior)."""

    state_root: Path
    clock: Callable[[], float] = time.time


class G4Executor:
    """Parks one checkpoint's G4 gate for a human and applies the verdict.

    All external effects are injectable seams (``decision_sink`` /
    ``request_sink``, ``clock``); defaults are deterministic file writes
    under ``state_root`` — no I/O happens until a decision is applied,
    and no network or model call happens ever.
    """

    def __init__(
        self,
        state_root: Path | str,
        *,
        decision_sink: Callable[[HumanGateRecord], Any] | None = None,
        request_sink: Callable[[HumanPlannerInputRequest], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = GateConfig4(state_root=Path(state_root), clock=clock)
        self._decision_sink = decision_sink or self._default_decision_sink
        self._request_sink = request_sink or self._default_request_sink
        self._clock = clock
        #: ordered ``(gate_id, from, to, actor)`` transitions observed.
        self.transition_log: list[tuple[str, GateStatus, GateStatus, str]] = []

    # -- seams: deterministic defaults ----------------------------------------

    def _human_dir(self, plan_id: str, checkpoint_id: str) -> Path:
        return (
            self.config.state_root
            / "runs"
            / safe_name(plan_id)
            / "human"
            / safe_name(checkpoint_id)
        )

    def _default_decision_sink(self, record: HumanGateRecord) -> Path:
        """One atomic JSON per accepted decision: decisions/<rev>.json."""
        return _atomic_write_json(
            self._human_dir(record.plan_id, record.checkpoint_id)
            / "decisions"
            / f"{record.revision}.json",
            record.model_dump(mode="json"),
        )

    def _default_request_sink(self, request: HumanPlannerInputRequest) -> Path:
        """Planner-input requests land next to the decisions (P11 only
        creates them; the planner consumes them later)."""
        return _atomic_write_json(
            self._human_dir(request.plan_id, request.checkpoint_id)
            / "planner_inputs"
            / f"{safe_name(request.request_id)}.json",
            request.model_dump(mode="json"),
        )

    # -- engine plumbing ---------------------------------------------------------

    def _engine(self, checkpoint: Checkpoint) -> GateEngine:
        if G4_GATE_ID not in checkpoint.gate_records:
            raise GateTransitionError(
                "checkpoint gate_records are not seeded — submit the plan "
                "(seed_gate_records) before executing gates"
            )
        return GateEngine(checkpoint)

    def _sync(self, checkpoint: Checkpoint, engine: GateEngine) -> None:
        checkpoint.gate_records = dict(engine.records)

    # -- the G4 sequence -----------------------------------------------------------

    def open(self, checkpoint: Checkpoint) -> Checkpoint:
        """PENDING -> ACTIVE (mirrors the g2 pattern's first step)."""
        engine = self._engine(checkpoint)
        record = engine.record(G4_GATE_ID)
        if record.applicability != APPLICABILITY_REQUIRED:
            raise GateTransitionError(
                f"{G4_GATE_ID}: plan says NOT_APPLICABLE — nothing for a "
                "human to approve"
            )
        if record.status == GateStatus.PENDING:
            frm = record.status
            engine.advance(G4_GATE_ID, GateStatus.ACTIVE, actor="executor")
            self.transition_log.append(
                (G4_GATE_ID, frm, GateStatus.ACTIVE, "executor")
            )
        elif record.status != GateStatus.ACTIVE:
            raise GateTransitionError(
                f"{G4_GATE_ID} is {record.status.value} (terminal or "
                "parked before open()); nothing to open"
            )
        self._sync(checkpoint, engine)
        return checkpoint

    def wait(self, checkpoint: Checkpoint, plan_id: str = "") -> dict:
        """ACTIVE -> WAITING_FOR_HUMAN; return the pending-human payload.

        ``plan_id`` is carried into the payload for the viewer; when
        empty the checkpoint alone identifies the pending action.
        """
        engine = self._engine(checkpoint)
        record = engine.record(G4_GATE_ID)
        if record.status == GateStatus.ACTIVE:
            engine.advance(
                G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN, actor="executor"
            )
            self.transition_log.append(
                (
                    G4_GATE_ID,
                    GateStatus.ACTIVE,
                    GateStatus.WAITING_FOR_HUMAN,
                    "executor",
                )
            )
        elif record.status != GateStatus.WAITING_FOR_HUMAN:
            raise GateTransitionError(
                f"{G4_GATE_ID} is {record.status.value} — call open() "
                "first (only ACTIVE may park for the human)"
            )
        self._sync(checkpoint, engine)
        return {
            "plan_id": plan_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "gate_id": G4_GATE_ID,
            "status": GateStatus.WAITING_FOR_HUMAN.value,
            "question": (
                f"Approve checkpoint {checkpoint.checkpoint_id} "
                f"({checkpoint.title}) — objective: {checkpoint.objective}?"
            ),
            "legal_decisions": [dict(d) for d in _PENDING_DECISIONS],
        }

    def apply_human_decision(
        self,
        checkpoint: Checkpoint,
        human_record: HumanGateRecord,
        request_sink: Callable[[HumanPlannerInputRequest], Any] | None = None,
    ) -> dict:
        """Apply a real human decision; returns a result summary.

        The engine validates EVERYTHING first (gate kind, status,
        revision monotonicity, feedback presence) — on
        ``GateTransitionError`` nothing is persisted and nothing is
        returned. On success: the decision is persisted via the
        decision sink, and a NEEDS_CHANGES additionally creates a
        ``HumanPlannerInputRequest`` (verbatim feedback) handed to the
        request sink. NO worker is dispatched, in any branch.
        """
        engine = self._engine(checkpoint)
        before = engine.record(G4_GATE_ID).status
        records = engine.submit_human_decision(G4_GATE_ID, human_record)
        checkpoint.gate_records = dict(records)
        self.transition_log.append(
            (
                G4_GATE_ID,
                before,
                records[G4_GATE_ID].status,
                human_record.actor,
            )
        )

        # Engine accepted -> durable. Decision ledger first.
        self._decision_sink(human_record)

        request: HumanPlannerInputRequest | None = None
        if human_record.decision == HumanDecision.NEEDS_CHANGES:
            request = HumanPlannerInputRequest(
                request_id=(
                    f"hpir-{human_record.checkpoint_id}-{human_record.revision}"
                ),
                plan_id=human_record.plan_id,
                checkpoint_id=human_record.checkpoint_id,
                gate_id=G4_GATE_ID,
                feedback=human_record.feedback,  # VERBATIM, never rewritten
                decision=human_record.decision,
                created_at=human_record.submitted_at,
                status="PENDING",
            )
            sink = request_sink or self._request_sink
            sink(request)

        return {
            "gate_id": G4_GATE_ID,
            "checkpoint_id": checkpoint.checkpoint_id,
            "decision": human_record.decision.value,
            "status": records[G4_GATE_ID].status.value,
            "revision": human_record.revision,
            "planner_input_request": request,
        }


# ---------------------------------------------------------------------------
# Live factory (mirrors g2/g3 factory shape; still NO model/network —
# for G4, "live" only means writing to the real per-plan state root)
# ---------------------------------------------------------------------------


def build_live_g4_executor(
    state_root: Path | str,
    *,
    clock: Callable[[], float] = time.time,
) -> G4Executor:
    """G4 executor wired for live operation.

    Decisions and planner-input requests persist atomically under
    ``<state_root>/runs/<plan_id>/human/<checkpoint_id>/`` — the same
    per-plan state root as the rest of AGY (agy_runstate convention).
    There is nothing else to wire: the human is the gatekeeper.
    """
    return G4Executor(state_root, clock=clock)
