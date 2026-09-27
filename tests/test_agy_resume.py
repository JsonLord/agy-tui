"""
P6 resume/recovery tests — network-free, everything injected
(contract §6, ``tests/test_agy_resume.py``). The stage machinery is
P3's real ``execute_stage`` + P6's real recovery/boundary code driven
by fake executors; NO network, NO subprocess, NO model, NO sleep.
Every resume test injects ``session_reusable`` so no check ever hits
the (possibly live) OpenCode server.

15-case contract item -> test id map:

 ====  ==============================================================
 item  test id
 ====  ==============================================================
  1    TestRestart01FreshRun.test_item01_restart_before_any_task_full_plan_runs
  2    TestRestartPendingStage.test_item02_stage1_accepted_stage2_pending_skips_stage1
  3    TestRestartPendingStage.test_item03_restart_before_verification_reruns_verification
  4    TestRestartGates.test_item04_restart_after_verification_reruns_gates_without_redispatch
  5    TestRestartGates.test_item05_restart_after_commit_runs_only_supervisor_gate
  6    TestAcceptedCarry.test_item06_accepted_stage_carried_forward_verbatim_same_sha
  7    TestAcceptedCarry.test_item07_accepted_stage_never_re_executed_raising_executor
  8    TestRestartGates.test_item08_plan_level_cumulative_suite_runs_on_resume
 10/session boundaries.
  9    TestSessionRecovery.test_item09_persisted_session_reused_as_known
 10    TestSessionRecovery.test_item10_unrecoverable_session_records_continuation
 11    TestCommitGateEvidence.test_item11_commit_gate_record_reload_and_corrupt
 12    TestCommitGateEvidence.test_item12_resume_creates_no_duplicate_commits
 13    TestPlanRepair.test_item13_plan_repair_state_survives_restart_bounded
 14    TestPlanRepair.test_item14_failover_events_carried_unchanged
 15    TestTerminalIdempotence.test_item15_plan_accepted_is_terminal_and_idempotent
 ====  ==============================================================
"""

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.config import AgySettings
from app.orchestration.agy_commitgate import (
    build_commit_gate_record,
    load_commit_gate_records,
    persist_commit_gate_record,
)
from app.orchestration.agy_execute import STAGE_ACCEPTED, STAGE_FAILED
from app.orchestration.agy_plan import (
    PLAN_ACCEPTED,
    PLAN_FAILED,
    PLAN_IN_PROGRESS,
    PLAN_REPAIR,
    execute_plan,
)
from app.orchestration.agy_resume import (
    PLAN_PENDING,
    STAGE_COMMITTED,
    TASK_RUNNING,
    TASK_VERIFIED,
    load_plan_document,
    recovery_state,
    resume_plan,
)
from app.orchestration.agy_runstate import PlanRunState, StageRunState
from app.orchestration.agy_session import TaskSessionLog
from app.orchestration.agy_timeline import Timeline
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage

FINAL_CMD = "python3 plan-final-acceptance"
CUM_CMD = "python3 plan-cumulative-suite"
BASELINE_FILES = ["calculator.py", "tests/test_calculator.py"]
SETTINGS = AgySettings.from_env({})
RESUME_SHA_SEED = 0x1000  # resume harness commits never collide with run 1


def install_host_runtime_stub() -> None:
    """Host-runtime stub, same as the other P3-P6 test modules."""
    if "google.antigravity" in sys.modules:
        return
    try:
        spec = importlib.util.find_spec("google.antigravity")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is not None:
        return
    import google

    module = types.ModuleType("google.antigravity")
    module.Agent = object
    module.LocalOpenAIAgentConfig = object
    module.types = types.SimpleNamespace()
    sys.modules["google.antigravity"] = module
    google.antigravity = module  # type: ignore[attr-defined]


install_host_runtime_stub()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_stage(stage_id: str, dependencies: list[str]) -> PlanStage:
    command = f"python3 stage-gate {stage_id}"
    return PlanStage(
        stage_id=stage_id,
        objective=f"Implement the {stage_id} slice",
        dependencies=list(dependencies),
        allowed_files=list(BASELINE_FILES),
        implementation_context=(
            f"Implement {stage_id} in calculator.py with a "
            "deterministic unittest case."
        ),
        acceptance_commands=[
            AcceptanceCommand(
                command=command,
                expected_exit_code=0,
                description=f"{stage_id} stage gate",
            )
        ],
        acceptance_conditions=[f"{stage_id} behaves as specified"],
        cumulative_invariants=["add(a, b) still returns a + b"],
    )


