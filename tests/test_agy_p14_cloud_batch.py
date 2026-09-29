"""P14 — Interrupt-Driven Cloud Batch Planner: focused deterministic tests.

ZERO live Gemini / MiMo calls: fake cloud planner (counting invocations),
fake worker runner (injected results), temp state roots.

P16 RETARGETING (user-confirmed 2026-09-29): P14's run_batch previously
executed mutating roles (implementer / test-author) directly. That is the
uncontrolled write path P16 closed — mutating delegations are LOWERED to
the checkpoint runtime (never executed), read-only delegations still
execute directly, and crash-resume invariants are re-asserted on read-only
batches. Every test that drove a MUTATING batch through run_batch is
retargeted to dispatch_batch against the NEW semantics; nothing deleted,
each invariant translated. The fixer MECHANISM stays in the dispatcher
(P13 reuse, retained for the P18 authority-execution integration) but the
direct path never spawns fixers — see test_22d for its unit coverage.

Proofs covered (per P14 contract):
 1. NEW -> exactly one CLARIFY call
 2. CLARIFY dispatches zero workers
 3. user answer -> one BATCH call
 4. ProjectContract carries original prompt + clarification answer
 5. malformed cloud action fails closed
 6. BATCH_RUNNING cannot invoke cloud
 7. individual worker completion cannot invoke cloud
 8. two independent workers start concurrently
 9. dependent worker waits
10. P13 delegation constraints respected (shared-file serialization,
    concurrency cap)
11. unknown role rejected
12. dependency cycle rejected
13. worker limit enforced
14. all required terminal -> REENTRY_READY
15. one reentry generation -> at most one cloud invocation
16. raw transcript absent from BatchOutcome
17. oversized result compressed deterministically
18. required failure -> escalation, not endless retry
19. NEEDS_HUMAN parks
20. FINAL -> COMPLETE
21. COMPLETE cannot invoke cloud
22. restart during BATCH_RUNNING does not duplicate work/cloud calls
23. role manifest carries no tool schemas
24. planner prompt stays within size budget
25. cloud tool-use event -> protocol violation
26. quota router: Gemini 429 -> Claude Opus fallback on the SAME turn
27. quota exhaustion persists across a router rebuild (no re-probe)
28. capacity 503 is transient (never persisted)
29. non-quota CLI failure fails closed (no tier hopping)
30. all tiers exhausted -> fail closed
31. quota expiry restores the primary tier
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.delegation_policy import (
    TaskStatus,
    spawn_fixer,
)
from app.orchestration.cloud_batch import (
    ROUTED_STATUS,
    BatchDispatcher,
    CloudPlanner,
    CloudProtocol,
    ProjectStore,
    WorkerRunResult,
    build_delegation_plan,
    dispatch_batch,
    schemas,
)
from app.orchestration.cloud_batch.planner import (
    BATCH_PROMPT_MAX_BYTES,
    CloudPlannerUnavailable,
    CloudTierCapacityError,
    QuotaRouter,
    TIER_PRIORITY,
    build_batch_prompt,
    build_clarify_prompt,
    parse_cloud_action,
    routed_agy_invoke,
)
from app.orchestration.cloud_batch.protocol import (
    CloudInvocationBlocked,
    CloudProtocolViolation,
)
from app.orchestration.cloud_batch.roles import render_role_manifest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class CountingPlanner:
    """Fake cloud planner port: counts every invocation, returns canned
    CloudAction objects, fails closed on demand."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.clarify_action = schemas.ClarificationRequest(
            questions=["What result do you want, and what is out of scope?"]
        )
        self.batch_action: schemas.BatchPlan | None = None
        self.reentry_actions: list[schemas.CloudAction] = []
        self.fail_next: str | None = None  # 'clarify' | 'batch' | 'reentry'

    def clarify(self, prompt: str) -> schemas.ClarificationRequest:
        self.calls.append("clarify")
        if self.fail_next == "clarify":
            raise ValueError("simulated malformed clarify action")
        return self.clarify_action

    def batch(self, prompt: str) -> schemas.BatchPlan:
        self.calls.append("batch")
        if self.fail_next == "batch":
            # Return a non-BatchPlan action: the protocol must fail closed.
            return schemas.NeedsHuman(question="unexpected")  # type: ignore[return-value]
        assert self.batch_action is not None
        return self.batch_action

    def reentry(self, prompt: str) -> schemas.CloudAction:
        self.calls.append("reentry")
        if self.fail_next == "reentry":
            raise ValueError("simulated malformed reentry action")
        assert self.reentry_actions, "reentry_actions must be pre-set"
        action, self.reentry_actions = self.reentry_actions[0], self.reentry_actions[1:]
        return action


class FakeRunner:
    """Injectable worker runner. ``results`` maps task id -> result or an
    Exception to raise. Tracks concurrency for serialization proofs."""

    def __init__(self, results: dict) -> None:
        self.results = results
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running_now: list[str] = []
        self.max_concurrent = 0
        self.lock = threading.Lock()
        self.barrier: threading.Event | None = None  # set by tests

    def run(self, task, *, workdir: str) -> WorkerRunResult:
        with self.lock:
            self.started.append(task.id)
            self.running_now.append(task.id)
            self.max_concurrent = max(self.max_concurrent, len(self.running_now))
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        outcome = self.results[task.id]
        if isinstance(outcome, Exception):
            with self.lock:
                self.running_now.remove(task.id)
            raise outcome
        time.sleep(0.01)  # deterministic small dwell so overlap is real
        with self.lock:
            self.running_now.remove(task.id)
            self.finished.append(task.id)
        if isinstance(outcome, WorkerRunResult):
            return outcome
        return WorkerRunResult(
            status=TaskStatus.VERIFIED.value, conclusion=f"{task.id} done", raw={}
        )


def make_store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path)


def make_contract() -> schemas.ProjectContract:
    return schemas.ProjectContract(
        project_id="p14-proj",
        initial_prompt="Build the thing, please.",
        objective="build the thing",
        base_sha="a" * 40,
    )


