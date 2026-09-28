"""P9 — deterministic tests for the generic gate state machine.

The engine is the runtime's proof layer: transitions are pure table
logic, PASSED requires deterministic evidence, NOT_APPLICABLE is
terminal from the plan. No I/O, no models, no network.
"""

from __future__ import annotations

import pytest

from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.schemas import (
    GATE_KINDS,
    Checkpoint,
    CheckpointGates,
    CheckpointStatus,
    GateExpectations,
    GateRecord,
    GateStatus,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _expectations(required: bool = True, reason: str | None = None) -> GateExpectations:
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=["tests/test_x.py"] if required else [],
    )


def make_checkpoint(cid: str = "cp-1", *, g2_required: bool = False) -> Checkpoint:
    gates = CheckpointGates(
        G1_behavior=_expectations(),
        G2_ui=_expectations(g2_required, None if g2_required else "no UI surface"),
        G3_adversarial=_expectations(),
        G4_human=_expectations(),
    )
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        gates=gates,
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


def sufficient_evidence(gate_id: str = "G1_behavior", checkpoint_id: str = "cp-1",
                        attempt: int = 1) -> GateExecutionRecord:
    """A fully-valid, SUFFICIENT G1 (or generic) evidence record."""
    return GateExecutionRecord(
        plan_id="plan-1",
        checkpoint_id=checkpoint_id,
        gate_id=gate_id,
        attempt=attempt,
        requirements={"required": True},
        worker_sessions=[{"role": "test-author", "session_id": "s1", "ok": True}],
        changed_files=["tests/test_x.py"],
        deterministic_checks=[{"name": "red", "ok": True, "detail": "exit 1"}],
        red_evidence={"ok": True, "returncode": 1},
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_session_id="sup-1",
        supervisor_decision="ACCEPT",
        repair_lineage=[],
        timestamps={"started_at": "2026-09-28T00:00:00+00:00"},
    )


# ---------------------------------------------------------------------------
# legal transitions
# ---------------------------------------------------------------------------


def test_every_legal_transition_succeeds_and_updates_record():
    checkpoint = make_checkpoint()
    engine = GateEngine(checkpoint)

    record = engine.advance("G1_behavior", GateStatus.ACTIVE)
    assert record.status == GateStatus.ACTIVE

    # ACTIVE -> REPAIR and REPAIR -> ACTIVE
    record = engine.advance("G1_behavior", GateStatus.REPAIR, actor="gatekeeper")
    assert record.status == GateStatus.REPAIR
    assert record.closed_by == "gatekeeper"
    assert record.closed_at is not None
    record = engine.advance("G1_behavior", GateStatus.ACTIVE, actor="runtime")
    assert record.status == GateStatus.ACTIVE

    # ACTIVE -> PASSED with sufficient evidence stamps closure + evidence ref
    record = engine.advance(
        "G1_behavior", GateStatus.PASSED,
        evidence=sufficient_evidence(), actor="gatekeeper",
    )
    assert record.status == GateStatus.PASSED
    assert record.closed_by == "gatekeeper"
    assert record.closed_at is not None
    assert any("attempt:1" in ref for ref in record.evidence_refs)

    # ACTIVE -> REJECTED (fresh gate) stamps closure
    engine2 = GateEngine(make_checkpoint("cp-2"))
    engine2.advance("G1_behavior", GateStatus.ACTIVE)
    record2 = engine2.advance("G1_behavior", GateStatus.REJECTED, actor="gatekeeper")
    assert record2.status == GateStatus.REJECTED
    assert record2.closed_by == "gatekeeper"
    assert record2.closed_at is not None


# ---------------------------------------------------------------------------
# illegal transitions (each raises)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "setup, target, with_evidence",
    [
        ("pending", GateStatus.PASSED, True),    # the hard TDD invariant
        ("pending", GateStatus.PASSED, False),
        ("pending", GateStatus.REJECTED, False),
        ("pending", GateStatus.REPAIR, False),
        ("active", GateStatus.PENDING, False),
        ("active", GateStatus.ACTIVE, False),
        ("passed", GateStatus.ACTIVE, False),
        ("passed", GateStatus.REPAIR, False),
        ("rejected", GateStatus.ACTIVE, False),
        ("repair", GateStatus.PASSED, True),
        ("repair", GateStatus.REJECTED, False),
    ],
)
def test_illegal_transitions_raise(setup, target, with_evidence):
    checkpoint = make_checkpoint()
    engine = GateEngine(checkpoint)
    if setup in ("active", "passed", "rejected", "repair"):
        engine.advance("G1_behavior", GateStatus.ACTIVE)
    if setup == "passed":
        engine.advance(
            "G1_behavior", GateStatus.PASSED,
            evidence=sufficient_evidence(), actor="gatekeeper",
        )
    elif setup == "rejected":
        engine.advance("G1_behavior", GateStatus.REJECTED)
    elif setup == "repair":
        engine.advance("G1_behavior", GateStatus.REPAIR)
    evidence = sufficient_evidence() if with_evidence else None
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", target, evidence=evidence)