def make_plan(plan_id: str = "agp-p6-resume") -> DevelopmentPlan:
    return DevelopmentPlan(
        plan_id=plan_id,
        objective="extend the calculator with deterministic tests",
        stages=[make_stage("s1", []), make_stage("s2", ["s1"])],
        final_acceptance=[
            AcceptanceCommand(
                command=FINAL_CMD,
                expected_exit_code=0,
                description="plan final acceptance",
            )
        ],
    )


def run(coro):
    return asyncio.run(coro)


class ResumeHarness:
    """
    Deterministic environment recording exactly what every injected
    seam was asked to do (the P5 PlanHarness pattern, extended with
    per-sha seeding + a make_executor trap for accepted stages).
    """

    def __init__(
        self,
        *,
        decisions: list[str] | None = None,
        stage_validate: dict[str, bool] | None = None,
        emit_event: dict | None = None,
        session_source: str | None = "readback",
        raise_on: set[str] | None = None,
        sha_seed: int = 0,
    ) -> None:
        self.decisions = list(decisions or ["ACCEPT"])
        self.stage_validate = dict(stage_validate or {})
        self.emit_event = emit_event
        self.session_source = session_source
        self.raise_on = set(raise_on or ())
        self.sha_seed = sha_seed

        self.built_stages: list[str] = []
        self.executed: list[tuple[str, str, int]] = []
        self.validated: list[str] = []
        self.command_calls: list[tuple[str, int]] = []
        self.review_calls: list[dict] = []
        self.commit_calls: list[str] = []
        self.commit_count = 0
        self.session_logs: dict[str, TaskSessionLog] = {}
        self._session_counter = 0
        self._emitted_event = False

    # -- seams ---------------------------------------------------------------

    def make_executor(self, plan, stage):
        if stage.stage_id in self.raise_on:
            raise RuntimeError(
                f"executor must not be built for accepted stage "
                f"{stage.stage_id}"
            )
        self.built_stages.append(stage.stage_id)
        log = TaskSessionLog()
        self.session_logs[stage.stage_id] = log
        harness = self

        async def execute_task(task, attempt, repair_context):
            harness._session_counter += 1
            if harness.session_source:
                session_id = f"ses_{harness._session_counter:012x}"
                log.record(task.id, session_id, harness.session_source)
            harness.executed.append((stage.stage_id, task.id, attempt))
            return f"worker prose: implemented {task.id}"

        async def validate_task(task):
            harness.validated.append(task.id)
            ok = harness.stage_validate.get(task.id, True)
            return ok, ("verification passed" if ok else "verification broke")

        def collect_changed_files(task=None):
            return list(BASELINE_FILES)

        async def executor_run_command(command):
            harness.command_calls.append(
                (command.command, command.expected_exit_code)
            )
            return 0, "stage-local runner (unused by the driver)"

        setattr(execute_task, "session_log", log)
        return execute_task, validate_task, collect_changed_files, executor_run_command

    async def run_command(self, command):
        self.command_calls.append(
            (command.command, command.expected_exit_code)
        )
        return 0, "all green"

    def make_commit(self, evidence):
        self.commit_count += 1
        self.commit_calls.append(evidence["stage_id"])
        sha = f"{self.sha_seed + self.commit_count:040x}"
        return {
            "plan_id": evidence["plan_id"],
            "stage_id": evidence["stage_id"],
            "parent_sha": f"{self.sha_seed + self.commit_count - 1:040x}",
            "commit_sha": sha,
            "changed_files": list(evidence["changed_files"]),
            "message": (
                f"AGY stage {evidence['stage_id']}: "
                f"{evidence.get('stage_objective', '')}"
            ),
        }

    async def review(self, evidence=None, on_failover_event=None):
        self.review_calls.append(
            {
                "stage_id": (evidence or {}).get("stage_id"),
                "evidence": evidence,
            }
        )
        if self.emit_event is not None and not self._emitted_event:
            self._emitted_event = True
            return {
                "decision": "ACCEPT",
                "rationale": "green (with one transient noted)",
                "repair_targets": [],
                "model": "supervisor-role-model",
                "failover_event": dict(self.emit_event),
            }
        decision = (
            self.decisions.pop(0)
            if len(self.decisions) > 1
            else self.decisions[0]
        )
        return {
            "decision": decision,
            "rationale": f"supervisor reason: {decision}",
            "repair_targets": (
                ["make the failing check deterministic"]
                if decision == "REPAIR"
                else []
            ),
            "model": "supervisor-role-model",
            "failover_event": None,
        }

    # -- drivers -------------------------------------------------------------

    def execute(self, plan, run_state, *, timeline=None):
        return run(
            execute_plan(
                plan,
                settings=SETTINGS,
                repo="/nonexistent-agy-p6-resume",
                make_executor=self.make_executor,
                run_command=self.run_command,
                cumulative_suite=[
                    AcceptanceCommand(
                        command=CUM_CMD,
                        expected_exit_code=0,
                        description="plan-level cumulative suite",
                    )
                ],
                make_commit=self.make_commit,
                supervisor_review=self.review,
                run_state=run_state,
                timeline=timeline,
            )
        )

    def resume(
        self,
        plan,
        run_state,
        *,
        timeline=None,
        session_reusable=None,
    ):
        # Every resume test injects the session check: zero network.
        if session_reusable is None:
            session_reusable = lambda sid: False  # noqa: E731
        return run(
            resume_plan(
                plan,
                settings=SETTINGS,
                repo="/nonexistent-agy-p6-resume",
                make_executor=self.make_executor,
                run_command=self.run_command,
                cumulative_suite=[
                    AcceptanceCommand(
                        command=CUM_CMD,
                        expected_exit_code=0,
                        description="plan-level cumulative suite",
                    )
                ],
                make_commit=self.make_commit,
                supervisor_review=self.review,
                run_state=run_state,
                timeline=timeline,
                session_reusable=session_reusable,
            )
        )


