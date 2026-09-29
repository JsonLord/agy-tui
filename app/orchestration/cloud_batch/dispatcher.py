"""P14 — BatchDispatcher: deterministic dispatch of a validated BatchPlan.

The dispatcher is the ONLY executor of a batch and it is STRUCTURALLY
incapable of invoking the cloud: it imports no planner, takes no
planner, and worker completion can only (a) write local state via the
:class:`ProjectStore` and (b) evaluate the deterministic reentry gate
and mark REENTRY_READY through the protocol (which itself refuses any
cloud call except at a supervised reentry).

P13 is reused, not reimplemented:

* the BatchPlan is lowered to a P13 :class:`DelegationPlan` — its
  validators (unique ids, known deps, P2 stage-DAG via
  ``DevelopmentPlan``, shared-file rules, ``concurrency_limit`` ge=1
  le=4) re-apply unchanged; P14's ``parallelism`` hint is CLAMPED to
  the P13 cap, never widened (accepted P13 limits are not weakened);
* waves come from P13 :func:`dispatch_waves` (independent-only
  parallelism, shared-file serialization);
* failures use P13 :func:`spawn_fixer` (fresh bounded fixer, max 3)
  and :func:`reconcile_after_failure` semantics — the fixer takes the
  failed task's slot: when a fixer reaches VERIFIED/ACCEPTED the
  original task's status is promoted to the fixer's terminal status
  (deterministic; this is what unblocks dependents, whose deps name the
  original task id).

Bounded repair, no endless retry: when the fixer budget is exhausted
the original task stays FAILED and the reentry gate escalates
(BATCH_FAILED_WITH_ESCALATION). Tasks blocked by a failed dependency
are deterministically CANCELLED (with a raw-evidence note) so the
outcome is always complete.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from app.orchestration.checkpoint_planning.delegation_policy import (
    DelegationPlan,
    DelegatedTask,
    TaskStatus,
    TokenBudget,
    dispatch_waves,
    spawn_fixer,
)
from app.orchestration.cloud_batch.outcome import (
    build_batch_outcome,
    compress_worker_result,
)
from app.orchestration.cloud_batch.protocol import CloudProtocol
from app.orchestration.cloud_batch.roles import agent_for_role
from app.orchestration.cloud_batch.schemas import BatchPlan
from app.orchestration.cloud_batch.state import ProjectStore
from app.schemas import AcceptanceCommand, PlanStage

#: P13 hard cap on concurrent implementation children (le=4 on
#: DelegationPlan.concurrency_limit). P14's planner parallelism hint is
#: clamped to this — never above it.
P13_CONCURRENCY_CAP = 4

#: P13 terminal statuses (a task in one of these is never re-executed).
_TERMINAL = {
    TaskStatus.VERIFIED,
    TaskStatus.ACCEPTED,
    TaskStatus.FAILED,
    TaskStatus.REJECTED,
    TaskStatus.CANCELLED,
}


class DispatchError(RuntimeError):
    """Deterministic dispatch failure (fail closed; the batch is not
    silently retried — the reentry gate / supervisor escalates)."""


@dataclass
class WorkerRunResult:
    """What a worker runner returns for one delegated task.

    ``status`` is a P13 :class:`TaskStatus` value. ``raw`` holds the
    LOCAL-ONLY evidence (transcript excerpts, stdout, returncode) — it
    is persisted to ``workers/<id>.raw.json`` and never enters a
    cloud-facing object.
    """

    status: str
    conclusion: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class WorkerRunner(Protocol):
    """Injected worker execution seam (tests: fakes; production:
    :class:`OpenCodeWorkerRunner`)."""

    def run(self, task: DelegatedTask, *, workdir: str) -> WorkerRunResult: ...


def build_delegation_plan(
    plan: BatchPlan, *, base_sha: str, workdir: str
) -> DelegationPlan:
    """Lower a P14 BatchPlan onto the P13 DelegationPlan (single batch
    stage). All P13 validation runs here — P14's schema already
    pre-validates ids/roles/deps; P13 re-checks the DAG + scope.
    Unknown roles raise (fail closed) via :func:`agent_for_role`."""
    for d in plan.delegations:
        agent_for_role(d.role)
    stages = [
        PlanStage(
            stage_id="batch",
            objective=plan.objective[:400],
            dependencies=[],
            allowed_files=sorted(
                {f for d in plan.delegations for f in d.owned_files}
            ),
            implementation_context=(
                "P14 cloud batch: bounded delegations dispatched by the "
                "deterministic dispatcher under the P13 delegation policy."
            ),
            acceptance_commands=[AcceptanceCommand(command="git diff --check")],
            acceptance_conditions=["all required delegations terminal"],
        )
    ]
    tasks = [
        DelegatedTask(
            id=d.id,
            stage="batch",
            objective=d.objective,
            owned_files=list(d.owned_files),
            dependencies=list(d.deps),
            worker_role=d.role,
            verification_commands=list(d.verification) or ["git diff --check"],
            base_sha=base_sha,
            worktree=workdir,
            read_only_context=list(d.reads),
            invariants=list(d.invariants),
        )
        for d in plan.delegations
    ]
    return DelegationPlan(
        plan_id=plan.batch_id,
        objective=plan.objective,
        stages=stages,
        tasks=tasks,
        concurrency_limit=max(1, min(plan.parallelism, P13_CONCURRENCY_CAP)),
        promotion_gate="batch-promotion",
    )


class BatchDispatcher:
    """Deterministic executor of one validated batch.

    Never invokes the cloud. Responsibilities: persist, resolve
    dependency waves (P13), dispatch eligible workers, collect results,
    apply bounded retry/failure rules (P13 fresh fixers), create the
    compact BatchOutcome, evaluate the reentry condition, mark
    REENTRY_READY.
    """

    def __init__(
        self,
        store: ProjectStore,
        project_id: str,
        *,
        runner: WorkerRunner,
        protocol: CloudProtocol | None = None,
        base_sha: str = "0" * 40,
        workdir: str = ".",
        max_fixers: int = 3,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.runner = runner
        self.protocol = protocol
        self.base_sha = base_sha
        self.workdir = workdir
        self.max_fixers = max_fixers

    # -- main entry -----------------------------------------------------------

    def run_batch(self, batch_id: str) -> str:
        """Execute the batch to its deterministic reentry condition.
        Returns the reentry reason. Idempotent across restarts: terminal
        worker results already persisted are loaded, never re-executed —
        including runtime-spawned fixers (see _rehydrate_fixers)."""
        plan = self.store.load_batch_plan(self.project_id, batch_id)
        delegation = build_delegation_plan(
            plan, base_sha=self.base_sha, workdir=self.workdir
        )

        statuses: dict[str, TaskStatus] = {}
        outcomes: dict[str, Any] = self.store.load_worker_outcomes(
            self.project_id, batch_id
        )
        for delegation_id, wo in outcomes.items():
            statuses[delegation_id] = TaskStatus(wo.status)

        # Crash-resume rehydration (Worker-A defect 1): fixers are spawned
        # at RUNTIME and only ever persisted as worker outcomes — plan.json
        # never contains them. Without this, P13 spawn_fixer (which counts
        # fixers by scanning plan.tasks) would re-number to -fix-1 and the
        # fixer budget would reset, and _promote_fixer_results would miss
        # a pre-crash VERIFIED fixer. Rebuild the fixer task shells so the
        # canonical P13 budget/promotion logic sees them again.
        self._rehydrate_fixers(delegation, plan, outcomes, statuses)
        # Re-apply fixer-takes-the-slot promotion to the hydrated statuses
        # BEFORE wave computation: the in-loop promotion is a no-op when
        # both the fixer and its original are already terminal (e.g. a
        # pre-crash VERIFIED fixer whose original's promoted outcome was
        # never persisted), so the resume would otherwise treat the
        # original as FAILED and block/respawn.
        self._promote_fixer_results(delegation, statuses)

        while True:
            nonterminal = [
                t for t in delegation.tasks if statuses.get(t.id, TaskStatus.PENDING) not in _TERMINAL
            ]
            if not nonterminal:
                break

            try:
                waves = dispatch_waves(
                    delegation,
                    statuses,
                    concurrency_limit=delegation.concurrency_limit,
                )
            except ValueError:
                # The plan was validated acyclic, so an unmet dependency
                # here can only mean a dependency that is FAILED with the
                # fixer budget exhausted (FAILED is not a done status for
                # P13 wave computation). Block the dependents and stop.
                self._cancel_blocked(delegation, statuses, outcomes, batch_id)
                break

            # P13 dispatch_waves is a pure structural decomposition: it
            # marks every task in a wave "resolved", so a dependent of a
            # FAILED task would otherwise become runnable. The blocking
            # semantics (reconcile_after_failure's job in P13) are applied
            # here: dependents of a FAILED/CANCELLED task never run.
            # Recomputed BEFORE EACH WAVE: a failure in an earlier wave of
            # the same round must block later waves in that round.
            executed = 0
            for wave in waves:
                blocked = self._blocked_ids(delegation, statuses)
                eligible = [
                    tid
                    for tid in wave
                    if statuses.get(tid, TaskStatus.PENDING) not in _TERMINAL
                    and tid not in blocked
                ]
                if not eligible:
                    continue
                self._run_wave(eligible, delegation, statuses, outcomes, batch_id)
                executed += len(eligible)
            self._promote_fixer_results(delegation, statuses)

            spawned = self._try_spawn_fixers(delegation, statuses)
            if executed == 0 and not spawned:
                # No runnable work and nothing new to spawn: the rest is
                # blocked on a failed dependency (or the plan was
                # corrupted) — fail closed into escalation.
                self._cancel_blocked(delegation, statuses, outcomes, batch_id)
                break

        # Persist any outcomes created by _cancel_blocked (done inside).
        reason = self._evaluate_reentry(plan, statuses)
        outcome = build_batch_outcome(
            batch_id,
            plan.objective,
            [outcomes[tid] for tid in sorted(outcomes)],
            reentry_reason=reason,
        )
        self.store.save_batch_outcome(self.project_id, batch_id, outcome)
        if self.protocol is not None:
            self.protocol.mark_reentry_ready(batch_id, reason)
        return reason

    # -- wave execution ----------------------------------------------------------

    def _run_wave(
        self,
        wave: list[str],
        delegation: DelegationPlan,
        statuses: dict[str, TaskStatus],
        outcomes: dict[str, Any],
        batch_id: str,
    ) -> None:
        """Run one P13 wave's tasks concurrently (independent by
        construction; shared files are serialized by dispatch_waves)."""
        by_id = {t.id: t for t in delegation.tasks}

        def _one(tid: str) -> tuple[str, WorkerRunResult]:
            return tid, self.runner.run(by_id[tid], workdir=self.workdir)

        with ThreadPoolExecutor(max_workers=max(1, len(wave))) as pool:
            for tid, result in pool.map(_one, wave):
                statuses[tid] = TaskStatus(result.status)
                raw_ref = self.store.save_worker_raw(
                    self.project_id, batch_id, tid, result.raw
                )
                outcome = compress_worker_result(
                    tid,
                    by_id[tid].worker_role,
                    result.status,
                    {
                        "conclusion": result.conclusion,
                        "evidence_refs": result.evidence_refs,
                        "risks": result.risks,
                        "unknowns": result.unknowns,
                    },
                    raw_evidence_ref=raw_ref,
                )
                outcomes[tid] = outcome
                self.store.save_worker_outcome(self.project_id, batch_id, outcome)

    # -- bounded repair (P13 fresh fixers, fixer takes the slot) ------------------

    @staticmethod
    def _original_of(fixer_id: str) -> str | None:
        if "-fix-" not in fixer_id:
            return None
        original, _, tail = fixer_id.rpartition("-fix-")
        if original and tail.isdigit():
            return original
        return None

    def _rehydrate_fixers(
        self,
        delegation: DelegationPlan,
        plan: BatchPlan,
        outcomes: dict[str, Any],
        statuses: dict[str, TaskStatus],
    ) -> None:
        """Crash-resume: re-add runtime-spawned fixer task shells.

        Fixers are spawned during dispatch and appended to
        ``delegation.tasks`` in memory; the canonical plan.json only ever
        holds the cloud-authored delegations. On resume, rebuild a
        structurally-equivalent DelegatedTask for every persisted fixer
        worker outcome (``<id>-fix-<n>``) that is not already in the
        rebuilt plan. This restores:

        * P13 ``spawn_fixer`` budget counting (it counts fixers by
          scanning ``plan.tasks`` for ``<task_id>-fix-*``), so the max-3
          bound holds ACROSS restarts, not just within a run;
        * ``_promote_fixer_results`` (iterates ``delegation.tasks``), so
          a pre-crash VERIFIED fixer still promotes its original;
        * the never-re-execute invariant: the rehydrated task's status
          is its PERSISTED terminal status, so wave eligibility skips it.

        Scope mirrors what ``spawn_fixer`` creates (owned files,
        verification, worktree, base_sha of the original task) with the
        P14 canonical fixer role; the objective is a deterministic
        placeholder — a rehydrated task is never re-run (it is terminal)
        and only the shell matters for counting and promotion.
        """
        by_id = {t.id: t for t in delegation.tasks}
        specs_by_id = {d.id: d for d in plan.delegations}
        for task_id in sorted(outcomes):
            original = self._original_of(task_id)
            if original is None or original not in by_id:
                continue
            if task_id in by_id:
                continue  # already present (same-run rehydration is idempotent)
            orig = by_id[original]
            spec = specs_by_id.get(original)
            fixer = DelegatedTask(
                id=task_id,
                stage=orig.stage,
                objective=(
                    f"Repair {original!r} only (rehydrated from persisted "
                    "worker outcome after restart — terminal, not re-run)."
                )[:400],
                owned_files=list(orig.owned_files),
                dependencies=[],
                worker_role="fixer",
                model_budget=TokenBudget.TINY,
                status=TaskStatus.PENDING,
                base_sha=orig.base_sha,
                worktree=orig.worktree,
                read_only_context=list(orig.read_only_context),
                invariants=list(orig.invariants),
                forbidden_scope=list(orig.forbidden_scope),
            )
            if spec is not None and spec.verification:
                fixer.verification_commands = list(spec.verification)
            # Restore the persisted terminal status in BOTH the canonical
            # side-dict (wave eligibility: terminal => never re-run) and
            # the task model (P13 spawn_fixer re-checks the original's
            # status field; keeps the two consistent).
            persisted = TaskStatus(outcomes[task_id].status)
            statuses[task_id] = persisted
            fixer.status = persisted
            delegation.tasks.append(fixer)

    def _promote_fixer_results(
        self, delegation: DelegationPlan, statuses: dict[str, TaskStatus]
    ) -> None:
        """reconcile_after_failure semantics: the fixer takes the failed
        task's slot. A fixer that reached VERIFIED/ACCEPTED promotes the
        original task (unblocking dependents, whose deps name the
        original id). A FAILED fixer leaves the original FAILED (next
        fixer round or escalation)."""
        by_id = {t.id: t for t in delegation.tasks}
        for task in delegation.tasks:
            original = self._original_of(task.id)
            if original is None or original not in by_id:
                continue
            if task.id not in statuses:
                continue
            fixer_status = statuses[task.id]
            if fixer_status in {TaskStatus.VERIFIED, TaskStatus.ACCEPTED}:
                if statuses.get(original) == TaskStatus.FAILED:
                    statuses[original] = fixer_status

    def _try_spawn_fixers(
        self, delegation: DelegationPlan, statuses: dict[str, TaskStatus]
    ) -> bool:
        """For each FAILED cloud-authored task whose fixers are all
        terminal: spawn the next fresh bounded fixer (P13). Budget
        exhausted -> left FAILED (escalation at reentry; no endless
        retry). Returns True if anything was spawned."""
        by_id = {t.id: t for t in delegation.tasks}
        spawned_any = False
        for task in list(delegation.tasks):
            if statuses.get(task.id) != TaskStatus.FAILED:
                continue
            if self._original_of(task.id) is not None:
                continue  # task itself is a fixer — original handles it
            fixers = [
                t.id
                for t in delegation.tasks
                if self._original_of(t.id) == task.id
            ]
            if any(
                statuses.get(fid, TaskStatus.PENDING)
                in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.IMPLEMENTED}
                for fid in fixers
            ):
                continue  # a fixer is still in flight
            try:
                # P13 spawn_fixer re-checks the task's OWN status field
                # (==FAILED) — keep the model in sync with the canonical
                # side-dict before spawning.
                task.status = TaskStatus.FAILED
                fixer = spawn_fixer(
                    delegation,
                    task.id,
                    "verification failed; see raw evidence",
                    max_fixers=self.max_fixers,
                )
            except ValueError:
                continue  # budget exhausted — escalate at reentry
            # P13 names the fixer role "<role>-fixer"; P14 lowers every
            # fixer onto the canonical bounded fixer agent (agy-fixer).
            fixer.worker_role = "fixer"
            delegation.tasks.append(fixer)
            statuses[fixer.id] = TaskStatus.PENDING
            spawned_any = True
        return spawned_any

    def _blocked_ids(
        self, delegation: DelegationPlan, statuses: dict[str, TaskStatus]
    ) -> set[str]:
        """reconcile_after_failure blocking semantics: ids that can never
        run because a dependency is FAILED (fixer budget exhausted) or
        already CANCELLED this pass. REJECTED is P13 "done" for wave
        computation — dependents of a gate-rejected task keep P13
        semantics (they may still run)."""
        by_id = {t.id: t for t in delegation.tasks}

        def blocked_transitively(task_id: str) -> bool:
            for dep in by_id[task_id].dependencies:
                dep_status = statuses.get(dep, TaskStatus.PENDING)
                if dep_status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                    return True
                if dep_status in {TaskStatus.VERIFIED, TaskStatus.ACCEPTED}:
                    continue
                if blocked_transitively(dep):
                    return True
            return False

        return {
            t.id
            for t in delegation.tasks
            if statuses.get(t.id, TaskStatus.PENDING) not in _TERMINAL
            and blocked_transitively(t.id)
        }

    def _cancel_blocked(
        self,
        delegation: DelegationPlan,
        statuses: dict[str, TaskStatus],
        outcomes: dict[str, Any],
        batch_id: str,
    ) -> None:
        """Deterministically CANCEL tasks that can never run because a
        dependency is FAILED with the fixer budget exhausted. Keeps the
        BatchOutcome complete and honest."""
        for task_id in sorted(self._blocked_ids(delegation, statuses)):
            task = next(t for t in delegation.tasks if t.id == task_id)
            statuses[task_id] = TaskStatus.CANCELLED
            outcome = compress_worker_result(
                task.id,
                task.worker_role,
                TaskStatus.CANCELLED.value,
                {
                    "conclusion": (
                        "cancelled: a dependency failed and its fixer "
                        "budget is exhausted"
                    ),
                },
            )
            outcomes[task.id] = outcome
            self.store.save_worker_outcome(self.project_id, batch_id, outcome)

    # -- reentry condition -----------------------------------------------------------

    def _evaluate_reentry(self, plan: BatchPlan, statuses: dict[str, TaskStatus]) -> str:
        """Deterministic reentry reason. The dispatcher emits
        ALL_REQUIRED_DELEGATIONS_COMPLETE or BATCH_FAILED_WITH_ESCALATION;
        REPLAN_REQUIRED and HUMAN_FEEDBACK_CONTINUATION_READY are set by
        the higher supervisor through protocol.mark_reentry_ready (e.g.
        after a P13-admitted human-feedback continuation is ready)."""
        required = (
            set(plan.reentry.required_ids)
            if plan.reentry.required_ids
            else {d.id for d in plan.delegations}
        )
        failed_required = [
            tid for tid in required if statuses.get(tid) == TaskStatus.FAILED
        ]
        if failed_required:
            return "BATCH_FAILED_WITH_ESCALATION"
        all_required = all(
            statuses.get(tid) in {TaskStatus.VERIFIED, TaskStatus.ACCEPTED}
            for tid in required
        )
        if all_required:
            return "ALL_REQUIRED_DELEGATIONS_COMPLETE"
        # Required set not fully verified but nothing in it failed:
        # dependents are blocked by a non-terminal failure path —
        # escalate (fail closed, never spin).
        return "BATCH_FAILED_WITH_ESCALATION"


# ---------------------------------------------------------------------------
# Production worker runner: P13 delegation -> OpenCode named agents
# ---------------------------------------------------------------------------


class OpenCodeWorkerRunner:
    """Runs a delegated task through the existing supervised OpenCode
    stack (P13 ``to_worker_task`` + ``run_supervised`` with the EXPLICIT
    agent named from the role — no implicit default, P9.2 rule).
    Deterministic validation (the task's verification command, checked
    by the supervisor stack's exit status) is authoritative for the
    reported status; raw stdout/stderr stay local-only.
    """

    def __init__(
        self,
        *,
        timeout: int = 1800,
        run_supervised: Callable[..., Any] | None = None,
    ) -> None:
        self._timeout = timeout
        self._run_supervised = run_supervised  # injectable for tests

    def run(self, task: DelegatedTask, *, workdir: str) -> WorkerRunResult:
        if self._run_supervised is None:
            from app.integrations.external_stack import run_supervised

            self._run_supervised = run_supervised
        worker_task = task.to_worker_task()
        agent = agent_for_role(task.worker_role)
        result = self._run_supervised(
            repo=workdir,
            task=worker_task.worker_instruction,
            agent=agent,
            timeout=self._timeout,
        )
        ok = bool(getattr(result, "ok", False))
        status = TaskStatus.VERIFIED.value if ok else TaskStatus.FAILED.value
        conclusion = (
            f"{worker_task.id}: {'verification passed' if ok else 'verification failed'} "
            f"(returncode={getattr(result, 'returncode', None)})"
        )
        # Raw evidence stays local: bounded transcript tails only.
        raw = {
            "returncode": getattr(result, "returncode", None),
            "stdout_tail": (getattr(result, "stdout", "") or "")[-4000:],
            "stderr_tail": (getattr(result, "stderr", "") or "")[-4000:],
        }
        return WorkerRunResult(status=status, conclusion=conclusion, raw=raw)


__all__ = [
    "P13_CONCURRENCY_CAP",
    "DispatchError",
    "WorkerRunResult",
    "WorkerRunner",
    "build_delegation_plan",
    "BatchDispatcher",
    "OpenCodeWorkerRunner",
]
