"""
Deterministic, network-free tests for the P3 stage execution loop —
the 23-case contract matrix:

 1  only dependency-ready tasks execute (unmet deps -> SKIPPED)
 2  task order deterministic (same input twice -> same call sequence)
 3  allowed scope enforced (out-of-scope file -> violations, no commit)
 4  zero-diff fake success rejected (STAGE_FAILED, no commit, no
    supervisor ACCEPT path)
 5  task verification authoritative (prose ignored)
 6  task success does not imply stage acceptance (no commit,
    supervisor constrained)
 7  stage acceptance commands execute verbatim, results recorded
 8  cumulative invariant failure blocks commit acceptance
 9  meaningful stage commit metadata recorded
10  supervisor ACCEPT -> STAGE_ACCEPTED
11  supervisor REPAIR -> bounded in-stage repair, re-verify, repair
    commit, re-review -> ACCEPT
12  supervisor REJECT -> STAGE_REJECTED with rationale recorded
13  repair stays within the same stage lineage
14  no whole-plan restart (single decomposition, bounded replan)
15  role distinctness (dispatch executor vs supervisor review)
16  dispatch rate-limit retries (agy_runtime level, fake supervisor
    runner) + non-transient failures do NOT retry
17  fallback after exhausted transient retries (same session, event
    validates against the shipped schema verbatim)
18  same session retained across the retry budget
19  supervisor primary/fallback symmetry -> tests/test_agy_failover.py
20  deterministic failures do NOT invoke model fallback (zero fallback
    transport calls, zero events)
21  failover event persisted (schema-shaped record collected from the
    gatekeeper pathway; provider-layer session_id=None filled by the
    OpenCode layer before schema validation — documented in-test)
22  run state recoverable (StageRunState round-trip incl.
    repair_lineage + failover_events; tasks + commit files)
23  no banned runtime dependency (static check over the NEW p3
    modules + app/config.py)

All external effects are injected fakes: no network, no subprocess,
no model, no sleep.
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import jsonschema

from app.config import AgySettings
from app.integrations.external_stack import ExternalResult
from app.orchestration import agy_execute as agy_execute_module
from app.orchestration.agy_execute import (
    STAGE_ACCEPTED,
    STAGE_FAILED,
    STAGE_REJECTED,
    STAGE_REPAIR,
    FailoverRecorder,
    SessionRecorder,
    execute_stage,
)
from app.orchestration.agy_gatekeeper import review_stage
from app.orchestration.agy_runtime import build_live_executor
from app.orchestration.plan_decompose import decompose_stage
from app.orchestration.agy_runstate import StageRunState
from app.providers import OpenAICompatibleClient, RoleModels
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage

SCHEMA_PATH = (
    "/home/leon/antigravity-hybrid/state/autopilot/"
    "agy-failover-state.schema.json"
)

DEFAULT_CMD = "python3 -m unittest discover -s tests -v"
FOUR_FILE_FILES = [
    "calculator.py",
    "tests/test_calculator.py",
    "helpers.py",
    "extra.py",
]

# Provider-layer failover record exactly as failover.py builds it for
# a gatekeeper-role event: session_id is None until the OpenCode
# layer fills the REAL session id (see contract §2).
PROVIDER_LAYER_EVENT = {
    "role": "supervisor",
    "primary_model": "alias-mimo-v2.6-pro",
    "fallback_model": "alias-qwen3.8-27b",
    "active_model": "alias-qwen3.8-27b",
    "primary_attempts": 4,
    "fallback_reason": "rate_limit_exhausted",
    "session_id": None,
    "fallback_count": 1,
    "parent_session_id": None,
    "ts": "2026-09-26T23:45:00+02:00",
}


def install_host_runtime_stub() -> None:
    """Host-runtime stub, same as the P2 development-plan tests."""
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


def make_stage(
    stage_id: str = "multiply",
    allowed_files: list[str] | None = None,
    acceptance_commands: list[str] | None = None,
) -> PlanStage:
    commands = acceptance_commands or [DEFAULT_CMD]
    return PlanStage(
        stage_id=stage_id,
        objective="Implement multiply(a, b) in calculator.py",
        dependencies=[],
        allowed_files=allowed_files
        or ["calculator.py", "tests/test_calculator.py"],
        implementation_context=(
            "Add multiply(a, b) to calculator.py and a deterministic "
            "unit test for it."
        ),
        acceptance_commands=[
            AcceptanceCommand(
                command=command,
                expected_exit_code=0,
                description=f"check {index}",
            )
            for index, command in enumerate(commands, start=1)
        ],
        acceptance_conditions=["multiply(2, 3) returns 6"],
        cumulative_invariants=["add(a, b) still returns a + b"],
    )


def make_plan(stage: PlanStage, plan_id: str = "agp-p3-test") -> DevelopmentPlan:
    return DevelopmentPlan(
        plan_id=plan_id,
        objective="add multiply to the calculator with deterministic tests",
        stages=[stage],
        final_acceptance=[
            AcceptanceCommand(command=DEFAULT_CMD, expected_exit_code=0)
        ],
    )


class Harness:
    """
    One deterministic stage-execution environment. Every injected
    callable the contract asks for lives here and records exactly
    what it was asked to do.
    """

    def __init__(
        self,
        *,
        changed: list[str] | None = None,
        validate: dict[str, bool] | None = None,
        command_results: dict[str, tuple[int, str]] | None = None,
        decisions: list[str] | None = None,
        repair_targets: list[str] | None = None,
        emit_event: dict | None = None,
        review_result=None,
    ):
        self.changed = list(
            changed
            if changed is not None
            else ["calculator.py", "tests/test_calculator.py"]
        )
        self.validate = dict(validate or {})
        self.command_results = dict(
            command_results if command_results is not None else {}
        )
        self.decisions = list(decisions or ["ACCEPT"])
        self.repair_targets = list(
            repair_targets or ["unittest must exit 0"]
        )
        self.emit_event = emit_event
        self.review_result = review_result  # raw override (invalid output)

        self.executed: list[tuple[str, str, int]] = []  # (role, id, attempt)
        self.validated: list[str] = []
        self.command_calls: list[tuple[str, int]] = []
        self.review_calls: list[dict] = []
        self.commit_evidence: list[dict] = []
        self.commit_count = 0

    # -- injected callables -------------------------------------------------

    async def execute_task(self, task, attempt, repair_context):
        self.executed.append(("dispatch", task.id, attempt))
        return f"worker prose: implemented {task.id} attempt {attempt}"

    async def validate_task(self, task):
        self.validated.append(task.id)
        ok = self.validate.get(task.id, True)
        return (
            ok,
            "deterministic verification passed"
            if ok
            else "deterministic verification FAILED: expectation broken",
        )

    def collect_changed_files(self, task=None):
        return list(self.changed)

    async def run_command(self, command):
        self.command_calls.append(
            (command.command, command.expected_exit_code)
        )
        return self.command_results.get(command.command, (0, "all green"))

    def make_commit(self, evidence):
        self.commit_count += 1
        self.commit_evidence.append(evidence)
        stage_id = evidence["stage_id"]
        return {
            "plan_id": evidence["plan_id"],
            "stage_id": stage_id,
            # Realistic linkage: each commit's parent is the previous
            # commit's sha (first parent = baseline "00" * 20).
            "parent_sha": f"{self.commit_count - 1:040x}",
            "commit_sha": f"{self.commit_count:040x}",
            "changed_files": list(evidence["changed_files"]),
            "message": (
                f"AGY stage {stage_id}: "
                f"{evidence.get('stage_objective', '')}"
            ),
        }

    async def review(self, evidence=None, on_failover_event=None):
        self.review_calls.append(
            {
                "role": "supervisor",
                "evidence": evidence,
                "on_failover_event": on_failover_event,
            }
        )

        if self.review_result is not None:
            raw = self.review_result
            return raw(raw, None) if callable(raw) else raw

        decision = (
            self.decisions.pop(0)
            if len(self.decisions) > 1
            else self.decisions[0]
        )

        result = {
            "decision": decision,
            "rationale": f"supervisor reason: {decision}",
            "repair_targets": (
                list(self.repair_targets) if decision == "REPAIR" else []
            ),
            "model": "alias-mimo-v2.6-pro",
            "failover_event": None,
        }

        if self.emit_event is not None and on_failover_event is not None:
            on_failover_event(self.emit_event)
            result["failover_event"] = self.emit_event

        return result

    # -- runner -------------------------------------------------------------

    async def run_stage(
        self,
        plan,
        stage,
        *,
        cumulative=None,
        scope_checker=None,
        on_session=None,
        on_failover_event=None,
        use_review=True,
        use_commit=True,
        settings=None,
        max_task_attempts=None,
    ):
        kwargs = {}
        if max_task_attempts is not None:
            kwargs["max_task_attempts"] = max_task_attempts

        return await execute_stage(
            plan,
            stage,
            settings=settings or AgySettings.from_env({}),
            execute_task=self.execute_task,
            validate_task=self.validate_task,
            collect_changed_files=self.collect_changed_files,
            run_command=self.run_command,
            cumulative_suite=(
                list(stage.acceptance_commands)
                if cumulative is None
                else cumulative
            ),
            scope_checker=scope_checker,
            make_commit=self.make_commit if use_commit else None,
            supervisor_review=self.review if use_review else None,
            on_session=on_session,
            on_failover_event=on_failover_event,
            **kwargs,
        )


def run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cases 1-9: execution + deterministic gates
# ---------------------------------------------------------------------------


class TestExecutionGates(unittest.TestCase):
    def test_01_only_dependency_ready_tasks_execute(self):
        """Case 1: unmet deps -> SKIPPED_DEPENDENCY, never executed."""
        stage = make_stage(allowed_files=FOUR_FILE_FILES)
        plan = make_plan(stage)
        harness = Harness(validate={"multiply-t00": False})

        result = run(harness.run_stage(plan, stage))

        executed_ids = [task_id for _, task_id, _ in harness.executed]
        self.assertEqual(
            executed_ids, ["multiply-t00", "multiply-t00"]
        )  # 2 attempts on t00, t01 never launched
        skipped = [
            r for r in result.task_results
            if r["status"] == "SKIPPED_DEPENDENCY"
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["worker_task_id"], "multiply-t01")
        self.assertIn("multiply-t00", skipped[0]["failure_reason"])
        self.assertEqual(result.status, STAGE_FAILED)

    def test_02_task_order_deterministic(self):
        """Case 2: same input twice -> same order + call sequences."""
        sequences = []
        statuses = []
        for _ in range(2):
            stage = make_stage(allowed_files=FOUR_FILE_FILES)
            plan = make_plan(stage)
            harness = Harness(validate={"multiply-t00": False})
            result = run(harness.run_stage(plan, stage))
            sequences.append(
                [(role, task_id, attempt)
                 for role, task_id, attempt in harness.executed]
            )
            statuses.append(
                [(r["worker_task_id"], r["status"], r["attempt"])
                 for r in result.task_results]
            )

        self.assertEqual(sequences[0], sequences[1])
        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual(sequences[0][0][1], "multiply-t00")

    def test_03_allowed_scope_enforced(self):
        """Case 3: out-of-scope change -> violations + NO commit ever."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            changed=["calculator.py", "secrets.env"],
            decisions=["ACCEPT"],
        )

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.scope_violations, ["secrets.env"])
        self.assertIsNone(result.commit)
        self.assertEqual(harness.commit_evidence, [])
        self.assertIn("secrets.env", result.changed_files)
        # Supervisor still reviewed, but was CONSTRAINED: model
        # ACCEPT forced to REPAIR (contract §9).
        self.assertEqual(len(harness.review_calls), 1)
        self.assertEqual(
            harness.review_calls[0]["evidence"]["deterministic_failures"],
            [
                "scope violation: secrets.env is not in stage.allowed_files"
            ],
        )
        decision = result.supervisor_decision
        self.assertEqual(decision["decision"], "REPAIR")
        self.assertTrue(decision["deterministic_override"])
        # Bounded stop after the single repair cycle still violated.
        self.assertEqual(result.status, STAGE_REPAIR)
        self.assertEqual(result.repair_lineage, ["multiply-repair-1"])

    def test_04_zero_diff_fake_success_rejected(self):
        """Case 4: prose success + empty diff -> STAGE_FAILED."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(changed=[])

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.status, STAGE_FAILED)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(
            result.failure_reason,
            "zero-diff with implementation required",
        )
        self.assertIsNone(result.commit)
        self.assertEqual(harness.commit_evidence, [])
        # No supervisor ACCEPT path: the review never ran at all.
        self.assertEqual(harness.review_calls, [])
        self.assertIsNone(result.supervisor_decision)

    def test_05_task_verification_authoritative(self):
        """Case 5: worker prose claims success, validate says no."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(validate={"multiply-t00": False})

        result = run(harness.run_stage(plan, stage))

        task_result = result.task_results[0]
        self.assertEqual(task_result["status"], "FAILED")
        self.assertFalse(task_result["validation_ok"])
        self.assertIn(
            "FAILED",
            task_result["validation_output"],
        )
        self.assertEqual(result.status, STAGE_FAILED)
        # Stage was never accepted: acceptance gate never ran.
        self.assertTrue(result.stage_acceptance.get("skipped"))
        self.assertIsNone(result.commit)
        self.assertEqual(harness.review_calls, [])

    def test_06_task_success_does_not_imply_stage_acceptance(self):
        """Case 6: tasks green, acceptance red -> no commit, constrained."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            command_results={DEFAULT_CMD: (1, "FAIL: acceptance broken")},
            decisions=["ACCEPT"],
        )

        result = run(harness.run_stage(plan, stage))

        statuses = {
            r["worker_task_id"]: r["status"]
            for r in result.task_results
        }
        self.assertEqual(statuses["multiply-t00"], "SUCCESS")
        self.assertFalse(result.stage_acceptance["ok"])
        self.assertIsNone(result.commit)
        self.assertEqual(harness.commit_evidence, [])
        self.assertEqual(len(harness.review_calls), 1)
        self.assertTrue(
            harness.review_calls[0]["evidence"]["deterministic_failures"]
        )
        decision = result.supervisor_decision
        self.assertEqual(decision["decision"], "REPAIR")  # constrained
        self.assertTrue(decision["deterministic_override"])
        self.assertEqual(decision["original_decision"], "ACCEPT")
        # Acceptance kept failing across the bounded repair cycle:
        # acceptance + cumulative on the first pass and again on the
        # repair re-verification pass.
        self.assertEqual(result.status, STAGE_REPAIR)
        self.assertEqual(
            harness.command_calls.count((DEFAULT_CMD, 0)), 4
        )

    def test_07_stage_acceptance_commands_execute_verbatim(self):
        """Case 7: every AcceptanceCommand verbatim, results recorded."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            command_results={DEFAULT_CMD: (0, "Ran 2 tests...OK")}
        )

        result = run(
            harness.run_stage(
                plan,
                stage,
                cumulative=[],
            )
        )

        self.assertEqual(result.status, STAGE_ACCEPTED)
        verbatim = stage.acceptance_commands[0].command
        self.assertTrue(harness.command_calls)
        for command, expected in harness.command_calls:
            self.assertEqual(command, verbatim)
            self.assertEqual(expected, 0)

        checks = result.stage_acceptance["checks"]
        self.assertEqual(len(checks), len(stage.acceptance_commands))
        self.assertEqual(checks[0]["command"], verbatim)
        self.assertEqual(checks[0]["description"], "check 1")
        self.assertEqual(checks[0]["expected_exit_code"], 0)
        self.assertEqual(checks[0]["exit_code"], 0)
        self.assertTrue(checks[0]["ok"])
        self.assertIn("Ran 2 tests", checks[0]["output"])
        self.assertEqual(checks[0]["stage_id"], "multiply")

    def test_08_cumulative_failure_blocks_commit(self):
        """Case 8: cumulative red while stage acceptance green."""
        cumulative_cmd = "python3 -m unittest -v test_regression"
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            command_results={
                DEFAULT_CMD: (0, "stage checks green"),
                cumulative_cmd: (4, "regression broken"),
            },
            decisions=["ACCEPT"],
        )

        result = run(
            harness.run_stage(
                plan,
                stage,
                cumulative=[cumulative_cmd],
            )
        )

        self.assertTrue(result.stage_acceptance["ok"])
        self.assertFalse(result.cumulative_gate["ok"])
        self.assertEqual(
            sorted(result.cumulative_gate["checks"][0]),
            ["command", "exit_code", "name", "ok", "output"],
        )
        self.assertEqual(
            result.cumulative_gate["checks"][0]["name"], "cumulative-01"
        )
        self.assertEqual(
            result.cumulative_gate["checks"][0]["exit_code"], 4
        )
        self.assertEqual(result.cumulative_gate["checks"][0]["output"],
                         "regression broken")
        self.assertIsNone(result.commit)
        self.assertEqual(harness.commit_evidence, [])
        self.assertEqual(result.supervisor_decision["deterministic_override"],
                         True)

    def test_09_meaningful_stage_commit_metadata_recorded(self):
        """Case 9: one meaningful commit, complete commit metadata."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness()

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(harness.commit_count, 1)
        self.assertEqual(len(harness.commit_evidence), 1)
        evidence = harness.commit_evidence[0]
        # The commit helper was handed what it needs for the mandated
        # message format.
        self.assertEqual(evidence["stage_id"], "multiply")
        self.assertEqual(evidence["stage_objective"], stage.objective)
        self.assertEqual(evidence["plan_id"], plan.plan_id)
        self.assertEqual(evidence["invariants"], stage.cumulative_invariants)

        commit = result.commit
        required = {
            "plan_id",
            "stage_id",
            "parent_sha",
            "commit_sha",
            "changed_files",
            "task_results",
            "verification",
            "message",
        }
        self.assertTrue(required.issubset(commit), commit.keys())
        self.assertEqual(commit["plan_id"], "agp-p3-test")
        self.assertEqual(commit["stage_id"], "multiply")
        self.assertEqual(commit["parent_sha"], "0" * 40)
        self.assertEqual(commit["commit_sha"], f"{1:040x}")
        self.assertEqual(commit["changed_files"], harness.changed)
        self.assertEqual(
            commit["message"],
            f"AGY stage multiply: {stage.objective}",
        )
        self.assertEqual(
            commit["task_results"],
            [
                {
                    "worker_task_id": "multiply-t00",
                    "attempt": 1,
                    "status": "SUCCESS",
                    "validation_ok": True,
                }
            ],
        )
        self.assertIn("multiply-t00", commit["verification"])


# ---------------------------------------------------------------------------
# Cases 10-15: supervisor decisions + bounded repair
# ---------------------------------------------------------------------------


class TestSupervisorDecisions(unittest.TestCase):
    def test_10_supervisor_accept_stage_accepted(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["ACCEPT"])

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.status, STAGE_ACCEPTED)
        self.assertEqual(result.supervisor_decision["decision"], "ACCEPT")
        self.assertEqual(
            result.supervisor_decision["model"], "alias-mimo-v2.6-pro"
        )
        self.assertFalse(result.supervisor_decision["deterministic_override"])
        self.assertIsNotNone(result.commit)
        self.assertEqual(result.scope_violations, [])
        self.assertTrue(result.stage_acceptance["ok"])
        self.assertTrue(result.cumulative_gate["ok"])

    def test_11_supervisor_repair_bounded_cycle_then_accept(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["REPAIR", "ACCEPT"])

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.status, STAGE_ACCEPTED)
        self.assertEqual(len(harness.review_calls), 2)
        self.assertEqual(harness.commit_count, 2)

        executed_ids = [task_id for _, task_id, _ in harness.executed]
        self.assertIn("multiply-repair-1", executed_ids)
        self.assertIn("multiply-repair-1", harness.validated)

        self.assertEqual(result.repair_lineage, ["multiply-repair-1"])
        repair_record = result.task_results[-1]
        self.assertEqual(repair_record["worker_task_id"], "multiply-repair-1")
        self.assertEqual(repair_record["status"], "SUCCESS")

        repair_evidence = harness.commit_evidence[-1]
        self.assertTrue(
            repair_evidence["message"].startswith(
                "AGY stage multiply repair 1: "
            )
        )
        # The re-review saw the repair task in its evidence.
        second_review = harness.review_calls[1]["evidence"]
        self.assertIn(
            "multiply-repair-1",
            [t["worker_task_id"] for t in second_review["tasks"]],
        )
        self.assertEqual(
            second_review["deterministic_failures"], []
        )
        self.assertEqual(
            result.supervisor_decision["decision"], "ACCEPT"
        )

    def test_12_supervisor_reject_stage_rejected(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["REJECT"])

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.status, STAGE_REJECTED)
        decision = result.supervisor_decision
        self.assertEqual(decision["decision"], "REJECT")
        self.assertEqual(decision["rationale"], "supervisor reason: REJECT")
        self.assertFalse(decision["deterministic_override"])
        # All deterministic checks were green, so the commit exists
        # (commit happens before review) but the stage is NOT accepted.
        self.assertIsNotNone(result.commit)

    def test_13_repair_stays_within_same_stage_lineage(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["REPAIR", "ACCEPT"])

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.repair_lineage, ["multiply-repair-1"])
        self.assertEqual(result.plan_id, plan.plan_id)  # unchanged
        self.assertEqual(result.stage_id, "multiply")  # unchanged
        task_ids = {r["worker_task_id"] for r in result.task_results}
        self.assertTrue(
            task_ids.issubset({"multiply-t00", "multiply-repair-1"}),
            task_ids,
        )
        # No new plan/stage ids anywhere in the persisted shape.
        round_tripped = result.to_dict()
        self.assertEqual(round_tripped["plan_id"], plan.plan_id)
        self.assertEqual(round_tripped["stage_id"], stage.stage_id)
        self.assertEqual(round_tripped["repair_lineage"],
                         ["multiply-repair-1"])

    def test_14_no_whole_plan_restart(self):
        """Case 14: one decomposition + bounded replan scope."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["REPAIR", "ACCEPT"])

        original = agy_execute_module.decompose_stage
        calls = {"count": 0}

        def counting_decompose(*args, **kwargs):
            calls["count"] += 1
            return original(*args, **kwargs)

        agy_execute_module.decompose_stage = counting_decompose
        try:
            result = run(harness.run_stage(plan, stage))
        finally:
            agy_execute_module.decompose_stage = original

        self.assertEqual(result.status, STAGE_ACCEPTED)
        self.assertEqual(calls["count"], 1)  # repair never re-decomposes
        self.assertEqual(result.repair_lineage, ["multiply-repair-1"])

        # Bounded replan scope on task failure: failed + blocked only.
        stage2 = make_stage(allowed_files=FOUR_FILE_FILES)
        plan2 = make_plan(stage2)
        failing = Harness(validate={"multiply-t00": False})
        failed_result = run(failing.run_stage(plan2, stage2))

        self.assertEqual(failed_result.status, STAGE_FAILED)
        self.assertEqual(failed_result.replan["failed_task_ids"],
                         ["multiply-t00"])
        self.assertEqual(failed_result.replan["blocked_task_ids"],
                         ["multiply-t01"])
        self.assertEqual(
            set(failed_result.replan["replan_scope_task_ids"]),
            {"multiply-t00", "multiply-t01"},
        )
        self.assertIn("ONLY the failed tasks",
                      failed_result.replan["instruction"])
        self.assertIn("multiply-t00", failed_result.failure_reason)

    def test_15_role_distinctness(self):
        """Case 15: dispatch executor vs supervisor review never cross."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness()
        settings = AgySettings.from_env({})

        result = run(harness.run_stage(plan, stage, settings=settings))

        executor_roles = {role for role, _, _ in harness.executed}
        review_roles = {call["role"] for call in harness.review_calls}
        self.assertEqual(executor_roles, {"dispatch"})
        self.assertEqual(review_roles, {"supervisor"})
        self.assertFalse(executor_roles & review_roles)

        # The executor never saw a review (and the reviewer never
        # executed): no evidence/decision text leaked into execution.
        self.assertTrue(harness.executed)
        self.assertEqual(result.status, STAGE_ACCEPTED)

        # Model binding distinctness for the two roles.
        from app.providers import ModelRole

        models = RoleModels.for_agy(settings)
        self.assertNotEqual(
            models.model_for(ModelRole.DISPATCH),
            models.model_for(ModelRole.SUPERVISOR),
        )


# ---------------------------------------------------------------------------
# Cases 16-18: agy_runtime OpenCode-layer retries/failover/sessions
# ---------------------------------------------------------------------------


SESSION_A = "ses_A1b2C3d4E5f6"

TRANSIENT_FAIL = ExternalResult(
    ok=False,
    returncode=1,
    stdout=f"Session ID: {SESSION_A}\n",
    stderr="provider returned HTTP 429: rate limit exceeded",
)
NON_TRANSIENT_FAIL = ExternalResult(
    ok=False,
    returncode=1,
    stdout="",
    stderr="permission denied while writing calculator.py",
)
OK_RUN = ExternalResult(
    ok=True,
    returncode=0,
    stdout=f"Session ID: {SESSION_A}\nimplemented multiply\n",
    stderr="",
)


class ScriptedRunner:
    """Fake opencode-supervise runner: scripted ExternalResults."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, settings, repo, task, *, agent=None, session=None,
                 **kwargs):
        index = len(self.calls)
        self.calls.append(
            {
                "agent": agent,
                "session": session,
                "prompt": task,
                "settings": settings,
                "repo": repo,
            }
        )
        response = self.responses[min(index, len(self.responses) - 1)]
        if callable(response):
            response = response(index, agent, session)
        return response


