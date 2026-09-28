"""P9 — strict schema + evidence-predicate tests for GateExecutionRecord.

Mirrors the CommitGateRecord discipline: exact field set on both sides
of (de)serialization, and PASSED-proving only through the deterministic
predicate (ACCEPT + GREEN + cumulative + all checks ok; G1 additionally
requires RED).
"""

from __future__ import annotations

import dataclasses
import pytest

from app.orchestration.checkpoint_planning.gate_evidence import (
    RECORD_FIELDS,
    GateExecutionRecord,
    g1_evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.schemas import GATE_KINDS


def full_record(**overrides) -> GateExecutionRecord:
    kwargs: dict = dict(
        plan_id="plan-1",
        checkpoint_id="cp-1",
        gate_id="G1_behavior",
        attempt=2,
        requirements={"required": True, "planned_tests": ["tests/test_x.py"]},
        worker_sessions=[{"role": "test-author", "session_id": "s1", "ok": True}],
        changed_files=["tests/test_x.py", "app/x.py"],
        deterministic_checks=[
            {"name": "red", "ok": True, "detail": "exit 1"},
            {"name": "green", "ok": True, "detail": "exit 0"},
        ],
        red_evidence={"ok": True, "returncode": 1},
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_session_id="sup-9",
        supervisor_decision="ACCEPT",
        repair_lineage=[1],
        timestamps={"started_at": "2026-09-28T00:00:00+00:00",
                    "finished_at": "2026-09-28T00:05:00+00:00"},
    )
    kwargs.update(overrides)
    return GateExecutionRecord(**kwargs)


# ---------------------------------------------------------------------------
# frozen + round-trip
# ---------------------------------------------------------------------------


def test_record_is_frozen():
    rec = full_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.attempt = 9  # type: ignore[misc]


def test_to_dict_from_dict_round_trip_equality():
    rec = full_record()
    assert GateExecutionRecord.from_dict(rec.to_dict()) == rec
    assert list(rec.to_dict().keys()) == list(RECORD_FIELDS)


# ---------------------------------------------------------------------------
# strict from_dict
# ---------------------------------------------------------------------------


def test_from_dict_rejects_extra_key_naming_it():
    payload = full_record().to_dict()
    payload["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        GateExecutionRecord.from_dict(payload)


def test_from_dict_rejects_missing_key_naming_it():
    payload = full_record().to_dict()
    del payload["repair_lineage"]
    with pytest.raises(ValueError, match="repair_lineage"):
        GateExecutionRecord.from_dict(payload)


def test_from_dict_rejects_non_object():
    with pytest.raises(ValueError):
        GateExecutionRecord.from_dict(["not", "a", "dict"])


# ---------------------------------------------------------------------------
# constructor validation
# ---------------------------------------------------------------------------


def test_gate_id_must_be_a_known_kind():
    with pytest.raises(ValueError):
        full_record(gate_id="G9_vibes")
    for kind in GATE_KINDS:
        full_record(gate_id=kind)  # no raise


def test_attempt_must_be_at_least_one():
    with pytest.raises(ValueError):
        full_record(attempt=0)
    with pytest.raises(ValueError):
        full_record(attempt=-3)


def test_bad_supervisor_decision_rejected():
    with pytest.raises(ValueError):
        full_record(supervisor_decision="MAYBE")
    for decision in (None, "ACCEPT", "REPAIR", "REJECT"):
        full_record(supervisor_decision=decision,
                    **({"green_evidence": {"ok": True}} if decision == "ACCEPT" else {}))


def test_accept_without_green_evidence_rejected():
    with pytest.raises(ValueError, match="green_evidence"):
        full_record(green_evidence=None, supervisor_decision="ACCEPT")


# ---------------------------------------------------------------------------
# is_passed matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, True),
        ({"supervisor_decision": "REPAIR"}, False),
        ({"supervisor_decision": "REJECT"}, False),
        ({"supervisor_decision": None}, False),
        ({"green_evidence": None, "supervisor_decision": "REPAIR"}, False),
        ({"cumulative_gate": None}, False),
        ({"deterministic_checks": [{"name": "red", "ok": False}]}, False),
        ({"deterministic_checks": [{"name": "red"}]}, False),  # missing ok -> not ok
        ({"deterministic_checks": []}, True),
    ],
)
def test_is_passed_matrix(overrides, expected):
    if overrides.get("supervisor_decision", "ACCEPT") == "ACCEPT" and (
        overrides.get("green_evidence", "keep") is None
    ):
        pytest.skip("ACCEPT without green is unconstructable (fail-closed)")
    assert full_record(**overrides).is_passed() is expected


def test_is_passed_false_when_green_none_and_decision_not_accept():
    rec = full_record(green_evidence=None, supervisor_decision=None)
    assert rec.is_passed() is False


# ---------------------------------------------------------------------------
# G1 sufficiency
# ---------------------------------------------------------------------------


def test_g1_sufficiency_requires_red_evidence():
    assert g1_evidence_is_sufficient(full_record()) is True
    no_red = full_record(
        red_evidence=None,
        supervisor_decision="REPAIR",  # not ACCEPT -> is_passed False anyway
    )
    assert g1_evidence_is_sufficient(no_red) is False
    # The interesting case: everything passes but RED was never proven.
    # A non-ACCEPT record with green but no red is constructable; show the
    # G1 helper refuses sufficiency without red even if is_passed would
    # otherwise hold (ACCEPT forces green; red is the only missing leg).
    almost = full_record()
    mutated = dataclasses.replace(almost, red_evidence=None)
    assert mutated.is_passed() is True  # is_passed does not require RED
    assert g1_evidence_is_sufficient(mutated) is False  # G1 does
