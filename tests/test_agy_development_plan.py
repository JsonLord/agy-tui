"""
Deterministic tests for the AGY P2 staged development plan.

Covers: development-plan schema validation (duplicate/unknown/self
dependencies, cycles), stable stage ordering, stage -> bounded-task
translation (file/step splits, verification and acceptance
preservation), dispatch-prompt isolation, and the AGY port of the
WorkPlan decomposition boundary.

No network and no model here: an urllib guard is installed for the
whole module, and every model touchpoint is an injected callable.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
import urllib.request

from app.orchestration.decompose import (
    DecompositionError,
    decompose_plan,
    fallback_decomposition,
    parse_and_validate_plan,
    parse_and_validate_tasks,
    run_stage_acceptance,
)
from app.orchestration.plan_decompose import (
    build_stage_dispatch_prompt,
    decompose_plan_to_tasks,
    decompose_stage,
    stage_execution_order,
    validate_plan,
)
from app.schemas import (
    AcceptanceCommand,
    DevelopmentPlan,
    PlanDependencyError,
    PlanStage,
    TaskVerification,
    WorkerTask,
    WorkPlan,
)


# ---------------------------------------------------------------------------
# urllib guard for this module (proves no test here reaches the network)
# ---------------------------------------------------------------------------

_urllib_visits: list[object] = []

_original_urlopen = urllib.request.urlopen


def _blocked_urlopen(*args, **kwargs):
    _urllib_visits.append(args)
    raise AssertionError(
        "live network use is forbidden in AGY P2 tests"
    )


def setUpModule() -> None:
    urllib.request.urlopen = _blocked_urlopen
    _install_host_runtime_stub()


def tearDownModule() -> None:
    urllib.request.urlopen = _original_urlopen


def _install_host_runtime_stub() -> None:
    """
    ``app.agents.planner`` (owner of ``_extract_json``) imports the
    host-provided ``google.antigravity`` runtime at module import
    time; it is absent outside the host. Register a minimal
    placeholder so the real extraction path can be exercised — still
    no network, no model, no execution of anything else.
    """
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


# ---------------------------------------------------------------------------
# Plan/stage fixtures (raw dicts -> schema validation is always exercised)
# ---------------------------------------------------------------------------

DEFAULT_CONDITION = "Diagnostics keep their current shape."

DEFAULT_INVARIANT = "Never touch files outside the stage scope."


def stage_payload(
    stage_id: str,
    *,
    objective: str | None = None,
    dependencies: tuple[str, ...] = (),
    allowed_files: tuple[str, ...] = ("app/module.py",),
    implementation_context: str | None = None,
    acceptance_commands: tuple[
        tuple[str, int, str], ...
    ] = (("python3 -m pytest -q", 0, "tests stay green"),),
    acceptance_conditions: tuple[str, ...] = (DEFAULT_CONDITION,),
    cumulative_invariants: tuple[str, ...] = (),
) -> dict:
    return {
        "stage_id": stage_id,
        "objective": objective or f"Deliver stage {stage_id}",
        "dependencies": list(dependencies),
        "allowed_files": list(allowed_files),
        "implementation_context": (
            implementation_context
            or f"CONTEXT_{stage_id.upper()}: notes for {stage_id}."
        ),
        "acceptance_commands": [
            {
                "command": command,
                "expected_exit_code": exit_code,
                "description": description,
            }
            for command, exit_code, description in acceptance_commands
        ],
        "acceptance_conditions": list(acceptance_conditions),
        "cumulative_invariants": list(cumulative_invariants),
    }


def plan_payload(*stages: dict) -> dict:
    return {
        "plan_id": "agy-plan-001",
        "objective": "Deliver the staged development plan",
        "stages": list(stages),
        "final_acceptance": [
            {
                "command": "python3 -m compileall -q app tests",
                "expected_exit_code": 0,
                "description": "final compile gate",
            }
        ],
    }


def make_plan(*stages: dict) -> DevelopmentPlan:
    return DevelopmentPlan.model_validate(plan_payload(*stages))


def order_ids(plan: DevelopmentPlan) -> list[str]:
    return [stage.stage_id for stage in stage_execution_order(plan)]


class TestPlanParsing(unittest.TestCase):
    """Enumerated cases 1-6: parsing and structural rejection."""

    def test_valid_development_plan_parses_and_round_trips(self):
        raw = plan_payload(
            stage_payload("alpha"),
            stage_payload("beta", dependencies=("alpha",)),
        )
        plan = DevelopmentPlan.model_validate(raw)

        self.assertEqual(plan.plan_id, "agy-plan-001")
        self.assertEqual(
            plan.objective, "Deliver the staged development plan"
        )
        self.assertEqual(len(plan.stages), 2)

        alpha = plan.stages[0]
        self.assertEqual(alpha.stage_id, "alpha")
        self.assertEqual(alpha.dependencies, [])
        self.assertEqual(alpha.allowed_files, ["app/module.py"])
        self.assertTrue(alpha.implementation_context)
        self.assertEqual(len(alpha.acceptance_commands), 1)
        self.assertIsInstance(
            alpha.acceptance_commands[0], AcceptanceCommand
        )
        self.assertEqual(
            alpha.acceptance_commands[0].command,
            "python3 -m pytest -q",
        )
        self.assertEqual(
            alpha.acceptance_commands[0].expected_exit_code, 0
        )
        self.assertEqual(
            alpha.acceptance_commands[0].description, "tests stay green"
        )
        self.assertEqual(alpha.acceptance_conditions, [DEFAULT_CONDITION])
        self.assertEqual(alpha.cumulative_invariants, [])

        beta = plan.stages[1]
        self.assertEqual(beta.dependencies, ["alpha"])

        self.assertEqual(len(plan.final_acceptance), 1)
        self.assertEqual(
            plan.final_acceptance[0].command,
            "python3 -m compileall -q app tests",
        )

        # round-trip: dump -> validate -> identical dump
        dumped = plan.model_dump()
        revalidated = DevelopmentPlan.model_validate(dumped)
        self.assertEqual(revalidated.model_dump(), dumped)

    def test_missing_or_blank_stage_fields_rejected(self):
        missing_objective = stage_payload("alpha")
        del missing_objective["objective"]

        missing_acceptance = stage_payload("beta")
        del missing_acceptance["acceptance_commands"]

        missing_context = stage_payload("gamma")
        del missing_context["implementation_context"]

        for stage in (
            missing_objective,
            missing_acceptance,
            missing_context,
        ):
            with self.assertRaises(ValueError):
                DevelopmentPlan.model_validate(plan_payload(stage))

        with self.assertRaises(ValueError):
            DevelopmentPlan.model_validate(
                plan_payload(stage_payload("alpha", objective="   "))
            )

    def test_duplicate_stage_ids_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_plan(
                stage_payload("alpha"),
                stage_payload("alpha"),
            )

        self.assertIn("duplicate stage ids", str(ctx.exception))

    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_plan(
                stage_payload("alpha", dependencies=("ghost",))
            )

        self.assertIn("unknown", str(ctx.exception))
        self.assertIn("ghost", str(ctx.exception))

    def test_self_dependency_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            make_plan(
                stage_payload("alpha", dependencies=("alpha",))
            )

        self.assertIn("depends on itself", str(ctx.exception))

    def test_dependency_cycles_rejected(self):
        # 2-cycle: alpha <-> beta
        with self.assertRaises(ValueError):
            make_plan(
                stage_payload("alpha", dependencies=("beta",)),
                stage_payload("beta", dependencies=("alpha",)),
            )

        # 3-cycle: a -> b -> c -> a
        with self.assertRaises(ValueError):
            make_plan(
                stage_payload("a", dependencies=("c",)),
                stage_payload("b", dependencies=("a",)),
                stage_payload("c", dependencies=("b",)),
            )


class TestStageOrdering(unittest.TestCase):
    """Enumerated cases 7-8: deterministic, dependency-respecting order."""

    def test_dependency_order_is_deterministic(self):
        raw_stages = (
            stage_payload("alpha"),
            stage_payload("beta", dependencies=("alpha",)),
            stage_payload("gamma", dependencies=("alpha",)),
            stage_payload("delta", dependencies=("beta", "gamma")),
        )

        first = make_plan(*raw_stages)
        second = make_plan(*raw_stages)

        self.assertEqual(order_ids(first), order_ids(second))
        self.assertEqual(
            order_ids(first),
            ["alpha", "beta", "gamma", "delta"],
        )

        order = order_ids(first)
        for stage in first.stages:
            for dependency in stage.dependencies:
                self.assertLess(
                    order.index(dependency),
                    order.index(stage.stage_id),
                )

        # module-level delegate agrees with the schema-owned helper
        self.assertEqual(
            [stage.stage_id for stage in stage_execution_order(first)],
            [stage.stage_id for stage in first.stage_execution_order()],
        )

    def test_independent_branches_stay_stable(self):
        plan = make_plan(
            stage_payload("branch-one"),
            stage_payload("branch-two"),
        )

        order = order_ids(plan)
        self.assertEqual(order, ["branch-one", "branch-two"])
        self.assertCountEqual(order, ["branch-one", "branch-two"])

        # input order wins among ready stages (stability)
        swapped = make_plan(
            stage_payload("branch-two"),
            stage_payload("branch-one"),
        )
        self.assertEqual(
            order_ids(swapped), ["branch-two", "branch-one"]
        )


class TestPlanValidationGate(unittest.TestCase):
    """Explicit ``validate_plan`` API + PlanDependencyError safety net."""

    def test_validate_plan_returns_the_same_plan(self):
        plan = make_plan(
            stage_payload("alpha"),
            stage_payload("beta", dependencies=("alpha",)),
        )
        self.assertIs(validate_plan(plan), plan)

    def test_validate_plan_reasserts_invalid_constructed_graphs(self):
        empty = DevelopmentPlan.model_construct(
            plan_id="p",
            objective="o",
            stages=[],
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            validate_plan(empty)
        self.assertIn("at least one stage", str(ctx.exception))

        duplicates = DevelopmentPlan.model_construct(
            plan_id="p",
            objective="o",
            stages=[
                PlanStage.model_validate(stage_payload("alpha")),
                PlanStage.model_validate(stage_payload("alpha")),
            ],
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            validate_plan(duplicates)
        self.assertIn("duplicate", str(ctx.exception))

        unknown = DevelopmentPlan.model_construct(
            plan_id="p",
            objective="o",
            stages=[
                PlanStage.model_validate(
                    stage_payload("alpha", dependencies=("ghost",))
                )
            ],
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            validate_plan(unknown)
        self.assertIn("unknown", str(ctx.exception))

        self_dep = DevelopmentPlan.model_construct(
            plan_id="p",
            objective="o",
            stages=[
                PlanStage.model_validate(
                    stage_payload("alpha", dependencies=("alpha",))
                )
            ],
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )
        with self.assertRaises(ValueError) as ctx:
            validate_plan(self_dep)
        self.assertIn("itself", str(ctx.exception))

    def test_stage_execution_order_safety_net_raises_dependency_error(
        self,
    ):
        cyclic = DevelopmentPlan.model_construct(
            plan_id="p",
            objective="o",
            stages=[
                PlanStage.model_validate(
                    stage_payload("alpha", dependencies=("beta",))
                ),
                PlanStage.model_validate(
                    stage_payload("beta", dependencies=("alpha",))
                ),
            ],
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )

        with self.assertRaises(PlanDependencyError):
            cyclic.stage_execution_order()

        # validate_plan mirrors schema semantics on the same plan
        with self.assertRaises(ValueError) as ctx:
            validate_plan(cyclic)
        self.assertNotIsInstance(
            ctx.exception, PlanDependencyError
        )

    def test_stage_execution_order_is_pure(self):
        plan = make_plan(
            stage_payload("alpha"),
            stage_payload("beta", dependencies=("alpha",)),
        )
        before = plan.model_dump()

        order_ids(plan)
        order_ids(plan)

        self.assertEqual(plan.model_dump(), before)


class TestBoundedTaskTranslation(unittest.TestCase):
    """Enumerated cases 9-15: stage -> bounded WorkerTask seam."""

    def test_stage_translates_to_bounded_tasks(self):
        stage = PlanStage.model_validate(
            stage_payload(
                "alpha",
                allowed_files=("app/a.py", "app/b.py"),
                acceptance_commands=(
                    (
                        "python3 -m pytest -q tests/test_alpha.py",
                        0,
                        "alpha stays green",
                    ),
                ),
                cumulative_invariants=(DEFAULT_INVARIANT,),
            )
        )

        tasks = decompose_stage(
            stage, objective="Deliver the staged development plan"
        )

        self.assertGreaterEqual(len(tasks), 1)
        for index, task in enumerate(tasks):
            self.assertEqual(task.id, f"alpha-t{index:02d}")
            self.assertEqual(task.goal, stage.objective)
            self.assertIsInstance(task.verification, TaskVerification)
            self.assertLessEqual(len(task.files), 3)
            self.assertGreaterEqual(len(task.steps), 1)
            self.assertLessEqual(len(task.steps), 5)
            self.assertTrue(all(s.strip() for s in task.steps))
            self.assertIn("alpha", task.worker_instruction)
            self.assertIn(
                "only", task.worker_instruction.lower()
            )

        # task ids are stable across repeated translations
        again = decompose_stage(
            stage, objective="Deliver the staged development plan"
        )
        self.assertEqual(
            [task.id for task in tasks], [task.id for task in again]
        )

    def test_allowed_files_preserved_across_tasks(self):
        files = ("app/one.py", "app/two.py", "app/three.py", "app/four.py", "app/five.py")
        stage = PlanStage.model_validate(
            stage_payload("alpha", allowed_files=files)
        )

        tasks = decompose_stage(stage, objective="plan objective")

        flattened = [path for task in tasks for path in task.files]
        self.assertEqual(
            flattened, list(files), "files lost or invented"
        )
        self.assertEqual(len(flattened), len(set(flattened)))
        for task in tasks:
            self.assertLessEqual(len(task.files), 3)

    def test_verification_command_preserved_exactly(self):
        command = 'python3 -m pytest -q -k "not slow"  # EXACT'
        stage = PlanStage.model_validate(
            stage_payload(
                "alpha",
                allowed_files=("app/one.py", "app/two.py", "app/three.py", "app/four.py"),
                acceptance_commands=((command, 3, "exact check"),),
            )
        )

        tasks = decompose_stage(stage, objective="plan objective")

        self.assertGreaterEqual(len(tasks), 2)
        for task in tasks:
            self.assertEqual(task.verification.command, command)
            self.assertEqual(task.verification.expected_exit_code, 3)

        # stage-level object itself is untouched
        self.assertEqual(stage.acceptance_commands[0].command, command)
        self.assertEqual(
            stage.acceptance_commands[0].expected_exit_code, 3
        )

    def test_acceptance_conditions_and_invariants_preserved(self):
        conditions = (
            "MARKER_CONDITION stays satisfied",
            "No unrelated files change",
        )
        invariants = (
            "MARKER_INVARIANT never violated",
            "Public API keeps its shape",
        )
        stage = PlanStage.model_validate(
            stage_payload(
                "alpha",
                acceptance_conditions=conditions,
                cumulative_invariants=invariants,
            )
        )
        plan = make_plan(stage)

        tasks = decompose_stage(stage, objective=plan.objective)
        for task in tasks:
            self.assertEqual(task.constraints, list(invariants))
            self.assertNotIn("MARKER_CONDITION", task.constraints)

        prompt = build_stage_dispatch_prompt(plan, stage, tasks[0])
        for condition in conditions:
            self.assertIn(condition, prompt)
        for invariant in invariants:
            self.assertIn(invariant, prompt)
            self.assertIn(invariant, tasks[0].constraints)

    def test_dispatch_prompt_excludes_sibling_stages(self):
        current = stage_payload(
            "alpha",
            objective="Wire the dispatch seam",
            allowed_files=("app/orchestration/agy_dispatch.py",),
            implementation_context=(
                "ALPHA_CONTEXT_MARKER: only the current stage's "
                "context may reach the worker."
            ),
        )
        sibling = stage_payload(
            "stage-xray",
            objective="XRAY_OBJECTIVE_MARKER unrelated sibling work",
            dependencies=("alpha",),
            allowed_files=("app/xray/unrelated.py",),
            implementation_context=(
                "XRAY_CONTEXT_MARKER: sibling internals that must "
                "never appear in a dispatch prompt."
            ),
        )
        plan = make_plan(current, sibling)
        alpha_stage = plan.stages[0]

        tasks = decompose_plan_to_tasks(plan)
        alpha_tasks = [
            task for task in tasks if task.id.startswith("alpha-")
        ]
        prompt = build_stage_dispatch_prompt(
            plan, alpha_stage, alpha_tasks[0]
        )

        # current stage present (all required material + plan objective)
        self.assertIn(plan.objective, prompt)
        self.assertIn("Wire the dispatch seam", prompt)
        self.assertIn("ALPHA_CONTEXT_MARKER", prompt)
        self.assertIn(DEFAULT_CONDITION, prompt)
        self.assertIn("alpha-t00", prompt)
        self.assertIn("python3 -m pytest -q", prompt)

        # sibling material absent
        self.assertNotIn("XRAY_CONTEXT_MARKER", prompt)
        self.assertNotIn("XRAY_OBJECTIVE_MARKER", prompt)
        self.assertNotIn("app/xray/unrelated.py", prompt)
        self.assertNotIn("stage-xray", prompt)
        # full DevelopmentPlan dump absent
        self.assertNotIn(plan.model_dump_json(indent=2), prompt)
        # explicit scope statement
        self.assertIn("Siblings are out of scope", prompt)
        self.assertIn("Worker prose is not acceptance evidence", prompt)

    def test_excess_allowed_files_split_into_chained_tasks(self):
        files = tuple(f"app/file_{i}.py" for i in range(7))
        stage = PlanStage.model_validate(
            stage_payload("alpha", allowed_files=files)
        )

        tasks = decompose_stage(stage, objective="plan objective")

        self.assertGreaterEqual(len(tasks), 3)
        self.assertEqual(
            [task for task in tasks][0].depends_on, []
        )
        for index, task in enumerate(tasks):
            self.assertLessEqual(len(task.files), 3)
            if index == 0:
                self.assertEqual(task.depends_on, [])
            else:
                self.assertEqual(
                    task.depends_on, [tasks[index - 1].id]
                )

        flattened = [path for task in tasks for path in task.files]
        self.assertEqual(flattened, list(files))

    def test_plan_to_tasks_mirrors_stage_dag(self):
        raw_stages = (
            stage_payload(
                "root",
                allowed_files=(
                    "app/root_one.py",
                    "app/root_two.py",
                    "app/root_three.py",
                    "app/root_four.py",
                ),
            ),
            stage_payload("brancha", dependencies=("root",)),
            stage_payload(
                "branchb",
                dependencies=("root",),
                allowed_files=(
                    "app/branchb_one.py",
                    "app/branchb_two.py",
                    "app/branchb_three.py",
                    "app/branchb_four.py",
                ),
            ),
            stage_payload(
                "join", dependencies=("brancha", "branchb")
            ),
        )
        plan = make_plan(*raw_stages)

        tasks = decompose_plan_to_tasks(plan)
        ids = [task.id for task in tasks]
        position = {task_id: index for index, task_id in enumerate(ids)}

        self.assertEqual(
            ids,
            [
                "root-t00",
                "root-t01",
                "brancha-t00",
                "branchb-t00",
                "branchb-t01",
                "join-t00",
            ],
        )

        by_id = {task.id: task for task in tasks}
        # tasks of a dependent stage depend on the LAST task id of
        # each dependency stage (stage DAG mirrored across the flat list)
        self.assertEqual(by_id["root-t00"].depends_on, [])
        self.assertEqual(by_id["root-t01"].depends_on, ["root-t00"])
        self.assertEqual(
            by_id["brancha-t00"].depends_on, ["root-t01"]
        )
        self.assertEqual(
            by_id["branchb-t00"].depends_on, ["root-t01"]
        )
        self.assertEqual(
            by_id["branchb-t01"].depends_on,
            ["branchb-t00", "root-t01"],
        )
        self.assertEqual(
            by_id["join-t00"].depends_on,
            ["brancha-t00", "branchb-t01"],
        )

        for task in tasks:
            for dependency in task.depends_on:
                self.assertIn(dependency, position)
                self.assertLess(position[dependency], position[task.id])

        # every stage's files survive the flattening
        for stage in plan.stages:
            stage_files = [
                path
                for task in tasks
                if task.id.startswith(f"{stage.stage_id}-")
                for path in task.files
            ]
            self.assertEqual(stage_files, list(stage.allowed_files))

        # deterministic on an equivalent second plan
        again = decompose_plan_to_tasks(make_plan(*raw_stages))
        self.assertEqual(
            [(t.id, tuple(t.depends_on)) for t in tasks],
            [(t.id, tuple(t.depends_on)) for t in again],
        )


class TestPortedBoundaries(unittest.TestCase):
    """Extra coverage for the AGY ports in app.orchestration.decompose."""

    def test_worker_task_scope_rules_hold(self):
        worker = TaskVerification(command="git diff --check")

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                files=[f"app/f{i}.py" for i in range(4)],
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                files=["/absolute/path.py"],
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                files=["app/escape/../secret.py"],
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                files=["app/Case.py", "APP/case.py"],
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                steps=["step", "   "],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="   ",
                goal="goal",
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
            )

        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                steps=["step"],
                verification=TaskVerification(command="   "),
                worker_instruction="do it",
            )

        # self-dependency is a graph concern: rejected by _clean_ids
        # (DecompositionError) and by DevelopmentPlan, not by schema
        with self.assertRaises(ValueError):
            WorkerTask(
                id="task-00",
                goal="goal",
                steps=["step"],
                verification=worker,
                worker_instruction="do it",
                depends_on=["  "],
            )

        with self.assertRaises(ValueError):
            AcceptanceCommand(command="  ")

        with self.assertRaises(ValueError):
            PlanStage.model_validate(
                stage_payload(
                    "alpha", allowed_files=("../outside.py",)
                )
            )

        with self.assertRaises(ValueError):
            PlanStage.model_validate(
                stage_payload(
                    "alpha", allowed_files=("app/Dup.py", "app/dup.py")
                )
            )

    def test_fallback_decomposition_is_deterministic(self):
        plan = WorkPlan(
            goal="archive old notes",
            files=["app/a.py", "app/b.py", "app/c.py", "app/d.py"],
            steps=[f"step {i}" for i in range(1, 13)],
            constraints=["respect the layout"],
            verification=["git diff --check"],
            worker_instruction="do the archive work",
        )

        first = fallback_decomposition(plan)
        self.assertEqual(
            [task.id for task in first],
            ["task-00", "task-01", "task-02"],
        )
        for index, task in enumerate(first):
            self.assertLessEqual(len(task.steps), 5)
            self.assertLessEqual(len(task.files), 3)
            self.assertEqual(
                task.verification.command, "git diff --check"
            )
            if index:
                self.assertEqual(
                    task.depends_on, [f"task-{index - 1:02d}"]
                )
            else:
                self.assertEqual(task.depends_on, [])

        second = fallback_decomposition(plan)
        self.assertEqual(
            [task.model_dump() for task in first],
            [task.model_dump() for task in second],
        )

        asyncio.run(decompose_plan(plan))  # identical via the async API
        self.assertEqual(
            [task.id for task in asyncio.run(decompose_plan(plan))],
            [task.id for task in first],
        )

    def test_decompose_plan_uses_injected_model_response(self):
        plan = WorkPlan(
            goal="bounded",
            files=["app/x.py"],
            steps=["step one"],
            worker_instruction="do it",
        )
        payload = {
            "tasks": [
                {
                    "id": "model-00",
                    "goal": "Model-decomposed unit",
                    "files": ["app/x.py"],
                    "steps": ["step one"],
                    "constraints": [],
                    "verification": {
                        "command": "git diff --check",
                        "expected_exit_code": 0,
                    },
                    "worker_instruction": "do only this",
                    "depends_on": [],
                }
            ]
        }
        fenced = "```json\n" + json.dumps(payload) + "\n```"

        tasks = asyncio.run(
            decompose_plan(plan, model_response=fenced)
        )
        self.assertEqual([task.id for task in tasks], ["model-00"])
        self.assertEqual(_urllib_visits, [])

    def test_decompose_plan_accepts_injected_model_callable(self):
        plan = WorkPlan(
            goal="bounded",
            files=["app/x.py"],
            steps=["step one"],
            worker_instruction="do it",
        )
        seen: list[tuple[str, str]] = []

        async def fake_model(system: str, prompt: str) -> str:
            seen.append((system, prompt))
            return json.dumps(
                {
                    "tasks": [
                        {
                            "id": "callable-00",
                            "goal": "Injected unit",
                            "files": [],
                            "steps": ["step one"],
                            "constraints": [],
                            "verification": {
                                "command": "git diff --check",
                                "expected_exit_code": 0,
                            },
                            "worker_instruction": "do only this",
                            "depends_on": [],
                        }
                    ]
                }
            )

        tasks = asyncio.run(decompose_plan(plan, model=fake_model))

        self.assertEqual([task.id for task in tasks], ["callable-00"])
        self.assertEqual(len(seen), 1)
        self.assertIn("bounded worker tasks", seen[0][1])
        self.assertIn('"goal": "bounded"', seen[0][1])
        self.assertEqual(_urllib_visits, [])

    def test_parse_and_validate_tasks_rejects_malformed_output(self):
        for payload in (
            "not structured",
            {"tasks": []},
            {"tasks": [{"id": "x"}]},
            {
                "tasks": [
                    {
                        "id": "dup",
                        "goal": "goal",
                        "files": [],
                        "steps": ["step"],
                        "constraints": [],
                        "verification": {
                            "command": "git diff --check",
                            "expected_exit_code": 0,
                        },
                        "worker_instruction": "do it",
                        "depends_on": [],
                    },
                    {
                        "id": "dup",
                        "goal": "goal",
                        "files": [],
                        "steps": ["step"],
                        "constraints": [],
                        "verification": {
                            "command": "git diff --check",
                            "expected_exit_code": 0,
                        },
                        "worker_instruction": "do it",
                        "depends_on": [],
                    },
                ]
            },
            {
                "tasks": [
                    {
                        "id": "task-a",
                        "goal": "goal",
                        "files": [],
                        "steps": ["step"],
                        "constraints": [],
                        "verification": {
                            "command": "git diff --check",
                            "expected_exit_code": 0,
                        },
                        "worker_instruction": "do it",
                        "depends_on": ["missing-task"],
                    }
                ]
            },
            {
                "tasks": [
                    {
                        "id": "loop",
                        "goal": "goal",
                        "files": [],
                        "steps": ["step"],
                        "constraints": [],
                        "verification": {
                            "command": "git diff --check",
                            "expected_exit_code": 0,
                        },
                        "worker_instruction": "do it",
                        "depends_on": ["loop"],
                    }
                ]
            },
        ):
            with self.assertRaises(DecompositionError):
                parse_and_validate_tasks(payload)

    def test_decompose_plan_rejects_unparseable_model_output(self):
        plan = WorkPlan(
            goal="bounded",
            files=[],
            steps=["step one"],
            worker_instruction="do it",
        )
        with self.assertRaises(DecompositionError):
            asyncio.run(
                decompose_plan(plan, model_response="no json here")
            )

    def test_parse_and_validate_plan_schema_validates_output(self):
        plan = parse_and_validate_plan(
            plan_payload(
                stage_payload("alpha"),
                stage_payload("beta", dependencies=("alpha",)),
            )
        )
        self.assertEqual(plan.plan_id, "agy-plan-001")
        self.assertEqual(order_ids(plan), ["alpha", "beta"])

        for payload in (
            ["not", "an", "object"],
            plan_payload(stage_payload("alpha", dependencies=("ghost",))),
            plan_payload(),
        ):
            with self.assertRaises(DecompositionError):
                parse_and_validate_plan(payload)

    def test_run_stage_acceptance_uses_only_injected_runner(self):
        stage = PlanStage.model_validate(
            stage_payload(
                "alpha",
                acceptance_commands=(
                    ("python3 -m pytest -q", 0, "first"),
                    ("git diff --check", 0, "second"),
                ),
            )
        )
        seen: list[tuple[str, int]] = []

        async def runner(command: AcceptanceCommand) -> tuple[int, str]:
            seen.append(
                (command.command, command.expected_exit_code)
            )
            outputs = {
                "python3 -m pytest -q": (0, "39 passed"),
                "git diff --check": (1, "whitespace error"),
            }
            return outputs[command.command]

        report = asyncio.run(
            run_stage_acceptance(stage, run_command=runner)
        )

        self.assertEqual(
            seen,
            [
                ("python3 -m pytest -q", 0),
                ("git diff --check", 0),
            ],
        )
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["checks"]), 2)
        self.assertTrue(report["checks"][0]["ok"])
        self.assertFalse(report["checks"][1]["ok"])
        self.assertEqual(_urllib_visits, [])


class TestNoLiveNetwork(unittest.TestCase):
    def test_urllib_guard_is_installed_and_untouched(self):
        # Case 19 for this module: no flow above reaches urllib; the
        # guard proves it (and would trips loudly if ever called).
        self.assertIs(urllib.request.urlopen, _blocked_urlopen)
        self.assertEqual(_urllib_visits, [])

        install = _install_host_runtime_stub
        install()  # idempotent: never touches the network either
        self.assertEqual(_urllib_visits, [])


if __name__ == "__main__":
    unittest.main()