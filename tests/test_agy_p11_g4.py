"""P11 — deterministic tests: G4 human approval, feedback -> planner
input, curated learnings, checkpoint viewer.

No live model, no network, no dispatch: G4's whole point is that the
HUMAN is the gatekeeper, so every test below drives the engine /
executor / viewer with pure in-memory or tmp-path seams.

Spec coverage map (numbered per the P11 spec):
  1. test_g4_enters_waiting_for_human
  2. test_model_can_never_stamp_g4_passed + test_executor_has_no_model_or_dispatch_path
  3. test_human_approve_passes_g4
  4. test_needs_changes_repairs_not_passes
  5. test_feedback_persists_verbatim
  6. test_needs_changes_creates_planner_request_without_worker
  7. test_cancel_rejects_deterministically
  8. test_duplicate_revision_rejected  (revision rule: strictly
     monotonic per-gate, next = last accepted + 1, ledger in
     evidence_refs — chosen and documented in gate_engine)
  9. test_stale_revision_cannot_overwrite_newer_state
 10. test_learning_promotion_is_deliberate_only
 11. test_viewer_renders_mixed_gate_states + two CLI fail-closed tests
 12. test_malformed_human_actions_fail_closed
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.orchestration.checkpoint_planning.checkpoint_viewer import (
    CheckpointViewerError,
    view_checkpoint,
)
from app.orchestration.checkpoint_planning.g4_executor import (
    G4_GATE_ID,
    G4Executor,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.learnings import (
    promote_learning,
    propose_learning_from_feedback,
    read_learnings,
)
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    CheckpointStatus,
    GateExpectations,
    GateStatus,
    HumanDecision,
    HumanGateRecord,
    HumanPlannerInputRequest,
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records
from app.main import checkpoint_main


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _expectations(required: bool = True, reason: str | None = None) -> GateExpectations:
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=["tests/test_x.py"] if required else [],
    )


def make_checkpoint(cid: str = "cp-1") -> Checkpoint:
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        gates=CheckpointGates(
            G1_behavior=_expectations(),
            G2_ui=_expectations(False, "no UI surface"),
            G3_adversarial=_expectations(),
            G4_human=_expectations(),
        ),
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


def human_record(
    decision: HumanDecision,
    *,
    cid: str = "cp-1",
    revision: int = 1,
    feedback: str = "",
    plan_id: str = "plan-1",
    actor: str = "human:leon",
) -> HumanGateRecord:
    return HumanGateRecord(
        plan_id=plan_id,
        checkpoint_id=cid,
        gate_id=G4_GATE_ID,
        decision=decision,
        feedback=feedback,
        submitted_at="2026-09-28T12:00:00+00:00",
        actor=actor,
        provenance="cli",
        revision=revision,
    )


def parked_checkpoint(cid: str = "cp-1") -> tuple[Checkpoint, GateEngine]:
    """G4 driven PENDING -> ACTIVE -> WAITING_FOR_HUMAN via the engine."""
    checkpoint = make_checkpoint(cid)
    engine = GateEngine(checkpoint)
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE, actor="executor")
    engine.advance(
        G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN, actor="executor"
    )
    return checkpoint, engine


def executor_with_sink(tmp_path: Path) -> tuple[G4Executor, list, list]:
    """Executor whose persistence seams are pure capture lists (no I/O)."""
    decisions: list = []
    requests: list = []
    ex = G4Executor(
        tmp_path / "state",
        decision_sink=decisions.append,
        request_sink=requests.append,
    )
    return ex, decisions, requests


# ---------------------------------------------------------------------------
# 1. G4 can enter WAITING_FOR_HUMAN
# ---------------------------------------------------------------------------


def test_g4_enters_waiting_for_human(tmp_path):
    checkpoint = make_checkpoint()
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.PENDING
    ex = build_live_g4_executor(tmp_path / "state")
    ex.open(checkpoint)
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.ACTIVE
    payload = ex.wait(checkpoint, plan_id="plan-1")
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.WAITING_FOR_HUMAN
    assert [t[1:3] for t in ex.transition_log] == [
        (GateStatus.PENDING, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.WAITING_FOR_HUMAN),
    ]
    # pending payload is what the viewer needs: question + three options
    assert payload["status"] == "WAITING_FOR_HUMAN"
    assert [d["decision"] for d in payload["legal_decisions"]] == [
        "APPROVE", "NEEDS_CHANGES", "CANCEL",
    ]


def test_waiting_for_human_only_legal_on_g4_from_active():
    # machine gates can never park for a human
    checkpoint = make_checkpoint()
    engine = GateEngine(checkpoint)
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.WAITING_FOR_HUMAN)
    # G4 can only park from ACTIVE (not PENDING)
    engine2 = GateEngine(make_checkpoint("cp-2"))
    with pytest.raises(GateTransitionError):
        engine2.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN)


# ---------------------------------------------------------------------------
# 2. A model/executor CANNOT mark G4 PASSED
# ---------------------------------------------------------------------------


def test_model_can_never_stamp_g4_passed():
    from app.orchestration.checkpoint_planning.gate_evidence import (
        GateExecutionRecord,
    )

    # Even ACTIVE -> PASSED with evidence that WOULD satisfy the machine
    # sufficiency predicate is hard-blocked for G4: no model output can
    # close the human gate.
    checkpoint = make_checkpoint()
    engine = GateEngine(checkpoint)
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE)
    fake = GateExecutionRecord(
        plan_id="plan-1",
        checkpoint_id="cp-1",
        gate_id=G4_GATE_ID,
        attempt=1,
        requirements={"required": True},
        deterministic_checks=[{"name": "x", "ok": True, "detail": ""}],
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_decision="ACCEPT",
    )
    assert fake.is_passed()  # the machine predicate would say yes...
    with pytest.raises(GateTransitionError, match="human authority"):
        engine.advance(G4_GATE_ID, GateStatus.PASSED, evidence=fake)
    # ...and the engine says no. Gate stays ACTIVE.
    assert engine.record(G4_GATE_ID).status == GateStatus.ACTIVE

    # Once parked, the machine path is refused entirely.
    engine.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN)
    with pytest.raises(GateTransitionError, match="submit_human_decision"):
        engine.advance(G4_GATE_ID, GateStatus.PASSED, evidence=fake)
    with pytest.raises(GateTransitionError):
        engine.advance(G4_GATE_ID, GateStatus.REJECTED)


def test_executor_has_no_model_or_dispatch_path():
    """G4's contract: NO model call, NO network, NO run_supervised."""
    import ast

    import app.orchestration.checkpoint_planning.g4_executor as g4mod

    tree = ast.parse(Path(g4mod.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.add(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in {"run_supervised", "urlopen", "socket", "connect"}:
                raise AssertionError(f"G4 must never call {name!r}")
    forbidden_modules = {"requests", "http", "urllib", "socket", "subprocess"}
    assert not imported & forbidden_modules, (
        f"G4 must not import network/subprocess modules: "
        f"{imported & forbidden_modules}"
    )
    # model machinery must not even be importable into the G4 lane
    assert "app.orchestration.checkpoint_planning.g1_executor" not in imported
    assert "app.integrations" not in imported
    # a live G4 executor takes no gatekeeper/endpoint config at all (g2 does)
    sig = inspect.signature(build_live_g4_executor)
    assert not any("gatekeeper" in p or "url" in p for p in sig.parameters)


# ---------------------------------------------------------------------------
# 3. Human APPROVE -> PASSED (the ONLY path)
# ---------------------------------------------------------------------------


def test_human_approve_passes_g4(tmp_path):
    ex, decisions, requests = executor_with_sink(tmp_path)
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    result = ex.apply_human_decision(checkpoint, human_record(HumanDecision.APPROVE))
    assert result["status"] == "PASSED"
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.PASSED
    assert checkpoint.gate_records[G4_GATE_ID].closed_by == "human:leon"
    assert checkpoint.gate_records[G4_GATE_ID].closed_at == "2026-09-28T12:00:00+00:00"
    assert any(
        ref.startswith("human_decision:rev1:")
        for ref in checkpoint.gate_records[G4_GATE_ID].evidence_refs
    )
    assert [d.decision for d in decisions] == [HumanDecision.APPROVE]
    assert requests == []  # APPROVE creates no planner input


# ---------------------------------------------------------------------------
# 4. NEEDS_CHANGES -> REPAIR (never PASSED)
# ---------------------------------------------------------------------------


def test_needs_changes_repairs_not_passes(tmp_path):
    ex, decisions, requests = executor_with_sink(tmp_path)
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    result = ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.NEEDS_CHANGES, feedback="button label wrong"),
    )
    assert result["status"] == "REPAIR"
    record = checkpoint.gate_records[G4_GATE_ID]
    assert record.status == GateStatus.REPAIR
    assert record.status != GateStatus.PASSED
    assert record.repair_lineage  # bounded repair path preserved
    # REPAIR is re-enterable by the normal machine flow afterwards
    engine = GateEngine(checkpoint)
    assert GateStatus.ACTIVE in engine.legal_target_states(G4_GATE_ID)


# ---------------------------------------------------------------------------
# 5. Feedback persists verbatim (record + planner request + ledger JSON)
# ---------------------------------------------------------------------------


def test_feedback_persists_verbatim(tmp_path):
    weird = "  keep the “Cancel” action\tand DO NOT trim  \n line two: 50%  "
    ex, decisions, requests = executor_with_sink(tmp_path / "sinkstate")
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    record = human_record(HumanDecision.NEEDS_CHANGES, feedback=weird)
    result = ex.apply_human_decision(checkpoint, record)

    assert decisions[0].feedback == weird          # HumanGateRecord verbatim
    request = result["planner_input_request"]
    assert isinstance(request, HumanPlannerInputRequest)
    assert request.feedback == weird              # planner input verbatim
    assert request.status == "PENDING"            # created, not consumed
    # and in the durable ledger (JSON round-trip keeps the exact string)
    payload = json.loads(
        json.dumps(record.model_dump(mode="json"), sort_keys=True)
    )
    assert payload["feedback"] == weird


# ---------------------------------------------------------------------------
# 6. NEEDS_CHANGES creates the request via the seam, executes no worker
# ---------------------------------------------------------------------------


def test_needs_changes_creates_request_and_nothing_executes(tmp_path):
    requests: list = []
    dispatched: list = []
    # The ONLY seams G4 offers are persistence sinks; there is no worker
    # dispatch seam to call, and we also guard against accidental reuse
    # of the G1/G2 machinery by spying on G1Executor.execute_checkpoint.
    from app.orchestration.checkpoint_planning import g1_executor as g1mod

    def _never(*args, **kwargs):
        dispatched.append((args, kwargs))
        raise AssertionError("G4 must never execute a worker")

    original = g1mod.G1Executor.execute_g1
    g1mod.G1Executor.execute_g1 = _never
    try:
        ex = G4Executor(
            tmp_path / "state",
            decision_sink=lambda r: None,
            request_sink=requests.append,
        )
        checkpoint = make_checkpoint()
        ex.open(checkpoint)
        ex.wait(checkpoint, plan_id="plan-1")
        result = ex.apply_human_decision(
            checkpoint,
            human_record(HumanDecision.NEEDS_CHANGES, feedback="tighten spacing"),
        )
    finally:
        g1mod.G1Executor.execute_g1 = original

    assert dispatched == []                       # no worker ran
    assert len(requests) == 1
    assert requests[0].feedback == "tighten spacing"
    assert requests[0].gate_id == G4_GATE_ID
    assert result["planner_input_request"] is requests[0]


# ---------------------------------------------------------------------------
# 7. CANCEL -> REJECTED deterministically
# ---------------------------------------------------------------------------


def test_cancel_rejects_deterministically(tmp_path):
    ex, decisions, requests = executor_with_sink(tmp_path)
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    result = ex.apply_human_decision(checkpoint, human_record(HumanDecision.CANCEL))
    assert result["status"] == "REJECTED"
    record = checkpoint.gate_records[G4_GATE_ID]
    assert record.status == GateStatus.REJECTED
    assert record.closed_by == "human:leon"
    assert requests == []
    # terminal: nothing more legal, and a second human decision is refused
    engine = GateEngine(checkpoint)
    assert engine.is_terminal(G4_GATE_ID)
    assert engine.legal_target_states(G4_GATE_ID) == set()
    with pytest.raises(GateTransitionError):
        engine.submit_human_decision(G4_GATE_ID, human_record(
            HumanDecision.APPROVE, revision=2))


# ---------------------------------------------------------------------------
# 8./9. Revision rule: strictly monotonic per gate (next = last + 1),
# ledger = evidence_refs "human_decision:rev<N>:...". Duplicates and
# stale revisions are rejected; a late decision can never overwrite
# newer human state.
# ---------------------------------------------------------------------------


def _repark(checkpoint: Checkpoint) -> None:
    """REPAIR -> ACTIVE -> WAITING (the bounded repair loop re-parks G4)."""
    engine = GateEngine(checkpoint)
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE, actor="runtime")
    engine.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN, actor="runtime")
    checkpoint.gate_records = dict(engine.records)


