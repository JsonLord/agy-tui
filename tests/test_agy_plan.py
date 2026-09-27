"""
P4 multi-stage plan-loop tests — network-free, everything injected.

The 18-case contract matrix (contract §5, ``tests/test_agy_plan.py``):

 1  multi-stage plan: 3-stage linear plan (s1 <- s2 <- s3) accepted in
    topological order; on_stage called in order; PLAN_ACCEPTED
 2  only dependency-ready stages run: s3's executor NEVER called when
    s2 fails first
 3  stage failure stops the loop: s2 STAGE_FAILED -> s3 never runs,
    status PLAN_FAILED, stopped_at=s2, plan gate never ran
 4  stage rejection stops: s2 STAGE_REJECTED -> PLAN_REJECTED
 5  plan-level final acceptance executes (exact run_command sequence)
 6  plan-level cumulative gate runs ONCE at plan level and must be
    green for PLAN_ACCEPTED (red gate is deterministically forced to
    repair, never PLAN_ACCEPTED)
 7  PLAN_ACCEPTED only when all stages accepted AND final gate green
    AND supervisor ACCEPT
 8  supervisor REJECT at plan level -> PLAN_REJECTED
 9  supervisor REPAIR at plan level -> bounded repair stage
    ('plan-repair-1'), accepted stages NOT re-run, repair stage gets
    its own commit + gate, re-review -> PLAN_ACCEPTED
10  no whole-plan restart: accepted stages' commit SHAs unchanged
    after the plan repair (same commit records reused)
11  deterministic override: final acceptance fails + supervisor says
    ACCEPT -> forced REPAIR with deterministic_override=True, never
    PLAN_ACCEPTED
12  failover events aggregated from stages into
    PlanRunResult.failover_events (fake reviewer emits an event)
13  session ids + source aggregated and persisted (fake executor's
    stdout is empty, so the TaskSessionLog carries source 'readback');
    the injected session_capturer override is exercised too
14  plan run-state recoverable: save_plan/load_plan round-trips a full
    PlanRunResult (stage_results, repair_lineage, failover_events,
    session sources); stage run-state still round-trips (P3
    regression through the same run_state)
15  stage stop preserves prior stage state (plan.json shows s1
    STAGE_ACCEPTED with its commit even though s2 failed)
16  max_plan_repairs enforced: a second repair request is not honored
    (stops at PLAN_REPAIR, no loop) + max_plan_repairs=0 denies repair
    entirely
17  execute_plan never raises on repo/test/model outcomes (STAGE_FAILED
    handled; invalid / non-dict supervisor answers fail closed, never
    raised)
18  no banned runtime dependency (static check — documented scope:
    whole-file text of the two NEW P4 modules agy_plan.py and
    agy_session.py AND of the two MODIFIED modules agy_runstate.py and
    agy_runtime.py; banned words gemma/nemotron/minicpm/jules/hermes
    case-insensitive + hardcoded model ids ('alias-'; roles only))

All external effects are injected fakes: no network, no subprocess, no
model, no sleep. The stage machinery itself is P3's ``execute_stage``
(actual code, not a mock) driven by fake executor callables.
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from app.config import AgySettings
from app.orchestration import agy_plan as agy_plan_module
from app.orchestration.agy_execute import (
    STAGE_ACCEPTED,
    STAGE_FAILED,
)
from app.orchestration.agy_plan import (
    PLAN_ACCEPTED,
    PLAN_FAILED,
    PLAN_INCOMPLETE,
    PLAN_IN_PROGRESS,
    PLAN_REJECTED,
    PLAN_REPAIR,
    PlanRunResult,
    execute_plan,
)
from app.orchestration.agy_runstate import PlanRunState, StageRunState
from app.orchestration.agy_session import TaskSessionLog
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage

STAGE_CMD = "python3 stage-gate-runner"
FINAL_CMD = "python3 plan-final-acceptance"
CUM_CMD = "python3 plan-cumulative-suite"
DEFAULT_FILES = ["calculator.py", "tests/test_calculator.py"]
SETTINGS = AgySettings.from_env({})


def install_host_runtime_stub() -> None:
    """Host-runtime stub, same as the other P3/P4 test modules."""
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
    stage_id: str,
    dependencies: list[str] | None = None,
    allowed_files: list[str] | None = None,
) -> PlanStage:
    return PlanStage(
        stage_id=stage_id,
        objective=f"Implement the {stage_id} slice of the calculator plan",
        dependencies=list(dependencies or []),
        allowed_files=list(allowed_files or DEFAULT_FILES),
        implementation_context=(
            f"Implement {stage_id} in calculator.py with a "
            "deterministic unittest case in tests/test_calculator.py."
        ),
        acceptance_commands=[
            AcceptanceCommand(
                command=STAGE_CMD,
                expected_exit_code=0,
                description=f"{stage_id} stage gate",
            )
        ],
        acceptance_conditions=[f"{stage_id} behaves as specified"],
        cumulative_invariants=["add(a, b) still returns a + b"],
    )


def make_linear_plan(stages: int = 3) -> DevelopmentPlan:
    """s1 <- s2 <- s3 (each depends on the previous)."""
    ids = [f"s{index}" for index in range(1, stages + 1)]
    made = [
        make_stage(stage_id, [f"s{index}"] if index else [])
        for index, stage_id in enumerate(ids)
    ]
    return DevelopmentPlan(
        plan_id="agp-p4-test",
        objective="extend the calculator with deterministic tests",
        stages=made,
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


# ---------------------------------------------------------------------------
# The deterministic harness (every injected seam of execute_plan)
# ---------------------------------------------------------------------------


class PlanHarness:
    """
    One deterministic plan-execution environment recording exactly
    what each injected seam was asked to do.
    """

    def __init__(
        self,
        *,
        decisions: list[str] | None = None,
        repair_targets: list[str] | None = None,
        command_results: dict[str, list | tuple] | None = None,
        stage_changed: dict[str, list[str]] | None = None,
        changed: list[str] | None = None,
        stage_validate: dict[str, bool] | None = None,
        emit_event: dict | None = None,
        review_hook=None,
        session_source: str | None = "readback",
    ):
        # Decision queue: pops while more than one entry, then the
        # last entry repeats (identical to P3's Harness.review).
        self.decisions = list(decisions or ["ACCEPT"])
        self.repair_targets = list(
            repair_targets or ["make the failing check deterministic"]
        )
        self.command_results = {
            # queues: list -> successive results, last repeats;
            # tuple -> constant (exit_code, output).
            command: (
                [tuple(value)]
                if isinstance(value, tuple)
                else [tuple(entry) for entry in value]
            )
            for command, value in (command_results or {}).items()
        }
        self.stage_changed = dict(stage_changed or {})
        self.default_changed = list(
            changed if changed is not None else DEFAULT_FILES
        )
        self.stage_validate = dict(stage_validate or {})
        self.emit_event = emit_event
        self.review_hook = review_hook
        self.session_source = session_source

        self.built_stages: list[str] = []
        self.executed: list[tuple[str, str, int]] = []
        self.validated: list[str] = []
        self.command_calls: list[tuple[str, int]] = []
        self.review_calls: list[dict] = []
        self.commit_evidence: list[dict] = []
        self.commit_count = 0
        self.session_logs: dict[str, TaskSessionLog] = {}
        self._session_counter = 0
        self._emitted_event = False

    # -- seam implementations ------------------------------------------------

    def make_executor(self, plan, stage):
        self.built_stages.append(stage.stage_id)
        changed = list(
            self.stage_changed.get(stage.stage_id, self.default_changed)
        )
        log = TaskSessionLog()
        self.session_logs[stage.stage_id] = log

        harness = self

        async def execute_task(task, attempt, repair_context):
            harness._session_counter += 1
            if harness.session_source:
                # Emulates stdout WITHOUT a session id falling back to
                # the server read-back (the P3 defect fix).
                session_id = f"ses_{harness._session_counter:012x}"
                log.record(task.id, session_id, harness.session_source)
            harness.executed.append((stage.stage_id, task.id, attempt))
            return f"worker prose: implemented {task.id}"

        async def validate_task(task):
            harness.validated.append(task.id)
            ok = harness.stage_validate.get(task.id, True)
            return ok, ("verification passed" if ok else "verification broke")

        def collect_changed_files(task=None):
            return list(changed)

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
        queue = self.command_results.get(command.command)
        if queue is None:
            return 0, "all green"
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def make_commit(self, evidence):
        self.commit_count += 1
        self.commit_evidence.append(evidence)
        stage_id = evidence["stage_id"]
        return {
            "plan_id": evidence["plan_id"],
            "stage_id": stage_id,
            "parent_sha": f"{self.commit_count - 1:040x}",
            "commit_sha": f"{self.commit_count:040x}",
            "changed_files": list(evidence["changed_files"]),
            "message": (
                f"AGY stage {stage_id}: "
                f"{evidence.get('stage_objective', '')}"
            ),
        }

    async def review(self, evidence=None, on_failover_event=None):
        record = {
            "evidence": evidence,
            "on_failover_event": on_failover_event,
        }
        self.review_calls.append(record)

        if self.review_hook is not None:
            return self.review_hook(evidence, on_failover_event)

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
            "model": "supervisor-role-model",
            "failover_event": None,
        }
        if (
            self.emit_event is not None
            and on_failover_event is not None
            and not self._emitted_event
        ):
            self._emitted_event = True
            on_failover_event(self.emit_event)
            result["failover_event"] = self.emit_event
        return result

    # -- driver ---------------------------------------------------------------

    def run(
        self,
        plan,
        *,
        run_state=None,
        session_capturer=None,
        on_stage=None,
        on_failover_event=None,
        max_plan_repairs=1,
        use_review=True,
    ):
        return run(
            execute_plan(
                plan,
                settings=SETTINGS,
                repo="/nonexistent-agy-plan-e2e",
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
                supervisor_review=self.review if use_review else None,
                run_state=run_state,
                session_capturer=session_capturer,
                on_stage=on_stage,
                on_failover_event=on_failover_event,
                max_plan_repairs=max_plan_repairs,
            )
        )


def command_tuple(harness, command: str) -> int:
    """How often ``command`` was executed."""
    return sum(1 for name, _ in harness.command_calls if name == command)


def green_acceptance(*harness_decisions: str) -> list[str]:
    return list(harness_decisions)


# ---------------------------------------------------------------------------
# Cases 1-4: ordering + stops
# ---------------------------------------------------------------------------


class TestStageLoopControl(unittest.TestCase):
    def test_01_multi_stage_linear_plan_all_accepted_in_order(self):
        """Case 1: 3-stage linear plan accepted in topological order."""
        plan = make_linear_plan(3)
        harness = PlanHarness(decisions=green_acceptance(*["ACCEPT"] * 4))
        stage_events: list[str] = []

        result = harness.run(plan, on_stage=lambda sid, res: stage_events.append(sid))

        self.assertEqual(result.status, PLAN_ACCEPTED)
        self.assertEqual(result.stage_order, ["s1", "s2", "s3"])
        self.assertEqual(
            result.stage_order,
            [s.stage_id for s in plan.stage_execution_order()],
        )
        self.assertEqual(harness.built_stages, ["s1", "s2", "s3"])
        self.assertEqual(stage_events, ["s1", "s2", "s3"])
        for stage_id in ("s1", "s2", "s3"):
            staged = result.stage_results[stage_id]
            self.assertEqual(staged["status"], STAGE_ACCEPTED)
            self.assertIsNotNone(staged["commit"])
        self.assertEqual(result.repair_lineage, [])
        self.assertIsNone(result.stopped_at)
        self.assertEqual(result.plan_gate["decision"], "ACCEPT")
        self.assertTrue(result.cumulative_invariants_ok)
        self.assertTrue(result.final_acceptance["ok"])

    def test_02_only_dependency_ready_stages_run(self):
        """Case 2: s3's executor is NEVER called when s2 fails first."""
        plan = make_linear_plan(3)
        harness = PlanHarness(decisions=green_acceptance("ACCEPT"),
                              stage_changed={"s2": []})  # s2 zero-diff

        result = harness.run(plan)

        self.assertEqual(harness.built_stages, ["s1", "s2"])
        self.assertNotIn("s3", harness.built_stages)
        executed_stages = {stage_id for stage_id, _, _ in harness.executed}
        self.assertEqual(executed_stages, {"s1", "s2"})
        self.assertNotIn("s3", result.stage_results)

    def test_03_stage_failure_stops_the_loop(self):
        """Case 3: s2 STAGE_FAILED -> PLAN_FAILED, stopped_at=s2."""
        plan = make_linear_plan(3)
        harness = PlanHarness(decisions=green_acceptance("ACCEPT"),
                              stage_changed={"s2": []})

        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_FAILED)
        self.assertEqual(result.stopped_at, "s2")
        self.assertEqual(result.stage_results["s2"]["status"], STAGE_FAILED)
        self.assertNotIn("s3", result.stage_results)
        # The plan gate never ran: no final-acceptance execution.
        self.assertEqual(command_tuple(harness, FINAL_CMD), 0)
        self.assertFalse(result.final_acceptance.get("checks"))
        self.assertTrue(result.final_acceptance.get("skipped"))
        self.assertEqual(result.plan_gate_reviews, [])

    def test_04_stage_rejection_stops_the_loop(self):
        """Case 4: s2 STAGE_REJECTED -> PLAN_REJECTED (gate not run)."""
        plan = make_linear_plan(3)
        # s1 accepted, supervisor REJECTs s2 (s2 must have made real
        # changes so the review path runs).
        harness = PlanHarness(decisions=["ACCEPT", "REJECT"])

        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_REJECTED)
        self.assertEqual(result.stopped_at, "s2")
        self.assertEqual(
            result.stage_results["s2"]["status"], "STAGE_REJECTED"
        )
        self.assertNotIn("s3", result.stage_results)
        self.assertEqual(command_tuple(harness, FINAL_CMD), 0)
        # Only s1 + s2 were reviewed; the plan gate was never reached.
        self.assertEqual(len(harness.review_calls), 2)
        self.assertEqual(result.plan_gate_reviews, [])


