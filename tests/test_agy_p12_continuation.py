"""P12 — deterministic tests: human-feedback continuation wiring.

Ten numbered invariants (per the P12 spec); fake planner, temp state
roots, no network, zero live model calls:

  1. human feedback never routes directly to worker execution
  2. one request is consumed at most once (idempotent re-run)
  3. planner failure preserves request + verbatim feedback (FAILED)
  4. schema-invalid planner result cannot resume runtime (FAILED, no doc)
  5. valid continuation passes the EXISTING plan validation pipeline
  6. resume surface (pending_continuations) is empty until CONSUMED
  7. G4 approval still requires the human path (machine still blocked)
  8. (CLI cases live in tests/test_agy_p12_cli.py)
  9. NEEDS_CHANGES parks (REPAIR + PENDING request) BEFORE consumption
 10. no model loop waits for human activity (no sleeps/polling)
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.g4_executor import (
    G4_GATE_ID,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.planner_input_consumer import (
    CanonicalPlanMissingError,
    PlannerInputConsumer,
    continuations_dir,
    pending_continuations,
    requests_dir,
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
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records

PLAN_ID = "plan-12"
CP_ID = "cp-1"
FEEDBACK = "button label wrong — keep the “Cancel” action verbatim"


# ---------------------------------------------------------------------------
# fixtures / helpers (P11 recipe: seed_gate_records + tiny canonical plan)
# ---------------------------------------------------------------------------


def _expectations(required: bool = True, reason: str | None = None) -> GateExpectations:
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=["tests/test_x.py"] if required else [],
    )


def make_checkpoint(cid: str = CP_ID) -> Checkpoint:
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
    revision: int = 1,
    feedback: str = "",
) -> HumanGateRecord:
    return HumanGateRecord(
        plan_id=PLAN_ID,
        checkpoint_id=CP_ID,
        gate_id=G4_GATE_ID,
        decision=decision,
        feedback=feedback,
        submitted_at="2026-09-29T12:00:00+00:00",
        actor="human:leon",
        provenance="cli",
        revision=revision,
    )


def make_plan_doc_checkpoint(status: GateStatus = GateStatus.REPAIR) -> Checkpoint:
    """Checkpoint whose seeded G4 record carries the given parked status
    (shape submission writes: seeded gate_records + closed_by)."""
    checkpoint = make_checkpoint()
    record = checkpoint.gate_records[G4_GATE_ID]
    record.status = status
    record.closed_by = "human:leon"
    return checkpoint


def canonical_plan_doc(status: GateStatus = GateStatus.REPAIR) -> CheckpointPlan:
    return CheckpointPlan(
        plan_id=PLAN_ID,
        feature_id="p12",
        objective="continuation fixture",
        planner=PlannerProvenance(role="cloud", provider="gemini"),
        checkpoints=[make_plan_doc_checkpoint(status)],
        status="SUBMITTED",
        revision=1,
    )


def write_canonical_plan(state_root: Path, plan: CheckpointPlan | None = None) -> Path:
    plan = plan or canonical_plan_doc()
    plan_dir = state_root / "plans" / plan.plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    path = plan_dir / "checkpoint_plan.json"
    path.write_text(
        json.dumps(plan.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    return path


def park_and_request(tmp_path: Path) -> tuple[Path, Checkpoint]:
    """P11 half: park G4, apply NEEDS_CHANGES via the LIVE executor so
    the PENDING planner-input request lands on disk; write the canonical
    plan doc. Returns (state_root, mutated in-memory checkpoint)."""
    state_root = tmp_path / "state"
    checkpoint = make_checkpoint()
    ex = build_live_g4_executor(state_root)
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id=PLAN_ID)
    ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.NEEDS_CHANGES, feedback=FEEDBACK),
    )
    write_canonical_plan(state_root)
    return state_root, checkpoint


class FakePlanner:
    """Injected CheckpointPlanner (P8 surface). No network, ever."""

    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls: list[dict] = []

    def create_plan(self, **kwargs) -> CheckpointPlan:  # pragma: no cover
        raise AssertionError("consumer must use revise_plan, never create_plan")

    def revise_plan(
        self, *, plan: CheckpointPlan, evidence: dict, reason: str
    ) -> CheckpointPlan:
        self.calls.append({"plan": plan, "evidence": evidence, "reason": reason})
        if self.mode == "raise":
            raise RuntimeError("planner exploded")
        if self.mode == "invalid_schema":
            return {"plan_id": "not-a-plan"}  # type: ignore[return-value]
        if self.mode == "invalid_domain":
            broken = plan.model_copy(deep=True)
            broken.checkpoints[0].gates.G1_behavior.planned_tests = []
            broken.revision += 1
            return broken
        continuation = plan.model_copy(deep=True)
        continuation.revision += 1
        return continuation


def consumer(state_root: Path, mode: str = "valid", clock=None) -> tuple[PlannerInputConsumer, FakePlanner]:
    planner = FakePlanner(mode)
    kwargs = {"clock": clock} if clock else {}
    return PlannerInputConsumer(state_root, planner=planner, **kwargs), planner


def request_files(state_root: Path) -> list[Path]:
    return sorted(requests_dir(state_root, PLAN_ID, CP_ID).glob("*.json"))


def request_doc(state_root: Path) -> dict:
    files = request_files(state_root)
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def continuation_files(state_root: Path) -> list[Path]:
    return sorted(continuations_dir(state_root, PLAN_ID).glob("*.json"))


# ---------------------------------------------------------------------------
# 1. Human feedback NEVER routes directly to worker execution
# ---------------------------------------------------------------------------


def test_feedback_never_routes_to_worker(tmp_path, monkeypatch):
    state_root, _ = park_and_request(tmp_path)

    from app.orchestration.checkpoint_planning import g1_executor as g1mod

    dispatched: list = []

    def _never(*args, **kwargs):
        dispatched.append((args, kwargs))
        raise AssertionError("P12 must never dispatch a worker")

    monkeypatch.setattr(g1mod.G1Executor, "execute_g1", _never)

    consumer_, planner = consumer(state_root)
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "CONSUMED"

    assert dispatched == []                                   # no worker ran
    assert len(planner.calls) == 1
    call = planner.calls[0]
    # human text travels as QUOTED DATA inside evidence, never in the
    # instruction channel (reason):
    assert call["evidence"]["human_feedback_verbatim"] == FEEDBACK
    assert FEEDBACK not in call["reason"]
    # the ONLY artifacts consumption produced are the updated request +
    # the continuation JSON (nothing else appeared under the state root):
    names = sorted(
        str(p.relative_to(state_root)) for p in state_root.rglob("*.json")
    )
    assert names == [
        f"plans/{PLAN_ID}/checkpoint_plan.json",
        f"plans/{PLAN_ID}/continuations/hpir-{CP_ID}-1.json",
        f"runs/{PLAN_ID}/human/{CP_ID}/decisions/1.json",
        f"runs/{PLAN_ID}/human/{CP_ID}/planner_inputs/hpir-{CP_ID}-1.json",
    ]


# ---------------------------------------------------------------------------
# 2. One request is consumed at most once
# ---------------------------------------------------------------------------


def test_double_consume_is_idempotent(tmp_path):
    state_root, _ = park_and_request(tmp_path)
    consumer_, planner = consumer(state_root)

    first = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert first["status"] == "CONSUMED"
    second = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert second["consumed"] is None
    assert len(planner.calls) == 1                     # planner asked ONCE
    doc = request_doc(state_root)
    assert doc["status"] == "CONSUMED"                 # exactly once, terminal
    assert len(continuation_files(state_root)) == 1


# ---------------------------------------------------------------------------
# 3. Planner failure preserves request + feedback verbatim
# ---------------------------------------------------------------------------


def test_planner_failure_preserves_feedback(tmp_path):
    state_root, _ = park_and_request(tmp_path)
    consumer_, _ = consumer(state_root, mode="raise")
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "FAILED"
    assert "planner exploded" in result["reason"]
    doc = request_doc(state_root)
    assert doc["status"] == "FAILED"
    assert doc["feedback"] == FEEDBACK                 # VERBATIM, never lost
    assert doc["failed_at"]
    assert continuation_files(state_root) == []


# ---------------------------------------------------------------------------
# 4. Invalid planner result cannot resume the runtime
# ---------------------------------------------------------------------------


def test_schema_invalid_continuation_is_failed(tmp_path):
    state_root, _ = park_and_request(tmp_path)
    consumer_, _ = consumer(state_root, mode="invalid_schema")
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "FAILED"
    assert request_doc(state_root)["status"] == "FAILED"
    assert continuation_files(state_root) == []        # nothing to resume
    assert pending_continuations(state_root, PLAN_ID) == []


def test_domain_invalid_continuation_is_failed(tmp_path):
    state_root, _ = park_and_request(tmp_path)
    consumer_, _ = consumer(state_root, mode="invalid_domain")
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "FAILED"
    assert "planned_tests" in result["reason"]         # validator error surfaced
    assert continuation_files(state_root) == []


# ---------------------------------------------------------------------------
# 5. Valid continuation passes the EXISTING validation pipeline
# ---------------------------------------------------------------------------


def test_valid_continuation_survives_existing_validation(tmp_path):
    from app.orchestration.checkpoint_planning.validation import (
        validate_checkpoint_plan,
    )

    state_root, _ = park_and_request(tmp_path)
    consumer_, _ = consumer(state_root)
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "CONSUMED"

    doc = json.loads(continuation_files(state_root)[0].read_text(encoding="utf-8"))
    # persisted through the canonical schema + validator:
    continuation = CheckpointPlan.model_validate(doc["continuation"])
    assert validate_checkpoint_plan(continuation).ok
    assert continuation.revision == 2
    # human provenance carried alongside:
    prov = doc["human_provenance"]
    assert prov["source_request_id"] == f"hpir-{CP_ID}-1"
    assert prov["feedback"] == FEEDBACK                # verbatim
    assert prov["actor"] == "human:leon"               # from G4 closed_by
    request = request_doc(state_root)
    assert request["status"] == "CONSUMED"
    assert request["continuation_plan_id"] == continuation.plan_id
    assert request["continuation_revision"] == continuation.revision


def test_missing_canonical_plan_fails_closed(tmp_path):
    state_root = tmp_path / "state"
    checkpoint = make_checkpoint()
    ex = build_live_g4_executor(state_root)
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id=PLAN_ID)
    ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.NEEDS_CHANGES, feedback=FEEDBACK),
    )
    # deliberately NO plans/<id>/checkpoint_plan.json written
    consumer_, planner = consumer(state_root)
    with pytest.raises(CanonicalPlanMissingError):
        consumer_.consume_pending(PLAN_ID, CP_ID)
    assert planner.calls == []                         # never fabricated state
    assert request_doc(state_root)["status"] == "PENDING"  # claim untouched


# ---------------------------------------------------------------------------
# 6. Resume only after validated continuation state exists
# ---------------------------------------------------------------------------


def test_resume_surface_tracks_consumption(tmp_path):
    state_root, _ = park_and_request(tmp_path)
    assert pending_continuations(state_root, PLAN_ID) == []   # nothing yet
    consumer_, _ = consumer(state_root)
    consumer_.consume_pending(PLAN_ID, CP_ID)
    pend = pending_continuations(state_root, PLAN_ID)
    assert len(pend) == 1
    assert pend[0]["human_provenance"]["feedback"] == FEEDBACK


# ---------------------------------------------------------------------------
# 7. G4 approval still requires the human path (P11 contract preserved)
# ---------------------------------------------------------------------------


def test_human_path_still_the_only_way_to_pass_g4():
    checkpoint = make_checkpoint("cp-7")
    engine = GateEngine(checkpoint)
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE, actor="executor")
    # machine still hard-blocked from stamping G4 PASSED:
    with pytest.raises(GateTransitionError, match="human authority"):
        engine.advance(G4_GATE_ID, GateStatus.PASSED)
    engine.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN, actor="executor")
    with pytest.raises(GateTransitionError, match="submit_human_decision"):
        engine.advance(G4_GATE_ID, GateStatus.PASSED)
    # only the human decision closes it:
    records = engine.submit_human_decision(
        G4_GATE_ID,
        HumanGateRecord(
            plan_id=PLAN_ID, checkpoint_id="cp-7", gate_id=G4_GATE_ID,
            decision=HumanDecision.APPROVE,
            submitted_at="2026-09-29T12:00:00+00:00", actor="human:leon",
            revision=1,
        ),
    )
    assert records[G4_GATE_ID].status == GateStatus.PASSED


# ---------------------------------------------------------------------------
# 9. NEEDS_CHANGES parks BEFORE any planner consumption
# ---------------------------------------------------------------------------


def test_needs_changes_parks_before_planner(tmp_path):
    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        view_checkpoint,
    )

    state_root = tmp_path / "state"
    checkpoint = make_checkpoint()
    ex = build_live_g4_executor(state_root)
    ex.open(checkpoint)
    ex.wait(checkpoint, plan_id=PLAN_ID)
    ex.apply_human_decision(
        checkpoint,
        human_record(HumanDecision.NEEDS_CHANGES, feedback=FEEDBACK),
    )
    # BEFORE any consumer exists/runs: gate REPAIR, request PENDING on disk
    assert checkpoint.gate_records[G4_GATE_ID].status == GateStatus.REPAIR
    assert request_doc(state_root)["status"] == "PENDING"

    write_canonical_plan(state_root)
    # viewer (P12-C) surfaces the planner-replan state for a repaired gate
    rendered = view_checkpoint(state_root, PLAN_ID)
    assert "planner replan: PENDING" in rendered
    assert f"hpir-{CP_ID}-1" in rendered

    consumer_, _ = consumer(state_root)
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "CONSUMED"
    assert request_doc(state_root)["status"] == "CONSUMED"
    assert "planner replan: CONSUMED" in view_checkpoint(state_root, PLAN_ID)


# ---------------------------------------------------------------------------
# 10. No model loop waits for human activity
# ---------------------------------------------------------------------------


def test_consumer_has_no_sleep_or_polling_pattern(tmp_path):
    import app.orchestration.checkpoint_planning.planner_input_consumer as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    for banned in ("time.sleep", "sleep(", "while True", "poll(", "select("):
        assert banned not in source, f"consumer must not wait/poll: {banned!r}"
    # fully deterministic seam: injected clock, synchronous signature
    sig = inspect.signature(PlannerInputConsumer.__init__)
    assert "clock" in sig.parameters
    assert "planner" in sig.parameters
    assert not inspect.iscoroutinefunction(PlannerInputConsumer.consume_pending)

    # a deterministic run consumes end-to-end with a fixed injected clock
    # (no wait/advance exists anywhere — the timestamps are pure stamps):
    state_root, _ = park_and_request(tmp_path)

    planner = FakePlanner("valid")
    consumer_ = PlannerInputConsumer(
        state_root, planner=planner, clock=lambda: 1_730_000_000.0
    )
    result = consumer_.consume_pending(PLAN_ID, CP_ID)
    assert result["status"] == "CONSUMED"
    assert request_doc(state_root)["consumed_at"].startswith("2024-")
