"""P16 — Authority bridge: mutation classifier / lowering / dispatch guard.

P14 is ORCHESTRATION. The checkpoint runtime is AUTHORITY. These tests
prove, with ZERO live model calls, zero network and tmp state roots:

 1. read-only batches dispatch directly (unchanged P14 mechanics);
 2. mutating batches cannot be dispatched directly — the choke-point
    guard (BatchDispatcher.run_batch) refuses before anything happens;
 3. mutating delegations lower into ONE canonical, VALID CheckpointPlan
    intent (correct gate applicability, human-owned G0);
 4. invalid lowering fails closed (no acceptance verification, unknown
    role, mutating-subgraph cycle);
 5. submitted lowered plans get FRESH gate records (PENDING/NOT_APPLICABLE);
 6. planner-claimed PASSED gate records are discarded at submission;
 7. mutating work cannot be accepted before gates (engine refusals);
 8. the direct path never spawns mutating fixers (max_fixers=0 routing);
 9. G4 remains human-only even for lowered batch plans (surface guard +
    engine rule);
10. a cloud FINAL cannot accept an unfinished lowered mutation — the
    lowered checkpoint plan survives project completion untouched;
11-14. routing/resume mechanics: mixed batches route, held read-only
    dependents wait, resume never re-lowers or re-executes, and a
    persisted CHECKPOINT_ROUTED outcome survives dispatcher resume.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.orchestration.cloud_batch.dispatcher as dispatcher_module
from app.orchestration.checkpoint_planning.delegation_policy import TaskStatus
from app.orchestration.checkpoint_planning.g4_executor import (
    G4_GATE_ID,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.gate_evidence import GateExecutionRecord
from app.orchestration.checkpoint_planning.schemas import (
    G0Approval,
    CheckpointPlan,
    GateStatus,
)
from app.orchestration.checkpoint_planning.submission import (
    seed_gate_records,
    submit_checkpoint_plan,
)
from app.orchestration.checkpoint_planning.validation import (
    validate_checkpoint_plan,
)
from app.orchestration.cloud_batch import (
    ROLE_CLASSIFICATION,
    ROUTED_STATUS,
    BatchDispatcher,
    CloudProtocol,
    LoweringError,
    MutationClass,
    MutationClassificationError,
    ProjectStore,
    WorkerRunResult,
    classify_delegation,
    dispatch_batch,
    lower_batch_to_checkpoint,
    schemas,
)
from app.human_surface import HumanAuthorityError, apply_g4_decision

PROJECT = "p16-proj"
BATCH = "batch-001"
BASE = "a" * 40
NOW = "2026-09-30T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def dspec(rid: str, role: str, **over) -> schemas.DelegationSpec:
    defaults = dict(
        id=rid,
        role=role,
        objective=f"{role} work for {rid}",
        owned_files=[f"{rid}.py"],
        verification=[f"pytest -q tests/test_{rid}.py"],
    )
    defaults.update(over)
    return schemas.DelegationSpec(**defaults)


def ro_plan(**over) -> schemas.BatchPlan:
    """All-read-only two-delegation batch (direct path)."""
    defaults = dict(
        batch_id=BATCH,
        objective="investigate and review",
        delegations=[dspec("r1", "codebase-investigator"), dspec("r2", "ui-reviewer")],
    )
    defaults.update(over)
    return schemas.BatchPlan(**defaults)


def mixed_plan(**over) -> schemas.BatchPlan:
    """One read-only + two mutating delegations (m2 depends on m1)."""
    defaults = dict(
        batch_id=BATCH,
        objective="investigate then build the thing",
        delegations=[
            dspec("r1", "codebase-investigator"),
            dspec("m1", "implementer", objective="implement the core module",
                  invariants=["scope guard: never widen owned files"]),
            dspec("m2", "test-author", objective="write tests for core", deps=["m1"]),
        ],
    )
    defaults.update(over)
    return schemas.BatchPlan(**defaults)


def make_store(tmp_path: Path, plan: schemas.BatchPlan | None = None) -> ProjectStore:
    store = ProjectStore(tmp_path)
    store.create_project(
        schemas.ProjectContract(
            project_id=PROJECT,
            initial_prompt="Build the thing, please.",
            objective="build the thing",
            base_sha=BASE,
        )
    )
    if plan is not None:
        store.save_batch_plan(PROJECT, plan.batch_id, plan)
    return store


class FakeRunner:
    """Counts every run; returns the canned result (default VERIFIED)."""

    def __init__(self, results: dict[str, WorkerRunResult] | None = None) -> None:
        self.results = results or {}
        self.started: list[str] = []

    def run(self, task, *, workdir: str) -> WorkerRunResult:
        self.started.append(task.id)
        out = self.results.get(task.id)
        if out is not None:
            return out
        return WorkerRunResult(status=TaskStatus.VERIFIED.value, conclusion=f"{task.id} done")


class FakeCloudPlanner:
    """Cloud planner seam (zero live calls): canned clarify/batch/reentry."""

    def __init__(self, batch_action=None, reentry_actions=None) -> None:
        self.batch_action = batch_action
        self.reentry_actions = list(reentry_actions or [])
        self.calls: list[str] = []

    def clarify(self, prompt):
        self.calls.append("clarify")
        return schemas.ClarificationRequest(questions=["scope?"])

    def batch(self, prompt):
        self.calls.append("batch")
        assert self.batch_action is not None
        return self.batch_action

    def reentry(self, prompt):
        self.calls.append("reentry")
        action, self.reentry_actions = self.reentry_actions[0], self.reentry_actions[1:]
        return action


def run_protocol_to_running(tmp_path: Path, plan: schemas.BatchPlan):
    """NEW -> CLARIFY -> answer -> BATCH (state BATCH_RUNNING, plan stored)."""
    store = make_store(tmp_path)
    planner = FakeCloudPlanner(
        batch_action=plan,
        reentry_actions=[schemas.FinalResult(success=True, summary="done")],
    )
    protocol = CloudProtocol(store, PROJECT, planner)
    protocol.request_clarify()
    protocol.record_user_answer("build it")
    protocol.request_batch()
    return store, protocol, planner


def plan_doc_path(state_root: Path, plan_id: str) -> Path:
    return state_root / "plans" / plan_id / "checkpoint_plan.json"


def sufficient(plan_id: str, checkpoint_id: str, gate_id: str) -> GateExecutionRecord:
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


def lower_mixed(project_id: str = PROJECT, batch_id: str = BATCH) -> dict:
    return lower_batch_to_checkpoint(
        project_id=project_id, batch=mixed_plan(batch_id=batch_id), base_sha=BASE
    )


def submit_lowered(record: dict, state_root: Path):
    """Human-owned G0 APPROVE + canonical submission of a lowered intent."""
    plan = record["checkpoint_plan"]
    if not isinstance(plan, CheckpointPlan):
        plan = CheckpointPlan.model_validate(plan)
    result = submit_checkpoint_plan(
        plan,
        state_root=state_root,
        g0_approval=G0Approval(
            decision="APPROVE", revision=plan.revision, approver="human:cli",
            approved_at=NOW,
        ),
    )
    assert result.accepted, result.errors
    return result


# ---------------------------------------------------------------------------
# 1-2: the guard at the real dispatch choke point
# ---------------------------------------------------------------------------


def test_read_only_batch_dispatches_directly(tmp_path):
    """All-read-only batch -> the direct path runs it; nothing lowers."""
    store = make_store(tmp_path, ro_plan())
    runner = FakeRunner()
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=runner, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp == {"routed": False, "reason": "ALL_REQUIRED_DELEGATIONS_COMPLETE", "lowered": None}
    assert sorted(runner.started) == ["r1", "r2"]
    # No lowering occurred, no mutation-related state exists.
    assert store.load_lowered_intent(PROJECT, BATCH) == {}
    assert not store.authority_dir(PROJECT).exists()
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    assert all(wo.status != ROUTED_STATUS for wo in outcomes.values())
    assert {k: v.status for k, v in outcomes.items()} == {"r1": "VERIFIED", "r2": "VERIFIED"}


def test_mutating_batch_cannot_dispatch_directly(tmp_path):
    """run_batch is the choke point: any mutating delegation raises BEFORE
    anything else happens (no runner calls, no state writes)."""
    store = make_store(tmp_path, mixed_plan())
    runner = FakeRunner()
    dispatcher = BatchDispatcher(store, PROJECT, runner=runner)
    with pytest.raises(MutationClassificationError, match=r"m1\(implementer\)") as exc:
        dispatcher.run_batch(BATCH)
    msg = str(exc.value)
    assert "m2(test-author)" in msg  # every offending id/role is named
    assert runner.started == []
    assert store.load_worker_outcomes(PROJECT, BATCH) == {}
    assert store.load_lowered_intent(PROJECT, BATCH) == {}
    bdir = store.batch_dir(PROJECT, BATCH)
    doc_files = sorted(p.name for p in bdir.iterdir())
    assert doc_files == ["plan.json", "workers"]  # nothing else was written
    assert sorted(p.name for p in (bdir / "workers").iterdir()) == []
    assert not (bdir / "outcome.json").exists()
    assert not (bdir / "reentry.json").exists()


# ---------------------------------------------------------------------------
# 3-4: lowering correctness + fail-closed
# ---------------------------------------------------------------------------


def test_mutating_lowering_produces_valid_checkpoint_intent(tmp_path):
    """The lowering is one canonical, VALID CheckpointPlan intent: exact
    gate applicability, planned_tests, topo deps order, human-owned G0."""
    record = lower_mixed()
    plan_id = f"p16-{PROJECT}-{BATCH}"
    assert record["plan_id"] == plan_id
    assert record["batch_id"] == BATCH
    assert record["project_id"] == PROJECT
    assert record["base_sha"] == BASE
    assert record["read_only"] == ["r1"]
    assert record["mutating"] == ["m1", "m2"]
    assert record["held_ids"] == []

    cp_plan = record["checkpoint_plan"]
    assert isinstance(cp_plan, CheckpointPlan)
    verdict = validate_checkpoint_plan(cp_plan)
    assert verdict.ok, verdict.errors
    assert cp_plan.plan_id == plan_id
    assert cp_plan.feature_id == BATCH
    assert cp_plan.g0 is None  # approval is human-owned, supplied at submit
    assert cp_plan.planner.provider == "gemini"
    assert cp_plan.planner.role == "cloud"
    assert cp_plan.planner.fallback_used is False

    # ONE checkpoint per mutating delegation, in topological order.
    assert [cp.checkpoint_id for cp in cp_plan.checkpoints] == ["m1", "m2"]
    by_id = {cp.checkpoint_id: cp for cp in cp_plan.checkpoints}
    m1 = by_id["m1"]
    assert m1.title == "implement the core module"[:80]
    assert m1.objective == "implement the core module"  # verbatim
    assert m1.dependencies == []
    assert m1.relevant_files == ["m1.py"]
    assert m1.stages == ["m1"]
    g1 = m1.gates.G1_behavior
    assert g1.required is True
    assert g1.planned_tests == ["pytest -q tests/test_m1.py"]  # = spec.verification
    g2 = m1.gates.G2_ui
    assert g2.required is False  # correct applicability, never forced
    assert "not forced" in (g2.reason or "")
    g3 = m1.gates.G3_adversarial
    assert g3.required is True
    assert g3.focus == ["scope guard: never widen owned files"]  # = spec.invariants
    g4 = m1.gates.G4_human
    assert g4.required is True
    assert "human acceptance" in (g4.reason or "")

    # deps restricted to the mutating subgraph, in order.
    assert by_id["m2"].dependencies == ["m1"]
    # Deterministic plain-text context, no raw transcripts.
    assert "investigate then build the thing" in m1.implementation_context
    assert "scope guard: never widen owned files" in m1.implementation_context
    assert "transcript" not in m1.implementation_context.lower()


def test_invalid_lowering_fails_closed(tmp_path):
    """No acceptance verification -> LoweringError; unknown role ->
    ValueError (never defaulted); mutating cycle -> LoweringError."""
    # (a) mutating delegation without verification cannot lower (no G1
    # acceptance criterion exists).
    bare = schemas.BatchPlan(
        batch_id=BATCH,
        objective="x",
        delegations=[dspec("m1", "implementer", verification=[])],
    )
    with pytest.raises(LoweringError, match="acceptance verification"):
        lower_batch_to_checkpoint(project_id=PROJECT, batch=bare, base_sha=BASE)

    # (b) unknown role fails closed at classification (bare namespace spec).
    with pytest.raises(ValueError, match="unknown role"):
        classify_delegation(SimpleNamespace(role="quantum-teleporter"))
    assert set(ROLE_CLASSIFICATION) == set(ROLE_TO_CLASSIFICATION_EXPECTED)
    assert classify_delegation(SimpleNamespace(role="ui-reviewer")) is MutationClass.READ_ONLY

    # (c) a cycle in the mutating subgraph (hand-constructed specs bypass
    # the BatchPlan schema) is refused by the lowered intent.
    def ns_spec(i, deps):
        return SimpleNamespace(
            id=i, role="implementer", objective=f"obj {i}", owned_files=[f"{i}.py"],
            deps=list(deps), reads=[], invariants=[], verification=["pytest -q t.py"],
        )

    cyclic = SimpleNamespace(
        batch_id=BATCH,
        objective="cyclic",
        delegations=[ns_spec("c1", ["c2"]), ns_spec("c2", ["c1"])],
    )
    with pytest.raises(LoweringError, match="not orderable"):
        lower_batch_to_checkpoint(project_id=PROJECT, batch=cyclic, base_sha=BASE)


ROLE_TO_CLASSIFICATION_EXPECTED = [
    "codebase-investigator", "ui-reviewer", "adversarial-reviewer",
    "test-author", "implementer", "fixer",
]


# ---------------------------------------------------------------------------
# 5-7: the checkpoint runtime authorities on the lowered plan
# ---------------------------------------------------------------------------


def test_checkpoint_gate_records_are_fresh(tmp_path):
    """Submit (canonical path, human G0) -> SUBMITTED, every gate record
    freshly seeded; next_gate is the first checkpoint's G1."""
    record = lower_mixed()
    state_root = tmp_path / "state"
    submit_lowered(record, state_root)
    doc = json.loads(plan_doc_path(state_root, record["plan_id"]).read_text(encoding="utf-8"))
    assert doc["status"] == "SUBMITTED"
    assert doc["runtime_owner"] == "agy-local"
    assert doc["next_gate"] == "m1:G1_behavior"
    plan = CheckpointPlan.model_validate(doc)
    assert plan.g0 is not None
    assert plan.g0.decision == "APPROVE"
    assert plan.g0.approver == "human:cli"
    for cp in plan.checkpoints:
        for kind, rec in cp.gate_records.items():
            assert rec.status is GateStatus.PENDING, (cp.checkpoint_id, kind)
            expected = "NOT_APPLICABLE" if kind == "G2_ui" else "REQUIRED"
            assert rec.applicability == expected


