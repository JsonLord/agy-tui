"""AGY bounded delegation policy (P13 framework).

Canonical representation of the bounded parallel/sequential workflow
established across P8-P12, as a first-class, deterministic, testable
artifact. This module is pure: no I/O, no network, no model calls, no
process spawning — it computes DISPATCH DECISIONS that the coordinator
(control plane) executes. Nothing here executes a worker.

What is REUSED (not duplicated):

* :class:`app.schemas.DevelopmentPlan` / :class:`PlanStage` — the stage
  DAG (ids, dependencies, allowed_files, acceptance commands, topological
  ``stage_execution_order``). A :class:`DelegationPlan` embeds one.
* :class:`app.schemas.WorkerTask` / :class:`TaskVerification` — the P2
  bounded-worker contract (one goal, <=3 files, one deterministic
  verification, dependency ids). A :class:`DelegatedTask` carries the
  policy dimensions on top and can lower to a ``WorkerTask``.

What is NEW here (the policy layer only):

* per-task ``base_sha`` / ``worktree`` / ``worker_role`` / ``worker_model``
  / ``token_budget`` / ``test_policy`` / ``status`` (the dispatch record);
* scope enforcement (small worker scope, no concurrent writers to the same
  product file, explicit dependency edges);
* dispatch waves under a concurrency limit (parallel ONLY when independent);
* continue-while-tests-run (a dependent stage's IMPLEMENTATION may start as
  soon as its prerequisite's implementation is complete — test completion is
  a promotion condition, not an implementation blocker);
* failure handling (bounded fresh fixer, independent stages unaffected,
  bounded fixer budget);
* explicit test policy (DEVELOPMENT_TEST / PROMOTION_TEST /
  LIVE_MODEL_SMOKE) and token budgets (TINY / SMALL / NORMAL);
* the dispatch and repair PROMPT CONTRACTS;
* the coordinator acceptance checklist (worker prose never proves
  completion — disk state is authoritative).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas import (
    AcceptanceCommand,
    DevelopmentPlan,
    PlanStage,
    TaskVerification,
    WorkerTask,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TokenBudget(str, Enum):
    """Qualitative per-task budget (explicit on every dispatch record)."""

    TINY = "TINY"
    SMALL = "SMALL"
    NORMAL = "NORMAL"


class TestKind(str, Enum):
    """The three test classes AGY distinguishes (never conflates)."""

    DEVELOPMENT_TEST = "DEVELOPMENT_TEST"
    PROMOTION_TEST = "PROMOTION_TEST"
    LIVE_MODEL_SMOKE = "LIVE_MODEL_SMOKE"


class TaskStatus(str, Enum):
    """Lifecycle of one delegated task (deterministic state names)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    IMPLEMENTED = "IMPLEMENTED"  # code done; development tests may be running
    VERIFIED = "VERIFIED"  # development tests + acceptance commands passed
    ACCEPTED = "ACCEPTED"  # promotion gate passed (accepted at boundary)
    FAILED = "FAILED"
    REJECTED = "REJECTED"  # rejected by the promotion gate
    CANCELLED = "CANCELLED"


#: Production role routing is FIXED and separate from development worker
#: selection (which lives on the dispatch record, per task).
PRODUCTION_ROLES = {
    "planning": "gemini via AGY CLI",
    "planning_analyst": "mimo-flash read-only",
    "gatekeeper": "mimo-pro",
    "runtime_workers": "configured named OpenCode agents",
    "fallbacks": "existing AGY fallback policy",
}

#: Default development worker selection (dispatch-record field; does NOT
#: alter production roles). Coordinator may override per task.
DEFAULT_DEV_WORKER_MODEL = "helmholtz/alias-qwen3.8-flash-next"


# ---------------------------------------------------------------------------
# The dispatch record (extends, does not replace, the P2 WorkerTask)
# ---------------------------------------------------------------------------


