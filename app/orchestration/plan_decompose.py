"""
P2 translation seam: staged development plan -> bounded worker tasks
-> bounded dispatch prompt.

Everything here is deterministic and model-free: a ``PlanStage`` is
translated into bounded ``WorkerTask`` objects (files <= 3, steps
1..5, chain of ``depends_on``), the flat task list mirrors the plan's
stage DAG, and the dispatch prompt carries only the plan objective,
the CURRENT stage's acceptance material and ONE task's scope.

Sibling stages' implementation context, objectives and files, and any
full ``DevelopmentPlan`` dump are deliberately excluded from the
prompt: worker prose is not acceptance evidence, and the current
bounded unit is all a dispatched worker may see.
"""

from __future__ import annotations

from app.orchestration.decompose import _clean_ids, topological_order
from app.schemas import (
    DevelopmentPlan,
    PlanDependencyError,
    PlanStage,
    TaskVerification,
    WorkerTask,
)

MAX_TASK_FILES = 3

MAX_TASK_STEPS = 5

PROMPT_PLAN_OBJECTIVE_LIMIT = 600


def validate_plan(plan: DevelopmentPlan) -> DevelopmentPlan:
    """
    Explicit structural validation gate for a development plan.

    Re-asserts what the pydantic model validator already enforces —
    stages non-empty, unique stage IDs, known dependencies, no
    self-dependency, acyclic — so callers get one explicit API (and
    one future home for failure/blocking semantics) that does not
    depend on when the plan was constructed.

    Structural violations raise ``ValueError`` (mirroring the schema
    validator; a cycle is chained from the schema safety net's
    ``PlanDependencyError``). Returns the plan unchanged.
    """
    if not plan.stages:
        raise ValueError(
            "development plan must contain at least one stage"
        )

    stage_ids = [stage.stage_id for stage in plan.stages]
    duplicates = sorted(
        {sid for sid in stage_ids if stage_ids.count(sid) > 1}
    )
    if duplicates:
        raise ValueError(
            "duplicate stage ids: " + ", ".join(duplicates)
        )

    known = set(stage_ids)
    for stage in plan.stages:
        for dependency in stage.dependencies:
            if dependency == stage.stage_id:
                raise ValueError(
                    f"stage {stage.stage_id!r} depends on itself"
                )
            if dependency not in known:
                raise ValueError(
                    f"stage {stage.stage_id!r} depends on unknown "
                    f"stage {dependency!r}"
                )

    try:
        plan.stage_execution_order()

    except PlanDependencyError as exc:
        raise ValueError(str(exc)) from exc

    return plan


def stage_execution_order(plan: DevelopmentPlan) -> list[PlanStage]:
    """
    Deterministic topological stage order (Kahn, input order wins
    among ready stages).

    Delegates to ``DevelopmentPlan.stage_execution_order`` — the
    schema owns the ordering algorithm so the plan-level and
    task-level seams agree on it; ``PlanDependencyError`` propagates
    for an unorderable stage graph.
    """
    return plan.stage_execution_order()


def _derive_steps(stage: PlanStage) -> list[str]:
    """
    Deterministic, model-free step derivation for one stage.

    P2 has no live model: the first step is the stage's
    ``implementation_context`` (the use case / context the worker
    follows), then one step per acceptance command, so bounded
    execution ends by running the stage's checks.
    ``decompose_stage`` chunks this list into groups of at most
    ``MAX_TASK_STEPS`` steps.
    """
    steps = [stage.implementation_context.strip()]

    for index, command in enumerate(stage.acceptance_commands, 1):
        steps.append(
            f"Run acceptance command {index}: {command.command.strip()}"
        )

    return steps


def _scope_step(files: list[str]) -> str:
    """
    Deterministic step for a task that exists only to carry files
    after the derived steps ran out (a bounded task never gets an
    empty step list).
    """
    if files:
        return (
            "Implement the stage objective for this file group: "
            + ", ".join(files)
        )
    return "Complete this bounded slice of the stage objective."


def _worker_instruction(
    stage: PlanStage,
    task_id: str,
    plan_objective: str,
) -> str:
    """Short instruction naming the stage and the one bounded task."""
    brief = plan_objective.strip()
    if len(brief) > 160:
        brief = brief[:160].rstrip() + "..."

    prefix = f"Stage {stage.stage_id}"
    if brief:
        prefix += f" ({brief})"

    return (
        f"{prefix}: implement ONLY bounded task {task_id}; sibling "
        f"stages and all other files are out of scope."
    )