def test_duplicate_revision_rejected(tmp_path):
    ex, _, _ = executor_with_sink(tmp_path)
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    fb = human_record(HumanDecision.NEEDS_CHANGES, feedback="round 1", revision=1)
    ex.apply_human_decision(checkpoint, fb)
    _repark(checkpoint)
    # replay of the SAME revision (e.g. a double-submit) -> rejected
    with pytest.raises(GateTransitionError, match="stale or duplicate"):
        ex.apply_human_decision(
            checkpoint,
            human_record(HumanDecision.APPROVE, feedback="", revision=1),
        )
    # revision 0 / skipped revisions are equally refused
    with pytest.raises(GateTransitionError, match="stale or duplicate"):
        ex.apply_human_decision(
            checkpoint,
            human_record(HumanDecision.APPROVE, revision=3),
        )
    # the correct next revision is accepted
    result = ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.APPROVE, revision=2),
    )
    assert result["status"] == "PASSED"


def test_stale_revision_cannot_overwrite_newer_state(tmp_path):
    """A stale decision must not mutate a gate carrying newer human state."""
    ex, _, _ = executor_with_sink(tmp_path)
    checkpoint = make_checkpoint()
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id="plan-1")
    ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.APPROVE, revision=1),
    )
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.PASSED
    # an in-flight stale round-1 CANCEL arrives after the APPROVE landed
    stale = human_record(HumanDecision.CANCEL, revision=1)
    with pytest.raises(GateTransitionError):
        ex.apply_human_decision(checkpoint, stale)
    record = checkpoint.gate_records[G4_GATE_ID]
    assert record.status == GateStatus.PASSED        # unchanged
    assert record.closed_by == "human:leon"
    assert not any(":CANCEL" in r for r in record.evidence_refs)