# ---------------------------------------------------------------------------
# Cases 5-7: the plan-level gate
# ---------------------------------------------------------------------------


class TestPlanLevelGate(unittest.TestCase):
    def test_05_final_acceptance_executes_after_all_stages(self):
        """Case 5: exact verifiable run_command sequence through the gate."""
        plan = make_linear_plan(3)
        harness = PlanHarness(decisions=green_acceptance(*["ACCEPT"] * 4))

        result = harness.run(plan)

        sequence = [(name, expected) for name, expected in harness.command_calls]
        base = [(STAGE_CMD, 0), (CUM_CMD, 0)] * 3
        self.assertEqual(sequence, base + [(FINAL_CMD, 0), (CUM_CMD, 0)])
        # Final acceptance ran exactly once, as the LAST stage work.
        self.assertEqual(command_tuple(harness, FINAL_CMD), 1)
        self.assertEqual(result.final_acceptance["ok"], True)
        self.assertFalse(result.final_acceptance.get("skipped"))

    def test_06_cumulative_gate_runs_once_at_plan_level_and_must_be_green(self):
        """Case 6: one plan-level cumulative run; red gate != ACCEPTED."""
        # (a) green: cumulative executed once per stage AND once at plan level.
        plan = make_linear_plan(1)
        harness = PlanHarness(decisions=green_acceptance("ACCEPT", "ACCEPT"))
        result = harness.run(plan)
        self.assertEqual(result.status, PLAN_ACCEPTED)
        self.assertEqual(command_tuple(harness, CUM_CMD), 2)  # 1 stage + 1 plan
        self.assertTrue(result.cumulative_invariants_ok)

        # (b) red AT THE PLAN GATE: deterministically forced, never accepted.
        plan = make_linear_plan(1)
        red = PlanHarness(
            decisions=green_acceptance("ACCEPT", "ACCEPT", "ACCEPT",
                                       "ACCEPT", "ACCEPT"),
            command_results={CUM_CMD: [(0, "green at stage level"),
                                       (1, "red at plan gate")]},
        )
        red_result = red.run(plan)
        self.assertNotEqual(red_result.status, PLAN_ACCEPTED)
        self.assertEqual(red_result.status, PLAN_REPAIR)
        # The FIRST plan-level gate is where the red cumulative show:
        # model ACCEPT was deterministically forced to REPAIR.
        first_gate = red_result.plan_gate_reviews[0]
        self.assertEqual(first_gate["decision"], "REPAIR")
        self.assertTrue(first_gate["deterministic_override"])
        self.assertIn("plan-level cumulative gate failed",
                      first_gate["override_reason"])

    def test_07_accepted_requires_all_stages_then_green_gate_then_accept(self):
        """Case 7: PLAN_ACCEPTED only for all-accepted + green + ACCEPT."""
        plan = make_linear_plan(2)
        harness = PlanHarness(
            decisions=green_acceptance("ACCEPT", "ACCEPT", "ACCEPT")
        )
        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_ACCEPTED)
        self.assertTrue(
            all(
                stage["status"] == STAGE_ACCEPTED
                for stage in result.stage_results.values()
            )
        )
        self.assertTrue(result.final_acceptance["ok"])
        self.assertTrue(result.cumulative_invariants_ok)
        self.assertEqual(result.plan_gate["decision"], "ACCEPT")
        self.assertEqual(result.plan_gate["deterministic_override"], False)
        self.assertNotIn("override_reason", result.plan_gate)
        self.assertEqual(len(result.plan_gate_reviews), 1)




