"""P13 — DETERMINISTIC approval-chain test: cloud model -> human, end to end.

Zero model calls, zero network, tmp state only. Drives the FULL approval
chain through the REAL authorities, faking only the model/planner SEAMS
(the parts that are "theoretical" until a live cloud session runs):

  (0) baseline submitted plan at rev 1; G4 parked + human NEEDS_CHANGES
      (real G4Executor produces the PENDING planner-input request)
  (1) cloud-model planner          (FAKE seam — Gemini in production)
  (2) P12 consumer validates + persists the continuation        (REAL)
  (3) human G0 APPROVE             (deterministic fixture — the human)
  (4) P13 admission -> submitted canonical plan, gates re-seeded (REAL)
  (5) G1 behavior gate             (gatekeeper verdict faked ACCEPT; the
                                    REAL engine transition with sufficient
                                    deterministic evidence)
  (6) G2 ui gate                   (NOT_APPLICABLE: the legal terminal
                                    skip — the machine path is refused;
                                    a second scenario runs G2 REQUIRED)
  (7) G3 adversarial gate          (as G1)
  (8) G4 human gate                (REAL G4Executor: PENDING->ACTIVE->
                                    WAITING_FOR_HUMAN; machine refused)
  (9) human APPROVE                (REAL G4Executor.apply_human_decision —
                                    the ONLY path that may close G4)

Engine/executor semantics honored: each authority acts on the CURRENT
checkpoint (a fresh engine per step deep-copies checkpoint.gate_records —
the same shape the runtime takes), so the checkpoint object is the single
source of truth and every step syncs back, exactly like the CLI does.

This is the chain a live cloud session would walk; every authority
transition here is production code, so the live session only swaps the
two fakes for the real Gemini planner and the real MiMo Pro gatekeepers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.continuation_admission import (
    admit_continuation,
)
from app.orchestration.checkpoint_planning.g1_executor import GatekeeperDecision
from app.orchestration.checkpoint_planning.g4_executor import (
    G4_GATE_ID,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
)
from app.orchestration.checkpoint_planning.planner_input_consumer import (
    PlannerInputConsumer,
    requests_dir,
)
from app.orchestration.checkpoint_planning.schemas import (
    G0Approval,
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    CheckpointStatus,
    GateExpectations,
    GateStatus,
    HumanDecision,
    HumanGateRecord,
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.submission import (
    seed_gate_records,
)

PLAN_ID = "plan-chain"
CP_ID = "cp-1"
FEEDBACK = "button label wrong — keep the “Cancel” action verbatim"
NOW = "2026-09-29T12:00:00+00:00"


# ---------------------------------------------------------------------------
# fakes (the ONLY non-production pieces in the chain)
# ---------------------------------------------------------------------------


class FakeCloudPlanner:
    """The 'cloud model' seam. In production: GeminiCloudPlanner via the
    AGY CLI. Here it emits a deterministic valid continuation (same plan
    shape, revision + 1) — exactly what the REAL P12 consumer then
    validates through the existing pipeline."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_plan(self, **kwargs):  # pragma: no cover - never called
        raise AssertionError("consumer must use revise_plan")

    def revise_plan(self, *, plan, evidence, reason):
        self.calls.append({"reason": reason, "evidence": evidence})
        assert "human_feedback_verbatim" in evidence, (
            "human feedback must travel as quoted evidence data"
        )
        continuation = plan.model_copy(deep=True)
        continuation.revision += 1
        return continuation


class AcceptingModelGatekeeper:
    """The 'MiMo Pro gatekeeper' seam, theoretical: always ACCEPT.

    A fake verdict alone cannot pass a gate — the REAL engine still
    requires a sufficient GateExecutionRecord (supervisor_decision
    ACCEPT + RED/GREEN/cumulative evidence) for any PASSED transition."""

    def decide(self, evidence, prior_repairs) -> GatekeeperDecision:
        return GatekeeperDecision(
            decision="ACCEPT",
            reason=f"deterministic-fake ACCEPT for {evidence.gate_id}",
            session_id="fake-sup-1",
        )


def sufficient_evidence(plan_id: str, checkpoint_id: str, gate_id: str) -> GateExecutionRecord:
    """Sufficient deterministic evidence (the P9 recipe)."""
    return GateExecutionRecord(
        plan_id=plan_id,
        checkpoint_id=checkpoint_id,
        gate_id=gate_id,
        attempt=1,
        requirements={"required": True},
        worker_sessions=[{"role": "implementer", "session_id": "w1", "ok": True}],
        changed_files=["src/module.py"],
        deterministic_checks=[{"name": "red", "ok": True, "detail": "exit 1"}],
        red_evidence={"ok": True, "returncode": 1},
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_session_id="fake-sup-1",
        supervisor_decision="ACCEPT",
        repair_lineage=[],
        timestamps={"started_at": NOW},
    )


# ---------------------------------------------------------------------------
# chain drivers (production code paths)
# ---------------------------------------------------------------------------