def test_planner_cannot_pre_mark_gates(tmp_path):
    """Planner-claimed PASSED gate records are DISCARDED at submission —
    re-seeded PENDING through the canonical submission path."""
    record = lower_mixed()
    tampered = CheckpointPlan.model_validate(record["checkpoint_plan"].model_dump(mode="json"))
    for cp in tampered.checkpoints:
        seeded = seed_gate_records(cp)
        cp.gate_records = {
            kind: g.model_copy(
                update={"status": GateStatus.PASSED, "closed_by": "model:planner-claim", "closed_at": NOW}
            )
            for kind, g in seeded.items()
        }
    state_root = tmp_path / "state"
    submit_lowered({"checkpoint_plan": tampered, "plan_id": tampered.plan_id}, state_root)
    doc = json.loads(plan_doc_path(state_root, tampered.plan_id).read_text(encoding="utf-8"))
    plan = CheckpointPlan.model_validate(doc)
    for cp in plan.checkpoints:
        for kind, g in cp.gate_records.items():
            assert g.status is GateStatus.PENDING, (cp.checkpoint_id, kind)
            assert g.closed_by is None
            assert g.evidence_refs == []


def test_mutating_cannot_be_accepted_before_gates(tmp_path):
    """On the submitted lowered plan: wrong-gate evidence, fabricated
    evidence fields, skipped-ACTIVE, and any machine stamping of G4 are
    ALL refused by the canonical engine."""
    record = lower_mixed()
    state_root = tmp_path / "state"
    submit_lowered(record, state_root)
    doc = json.loads(plan_doc_path(state_root, record["plan_id"]).read_text(encoding="utf-8"))
    submitted = CheckpointPlan.model_validate(doc)
    cp = submitted.checkpoints[0]
    plan_id = submitted.plan_id

    engine = GateEngine(cp)
    # G1 -> PASSED straight from PENDING is illegal (must be ACTIVE first).
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=sufficient(plan_id, cp.checkpoint_id, "G1_behavior"))
    engine.advance("G1_behavior", GateStatus.ACTIVE)
    # Evidence for the WRONG gate id is refused.
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=sufficient(plan_id, cp.checkpoint_id, "G3_adversarial"))
    # A fabricated G1 record with mismatched fields is refused (the frozen
    # record is tampered exactly like an attacker would forge the JSON).
    fabricated = sufficient(plan_id, cp.checkpoint_id, "G1_behavior")
    object.__setattr__(fabricated, "supervisor_decision", "REPAIR")
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=fabricated)
    wrong_cp = sufficient(plan_id, "some-other-checkpoint", "G1_behavior")
    with pytest.raises(GateTransitionError):
        engine.advance("G1_behavior", GateStatus.PASSED, evidence=wrong_cp)

    # G4: NO non-human path may stamp it PASSED — even from ACTIVE with
    # fully sufficient G4 evidence (engine rule: only submit_human_decision).
    engine2 = GateEngine(cp)
    engine2.advance(G4_GATE_ID, GateStatus.ACTIVE)
    with pytest.raises(GateTransitionError, match="human authority"):
        engine2.advance(G4_GATE_ID, GateStatus.PASSED, evidence=sufficient(plan_id, cp.checkpoint_id, G4_GATE_ID))
    # Nothing advanced to a terminal state: the mutation stays unaccepted.
    assert not any(engine.is_passed(k) for k in ("G1_behavior", "G3_adversarial", G4_GATE_ID))