def command_count(harness: ResumeHarness, command: str) -> int:
    return sum(
        1 for name, _ in harness.command_calls if name == command
    )


def new_plan_dir(tmp: str) -> PlanRunState:
    return PlanRunState(Path(tmp) / "state" / "runs", "agp-p6-resume")


def stage_gate(run_state: PlanRunState, stage_id: str) -> str:
    return f"python3 stage-gate {stage_id}"


# ---------------------------------------------------------------------------
# Crafted persisted boundaries (what a kill looks like on disk)
# ---------------------------------------------------------------------------


def craft_task_running(
    run_state: PlanRunState, stage_id: str = "s1",
    session_id: str | None = None,
) -> None:
    """Tasks persisted, verification evidence missing/incomplete."""
    view = StageRunState(run_state.root, run_state.plan_id, stage_id)
    record = {
        "task_id": f"{stage_id}-t00",
        "worker_task_id": f"{stage_id}-t00",
        "attempt": 1,
        "status": "SUCCESS",
        "changed_files": ["calculator.py"],
        "session_source": "readback",
        "failure_reason": "",
    }
    if session_id:
        record["session_id"] = session_id
    view.save_task(record)


def craft_task_verified(
    run_state: PlanRunState, stage_id: str = "s1"
) -> None:
    """Tasks + full verification persisted; stage verdict NOT."""
    view = StageRunState(run_state.root, run_state.plan_id, stage_id)
    view.save_task(
        {
            "task_id": f"{stage_id}-t00",
            "worker_task_id": f"{stage_id}-t00",
            "attempt": 1,
            "status": "SUCCESS",
            "validation_ok": True,
            "validation_output": "verification passed",
            "changed_files": ["calculator.py"],
            "session_id": f"ses_{stage_id}00000000000000000000",
            "session_source": "stdout",
            "failure_reason": "",
        }
    )


def demote_to_committed(run_state: PlanRunState, stage_id: str) -> None:
    """
    Kill shape: commit.json exists, stage.json exists WITHOUT a
    supervisor decision (contract item 5's persisted wording).
    """
    view = StageRunState(run_state.root, run_state.plan_id, stage_id)
    payload = view.load_stage()
    payload.pop("supervisor_decision", None)
    payload.pop("status", None)
    view.save_stage(payload)


def drop_stage(run_state: PlanRunState, stage_id: str) -> None:
    view = StageRunState(run_state.root, run_state.plan_id, stage_id)
    shutil.rmtree(view.stage_dir)
    payload = run_state.load_plan()
    payload["stage_results"].pop(stage_id, None)
    payload.pop("stopped_at", None)
    run_state.save_plan(payload)


def set_plan_status(run_state: PlanRunState, status: str, **extra) -> None:
    payload = run_state.load_plan()
    payload["status"] = status
    payload.update(extra)
    run_state.save_plan(payload)


def run_until_stage2_fails(
    plan: DevelopmentPlan, run_state: PlanRunState
) -> tuple[ResumeHarness, object]:
    """First process: s1 accepted, s2 deterministically STAGE_FAILED."""
    harness = ResumeHarness(stage_validate={"s2-t00": False})
    result = harness.execute(plan, run_state)
    assert result.status == PLAN_FAILED, result.status
    assert result.stopped_at == "s2"
    assert harness.execute.__self__ is harness  # sanity
    assert plan.stage_execution_order()[0].stage_id == "s1"
    stage_state = StageRunState(run_state.root, plan.plan_id, "s1")
    s1 = stage_state.load_stage()
    assert s1["status"] == STAGE_ACCEPTED, s1["status"]
    return harness, result