# ---------------------------------------------------------------------------
# 10. Learnings: deliberate promotion only
# ---------------------------------------------------------------------------


def test_learning_promotion_is_deliberate_only(tmp_path):
    store = tmp_path / "learnings.md"

    # nothing lands without the explicit, reasoned promotion path
    with pytest.raises(ValueError, match="rationale"):
        promote_learning(store, "x", source="s", provenance="p", rationale="")
    with pytest.raises(ValueError, match="rationale"):
        promote_learning(store, "x", source="s", provenance="p", rationale="   ")
    with pytest.raises(ValueError, match="learning"):
        promote_learning(store, "", source="s", provenance="p", rationale="why")
    assert not store.exists()  # failed promotions wrote NOTHING

    # a feedback draft is inert: proposing does not persist
    request = HumanPlannerInputRequest(
        request_id="hpir-1", plan_id="plan-1", checkpoint_id="cp-1",
        gate_id=G4_GATE_ID, feedback="spacing was wrong twice",
        decision=HumanDecision.NEEDS_CHANGES,
        created_at="2026-09-28T12:00:00+00:00",
    )
    draft = propose_learning_from_feedback(request)
    assert "spacing was wrong twice" in draft
    assert not store.exists()

    # explicit promotion lands with date + source + rationale + provenance
    promote_learning(
        store, draft,
        source="hpir-1",
        provenance="g4 decision rev1 (human:leon)",
        rationale="recurring human feedback -> durable design lesson",
    )
    entries = read_learnings(store)
    assert len(entries) == 1
    assert entries[0]["source"] == "hpir-1"
    assert entries[0]["provenance"].startswith("g4 decision rev1")
    assert "recurring human feedback" in entries[0]["rationale"]
    assert entries[0]["learning"] == draft
    # still valid for the existing `##`-topic reader contract
    assert store.read_text(encoding="utf-8").startswith("## learning ")