# ---------------------------------------------------------------------------
# 8: the direct path never spawns mutating fixers
# ---------------------------------------------------------------------------


def test_fixer_mutation_obey_authority_path(tmp_path, monkeypatch):
    """A FAILED read-only task escalates (never gains a mutating repair):
    no *-fix-* outcomes, BATCH_FAILED_WITH_ESCALATION, and the dispatcher
    built by dispatch_batch provably carries max_fixers=0."""
    build_kwargs: dict = {}
    real_cls = dispatcher_module.BatchDispatcher

    class SpyDispatcher(real_cls):
        def __init__(self, *args, **kwargs):
            build_kwargs.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(dispatcher_module, "BatchDispatcher", SpyDispatcher)

    store = make_store(tmp_path, ro_plan())
    runner = FakeRunner({"r2": WorkerRunResult(status="FAILED", conclusion="review crashed")})
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=runner, base_sha=BASE, workdir=str(tmp_path),
    )
    assert build_kwargs.get("max_fixers") == 0
    assert resp["routed"] is False
    assert resp["reason"] == "BATCH_FAILED_WITH_ESCALATION"
    assert sorted(runner.started) == ["r1", "r2"]  # only the plan's own ids ran
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    assert not any("-fix-" in tid for tid in outcomes)
    assert not any("-fix-" in tid for tid in runner.started)
    assert {k: v.status for k, v in outcomes.items()} == {"r1": "VERIFIED", "r2": "FAILED"}