def decompose_stage(
    stage: PlanStage,
    *,
    objective: str,
) -> list[WorkerTask]:
    """
    Translate ONE ``PlanStage`` into bounded ``WorkerTask`` objects.

    Deterministic rules (no model, no randomness, stable IDs
    ``{stage_id}-t{index:02d}``):

    * ``allowed_files`` is consumed left to right in groups of at
      most ``MAX_TASK_FILES`` (3), so a >3-file stage becomes several
      tasks chained by ``depends_on``; no file is lost or invented.
    * ``steps`` are derived by :func:`_derive_steps` and consumed left
      to right in groups of at most ``MAX_TASK_STEPS`` (5); a task
      whose file group outlives the derived steps gets one
      deterministic scope step naming its own files. Tasks chain
      sequentially within the stage.
    * ``goal`` is ``stage.objective``; ``constraints`` are exactly the
      stage's ``cumulative_invariants`` (carried verbatim).
    * ``verification`` is the stage's FIRST acceptance command, with
      command string and expected exit code preserved exactly.
    * ``worker_instruction`` names the stage, a short plan-objective
      note (``objective``) and says ONLY this bounded task is in
      scope.

    Returns ``_clean_ids(topological_order(tasks))``.
    """
    first = stage.acceptance_commands[0]
    verification = TaskVerification(
        command=first.command,
        expected_exit_code=first.expected_exit_code,
    )

    derived_steps = _derive_steps(stage)
    stage_files = stage.allowed_files

    tasks: list[WorkerTask] = []
    step_index = 0
    file_index = 0
    previous_id: str | None = None

    # Each bounded task takes at most 5 derived steps and at most 3
    # files; the loop ends only when BOTH slices are exhausted
    # (derived_steps is never empty: implementation_context is
    # non-blank, and every iteration advances at least one slice).
    while (
        step_index < len(derived_steps)
        or file_index < len(stage_files)
    ):
        group = derived_steps[step_index : step_index + MAX_TASK_STEPS]
        step_index += len(group)

        files = list(stage_files[file_index : file_index + MAX_TASK_FILES])
        file_index += len(files)

        if not group:
            group = [_scope_step(files)]

        task_id = f"{stage.stage_id}-t{len(tasks):02d}"

        tasks.append(
            WorkerTask(
                id=task_id,
                goal=stage.objective,
                files=files,
                steps=group,
                constraints=list(stage.cumulative_invariants),
                verification=verification,
                worker_instruction=_worker_instruction(
                    stage,
                    task_id,
                    objective,
                ),
                depends_on=[previous_id] if previous_id else [],
            )
        )

        previous_id = task_id

    return _clean_ids(topological_order(tasks))


def _with_depends_on(task: WorkerTask, extra: list[str]) -> WorkerTask:
    """Add cross-stage dependencies to a task, re-validating the
    WorkerTask schema (depends_on stays unique and non-blank)."""
    merged = list(dict.fromkeys([*task.depends_on, *extra]))

    if merged == task.depends_on:
        return task

    data = task.model_dump()
    data["depends_on"] = merged
    return WorkerTask.model_validate(data)


def decompose_plan_to_tasks(plan: DevelopmentPlan) -> list[WorkerTask]:
    """
    Walk every stage in stage order and collect its bounded tasks.

    Cross-stage dependency relativization: every task of stage S also
    depends on the LAST task ID of each stage in ``S.dependencies``,
    so the flat WorkerTask DAG mirrors the PlanStage DAG (the stage
    DAG's edges become edges to the dependency stage's final task)
    instead of leaving cross-stage ordering implicit. Within-stage
    chaining stays as produced by :func:`decompose_stage`.

    Returns ``_clean_ids(topological_order(tasks))``.
    """
    ordered = stage_execution_order(plan)

    tasks: list[WorkerTask] = []
    last_task_id: dict[str, str] = {}

    for stage in ordered:
        stage_tasks = decompose_stage(
            stage,
            objective=plan.objective,
        )

        cross_stage = [
            last_task_id[dependency]
            for dependency in stage.dependencies
        ]
        if cross_stage:
            stage_tasks = [
                _with_depends_on(task, cross_stage)
                for task in stage_tasks
            ]

        tasks.extend(stage_tasks)
        last_task_id[stage.stage_id] = stage_tasks[-1].id

    return _clean_ids(topological_order(tasks))


def _clip(text: str, limit: int) -> str:
    text = text.strip()

    if len(text) <= limit:
        return text

    return text[:limit].rstrip() + "... (truncated)"


def build_stage_dispatch_prompt(
    plan: DevelopmentPlan,
    stage: PlanStage,
    task: WorkerTask,
) -> str:
    """
    Dispatch prompt for ONE bounded task.

    Contains ONLY: the short plan objective, the CURRENT stage's
    objective, implementation context, acceptance conditions,
    cumulative invariants and acceptance commands, plus this task's
    JSON/scope.

    Excluded on purpose: sibling stages' implementation context,
    objectives and files, and the full ``DevelopmentPlan`` dump —
    the prompt states the exclusion explicitly ("Siblings are out of
    scope"). This is the bounded-current-unit contract: worker prose
    is not acceptance evidence; the acceptance commands are.
    """
    plan_objective = _clip(plan.objective, PROMPT_PLAN_OBJECTIVE_LIMIT)

    conditions = "\n".join(
        f"- {condition}" for condition in stage.acceptance_conditions
    )

    invariants = (
        "\n".join(
            f"- {invariant}"
            for invariant in stage.cumulative_invariants
        )
        or "- (none declared for this stage)"
    )

    commands = "\n".join(
        f"- exit {command.expected_exit_code}: {command.command}"
        + (
            f"  # {command.description}"
            if command.description
            else ""
        )
        for command in stage.acceptance_commands
    )

    return f"""BOUNDED DISPATCH: implement exactly the task below.

PLAN OBJECTIVE:
{plan_objective}

CURRENT STAGE: {stage.stage_id}
STAGE OBJECTIVE:
{stage.objective}

STAGE IMPLEMENTATION CONTEXT:
{stage.implementation_context}

ACCEPTANCE CONDITIONS:
{conditions}

CUMULATIVE INVARIANTS:
{invariants}

STAGE ACCEPTANCE COMMANDS (deterministic evidence):
{commands}

CURRENT BOUNDED TASK (the only scope):
{task.model_dump_json(indent=2)}

SCOPE RULES:
- Implement ONLY this bounded task: its files, its steps, its verification command.
- Siblings are out of scope: other stages' objectives, files and implementation context are not included and must not be assumed.
- The full development plan is intentionally not included; no sibling stage's work belongs to this task.
- Worker prose is not acceptance evidence: completion is decided by the acceptance commands above and this task's verification command.
- Do not commit or push unless the bounded task itself says so.""".strip()