def two_task_plan(**over) -> schemas.BatchPlan:
    defaults = dict(
        batch_id="batch-001",
        objective="build the thing",
        delegations=[
            schemas.DelegationSpec(
                id="a",
                role="implementer",
                objective="implement core",
                owned_files=["core.py"],
            ),
            schemas.DelegationSpec(
                id="b",
                role="test-author",
                objective="write tests",
                owned_files=["tests/core.py"],
            ),
        ],
        parallelism=4,
    )
    defaults.update(over)
    return schemas.BatchPlan(**defaults)


def ro_two_task_plan(**over) -> schemas.BatchPlan:
    """P16: the same two-delegation shape as two_task_plan, but READ-ONLY
    roles (investigation/review) — these batches stay on the direct
    dispatch path under the P16 authority rules."""
    defaults = dict(
        batch_id="batch-001",
        objective="investigate and review the thing",
        delegations=[
            schemas.DelegationSpec(
                id="a",
                role="codebase-investigator",
                objective="map core",
                owned_files=["core.py"],
            ),
            schemas.DelegationSpec(
                id="b",
                role="ui-reviewer",
                objective="review surface",
                owned_files=["tests/core.py"],
            ),
        ],
        parallelism=4,
    )
    defaults.update(over)
    return schemas.BatchPlan(**defaults)


def chain(tmp_path: Path, planner: CountingPlanner, runner: FakeRunner):
    store = make_store(tmp_path)
    store.create_project(make_contract())
    protocol = CloudProtocol(store, "p14-proj", planner)
    dispatcher = BatchDispatcher(
        store, "p14-proj", runner=runner, protocol=protocol, workdir="/tmp"
    )
    return store, protocol, dispatcher


def dispatch(store, protocol, runner, batch_id="batch-001", workdir="/tmp") -> str:
    """P16 canonical dispatch entry: all-read-only plans execute directly
    (through this helper the dispatcher is built with max_fixers=0); any
    mutating delegation lowers/routes instead. Returns the reentry reason."""
    resp = dispatch_batch(
        store=store, project_id="p14-proj", protocol=protocol,
        batch_id=batch_id, runner=runner, workdir=workdir,
    )
    return resp["reason"]


# ---------------------------------------------------------------------------
# 1-4: clarify -> answer -> batch
# ---------------------------------------------------------------------------


def test_01_new_project_one_clarify_call(tmp_path):
    store, protocol, _ = chain(tmp_path, CountingPlanner(), FakeRunner({}))
    action = protocol.request_clarify()
    assert action.mode is schemas.CloudMode.CLARIFY
    assert action.questions
    assert store.cloud_call_count("p14-proj") == 1
    files = list((store.cloud_dir("p14-proj")).iterdir())
    assert [f.name for f in files] == ["0001-clarify.json"]


def test_02_clarify_dispatches_zero_workers(tmp_path):
    planner, runner = CountingPlanner(), FakeRunner({})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    assert runner.started == []
    batches = store.batches_dir("p14-proj")
    assert list(batches.iterdir()) == []


def test_03_user_answer_one_batch_call(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = two_task_plan()
    store, protocol, _ = chain(tmp_path, planner, FakeRunner({}))
    protocol.request_clarify()
    protocol.record_user_answer("In scope: core only. No deployment.")
    plan = protocol.request_batch()
    assert plan.batch_id == "batch-001"
    assert planner.calls.count("batch") == 1
    assert store.cloud_call_count("p14-proj") == 2  # clarify + batch


def test_04_contract_carries_prompt_and_answer(tmp_path):
    store, protocol, _ = chain(tmp_path, CountingPlanner(), FakeRunner({}))
    protocol.request_clarify()
    protocol.record_user_answer("VERBATIM ANSWER TOKEN 12345")
    contract = store.load_project("p14-proj")
    assert contract.initial_prompt == "Build the thing, please."
    assert contract.clarification_answer == "VERBATIM ANSWER TOKEN 12345"
    assert contract.state is schemas.CloudProjectState.READY_TO_PLAN


# ---------------------------------------------------------------------------
# 5: malformed cloud action fails closed
# ---------------------------------------------------------------------------


def test_05_malformed_cloud_action_fails_closed(tmp_path):
    planner = CountingPlanner()
    store, protocol, _ = chain(tmp_path, planner, FakeRunner({}))
    planner.batch_action = two_task_plan()
    # The batch turn returns a NON-BatchPlan action (NeedsHuman) ->
    # protocol violation, project FAILED, no batch persisted.
    planner.fail_next = "batch"
    protocol.request_clarify()
    protocol.record_user_answer("go")
    with pytest.raises(CloudProtocolViolation):
        protocol.request_batch()
    assert protocol.state() is schemas.CloudProjectState.FAILED
    assert list(store.batches_dir("p14-proj").iterdir()) == []


def test_05b_planner_exception_fails_closed(tmp_path):
    planner = CountingPlanner()
    store, protocol, _ = chain(tmp_path, planner, FakeRunner({}))
    planner.fail_next = "batch"
    protocol.request_clarify()
    protocol.record_user_answer("go")
    with pytest.raises(CloudProtocolViolation):
        protocol.request_batch()
    assert protocol.state() is schemas.CloudProjectState.FAILED


# ---------------------------------------------------------------------------
# 6-7, 14-15: dormancy + reentry
# ---------------------------------------------------------------------------


def test_06_batch_running_cannot_invoke_cloud(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = two_task_plan()
    store, protocol, _ = chain(tmp_path, planner, FakeRunner({}))
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    assert protocol.state() is schemas.CloudProjectState.BATCH_RUNNING
    with pytest.raises(CloudInvocationBlocked):
        protocol.request_batch()
    with pytest.raises(CloudInvocationBlocked):
        protocol.request_reentry("batch-001")
    assert store.cloud_call_count("p14-proj") == 2


def test_07_worker_completion_cannot_invoke_cloud(tmp_path):
    """P16-retargeted: the read-only delegation still executes directly;
    the mutating one is LOWERED/ROUTED (never executed). Neither worker
    completion nor the routing makes a single cloud call."""
    planner = CountingPlanner()
    planner.batch_action = two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="codebase-investigator",
                                   objective="map core", owned_files=["core.py"]),
            schemas.DelegationSpec(id="b", role="implementer",
                                   objective="implement core",
                                   owned_files=["impl.py"],
                                   verification=["pytest -q tests/t_b.py"]),
        ]
    )
    planner.reentry_actions = [
        schemas.FinalResult(success=True, summary="done")
    ]
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    calls_before = store.cloud_call_count("p14-proj")
    reason = dispatch(store, protocol, runner)
    calls_after_dispatch = store.cloud_call_count("p14-proj")
    # Completion of the read-only worker AND the mutating lowering made
    # NO cloud calls: only the deterministic gate ran.
    assert calls_after_dispatch == calls_before
    assert runner.started == ["a"]  # mutating 'b' was never dispatched
    assert "b" not in runner.started
    outcomes = store.load_worker_outcomes("p14-proj", "batch-001")
    assert outcomes["b"].status == ROUTED_STATUS
    assert store.load_lowered_intent("p14-proj", "batch-001")  # lowering persisted
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    assert protocol.state() is schemas.CloudProjectState.REENTRY_READY
    # Only the SUPERVISOR reentry may call the cloud — exactly once.
    protocol.request_reentry("batch-001")
    assert store.cloud_call_count("p14-proj") == calls_before + 1