def make_checkpoint(cid: str = CP_ID, *, g2_required: bool = False) -> Checkpoint:
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        gates=CheckpointGates(
            G1_behavior=GateExpectations(required=True, planned_tests=["tests/test_x.py"]),
            G2_ui=GateExpectations(
                required=g2_required,
                reason=None if g2_required else "no UI surface",
            ),
            G3_adversarial=GateExpectations(required=True),
            G4_human=GateExpectations(required=True),
        ),
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


class GateStep:
    """One model-gate step against the CURRENT checkpoint, mirroring the
    runtime: a fresh engine deep-copies checkpoint.gate_records, the
    authority acts, and the live records sync back onto the checkpoint.

    The (faked) gatekeeper verdict is consulted before the transition and
    its ACCEPT is the ``supervisor_decision`` the evidence record cites."""

    def __init__(self, checkpoint: Checkpoint, gatekeeper: AcceptingModelGatekeeper):
        self.cp = checkpoint
        self.gatekeeper = gatekeeper

    def pass_gate(self, gate_id: str) -> None:
        engine = GateEngine(self.cp)
        record = engine.record(gate_id)
        assert record.applicability == "REQUIRED", (
            f"{gate_id} unexpectedly NOT_APPLICABLE"
        )
        assert record.status == GateStatus.PENDING
        evidence = sufficient_evidence(PLAN_ID, self.cp.checkpoint_id, gate_id)
        decision = self.gatekeeper.decide(evidence, [])
        if decision.decision != "ACCEPT":
            raise AssertionError(f"gatekeeper did not ACCEPT: {decision.reason}")
        engine.advance(gate_id, GateStatus.ACTIVE, actor="runtime")
        engine.advance(gate_id, GateStatus.PASSED, evidence=evidence, actor="gatekeeper")
        self.cp.gate_records = dict(engine.records)


