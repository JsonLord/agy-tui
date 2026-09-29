"""P13 — AGY bounded delegation policy: focused deterministic tests.

Covers the CORE DELEGATION RULES (Part A) as deterministic, model-free
behavior of ``app.orchestration.checkpoint_planning.delegation_policy``:

1. small worker scope (1-3 files, enforced);
2. parallel ONLY when independent + no concurrent writers to one file;
3. continue-while-tests-run (dependent implementation may start while a
   prerequisite's development tests still run);
4. failure does not restart the plan (bounded fresh fixer, independent
   work intact);
5. fresh fixer ownership (the implementer is not its repair worker;
   bounded fixer budget);
plus the TEST POLICY classification (DEVELOPMENT / PROMOTION / LIVE),
token budgets, dispatch/repair prompt contracts, and coordinator
acceptance checklist. No model calls, no network, no I/O.
"""

from __future__ import annotations

import unittest

from app.orchestration.checkpoint_planning.delegation_policy import (
    DEFAULT_DEV_WORKER_MODEL,
    DelegationPlan,
    DelegatedTask,
    TestClassification,
    TokenBudget,
    TaskStatus,
    acceptance_checks,
    budget_for_scope,
    classify_test,
    dispatch_prompt,
    dispatch_waves,
    implementation_frontier,
    promotion_ready,
    reconcile_after_failure,
    repair_prompt,
    spawn_fixer,
    validate_task_budgets,
)
from app.schemas import AcceptanceCommand, PlanStage, WorkerTask

SHA = "9521caaa65a2eaac8fe9197c70afffecbe41ceb8"


def _stage(stage_id: str, files: list[str]) -> PlanStage:
    return PlanStage(
        stage_id=stage_id,
        objective="stage objective",
        dependencies=[],
        allowed_files=list(files),
        implementation_context="context",
        acceptance_commands=[AcceptanceCommand(command="git diff --check")],
        acceptance_conditions=["ok"],
    )


def _task(
    task_id: str,
    stage_id: str,
    files: list[str],
    deps: list[str] | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    budget: TokenBudget = TokenBudget.SMALL,
    verification: list[str] | None = None,
) -> DelegatedTask:
    return DelegatedTask(
        id=task_id,
        stage=stage_id,
        objective="bounded objective",
        owned_files=list(files),
        dependencies=list(deps or []),
        worker_role="dev",
        base_sha=SHA,
        worktree="/wt",
        status=status,
        model_budget=budget,
        verification_commands=list(verification or ["python3 -m pytest tests/t.py -q"]),
    )


def _plan(tasks: list[DelegatedTask], *, limit: int = 2) -> DelegationPlan:
    stages = []
    seen: set[str] = set()
    for t in tasks:
        if t.stage not in seen:
            seen.add(t.stage)
            stages.append(_stage(t.stage, ["a.py", "b.py", "c.py"]))
    return DelegationPlan(
        plan_id="plan-policy",
        objective="policy plan",
        stages=stages,
        tasks=tasks,
        concurrency_limit=limit,
        promotion_gate="PROMOTION_TEST",
    )


class SmallWorkerScopeTests(unittest.TestCase):
    def test_one_to_three_files_allowed(self):
        for n in (1, 2, 3):
            t = _task("A", "s", [f"f{i}.py" for i in range(n)])
            self.assertEqual(len(t.owned_files), n)

    def test_four_files_rejected(self):
        with self.assertRaises(Exception):
            _task("A", "s", ["a.py", "b.py", "c.py", "d.py"])

    def test_duplicate_owned_files_rejected(self):
        with self.assertRaises(Exception):
            _task("A", "s", ["a.py", "a.py"])

    def test_absolute_owned_file_rejected(self):
        with self.assertRaises(Exception):
            _task("A", "s", ["/etc/passwd"])

    def test_base_sha_must_be_hex(self):
        with self.assertRaises(Exception):
            DelegatedTask(
                id="A",
                stage="s",
                objective="o",
                owned_files=["a.py"],
                worker_role="dev",
                base_sha="not-a-sha",
                worktree="/wt",
            )

    def test_to_worker_task_lowers_to_p2_contract(self):
        t = _task("A", "s", ["a.py", "b.py"], verification=["git diff --check"])
        wt: WorkerTask = t.to_worker_task()
        self.assertIsInstance(wt, WorkerTask)
        self.assertEqual(wt.files, ["a.py", "b.py"])
        self.assertEqual(wt.verification.command, "git diff --check")
        self.assertEqual(len(wt.files), 2)  # bounded contract preserved