class TestRuntimeFailoverLayer(unittest.TestCase):
    def build(self, responses, *, workspace=None):
        plan = workspace or {}
        stage = make_stage()
        plan_obj = make_plan(stage)
        runner = ScriptedRunner(responses)
        recorder = SessionRecorder()
        events = FailoverRecorder()

        execute_task, validate_task, collect, run_command = (
            build_live_executor(
                AgySettings.from_env({}),
                "/nonexistent-agy-e2e-repo",
                plan_obj,
                stage,
                on_session=recorder,
                on_failover_event=events,
                run_supervised=runner,
                # P4: keep the P3 suite network-free. This fake's
                # stdout sometimes carries NO session id (NON_TRANSIENT
                # case), which would otherwise trigger the live server
                # read-back seam in build_live_executor.
                session_read_back=lambda **_kwargs: None,
            )
        )
        task = decompose_stage(stage, objective=plan_obj.objective)[0]
        return runner, recorder, events, execute_task, task, stage, plan_obj

    def test_16_dispatch_rate_limit_retries_then_success(self):
        """Case 16: 3 transient failures -> 4th attempt succeeds."""
        runner, recorder, events, execute_task, task, _, _ = self.build(
            [TRANSIENT_FAIL, TRANSIENT_FAIL, TRANSIENT_FAIL, OK_RUN]
        )

        output = run(execute_task(task, 1, None))

        self.assertEqual(len(runner.calls), 4)
        self.assertTrue(
            all(call["agent"] == "agy-dispatch"
                for call in runner.calls)
        )
        self.assertNotIn("agy-dispatch-fallback",
                         [call["agent"] for call in runner.calls])
        self.assertEqual(events.events, [])  # no failover
        self.assertIn("implemented multiply", output)
        self.assertEqual(recorder.sessions, [SESSION_A])
        self.assertEqual(recorder.last_for(task.id), SESSION_A)

    def test_16b_non_transient_failure_does_not_retry(self):
        """Case 16 (complement): deterministic failures never fail over."""
        runner, _, events, execute_task, task, _, _ = self.build(
            [NON_TRANSIENT_FAIL]
        )

        output = run(execute_task(task, 1, None))

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(events.events, [])
        self.assertIn("permission denied", output)

    def test_17_fallback_after_exhausted_transient_retries(self):
        """Case 17: 4 transient failures -> same-session fallback run."""
        runner, recorder, events, execute_task, task, _, _ = self.build(
            [TRANSIENT_FAIL] * 4 + [OK_RUN]
        )

        run(execute_task(task, 1, None))

        self.assertEqual(len(runner.calls), 5)  # 4 primary + 1 fallback
        primary = runner.calls[:4]
        fallback_call = runner.calls[4]

        for attempt_no, call in enumerate(primary, start=1):
            self.assertEqual(call["agent"], "agy-dispatch")
            self.assertEqual(
                call["session"],
                None if attempt_no == 1 else SESSION_A,
            )
        self.assertEqual(fallback_call["agent"], "agy-dispatch-fallback")
        self.assertEqual(fallback_call["session"], SESSION_A)

        self.assertEqual(len(events.events), 1)
        event = events.events[0]
        self.assertEqual(event["role"], "dispatch")
        self.assertEqual(event["primary_model"],
                         "alias-mimo-v2.6-flash")
        self.assertEqual(event["fallback_model"],
                         "alias-qwen3.8-flash-next")
        self.assertEqual(event["active_model"],
                         "alias-qwen3.8-flash-next")
        self.assertEqual(event["primary_attempts"], 4)
        self.assertEqual(event["fallback_count"], 1)
        self.assertEqual(event["fallback_reason"], "rate_limit_exhausted")
        self.assertEqual(event["session_id"], SESSION_A)
        self.assertIsNone(event["parent_session_id"])
        self.assertIn("T", event["ts"])

        # Case 21 (part 1): a REAL runtime event validates against the
        # shipped schema VERBATIM (session id present, pattern ^ses_).
        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            schema = json.load(handle)
        jsonschema.validate([event], schema)

    def test_18_same_session_retained_across_retries(self):
        """Case 18: the SAME session carries every retry."""
        runner, recorder, events, execute_task, task, _, _ = self.build(
            [TRANSIENT_FAIL, TRANSIENT_FAIL, TRANSIENT_FAIL, OK_RUN]
        )

        run(execute_task(task, 1, None))

        sessions = [call["session"] for call in runner.calls]
        self.assertEqual(sessions, [None, SESSION_A, SESSION_A, SESSION_A])
        self.assertEqual(recorder.sessions, [SESSION_A])
        self.assertEqual(events.events, [])

    def test_runtime_prompt_is_the_bounded_dispatch_prompt(self):
        """Sanity: the executor builds exactly P2's bounded prompt."""
        _, _, _, _, task, stage, plan_obj = self.build([OK_RUN])
        runner = ScriptedRunner([OK_RUN])
        execute_task2, _, _, _ = build_live_executor(
            AgySettings.from_env({}),
            "/nonexistent-agy-e2e-repo",
            plan_obj,
            stage,
            run_supervised=runner,
        )
        run(execute_task2(task, 1, None))
        prompt = runner.calls[0]["prompt"]
        self.assertIn("BOUNDED DISPATCH", prompt)
        self.assertIn(stage.stage_id, prompt)
        self.assertIn(task.id, prompt)
        self.assertIn("Siblings are out of scope", prompt)