# ---------------------------------------------------------------------------
# Cases 8-10: supervisor decisions at plan level + bounded repair
# ---------------------------------------------------------------------------


class TestPlanSupervisorDecisions(unittest.TestCase):
    def test_08_supervisor_reject_at_plan_level(self):
        """Case 8: supervisor REJECT at the plan gate -> PLAN_REJECTED."""
        plan = make_linear_plan(1)
        harness = PlanHarness(decisions=["ACCEPT", "REJECT"])

        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_REJECTED)
        self.assertEqual(result.plan_gate["decision"], "REJECT")
        self.assertIn("supervisor reason: REJECT",
                      result.plan_gate["rationale"])
        self.assertEqual(result.repair_lineage, [])
        self.assertEqual(harness.built_stages, ["s1"])  # no repair stage
        # Green deterministic gates: the reject came from the model.
        self.assertFalse(result.plan_gate["deterministic_override"])
        self.assertEqual(len(result.plan_gate_reviews), 1)
        self.assertTrue(result.final_acceptance["ok"])

    def test_09_plan_repair_stage_own_commit_and_gate_then_accept(self):
        """Case 9: plan REPAIR -> 'plan-repair-1' with own commit + gate."""
        plan = make_linear_plan(2)
        harness = PlanHarness(
            decisions=[
                "ACCEPT",   # stage s1 gate
                "ACCEPT",   # stage s2 gate
                "REPAIR",   # plan-level gate #1
                "ACCEPT",   # the repair STAGE's own gate
                "ACCEPT",   # plan-level gate #2 (re-review)
            ],
            repair_targets=["add a subtract regression check"],
        )

        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_ACCEPTED)
        self.assertEqual(result.repair_lineage, ["plan-repair-1"])
        self.assertEqual(
            harness.built_stages, ["s1", "s2", "plan-repair-1"]
        )
        # The repair stage has its OWN commit and its OWN stage gate.
        repair = result.stage_results["plan-repair-1"]
        self.assertEqual(repair["status"], STAGE_ACCEPTED)
        self.assertIsNotNone(repair["commit"])
        self.assertEqual(repair["commit"]["stage_id"], "plan-repair-1")
        self.assertEqual(repair["supervisor_decision"]["decision"], "ACCEPT")
        # Original stages were NOT re-run; exactly 3 commits total.
        self.assertEqual(harness.commit_count, 3)
        # Two plan-level reviews: REPAIR then ACCEPT.
        self.assertEqual(
            [r["decision"] for r in result.plan_gate_reviews],
            ["REPAIR", "ACCEPT"],
        )
        self.assertEqual(result.plan_gate["decision"], "ACCEPT")

    def test_10_no_whole_plan_restart_commits_unchanged(self):
        """Case 10: after a plan repair the accepted stages keep SHAs."""
        plan = make_linear_plan(2)
        harness = PlanHarness(
            decisions=["ACCEPT", "ACCEPT", "REPAIR", "ACCEPT", "ACCEPT"]
        )

        result = harness.run(plan)

        self.assertEqual(result.status, PLAN_ACCEPTED)
        sha_s1 = result.stage_results["s1"]["commit"]["commit_sha"]
        sha_s2 = result.stage_results["s2"]["commit"]["commit_sha"]
        self.assertEqual(sha_s1, f"{1:040x}")
        self.assertEqual(sha_s2, f"{2:040x}")
        # No stage executed twice: every (stage, task) pair at most one.
        pairs = [(stage, task) for stage, task, _ in harness.executed]
        self.assertEqual(len(pairs), len(set(pairs)))
        for stage_id in ("s1", "s2"):
            self.assertEqual(harness.built_stages.count(stage_id), 1)
        # Commit records for accepted stages were written exactly once.
        for stage_id in ("s1", "s2"):
            commits = [
                record
                for record in harness.commit_evidence
                if record["stage_id"] == stage_id
            ]
            self.assertEqual(len(commits), 1)
        # Repair stage staged on top (its own third commit).
        self.assertEqual(
            result.stage_results["plan-repair-1"]["commit"]["parent_sha"],
            f"{2:040x}",
        )

    def test_11_deterministic_override_forces_repair_never_accepted(self):
        """Case 11: failing final acceptance + model ACCEPT -> REPAIR."""
        plan = make_linear_plan(1)
        harness = PlanHarness(
            decisions=green_acceptance(*["ACCEPT"] * 6),
            command_results={FINAL_CMD: (1, "final acceptance broken")},
        )

        result = harness.run(plan)

        self.assertNotEqual(result.status, PLAN_ACCEPTED)
        self.assertEqual(result.status, PLAN_REPAIR)
        self.assertEqual(result.repair_lineage, ["plan-repair-1"])
        # The FIRST plan gate carried the model's ACCEPT and was
        # force-converted by the deterministic rule.
        first_gate = result.plan_gate_reviews[0]
        self.assertEqual(first_gate["original_decision"], "ACCEPT")
        self.assertEqual(first_gate["decision"], "REPAIR")
        self.assertTrue(first_gate["deterministic_override"])
        self.assertIn("final acceptance failed",
                      first_gate["override_reason"])
        # Final acceptance is still red on disk.
        for check in result.final_acceptance["checks"]:
            if check["command"] == FINAL_CMD:
                self.assertFalse(check["ok"])
        self.assertFalse(result.final_acceptance["ok"])


