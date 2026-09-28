"""P9 — generic deterministic gate state machine (all four gate kinds).

The planner defines what a gate requires; the runtime proves whether it
passed. ``GateEngine`` is the proving layer's state machine: pure
transition logic + evidence checks, no I/O, no network, no model calls,
so it is fully unit-testable.

Transition table (ENFORCED — anything else raises ``GateTransitionError``)::

    PENDING  -> {ACTIVE}
    ACTIVE   -> {PASSED, REPAIR, REJECTED}
    REPAIR   -> {ACTIVE}
    PASSED   -> {}            (terminal)
    REJECTED -> {}            (terminal)

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

from datetime import datetime, timezone

from app.orchestration.checkpoint_planning.schemas import (
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_REQUIRED,
    GATE_KINDS,
    Checkpoint,
    GateRecord,
    GateStatus,
)


class GateTransitionError(RuntimeError):
    """Raised on any illegal gate-state transition."""


# The enforced transition table (per current GateStatus).
_TRANSITIONS: dict[GateStatus, frozenset[GateStatus]] = {
    GateStatus.PENDING: frozenset({GateStatus.ACTIVE}),
    GateStatus.ACTIVE: frozenset(
        {GateStatus.PASSED, GateStatus.REPAIR, GateStatus.REJECTED}
    ),
    GateStatus.REPAIR: frozenset({GateStatus.ACTIVE}),
    GateStatus.PASSED: frozenset(),
    GateStatus.REJECTED: frozenset(),
}

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

        NOT_APPLICABLE gates have no legal targets (terminal).
        """
        record = self.record(gate_id)
        if record.applicability == APPLICABILITY_NOT_APPLICABLE:
            return set()
        return set(_TRANSITIONS[record.status])

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
        if to not in legal:
            raise GateTransitionError(
                f"illegal gate transition {gate_id!r}: {current.value} -> "
                f"{to.value} (legal targets: "
                f"{sorted(s.value for s in legal) or 'none — terminal'})"
            )

        if to == GateStatus.PASSED:
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