class DelegatedTask(BaseModel):
    """One bounded implementation dispatch under the AGY policy.

    Small scope is enforced: one objective, 1-3 owned files (or one tightly
    bounded coherent module), explicit dependencies, an exact base SHA and
    worktree, an explicit token budget and test policy, and a worker-model
    selection that belongs to the dispatch record (never to the production
    role table).
    """

    id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=400)
    owned_files: list[str] = Field(min_length=1, max_length=3)
    dependencies: list[str] = Field(default_factory=list)
    worker_role: str = Field(min_length=1)
    worker_model: str = DEFAULT_DEV_WORKER_MODEL
    verification_commands: list[str] = Field(default_factory=list)
    model_budget: TokenBudget = TokenBudget.SMALL
    status: TaskStatus = TaskStatus.PENDING
    base_sha: str = Field(min_length=7, max_length=40)
    worktree: str = Field(min_length=1)
    #: Specific allowed adjacent paths/symbols the worker may READ (not write).
    read_only_context: list[str] = Field(default_factory=list)
    #: Short numbered invariants the worker must hold.
    invariants: list[str] = Field(default_factory=list)
    #: Explicit scope exclusions (forbidden scope).
    forbidden_scope: list[str] = Field(default_factory=list)

    @field_validator("id", "stage", "objective", "worker_role")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("owned_files")
    @classmethod
    def _owned_files_shape(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("owned_files must not contain blanks")
            if item.startswith("/") or item.startswith("~/"):
                raise ValueError(
                    f"owned_files must be repo-relative, got {item!r}"
                )
            cleaned.append(item)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("owned_files must be unique")
        return cleaned

    @field_validator("dependencies")
    @classmethod
    def _deps_unique(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("dependencies must be unique")
        return cleaned

    @field_validator("verification_commands")
    @classmethod
    def _cmds_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return cleaned

    @field_validator("base_sha")
    @classmethod
    def _sha_shape(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value):
            raise ValueError(f"base_sha must be a hex git SHA, got {value!r}")
        return value.lower()

    def to_worker_task(self) -> WorkerTask:
        """Lower to the existing P2 bounded-worker contract (no duplication).

        The policy dimensions (budgets, model, worktree) stay on this
        record; the P2 shape carries exactly what the coding worker sees.
        """
        return WorkerTask(
            id=self.id,
            goal=self.objective,
            files=list(self.owned_files),
            steps=[f"step-{i}" for i in range(1, 2)],
            constraints=list(self.invariants) + list(self.forbidden_scope),
            verification=TaskVerification(
                command=self.verification_commands[0]
                if self.verification_commands
                else "git diff --check"
            ),
            worker_instruction=dispatch_prompt(self),
            depends_on=list(self.dependencies),
        )


# ---------------------------------------------------------------------------
# The delegation plan (embeds, does not replace, the P2 stage DAG)
# ---------------------------------------------------------------------------


class DelegationPlan(BaseModel):
    """Bounded delegation plan: a P2 stage DAG + policy constraints.

    ``stages`` reuse :class:`PlanStage` (scope + dependencies + acceptance);
    ``tasks`` are the per-stage dispatch records; ``concurrency_limit``
    bounds independent implementation children; ``promotion_gate`` names
    the boundary where PROMOTION_TEST runs (once, canonical).
    """

    plan_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    stages: list[PlanStage] = Field(min_length=1)
    tasks: list[DelegatedTask] = Field(default_factory=list)
    concurrency_limit: int = Field(default=2, ge=1, le=4)
    promotion_gate: str = Field(min_length=1)

    @field_validator("plan_id", "objective", "promotion_gate")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _validate_structure(self) -> "DelegationPlan":
        # 1. Stage DAG validity — delegate to the EXISTING P2 validation.
        DevelopmentPlan(
            plan_id=self.plan_id,
            objective=self.objective,
            stages=list(self.stages),
            final_acceptance=[
                AcceptanceCommand(command="git diff --check")
            ],
        )

        # 2. Every task maps to exactly one known stage; deps are task ids.
        stage_ids = {s.stage_id for s in self.stages}
        task_ids = [t.id for t in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("duplicate task ids")
        known_tasks = set(task_ids)
        for task in self.tasks:
            if task.stage not in stage_ids:
                raise ValueError(
                    f"task {task.id!r} references unknown stage "
                    f"{task.stage!r}"
                )
            for dep in task.dependencies:
                if dep not in known_tasks:
                    raise ValueError(
                        f"task {task.id!r} depends on unknown task {dep!r}"
                    )

        # 3. Scope + shared-file rules (small worker scope, one writer per
        #    product file).
        enforce_delegation_scope(self)
        return self

    def task(self, task_id: str) -> DelegatedTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(f"unknown task {task_id!r}")

    def tasks_for_stage(self, stage_id: str) -> list[DelegatedTask]:
        return [t for t in self.tasks if t.stage == stage_id]


# ---------------------------------------------------------------------------
# Policy enforcement (pure, deterministic)
# ---------------------------------------------------------------------------


def enforce_delegation_scope(plan: DelegationPlan) -> None:
    """Reject oversized tasks and concurrent writers to one product file.

    * owned_files is already capped at 3 by the schema;
    * two tasks in the SAME dispatch wave may not own the same file —
      shared-file ownership is an explicit dependency unless the
      coordinator serializes them (enforced via the wave computation,
      which refuses to wave-parallelize conflicting tasks).
    """
    # Same-stage tasks run in sequence within their stage, so the real
    # concurrency risk is cross-stage wave parallelism; that is checked in
    # dispatch_waves(). Here we only reject self-overlap within a task
    # (already unique) and empty plans.
    if not plan.stages:
        raise ValueError("delegation plan needs at least one stage")


def dispatch_waves(
    plan: DelegationPlan,
    statuses: dict[str, TaskStatus | str] | None = None,
    *,
    concurrency_limit: int | None = None,
) -> list[list[str]]:
    """Wave-decompose the task DAG under the concurrency limit.

    Wave ``k`` holds the tasks whose task-level dependencies are all in
    earlier waves. Within a wave, tasks that share an owned file are NOT
    parallel-safe: they are split into consecutive sub-waves (one writer
    per product file at a time). A wave is never larger than
    ``concurrency_limit`` (tasks beyond the limit move to the next wave).

    This encodes the two core rules:
    * parallel ONLY when independent (DAG);
    * no concurrent writers to the same product file.
    """
    limit = plan.concurrency_limit if concurrency_limit is None else concurrency_limit
    statuses = statuses or {}

    done_statuses = {
        TaskStatus.VERIFIED,
        TaskStatus.ACCEPTED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    }

    remaining = [t for t in plan.tasks if statuses.get(t.id) not in done_statuses]
    resolved: set[str] = {
        t.id for t in plan.tasks if statuses.get(t.id) in done_statuses
    }
    waves: list[list[str]] = []

    while remaining:
        ready = [
            t for t in remaining if all(dep in resolved for dep in t.dependencies)
        ]
        if not ready:
            unfinished = ", ".join(t.id for t in remaining)
            raise ValueError(
                f"dependency cycle or unmet dependency among tasks: {unfinished}"
            )

        # Order: input order (deterministic), then split by shared files
        # (no two tasks with an overlapping owned file in the same wave).
        current: list[DelegatedTask] = []
        owned_so_far: set[str] = set()
        deferred: list[DelegatedTask] = []
        for task in ready:
            if owned_so_far & set(task.owned_files):
                deferred.append(task)
            else:
                current.append(task)
                owned_so_far |= set(task.owned_files)

        # Enforce the concurrency limit (input order fills the wave first).
        in_wave = current[:limit]
        overflow = current[limit:]

        waves.append([t.id for t in in_wave])
        in_wave_ids = {t.id for t in in_wave}
        for task_id in in_wave_ids:
            resolved.add(task_id)

        # Rebuild the work queue: in-wave tasks are DONE (removed);
        # overlap-deferred and overflow go back to the FRONT for the next
        # round; everything else (including not-ready tasks) keeps order.
        # {in_wave, overflow, deferred} partition `ready`, so excluding all
        # three id sets from the tail is a clean partition of `remaining`.
        moved_ids = in_wave_ids | {t.id for t in overflow} | {t.id for t in deferred}
        remaining = deferred + overflow + [
            t for t in remaining if t.id not in moved_ids
        ]

    return waves


def implementation_frontier(
    plan: DelegationPlan,
    statuses: dict[str, TaskStatus | str],
) -> list[str]:
    """Tasks whose IMPLEMENTATION may start NOW (continue-while-tests-run).

    A dependent stage's implementation starts as soon as every prerequisite
    is at least IMPLEMENTED (code complete) — a prerequisite still running
    its DEVELOPMENT_TEST does not block the dependent's implementation.
    Test completion remains a PROMOTION condition (see promotion_ready),
    never an implementation blocker. A FAILED prerequisite blocks its
    dependents (they wait for the bounded fixer).
    """
    code_done = {TaskStatus.IMPLEMENTED, TaskStatus.VERIFIED, TaskStatus.ACCEPTED}
    out: list[str] = []
    for task in plan.tasks:
        current = statuses.get(task.id, task.status)
        if current in code_done or current in {
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
        }:
            continue  # implementation already complete (or closed)
        deps_ok = all(statuses.get(dep) in code_done for dep in task.dependencies)
        if deps_ok:
            out.append(task.id)
    return out


def promotion_ready(
    plan: DelegationPlan,
    statuses: dict[str, TaskStatus | str],
    task_id: str,
) -> bool:
    """A task may pass its promotion gate only when it AND all of its
    prerequisites are VERIFIED/ACCEPTED (test completion is a promotion
    condition, not an implementation blocker)."""
    done = {TaskStatus.VERIFIED, TaskStatus.ACCEPTED}
    by_id = {t.id: t for t in plan.tasks}
    task = by_id[task_id]

    def ready(tid: str) -> bool:
        return statuses.get(tid) in done

    if not ready(task.id):
        return False
    return all(ready(dep) for dep in task.dependencies)


def spawn_fixer(
    plan: DelegationPlan,
    task_id: str,
    failure_evidence: str,
    *,
    fixer_number: int | None = None,
    max_fixers: int = 3,
) -> DelegatedTask:
    """Fresh bounded fixer for a FAILED stage (rule: the implementing
    worker does not automatically become its unrestricted repair worker).

    The fixer is a NEW task id (``<task_id>-fix-N``), reuses the failed
    task's stage/scope exactly, and carries the failure evidence as its
    objective prefix. The failed task's independent siblings are untouched.
    Fixer budget is bounded (default 3 per original task).
    """
    by_id = {t.id: t for t in plan.tasks}
    if task_id not in by_id:
        raise KeyError(f"unknown task {task_id!r}")
    original = by_id[task_id]
    if original.status != TaskStatus.FAILED:
        raise ValueError(
            f"task {task_id!r} is {original.status.value}, not FAILED — no "
            "fixer is spawned for a non-failed stage"
        )

    existing = [t.id for t in plan.tasks if t.id.startswith(task_id + "-fix-")]
    if fixer_number is None:
        fixer_number = len(existing) + 1
    if fixer_number > max_fixers:
        raise ValueError(
            f"fixer budget exhausted for {task_id!r} "
            f"({max_fixers} fixers max) — escalate, do not respawn"
        )

    objective = f"Repair {task_id!r} only. FAILURE: {failure_evidence.strip()}"
    fixer = DelegatedTask(
        id=f"{task_id}-fix-{fixer_number}",
        stage=original.stage,
        objective=objective[:400],
        owned_files=list(original.owned_files),
        dependencies=[],
        worker_role=original.worker_role + "-fixer",
        worker_model=original.worker_model,
        verification_commands=list(original.verification_commands),
        model_budget=TokenBudget.TINY,
        status=TaskStatus.PENDING,
        base_sha=original.base_sha,
        worktree=original.worktree,
        read_only_context=list(original.read_only_context),
        invariants=["regression test first where meaningful"]
        + list(original.invariants),
        forbidden_scope=list(original.forbidden_scope)
        + ["do not redesign adjacent architecture"],
    )
    return fixer


def reconcile_after_failure(
    plan: DelegationPlan,
    failed_task_id: str,
    statuses: dict[str, TaskStatus | str],
    fixer: DelegatedTask,
) -> dict:
    """After a stage failure: block ONLY dependent stages; independent
    accepted work stays intact. The fixer takes the failed task's slot."""
    by_id = {t.id: t for t in plan.tasks}
    failed = by_id[failed_task_id]

    def transitive_deps(task_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            cur = stack.pop()
            for other in plan.tasks:
                if cur in other.dependencies and other.id not in seen:
                    seen.add(other.id)
                    stack.append(other.id)
        return seen

    affected = transitive_deps(failed_task_id)
    unaffected = [
        t.id
        for t in plan.tasks
        if t.id not in affected
        and t.id != failed_task_id
        and statuses.get(t.id) in {TaskStatus.ACCEPTED, TaskStatus.VERIFIED}
    ]
    return {
        "failed_task": failed_task_id,
        "fixer": fixer.id,
        "blocked_dependents": sorted(affected),
        "intact_independent": sorted(unaffected),
    }


# ---------------------------------------------------------------------------
# Test + token budget policy
# ---------------------------------------------------------------------------


def classify_test(command: str) -> TestKind:
    """Deterministic classification of a verification command.

    * LIVE_MODEL_SMOKE: a command that invokes a model/planner by name.
    * PROMOTION_TEST: the full canonical suite (``pytest -q`` with no
      path filter, or ``pytest tests``).
    * DEVELOPMENT_TEST: anything else (focused file/module, compile,
      diff --check).
    """
    low = command.strip().lower()
    model_markers = (
        "agy -p", "agy --model", "gemini", "run_supervised", "chat/completions",
        "planner", "gatekeeper", "mimo", "openai", "chat.completions",
    )
    if any(m in low for m in model_markers):
        return TestKind.LIVE_MODEL_SMOKE
    tokens = low.split()
    if "pytest" in low or "unittest" in low:
        is_full = True
        for token in tokens:
            if token.endswith(".py") or (
                "/" in token and "test" in token and token != "tests"
            ):
                is_full = False
        if is_full:
            return TestKind.PROMOTION_TEST
    return TestKind.DEVELOPMENT_TEST


def validate_task_budgets(plan: DelegationPlan) -> list[str]:
    """Return policy violations (empty list = compliant).

    * a task with a LIVE_MODEL_SMOKE command must declare why (its
      objective mentions the smoke target) — advisory, not an error;
    * full-suite commands on a task are PROMOTION_TEST and belong to the
      promotion boundary, not per-task development verification;
    * NORMAL budget tasks must declare more than 3 owned files is impossible
      (schema caps at 3) — NORMAL is only legitimate for cross-cutting
      refactors, which must name the stage objective accordingly (advisory).
    """
    issues: list[str] = []
    for task in plan.tasks:
        for cmd in task.verification_commands:
            kind = classify_test(cmd)
            if kind == TestKind.PROMOTION_TEST:
                issues.append(
                    f"task {task.id!r}: full-suite command {cmd!r} is "
                    "PROMOTION_TEST — run it once at the promotion boundary, "
                    "not as per-task development verification"
                )
            if kind == TestKind.LIVE_MODEL_SMOKE:
                if task.model_budget != TokenBudget.TINY:
                    issues.append(
                        f"task {task.id!r}: live-model verification {cmd!r} "
                        "requires a TINY token budget"
                    )
    return issues


def budget_for_scope(n_owned_files: int) -> TokenBudget:
    """Default budget for a bounded implementation task: SMALL (tests TINY)."""
    if n_owned_files <= 1:
        return TokenBudget.SMALL
    return TokenBudget.SMALL


# ---------------------------------------------------------------------------
# Prompt contracts (coordinator generates; children never see giant dumps)
# ---------------------------------------------------------------------------


def _items(header: str, items: list[str], empty: str) -> list[str]:
    return [header, *(f"- {item}" for item in items)] if items else [header, empty]


def dispatch_prompt(task: DelegatedTask) -> str:
    """The bounded worker prompt (dispatch contract shape)."""
    lines = [
        "ROLE",
        "You are a bounded implementation worker.",
        "",
        "WORKTREE",
        task.worktree,
        "",
        "BASE",
        task.base_sha,
        "",
        "TASK",
        task.objective,
        "",
        "OWNED FILES",
        *(f"- {f}" for f in task.owned_files),
        "",
        *_items(
            "READ-ONLY CONTEXT", task.read_only_context, "- none"
        ),
        "",
        "INVARIANTS",
        *(
            [f"{i + 1}. {inv}" for i, inv in enumerate(task.invariants)]
            or ["1. none stated"]
        ),
        "",
        *_items("DO NOT", task.forbidden_scope, "- nothing extra stated"),
        "",
        *_items(
            "FOCUSED VERIFICATION", task.verification_commands, "- git diff --check"
        ),
        "",
        "DELIVER",
        "- files changed",
        "- focused test results",
        "- remaining issue",
        "",
        "DO NOT COMMIT/PUSH unless explicitly authorized.",
        "",
        f"(worker model: {task.worker_model}; budget: {task.model_budget.value}; "
        f"test policy: DEVELOPMENT_TEST while implementing)",
    ]
    return "\n".join(lines)


def repair_prompt(task: DelegatedTask) -> str:
    """The bounded repair worker prompt (repair contract shape)."""
    # task.objective carries the FAILURE text after the marker (spawn_fixer).
    _prefix, _, failure = task.objective.partition("FAILURE: ")
    lines = [
        "ROLE",
        "Bounded repair worker.",
        "",
        "WORKTREE",
        task.worktree,
        "",
        "BASE",
        task.base_sha,
        "",
        "FAILURE",
        failure.strip() or "(see objective)",
        "",
        "REPRODUCTION",
        task.verification_commands[0] if task.verification_commands else "(unspecified)",
        "",
        "EXPECTED",
        "the failing check passes; nothing else changes",
        "",
        "ALLOWED FILES",
        *(f"- {f}" for f in task.owned_files),
        "",
        "RULE",
        "Write regression first where meaningful.",
        "",
        "VERIFY",
        *(
            [f"- {c}" for c in task.verification_commands]
            or ["- (unspecified)"]
        ),
        "",
        "No feature re-planning.",
        "",
        "DO NOT COMMIT/PUSH unless explicitly authorized.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coordinator acceptance (worker prose never proves completion)
# ---------------------------------------------------------------------------


def acceptance_checks(task: DelegatedTask) -> list[str]:
    """The coordinator's disk-authoritative checklist for one task."""
    commands = [
        "git status --short",
        "git diff --stat",
        "git diff",
        "git diff --check",
        f"git rev-parse HEAD (expected base {task.base_sha} or its authorized descendants)",
        *task.verification_commands,
        "python3 -m compileall -q <changed modules> && echo OK",
    ]
    if task.worker_role in {"agy-ui-reviewer", "agy-adversarial-reviewer", "agy-fixer"}:
        commands.append("session/model/provider metadata (if model routing matters)")
    return commands


@dataclass
class PromotionGate:
    """The one boundary where the canonical full suite runs (once).

    ``full_suite_ran_sha`` records the exact committed SHA the suite ran at;
    rerunning is only legal after product code changes after that SHA.
    """

    name: str
    affected_tests: list[str] = field(default_factory=list)
    full_suite_command: str = "python3 -m pytest -q"
    full_suite_ran_sha: str | None = None

    def may_run_full_suite(self, current_sha: str) -> bool:
        if self.full_suite_ran_sha is None:
            return True
        return current_sha != self.full_suite_ran_sha


__all__ = [
    "TokenBudget",
    "TestKind",
    "TaskStatus",
    "PRODUCTION_ROLES",
    "DEFAULT_DEV_WORKER_MODEL",
    "DelegatedTask",
    "DelegationPlan",
    "enforce_delegation_scope",
    "dispatch_waves",
    "implementation_frontier",
    "promotion_ready",
    "spawn_fixer",
    "reconcile_after_failure",
    "classify_test",
    "validate_task_budgets",
    "budget_for_scope",
    "dispatch_prompt",
    "repair_prompt",
    "acceptance_checks",
    "PromotionGate",
]