def _run_to_reentry(tmp_path, planner, runner):
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    reason = dispatch(store, protocol, runner)
    return store, protocol, reason


def test_14_all_required_terminal_marks_reentry_ready(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, reason = _run_to_reentry(tmp_path, planner, runner)
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    record = store.load_reentry("p14-proj", "batch-001")
    assert record["reason"] == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    assert record["cloud_calls"] == 0


def test_15_one_reentry_generation_one_cloud_call(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    planner.reentry_actions = [
        schemas.FinalResult(success=True, summary="done")
    ]
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    dispatch(store, protocol, runner)
    action = protocol.request_reentry("batch-001")
    assert isinstance(action, schemas.FinalResult)
    # The generation's single call is consumed: a duplicate is refused.
    with pytest.raises(CloudInvocationBlocked):
        protocol.request_reentry("batch-001")
    assert store.cloud_call_count("p14-proj") == 3  # clarify + batch + 1 reentry


# ---------------------------------------------------------------------------
# 8-10, 13: dispatch mechanics via P13
# ---------------------------------------------------------------------------


def test_08_two_independent_workers_concurrent(tmp_path):
    # P16-retargeted to read-only roles: the direct path is read-only-only.
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    barrier = threading.Event()
    runner.barrier = barrier
    store, protocol, dispatcher = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()

    started = threading.Event()
    # Release the barrier once BOTH tasks are running.
    def _watcher():
        deadline = time.time() + 5
        while time.time() < deadline:
            with runner.lock:
                if len(runner.running_now) == 2:
                    started.set()
                    barrier.set()
                    return
            time.sleep(0.005)
        started.set()
        barrier.set()

    t = threading.Thread(target=_watcher)
    t.start()
    reason = dispatch(store, protocol, runner)
    t.join(timeout=5)
    assert started.is_set(), "both independent tasks never ran together"
    assert runner.max_concurrent == 2
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"


def test_09_dependent_worker_waits(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="codebase-investigator",
                                   objective="core", owned_files=["core.py"]),
            schemas.DelegationSpec(id="b", role="ui-reviewer",
                                   objective="review",
                                   owned_files=["tests/core.py"], deps=["a"]),
        ]
    )
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    reason = dispatch(store, protocol, runner)
    assert runner.started.index("a") < runner.started.index("b")
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"


def test_10_p13_shared_file_serialization(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan(
        parallelism=4,
        delegations=[
            schemas.DelegationSpec(id="a", role="codebase-investigator",
                                   objective="x", owned_files=["shared.py"]),
            schemas.DelegationSpec(id="b", role="ui-reviewer",
                                   objective="y", owned_files=["shared.py"]),
            schemas.DelegationSpec(id="c", role="adversarial-reviewer",
                                   objective="z", owned_files=["shared.py"]),
        ],
    )
    runner = FakeRunner({
        "a": WorkerRunResult(status="VERIFIED"),
        "b": WorkerRunResult(status="VERIFIED"),
        "c": WorkerRunResult(status="VERIFIED"),
    })
    barrier = threading.Event()
    runner.barrier = barrier

    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()

    # While any two tasks are simultaneously running, they must not share
    # an owned file (P13 serialization). The barrier holds all RUNNING
    # tasks, so check the running set.
    def _watcher():
        deadline = time.time() + 5
        while time.time() < deadline:
            with runner.lock:
                if len(runner.running_now) >= 2:
                    overlap = [
                        tid for tid in runner.running_now
                    ]
                    if len(overlap) >= 2:
                        # all three own shared.py -> P13 must never put two
                        # in one wave
                        overlap_shared = overlap
                        assert False, (
                            f"P13 shared-file violation: "
                            f"{overlap_shared} ran concurrently"
                        )
                    barrier.set()
                    return
            time.sleep(0.005)
        barrier.set()

    t = threading.Thread(target=_watcher)
    t.start()
    dispatch(store, protocol, runner)
    t.join(timeout=5)
    assert runner.max_concurrent <= 1, (
        "shared-file tasks were never serialized: "
        f"max_concurrent={runner.max_concurrent}"
    )


def test_13_worker_limit_enforced(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan(
        parallelism=2,
        delegations=[
            schemas.DelegationSpec(id=f"t{i}",
                                   role=["codebase-investigator", "ui-reviewer",
                                         "adversarial-reviewer",
                                         "codebase-investigator"][i],
                                   objective=f"task {i}",
                                   owned_files=[f"mod{i}.py"])
            for i in range(4)
        ],
    )
    runner = FakeRunner({f"t{i}": WorkerRunResult(status="VERIFIED") for i in range(4)})
    barrier = threading.Event()
    runner.barrier = barrier
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()

    def _watcher():
        deadline = time.time() + 8
        while time.time() < deadline:
            with runner.lock:
                if len(runner.running_now) > 0:
                    return
            time.sleep(0.005)
        barrier.set()

    t = threading.Thread(target=_watcher)
    t.start()
    dispatch(store, protocol, runner)
    t.join(timeout=8)
    # concurrency_limit=2 (clamped from parallelism=2 by P13 cap): never 3+
    assert runner.max_concurrent <= 2
    assert runner.max_concurrent == 2, "limit should be used, not ignored"


# ---------------------------------------------------------------------------
# 11-12: structural rejection
# ---------------------------------------------------------------------------


def test_11_unknown_role_rejected(tmp_path):
    """Unknown roles fail closed in the P16 classifier BEFORE dispatch —
    same ValueError contract as P14's role lowering, now at the guard."""
    planner = CountingPlanner()
    planner.batch_action = two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="quantum-teleporter",
                                   objective="x", owned_files=["x.py"])
        ]
    )
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED")})
    store, protocol, dispatcher = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    with pytest.raises(ValueError, match="unknown role"):
        dispatcher.run_batch("batch-001")
    # No worker ran, no outcome persisted for the bogus role.
    assert runner.started == []