def run_accepted_before_plan_gate(
    plan: DevelopmentPlan, run_state: PlanRunState
) -> ResumeHarness:
    """First process: BOTH stages accepted, killed before the plan gate."""
    harness = ResumeHarness()
    result = harness.execute(plan, run_state)
    assert result.status == PLAN_ACCEPTED, result.status
    set_plan_status(run_state, PLAN_IN_PROGRESS)
    return harness


# ---------------------------------------------------------------------------
# Cases 1-3
# ---------------------------------------------------------------------------


class TestRestart01FreshRun(unittest.TestCase):
    def test_item01_restart_before_any_task_full_plan_runs(self):
        """(1) Fresh run state -> resume -> full plan runs: executor
        called for every stage, terminal status reached."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            # NOT even plan.json exists: pure "restart before any task".
            self.assertIsNone(run_state.load_plan())
            self.assertEqual(
                recovery_state("s1", run_state), PLAN_PENDING
            )

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            self.assertEqual(harness.built_stages, ["s1", "s2"])
            self.assertEqual(
                [t for _stage, t, _a in harness.executed].count(
                    "s1-t00"
                ),
                1,
            )
            self.assertEqual(harness.commit_count, 2)
            persisted = run_state.load_plan()
            self.assertEqual(persisted["status"], PLAN_ACCEPTED)
            # The original plan document was persisted at plan start.
            self.assertIsNotNone(run_state.load_plan_doc())
            reloaded = load_plan_document(run_state)
            self.assertEqual(reloaded.plan_id, plan.plan_id)
            self.assertEqual(
                reloaded.model_dump(), plan.model_dump()
            )


class TestRestartPendingStage(unittest.TestCase):
    def test_item02_stage1_accepted_stage2_pending_skips_stage1(self):
        """(2) s1 accepted + s2 still failing/entry -> resume runs
        stage 2 onward; s1's executor is NEVER called."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            _first, _first_result = run_until_stage2_fails(
                plan, run_state
            )

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            # Item 2: stage 1's executor never called on resume.
            self.assertNotIn("s1", harness.built_stages)
            self.assertEqual(harness.built_stages, ["s2"])
            self.assertNotIn(
                ("s1", "s1-t00", 1), harness.executed
            )
            # s1's persisted work carried forward.
            persisted = run_state.load_plan()
            self.assertEqual(persisted["status"], PLAN_ACCEPTED)
            self.assertEqual(
                persisted["stage_results"]["s1"]["status"],
                STAGE_ACCEPTED,
            )
            # No supervisor call for the carried stage.
            s1_reviews = [
                call for call in harness.review_calls
                if call["stage_id"] == "s1"
            ]
            self.assertEqual(s1_reviews, [])

    def test_item03_restart_before_verification_reruns_verification(
        self,
    ):
        """(3) Tasks persisted WITHOUT verification evidence ->
        resume RUNS VERIFICATION (fake validator called) and the
        stage completes."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            craft_task_running(run_state, "s1")
            self.assertEqual(
                recovery_state("s1", run_state), TASK_RUNNING
            )

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            # The fake verifier ran for the incomplete task.
            self.assertIn("s1-t00", harness.validated)
            # The stage ran to completion with a commit + verdict.
            view = StageRunState(run_state.root, plan.plan_id, "s1")
            self.assertEqual(
                view.load_stage()["status"], STAGE_ACCEPTED
            )
            self.assertIsNotNone(view.load_commit())
            self.assertIn("s1", harness.built_stages)


# ---------------------------------------------------------------------------
# Cases 4, 5, 8 (gate-level boundaries + plan gate)
# ---------------------------------------------------------------------------


class TestRestartGates(unittest.TestCase):
    def test_item04_restart_after_verification_reruns_gates_without_redispatch(
        self,
    ):
        """(4) TASK_VERIFIED: acceptance/gate rerun WITHOUT
        re-dispatching the task (executor not called again)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            craft_task_verified(run_state, "s1")
            self.assertEqual(
                recovery_state("s1", run_state), TASK_VERIFIED
            )

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            # Executor NOT called for the verified stage's tasks:
            self.assertNotIn(
                ("s1", "s1-t00", 1), harness.executed
            )
            self.assertEqual(
                [stage for stage, _t, _a in harness.executed], ["s2"]
            )
            self.assertEqual(harness.built_stages, ["s2"])
            # ...but the stage acceptance + cumulative gates DID rerun:
            self.assertEqual(
                command_count(harness, stage_gate(run_state, "s1")), 1
            )
            self.assertGreaterEqual(command_count(harness, CUM_CMD), 1)
            # The gate path went through the supervisor + ONE new commit.
            s1_reviews = [
                call for call in harness.review_calls
                if call["stage_id"] == "s1"
            ]
            self.assertEqual(len(s1_reviews), 1)
            self.assertEqual(harness.commit_calls.count("s1"), 1)
            view = StageRunState(run_state.root, plan.plan_id, "s1")
            self.assertEqual(
                view.load_stage()["status"], STAGE_ACCEPTED
            )

    def test_item05_restart_after_commit_runs_only_supervisor_gate(self):
        """(5) STAGE_COMMITTED -> ONLY supervisor_review (once), no
        executor, no run_command for that stage, no new commit."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            h1 = ResumeHarness()
            first = h1.execute(plan, run_state)
            self.assertEqual(first.status, PLAN_ACCEPTED)
            persisted_s1_sha = first.stage_results["s1"]["commit"][
                "commit_sha"
            ]

            # Craft the kill: supervisor never decided; s2 not run.
            demote_to_committed(run_state, "s1")
            drop_stage(run_state, "s2")
            set_plan_status(run_state, PLAN_IN_PROGRESS)
            self.assertEqual(
                recovery_state("s1", run_state), STAGE_COMMITTED
            )

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            built_for_s1 = [
                stage for stage in harness.built_stages if stage == "s1"
            ]
            self.assertEqual(built_for_s1, [])  # no executor at all
            self.assertNotIn(("s1", "s1-t00", 1), harness.executed)
            s1_reviews = [
                call for call in harness.review_calls
                if call["stage_id"] == "s1"
            ]
            self.assertEqual(len(s1_reviews), 1)  # supervisor once
            # No stage acceptance/cumulative commands for s1 either:
            self.assertEqual(
                command_count(harness, stage_gate(run_state, "s1")), 0
            )
            # No new commit for s1: only s2 committed in resume.
            self.assertEqual(harness.commit_calls, ["s2"])
            self.assertEqual(harness.commit_count, 1)
            # The persisted commit sha is untouched.
            view = StageRunState(run_state.root, plan.plan_id, "s1")
            self.assertEqual(
                view.load_commit()["commit_sha"], persisted_s1_sha
            )

    def test_item08_plan_level_cumulative_suite_runs_on_resume(self):
        """(8) On resume with every stage already accepted, the
        PLAN-LEVEL cumulative suite runs (run_command count)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            run_accepted_before_plan_gate(plan, run_state)

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            # No stage machinery ran => every command call below is
            # attributable to the plan-level gate.
            self.assertEqual(harness.built_stages, [])
            self.assertEqual(harness.executed, [])
            self.assertEqual(command_count(harness, FINAL_CMD), 1)
            self.assertEqual(command_count(harness, CUM_CMD), 1)
            self.assertTrue(result.final_acceptance["ok"])
            self.assertTrue(result.cumulative_invariants_ok)


