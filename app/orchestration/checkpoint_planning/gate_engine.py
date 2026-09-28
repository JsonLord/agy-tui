"""P9 — generic deterministic gate state machine (all four gate kinds).

The planner defines what a gate requires; the runtime proves whether it
passed. ``GateEngine`` is the proving layer's state machine: pure
transition logic + evidence checks, no I/O, no network, no model calls,
so it is fully unit-testable.

Transition table (ENFORCED — anything else raises ``GateTransitionError``)::

    PENDING            -> {ACTIVE}
    ACTIVE             -> {PASSED, REPAIR, REJECTED, WAITING_FOR_HUMAN}
    WAITING_FOR_HUMAN  -> {PASSED, REJECTED}   (human decisions ONLY —
                          via submit_human_decision; advance() refuses.
                          NEEDS_CHANGES -> REPAIR is additionally legal on
                          the human path, which is not table-bound)
    REPAIR             -> {ACTIVE}
    PASSED             -> {}            (terminal)
    REJECTED           -> {}            (terminal)

P11 additions (G4 human authority):

* ``WAITING_FOR_HUMAN`` is entered by the G4 executor (ACTIVE ->
  WAITING_FOR_HUMAN) and resolved ONLY by
  ``GateEngine.submit_human_decision``: APPROVE -> PASSED, NEEDS_CHANGES
  -> REPAIR, CANCEL -> REJECTED. ``advance(to=PASSED)`` is hard-blocked
  for the G4 gate — no model, executor, or piece of machine evidence may
  ever stamp G4 PASSED; the human decision is the sole authority.

A gate whose applicability is NOT_APPLICABLE is terminal from the start:
it comes only from the validated submitted plan and a worker can never
force it PASSED.

Hard invariants encoded here:

* PENDING -> PASSED is NEVER allowed (even with fully-valid evidence —
  a gate must be ACTIVE first).
* advance(to=PASSED) requires a ``GateExecutionRecord`` that satisfies
  the gate's sufficiency predicate (for G1: deterministic RED + GREEN +
  cumulative + all checks ok; the gatekeeper ACCEPT is stamped into the
  record by the executor, never assumed).
* PASSED / REJECTED / REPAIR transitions stamp ``closed_at`` /
  ``closed_by``.
* The engine operates on a deep copy of the checkpoint — durable state
  is only mutated through the returned ``records`` by whoever owns the
  engine (the executor persists them).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_REQUIRED,
    GATE_KINDS,
    Checkpoint,
    GateRecord,
    GateStatus,
    HumanDecision,
    HumanGateRecord,
)


class GateTransitionError(RuntimeError):
    """Raised on any illegal gate-state transition."""


# The enforced transition table (per current GateStatus).
#
# P11 note: WAITING_FOR_HUMAN is only ever ENTERED on the G4_human gate,
# via a dedicated rule in ``advance`` (kept OUT of ACTIVE's table entry so
# the machine-gate contract P9 froze — legal targets of ACTIVE are exactly
# {PASSED, REPAIR, REJECTED} — stays literally true). Resolving
# WAITING_FOR_HUMAN is only ever done by ``submit_human_decision`` (which
# bypasses this table deliberately); the machine path refuses everything
# from WAITING_FOR_HUMAN, and refuses PASSED for G4 outright.
_TRANSITIONS: dict[GateStatus, frozenset[GateStatus]] = {
    GateStatus.PENDING: frozenset({GateStatus.ACTIVE}),
    GateStatus.ACTIVE: frozenset(
        {GateStatus.PASSED, GateStatus.REPAIR, GateStatus.REJECTED}
    ),
    GateStatus.WAITING_FOR_HUMAN: frozenset(
        {GateStatus.PASSED, GateStatus.REJECTED}
    ),
    GateStatus.REPAIR: frozenset({GateStatus.ACTIVE}),
    GateStatus.PASSED: frozenset(),
    GateStatus.REJECTED: frozenset(),
}

#: The human-authority gate kind (P11). GATE_KINDS already contains it.
G4_HUMAN_GATE_ID = "G4_human"

# Statuses that end the gate's life (no legal targets).
_TERMINAL_STATUSES = frozenset({GateStatus.PASSED, GateStatus.REJECTED})

# Terminal-or-repair states that stamp closed_at/closed_by when entered.
_CLOSING_STATUSES = frozenset(
    {GateStatus.PASSED, GateStatus.REJECTED, GateStatus.REPAIR}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GateEngine:
    """Deterministic per-checkpoint gate state machine.

    ``checkpoint.gate_records`` must already be seeded (submission's
    ``seed_gate_records``). The engine deep-copies the records so the
    input checkpoint is never mutated in place; ``engine.records`` is
    the live state.
    """

    def __init__(self, checkpoint: Checkpoint) -> None:
        self._checkpoint_id = checkpoint.checkpoint_id
        self.records: dict[str, GateRecord] = {
            gate_id: record.model_copy(deep=True)
            for gate_id, record in checkpoint.gate_records.items()
        }

    # -- introspection ---------------------------------------------------------

    def record(self, gate_id: str) -> GateRecord:
        try:
            return self.records[gate_id]
        except KeyError:
            raise GateTransitionError(f"unknown gate: {gate_id!r}") from None

    def legal_target_states(self, gate_id: str) -> set[GateStatus]:
        """States the gate MAY move to from its current state.

        NOT_APPLICABLE gates have no legal targets (terminal). P11: on
        the G4 human gate, WAITING_FOR_HUMAN is additionally legal from
        ACTIVE (the executor's park step); nothing is ever legal from
        WAITING_FOR_HUMAN through this machine-facing view — resolving
        it is the human path (``submit_human_decision``).
        """
        record = self.record(gate_id)
        if record.applicability == APPLICABILITY_NOT_APPLICABLE:
            return set()
        if record.status == GateStatus.WAITING_FOR_HUMAN:
            # machine-facing view: the human path resolves it, advance()
            # never does — nothing is legal here.
            return set()
        targets = set(_TRANSITIONS[record.status])
        if (
            gate_id == G4_HUMAN_GATE_ID
            and record.status == GateStatus.ACTIVE
        ):
            targets.add(GateStatus.WAITING_FOR_HUMAN)
        return targets

    def is_passed(self, gate_id: str) -> bool:
        return self.record(gate_id).status == GateStatus.PASSED

    def is_terminal(self, gate_id: str) -> bool:
        record = self.record(gate_id)
        if record.applicability == APPLICABILITY_NOT_APPLICABLE:
            return True
        return record.status in _TERMINAL_STATUSES

    def next_gate(self) -> str | None:
        """First REQUIRED, non-terminal gate in GATE_KINDS order that is
        PENDING (the runtime's "what to work next"); None when all
        required gates are terminal."""
        for gate_id in GATE_KINDS:
            record = self.records.get(gate_id)
            if record is None:
                continue
            if record.applicability != APPLICABILITY_REQUIRED:
                continue
            if not self.is_terminal(gate_id) and record.status == GateStatus.PENDING:
                return gate_id
        return None

    # -- transitions -------------------------------------------------------------

    def advance(
        self,
        gate_id: str,
        to: GateStatus,
        *,
        evidence=None,
        actor: str = "runtime",
    ) -> GateRecord:
        """Apply one transition, persist it on the live GateRecord, return it.

        ``to=PASSED`` additionally requires ``evidence`` — a
        ``GateExecutionRecord`` for this gate whose sufficiency predicate
        holds (see ``gate_evidence.evidence_is_sufficient``).
        """
        record = self.record(gate_id)

        if record.applicability == APPLICABILITY_NOT_APPLICABLE:
            raise GateTransitionError(
                f"gate {gate_id!r} is NOT_APPLICABLE (terminal from the plan); "
                "advance is not allowed"
            )

        current = record.status
        legal = _TRANSITIONS[current]
        # P11 G4 human-authority rules: once parked for the human, the
        # MACHINE path is refused for every target (resolved only by
        # submit_human_decision); WAITING_FOR_HUMAN is entered only from
        # ACTIVE on G4_human. Checked BEFORE the table so the refusal
        # message names the authority, not the table.
        if current == GateStatus.WAITING_FOR_HUMAN:
            raise GateTransitionError(
                f"gate {gate_id!r} is WAITING_FOR_HUMAN — only "
                "submit_human_decision() may resolve it (the machine "
                "path is refused)"
            )
        if to not in legal:
            if to == GateStatus.WAITING_FOR_HUMAN:
                if gate_id != G4_HUMAN_GATE_ID or current != GateStatus.ACTIVE:
                    raise GateTransitionError(
                        f"illegal gate transition {gate_id!r}: "
                        f"{current.value} -> {to.value} — WAITING_FOR_HUMAN "
                        f"is entered only from ACTIVE on {G4_HUMAN_GATE_ID!r}"
                    )
            else:
                raise GateTransitionError(
                    f"illegal gate transition {gate_id!r}: {current.value} -> "
                    f"{to.value} (legal targets: "
                    f"{sorted(s.value for s in legal) or 'none — terminal'})"
                )

        if to == GateStatus.PASSED:
            if gate_id == G4_HUMAN_GATE_ID:
                raise GateTransitionError(
                    f"gate {gate_id!r} is human authority: no machine "
                    "evidence or executor may stamp G4 PASSED — only "
                    "submit_human_decision() with a real APPROVE may "
                    "close it"
                )
            if evidence is None:
                raise GateTransitionError(
                    f"gate {gate_id!r} cannot advance to PASSED without "
                    "deterministic evidence"
                )
            from app.orchestration.checkpoint_planning.gate_evidence import (
                evidence_is_sufficient,
            )

            if not evidence_is_sufficient(gate_id, evidence):
                raise GateTransitionError(
                    f"gate {gate_id!r} refuses PASSED: the supplied evidence "
                    "record does not prove the gate (needs ACCEPT, GREEN, "
                    "cumulative run, and all deterministic checks ok"
                    + (" plus RED" if gate_id == "G1_behavior" else "")
                    + ")"
                )
            if getattr(evidence, "gate_id", None) != gate_id:
                raise GateTransitionError(
                    f"evidence record is for gate "
                    f"{getattr(evidence, 'gate_id', None)!r}, not {gate_id!r}"
                )
            if getattr(evidence, "checkpoint_id", None) != self._checkpoint_id:
                raise GateTransitionError(
                    "evidence record belongs to a different checkpoint"
                )

        record.status = to
        if to in _CLOSING_STATUSES:
            record.closed_at = _utc_now()
            record.closed_by = actor
        else:
            record.closed_at = None
            record.closed_by = None
        if to == GateStatus.REPAIR:
            record.repair_lineage.append(f"repair:{record.closed_at}")
        if to == GateStatus.PASSED and evidence is not None:
            record.evidence_refs.append(
                f"gate_evidence:{gate_id}:attempt:{getattr(evidence, 'attempt', '?')}"
            )
        return record

    # -- P11: the human-authority path (G4) ----------------------------------

    def submit_human_decision(
        self, gate_id: str, record: HumanGateRecord
    ) -> dict[str, GateRecord]:
        """Resolve a WAITING_FOR_HUMAN G4 gate with a REAL human decision.

        The ONLY path by which G4 may leave WAITING_FOR_HUMAN — and the
        ONLY path by which G4 ever reaches PASSED. Fail-closed rules:

        * the gate must exist, be of kind ``G4_human``, REQUIRED, and
          currently WAITING_FOR_HUMAN;
        * ``record.checkpoint_id`` must match this engine's checkpoint;
        * revision rule (monotonic, no stale overwrite): the accepted
          decision ledger lives in the gate's own ``evidence_refs`` —
          every accepted decision appends
          ``human_decision:rev<N>:<actor>:<DECISION>``. The first
          accepted decision must carry revision 1; each later one must
          carry exactly (last accepted revision) + 1. A duplicate or
          stale revision raises — a late/double submission can never
          overwrite a newer human state;
        * APPROVE -> PASSED, NEEDS_CHANGES -> REPAIR (feedback must be
          non-empty — enforced here as well as on the model), CANCEL ->
          REJECTED.

        Returns the engine's live ``records`` dict (same ownership
        convention as ``advance``: the caller persists it).
        """
        gate = self.record(gate_id)

        if gate_id != G4_HUMAN_GATE_ID:
            raise GateTransitionError(
                f"submit_human_decision is only valid for "
                f"{G4_HUMAN_GATE_ID!r}, not {gate_id!r}"
            )
        if gate.applicability != APPLICABILITY_REQUIRED:
            raise GateTransitionError(
                f"gate {gate_id!r} is {gate.applicability} — nothing for a "
                "human to decide"
            )
        if gate.status != GateStatus.WAITING_FOR_HUMAN:
            raise GateTransitionError(
                f"gate {gate_id!r} is {gate.status.value}, not "
                "WAITING_FOR_HUMAN — a human decision is only accepted "
                "while the gate is parked for the human"
            )
        if record.checkpoint_id != self._checkpoint_id:
            raise GateTransitionError(
                "human decision belongs to a different checkpoint: "
                f"{record.checkpoint_id!r} != {self._checkpoint_id!r}"
            )
        if not isinstance(record.decision, HumanDecision):
            raise GateTransitionError(
                f"malformed human decision: {record.decision!r} (must be "
                f"one of {[d.value for d in HumanDecision]})"
            )

        last = _last_accepted_human_revision(gate)
        if record.revision != last + 1:
            raise GateTransitionError(
                f"stale or duplicate human decision revision: got "
                f"{record.revision}, expected {last + 1} (last accepted: "
                f"{last}) — decisions are strictly monotonic so a late "
                "submission can never overwrite a newer human state"
            )
        if (
            record.decision == HumanDecision.NEEDS_CHANGES
            and not record.feedback.strip()
        ):
            raise GateTransitionError(
                "NEEDS_CHANGES without verbatim feedback is invalid "
                "(fail closed — the planner has nothing to interpret)"
            )

        if record.decision == HumanDecision.APPROVE:
            to = GateStatus.PASSED
        elif record.decision == HumanDecision.NEEDS_CHANGES:
            to = GateStatus.REPAIR
        else:
            to = GateStatus.REJECTED

        gate.status = to
        gate.closed_at = record.submitted_at or _utc_now()
        gate.closed_by = record.actor
        gate.evidence_refs.append(
            f"human_decision:rev{record.revision}:{record.actor}:"
            f"{record.decision.value}"
        )
        gate.evidence_refs.append(
            "human_record:"
            + json.dumps(record.model_dump(mode="json"), sort_keys=True)
        )
        if to == GateStatus.REPAIR:
            gate.repair_lineage.append(f"human-repair:{gate.closed_at}")
        return self.records


def _last_accepted_human_revision(gate: GateRecord) -> int:
    """Highest accepted human decision revision on this gate (0 = none).

    The ledger is the gate's own ``evidence_refs``: every accepted
    decision appends ``human_decision:rev<N>:<actor>:<DECISION>``.
    Reading the authority back off the durable record keeps the rule
    deterministic and crash-safe (no engine-side side store to lose).
    """
    latest = 0
    for ref in gate.evidence_refs:
        match = _HUMAN_REV_RE.match(str(ref))
        if match:
            latest = max(latest, int(match.group(1)))
    return latest


_HUMAN_REV_RE = re.compile(r"^human_decision:rev(\d+):")