def test_pending_to_passed_never_allowed_even_with_valid_evidence():
    """Dedicated hard-invariant test: a gate NEVER passes without being
    ACTIVE first, no matter how perfect the evidence."""
    engine = GateEngine(make_checkpoint())
    with pytest.raises(GateTransitionError):
        engine.advance(
            "G1_behavior", GateStatus.PASSED,
            evidence=sufficient_evidence(), actor="worker",
        )
    assert engine.record("G1_behavior").status == GateStatus.PENDING


def test_active_to_passed_requires_evidence():
    engine = GateEngine(make_checkpoint())
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=None)
    # ... and a non-sufficient record (no ACCEPT / no cumulative) is refused
    weak = GateExecutionRecord(
        plan_id="plan-1", checkpoint_id="cp-1", gate_id="G1_behavior",
        attempt=1, requirements={}, worker_sessions=[], changed_files=[],
        deterministic_checks=[], red_evidence=None,
        green_evidence={"ok": True}, cumulative_gate=None,
        supervisor_session_id=None, supervisor_decision=None,
        repair_lineage=[], timestamps={},
    )
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=weak)


def test_evidence_must_match_gate_and_checkpoint():
    engine = GateEngine(make_checkpoint())
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError):
        engine.advance(
            "G1_behavior", GateStatus.PASSED,
            evidence=sufficient_evidence(gate_id="G3_adversarial"),
        )
    with pytest.raises(GateTransitionError):
        engine.advance(
            "G1_behavior", GateStatus.PASSED,
            evidence=sufficient_evidence(checkpoint_id="cp-other"),
        )


# ---------------------------------------------------------------------------
# NOT_APPLICABLE terminals
# ---------------------------------------------------------------------------


def test_not_applicable_gate_is_terminal_and_immovable():
    engine = GateEngine(make_checkpoint())  # G2_ui NOT_APPLICABLE by default
    assert engine.legal_target_states("G2_ui") == set()
    assert engine.is_terminal("G2_ui") is True
    with pytest.raises(GateTransitionError):
        engine.advance("G2_ui", GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError):
        engine.advance(
            "G2_ui", GateStatus.PASSED,
            evidence=sufficient_evidence(gate_id="G2_ui"),
        )


# ---------------------------------------------------------------------------
# copy discipline + next_gate
# ---------------------------------------------------------------------------


def test_engine_operates_on_a_copy():
    checkpoint = make_checkpoint()
    before = {
        gid: rec.model_dump(mode="json")
        for gid, rec in checkpoint.gate_records.items()
    }
    engine = GateEngine(checkpoint)
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    assert checkpoint.gate_records["G1_behavior"].status == GateStatus.PENDING
    assert {
        gid: rec.model_dump(mode="json")
        for gid, rec in checkpoint.gate_records.items()
    } == before
    assert engine.records["G1_behavior"].status == GateStatus.ACTIVE


def test_next_gate_order_and_termination():
    engine = GateEngine(make_checkpoint())
    assert engine.next_gate() == "G1_behavior"

    engine.advance("G1_behavior", GateStatus.ACTIVE)
    engine.advance(
        "G1_behavior", GateStatus.PASSED, evidence=sufficient_evidence(),
        actor="gatekeeper",
    )
    # G2 is NOT_APPLICABLE (skipped); G3 is next REQUIRED PENDING gate
    assert engine.next_gate() == "G3_adversarial"

    engine.advance("G3_adversarial", GateStatus.ACTIVE)
    engine.advance("G3_adversarial", GateStatus.REJECTED)
    engine.advance("G4_human", GateStatus.ACTIVE)
    engine.advance("G4_human", GateStatus.REJECTED)
    assert engine.next_gate() is None


def test_legal_target_states_match_table():
    engine = GateEngine(make_checkpoint())
    assert engine.legal_target_states("G1_behavior") == {GateStatus.ACTIVE}
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    assert engine.legal_target_states("G1_behavior") == {
        GateStatus.PASSED, GateStatus.REPAIR, GateStatus.REJECTED,
    }
    engine.advance("G1_behavior", GateStatus.REPAIR)
    assert engine.legal_target_states("G1_behavior") == {GateStatus.ACTIVE}
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    engine.advance(
        "G1_behavior", GateStatus.PASSED, evidence=sufficient_evidence(),
    )
    assert engine.legal_target_states("G1_behavior") == set()
    assert engine.is_passed("G1_behavior") is True
    assert engine.is_terminal("G1_behavior") is True


def test_unknown_gate_raises():
    engine = GateEngine(make_checkpoint())
    with pytest.raises(GateTransitionError):
        engine.advance("G9_nope", GateStatus.ACTIVE)


def test_all_gate_kinds_seedable():
    checkpoint = make_checkpoint(g2_required=True)
    engine = GateEngine(checkpoint)
    for gate_id in GATE_KINDS:
        assert isinstance(engine.record(gate_id), GateRecord)
        assert engine.record(gate_id).status == GateStatus.PENDING