# ---------------------------------------------------------------------------
# Cases 6-7: accepted carry-forward
# ---------------------------------------------------------------------------


class TestAcceptedCarry(unittest.TestCase):
    def test_item06_accepted_stage_carried_forward_verbatim_same_sha(self):
        """(6) STAGE_ACCEPTED carried verbatim: the SAME commit SHA
        appears in the final result; supervisor not called for it."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            first, first_result = run_until_stage2_fails(
                plan, run_state
            )
            persisted_s1_sha = first_result.stage_results["s1"][
                "commit"
            ]["commit_sha"]

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            carried = result.stage_results["s1"]
            self.assertEqual(carried["status"], STAGE_ACCEPTED)
            self.assertEqual(
                carried["commit"]["commit_sha"], persisted_s1_sha
            )
            # Verbatim: every s1 field from run 1 is unchanged.
            for key, value in first_result.stage_results[
                "s1"
            ].items():
                self.assertEqual(carried[key], value, key)
            self.assertEqual(
                [
                    call for call in harness.review_calls
                    if call["stage_id"] == "s1"
                ],
                [],
            )

    def test_item07_accepted_stage_never_re_executed_raising_executor(
        self,
    ):
        """(7) An executor that RAISES for the accepted stage id:
        resume completes cleanly (it is never built for s1)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            run_until_stage2_fails(plan, run_state)

            harness = ResumeHarness(
                sha_seed=RESUME_SHA_SEED, raise_on={"s1"}
            )
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            self.assertNotIn("s1", harness.built_stages)
            self.assertIn("s2", harness.built_stages)