class ParallelIndependenceTests(unittest.TestCase):
    def test_independent_tasks_wave_together(self):
        plan = _plan([_task("A", "s1", ["a.py"]), _task("B", "s2", ["b.py"])])
        waves = dispatch_waves(plan)
        self.assertEqual(waves[0], ["A", "B"])

    def test_dependent_task_waits_for_prerequisite(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"]),
                _task("B", "s2", ["b.py"]),
                _task("C", "s3", ["c.py"], deps=["A"]),
            ]
        )
        waves = dispatch_waves(plan)
        self.assertEqual(waves[0], ["A", "B"])
        self.assertIn("C", waves[1])
        self.assertNotIn("C", waves[0])

    def test_shared_file_ownership_serialized(self):
        plan = _plan([_task("A", "s1", ["a.py"]), _task("B", "s2", ["a.py"])])
        waves = dispatch_waves(plan)
        # No wave contains both writers of a.py.
        for wave in waves:
            self.assertNotEqual(set(wave), {"A", "B"})
        self.assertEqual(len(waves), 2)

    def test_concurrency_limit_respected(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"]),
                _task("B", "s2", ["b.py"]),
                _task("C", "s3", ["c.py"]),
            ],
            limit=2,
        )
        waves = dispatch_waves(plan)
        self.assertTrue(all(len(w) <= 2 for w in waves))
        self.assertEqual(sorted(x for w in waves for x in w), ["A", "B", "C"])

    def test_cycle_or_unmet_dependency_rejected(self):
        plan = _plan([_task("A", "s1", ["a.py"], deps=["B"]), _task("B", "s2", ["b.py"], deps=["A"])])
        with self.assertRaises(ValueError):
            dispatch_waves(plan)

    def test_completed_tasks_are_not_redispatched(self):
        plan = _plan([_task("A", "s1", ["a.py"]), _task("B", "s2", ["b.py"])])
        waves = dispatch_waves(
            plan, {"A": TaskStatus.VERIFIED, "B": TaskStatus.PENDING}
        )
        self.assertEqual(waves, [["B"]])


class ContinueWhileTestsRunTests(unittest.TestCase):
    def test_dependent_implementation_starts_while_prereq_tests_run(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"], status=TaskStatus.IMPLEMENTED),
                _task("B", "s2", ["b.py"], deps=["A"]),
            ]
        )
        # A code-complete, development tests still running (IMPLEMENTED,
        # not yet VERIFIED). B's IMPLEMENTATION may start.
        frontier = implementation_frontier(
            plan,
            {"A": TaskStatus.IMPLEMENTED, "B": TaskStatus.PENDING},
        )
        self.assertIn("B", frontier)
        self.assertNotIn("A", frontier)

    def test_dependent_blocked_until_prereq_code_complete(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"], status=TaskStatus.RUNNING),
                _task("B", "s2", ["b.py"], deps=["A"]),
            ]
        )
        frontier = implementation_frontier(
            plan, {"A": TaskStatus.RUNNING, "B": TaskStatus.PENDING}
        )
        self.assertNotIn("B", frontier)

    def test_failed_prereq_blocks_dependent(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"], status=TaskStatus.FAILED),
                _task("B", "s2", ["b.py"], deps=["A"]),
            ]
        )
        frontier = implementation_frontier(
            plan, {"A": TaskStatus.FAILED, "B": TaskStatus.PENDING}
        )
        self.assertNotIn("B", frontier)

    def test_promotion_requires_test_completion(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"], status=TaskStatus.VERIFIED),
                _task("B", "s2", ["b.py"], deps=["A"], status=TaskStatus.VERIFIED),
            ]
        )
        # B is VERIFIED and its prereq A is VERIFIED -> promotable.
        self.assertTrue(promotion_ready(plan, {"A": TaskStatus.VERIFIED, "B": TaskStatus.VERIFIED}, "B"))
        # B code-complete (IMPLEMENTED) but tests not run yet -> NOT promotable.
        self.assertFalse(promotion_ready(plan, {"A": TaskStatus.VERIFIED, "B": TaskStatus.IMPLEMENTED}, "B"))


