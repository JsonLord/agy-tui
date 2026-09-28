"""P12 — CLI tests: `checkpoint decide` (invariant 8).

Four cases, fake-free but model-free: the CLI drives the CANONICAL
G4Executor on a temp state root (no network, no planner call, no model —
G4's whole contract is that the human decision is pure state transition).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.main import checkpoint_main

PLAN_ID = "plan-cli"
CP_ID = "cp-1"


def _state_with_parked_g4(tmp_path: Path, *, evidence_refs: list[str] | None = None):
    """Canonical plan doc whose G4 gate sits WAITING_FOR_HUMAN (rev none)."""
    from app.orchestration.checkpoint_planning.schemas import GateStatus

    from tests.test_agy_p12_continuation import canonical_plan_doc, write_canonical_plan

    plan = canonical_plan_doc(GateStatus.WAITING_FOR_HUMAN)
    plan.plan_id = PLAN_ID
    if evidence_refs:
        plan.checkpoints[0].gate_records["G4_human"].evidence_refs.extend(
            evidence_refs
        )
    write_canonical_plan(tmp_path, plan)
    return tmp_path


def _plan_gate_status(state_root: Path) -> str:
    doc = json.loads(
        (state_root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text(
            encoding="utf-8"
        )
    )
    gates = doc["checkpoints"][0]["gate_records"]
    return gates["G4_human"]["status"]


# ---------------------------------------------------------------------------
# 8a. APPROVE succeeds THROUGH the canonical G4Executor.apply_human_decision
# ---------------------------------------------------------------------------


def test_cli_approve_uses_canonical_function(tmp_path, monkeypatch, capsys):
    state = _state_with_parked_g4(tmp_path)
    from app.orchestration.checkpoint_planning import g4_executor as g4mod

    seen: list = []
    original = g4mod.G4Executor.apply_human_decision

    def spy(self, checkpoint, human_record, request_sink=None):
        seen.append(human_record)
        return original(self, checkpoint, human_record, request_sink)

    monkeypatch.setattr(g4mod.G4Executor, "apply_human_decision", spy)

    code = checkpoint_main(
        ["decide", "--plan-id", PLAN_ID, "--checkpoint-id", CP_ID,
         "--decision", "approve", "--state-root", str(state)]
    )
    assert code == 0
    assert len(seen) == 1                             # the canonical path ran
    assert seen[0].decision.value == "APPROVE"
    assert seen[0].revision == 1                      # ledger default, first = 1
    assert seen[0].actor == "human:cli"
    assert _plan_gate_status(state) == "PASSED"       # durable, reflected
    out = capsys.readouterr().out
    assert out == f"G4 {CP_ID}: APPROVE -> PASSED (revision 1)\n"


# ---------------------------------------------------------------------------
# 8b. NEEDS_CHANGES + feedback persists the planner-input request
# ---------------------------------------------------------------------------


def test_cli_needs_changes_persists_request(tmp_path, capsys):
    state = _state_with_parked_g4(tmp_path)
    code = checkpoint_main(
        ["decide", "--plan-id", PLAN_ID, "--checkpoint-id", CP_ID,
         "--decision", "needs-changes",
         "--feedback", "tighten the spacing",
         "--actor", "human:tester",
         "--state-root", str(state)]
    )
    assert code == 0
    assert _plan_gate_status(state) == "REPAIR"
    requests = sorted(
        (state / "runs" / PLAN_ID / "human" / CP_ID / "planner_inputs")
        .glob("*.json")
    )
    assert len(requests) == 1
    doc = json.loads(requests[0].read_text(encoding="utf-8"))
    assert doc["status"] == "PENDING"
    assert doc["feedback"] == "tighten the spacing"   # VERBATIM
    assert doc["request_id"] == f"hpir-{CP_ID}-1"
    decisions = sorted(
        (state / "runs" / PLAN_ID / "human" / CP_ID / "decisions").glob("*.json")
    )
    assert [p.name for p in decisions] == ["1.json"]
    assert "REPAIR (revision 1)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 8c. invalid decision exits non-zero (argparse choices at parse time)
# ---------------------------------------------------------------------------


def test_cli_invalid_decision_exits_nonzero(tmp_path):
    state = _state_with_parked_g4(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        checkpoint_main(
            ["decide", "--plan-id", PLAN_ID, "--checkpoint-id", CP_ID,
             "--decision", "ship-it", "--state-root", str(state)]
        )
    assert excinfo.value.code != 0
    # nothing was persisted — parse failed before any state touched
    assert not (state / "runs").exists()
    assert _plan_gate_status(state) == "WAITING_FOR_HUMAN"


# ---------------------------------------------------------------------------
# 8d. stale explicit revision exits non-zero via the engine (canonical
#     monotonic rule), with nothing persisted
# ---------------------------------------------------------------------------


def test_cli_stale_revision_exits_nonzero(tmp_path, capsys):
    # canonical gate already carries an accepted rev-2 ledger entry
    # (evidence_refs) -> the engine expects revision 3 next.
    state = _state_with_parked_g4(
        tmp_path,
        evidence_refs=["human_decision:rev2:human:leo:NEEDS_CHANGES"],
    )
    ledger = tmp_path / "runs" / PLAN_ID / "human" / CP_ID / "decisions"
    ledger.mkdir(parents=True)
    for rev in (1, 2):
        (ledger / f"{rev}.json").write_text("{}", encoding="utf-8")

    code = checkpoint_main(
        ["decide", "--plan-id", PLAN_ID, "--checkpoint-id", CP_ID,
         "--decision", "approve", "--revision", "1",
         "--state-root", str(tmp_path)]
    )
    assert code == 1                                   # canonical failure
    err = capsys.readouterr().err
    assert "stale or duplicate" in err
    assert _plan_gate_status(tmp_path) == "WAITING_FOR_HUMAN"  # untouched
    assert sorted(p.name for p in ledger.glob("*.json")) == ["1.json", "2.json"]