# ---------------------------------------------------------------------------
# Cases 9-10: session recovery (injected checks, zero network)
# ---------------------------------------------------------------------------


class TestSessionRecovery(unittest.TestCase):
    def _craft_partial_stage(self, run_state) -> None:
        craft_task_running(
            run_state, "s1", session_id="ses_persisted_one"
        )

    def test_item09_persisted_session_reused_as_known(self):
        """(9) Fake server check says reusable -> the persisted id
        RESTORED as the known session (source 'known'); no
        SESSION_CONTINUATION event."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            self._craft_partial_stage(run_state)
            timeline = Timeline(run_state.plan_dir / "timeline.jsonl")

            checked: list[str] = []

            def fake_reusable(session_id: str) -> bool:
                checked.append(session_id)
                return session_id == "ses_persisted_one"

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(
                plan,
                run_state,
                timeline=timeline,
                session_reusable=fake_reusable,
            )

            self.assertEqual(result.status, PLAN_ACCEPTED)
            # The check ran with the persisted id (injected, no network).
            self.assertEqual(checked, ["ses_persisted_one"])

            view = StageRunState(run_state.root, plan.plan_id, "s1")
            records = view.load_tasks()
            self.assertEqual(len(records), 1)
            self.assertEqual(
                records[0]["session_id"], "ses_persisted_one"
            )
            self.assertEqual(records[0]["session_source"], "known")
            self.assertNotIn("parent_session_id", records[0])
            # No continuation event: continuity was real.
            self.assertEqual(
                timeline.events_of("SESSION_CONTINUATION"), []
            )

    def test_item10_unrecoverable_session_records_continuation(self):
        """(10) Fake server check says absent -> FRESH session used
        AND a SESSION_CONTINUATION timeline event + parent_session_id
        recorded (no silent continuity)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            self._craft_partial_stage(run_state)
            timeline = Timeline(run_state.plan_dir / "timeline.jsonl")

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(
                plan,
                run_state,
                timeline=timeline,
                session_reusable=lambda session_id: False,  # absent
            )

            self.assertEqual(result.status, PLAN_ACCEPTED)

            # The explicit continuation event carries the parent id.
            events = timeline.events_of("SESSION_CONTINUATION")
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["parent_session_id"], "ses_persisted_one"
            )
            self.assertEqual(events[0]["stage_id"], "s1")
            self.assertTrue(events[0]["continuation_reason"])

            view = StageRunState(run_state.root, plan.plan_id, "s1")
            record = view.load_tasks()[0]
            # Lineage in the task record itself...
            self.assertEqual(
                record["parent_session_id"], "ses_persisted_one"
            )
            self.assertTrue(record["continuation_reason"])
            # ...and a FRESH session really was used (harness recorded
            # one during the re-dispatch): never the stale parent.
            fresh = record.get("session_id")
            self.assertIsNotNone(fresh)
            self.assertNotEqual(fresh, "ses_persisted_one")
            self.assertEqual(record["session_source"], "readback")


# ---------------------------------------------------------------------------
# Cases 11-12: commit-gate evidence under resume
# ---------------------------------------------------------------------------