# ---------------------------------------------------------------------------
# 11. Viewer renders G1–G4 accurately; malformed/missing state fails closed
# ---------------------------------------------------------------------------


def _mixed_plan() -> CheckpointPlan:
    """Fixture plan: G1 PASSED, G2 NOT_APPLICABLE, G3 REPAIR, G4 WAITING."""
    checkpoint = make_checkpoint("cp-9")
    engine = GateEngine(checkpoint)
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    engine.advance(
        "G1_behavior",
        GateStatus.PASSED,
        evidence=_sufficient_g1(),
        actor="gatekeeper",
    )
    engine.advance("G3_adversarial", GateStatus.ACTIVE)
    engine.advance("G3_adversarial", GateStatus.REPAIR, actor="gatekeeper")
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE)
    engine.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN)
    cp = checkpoint.model_copy(deep=True)
    cp.gate_records = dict(engine.records)
    return CheckpointPlan(
        plan_id="plan-view",
        feature_id="p11",
        objective="viewer fixture",
        planner=PlannerProvenance(provider="test"),
        checkpoints=[cp],
        status="EXECUTING",
    )


def _sufficient_g1():
    from app.orchestration.checkpoint_planning.gate_evidence import (
        GateExecutionRecord,
    )

    return GateExecutionRecord(
        plan_id="plan-view", checkpoint_id="cp-9", gate_id="G1_behavior",
        attempt=1, requirements={"required": True},
        deterministic_checks=[{"name": "red", "ok": True, "detail": ""}],
        red_evidence={"ok": True, "returncode": 1},
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_decision="ACCEPT",
    )