class FailureFixerTests(unittest.TestCase):
    def test_fixer_is_fresh_bounded_and_same_scope(self):
        plan = _plan(
            [_task("A", "s1", ["a.py"], status=TaskStatus.FAILED),
             _task("B", "s2", ["b.py"], status=TaskStatus.VERIFIED)]
        )
        fixer = spawn_fixer(plan, "A", "test_x failed: assert 1==0")
        self.assertEqual(fixer.id, "A-fix-1")
        self.assertEqual(fixer.owned_files, ["a.py"])  # same bounded scope
        self.assertEqual(fixer.stage, "s1")
        self.assertEqual(fixer.worker_role, "dev-fixer")
        self.assertEqual(fixer.model_budget, TokenBudget.TINY)
        self.assertIn("assert 1==0", fixer.objective)
        self.assertIn("regression test first", " ".join(fixer.invariants).lower())

    def test_implementer_is_not_auto_repair_worker(self):
        # A fixer is a distinct id from the implementer, not a status flip.
        plan = _plan([_task("A", "s1", ["a.py"], status=TaskStatus.FAILED)])
        fixer = spawn_fixer(plan, "A", "boom")
        self.assertNotEqual(fixer.id, "A")

    def test_independent_accepted_work_stays_intact(self):
        plan = _plan(
            [_task("A", "s1", ["a.py"], status=TaskStatus.FAILED),
             _task("B", "s2", ["b.py"], status=TaskStatus.VERIFIED)]
        )
        fixer = spawn_fixer(plan, "A", "boom")
        report = reconcile_after_failure(
            plan, "A", {"A": TaskStatus.FAILED, "B": TaskStatus.VERIFIED}, fixer
        )
        self.assertEqual(report["intact_independent"], ["B"])
        self.assertEqual(report["fixer"], "A-fix-1")

    def test_dependents_are_blocked_not_discarded(self):
        plan = _plan(
            [
                _task("A", "s1", ["a.py"], status=TaskStatus.FAILED),
                _task("C", "s2", ["c.py"], deps=["A"]),
            ]
        )
        fixer = spawn_fixer(plan, "A", "boom")
        report = reconcile_after_failure(plan, "A", {"A": TaskStatus.FAILED}, fixer)
        self.assertIn("C", report["blocked_dependents"])

    def test_fixer_budget_bounded(self):
        plan = _plan([_task("A", "s1", ["a.py"], status=TaskStatus.FAILED)])
        for n in (1, 2, 3):
            spawn_fixer(plan, "A", "x", fixer_number=n)
        with self.assertRaises(ValueError):
            spawn_fixer(plan, "A", "x", fixer_number=4)

    def test_fixer_only_for_failed_stage(self):
        plan = _plan([_task("A", "s1", ["a.py"], status=TaskStatus.VERIFIED)])
        with self.assertRaises(ValueError):
            spawn_fixer(plan, "A", "no failure here")


class TestClassificationTests(unittest.TestCase):
    def test_full_suite_is_promotion_test(self):
        self.assertEqual(classify_test("python3 -m pytest -q"), TestClassification.PROMOTION_TEST)
        self.assertEqual(classify_test("python3 -m pytest tests"), TestClassification.PROMOTION_TEST)

    def test_focused_file_is_development_test(self):
        self.assertEqual(
            classify_test("python3 -m pytest tests/test_x.py -q"),
            TestClassification.DEVELOPMENT_TEST,
        )
        self.assertEqual(
            classify_test("python3 -m compileall -q app"), TestClassification.DEVELOPMENT_TEST
        )
        self.assertEqual(classify_test("git diff --check"), TestClassification.DEVELOPMENT_TEST)

    def test_model_command_is_live_smoke(self):
        self.assertEqual(classify_test("agy -p make a plan"), TestClassification.LIVE_MODEL_SMOKE)
        self.assertEqual(
            classify_test("python3 -m app.run_supervised"), TestClassification.LIVE_MODEL_SMOKE
        )

    def test_full_suite_in_task_flagged(self):
        plan = _plan(
            [
                DelegatedTask(
                    id="A",
                    stage="s1",
                    objective="o",
                    owned_files=["a.py"],
                    worker_role="dev",
                    base_sha=SHA,
                    worktree="/wt",
                    verification_commands=["python3 -m pytest -q"],
                )
            ]
        )
        issues = validate_task_budgets(plan)
        self.assertTrue(any("PROMOTION_TEST" in i for i in issues))

    def test_live_smoke_requires_tiny_budget(self):
        plan = _plan(
            [
                _task(
                    "A",
                    "s1",
                    ["a.py"],
                    budget=TokenBudget.NORMAL,
                    verification=["agy -p plan"],
                )
            ]
        )
        issues = validate_task_budgets(plan)
        self.assertTrue(any("TINY" in i for i in issues))

    def test_compliant_plan_has_no_issues(self):
        plan = _plan([_task("A", "s1", ["a.py"])])
        self.assertEqual(validate_task_budgets(plan), [])

    def test_budget_default_small(self):
        self.assertEqual(budget_for_scope(1), TokenBudget.SMALL)
        self.assertEqual(budget_for_scope(3), TokenBudget.SMALL)