def run_chain(tmp_path: Path, *, g2_required: bool = False) -> dict:
    state_root = tmp_path / "state"

    # (0) baseline submitted plan at rev 1, G4 parked + NEEDS_CHANGES
    baseline = make_checkpoint(g2_required=g2_required)
    g4a = build_live_g4_executor(state_root)
    g4a.open(baseline)
    g4a.wait(baseline, plan_id=PLAN_ID)
    g4a.apply_human_decision(
        baseline,
        HumanGateRecord(
            plan_id=PLAN_ID, checkpoint_id=CP_ID, gate_id=G4_GATE_ID,
            decision=HumanDecision.NEEDS_CHANGES, feedback=FEEDBACK,
            submitted_at=NOW, actor="human:leon", provenance="cli", revision=1,
        ),
    )
    # the canonical plan doc the consumer loads (submitted, gates seeded,
    # G4 parked REPAIR by the real executor above — apply_human_decision
    # has already synced the post-decision gate state onto the checkpoint)
    baseline_doc = CheckpointPlan(
        plan_id=PLAN_ID,
        feature_id="chain",
        objective="refund window behaviour",
        planner=PlannerProvenance(role="cloud", provider="gemini"),
        checkpoints=[baseline],
        status="SUBMITTED",
        revision=1,
        created_at=NOW,
    )
    (state_root / "plans" / PLAN_ID).mkdir(parents=True, exist_ok=True)
    (state_root / "plans" / PLAN_ID / "checkpoint_plan.json").write_text(
        json.dumps(baseline_doc.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )

    # (1)+(2) cloud-model planner (FAKE) -> P12 consumer (REAL)
    planner = FakeCloudPlanner()
    consumer = PlannerInputConsumer(state_root, planner=planner)
    result = consumer.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "CONSUMED", result
    request_id = result["consumed"]
    assert len(planner.calls) == 1
    req_doc = json.loads(
        sorted(requests_dir(state_root, PLAN_ID, CP_ID).glob("*.json"))[0].read_text()
    )
    assert req_doc["status"] == "CONSUMED"
    assert req_doc["feedback"] == FEEDBACK
    cont_rev = result["continuation_revision"]

    # (3)+(4) human G0 APPROVE (deterministic fixture) -> P13 admission (REAL)
    approval = G0Approval(
        decision="APPROVE", revision=cont_rev,
        approver="human:leon", approved_at=NOW,
    )
    admission = admit_continuation(state_root, PLAN_ID, request_id, g0_approval=approval)
    assert admission.accepted and not admission.idempotent
    assert admission.revision == cont_rev
    plan_doc = json.loads(
        (state_root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
    )
    assert plan_doc["status"] == "SUBMITTED"
    assert plan_doc["g0"]["decision"] == "APPROVE"
    assert plan_doc["g0"]["revision"] == cont_rev

    # (5)-(9) run the approved plan's gates through the REAL authorities
    submitted = CheckpointPlan.model_validate(plan_doc)
    cp = submitted.checkpoints[0]
    gatekeeper = AcceptingModelGatekeeper()
    step = GateStep(cp, gatekeeper)
    g2_applicability = cp.gate_records["G2_ui"].applicability

    step.pass_gate("G1_behavior")                      # (5) model gate
    if g2_applicability == "NOT_APPLICABLE":           # (6) legal terminal skip
        engine = GateEngine(cp)
        with pytest.raises(GateTransitionError):
            engine.advance("G2_ui", GateStatus.ACTIVE)
    else:
        step.pass_gate("G2_ui")                        # (6') required G2 runs
    step.pass_gate("G3_adversarial")                   # (7) model gate

    # (8) G4 human — real executor parks for the human
    g4b = build_live_g4_executor(state_root)
    g4b.open(cp)
    g4b.wait(cp, plan_id=PLAN_ID)
    engine = GateEngine(cp)
    assert engine.record(G4_GATE_ID).status == GateStatus.WAITING_FOR_HUMAN
    # negative control: NO model/executor may stamp G4 PASSED
    with pytest.raises(GateTransitionError):
        engine.advance(G4_GATE_ID, GateStatus.PASSED,
                       evidence=sufficient_evidence(PLAN_ID, CP_ID, G4_GATE_ID))
    # (9) human APPROVE — the ONLY legal G4 closure (real executor)
    g4b.apply_human_decision(
        cp,
        HumanGateRecord(
            plan_id=PLAN_ID, checkpoint_id=CP_ID, gate_id=G4_GATE_ID,
            decision=HumanDecision.APPROVE, feedback="",
            submitted_at=NOW, actor="human:leon", provenance="cli", revision=1,
        ),
    )
    engine = GateEngine(cp)
    assert engine.record(G4_GATE_ID).status == GateStatus.PASSED

    # reflect final gate state into the canonical doc (as the CLI does)
    from app.orchestration.agy_runstate import _atomic_write_json

    doc = json.loads(
        (state_root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
    )
    for entry in doc["checkpoints"]:
        if entry["checkpoint_id"] == CP_ID:
            entry["gate_records"] = {
                k: rec.model_dump(mode="json") for k, rec in cp.gate_records.items()
            }
    _atomic_write_json(state_root / "plans" / PLAN_ID / "checkpoint_plan.json", doc)

    final = json.loads(
        (state_root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
    )
    return {
        "planner_calls": len(planner.calls),
        "g2_applicability": g2_applicability,
        "final_gates": {
            k: v["status"] for k, v in final["checkpoints"][0]["gate_records"].items()
        },
        "g4_closed_by": final["checkpoints"][0]["gate_records"][G4_GATE_ID]["closed_by"],
        "next_gate_at_admission": admission.next_gate,
        "continuation_revision": cont_rev,
    }


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


def test_full_approval_chain_cloud_to_human(tmp_path):
    out = run_chain(tmp_path, g2_required=False)
    assert out["final_gates"] == {
        "G1_behavior": "PASSED",
        "G2_ui": "PENDING",          # NOT_APPLICABLE: stays seeded, never ran
        "G3_adversarial": "PASSED",
        "G4_human": "PASSED",
    }
    assert out["g4_closed_by"] == "human:leon"     # only the human closed G4
    assert out["g2_applicability"] == "NOT_APPLICABLE"
    assert out["planner_calls"] == 1               # one cloud-model interaction
    assert out["next_gate_at_admission"] == f"{CP_ID}:G1_behavior"
    assert out["continuation_revision"] == 2


def test_full_approval_chain_with_required_g2(tmp_path):
    """Same chain with G2 REQUIRED: the (faked) UI-review model gate runs
    through the same real engine path as G1/G3."""
    out = run_chain(tmp_path, g2_required=True)
    assert out["final_gates"] == {
        "G1_behavior": "PASSED",
        "G2_ui": "PASSED",
        "G3_adversarial": "PASSED",
        "G4_human": "PASSED",
    }
    assert out["g4_closed_by"] == "human:leon"
    assert out["planner_calls"] == 1


def test_machine_cannot_stamp_g4_even_after_model_gates_pass(tmp_path):
    """The negative control in isolation: after G1..G3 PASSED (all model
    verdicts), the ONLY G4 path is the human."""
    state_root = tmp_path / "state"
    cp = make_checkpoint()
    step = GateStep(cp, AcceptingModelGatekeeper())
    step.pass_gate("G1_behavior")
    g4 = build_live_g4_executor(state_root)
    g4.open(cp)
    g4.wait(cp, plan_id=PLAN_ID)
    engine = GateEngine(cp)
    with pytest.raises(GateTransitionError):
        engine.advance(G4_GATE_ID, GateStatus.PASSED,
                       evidence=sufficient_evidence(PLAN_ID, CP_ID, G4_GATE_ID))
    g4.apply_human_decision(
        cp,
        HumanGateRecord(
            plan_id=PLAN_ID, checkpoint_id=CP_ID, gate_id=G4_GATE_ID,
            decision=HumanDecision.APPROVE, feedback="",
            submitted_at=NOW, actor="human:leo", provenance="cli", revision=1,
        ),
    )
    engine = GateEngine(cp)
    assert engine.record(G4_GATE_ID).status == GateStatus.PASSED
    assert engine.record(G4_GATE_ID).closed_by == "human:leo"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