def test_12_dependency_cycle_rejected(tmp_path):
    with pytest.raises(ValueError):
        two_task_plan(
            delegations=[
                schemas.DelegationSpec(id="a", role="implementer",
                                       objective="x", owned_files=["a.py"],
                                       deps=["b"]),
                schemas.DelegationSpec(id="b", role="implementer",
                                       objective="y", owned_files=["b.py"],
                                       deps=["a"]),
            ]
        )


def test_12b_unknown_dependency_rejected(tmp_path):
    with pytest.raises(ValueError):
        two_task_plan(
            delegations=[
                schemas.DelegationSpec(id="a", role="implementer",
                                       objective="x", owned_files=["a.py"],
                                       deps=["ghost"]),
            ]
        )


# ---------------------------------------------------------------------------
# 16-18: outcome compression + bounded failure
# ---------------------------------------------------------------------------


RAW_TRANSCRIPT = "SECRET-TRANSCRIPT-DO-NOT-LEAK " * 40


def test_16_raw_transcript_absent_from_batch_outcome(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    runner = FakeRunner({
        "a": WorkerRunResult(status="VERIFIED", conclusion="ok",
                             raw={"transcript": RAW_TRANSCRIPT}),
        "b": WorkerRunResult(status="VERIFIED",
                             raw={"stdout": RAW_TRANSCRIPT * 2}),
    })
    store, protocol, _ = _run_to_reentry(tmp_path, planner, runner)
    outcome_path = store.batch_dir("p14-proj", "batch-001") / "outcome.json"
    blob = outcome_path.read_text()
    assert "SECRET-TRANSCRIPT" not in blob
    # Raw evidence IS persisted locally (the reference target exists).
    raw_a = store.batch_dir("p14-proj", "batch-001") / "workers" / "a.raw.json"
    assert raw_a.exists()
    assert "SECRET-TRANSCRIPT" in raw_a.read_text()


def test_17_oversized_result_compressed_deterministically(tmp_path):
    from app.orchestration.cloud_batch.outcome import compress_worker_result

    huge = "x" * 9000
    outcome = compress_worker_result(
        "a", "implementer", "VERIFIED",
        {
            "conclusion": huge,
            "evidence_refs": [f"ref-{i}" for i in range(60)],
            "risks": [f"risk-{i}" for i in range(50)],
            "unknowns": [f"unk-{i}" for i in range(50)],
        },
    )
    assert outcome.truncated is True
    assert len(outcome.conclusion) <= schemas.WORKER_CONCLUSION_MAX_CHARS
    assert len(outcome.evidence_refs) == schemas.WORKER_EVIDENCE_REF_MAX
    assert len(outcome.risks) == schemas.WORKER_RISK_MAX
    assert len(outcome.unknowns) == schemas.WORKER_UNKNOWN_MAX
    # Deterministic: same input -> same output.
    again = compress_worker_result(
        "a", "implementer", "VERIFIED",
        {
            "conclusion": huge,
            "evidence_refs": [f"ref-{i}" for i in range(60)],
            "risks": [f"risk-{i}" for i in range(50)],
            "unknowns": [f"unk-{i}" for i in range(50)],
        },
    )
    assert outcome == again


def test_18_required_failure_escalates_not_endless(tmp_path):
    """P16-retargeted: a FAILED read-only task escalates via the reentry
    gate — it NEVER gains a mutating repair. The direct dispatch path is
    built with max_fixers=0: no fixer may spawn, no endless retry; the
    dependent is deterministically CANCELLED."""
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="codebase-investigator",
                                   objective="x", owned_files=["x.py"]),
            schemas.DelegationSpec(id="b", role="ui-reviewer",
                                   objective="y", owned_files=["y.py"],
                                   deps=["a"]),
        ]
    )
    runner = FakeRunner({
        "a": WorkerRunResult(status="FAILED", conclusion="review crashed"),
    })
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    reason = dispatcher_run(store, protocol, runner)
    assert reason == "BATCH_FAILED_WITH_ESCALATION"
    executed = set(runner.started)
    # No fixer spawning on the P16 direct path: the original attempted
    # once, and no *-fix-* id was ever executed.
    assert "a" in executed
    assert not any(t.startswith("a-fix-") for t in executed)
    # Dependent b was deterministically cancelled (blocked by the failed
    # dependency), never executed.
    assert "b" not in executed
    statuses = store.load_worker_outcomes("p14-proj", "batch-001")
    assert statuses["b"].status == "CANCELLED"
    assert not any(tid.startswith("a-fix-") for tid in statuses)


def dispatcher_run(store, protocol, runner):
    """P16: batches dispatch through the authority entry point (direct
    path is read-only-only, max_fixers=0)."""
    return dispatch(store, protocol, runner)


# ---------------------------------------------------------------------------
# 19-21: NEEDS_HUMAN / FINAL / terminal
# ---------------------------------------------------------------------------


