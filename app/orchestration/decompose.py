"""Decomposition boundary between a high-level plan and bounded worker
execution (AGY port of the validated primitive).

A ``WorkPlan`` may legitimately be architecture-wide. The bounded
execution path does not receive it wholesale: it receives ONE bounded
``WorkerTask`` at a time. This module owns that boundary:

* ``decompose_plan``  -- WorkPlan -> ordered list[WorkerTask]
  (model-backed when available; ALWAYS schema-validated before use;
  deterministic fallback when no model is configured).
* ``topological_order`` -- dependency-respecting execution order.
* ``parse_and_validate_plan`` -- schema-validate a raw supervisor
  ``DevelopmentPlan`` payload (never trust the model).
* ``run_stage_acceptance`` / ``run_worker_tasks`` -- P3/P4 gate and
  execution plumbing: deterministic, injected callables only, NOT
  wired to a live model in P2 (no live call ever happens here).

The parsed model output is never trusted: every task passes through
the ``WorkerTask`` pydantic schema, unique-ID and dependency-integrity
checks. Invalid output raises :class:`DecompositionError` rather than
reaching a worker.

``PlanDependencyError`` (stage-graph failures, owned by
``app.schemas``) is re-exported through this boundary next to
``DecompositionError`` (task-graph failures) so staged callers have
both failure types where they decompose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.schemas import (
    AcceptanceCommand,
    DevelopmentPlan,
    PlanDependencyError,  # re-exported: stage-graph failure type
    PlanStage,
    TaskVerification,
    WorkerTask,
    WorkPlan,
)


class DecompositionError(RuntimeError):
    pass


DECOMPOSER_SYSTEM = """
You convert one high-level implementation plan into a SHORT sequence of
BOUNDED worker tasks executed one at a time by a bounded-stage worker.

Hard rules for EVERY task:
- exactly one concrete implementation objective;
- at most 3 repository-relative file paths;
- 1 to 5 atomic steps;
- exactly one deterministic verification command
  ({"command": "...", "expected_exit_code": 0});
- no scope overlap beyond what dependencies require;
- "depends_on" lists only task IDs that must succeed first;
- tasks are ordered so dependencies appear before dependents.

Do NOT summarize or restate the overall architecture in each task.
Do NOT drop any part of the plan: every plan step must be covered by
exactly one task.

Return exactly one JSON object:

{
  "tasks": [
    {
      "id": "stable-slug-id",
      "goal": "one concrete implementation objective",
      "files": ["repo/relative/path"],
      "steps": ["atomic action"],
      "constraints": ["constraint"],
      "verification": {"command": "cmd", "expected_exit_code": 0},
      "worker_instruction": "short implementation instruction",
      "depends_on": []
    }
  ]
}
"""


def _extract_json(text: str) -> dict[str, Any]:
    """
    ``_extract_json`` from :mod:`app.agents.planner`, imported lazily.

    ``app.agents.planner`` pulls in the host-provided
    ``google.antigravity`` runtime at module import time, and that
    runtime is not present in a plain test environment. The extractor
    itself is pure, so importing it at call time keeps this module
    importable (and decomposition importable everywhere) without the
    host runtime.
    """
    from app.agents.planner import _extract_json as planner

    return planner(text)


def _clean_ids(tasks: list[WorkerTask]) -> list[WorkerTask]:
    ids = [task.id for task in tasks]

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise DecompositionError(
            f"duplicate worker task ids: {', '.join(duplicates)}"
        )

    known = set(ids)
    for task in tasks:
        unknown = sorted(set(task.depends_on) - known)
        if unknown:
            raise DecompositionError(
                f"task {task.id!r} depends on unknown ids: "
                + ", ".join(unknown)
            )
        if task.id in task.depends_on:
            raise DecompositionError(
                f"task {task.id!r} depends on itself"
            )

    return tasks


def topological_order(tasks: list[WorkerTask]) -> list[WorkerTask]:
    """Stable, dependency-respecting order; cycles -> DecompositionError."""
    by_id = {task.id: task for task in tasks}
    ordered: list[WorkerTask] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task: WorkerTask) -> None:
        if task.id in visited:
            return
        if task.id in visiting:
            raise DecompositionError(
                f"dependency cycle involving task {task.id!r}"
            )
        visiting.add(task.id)
        for dep in task.depends_on:
            visit(by_id[dep])
        visiting.discard(task.id)
        visited.add(task.id)
        ordered.append(task)

    for task in tasks:
        visit(task)

    return ordered


def parse_and_validate_tasks(payload: Any) -> list[WorkerTask]:
    """Schema-validate raw model output. Never trusts the model."""
    if isinstance(payload, dict):
        raw_tasks = payload.get("tasks")
    elif isinstance(payload, list):
        raw_tasks = payload
    else:
        raise DecompositionError(
            "decomposer output must be an object with 'tasks' or a list"
        )

    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise DecompositionError("decomposer returned no tasks")

    tasks: list[WorkerTask] = []
    for index, raw in enumerate(raw_tasks):
        try:
            tasks.append(WorkerTask.model_validate(raw))
        except Exception as exc:
            raise DecompositionError(
                f"worker task #{index + 1} failed schema validation: "
                f"{exc}"
            ) from exc

    return _clean_ids(tasks)


def parse_and_validate_plan(payload: Any) -> DevelopmentPlan:
    """
    Schema-validate a raw ``DevelopmentPlan`` payload (stage DAG and
    all) before it can influence decomposition. Never trusts the
    model: malformed, ambiguous or structurally invalid plans (blank
    fields, duplicate/unknown/self dependencies, cycles) become a
    :class:`DecompositionError`.
    """
    if not isinstance(payload, dict):
        raise DecompositionError(
            "development plan output must be a JSON object"
        )

    try:
        plan = DevelopmentPlan.model_validate(payload)
    except Exception as exc:
        raise DecompositionError(
            f"development plan failed schema validation: {exc}"
        ) from exc

    return plan



def fallback_decomposition(plan: WorkPlan) -> list[WorkerTask]:
    """
    Deterministic, model-free decomposition.

    Groups plan steps into consecutive chunks of at most 5, attaches
    plan files in chunks of at most 3 (cycled across tasks), and gives
    every task the plan's first verification command (or a
    ``git diff --check`` baseline). Tasks form a linear chain so
    execution order is exactly the plan order.
    """
    steps = [s for s in (item.strip() for item in plan.steps) if s]
    if not steps:
        raise DecompositionError("plan contains no usable steps")

    files = list(dict.fromkeys(plan.files))  # unique, order-preserving
    verification_command = (
        plan.verification[0] if plan.verification
        else "git diff --check"
    )

    chunks = [
        steps[i : i + 5] for i in range(0, len(steps), 5)
    ]
    task_count = len(chunks)

    tasks: list[WorkerTask] = []
    for index, chunk in enumerate(chunks):
        file_slice = files[index::task_count][:3] if files else []
        depends_on = (
            [f"task-{index - 1:02d}"] if index else []
        )

        tasks.append(
            WorkerTask(
                id=f"task-{index:02d}",
                goal=chunk[0],
                files=file_slice,
                steps=chunk,
                constraints=list(plan.constraints),
                verification=TaskVerification(
                    command=verification_command,
                    expected_exit_code=0,
                ),
                worker_instruction=(
                    plan.worker_instruction
                    if index == 0
                    else (
                        "Implement only the listed steps for this "
                        "bounded task."
                    )
                ),
                depends_on=depends_on,
            )
        )

    return _clean_ids(tasks)


async def decompose_plan(
    plan: WorkPlan,
    *,
    model_response: str | None = None,
    model: Callable[[str, str], Awaitable[str]] | None = None,
) -> list[WorkerTask]:
    """
    WorkPlan -> ordered list[WorkerTask].

    Injection contract for deterministic tests:
    * ``model_response``: raw text as the model would have returned it
      (skips any live call entirely);
    * ``model``: async ``(system, prompt) -> text`` stand-in.

    With neither, falls back to :func:`fallback_decomposition`.
    Whatever path produced the payload, output is schema-validated and
    dependency-checked before it can reach a worker.
    """
    raw_text: str | None = model_response

    if raw_text is None and model is not None:
        prompt = (
            "HIGH-LEVEL PLAN:\n\n"
            + plan.model_dump_json(indent=2)
            + "\n\nProduce the bounded worker tasks now."
        )
        raw_text = await model(DECOMPOSER_SYSTEM, prompt)

    if raw_text is not None:
        try:
            payload = _extract_json(raw_text)
        except Exception as exc:
            raise DecompositionError(
                f"decomposer output was not parseable JSON: {exc}"
            ) from exc
        return topological_order(parse_and_validate_tasks(payload))

    return topological_order(fallback_decomposition(plan))


# ---------------------------------------------------------------------------
# P3/P4 seam: gate + execution plumbing (no live model in P2)
# ---------------------------------------------------------------------------


@dataclass
class WorkerTaskResult:
    worker_task_id: str
    attempt: int
    status: str  # SUCCESS | FAILED | SKIPPED_DEPENDENCY | SKIPPED_UPSTREAM_FAILURE
    validation_ok: bool
    validation_output: str = ""
    changed_files: list[str] = field(default_factory=list)
    session_id: str | None = None
    failure_reason: str = ""
    worker_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_task_id": self.worker_task_id,
            "attempt": self.attempt,
            "status": self.status,
            "validation_ok": self.validation_ok,
            "validation_output": self.validation_output,
            "changed_files": list(self.changed_files),
            "session_id": self.session_id,
            "failure_reason": self.failure_reason,
        }


ExecuteTask = Callable[
    [WorkerTask, int, str | None],
    Awaitable[str],
]
ValidateTask = Callable[[WorkerTask], Awaitable[tuple[bool, str]]]
RunCommand = Callable[
    [AcceptanceCommand],
    Awaitable[tuple[int, str]],
]


async def run_stage_acceptance(
    stage: PlanStage,
    *,
    run_command: RunCommand,
) -> dict[str, Any]:
    """
    P3/P4 acceptance gate for ONE stage — NOT wired to a live model
    (or to a shell) in P2.

    Every ``AcceptanceCommand`` is executed exactly once, in order,
    through the injected ``run_command`` callable (which returns
    ``(exit_code, output)``). Nothing here spawns processes itself,
    and command strings / expected exit codes are passed verbatim.
    """
    checks: list[dict[str, Any]] = []

    for command in stage.acceptance_commands:
        exit_code, output = await run_command(command)
        checks.append(
            {
                "stage_id": stage.stage_id,
                "command": command.command,
                "description": command.description,
                "expected_exit_code": command.expected_exit_code,
                "exit_code": exit_code,
                "ok": exit_code == command.expected_exit_code,
                "output": output,
            }
        )

    return {
        "stage_id": stage.stage_id,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }


async def run_worker_tasks(
    tasks: list[WorkerTask],
    *,
    execute_task: ExecuteTask,
    validate_task: ValidateTask,
    max_attempts: int = 2,
    final_validation: Callable[[], Awaitable[tuple[bool, str]]]
    | None = None,
    collect_changed_files: Callable[[WorkerTask], list[str]]
    | None = None,
    session_id_for_task: Callable[[WorkerTask], str | None]
    | None = None,
) -> dict[str, Any]:
    """
    P3/P4 execution loop — NOT wired to a live model in P2.

    Sequential, dependency-aware execution of bounded tasks. Every
    step goes through injected callables (``execute_task``,
    ``validate_task``, ...); this module never contacts a provider by
    itself.

    For each task (in topological order):
      execute -> deterministic task validation -> retry the SAME task id
      on failure (attempt increments; repair context carries the
      validation output) up to ``max_attempts``.

    On task failure after retries:
      * dependent tasks are SKIPPED_DEPENDENCY (the dependent subtree
        is NOT executed);
      * independent tasks still run;
      * a structured ``replan`` section names the failed task and the
        blocked descendants — the replan scope is bounded to the failed
        task plus its dependency subtree, never the whole original
        WorkPlan.

    Whole-plan validation runs only when every task succeeded.
    """
    ordered = topological_order(tasks)

    results: list[WorkerTaskResult] = []
    failed_ids: set[str] = set()

    def blocked_by(task: WorkerTask) -> list[str]:
        return [
            dep for dep in task.depends_on if dep in failed_ids
        ]

    for task in ordered:
        blockers = blocked_by(task)
        if blockers:
            results.append(
                WorkerTaskResult(
                    worker_task_id=task.id,
                    attempt=0,
                    status="SKIPPED_DEPENDENCY",
                    validation_ok=False,
                    failure_reason=(
                        "dependency failed: "
                        + ", ".join(sorted(blockers))
                    ),
                )
            )
            failed_ids.add(task.id)
            continue

        attempt = 0
        repair_context: str | None = None
        task_result: WorkerTaskResult | None = None

        while attempt < max_attempts:
            attempt += 1
            worker_output = await execute_task(
                task, attempt, repair_context
            )
            ok, output = await validate_task(task)

            task_result = WorkerTaskResult(
                worker_task_id=task.id,
                attempt=attempt,
                status="SUCCESS" if ok else "FAILED",
                validation_ok=ok,
                validation_output=output,
                worker_output=worker_output,
                changed_files=(
                    collect_changed_files(task)
                    if collect_changed_files
                    else []
                ),
                session_id=(
                    session_id_for_task(task)
                    if session_id_for_task
                    else None
                ),
                failure_reason=(
                    "" if ok else
                    f"task validation failed on attempt {attempt}"
                ),
            )

            if ok:
                break

            repair_context = (
                f"Retry {attempt} of task {task.id!r}.\n"
                f"PREVIOUS VALIDATION FAILURE:\n{output}"
            )

        assert task_result is not None
        results.append(task_result)

        if not task_result.validation_ok:
            failed_ids.add(task.id)

    status = "SUCCESS" if not failed_ids else "TASK_FAILURE"

    replan: dict[str, Any] | None = None
    if failed_ids:
        failed_direct = [
            r.worker_task_id
            for r in results
            if r.status == "FAILED"
        ]
        blocked_descendants = [
            r.worker_task_id
            for r in results
            if r.status in
            ("SKIPPED_DEPENDENCY", "SKIPPED_UPSTREAM_FAILURE")
        ]

        replan = {
            "failed_task_ids": sorted(failed_direct),
            "blocked_task_ids": sorted(blocked_descendants),
            "replan_scope_task_ids": sorted(failed_ids),
            "instruction": (
                "Replan may replace ONLY the failed tasks and their "
                "blocked descendants. Already-succeeded tasks are "
                "preserved; do not restate the whole WorkPlan."
            ),
        }

    final: dict[str, Any] | None = None
    if final_validation is not None and not replan:
        ok, output = await final_validation()
        final = {"ok": ok, "output": output}
        if not ok:
            status = "WHOLE_PLAN_VALIDATION_FAILED"

    return {
        "status": status,
        "task_results": [r.to_dict() for r in results],
        "replan": replan,
        "final_validation": final,
    }