# ---------------------------------------------------------------------------
# 9: G4 remains human-only on lowered batch plans
# ---------------------------------------------------------------------------


def _reflect_gate_records(state_root: Path, plan_id: str, cp) -> None:
    """Mirror what the CLI/surface does after an executor transition."""
    path = plan_doc_path(state_root, plan_id)
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = {k: r.model_dump(mode="json") for k, r in cp.gate_records.items()}
    for entry in doc["checkpoints"]:
        if entry["checkpoint_id"] == cp.checkpoint_id:
            entry["gate_records"] = records
    from app.orchestration.agy_runstate import _atomic_write_json

    _atomic_write_json(path, doc)


def test_g4_remains_human_only_via_lowering(tmp_path):
    """Lowered plan submitted -> park G4; machine attempts (surface guard
    + engine rule) leave it WAITING_FOR_HUMAN; only a human: record closes
    it."""
    record = lower_mixed()
    state_root = tmp_path / "state"
    submit_lowered(record, state_root)
    plan_id = record["plan_id"]

    doc = json.loads(plan_doc_path(state_root, plan_id).read_text(encoding="utf-8"))
    cp = CheckpointPlan.model_validate(doc).checkpoints[0]
    g4 = build_live_g4_executor(state_root)
    g4.open(cp)
    g4.wait(cp, plan_id=plan_id)
    _reflect_gate_records(state_root, plan_id, cp)

    # Machine attempt via the P15 surface: rejected by the provenance guard
    # (a non-human actor never even reaches the executor).
    with pytest.raises(HumanAuthorityError):
        apply_g4_decision(state_root, plan_id, cp.checkpoint_id, "approve", "", actor="model:emitter")
    # Machine attempt via the engine: refused while parked for the human.
    cp_now = CheckpointPlan.model_validate(
        json.loads(plan_doc_path(state_root, plan_id).read_text(encoding="utf-8"))
    ).checkpoints[0]
    engine = GateEngine(cp_now)
    assert engine.record(G4_GATE_ID).status is GateStatus.WAITING_FOR_HUMAN
    with pytest.raises(GateTransitionError):
        engine.advance(G4_GATE_ID, GateStatus.PASSED, evidence=sufficient(plan_id, cp.checkpoint_id, G4_GATE_ID))
    still = CheckpointPlan.model_validate(
        json.loads(plan_doc_path(state_root, plan_id).read_text(encoding="utf-8"))
    ).checkpoints[0]
    assert still.gate_records[G4_GATE_ID].status is GateStatus.WAITING_FOR_HUMAN

    # Only a human: actor through the surface closes it.
    result = apply_g4_decision(state_root, plan_id, cp.checkpoint_id, "approve", "", actor="human:cli")
    assert result["status"] == "PASSED"
    final = CheckpointPlan.model_validate(
        json.loads(plan_doc_path(state_root, plan_id).read_text(encoding="utf-8"))
    ).checkpoints[0]
    assert final.gate_records[G4_GATE_ID].status is GateStatus.PASSED
    assert final.gate_records[G4_GATE_ID].closed_by == "human:cli"