class TestCommitGateEvidence(unittest.TestCase):
    _CG_PLAN_ID = "agp-p6-cg-resume"

    def _plan_and_stage(self):
        stage = make_stage("s1", [])
        return make_plan(self._CG_PLAN_ID), stage

    def _stage_payload(self, sha: str) -> dict:
        return {
            "plan_id": self._CG_PLAN_ID,
            "stage_id": "s1",
            "status": STAGE_ACCEPTED,
            "task_results": [
                {
                    "worker_task_id": "s1-t00",
                    "task_id": "s1-t00",
                    "attempt": 1,
                    "status": "SUCCESS",
                    "validation_ok": True,
                    "changed_files": ["calculator.py"],
                    "session_id": "ses_cg",
                    "session_source": "readback",
                }
            ],
            "changed_files": ["calculator.py"],
            "scope_violations": [],
            "task_verification": {},
            "stage_acceptance": {"ok": True, "checks": []},
            "cumulative_gate": {"ok": True, "checks": []},
            "commit": {
                "plan_id": self._CG_PLAN_ID,
                "stage_id": "s1",
                "parent_sha": "b" * 40,
                "commit_sha": sha,
                "changed_files": ["calculator.py"],
                "message": "AGY stage s1: Implement the s1 slice",
            },
            "supervisor_decision": {
                "decision": "ACCEPT",
                "rationale": "all green",
                "repair_targets": [],
                "model": "supervisor-role-model",
                "deterministic_override": False,
            },
            "repair_lineage": [],
            "failover_events": [],
            "session_ids": ["ses_cg"],
            "failure_reason": "",
            "replan": None,
        }

    def test_item11_commit_gate_record_reload_and_corrupt(self):
        """(11) Persist 2 records, reload round-trips ALL fields;
        corrupt file -> ValueError naming the file."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg-resume"
            plan, stage = self._plan_and_stage()

            first = build_commit_gate_record(
                plan, stage, self._stage_payload("1" * 40)
            )
            second = build_commit_gate_record(
                plan, stage, self._stage_payload("2" * 40)
            )
            persist_commit_gate_record(run_dir, first)
            persist_commit_gate_record(run_dir, second)

            loaded = load_commit_gate_records(run_dir)
            self.assertEqual(
                [entry.commit_sha for entry in loaded],
                ["1" * 40, "2" * 40],
            )
            self.assertEqual(loaded[0].to_dict(), first.to_dict())
            self.assertEqual(loaded[1].to_dict(), second.to_dict())

            target = run_dir / "commits" / ("1" * 40 + ".json")
            target.write_text("}{ broken json", encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                load_commit_gate_records(run_dir)
            self.assertIn(
                "1" * 40 + ".json", str(error.exception)
            )

    def test_item12_resume_creates_no_duplicate_commits(self):
        """(12) On resume the committed stage is NOT re-committed
        (make_commit call count == persisted count for it); a
        same-SHA re-persist is a no-op; conflicting content for the
        same sha raises."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            h1 = ResumeHarness()
            first = h1.execute(plan, run_state)
            persisted_s1_sha = first.stage_results["s1"]["commit"][
                "commit_sha"
            ]
            persisted_shas_by_stage = {
                stage_id: entry["commit"]["commit_sha"]
                for stage_id, entry in first.stage_results.items()
                if entry.get("commit")
            }

            demote_to_committed(run_state, "s1")
            drop_stage(run_state, "s2")
            set_plan_status(run_state, PLAN_IN_PROGRESS)

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            harness.resume(plan, run_state)

            # make_commit was NEVER called for the committed stage:
            self.assertEqual(
                harness.commit_calls.count("s1"), 0
            )
            # ...while total new commits == the count NOT persisted
            # (only s2 lacked one).
            self.assertEqual(harness.commit_count, 1)
            self.assertNotEqual(
                persisted_shas_by_stage.get("s1"), "2" * 40
            )
            view = StageRunState(run_state.root, plan.plan_id, "s1")
            self.assertEqual(
                view.load_commit()["commit_sha"], persisted_s1_sha
            )

            # Duplicate-persist semantics on a real record:
            plan_cg, stage_cg = self._plan_and_stage()
            record = build_commit_gate_record(
                plan_cg, stage_cg, self._stage_payload("9" * 40)
            )
            path1 = persist_commit_gate_record(run_state.plan_dir, record)
            path2 = persist_commit_gate_record(run_state.plan_dir, record)
            self.assertEqual(path1, path2)  # idempotent same sha

            conflicting = build_commit_gate_record(
                plan_cg, stage_cg, self._stage_payload("9" * 40)
            )
            conflicting.supervisor_decision = dict(
                conflicting.supervisor_decision,
                rationale="edited after the fact",
            )
            with self.assertRaises(ValueError) as conflict:
                persist_commit_gate_record(
                    run_state.plan_dir, conflicting
                )
            self.assertIn(
                "9" * 40, str(conflict.exception)
            )


# ---------------------------------------------------------------------------
# Cases 13-14: repair lineage + failover survive the restart
# ---------------------------------------------------------------------------