def test_viewer_renders_mixed_gate_states(tmp_path):
    plan = _mixed_plan()
    plan_dir = tmp_path / "plans" / plan.plan_id
    plan_dir.mkdir(parents=True)
    (plan_dir / "checkpoint_plan.json").write_text(
        json.dumps(plan.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    out = view_checkpoint(tmp_path, plan.plan_id)
    assert "PLAN plan-view" in out
    assert "CHECKPOINT cp-9" in out
    assert "G1_behavior" in out and "PASSED" in out
    assert "NOT_APPLICABLE" in out
    assert "G3_adversarial" in out and "REPAIR" in out
    assert "WAITING_FOR_HUMAN" in out
    assert "AWAITING YOUR DECISION" in out
    # the three legal decisions and their effects are spelled out
    for marker in ("APPROVE", "NEEDS_CHANGES", "CANCEL", "REJECTED"):
        assert marker in out
    assert "evidence:" in out  # per-gate evidence summary line


def test_viewer_cli_exits_nonzero_on_missing_state(tmp_path):
    code = checkpoint_main(
        ["view", "--plan-id", "nope", "--state-root", str(tmp_path)]
    )
    assert code != 0


def test_viewer_cli_exits_nonzero_on_malformed_plan(tmp_path):
    plan_dir = tmp_path / "plans" / "broken"
    plan_dir.mkdir(parents=True)
    (plan_dir / "checkpoint_plan.json").write_text("{not json!!", encoding="utf-8")
    code = checkpoint_main(
        ["view", "--plan-id", "broken", "--state-root", str(tmp_path)]
    )
    assert code != 0
    with pytest.raises(CheckpointViewerError):
        view_checkpoint(tmp_path, "broken")


# ---------------------------------------------------------------------------
# 12. Malformed human actions fail closed
# ---------------------------------------------------------------------------


def test_malformed_human_actions_fail_closed(tmp_path):
    checkpoint, engine = parked_checkpoint()

    # wrong gate kind (a machine gate can't take a human decision)
    with pytest.raises(GateTransitionError, match="only valid for"):
        engine.submit_human_decision(
            "G1_behavior", human_record(HumanDecision.APPROVE)
        )

    # wrong current status (gate not parked)
    cp2 = make_checkpoint("cp-2")
    engine2 = GateEngine(cp2)
    engine2.advance(G4_GATE_ID, GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError, match="not WAITING_FOR_HUMAN"):
        engine2.submit_human_decision(G4_GATE_ID, human_record(
            HumanDecision.APPROVE, cid="cp-2"))

    # unknown gate
    with pytest.raises(GateTransitionError, match="unknown gate"):
        engine.submit_human_decision("G7_ghost", human_record(HumanDecision.APPROVE))

    # checkpoint mismatch
    with pytest.raises(GateTransitionError, match="different checkpoint"):
        engine.submit_human_decision(
            G4_GATE_ID, human_record(HumanDecision.APPROVE, cid="cp-other")
        )

    # bad decision value: rejected at the schema boundary (fail closed)
    with pytest.raises(ValidationError):
        HumanGateRecord(
            plan_id="plan-1", checkpoint_id="cp-1", gate_id=G4_GATE_ID,
            decision="SHIP_IT", feedback="",
            submitted_at="2026-09-28T12:00:00+00:00", actor="human:leon",
        )

    # missing feedback on NEEDS_CHANGES: rejected at the schema boundary…
    with pytest.raises(ValidationError):
        human_record(HumanDecision.NEEDS_CHANGES, feedback="   ")
    # …and again defensively in the engine (bypassed model instance)
    bypass = human_record(HumanDecision.NEEDS_CHANGES, feedback="x")
    object.__setattr__(bypass, "feedback", "")  # simulate a corrupt/legacy record
    with pytest.raises(GateTransitionError, match="without verbatim feedback"):
        engine.submit_human_decision(G4_GATE_ID, bypass)

    # nothing above changed the gate: still parked, still round 1
    assert engine.record(G4_GATE_ID).status == GateStatus.WAITING_FOR_HUMAN
    assert engine.record(G4_GATE_ID).evidence_refs == []
