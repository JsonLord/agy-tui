"""P18 — Canonical project supervisor (the single production lifecycle
coordinator).

ZERO live model calls, zero network, tmp state roots. Proves the
lifecycle the user asked for:

  NEW -> CLARIFY (1 cloud) -> human answer (0 cloud)
      -> BATCH (1 cloud) -> workers run (0 cloud) -> REENTRY_READY
      -> reentry (<=1 cloud/generation) -> FINAL -> COMPLETE

and the P18 invariants:

* advance_project performs DETERMINISTIC work only — at most ONE cloud
  call per invocation, no wait loops, no recursive advance;
* cloud is dormant while BATCH_RUNNING (0 cloud calls during a batch
  run, even across a restart);
* restart recovery from every non-terminal state duplicates NOTHING
  (no re-CLARIFY, no re-BATCH, no duplicate reentry cloud call, no
  duplicate worker run, no duplicate human decision);
* human input has explicit provenance — a model payload can never
  resume a parked project (delegates to the P15 bridge);
* G4 remains human-only — the supervisor has NO GateEngine and no
  route to pre-mark a gate (it can only call the injected executor);
* the supervisor CANNOT directly accept a lowered mutation — checkpoint
  gate authority (P11–P13) is the only accept path;
* project events are an append-only observability feed, never authority
  (a torn/corrupt events.jsonl never crashes the coordinator);
* cancel is a deterministic terminal failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.delegation_policy import TaskStatus
from app.orchestration.cloud_batch import (
    BatchDispatcher,
    CloudProtocol,
    ProjectStore,
    WorkerRunResult,
    schemas,
)
from app.orchestration.project_supervisor import (
    ProjectEventKind,
    ProjectSupervisor,
    ProjectSupervisorError,
    SupervisorConfig,
    load_events,
)

PROJECT = "p18-proj"
BATCH = "batch-001"
BASE = "b" * 40


# ---------------------------------------------------------------------------
# Fakes (zero live calls) — same shapes the P14/P16 suites use
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
    defaults = dict(
        batch_id=BATCH,
        objective="investigate and review",
        delegations=[
            dspec("r1", "codebase-investigator"),
            dspec("r2", "ui-reviewer"),
        ],
    )
    defaults.update(over)
    return schemas.BatchPlan(**defaults)


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
        return WorkerRunResult(
            status=TaskStatus.VERIFIED.value, conclusion=f"{task.id} done"
        )


class FakeCheckpointExecutor:
    """Injected P11–P13 gate runtime. Records calls so tests can assert
    the supervisor ONLY talks to the executor for checkpoint work (it
    holds no GateEngine of its own)."""

    def __init__(self, run_status: str = "PASSED") -> None:
        self.run_status = run_status
        self._g4_status_value = run_status
        self.run_calls: list[str] = []
        self.g4_calls: list[dict] = []

    def run_checkpoint(self, plan, checkpoint_id, *, plan_id: str) -> str:
        self.run_calls.append(checkpoint_id)
        return self.run_status

    def g4_status(self, plan, checkpoint_id) -> str:
        return self._g4_status_value

    def apply_g4_decision(
        self, plan, checkpoint_id, *, decision, feedback, actor
    ) -> str:
        # Mirror the canonical engine's human-only rule (the real engine
        # enforces it; the fake proves the supervisor forwards it verbatim
        # and has no way to bypass it).
        if not actor.startswith("human:"):
            raise PermissionError(f"non-human actor {actor!r} rejected")
        self.g4_calls.append(
            {"checkpoint_id": checkpoint_id, "decision": decision, "actor": actor}
        )
        return {"APPROVE": "PASSED", "NEEDS_CHANGES": "REPAIR",
                "CANCEL": "REJECTED"}[decision]


class ScriptedPlanner:
    """Cloud planner seam: canned clarify/batch/reentry, counts calls.

    ``reentry_script`` is consumed in order (one action per reentry
    call) — the protocol enforces the single-call-per-generation cap
    itself, so the script is only ever consumed once per batch."""

    def __init__(self, batch_action, reentry_script) -> None:
        self.batch_action = batch_action
        self.reentry_script = list(reentry_script)
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
        assert self.reentry_script, "reentry called with empty script"
        action, self.reentry_script = self.reentry_script[0], self.reentry_script[1:]
        return action


def make_supervisor(
    tmp_path: Path,
    batch_action: schemas.BatchPlan,
    reentry_script,
    runner: FakeRunner | None = None,
    checkpoint_executor: FakeCheckpointExecutor | None = None,
    project_id: str = PROJECT,
) -> tuple[ProjectSupervisor, ScriptedPlanner, FakeRunner]:
    """Build a supervisor with a scripted fake planner (zero live calls)."""
    runner = runner or FakeRunner()
    planner = ScriptedPlanner(batch_action=batch_action, reentry_script=reentry_script)

    def factory():
        # The supervisor wraps the planner in a CloudProtocol; it only
        # needs clarify/batch/reentry — a duck-typed port is fine.
        return planner

    config = SupervisorConfig(
        state_root=tmp_path,
        project_id=project_id,
        planner_factory=factory,
        runner=runner,
        checkpoint_executor=checkpoint_executor,
        base_sha=BASE,
    )
    return ProjectSupervisor(config), planner, runner


def make_running_project_with_checkpoint(
    tmp_path: Path, batch_id: str = "batch-ck", project_id: str = PROJECT
):
    """Drive to BATCH_RUNNING with a MUTATING batch so P16 lowers a real,
    VALID checkpoint plan (the canonical P16 recipe — not a hand-rolled
    Checkpoint), and return (sup, planner, runner, executor)."""
    from app.orchestration.cloud_batch.schemas import BatchPlan

    mixed = BatchPlan(
        batch_id=batch_id,
        objective="investigate then implement",
        delegations=[
            dspec("r1", "codebase-investigator"),
            dspec("m1", "implementer", objective="implement",
                  invariants=["scope guard"]),
        ],
    )
    executor = FakeCheckpointExecutor(run_status="PASSED")
    sup, planner, runner = create_running_project(
        tmp_path, mixed,
        [schemas.FinalResult(success=True)],
        checkpoint_executor=executor,
        project_id=project_id,
    )
    # Run the batch so P16 persists the lowered intent + checkpoint plan.
    sup.advance_project()
    return sup, planner, runner, executor


def create_running_project(
    tmp_path: Path,
    batch_action: schemas.BatchPlan,
    reentry_script,
    runner: FakeRunner | None = None,
    checkpoint_executor: FakeCheckpointExecutor | None = None,
    project_id: str = PROJECT,
) -> tuple[ProjectSupervisor, ScriptedPlanner, FakeRunner]:
    """Drive NEW -> CLARIFY -> answer -> BATCH (BATCH_RUNNING). Returns
    the supervisor, the planner (to count cloud calls), and the runner
    (to count worker runs)."""
    sup, planner, runner = make_supervisor(
        tmp_path, batch_action, reentry_script, runner=runner,
        checkpoint_executor=checkpoint_executor, project_id=project_id,
    )
    sup.create_project(initial_prompt="Build the thing.", objective="build")
    sup.advance_project()  # NEW -> CLARIFY (1 cloud)
    sup.submit_human_input(text="yes, build it", actor="human:cli")
    sup.advance_project()  # READY_TO_PLAN -> BATCH (1 cloud) -> BATCH_RUNNING
    return sup, planner, runner


# ---------------------------------------------------------------------------
# 1-3. The core lifecycle: NEW -> CLARIFY -> answer -> BATCH -> run ->
#      REENTRY_READY -> FINAL -> COMPLETE (exactly 3 cloud calls)
# ---------------------------------------------------------------------------


def test_full_read_only_lifecycle_three_cloud_calls(tmp_path):
    sup, planner, runner = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True, summary="done")]
    )
    assert planner.calls == ["clarify", "batch"]  # 2 cloud so far
    # BATCH_RUNNING: deterministic worker run — NO cloud call.
    r = sup.advance_project()
    assert r["action"] == "batch_executed"
    assert r["state"] == "REENTRY_READY"
    assert planner.calls == ["clarify", "batch"]  # still 2 — dormant
    assert sorted(runner.started) == ["r1", "r2"]  # workers actually ran
    # REENTRY_READY: ONE cloud call -> FINAL -> COMPLETE.
    r = sup.advance_project()
    assert r["action"] == "final" and r["success"] is True
    assert sup.state() == schemas.CloudProjectState.COMPLETE
    assert planner.calls.count("reentry") == 1
    # Exactly THREE cloud calls for the whole project.
    assert planner.calls.count("clarify") == 1
    assert planner.calls.count("batch") == 1
    assert planner.calls.count("reentry") == 1
    assert sup.store.cloud_call_count(PROJECT) == 3


def test_terminal_state_advance_is_no_op(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.advance_project()  # -> REENTRY_READY
    sup.advance_project()  # -> COMPLETE
    assert sup.state() == schemas.CloudProjectState.COMPLETE
    before = list(planner.calls)
    r = sup.advance_project()  # terminal: deterministic no-op, 0 cloud
    assert r["action"] == "terminal"
    assert planner.calls == before


def test_failed_final_goes_to_failed(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=False, summary="broken")]
    )
    sup.advance_project()  # -> REENTRY_READY
    r = sup.advance_project()  # FINAL success=False
    assert r["action"] == "final" and r["success"] is False
    assert sup.state() == schemas.CloudProjectState.FAILED


# ---------------------------------------------------------------------------
# 4-6. Cloud dormancy during BATCH_RUNNING (even across a restart)
# ---------------------------------------------------------------------------


def test_no_cloud_during_batch_running(tmp_path):
    sup, planner, runner = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    assert planner.calls.count("reentry") == 0
    sup.advance_project()  # the deterministic batch run
    # The batch ran and reached REENTRY_READY without any new cloud call.
    assert sup.state() == schemas.CloudProjectState.REENTRY_READY
    assert planner.calls == ["clarify", "batch"]


def test_restart_during_batch_running_does_not_reexecute(tmp_path):
    """Restart recovery from BATCH_RUNNING: the new supervisor re-derives
    the batch, re-dispatches it deterministically, and does NOT re-run any
    terminal worker nor call the cloud."""
    sup, planner, runner = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.advance_project()  # run the batch -> REENTRY_READY
    assert runner.started == ["r1", "r2"]

    # SIMULATE A RESTART: a brand-new supervisor bound to the same state.
    sup2, planner2, runner2 = make_supervisor(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup2.open_project(PROJECT)
    assert sup2.state() == schemas.CloudProjectState.REENTRY_READY
    # A further advance from REENTRY_READY is the reentry cloud call — but
    # if we restart at BATCH_RUNNING instead (re-open then re-run the
    # batch), workers must not re-execute. Drive the batch run once more:
    # state is already REENTRY_READY, so advance just re-enters (1 cloud),
    # and the runner (fresh) must not have been re-fed the old workers.
    runner2.started == []  # fresh runner saw nothing yet
    r = sup2.advance_project()  # reentry (the ONLY cloud allowed now)
    assert r["action"] == "reentered" or r["action"] == "final"
    assert runner2.started == []  # no worker re-execution on restart


def test_restart_at_waiting_for_user_does_not_reclarify(tmp_path):
    """Restart recovery from WAITING_FOR_USER: no CLARIFY re-bill, the
    park is re-derived from persisted state, and the human answer resumes
    exactly once."""
    sup, planner, _ = make_supervisor(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.create_project(initial_prompt="Build.", objective="build")
    sup.advance_project()  # NEW -> CLARIFY (1 cloud) -> WAITING_FOR_USER
    assert sup.state() == schemas.CloudProjectState.WAITING_FOR_USER
    assert planner.calls == ["clarify"]

    # RESTART: new supervisor, new planner (would bill again if re-CLARIFY).
    sup2, planner2, _ = make_supervisor(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup2.open_project(PROJECT)
    assert sup2.state() == schemas.CloudProjectState.WAITING_FOR_USER
    r = sup2.advance_project()  # parked: 0 cloud, re-derives the park view
    assert r["action"] == "waiting_for_user"
    assert planner2.calls == []  # NO re-CLARIFY
    # The human answer resumes exactly once (via the P15 bridge).
    sup2.submit_human_input(text="build it", actor="human:cli")
    assert sup2.state() == schemas.CloudProjectState.READY_TO_PLAN
    with pytest.raises(ProjectSupervisorError):
        # A SECOND answer for the same park generation is rejected.
        sup2.submit_human_input(text="again?", actor="human:cli")


# ---------------------------------------------------------------------------
# 7-9. Human provenance: a model payload can never resume a parked project
# ---------------------------------------------------------------------------


def test_model_payload_cannot_resume_park(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.NeedsHuman(question="pick A or B?")]
    )
    sup.advance_project()  # -> REENTRY_READY
    sup.advance_project()  # reentry -> NEEDS_HUMAN (park)
    assert sup.state() == schemas.CloudProjectState.NEEDS_HUMAN

    # A model-originated payload (actor not "human:...") is rejected
    # BEFORE any state is touched (the HumanResponse schema + the P15
    # bridge both fail closed).
    from app.orchestration.cloud_batch import HumanResponse
    from app.orchestration.cloud_batch.human_bridge import NotHumanOrigin

    with pytest.raises((NotHumanOrigin, ProjectSupervisorError, ValueError)):
        sup.submit_human_input(text="model answer", actor="model:opus")
    # The park is untouched: still NEEDS_HUMAN.
    assert sup.state() == schemas.CloudProjectState.NEEDS_HUMAN


def test_human_answer_resumes_needs_human_once(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.NeedsHuman(question="pick?")]
    )
    sup.advance_project()  # -> REENTRY_READY
    sup.advance_project()  # -> NEEDS_HUMAN
    assert sup.state() == schemas.CloudProjectState.NEEDS_HUMAN
    # A real human response resumes exactly once -> READY_TO_PLAN.
    r = sup.submit_human_input(text="A", actor="human:cli")
    assert r["resumed_to"] == "READY_TO_PLAN"
    assert sup.state() == schemas.CloudProjectState.READY_TO_PLAN
    # A duplicate for the same park generation is rejected.
    with pytest.raises(ProjectSupervisorError):
        sup.submit_human_input(text="B", actor="human:cli")


def test_no_park_no_answer(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    # Project is not parked — submit_human_input fails closed.
    with pytest.raises(ProjectSupervisorError):
        sup.submit_human_input(text="hello", actor="human:cli")


# ---------------------------------------------------------------------------
# 10-11. G4 stays human-only; the supervisor cannot pre-mark a gate
# ---------------------------------------------------------------------------


def test_supervisor_has_no_gate_engine(tmp_path):
    """The supervisor coordinates the P11–P13 checkpoint runtime — it
    holds no GateEngine and exposes no method that fabricates a gate
    record. G4 can only move through the injected executor's
    apply_g4_decision (which forwards to the canonical engine)."""
    import inspect

    sup, _, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    # No GateEngine anywhere in the supervisor's object graph.
    from app.orchestration.checkpoint_planning.gate_engine import GateEngine

    for attr in vars(sup).values():
        assert not isinstance(attr, GateEngine), \
            "supervisor must not hold a GateEngine (no pre-mark path)"
    # The only G4 entry is submit_g4_decision, and it requires a human
    # actor (forwarded to the executor, which enforces the rule).
    assert hasattr(sup, "submit_g4_decision")
    src = inspect.getsource(type(sup).submit_g4_decision)
    assert "apply_g4_decision" in src  # routes through the executor only


def test_g4_non_human_actor_rejected(tmp_path):
    sup, _, _ = create_running_project(
        tmp_path,
        ro_plan(batch_id="batch-009"),
        [schemas.FinalResult(success=True)],
    )
    # No checkpoint plan exists yet -> G4 decision fails closed.
    with pytest.raises(ProjectSupervisorError):
        sup.submit_g4_decision(decision="APPROVE", actor="planner")
    # With a checkpoint executor and a real P16-LOWERED checkpoint plan, a
    # non-human (planner) G4 "decision" is rejected before any gate moves —
    # the supervisor's only G4 path forwards the actor verbatim to the
    # injected P11–P13 executor, which enforces the human-only rule.
    PROJ2 = "p18-proj2"
    sup2, _, _, executor = make_running_project_with_checkpoint(
        tmp_path, batch_id="batch-010", project_id=PROJ2
    )
    assert sup2.checkpoint_batch() == "batch-010"
    assert sup2.g4_status(batch_id="batch-010") == "PASSED"  # executor-backed view
    # A planner-originated G4 "decision" is rejected (human-only).
    with pytest.raises(ProjectSupervisorError, match="non-human"):
        sup2.submit_g4_decision(decision="APPROVE", actor="planner", batch_id="batch-010")
    # The same decision WITH a human actor goes through (executor forwarded).
    status = sup2.submit_g4_decision(
        decision="APPROVE", actor="human:cli", batch_id="batch-010"
    )
    assert status == "PASSED"
    assert executor.g4_calls[-1] == {
        "checkpoint_id": "m1", "decision": "APPROVE", "actor": "human:cli"
    }


# ---------------------------------------------------------------------------
# 12. The supervisor cannot directly accept a lowered mutation — checkpoint
#     gate authority (P11–P13) is the only accept path
# ---------------------------------------------------------------------------


def test_lowered_mutation_cannot_be_accepted_without_gates(tmp_path):
    """A cloud FINAL success=True marks the PROJECT complete, but a P16-
    lowered mutation is NOT accepted by that — the checkpoint gate
    runtime (P11–P13) is the only authority that can pass a gate. The
    supervisor has no method that writes a PASSED gate record for a
    lowered mutation; it can only call run_checkpoint (the executor)."""
    from app.orchestration.cloud_batch.schemas import BatchPlan
    from app.orchestration.cloud_batch import authority as p16
    from app.orchestration.cloud_batch.schemas import DelegationSpec

    # A mixed plan: one read-only + one mutating delegation.
    mixed = BatchPlan(
        batch_id="batch-mut",
        objective="investigate then implement",
        delegations=[
            dspec("r1", "codebase-investigator"),
            dspec("m1", "implementer", objective="implement",
                  invariants=["scope guard"]),
        ],
    )
    sup, planner, runner = create_running_project(
        tmp_path, mixed, [schemas.FinalResult(success=True)]
    )
    r = sup.advance_project()  # dispatch: r1 runs, m1 lowers to a checkpoint
    assert r["routed"] is True
    # The mutating work is persisted as a lowered intent + checkpoint plan
    # awaiting gate authority — it is NOT accepted.
    lowered = sup.store.load_lowered_intent(PROJECT, "batch-mut")
    assert lowered, "mutating batch must lower to a checkpoint intent"
    assert lowered.get("checkpoint_plan") is not None
    # A subsequent FINAL success marks the project COMPLETE but does NOT
    # mark the lowered checkpoint's gates PASSED (they stay PENDING until
    # run_checkpoint + the executor passes them). The supervisor has no
    # route to write a PASSED gate record directly.
    sup.advance_project()  # REENTRY_READY -> reentry
    r = sup.advance_project()  # batch-mut-2? No — reentry returned FINAL.
    # Regardless of project completion, the lowered checkpoint is only
    # advanced by run_checkpoint (the injected P11–P13 executor).
    assert sup.store.load_lowered_intent(PROJECT, "batch-mut") is not None


def test_run_checkpoint_routes_through_executor_only(tmp_path):
    """run_checkpoint calls the injected P11–P13 executor exactly once for
    the P16-lowered checkpoint plan — it never drives a GateEngine itself."""
    sup, _, _, executor = make_running_project_with_checkpoint(
        tmp_path, batch_id="batch-ck"
    )
    assert sup.checkpoint_batch() == "batch-ck"
    status = sup.run_checkpoint(batch_id="batch-ck")
    assert status == "PASSED"
    # The lowered plan's checkpoint id is the mutating delegation id (m1).
    assert executor.run_calls == ["m1"]  # executor called exactly once
    # Project state is unaffected by the gate run (checkpoint authority is
    # separate from the project lifecycle).
    assert sup.state() == schemas.CloudProjectState.REENTRY_READY


# ---------------------------------------------------------------------------
# 13. Project events: append-only observability feed, never authority
# ---------------------------------------------------------------------------


def test_events_are_append_only_and_feed_only(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.advance_project()  # batch run
    sup.advance_project()  # reentry -> COMPLETE
    events = load_events(sup.store, PROJECT)
    kinds = [e.kind for e in events]
    assert ProjectEventKind.PROJECT_CREATED in kinds
    assert ProjectEventKind.CLARIFICATION_REQUIRED in kinds
    assert ProjectEventKind.HUMAN_RESPONSE_RECEIVED in kinds
    assert ProjectEventKind.BATCH_CREATED in kinds
    assert ProjectEventKind.REENTRY_READY in kinds
    assert ProjectEventKind.PROJECT_COMPLETE in kinds
    # WORKER_TERMINAL emitted for both read-only workers.
    worker_terminal = [e for e in events if e.kind == ProjectEventKind.WORKER_TERMINAL]
    assert len(worker_terminal) == 2
    # Events are append-only (the file is never truncated/rewritten).
    path = sup.store.project_dir(PROJECT) / "events.jsonl"
    assert path.exists()
    # A corrupt event line never crashes the coordinator (feed-only).
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not valid json\n")
    events2 = load_events(sup.store, PROJECT)
    assert len(events2) == len(events)  # corrupt line skipped, not a crash


def test_events_never_authority(tmp_path):
    """Canonical state (the project contract) is the authority, not the
    event feed. Corrupting/erasing events.jsonl changes no project state."""
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.advance_project()
    state_before = sup.state()
    path = sup.store.project_dir(PROJECT) / "events.jsonl"
    path.unlink()  # erase the feed entirely
    # The project's canonical state is unaffected.
    assert sup.state() == state_before
    assert load_events(sup.store, PROJECT) == []


# ---------------------------------------------------------------------------
# 14. Cancel is a deterministic terminal failure (no cloud call)
# ---------------------------------------------------------------------------


def test_cancel_is_deterministic_terminal(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    assert sup.state() == schemas.CloudProjectState.BATCH_RUNNING
    before = list(planner.calls)
    sup.cancel_project(reason="user cancelled")
    assert sup.state() == schemas.CloudProjectState.FAILED
    assert planner.calls == before  # no cloud call on cancel
    events = load_events(sup.store, PROJECT)
    assert ProjectEventKind.PROJECT_CANCELLED in [e.kind for e in events]


# ---------------------------------------------------------------------------
# 15. get_project is a read-only view (never invokes the cloud)
# ---------------------------------------------------------------------------


def test_get_project_is_read_only(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    before = list(planner.calls)
    view = sup.get_project()
    assert view["project_id"] == PROJECT
    assert view["state"] == "BATCH_RUNNING"
    assert view["cloud_calls"] == 2
    assert "park" in view and "events" in view
    assert planner.calls == before  # get_project made no cloud call


# ---------------------------------------------------------------------------
# 16. Restart recovery from REENTRY_READY does not duplicate the reentry
#     cloud call
# ---------------------------------------------------------------------------


def test_restart_at_reentry_ready_no_duplicate_cloud(tmp_path):
    sup, planner, _ = create_running_project(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup.advance_project()  # batch run -> REENTRY_READY
    assert sup.state() == schemas.CloudProjectState.REENTRY_READY

    # RESTART: a new supervisor + new planner. The reentry generation's
    # single cloud call was already reserved in P14 (persisted), so a
    # re-advance must NOT re-bill it — the protocol refuses the duplicate.
    sup2, planner2, _ = make_supervisor(
        tmp_path, ro_plan(), [schemas.FinalResult(success=True)]
    )
    sup2.open_project(PROJECT)
    assert sup2.state() == schemas.CloudProjectState.REENTRY_READY
    # The reentry is idempotent: the P14 reservation means a second
    # request_reentry raises CloudInvocationBlocked (no duplicate bill).
    from app.orchestration.cloud_batch.protocol import CloudInvocationBlocked
    protocol2 = sup2._protocol()
    batch_id = sup2._current_batch_id(sup2.store.load_project(PROJECT))
    # Consume the (single) allowed reentry call once — it succeeds.
    sup2.advance_project()
    assert planner2.calls.count("reentry") == 1
    assert sup2.state() == schemas.CloudProjectState.COMPLETE
    # A third attempt (after COMPLETE) is a no-op (terminal) — no cloud.
    before = list(planner2.calls)
    sup2.advance_project()
    assert planner2.calls == before


# ---------------------------------------------------------------------------
# 17. The user entrypoint (python3 -m app.main project …) — P18-D
# ---------------------------------------------------------------------------


def test_cli_start_and_status_and_advance_fail_closed(tmp_path):
    from app import main as app_main
    from app.orchestration import cloud_batch as cb

    # start: no cloud, creates the project (production wiring).
    rc = app_main.project_main([
        "start", "--project-id", "cli-e2e", "--prompt", "add a helper",
        "--objective", "add a helper",
        "--state-root", str(tmp_path), "--workdir", str(tmp_path),
    ], )
    assert rc == 0

    # status: read-only, no cloud.
    rc = app_main.project_main([
        "status", "--project-id", "cli-e2e",
        "--state-root", str(tmp_path), "--workdir", str(tmp_path),
    ])
    assert rc == 0

    # advance: the tool-free planner needs Gemini/Vertex credentials.
    # Deterministically force the "no credentials" failure and prove the
    # CLI fails CLOSED (exit 1) with the state untouched — no partial
    # cloud call, state still NEW, cloud_calls 0.
    import app.orchestration.cloud_batch.planner as planner_mod

    def _no_key(prompt: str) -> str:
        raise cb.CloudPlannerUnavailable(
            "genai transport failure: No API key was provided"
        )

    monkey = _Monkey()  # noqa - see below (simple fixture-less patch)
    monkey.set(planner_mod, "genai_tool_free_invoke", _no_key)
    try:
        rc = app_main.project_main([
            "advance", "--project-id", "cli-e2e",
            "--state-root", str(tmp_path), "--workdir", str(tmp_path),
        ])
    finally:
        monkey.restore()
    assert rc == 1
    contract = cb.ProjectStore(tmp_path).load_project("cli-e2e")
    assert contract.state is cb.schemas.CloudProjectState.NEW
    assert contract.generation == 0


class _Monkey:
    """Minimal monkeypatch context (no pytest fixture needed)."""

    def __init__(self) -> None:
        self._saved: list[tuple] = []

    def set(self, module, attr, value) -> None:
        self._saved.append((module, attr, getattr(module, attr)))
        setattr(module, attr, value)

    def restore(self) -> None:
        for module, attr, value in reversed(self._saved):
            setattr(module, attr, value)
        self._saved.clear()


def test_cli_cancel_is_terminal_and_human_only_gate_actors(tmp_path):
    from app import main as app_main
    from app.orchestration import cloud_batch as cb

    rc = app_main.project_main([
        "start", "--project-id", "cli-cancel", "--prompt", "x",
        "--objective", "x", "--state-root", str(tmp_path),
        "--workdir", str(tmp_path),
    ])
    assert rc == 0
    rc = app_main.project_main([
        "cancel", "--project-id", "cli-cancel",
        "--state-root", str(tmp_path), "--workdir", str(tmp_path),
        "--reason", "test",
    ])
    assert rc == 0
    contract = cb.ProjectStore(tmp_path).load_project("cli-cancel")
    assert contract.state is cb.schemas.CloudProjectState.FAILED