class TestPlanRepair(unittest.TestCase):
    def test_item13_plan_repair_state_survives_restart_bounded(self):
        """(13) Persisted PLAN_REPAIR + repair_lineage -> resume
        re-enters the BOUNDED repair path (accepted stages still
        skipped, max_plan_repairs still bounds, no full restart)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            first = ResumeHarness()
            accepted = first.execute(plan, run_state)
            self.assertEqual(accepted.status, PLAN_ACCEPTED)

            # Persist the killed-at-plan-repair boundary.
            set_plan_status(
                run_state,
                PLAN_REPAIR,
                repair_lineage=["plan-repair-1"],
            )
            self.assertEqual(
                recovery_state(None, run_state), PLAN_REPAIR
            )

            # The plan gate asks for REPAIR again, but the bounded
            # budget (already spent) must stop it.
            harness = ResumeHarness(
                decisions=["REPAIR"], sha_seed=RESUME_SHA_SEED
            )
            timeline = Timeline(run_state.plan_dir / "timeline.jsonl")
            result = harness.resume(plan, run_state, timeline=timeline)

            self.assertEqual(result.status, PLAN_REPAIR)
            # Re-entered the repair path: the plan gate re-ran in the
            # resumed process (final acceptance called)...
            self.assertEqual(command_count(harness, FINAL_CMD), 1)
            # ...accepted stages were still skipped (no executor,
            # no plan-repair stage dispatched):
            self.assertEqual(harness.built_stages, [])
            self.assertEqual(harness.executed, [])
            self.assertEqual(harness.commit_count, 0)
            # ...and the budget stayed bounded: lineage unchanged.
            self.assertEqual(
                result.repair_lineage, ["plan-repair-1"]
            )
            persisted = run_state.load_plan()
            self.assertEqual(persisted["status"], PLAN_REPAIR)
            self.assertEqual(
                persisted["repair_lineage"], ["plan-repair-1"]
            )
            # The bounded stop did NOT emit a repair-dispatch event.
            self.assertEqual(
                timeline.events_of("PLAN_REPAIR_REQUESTED"), []
            )

    def test_item14_failover_events_carried_unchanged(self):
        """(14) Persisted failover_events flow into the resumed
        PlanRunResult unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            event = {
                "role": "dispatch",
                "primary_model": "dispatch-role-model",
                "fallback_model": "dispatch-fallback-role-model",
                "fallback_reason": "provider_rate_limited",
                "session_id": "ses_failover_one",
                "parent_session_id": None,
                "fallback_count": 1,
                "primary_attempts": 4,
                "ts": "2026-09-27T12:00:00+00:00",
            }
            first = ResumeHarness(
                stage_validate={"s2-t00": False}, emit_event=event
            )
            first_result = first.execute(plan, run_state)
            self.assertEqual(first_result.status, PLAN_FAILED)
            persisted = run_state.load_plan()
            self.assertEqual(persisted["failover_events"], [event])

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state)

            self.assertEqual(result.status, PLAN_ACCEPTED)
            carried = result.failover_events[0]
            self.assertEqual(carried, event)
            self.assertEqual(len(result.failover_events), 1)
            # And it survives into the next persisted plan.json.
            after = run_state.load_plan()
            self.assertEqual(after["failover_events"], [event])


# ---------------------------------------------------------------------------
# Case 15: PLAN_ACCEPTED terminal/idempotent
# ---------------------------------------------------------------------------


class TestTerminalIdempotence(unittest.TestCase):
    def test_item15_plan_accepted_is_terminal_and_idempotent(self):
        """(15) Resume on a PLAN_ACCEPTED run state returns the
        persisted result with ZERO executor/supervisor/run_command
        calls and emits the already-accepted event."""
        with tempfile.TemporaryDirectory() as tmp:
            run_state = new_plan_dir(tmp)
            plan = make_plan()
            first = ResumeHarness()
            accepted = first.execute(plan, run_state)
            self.assertEqual(accepted.status, PLAN_ACCEPTED)
            timeline = Timeline(run_state.plan_dir / "timeline.jsonl")
            plan_before = (run_state.plan_dir / "plan.json").read_bytes()

            harness = ResumeHarness(sha_seed=RESUME_SHA_SEED)
            result = harness.resume(plan, run_state, timeline=timeline)

            # Persisted result returned verbatim:
            self.assertEqual(result.status, PLAN_ACCEPTED)
            self.assertEqual(
                result.to_dict(), accepted.to_dict()
            )
            # ZERO calls of every kind in the resumed process:
            self.assertEqual(harness.built_stages, [])
            self.assertEqual(harness.executed, [])
            self.assertEqual(harness.validated, [])
            self.assertEqual(harness.review_calls, [])
            self.assertEqual(harness.command_calls, [])
            self.assertEqual(harness.commit_count, 0)
            # The event fired; no work events fired for this resume.
            kinds = [
                event["event_type"] for event in timeline.load()
            ]
            self.assertEqual(kinds, ["PLAN_ALREADY_ACCEPTED"])
            # plan.json untouched byte-for-byte; observability written.
            self.assertEqual(
                (run_state.plan_dir / "plan.json").read_bytes(),
                plan_before,
            )
            observability = run_state.plan_dir / "observability.json"
            self.assertTrue(observability.exists())
            summary = json.loads(
                observability.read_text(encoding="utf-8")
            )
            self.assertEqual(
                summary["plan"]["status"], PLAN_ACCEPTED
            )