class PromptContractTests(unittest.TestCase):
    def test_dispatch_prompt_shape(self):
        t = _task("A", "s1", ["a.py"], verification=["python3 -m pytest tests/t.py -q"])
        t.invariants = ["do not touch b.py"]
        t.forbidden_scope = ["no new dependencies"]
        p = dispatch_prompt(t)
        for marker in (
            "ROLE",
            "You are a bounded implementation worker.",
            "WORKTREE",
            "BASE",
            "TASK",
            "OWNED FILES",
            "READ-ONLY CONTEXT",
            "INVARIANTS",
            "DO NOT",
            "FOCUSED VERIFICATION",
            "DELIVER",
            "DO NOT COMMIT/PUSH unless explicitly authorized.",
        ):
            self.assertIn(marker, p)
        self.assertIn(t.base_sha, p)
        self.assertIn("a.py", p)

    def test_dispatch_prompt_carries_model_selection(self):
        t = _task("A", "s1", ["a.py"])
        self.assertIn(DEFAULT_DEV_WORKER_MODEL, dispatch_prompt(t))

    def test_repair_prompt_shape(self):
        plan = _plan([_task("A", "s1", ["a.py"], status=TaskStatus.FAILED)])
        fixer = spawn_fixer(plan, "A", "test_x failed: assert 1==0")
        p = repair_prompt(fixer)
        self.assertIn("Bounded repair worker.", p)
        self.assertIn("assert 1==0", p)
        self.assertIn("a.py", p)
        self.assertIn("regression first", p.lower())
        self.assertIn("No feature re-planning.", p)


class CoordinatorAcceptanceTests(unittest.TestCase):
    def test_acceptance_checklist_is_disk_authoritative(self):
        t = _task("A", "s1", ["a.py"], verification=["python3 -m pytest tests/t.py -q"])
        checks = acceptance_checks(t)
        self.assertIn("git status --short", checks)
        self.assertIn("git diff --check", checks)
        self.assertIn("git diff --stat", checks)
        self.assertTrue(any("git rev-parse HEAD" in c for c in checks))
        self.assertIn("python3 -m pytest tests/t.py -q", checks)

    def test_acceptance_includes_model_metadata_for_runtime_roles(self):
        t = _task("A", "s1", ["a.py"])
        t.worker_role = "agy-ui-reviewer"
        checks = acceptance_checks(t)
        self.assertTrue(any("session/model/provider" in c for c in checks))


class PlanStructureTests(unittest.TestCase):
    def test_task_must_reference_known_stage(self):
        with self.assertRaises(Exception):
            DelegationPlan(
                plan_id="p",
                objective="o",
                stages=[_stage("s1", ["a.py"])],
                tasks=[_task("A", "NOPE", ["a.py"])],
                concurrency_limit=2,
                promotion_gate="PG",
            )

    def test_task_dependency_must_reference_known_task(self):
        with self.assertRaises(Exception):
            DelegationPlan(
                plan_id="p",
                objective="o",
                stages=[_stage("s1", ["a.py"])],
                tasks=[_task("A", "s1", ["a.py"], deps=["GHOST"])],
                concurrency_limit=2,
                promotion_gate="PG",
            )

    def test_duplicate_task_ids_rejected(self):
        with self.assertRaises(Exception):
            DelegationPlan(
                plan_id="p",
                objective="o",
                stages=[_stage("s1", ["a.py"]), _stage("s2", ["b.py"])],
                tasks=[_task("A", "s1", ["a.py"]), _task("A", "s2", ["b.py"])],
                concurrency_limit=2,
                promotion_gate="PG",
            )


if __name__ == "__main__":
    unittest.main()