# ---------------------------------------------------------------------------
# 10: the cloud FINAL cannot accept an unfinished lowered mutation
# ---------------------------------------------------------------------------


def test_batch_final_cannot_accept_unfinished_mutation(tmp_path):
    """Routed batch -> cloud reentry says FINAL(success) -> project
    COMPLETE, yet the lowered mutation's checkpoint plan remains SUBMITTED
    with every gate PENDING: completion of the ORCHESTRATION does not
    accept the mutation — the AUTHORITY gates do."""
    plan = schemas.BatchPlan(
        batch_id=BATCH,
        objective="investigate and build",
        delegations=[
            dspec("r1", "codebase-investigator"),
            dspec("m1", "implementer", objective="implement the core module"),
        ],
    )
    store, protocol, planner = run_protocol_to_running(tmp_path, plan)

    runner = FakeRunner()
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=protocol,
        batch_id=BATCH, runner=runner, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp["routed"] is True
    assert runner.started == ["r1"]  # read-only part ran
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    assert outcomes["m1"].status == ROUTED_STATUS
    assert protocol.state() is schemas.CloudProjectState.REENTRY_READY

    # Human submits the lowered intent into the checkpoint state root.
    lowered = CheckpointPlan.model_validate(resp["lowered"]["checkpoint_plan"])
    state_root = tmp_path / "plans"
    result = submit_checkpoint_plan(
        lowered,
        state_root=state_root,
        g0_approval=G0Approval(
            decision="APPROVE", revision=lowered.revision,
            approver="human:cli", approved_at=NOW,
        ),
    )
    assert result.accepted, result.errors
    doc_path = plan_doc_path(state_root, lowered.plan_id)
    before = doc_path.read_bytes()

    # Cloud reentry declares FINAL success -> project COMPLETE.
    action = protocol.request_reentry(BATCH)
    assert isinstance(action, schemas.FinalResult) and action.success
    assert protocol.state() is schemas.CloudProjectState.COMPLETE

    # The lowered mutation was NOT accepted by the cloud: the canonical
    # checkpoint plan is byte-identical, SUBMITTED, all gates PENDING.
    assert doc_path.read_bytes() == before
    doc = json.loads(before.decode("utf-8"))
    assert doc["status"] == "SUBMITTED"
    for cp in doc["checkpoints"]:
        for kind, g in cp["gate_records"].items():
            assert g["status"] == "PENDING", (cp["checkpoint_id"], kind)
    assert lowered.g0 is None  # planner never carried a G0 of its own