# ---------------------------------------------------------------------------
# Cases 12-13: failover aggregation + session sources
# ---------------------------------------------------------------------------


class TestAggregation(unittest.TestCase):
    def test_12_failover_events_aggregated_from_stages(self):
        """Case 12: a stage-emitted event lands in the plan result once."""
        plan = make_linear_plan(1)
        event = {
            "role": "supervisor",
            "primary_model": "role-a",
            "fallback_model": "role-b",
            "active_model": "role-b",
            "primary_attempts": 4,
            "fallback_reason": "rate_limit_exhausted",
            "session_id": None,
            "fallback_count": 1,
            "parent_session_id": None,
            "ts": "2026-09-27T00:00:00+02:00",
        }
        harness = PlanHarness(
            decisions=green_acceptance("ACCEPT", "ACCEPT"),
            emit_event=event,
        )
        hook_events: list[dict] = []

        result = harness.run(
            plan, on_failover_event=hook_events.append
        )

        self.assertEqual(result.status, PLAN_ACCEPTED)
        self.assertEqual(len(result.failover_events), 1)
        self.assertEqual(result.failover_events[0], event)
        # Forwarded to the caller's hook exactly ONCE.
        self.assertEqual(hook_events, [event])
        # The event arose in the STAGE review pathway.
        self.assertIsNotNone(harness.review_calls[0]["on_failover_event"])
        stage_run = result.stage_results["s1"]
        self.assertEqual(len(stage_run["failover_events"]), 1)

    def test_13_session_ids_and_source_aggregated_and_persisted(self):
        """Case 13: empty-stdout fake -> per-task source 'readback'."""
        plan = make_linear_plan(2)
        harness = PlanHarness(
            decisions=green_acceptance("ACCEPT", "ACCEPT", "ACCEPT"),
            session_source="readback",
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_state = PlanRunState(tmp, plan.plan_id)
            result = harness.run(plan, run_state=run_state)

            # Aggregated into the plan result.
            self.assertTrue(result.session_ids)
            for session_id in result.session_ids:
                self.assertTrue(session_id.startswith("ses_"))
            sources = set(result.session_sources.values())
            self.assertEqual(sources, {"readback"})
            # Per-task stage results carry the id (the bridge works).
            first_task = result.stage_results["s1"]["task_results"][0]
            self.assertTrue(first_task["session_id"].startswith("ses_"))
            self.assertEqual(
                result.session_sources[first_task["worker_task_id"]],
                "readback",
            )
            # Persisted per task in run state.
            stage_state = StageRunState(tmp, plan.plan_id, "s1")
            records = stage_state.load_tasks()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["session_source"], "readback")
            self.assertTrue(records[0]["session_id"].startswith("ses_"))
            # plan.json carries the source map too.
            loaded = run_state.load_plan()
            self.assertEqual(loaded["session_sources"],
                             result.session_sources)

        # Injected session_capturer overrides the executor's log.
        harness_b = PlanHarness(decisions=green_acceptance("ACCEPT", "ACCEPT"))
        injected = {
            "s1-t00": {"session_id": "ses_injected1", "source": "stdout"},
        }
        result_b = harness_b.run(plan, session_capturer=injected)
        first_task_b = result_b.stage_results["s1"]["task_results"][0]
        self.assertEqual(first_task_b["session_id"], "ses_injected1")
        self.assertEqual(result_b.session_sources["s1-t00"], "stdout")