# ---------------------------------------------------------------------------
# Cases 20-23: failover-init discipline, persistence, banned words
# ---------------------------------------------------------------------------


class TestEvidenceDisciplineAndState(unittest.TestCase):
    def test_20_deterministic_failures_never_invoke_model_fallback(self):
        """
        Case 20: task-validation and acceptance failures are NOT
        failover triggers — zero fallback transport calls, zero
        events, even when the real gatekeeper reviews.
        """
        case_a_stage = make_stage()
        case_a_plan = make_plan(case_a_stage)
        harness_a = Harness(validate={"multiply-t00": False},
                            emit_event=None)
        events_a = FailoverRecorder()
        result_a = run(
            harness_a.run_stage(case_a_plan, case_a_stage,
                               on_failover_event=events_a)
        )
        self.assertEqual(result_a.status, STAGE_FAILED)
        self.assertEqual(harness_a.review_calls, [])
        self.assertEqual(events_a.events, [])
        self.assertEqual(result_a.failover_events, [])

        # Acceptance failure with the REAL review_stage wired:
        transport_calls = []

        def transport(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            transport_calls.append(payload)
            body = {
                "model": payload["model"],
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "ACCEPT",
                                    "rationale": "claims green",
                                    "repair_targets": [],
                                }
                            )
                        }
                    }
                ],
            }
            return 200, json.dumps(body).encode("utf-8")

        settings = AgySettings.from_env({})

        async def real_review(evidence=None, on_failover_event=None):
            return await review_stage(
                evidence=evidence,
                settings=settings,
                transport=transport,
                on_failover_event=on_failover_event,
            )

        case_b_stage = make_stage()
        case_b_plan = make_plan(case_b_stage)
        harness_b = Harness(command_results={DEFAULT_CMD: (1, "FAIL")})
        harness_b.review = real_review  # replace with the real gate
        events_b = FailoverRecorder()
        result_b = run(
            harness_b.run_stage(
                case_b_plan,
                case_b_stage,
                cumulative=[],
                on_failover_event=events_b,
                settings=settings,
            )
        )

        self.assertFalse(result_b.stage_acceptance["ok"])
        self.assertIsNone(result_b.commit)
        self.assertEqual(len(transport_calls), 1)
        for payload in transport_calls:
            self.assertNotEqual(
                payload["model"],
                settings.dispatch_fallback_model,
            )
            self.assertNotEqual(
                payload["model"],
                settings.supervisor_fallback_model,
            )
            self.assertEqual(payload["model"], settings.supervisor_model)
        self.assertEqual(events_b.events, [])
        self.assertEqual(result_b.failover_events, [])
        # Model claimed ACCEPT over failed checks -> forced.
        self.assertEqual(result_b.supervisor_decision["decision"], "REPAIR")
        self.assertTrue(
            result_b.supervisor_decision["deterministic_override"]
        )

    def test_21_failover_event_persisted_and_schema_validated(self):
        """Case 21: gatekeeper event collected end-to-end + schema."""
        stage = make_stage()
        plan = make_plan(stage)
        event = dict(PROVIDER_LAYER_EVENT)
        harness = Harness(decisions=["ACCEPT"], emit_event=event)
        events = FailoverRecorder()

        result = run(
            harness.run_stage(
                plan, stage, on_failover_event=events
            )
        )

        self.assertEqual(result.status, STAGE_ACCEPTED)
        self.assertEqual(len(result.failover_events), 1)
        self.assertEqual(result.failover_events[0], event)
        self.assertIsNone(event["session_id"])
        # Forwarded EXACTLY once to the caller's hook (record_event
        # dedups identity against the shared recorder's merge).
        self.assertEqual(events.events, [event])
        returned = harness.review_calls[0]["on_failover_event"]
        self.assertIsNotNone(returned)
        self.assertEqual(
            result.supervisor_decision["failover_event"], event
        )

        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            schema = json.load(handle)

        # The provider layer records session_id=None (contract §2:
        # the OpenCode layer fills the real session). The schema's
        # session_id property is typed string ^ses_ (only
        # parent_session_id is string|null), so a raw provider-layer
        # record is intentionally not yet schema-valid; filled with
        # the session the OpenCode layer would provide, EVERYTHING
        # validates — including every other field unchanged.
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate([event], schema)

        open_code_filled = dict(event, session_id=SESSION_A)
        jsonschema.validate([open_code_filled], schema)
        # Nothing else needed fixing:
        self.assertEqual(
            {k for k in open_code_filled},
            {k for k in event},
        )

    def test_22_run_state_recoverable_round_trip(self):
        """Case 22: StageRunState round-trips a full StageRunResult."""
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(decisions=["REPAIR", "ACCEPT"],
                          emit_event=dict(PROVIDER_LAYER_EVENT))
        result = run(harness.run_stage(plan, stage))

        # Full shape: repair lineage + at least one failover event.
        self.assertEqual(result.repair_lineage, ["multiply-repair-1"])
        self.assertEqual(len(result.failover_events), 1)
        self.assertEqual(len(result.task_results), 2)

        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(Path(tmp) / "runs",
                                  result.plan_id, result.stage_id)

            self.assertEqual(
                str(state.stage_dir),
                f"{tmp}/runs/agp-p3-test/multiply",
            )

            stage_path = state.save_stage(result)
            self.assertTrue(stage_path.exists())
            self.assertEqual(stage_path.name, "stage.json")
            # Atomic write: no temp files survive.
            self.assertEqual(
                sorted(p.name for p in state.stage_dir.glob("*.tmp")), []
            )

            loaded = state.load_stage()
            self.assertEqual(loaded, result.to_dict())
            self.assertEqual(loaded["repair_lineage"],
                             ["multiply-repair-1"])
            self.assertEqual(len(loaded["failover_events"]), 1)
            self.assertEqual(
                loaded["commit"]["commit_sha"], f"{2:040x}"
            )

            # Deterministic serialization (sort_keys, indent=2).
            stage_path_2 = state.save_stage(result)
            self.assertEqual(stage_path.read_bytes(),
                             stage_path_2.read_bytes())
            first_bytes = stage_path.read_bytes()
            self.assertIn(b'\n  "repair_lineage"', first_bytes)

            for task_record in result.task_results:
                task_path = state.save_task(task_record)
                self.assertTrue(task_path.parent.name == "tasks")
                self.assertTrue(task_path.exists())

            tasks = state.load_tasks()
            self.assertEqual(len(tasks), len(result.task_results))
            self.assertEqual(
                sorted(t["task_id"] for t in tasks),
                ["multiply-repair-1", "multiply-t00"],
            )
            for record in tasks:
                for key in (
                    "task_id",
                    "attempt",
                    "status",
                    "validation_ok",
                    "changed_files",
                    "session_id",
                    "failure_reason",
                ):
                    self.assertIn(key, record)

            commit_path = state.save_commit(result.commit)
            self.assertEqual(commit_path.name, "commit.json")
            loaded_commit = json.loads(
                commit_path.read_text(encoding="utf-8")
            )
            for key in (
                "plan_id",
                "stage_id",
                "parent_sha",
                "commit_sha",
                "changed_files",
                "task_results",
                "verification",
                "message",
            ):
                self.assertIn(key, loaded_commit)

            # Load-back recovery: a FRESH instance reads everything.
            recovered = StageRunState(Path(tmp) / "runs",
                                      plan.plan_id, stage.stage_id)
            self.assertEqual(recovered.load_stage(), result.to_dict())
            self.assertEqual(len(recovered.load_tasks()), 2)
            self.assertEqual(
                recovered.load_commit()["plan_id"], plan.plan_id
            )

    def test_23_no_banned_runtime_dependency(self):
        """
        Case 23: static check over the NEW p3 modules (full source,
        comments and docstrings included) and app/config.py.

        Scope note: agy_runtime may reference the installed
        opencode-supervise helper ('opencode-supervise' is OpenCode,
        not a banned runtime); only the five NEW p3 modules + config
        are checked here — pre-existing P1/P2 modules are outside the
        P3 file list.

        config.py note: the contract pins 'nothing else in config.py
        changes', and its PRE-EXISTING AgySettings docstring
        legitimately names the legacy runtimes in a negating sentence.
        config.py is therefore checked on executable code only
        (comments and strings tokenized away), while the NEW p3
        modules are checked raw — a new module must not even mention
        them.
        """
        banned = ["gemma", "nemotron", "minicpm", "hermes", "jules"]

        new_modules = [
            "app/orchestration/failover.py",
            "app/orchestration/agy_execute.py",
            "app/orchestration/agy_runstate.py",
            "app/orchestration/agy_gatekeeper.py",
            "app/orchestration/agy_runtime.py",
        ]

        for relative in new_modules:
            source = Path(relative).read_text(encoding="utf-8")
            lowered = source.lower()
            for word in banned:
                with self.subTest(module=relative, banned=word):
                    self.assertNotIn(word, lowered)

        # app/config.py: executable code only (comments/docstrings
        # stripped) so the untouched pre-existing docstring does not
        # force a forbidden config.py rewrite.
        import tokenize
        import io

        config_source = Path("app/config.py").read_text(encoding="utf-8")
        code_only = []
        for tok in tokenize.generate_tokens(
            io.StringIO(config_source).readline
        ):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            code_only.append(tok.string)
        executable = " ".join(code_only).lower()
        for word in banned:
            with self.subTest(module="app/config.py", banned=word):
                self.assertNotIn(word, executable)

        # The two new config fields are actually present.
        self.assertIn("supervisor_fallback_model", executable)
        self.assertIn("dispatch_fallback_model", executable)

    # -----------------------------------------------------------------------
    # Bonus fail-closed checks (beyond the 23, contract §9)
    # -----------------------------------------------------------------------

    def test_invalid_supervisor_output_fails_closed(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            review_result={
                "decision": "YEAH",
                "rationale": "feels right",
            },
        )

        result = run(harness.run_stage(plan, stage))

        self.assertEqual(result.status, STAGE_REJECTED)
        self.assertEqual(
            result.supervisor_decision["rationale"],
            "gatekeeper response invalid",
        )

    def test_no_supervisor_wired_deterministic_accept(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness()

        result = run(
            harness.run_stage(plan, stage, use_review=False)
        )

        self.assertEqual(result.status, STAGE_ACCEPTED)
        self.assertIsNone(result.supervisor_decision)
        self.assertIsNotNone(result.commit)

    def test_no_supervisor_wired_failure_is_failed(self):
        stage = make_stage()
        plan = make_plan(stage)
        harness = Harness(
            command_results={DEFAULT_CMD: (1, "FAIL")}
        )

        result = run(harness.run_stage(plan, stage, use_review=False))

        self.assertEqual(result.status, STAGE_FAILED)
        self.assertIn("stage acceptance failed", result.failure_reason)


if __name__ == "__main__":
    unittest.main()