# ---------------------------------------------------------------------------
# 11-14: routing + crash-resume mechanics
# ---------------------------------------------------------------------------


def test_mixed_batch_routes(tmp_path):
    """Read-only part runs; mutating lowered + routed outcomes persisted;
    reentry reason correct; the canonical plan.json is NEVER rewritten."""
    plan = mixed_plan()
    store, protocol, _ = run_protocol_to_running(tmp_path, plan)
    plan_path = store.batch_dir(PROJECT, BATCH) / "plan.json"
    before = plan_path.read_bytes()

    runner = FakeRunner()
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=protocol,
        batch_id=BATCH, runner=runner, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp["routed"] is True
    assert resp["reason"] == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    assert resp["lowered"]["plan_id"] == f"p16-{PROJECT}-{BATCH}"
    assert runner.started == ["r1"]

    rec = store.load_lowered_intent(PROJECT, BATCH)
    assert rec["mutating"] == ["m1", "m2"]
    assert CheckpointPlan.model_validate(rec["checkpoint_plan"])  # persists parseably
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    assert outcomes["r1"].status == "VERIFIED"
    for mid in ("m1", "m2"):
        assert outcomes[mid].status == ROUTED_STATUS
        assert f"p16-{PROJECT}-{BATCH}" in outcomes[mid].conclusion
        assert outcomes[mid].evidence_refs == ["authority/batch-001-lowering.json"]
    # Reentry was marked by the reduced dispatcher (not a re-plan).
    reentry = store.load_reentry(PROJECT, BATCH)
    assert reentry["reason"] == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    assert protocol.state() is schemas.CloudProjectState.REENTRY_READY
    # The reduction was an in-memory projection: canonical plan untouched.
    assert plan_path.read_bytes() == before