# ---------------------------------------------------------------------------
# Cases 14-15: run-state recovery + preserved prior state
# ---------------------------------------------------------------------------


class TestPlanRunState(unittest.TestCase):
    def test_14_full_round_trip_and_p3_stage_regression(self):
        """Case 14: save_plan/load_plan round-trips everything."""
        plan = make_linear_plan(2)
        harness = PlanHarness(
            decisions=["ACCEPT", "ACCEPT", "REPAIR", "ACCEPT", "ACCEPT"],
            emit_event={"role": "dispatch", "fallback_reason": "rate_limited",
                        "fallback_count": 1},
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_state = PlanRunState(tmp, plan.plan_id)
            result = harness.run(plan, run_state=run_state)

            recovered = run_state.load_plan()
            self.assertEqual(recovered, result.to_dict())
            # Explicit field-by-field checks (the contract's round trip).
            self.assertEqual(recovered["repair_lineage"],
                             ["plan-repair-1"])
            self.assertEqual(len(recovered["failover_events"]), 1)
            self.assertEqual(recovered["stage_results"]["s2"]["status"],
                             STAGE_ACCEPTED)
            self.assertTrue(
                all(source in {"stdout", "known", "readback", "none"}
                    for source in recovered["session_sources"].values())
            )
            # P3 stage state still round-trips from the SAME root.
            for stage_id in ("s1", "s2", "plan-repair-1"):
                stage_state = StageRunState(tmp, plan.plan_id, stage_id)
                self.assertEqual(
                    stage_state.load_stage(),
                    result.stage_results[stage_id],
                )
                self.assertEqual(
                    len(stage_state.load_tasks()),
                    len(result.stage_results[stage_id]["task_results"]),
                )
            commit = StageRunState(tmp, plan.plan_id, "s1").load_commit()
            self.assertEqual(commit["commit_sha"], f"{1:040x}")

    def test_15_stage_stop_preserves_prior_stage_state(self):
        """Case 15: plan.json keeps s1 ACCEPTED-with-commit though s2 failed."""
        plan = make_linear_plan(3)
        harness = PlanHarness(
            decisions=green_acceptance("ACCEPT"),
            stage_changed={"s2": []},
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_state = PlanRunState(tmp, plan.plan_id)
            result = harness.run(plan, run_state=run_state)

            self.assertEqual(result.status, PLAN_FAILED)
            self.assertEqual(result.stopped_at, "s2")
            persisted = run_state.load_plan()
            self.assertEqual(persisted["status"], PLAN_FAILED)
            self.assertEqual(persisted["stopped_at"], "s2")
            s1 = persisted["stage_results"]["s1"]
            self.assertEqual(s1["status"], STAGE_ACCEPTED)
            self.assertIsNotNone(s1["commit"])
            self.assertEqual(s1["commit"]["commit_sha"], f"{1:040x}")
            s2 = persisted["stage_results"]["s2"]
            self.assertEqual(s2["status"], STAGE_FAILED)
            self.assertIsNone(s2["commit"])


# ---------------------------------------------------------------------------
# Cases 16-18: bounded repairs, no-raise discipline, static check
# ---------------------------------------------------------------------------


class TestBoundedAndDiscipline(unittest.TestCase):
    def test_16_max_plan_repairs_enforced(self):
        """Case 16: a second repair request is NOT honored (no loop)."""
        plan = make_linear_plan(1)
        harness = PlanHarness(
            decisions=["ACCEPT",   # stage gate
                       "REPAIR",   # plan gate #1
                       "ACCEPT",   # repair stage's gate
                       "REPAIR"],  # plan gate #2 -> exhausted, stop
        )

        result = harness.run(plan, max_plan_repairs=1)

        self.assertEqual(result.status, PLAN_REPAIR)
        self.assertEqual(result.repair_lineage, ["plan-repair-1"])
        self.assertEqual(
            harness.built_stages, ["s1", "plan-repair-1"]
        )
        # Exactly TWO plan-level reviews: no third request honored.
        self.assertEqual(len(result.plan_gate_reviews), 2)
        # plan-final acceptance ran for gate #1, AGAIN as the repair
        # stage's own acceptance target (no deterministic failure ->
        # the repair targets the plan-level checks), and for gate #2.
        self.assertEqual(command_tuple(harness, FINAL_CMD), 3)
        # No further stage than the single repair was ever built.

        # max_plan_repairs=0 denies the plan repair entirely.
        zero_plan = make_linear_plan(1)
        zero_harness = PlanHarness(
            decisions=["ACCEPT", "REPAIR"]
        )
        zero_result = zero_harness.run(zero_plan, max_plan_repairs=0)
        self.assertEqual(zero_result.status, PLAN_REPAIR)
        self.assertEqual(zero_result.repair_lineage, [])
        self.assertEqual(zero_harness.built_stages, ["s1"])
        self.assertEqual(len(zero_result.plan_gate_reviews), 1)

    def test_17_never_raises_on_outcomes(self):
        """Case 17: STAGE_FAILED and bad supervisor answers are handled."""
        # (a) a fake that fails outright -> PLAN_FAILED, no exception.
        fail_plan = make_linear_plan(2)
        fail_harness = PlanHarness(
            decisions=green_acceptance("ACCEPT"),
            stage_changed={"s1": []},
        )
        failed = fail_harness.run(fail_plan)
        self.assertEqual(failed.status, PLAN_FAILED)
        self.assertEqual(failed.stopped_at, "s1")

        # (b) invalid enum at the PLAN gate -> fail-closed REJECT.
        def invalid_enum(evidence, on_event=None):
            if "final_acceptance" in (evidence or {}):
                return {"decision": "YEAH", "rationale": "feels right"}
            return {"decision": "ACCEPT", "rationale": "stage green",
                    "repair_targets": [], "model": "supervisor-role-model"}

        plan_b = make_linear_plan(1)
        harness_b = PlanHarness(review_hook=invalid_enum)
        result_b = harness_b.run(plan_b)
        self.assertEqual(result_b.status, PLAN_REJECTED)
        self.assertEqual(result_b.plan_gate["rationale"],
                         "gatekeeper response invalid")
        self.assertTrue(result_b.plan_gate["deterministic_override"] is False)

        # (c) non-dict answer at the PLAN gate -> same fail-closed path.
        def garbage(evidence, on_event=None):
            if "final_acceptance" in (evidence or {}):
                return "total garbage, not JSON at all"
            return {"decision": "ACCEPT", "rationale": "stage green",
                    "repair_targets": [], "model": "supervisor-role-model"}

        plan_c = make_linear_plan(1)
        result_c = PlanHarness(review_hook=garbage).run(plan_c)
        self.assertEqual(result_c.status, PLAN_REJECTED)
        self.assertEqual(result_c.plan_gate["rationale"],
                         "gatekeeper response invalid")

        # (d) make_executor returning a wrong shape is a WIRING error
        # and does propagate (documented contract boundary).
        broken = PlanHarness(decisions=green_acceptance("ACCEPT"))
        original = broken.make_executor

        def bad_executor(plan_arg, stage):
            return original(plan_arg, stage)[:2]

        broken.make_executor = bad_executor  # type: ignore[assignment]
        with self.assertRaises(TypeError):
            broken.run(make_linear_plan(1))

    def test_18_no_banned_runtime_dependency_static_check(self):
        """
        Case 18: static check — documented check scope.

        Scope: whole-file source text of the two NEW P4 modules
        (app/orchestration/agy_plan.py, app/orchestration/agy_session.py)
        AND of the two MODIFIED modules (agy_runstate.py, agy_runtime.py)
        — comments and docstrings included. The modified pair was
        already whole-file checked for the runner-backed banned words
        by the P3 suite (tests/test_agy_execute.py case 23) and P4 only
        added neutral orchestration code on top; checking them whole
        keeps this test self-contained (no dependency on git state).
        Checks: banned runtime words (gemma/nemotron/minicpm/jules/
        hermes, case-insensitive) and NO hardcoded model ids — only
        ROLES and injected settings are referenced (forbidden model-id
        marker: the provider model prefix 'alias-').
        """
        banned_words = ["gemma", "nemotron", "minicpm", "jules", "hermes"]
        forbidden_model_markers = ["alias-"]
        scope = [
            "app/orchestration/agy_plan.py",
            "app/orchestration/agy_session.py",
            "app/orchestration/agy_runstate.py",
            "app/orchestration/agy_runtime.py",
        ]

        texts = {}
        for relative in scope:
            texts[relative] = Path(relative).read_text(
                encoding="utf-8"
            ).lower()

        for relative, lowered in texts.items():
            for word in banned_words:
                with self.subTest(module=relative, banned=word):
                    self.assertNotIn(word, lowered)
            for marker in forbidden_model_markers:
                with self.subTest(module=relative, model_marker=marker):
                    self.assertNotIn(marker, lowered)

        # Roles, not models: the driver is wired through the injected
        # supervisor/seam names and settings objects only.
        self.assertIn("supervisor_review", texts["app/orchestration/agy_plan.py"])
        self.assertNotIn("os.environ", texts["app/orchestration/agy_plan.py"])
        self.assertNotIn("urllib", texts["app/orchestration/agy_plan.py"])
        # Plan driver itself performs no networking of its own.
        self.assertIn("urlopen", texts["app/orchestration/agy_session.py"])


if __name__ == "__main__":
    unittest.main()