def test_19_needs_human_parks(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    planner.reentry_actions = [
        schemas.NeedsHuman(question="Which architecture should we use?",
                           options=["A", "B"])
    ]
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    dispatch(store, protocol, runner)
    action = protocol.request_reentry("batch-001")
    assert isinstance(action, schemas.NeedsHuman)
    assert protocol.state() is schemas.CloudProjectState.NEEDS_HUMAN
    # Parked: no cloud allowed from NEEDS_HUMAN.
    with pytest.raises(CloudInvocationBlocked):
        protocol.request_reentry("batch-001")
    # Human decides -> resume -> exactly one more BATCH turn is possible.
    protocol.resume_from_needs_human()
    assert protocol.state() is schemas.CloudProjectState.READY_TO_PLAN


def test_20_final_completes(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    planner.reentry_actions = [
        schemas.FinalResult(success=True, summary="all done",
                            artifacts=["core.py"])
    ]
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    dispatch(store, protocol, runner)
    action = protocol.request_reentry("batch-001")
    assert isinstance(action, schemas.FinalResult)
    assert protocol.state() is schemas.CloudProjectState.COMPLETE
    assert store.cloud_call_count("p14-proj") == 3


def test_21_complete_cannot_invoke_cloud(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    planner.reentry_actions = [schemas.FinalResult(success=True, summary="done")]
    runner = FakeRunner({"a": WorkerRunResult(status="VERIFIED"),
                         "b": WorkerRunResult(status="VERIFIED")})
    store, protocol, _ = chain(tmp_path, planner, runner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    dispatch(store, protocol, runner)
    protocol.request_reentry("batch-001")
    for bad_call in (protocol.request_batch, protocol.request_clarify):
        with pytest.raises(CloudInvocationBlocked):
            bad_call()
    # COMPLETE is not REENTRY_READY -> the invocation guard fires.
    with pytest.raises(CloudInvocationBlocked):
        protocol.request_reentry("batch-001")


# ---------------------------------------------------------------------------
# 22: restart safety
# ---------------------------------------------------------------------------


class CrashRunner(FakeRunner):
    """Completes 'a' on the first dispatch, then crashes (process death)
    on any subsequent run — simulating a restart mid-batch."""

    def __init__(self, results: dict) -> None:
        super().__init__(results)
        self.first_call_done = False

    def run(self, task, *, workdir: str) -> WorkerRunResult:
        if not self.first_call_done:
            self.first_call_done = True
            return super().run(task, workdir=workdir)
        raise RuntimeError("simulated process crash")


def test_22_restart_no_duplicate_work_or_cloud_calls(tmp_path):
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    planner.reentry_actions = [schemas.FinalResult(success=True, summary="done")]
    store = make_store(tmp_path)
    store.create_project(make_contract())
    protocol = CloudProtocol(store, "p14-proj", planner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()
    calls_after_batch = store.cloud_call_count("p14-proj")

    # Phase 1: first wave runs; both workers "complete" but the process
    # crashes before the reentry gate is marked. Use a runner that raises
    # AFTER persisting 'a' and before 'b' by making 'b' raise.
    # (a crash is modeled as: some outcomes persisted, gate not marked.)
    crashy = FakeRunner({
        "a": WorkerRunResult(status="VERIFIED"),
        "b": RuntimeError("crash"),
    })
    with pytest.raises(Exception):
        dispatch(store, protocol, crashy)
    # 'a' is persisted terminal; 'b' is not.
    outcomes = store.load_worker_outcomes("p14-proj", "batch-001")
    assert "a" in outcomes
    assert protocol.state() is schemas.CloudProjectState.BATCH_RUNNING

    # Phase 2: RESTART — fresh protocol + dispatcher, same store.
    planner2 = CountingPlanner()
    planner2.reentry_actions = [schemas.FinalResult(success=True, summary="done")]
    protocol2 = CloudProtocol(store, "p14-proj", planner2)
    assert protocol2.state() is schemas.CloudProjectState.BATCH_RUNNING
    good = FakeRunner({"b": WorkerRunResult(status="VERIFIED")})
    reason = dispatch(store, protocol2, good)
    # 'a' was NOT re-executed after restart (loaded from persisted state).
    assert good.started == ["b"]
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    # Supervisor reentry: one call. The crashed phase consumed zero cloud
    # calls after the batch turn; total stays clarify+batch+1.
    protocol2.request_reentry("batch-001")
    assert store.cloud_call_count("p14-proj") == calls_after_batch + 1
    with pytest.raises(CloudInvocationBlocked):
        protocol2.request_reentry("batch-001")


def test_22b_crash_resume_does_not_reexecute_terminal_read_only(tmp_path):
    """P16-retargeted (was: terminal-FIXER re-execution/budget regression).
    The direct path is read-only-only with max_fixers=0, so the fixer
    budget is N/A there; what MUST still hold across a crash-resume of a
    READ-ONLY batch: terminal outcomes already persisted (even FAILED)
    are never re-executed, a never-run sibling IS executed, and a
    terminal failure still escalates instead of retrying forever."""
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan()
    store = make_store(tmp_path)
    store.create_project(make_contract())
    protocol = CloudProtocol(store, "p14-proj", planner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()

    # PRE-CRASH progress persisted to disk: a FAILED (terminal) while
    # sibling b never ran. State is still BATCH_RUNNING (the crash
    # happened before the reentry gate was marked).
    store.save_worker_outcome(
        "p14-proj", "batch-001",
        schemas.WorkerOutcome(delegation_id="a", role="codebase-investigator",
                              status="FAILED", conclusion="a failed"))

    # RESTART: fresh protocol + fresh dispatch, same store.
    planner2 = CountingPlanner()
    planner2.reentry_actions = [schemas.NeedsHuman(question="escalate?")]
    protocol2 = CloudProtocol(store, "p14-proj", planner2)
    assert protocol2.state() is schemas.CloudProjectState.BATCH_RUNNING
    runner = FakeRunner({"b": WorkerRunResult(status="VERIFIED")})
    reason = dispatch(store, protocol2, runner)
    # The terminal FAILED outcome was not re-executed; only sibling b ran;
    # and no fixer exists anywhere (the direct path cannot spawn one).
    assert "a" not in runner.started
    assert not any(t.startswith("a-fix-") for t in runner.started), runner.started
    assert runner.started == ["b"]
    # Pre-crash failure still holds across the restart: escalation.
    assert reason == "BATCH_FAILED_WITH_ESCALATION"
    assert protocol2.state() is schemas.CloudProjectState.REENTRY_READY
    outcomes = store.load_worker_outcomes("p14-proj", "batch-001")
    assert outcomes["a"].status == "FAILED"
    assert not any(tid.startswith("a-fix-") for tid in outcomes)


def test_22c_crash_resume_unblocks_dependents_of_terminal_verified(tmp_path):
    """P16-retargeted companion to 22b (was: VERIFIED-fixer promotion
    across a crash). The promoted slot is exactly the terminal-VERIFIED
    read-only outcome: a persisted VERIFIED must SATISFY its dependents
    across the restart (otherwise the dependent would be blocked or the
    original redundantly re-run)."""
    planner = CountingPlanner()
    planner.batch_action = ro_two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="codebase-investigator",
                                   objective="x", owned_files=["a.py"]),
            schemas.DelegationSpec(id="b", role="ui-reviewer",
                                   objective="t", owned_files=["tb.py"], deps=["a"]),
        ]
    )
    store = make_store(tmp_path)
    store.create_project(make_contract())
    protocol = CloudProtocol(store, "p14-proj", planner)
    protocol.request_clarify()
    protocol.record_user_answer("go")
    protocol.request_batch()

    # PRE-CRASH: a VERIFIED (persisted), crash before the reentry gate
    # ran. b (dependent on a) never ran.
    store.save_worker_outcome("p14-proj", "batch-001",
                              schemas.WorkerOutcome(delegation_id="a",
                                                    role="codebase-investigator",
                                                    status="VERIFIED",
                                                    conclusion="a done"))

    planner2 = CountingPlanner()
    planner2.reentry_actions = [schemas.FinalResult(success=True, summary="done")]
    protocol2 = CloudProtocol(store, "p14-proj", planner2)
    runner = FakeRunner({
        "b": WorkerRunResult(status="VERIFIED"),
    })
    reason = dispatch(store, protocol2, runner)
    # The pre-crash VERIFIED outcome satisfied b's dependency, so b (its
    # dependent) ran; a was not re-executed.
    assert "a" not in runner.started
    assert runner.started == ["b"]
    assert reason == "ALL_REQUIRED_DELEGATIONS_COMPLETE"
    outcomes = store.load_worker_outcomes("p14-proj", "batch-001")
    assert outcomes["a"].status == "VERIFIED"
    assert outcomes["b"].status == "VERIFIED"


def test_22d_fixer_rehydration_mechanism_retained_for_p18(tmp_path):
    """The P13 fixer mechanism stays in the dispatcher for the P18
    authority-execution integration, but it is UNREACHABLE from the P16
    direct path — so it is kept with a focused unit test that drives
    _rehydrate_fixers directly (hand-constructed P13 DelegationPlan +
    persisted fixer outcomes, never through run_batch): crash-resume
    restores the budget counting and the promotion shells."""
    plan = two_task_plan(
        delegations=[
            schemas.DelegationSpec(id="a", role="implementer",
                                   objective="x", owned_files=["a.py"]),
        ]
    )
    delegation = build_delegation_plan(plan, base_sha="a" * 40, workdir="/tmp")
    outcomes = {
        "a": schemas.WorkerOutcome(delegation_id="a", role="implementer",
                                   status="FAILED", conclusion="a broken"),
        "a-fix-1": schemas.WorkerOutcome(delegation_id="a-fix-1", role="fixer",
                                         status="FAILED", conclusion="still broken"),
        "a-fix-2": schemas.WorkerOutcome(delegation_id="a-fix-2", role="fixer",
                                         status="VERIFIED", conclusion="a fixed"),
    }
    statuses = {tid: TaskStatus(wo.status) for tid, wo in outcomes.items()}

    store = make_store(tmp_path)
    store.create_project(make_contract())
    d = BatchDispatcher(store, "p14-proj", runner=FakeRunner({}), workdir="/tmp")
    d._rehydrate_fixers(delegation, plan, outcomes, statuses)

    ids = {t.id for t in delegation.tasks}
    assert {"a-fix-1", "a-fix-2"} <= ids  # shells restored
    by_id = {t.id: t for t in delegation.tasks}
    assert by_id["a-fix-2"].worker_role == "fixer"
    assert statuses["a-fix-2"] is TaskStatus.VERIFIED
    # P13 spawn_fixer budget counting sees them: the NEXT fixer is #3.
    by_id["a"].status = TaskStatus.FAILED
    nxt = spawn_fixer(delegation, "a", "regression after rehydration probe",
                      max_fixers=3)
    assert nxt.id == "a-fix-3"
    # Fixer-takes-the-slot promotion shell restored: the pre-crash
    # VERIFIED fixer promotes its original.
    d._promote_fixer_results(delegation, statuses)
    assert statuses["a"] is TaskStatus.VERIFIED


# ---------------------------------------------------------------------------
# 23-25: budgets + tool-event guard
# ---------------------------------------------------------------------------


def test_23_role_manifest_no_tool_schemas():
    manifest = render_role_manifest()
    parsed = json.loads(manifest)
    # Gemini sees roles + purpose + capability words only.
    assert "tools" not in parsed
    assert "serena_find_symbol" not in manifest
    assert "serena_get_symbols_overview" not in manifest
    assert "opencode" not in manifest
    assert parsed["max_parallel"] == 4
    assert set(parsed["roles"]) == {
        "codebase-investigator", "test-author", "implementer",
        "ui-reviewer", "adversarial-reviewer", "fixer",
    }
    # No per-tool schema leakage: manifest is small.
    assert len(manifest.encode()) < schemas.ROLE_MANIFEST_MAX_BYTES


def test_24_planner_prompt_within_budget(tmp_path):
    contract = make_contract()
    clarify_prompt = build_clarify_prompt(contract)
    assert len(clarify_prompt.encode()) <= BATCH_PROMPT_MAX_BYTES
    batch_prompt = build_batch_prompt(
        contract, role_manifest=render_role_manifest(),
        durable_state={"last_batch": "batch-001", "state": "ok"},
    )
    assert len(batch_prompt.encode()) <= BATCH_PROMPT_MAX_BYTES
    assert "VERBATIM" not in clarify_prompt
    assert contract.initial_prompt in clarify_prompt


def test_24b_core_rules_state_strict_batch_field_rules():
    """Regression (live smoke 2026-09-29): the BATCH action shape must
    pin the strict field rules that the schema enforces, or a compliant
    model still fails closed (owned_files=[] or a prose reentry.reason).
    The rules must also stay within the prompt budget."""
    from app.orchestration.cloud_batch.planner import (
        CORE_RULES,
        core_rules_within_budget,
    )
    assert core_rules_within_budget()
    assert "never empty" in CORE_RULES                      # owned_files rule
    assert "EXACTLY ONE literal, never prose" in CORE_RULES  # reentry.reason rule
    assert '"ALL_REQUIRED_DELEGATIONS_COMPLETE"' in CORE_RULES
    # The template itself must carry a CONFORMING example (not "..."
    # placeholders) for the two historically-violated fields.
    assert '"owned_files":["a.py"]' in CORE_RULES
    assert '"reason":"ALL_REQUIRED_DELEGATIONS_COMPLETE"' in CORE_RULES
    # And a full batch prompt built from it must still fit the budget.
    contract = make_contract()
    prompt = build_batch_prompt(contract, role_manifest=render_role_manifest())
    assert len(prompt.encode()) <= BATCH_PROMPT_MAX_BYTES


def test_25_cloud_tool_event_protocol_violation():
    raw_with_tool_event = (
        '{"mode": "BATCH", "batch_id": "batch-001", "objective": "x", '
        '"delegations": [{"id": "a", "role": "implementer", '
        '"objective": "x", "owned_files": ["a.py"]}], '
        '"tool_calls": [{"function": {"name": "run_shell_command", '
        '"arguments": "{\\"cmd\\": \\"rm -rf /\\"}"}}]}'
    )
    with pytest.raises(CloudProtocolViolation, match="tool-use event"):
        parse_cloud_action(raw_with_tool_event)


def test_25b_clean_action_parses():
    raw = (
        '{"mode": "CLARIFY", "questions": ["What is the goal?"], '
        '"reasoning": "intent unclear"}'
    )
    action = parse_cloud_action(raw)
    assert isinstance(action, schemas.ClarificationRequest)


def test_25c_malformed_json_fails_closed():
    with pytest.raises(CloudProtocolViolation):
        parse_cloud_action("I will now edit the file core.py directly.")


def test_25d_mode_mismatch_fails_closed():
    with pytest.raises(CloudProtocolViolation):
        parse_cloud_action('{"mode": "OPERATIONAL_ASSIST", "text": "hi"}')


# ---------------------------------------------------------------------------
# Package-level: production planner adapter is wired to the same parse
# ---------------------------------------------------------------------------


def test_26_production_planner_adapter_parses_and_counts(tmp_path):
    invocations: list[str] = []

    def fake_invoke(prompt: str) -> str:
        invocations.append(prompt)
        return (
            '{"mode": "BATCH", "batch_id": "batch-009", '
            '"objective": "ship it", '
            '"delegations": [{"id": "a", "role": "implementer", '
            '"objective": "do it", "owned_files": ["a.py"]}]}'
        )

    planner = CloudPlanner(invoke=fake_invoke)
    plan = planner.batch("any prompt")
    assert plan.batch_id == "batch-009"
    assert len(invocations) == 1


def test_26b_production_planner_refuses_wrong_mode():
    def fake_invoke(prompt: str) -> str:
        return '{"mode": "CLARIFY", "questions": ["?"]}'

    planner = CloudPlanner(invoke=fake_invoke)
    with pytest.raises(CloudProtocolViolation, match="expected BATCH"):
        planner.batch("any prompt")


# ---------------------------------------------------------------------------
# Quota-tier transport routing (fluent Gemini -> Claude Opus)
# ---------------------------------------------------------------------------

_GEMINI = "Gemini 3.6 Flash (Medium)"
_CLAUDE = "claude-opus-4-6-thinking"

_QUOTA_429 = (
    "error: Individual quota reached. Please upgrade your subscription to "
    "increase your limits. Resets in 54h31m12s.\nAGY_ERROR: "
    '{"short_error":"RESOURCE_EXHAUSTED (code 429)"}'
)
_CAPACITY_503 = (
    "error: Our servers are experiencing high traffic right now, please try "
    "again in a minute. (UNAVAILABLE (code 503): No capacity available for "
    'model claude-opus-4-6-thinking on the server)'
)


def _fail_invoke(calls: list[str], script: dict[str, str]):
    """Fake agy_cli_invoke: model -> error string (raises) or raw output."""

    def fake(prompt: str, *, model: str | None = None) -> str:
        calls.append(model or "")
        outcome = script[model or ""]
        if "error" in outcome:
            raise CloudPlannerUnavailable(f"agy CLI exited 1 for model {model}: {outcome}")
        return outcome

    return fake


def test_27_quota_fallback_opus_on_gemini_429():
    """A 429 on the Gemini tier must fall through to Claude Opus for the
    SAME turn (one call per tier, first success wins)."""
    calls: list[str] = []
    import app.orchestration.cloud_batch.planner as planner_mod

    fake = _fail_invoke(calls, {
        _GEMINI: _QUOTA_429,
        _CLAUDE: '{"mode":"CLARIFY","questions":["scope?"]}',
    })
    original = planner_mod.agy_cli_invoke
    planner_mod.agy_cli_invoke = fake
    try:
        invoke = routed_agy_invoke(QuotaRouter())
        raw = invoke("prompt")
    finally:
        planner_mod.agy_cli_invoke = original
    assert calls == [_GEMINI, _CLAUDE]
    assert '"CLARIFY"' in raw


def test_28_quota_exhaustion_persists_across_router_rebuild(tmp_path):
    """A 429 parks the tier until its advertised reset AND survives a
    router rebuild (process restart) — the dead tier is not re-probed."""
    calls: list[str] = []
    import app.orchestration.cloud_batch.planner as planner_mod

    script = {_GEMINI: _QUOTA_429, _CLAUDE: "OK-OUTPUT"}
    fake = _fail_invoke(calls, script)
    original = planner_mod.agy_cli_invoke
    planner_mod.agy_cli_invoke = fake
    try:
        # First turn: Gemini 429 -> Claude Opus success.
        invoke = routed_agy_invoke(QuotaRouter(state_root=tmp_path, project_id="p14-proj"))
        assert invoke("p") == "OK-OUTPUT"
        # Restart: a fresh router must SKIP the exhausted Gemini tier.
        invoke2 = routed_agy_invoke(QuotaRouter(state_root=tmp_path, project_id="p14-proj"))
        assert invoke2("p") == "OK-OUTPUT"
    finally:
        planner_mod.agy_cli_invoke = original
    # Claude Opus only on the second turn — no Gemini re-probe.
    assert calls == [_GEMINI, _CLAUDE, _CLAUDE]
    assert (tmp_path / "projects" / "p14-proj" / "quota.json").exists()


def test_29_capacity_503_is_transient_not_persisted(tmp_path):
    """A 503 on Claude Opus must NOT persist a down state: a rebuilt
    router (restart) must be willing to try it again. With a quota wall
    present the failure reports CloudPlannerUnavailable (the quota
    dominates — waiting a minute cannot fix a 54h wall)."""
    calls: list[str] = []
    import app.orchestration.cloud_batch.planner as planner_mod

    script = {_GEMINI: _QUOTA_429, _CLAUDE: _CAPACITY_503}
    fake = _fail_invoke(calls, script)
    original = planner_mod.agy_cli_invoke
    planner_mod.agy_cli_invoke = fake
    try:
        # Turn 1: quota wall + transient down -> Unavailable (quota wins).
        invoke = routed_agy_invoke(QuotaRouter(state_root=tmp_path, project_id="p14-proj"))
        with pytest.raises(CloudPlannerUnavailable):
            invoke("p")
        # Restart: Claude (transient, never persisted) is eligible again.
        r2 = QuotaRouter(state_root=tmp_path, project_id="p14-proj")
        assert r2.next_model() == _CLAUDE  # Gemini still exhausted (persisted)
        statuses = {s["model"]: s["status"] for s in r2.tier_status()}
        assert statuses[_GEMINI] == "exhausted"
        assert statuses[_CLAUDE] == "healthy"
    finally:
        planner_mod.agy_cli_invoke = original
    assert calls == [_GEMINI, _CLAUDE]


def test_29b_pure_capacity_all_down_raises_capacity_error():
    """If NO quota wall is involved (every failure a transient 503), the
    no-healthy-tier error is the retryable-soon CloudTierCapacityError."""
    calls: list[str] = []
    import app.orchestration.cloud_batch.planner as planner_mod

    script = {_GEMINI: _CAPACITY_503, _CLAUDE: _CAPACITY_503}
    fake = _fail_invoke(calls, script)
    original = planner_mod.agy_cli_invoke
    planner_mod.agy_cli_invoke = fake
    try:
        invoke = routed_agy_invoke(QuotaRouter())
        with pytest.raises(CloudTierCapacityError):
            invoke("p")
    finally:
        planner_mod.agy_cli_invoke = original
    assert calls == [_GEMINI, _CLAUDE]


def test_30_non_quota_failure_does_not_switch_tiers():
    """A non-quota CLI failure (e.g. malformed model, auth) must fail
    closed on the current tier — no silent tier hopping, no retry."""
    calls: list[str] = []
    import app.orchestration.cloud_batch.planner as planner_mod

    def fake(prompt: str, *, model: str | None = None) -> str:
        calls.append(model or "")
        raise CloudPlannerUnavailable(f"agy CLI exited 2 for model {model}: invalid model selection")

    original = planner_mod.agy_cli_invoke
    planner_mod.agy_cli_invoke = fake
    try:
        invoke = routed_agy_invoke(QuotaRouter())
        with pytest.raises(CloudPlannerUnavailable, match="invalid model selection"):
            invoke("p")
    finally:
        planner_mod.agy_cli_invoke = original
    assert calls == [_GEMINI]


def test_31_all_tiers_exhausted_fails_closed():
    """With every tier quota-walled, next_model raises CloudPlannerUnavailable
    (a plan is never fabricated)."""
    r = QuotaRouter()
    r.record_exhausted(_GEMINI, 3600)
    r.record_exhausted(_CLAUDE, 3600)
    with pytest.raises(CloudPlannerUnavailable, match="all transport tiers"):
        r.next_model()


def test_32_quota_expiry_restores_tier(tmp_path):
    """After the advertised reset elapses, the parked Gemini tier is
    eligible again (primary priority restored) — including on a rebuilt
    router from persisted state."""
    r = QuotaRouter(state_root=tmp_path, project_id="p14-proj")
    r.record_exhausted(_GEMINI, 3600)
    r.record_exhausted(_CLAUDE, 3600)
    # Fast-forward past the reset in the PERSISTED state (simulates time
    # passing), then rebuild the router from disk.
    qpath = tmp_path / "projects" / "p14-proj" / "quota.json"
    qdata = json.loads(qpath.read_text())
    for model in qdata["exhausted_until"]:
        qdata["exhausted_until"][model] = time.time() - 10
    qpath.write_text(json.dumps(qdata))
    r2 = QuotaRouter(state_root=tmp_path, project_id="p14-proj")
    assert r2.next_model() == _GEMINI  # primary restored
    statuses = {s["model"]: s["status"] for s in r2.tier_status()}
    assert statuses == {_GEMINI: "healthy", _CLAUDE: "healthy"}