def test_held_read_only_depends_on_mutating(tmp_path):
    """A read-only delegation that transitively depends on a mutating id
    is held (NOT dispatched) — it awaits the checkpoint path; the next
    cloud batch re-plans it. With every read-only one held, nothing runs
    and the reentry mark comes from routing itself (step 4e)."""
    plan = mixed_plan(
        delegations=[
            dspec("m1", "implementer", objective="implement the core module"),
            dspec("r1", "ui-reviewer", deps=["m1"]),            # direct dep -> held
            dspec("r2", "adversarial-reviewer", deps=["r1"]),   # transitive dep -> held
        ],
        reentry=schemas.ReentryGate(required_ids=["r1", "r2", "m1"]),
    )
    store, protocol, _ = run_protocol_to_running(tmp_path, plan)
    runner = FakeRunner()
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=protocol,
        batch_id=BATCH, runner=runner, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp["routed"] is True
    assert runner.started == []  # held delegations never dispatch
    rec = store.load_lowered_intent(PROJECT, BATCH)
    assert sorted(rec["held_ids"]) == ["r1", "r2"]
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    # Held delegations are neither dispatched nor routed: no outcome.
    assert "r1" not in outcomes and "r2" not in outcomes
    assert outcomes["m1"].status == ROUTED_STATUS
    # R' empty -> dispatcher did not run; routing marked reentry itself.
    assert resp["reason"] == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    assert store.load_reentry(PROJECT, BATCH)["reason"] == (
        "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    )
    assert protocol.state() is schemas.CloudProjectState.REENTRY_READY


def test_all_mutating_batch_reentry_not_deadlocked(tmp_path):
    """Regression (review H-2): an ALL-MUTATING batch (R′ empty — the
    dispatcher never runs) must persist a BatchOutcome BEFORE marking
    reentry, because request_reentry reserves the generation's single
    cloud call and then reads outcome.json. Without the outcome, the
    reentry crashes with FileNotFoundError AFTER burning the call and the
    generation is permanently CloudInvocationBlocked. Here the reentry
    must complete: the cloud sees the CHECKPOINT_ROUTED workers, not a
    missing file."""
    plan = schemas.BatchPlan(
        batch_id=BATCH,
        objective="implement and test the core module",
        delegations=[
            dspec("m1", "implementer", objective="implement the core module"),
            dspec("m2", "test-author", objective="write tests for core", deps=["m1"]),
        ],
        reentry=schemas.ReentryGate(required_ids=["m1", "m2"]),
    )
    store, protocol, planner = run_protocol_to_running(tmp_path, plan)
    dispatch_batch(
        store=store, project_id=PROJECT, protocol=protocol,
        batch_id=BATCH, runner=FakeRunner(), base_sha=BASE, workdir=str(tmp_path),
    )
    assert protocol.state() is schemas.CloudProjectState.REENTRY_READY
    # The outcome must exist and carry the routed workers (honest routing).
    op = store.batch_dir(PROJECT, BATCH) / "outcome.json"
    assert op.exists()
    doc = json.loads(op.read_text(encoding="utf-8"))
    assert sorted(w["delegation_id"] for w in doc["workers"]) == ["m1", "m2"]
    assert all(w["status"] == ROUTED_STATUS for w in doc["workers"])
    assert doc["reentry_reason"] == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    # The reentry cloud call completes (no FileNotFoundError after the
    # reservation; no burned-call deadlock).
    protocol.request_reentry(BATCH)
    assert planner.calls.count("reentry") == 1
    assert protocol.state() is schemas.CloudProjectState.COMPLETE


def test_reissued_batch_id_with_different_content_fails_closed(tmp_path):
    """Regression (review H-6): a re-issued ``batch_id`` with DIFFERENT
    plan content must not inherit the stale lowered intent (it may have
    already been human-submitted and executed). The dispatch fails closed
    with LoweringError BEFORE any state write — the new mutation is
    neither silently dropped nor routed against a plan that never
    contained it; the stale record is untouched."""
    plan1 = schemas.BatchPlan(
        batch_id=BATCH,
        objective="gen1 implement module a",
        delegations=[
            dspec("m1", "implementer", objective="gen1 work", owned_files=["g1.py"]),
        ],
        reentry=schemas.ReentryGate(required_ids=["m1"]),
    )
    store = make_store(tmp_path, plan1)
    dispatch_batch(
        store=store, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=FakeRunner(), base_sha=BASE, workdir=str(tmp_path),
    )
    record_path = store.authority_dir(PROJECT) / f"{BATCH}-lowering.json"
    before = record_path.read_bytes()
    rec1 = store.load_lowered_intent(PROJECT, BATCH)
    assert rec1["mutating"] == ["m1"]

    # Re-issue the SAME batch id with entirely different mutating content.
    plan2 = schemas.BatchPlan(
        batch_id=BATCH,
        objective="gen2 totally different work",
        delegations=[
            dspec("m9", "implementer", objective="gen2 work", owned_files=["g9.py"]),
        ],
        reentry=schemas.ReentryGate(required_ids=["m9"]),
    )
    store.save_batch_plan(PROJECT, plan2.batch_id, plan2)  # overwrite plan.json
    with pytest.raises(LoweringError, match="content hash mismatch"):
        dispatch_batch(
            store=store, project_id=PROJECT, protocol=None,
            batch_id=BATCH, runner=FakeRunner(), base_sha=BASE, workdir=str(tmp_path),
        )
    # The stale intent is byte-identical (nothing inherited/overwritten).
    assert record_path.read_bytes() == before
    # The new mutation was never routed against the stale intent.
    assert "m9" not in store.load_worker_outcomes(PROJECT, BATCH)
    # Identical-content resume stays idempotent: restore the original plan
    # content and re-dispatch — the record is reused byte-for-byte (no
    # re-lower, no error).
    store.save_batch_plan(PROJECT, plan1.batch_id, plan1)
    resp = dispatch_batch(
        store=store, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=FakeRunner(), base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp["routed"] is True
    assert record_path.read_bytes() == before


def test_resume_does_not_relower_or_reexecute(tmp_path, monkeypatch):
    """Crash-resume: the persisted lowering record is REUSED byte-for-byte
    (never re-lowered — and saving again is provably not attempted), the
    read-only terminal outcome is not re-executed, and the routed markers
    stay untouched."""
    plan = mixed_plan()
    store1 = make_store(tmp_path, plan)
    runner1 = FakeRunner()
    resp1 = dispatch_batch(
        store=store1, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=runner1, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp1["routed"] is True
    record_path = store1.authority_dir(PROJECT) / f"{BATCH}-lowering.json"
    before = record_path.read_bytes()
    routed_before = {
        k: v for k, v in store1.load_worker_outcomes(PROJECT, BATCH).items()
        if v.status == ROUTED_STATUS
    }

    # RESTART: fresh store + fresh runner objects over the same root.
    def _boom(*args, **kwargs):
        raise AssertionError("resume must not re-lower/re-save the intent")

    store2 = ProjectStore(tmp_path)
    monkeypatch.setattr(store2, "save_lowered_intent", _boom)
    runner2 = FakeRunner()
    resp2 = dispatch_batch(
        store=store2, project_id=PROJECT, protocol=None,
        batch_id=BATCH, runner=runner2, base_sha=BASE, workdir=str(tmp_path),
    )
    assert resp2["routed"] is True
    assert record_path.read_bytes() == before  # byte-identical, not re-lowered
    assert runner2.started == []  # read-only terminal outcome not re-executed
    outcomes = store2.load_worker_outcomes(PROJECT, BATCH)
    assert outcomes["r1"].status == TaskStatus.VERIFIED.value
    for mid, wo in routed_before.items():
        assert outcomes[mid] == wo  # routed markers untouched


def test_routed_status_survives_resume_in_dispatcher(tmp_path):
    """The dispatcher tolerates persisted CHECKPOINT_ROUTED outcomes (not
    a TaskStatus): no crash on load, the routed id is never executed, and
    it counts as satisfied in the reentry gate."""
    plan = ro_plan(
        delegations=[
            dspec("s1", "codebase-investigator", owned_files=["s1.py"]),
            dspec("s2", "ui-reviewer", owned_files=["s2.py"], deps=["s1"]),
        ],
        reentry=schemas.ReentryGate(required_ids=["s1", "s2"]),
    )
    store = make_store(tmp_path, plan)
    store.save_worker_outcome(
        PROJECT, BATCH,
        schemas.WorkerOutcome(
            delegation_id="s1", role="codebase-investigator",
            status=ROUTED_STATUS, conclusion="lowered earlier — awaiting gates",
            evidence_refs=["authority/batch-001-lowering.json"],
        ),
    )
    runner = FakeRunner()
    dispatcher = BatchDispatcher(store, PROJECT, runner=runner, max_fixers=0)
    reason = dispatcher.run_batch(BATCH)  # would ValueError on TaskStatus(...) without the fix
    assert runner.started == ["s2"]  # s1 routed: terminal + satisfied, never re-run
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    outcomes = store.load_worker_outcomes(PROJECT, BATCH)
    assert outcomes["s1"].status == ROUTED_STATUS
    assert outcomes["s2"].status == "VERIFIED"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
