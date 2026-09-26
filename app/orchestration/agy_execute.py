"""P3 stage execution loop: real execution + the commit gate.

``execute_stage`` runs ONE ``PlanStage`` of a ``DevelopmentPlan``
through bounded worker tasks, deterministic gates, ONE meaningful
stage commit and the supervisor gatekeeper decision — with ALL
external effects injected as callables so tests are fully
deterministic (the live versions come from ``agy_runtime``).

Deterministic flow (order matters):

1. ``decompose_stage(stage, objective=plan.objective)`` then run ONLY
   dependency-ready tasks through P2 ``run_worker_tasks``. The
   ``session_id_for_task`` hook records each task's session id and
   forwards it to ``on_session``.
2. If ``run_worker_tasks`` status != SUCCESS -> STAGE_FAILED: no
   commit, no supervisor. The bounded replan scope (failed tasks +
   blocked descendants, never a whole-plan restart) is recorded.
3. Aggregate changed files across all tasks (dedup,
   order-preserving). Every changed file must be in
   ``stage.allowed_files`` (default scope check exactly this;
   ``scope_checker`` may replace it) -> ``scope_violations``.
4. ZERO-DIFF RULE: empty aggregate with implementation required ->
   STAGE_FAILED with reason ``zero-diff with implementation
   required`` and no commit — worker prose cannot create success.
5. Stage acceptance via P2 ``run_stage_acceptance`` (each
   ``AcceptanceCommand`` verbatim through ``run_command``), then the
   CUMULATIVE GATE over the caller-supplied ``list[AcceptanceCommand]``
   (P3 callers pass a conservative full-suite list; nothing
   repository-specific is hardcoded here).
6. Scope violations OR acceptance failure OR cumulative failure ->
   NO commit; supervisor review still runs on the structured evidence
   with the failures embedded in ``deterministic_failures``.
7. All green -> ``make_commit(evidence)`` creates the ONE meaningful
   stage commit (message format ``AGY stage <stage_id>: <objective>``
   is produced by the commit helper from ``stage_id`` +
   ``stage_objective``).
8. Supervisor review (``supervisor_review(evidence)`` — async or sync
   returning dict) on the full bundle.
9. Deterministic OVERRIDES — the model never overrides failed
   deterministic checks: any failure + model ACCEPT -> forced REPAIR
   (``deterministic_override=True`` with reason); with all checks
   green ACCEPT/REPAIR/REJECT are all allowed (REJECT must carry a
   rationale). Invalid/non-enum review output fails closed as
   ``REJECT`` with rationale ``gatekeeper response invalid``.
10. ONE bounded in-stage repair cycle max: repair stays in the same
    plan/stage lineage (``<stage_id>-repair-<n>`` appended to
    ``repair_lineage``), builds ONE deterministic ``WorkerTask`` from
    the concrete failing checks, executes through the SAME
    execute/validate callables, re-runs scope + acceptance +
    cumulative; green -> repair commit
    (``AGY stage <stage_id> repair <n>: ...``) + one re-review ->
    STAGE_ACCEPTED/STAGE_REJECTED; still failing -> stop at
    STAGE_REPAIR with the remaining evidence (never loops again in
    P3). The plan is decomposed exactly ONCE.
11. Failover: provider-backed calls live in the runtime/gatekeeper
    layers (``complete_with_failover``); every event reaches
    ``on_failover_event`` and is collected into
    ``StageRunResult.failover_events``. The execute/validate
    callables themselves are provider-agnostic.

``SessionRecorder`` / ``FailoverRecorder`` are the shared sinks the
live runtime binds to (they satisfy the plain
``Callable[[str], None]`` / ``Callable[[dict], None]`` hook protocols
and additionally expose the recorded lists for load-back merging —
see ``agy_runstate`` recovery).
"""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from app.config import AgySettings
from app.orchestration.decompose import run_stage_acceptance, run_worker_tasks
from app.orchestration.plan_decompose import decompose_stage
from app.schemas import (
    AcceptanceCommand,
    DevelopmentPlan,
    PlanStage,
    TaskVerification,
    WorkerTask,
)

STAGE_ACCEPTED = "STAGE_ACCEPTED"
STAGE_REPAIR = "STAGE_REPAIR"
STAGE_REJECTED = "STAGE_REJECTED"
STAGE_FAILED = "STAGE_FAILED"

DECISIONS = ("ACCEPT", "REPAIR", "REJECT")

ZERO_DIFF_REASON = "zero-diff with implementation required"
INVALID_GATE_REASON = "gatekeeper response invalid"

MAX_REPAIR_TASK_FILES = 3


# ---------------------------------------------------------------------------
# Shared hook sinks
# ---------------------------------------------------------------------------


class SessionRecorder:
    """
    ``on_session`` sink: records OpenCode session ids in order and,
    optionally, per worker task (``bind``). The live runtime calls
    ``bind(task_id, session_id)`` when it captures a session; tests
    may call it with just a session id.
    """

    def __init__(self) -> None:
        self.sessions: list[str] = []
        self._by_task: dict[str, str] = {}

    def __call__(self, session_id: str) -> None:
        if session_id and session_id not in self.sessions:
            self.sessions.append(session_id)

    def bind(self, task_id: str, session_id: str | None) -> None:
        if not session_id:
            return
        self._by_task[task_id] = session_id
        self(session_id)

    def last_for(self, task_id: str) -> str | None:
        return self._by_task.get(task_id)


class FailoverRecorder:
    """``on_failover_event`` sink: appends every event it receives."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------


@dataclass
class StageRunResult:
    """Everything one stage execution produced (round-trips to JSON)."""

    plan_id: str
    stage_id: str
    status: str  # STAGE_ACCEPTED | STAGE_REPAIR | STAGE_REJECTED | STAGE_FAILED
    task_results: list[dict]
    changed_files: list[str]
    scope_violations: list[str]
    task_verification: dict
    stage_acceptance: dict
    cumulative_gate: dict
    commit: dict | None
    supervisor_decision: dict | None
    repair_lineage: list[str]
    failover_events: list[dict]
    session_ids: list[str]
    # Additive evidence fields (not in the minimal contract list, kept
    # for recovery/reporting): failure_reason carries the STAGE_FAILED
    # cause and replan carries the bounded run_worker_tasks replan
    # scope (failed + blocked descendants only).
    failure_reason: str = ""
    replan: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "stage_id": self.stage_id,
            "status": self.status,
            "task_results": [dict(r) for r in self.task_results],
            "changed_files": list(self.changed_files),
            "scope_violations": list(self.scope_violations),
            "task_verification": copy.deepcopy(self.task_verification),
            "stage_acceptance": copy.deepcopy(self.stage_acceptance),
            "cumulative_gate": copy.deepcopy(self.cumulative_gate),
            "commit": copy.deepcopy(self.commit),
            "supervisor_decision": copy.deepcopy(self.supervisor_decision),
            "repair_lineage": list(self.repair_lineage),
            "failover_events": copy.deepcopy(self.failover_events),
            "session_ids": list(self.session_ids),
            "failure_reason": self.failure_reason,
            "replan": copy.deepcopy(self.replan),
        }


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _as_command(command: Any) -> AcceptanceCommand:
    """Accept an AcceptanceCommand, a plain string or a dict."""
    if isinstance(command, AcceptanceCommand):
        return command
    if isinstance(command, str):
        return AcceptanceCommand(command=command, expected_exit_code=0)
    if isinstance(command, dict):
        allowed = {
            "command",
            "expected_exit_code",
            "description",
        }
        return AcceptanceCommand(
            **{k: v for k, v in command.items() if k in allowed}
        )
    raise TypeError(
        "cumulative suite entries must be AcceptanceCommand, str or "
        f"dict, got {type(command).__name__}"
    )


def _aggregate_changed(task_results: list[dict]) -> list[str]:
    """Dedup, order-preserving union of per-task changed files."""
    seen: set[str] = set()
    out: list[str] = []

    for result in task_results:
        for path in result.get("changed_files", []) or []:
            if path not in seen:
                seen.add(path)
                out.append(path)

    return out


def _verification_map(task_results: list[dict]) -> dict[str, dict]:
    """Per-task verification evidence, keyed by worker task id."""
    return {
        r["worker_task_id"]: {
            "attempt": r.get("attempt"),
            "status": r.get("status"),
            "validation_ok": r.get("validation_ok"),
            "validation_output": r.get("validation_output", ""),
            "changed_files": list(r.get("changed_files", []) or []),
            "session_id": r.get("session_id"),
            "failure_reason": r.get("failure_reason", ""),
        }
        for r in task_results
    }


def _task_result_projection(task_results: list[dict]) -> list[dict]:
    """commit.json ``task_results``: ids + status only."""
    return [
        {
            "worker_task_id": r.get("worker_task_id"),
            "attempt": r.get("attempt"),
            "status": r.get("status"),
            "validation_ok": r.get("validation_ok"),
        }
        for r in task_results
    ]


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _skipped_gate(reason: str) -> dict[str, Any]:
    """Gate placeholder when execution never reached that gate."""
    return {"ok": False, "checks": [], "skipped": True, "reason": reason}


async def _invoke_review(
    review: Callable[..., Any],
    evidence: dict[str, Any],
    on_failover_event: Callable[[dict[str, Any]], None],
) -> Any:
    """
    Call the injected supervisor review, supporting both plain
    ``f(evidence)`` callables and keyword-aware ones (``review_stage``
    declares ``evidence`` keyword-only). When the callable advertises
    an ``on_failover_event`` parameter it receives this stage's
    collector, so gatekeeper failover events land in the result.
    """
    try:
        parameters = inspect.signature(review).parameters
    except (TypeError, ValueError):  # builtins / uninspectable
        parameters = {}

    wants_evidence_kw = "evidence" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in parameters.values()
    )
    wants_event = "on_failover_event" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in parameters.values()
    )

    kwargs: dict[str, Any] = {}
    if wants_event:
        kwargs["on_failover_event"] = on_failover_event

    if wants_evidence_kw:
        kwargs["evidence"] = evidence
        raw = review(**kwargs)
    else:
        raw = review(evidence, **kwargs)

    if inspect.isawaitable(raw):
        raw = await raw

    return raw


def _normalize_review(
    raw: Any,
    deterministic_failures: list[str],
) -> dict[str, Any]:
    """
    Normalize + constrain a supervisor decision (§5/§9).

    Fail-closed: anything that is not a dict with an enum decision
    becomes REJECT with rationale ``gatekeeper response invalid``.
    A model ACCEPT while deterministic checks failed is forced to
    REPAIR with ``deterministic_override=True``.
    """
    if not isinstance(raw, dict):
        return {
            "decision": "REJECT",
            "rationale": INVALID_GATE_REASON,
            "repair_targets": [],
            "model": None,
            "deterministic_override": False,
            "invalid": True,
        }

    decision = raw.get("decision")
    model = raw.get("model")
    rationale = raw.get("rationale")
    rationale = rationale if isinstance(rationale, str) else ""

    if decision not in DECISIONS:
        return {
            "decision": "REJECT",
            "rationale": INVALID_GATE_REASON,
            "repair_targets": [],
            "model": model if isinstance(model, str) else None,
            "deterministic_override": False,
            "invalid": True,
        }

    repair_targets_raw = raw.get("repair_targets")
    repair_targets = (
        [str(t) for t in repair_targets_raw if str(t).strip()]
        if isinstance(repair_targets_raw, list)
        else []
    )

    normalized: dict[str, Any] = {
        "decision": decision,
        "rationale": rationale,
        "repair_targets": repair_targets,
        "model": model if isinstance(model, str) else None,
        "deterministic_override": False,
    }

    if raw.get("failover_event") is not None:
        normalized["failover_event"] = raw["failover_event"]

    if deterministic_failures and decision == "ACCEPT":
        normalized["decision"] = "REPAIR"
        normalized["deterministic_override"] = True
        normalized["original_decision"] = "ACCEPT"
        normalized["override_reason"] = (
            "deterministic checks failed; ACCEPT is not permitted: "
            + "; ".join(deterministic_failures)
        )

    if (
        normalized["decision"] == "REJECT"
        and not normalized["rationale"].strip()
    ):
        normalized["rationale"] = (
            "supervisor returned REJECT without rationale"
        )

    return normalized


# ---------------------------------------------------------------------------
# The stage execution loop
# ---------------------------------------------------------------------------


async def execute_stage(
    plan: DevelopmentPlan,
    stage: PlanStage,
    *,
    settings: AgySettings,
    execute_task,
    validate_task,
    collect_changed_files,
    run_command,
    cumulative_suite,
    scope_checker: Callable[[list[str]], list[str]] | None = None,
    make_commit: Callable[[dict], dict] | None = None,
    supervisor_review: Callable[..., Any] | None = None,
    on_session: Callable[[str], None] | None = None,
    on_failover_event: Callable[[dict], None] | None = None,
    max_task_attempts: int = 2,
) -> StageRunResult:
    """
    Execute and gate ONE stage. See the module docstring for the
    pinned 11-step flow. Returns the full :class:`StageRunResult`;
    never raises for repository/test/model outcomes (only for wiring
    errors in the injected callables).
    """
    _ = settings  # carried for live wiring symmetry; not consulted here

    plan_id = plan.plan_id
    stage_id = stage.stage_id
    allowed = list(stage.allowed_files)
    allow_set = set(allowed)

    events: list[dict[str, Any]] = []
    sessions: list[str] = []
    repair_lineage: list[str] = []

    # -- hooks ---------------------------------------------------------------

    def _event_seen(event: dict[str, Any]) -> bool:
        """Identity check against this stage's collector AND, when a
        shared FailoverRecorder is wired, against the recorder the
        runtime already emitted into (never double-forward)."""
        if any(existing is event for existing in events):
            return True
        sink = getattr(on_failover_event, "events", None)
        return isinstance(sink, list) and any(
            existing is event for existing in sink
        )

    def record_event(event: dict[str, Any]) -> None:
        if event is None:
            return
        if _event_seen(event):
            if not any(existing is event for existing in events):
                events.append(event)
            return
        events.append(event)
        if on_failover_event is not None:
            on_failover_event(event)

    def record_session(session_id: str | None) -> None:
        if not session_id or session_id in sessions:
            return
        sessions.append(session_id)
        if on_session is not None:
            on_session(session_id)

    def session_id_for_task(task: WorkerTask) -> str | None:
        getter = getattr(on_session, "last_for", None)
        session_id = getter(task.id) if callable(getter) else None
        if session_id:
            record_session(session_id)
        return session_id

    # -- deterministic gates ---------------------------------------------------

    def check_scope(changed: list[str]) -> list[str]:
        if scope_checker is not None:
            return [str(v) for v in scope_checker(
                list(changed)
            )]
        # Default scope check: EVERY changed file must be in the
        # stage's allowed_files, exactly.
        return [path for path in changed if path not in allow_set]

    cumulative_commands = [
        _as_command(command) for command in (cumulative_suite or [])
    ]

    async def run_cumulative() -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        for index, command in enumerate(cumulative_commands, start=1):
            exit_code, output = await run_command(command)
            checks.append(
                {
                    "name": (
                        command.description
                        or f"cumulative-{index:02d}"
                    ),
                    "command": command.command,
                    "exit_code": exit_code,
                    "ok": exit_code == command.expected_exit_code,
                    "output": output,
                }
            )

        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    def failing_command() -> tuple[str, int]:
        """First failing check's (command, expected_exit_code)."""
        for check in stage_acceptance_entry["checks"]:
            if not check["ok"]:
                return check["command"], check["expected_exit_code"]

        for index, check in enumerate(cumulative_entry["checks"]):
            if not check["ok"]:
                command = cumulative_commands[index]
                return command.command, command.expected_exit_code

        first = stage.acceptance_commands[0]
        return first.command, first.expected_exit_code

    # mutable slots read by failing_command / _assemble (single pass,
    # assigned before any use — see the flow below).
    stage_acceptance_entry: dict[str, Any] = _skipped_gate("not run")
    cumulative_entry: dict[str, Any] = _skipped_gate("not run")

    async def run_acceptance() -> dict[str, Any]:
        return await run_stage_acceptance(stage, run_command=run_command)

    # -- evidence ---------------------------------------------------------------

    def deterministic_failures_for(
        violations: list[str],
        acceptance: dict,
        cumulative: dict,
    ) -> list[str]:
        failures: list[str] = [
            f"scope violation: {path} is not in stage.allowed_files"
            for path in violations
        ]

        for check in acceptance.get("checks", []):
            if not check["ok"]:
                failures.append(
                    "stage acceptance failed: "
                    f"{check['command']} exited {check['exit_code']}, "
                    f"expected {check['expected_exit_code']}"
                )

        for check in cumulative.get("checks", []):
            if not check["ok"]:
                failures.append(
                    f"cumulative gate failed: {check['command']} "
                    f"exited {check['exit_code']}"
                )

        return failures

    def build_evidence(
        task_results_local: list[dict],
        verification: dict,
        changed: list[str],
        violations: list[str],
        acceptance: dict,
        cumulative: dict,
        commit: dict | None,
        failures: list[str],
    ) -> dict[str, Any]:
        return {
            "plan_id": plan_id,
            "stage_id": stage_id,
            # stage_objective: needed by the commit helper to build
            # the mandated message "AGY stage <stage_id>: <objective>".
            "stage_objective": stage.objective,
            "tasks": [dict(r) for r in task_results_local],
            "verification": copy.deepcopy(verification),
            "changed_files": list(changed),
            "scope_violations": list(violations),
            "acceptance": copy.deepcopy(acceptance),
            "cumulative": copy.deepcopy(cumulative),
            "commit": copy.deepcopy(commit),
            "invariants": list(stage.cumulative_invariants),
            "deterministic_failures": list(failures),
        }

    # -- commit ------------------------------------------------------------------

    async def commit_stage(
        evidence: dict[str, Any],
        changed: list[str],
        task_results_local: list[dict],
        verification: dict,
        *,
        repair_number: int | None = None,
    ) -> dict[str, Any] | None:
        if make_commit is None:
            return None

        payload = dict(evidence)
        if repair_number is not None:
            payload["message"] = (
                f"AGY stage {stage_id} repair {repair_number}: "
                f"{stage.objective}"
            )

        raw = make_commit(payload)
        if inspect.isawaitable(raw):
            raw = await raw
        record = dict(raw) if isinstance(raw, dict) else {}
        record.setdefault("plan_id", plan_id)
        record.setdefault("stage_id", stage_id)
        record.setdefault(
            "message",
            (
                f"AGY stage {stage_id} repair {repair_number}: "
                f"{stage.objective}"
            )
            if repair_number is not None
            else f"AGY stage {stage_id}: {stage.objective}",
        )
        record.setdefault("changed_files", list(changed))
        record["task_results"] = _task_result_projection(task_results_local)
        record["verification"] = copy.deepcopy(verification)

        return record

    async def call_review(evidence: dict[str, Any]) -> tuple[Any, dict]:
        snapshot = copy.deepcopy(evidence)

        if supervisor_review is None:
            return None, snapshot

        raw = await _invoke_review(supervisor_review, snapshot, record_event)

        if isinstance(raw, dict) and raw.get("failover_event") is not None:
            record_event(raw["failover_event"])

        return raw, snapshot

    # -- result assembly ----------------------------------------------------------

    def merge_shared_sinks() -> None:
        for session_id in list(getattr(on_session, "sessions", []) or []):
            if session_id not in sessions:
                sessions.append(session_id)

        for event in list(getattr(on_failover_event, "events", []) or []):
            if not any(existing is event for existing in events):
                events.append(event)

    def assemble(
        *,
        status: str,
        task_results_local: list[dict],
        changed: list[str],
        violations: list[str],
        acceptance: dict,
        cumulative: dict,
        commit: dict | None,
        decision: dict | None,
        failure_reason: str = "",
        replan: dict | None = None,
    ) -> StageRunResult:
        merge_shared_sinks()

        return StageRunResult(
            plan_id=plan_id,
            stage_id=stage_id,
            status=status,
            task_results=[dict(r) for r in task_results_local],
            changed_files=list(changed),
            scope_violations=list(violations),
            task_verification=_verification_map(task_results_local),
            stage_acceptance=copy.deepcopy(acceptance),
            cumulative_gate=copy.deepcopy(cumulative),
            commit=copy.deepcopy(commit),
            supervisor_decision=copy.deepcopy(decision),
            repair_lineage=list(repair_lineage),
            failover_events=copy.deepcopy(events),
            session_ids=list(sessions),
            failure_reason=failure_reason,
            replan=copy.deepcopy(replan),
        )

    # =========================================================================
    # 1-2: decompose + execute bounded tasks
    # =========================================================================

    stage_tasks = decompose_stage(stage, objective=plan.objective)
    stage_task_ids = [task.id for task in stage_tasks]

    run = await run_worker_tasks(
        stage_tasks,
        execute_task=execute_task,
        validate_task=validate_task,
        max_attempts=max_task_attempts,
        collect_changed_files=collect_changed_files,
        session_id_for_task=session_id_for_task,
    )

    task_results: list[dict] = [dict(r) for r in run["task_results"]]
    replan: dict | None = run.get("replan")
    task_verification = _verification_map(task_results)
    changed = _aggregate_changed(task_results)
    violations = check_scope(changed)

    if run["status"] != "SUCCESS":
        reason = "task execution failed"
        if replan:
            reason += (
                f"; bounded replan scope: "
                f"{', '.join(replan.get('replan_scope_task_ids', []))} "
                f"(failed: "
                f"{', '.join(replan.get('failed_task_ids', []))}; "
                f"blocked: "
                f"{', '.join(replan.get('blocked_task_ids', []))})"
            )
        gate_reason = "stage gates not run: task execution failed"
        return assemble(
            status=STAGE_FAILED,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=_skipped_gate(gate_reason),
            cumulative=_skipped_gate(gate_reason),
            commit=None,
            decision=None,
            failure_reason=reason,
            replan=replan,
        )

    # 4: ZERO-DIFF RULE — prose cannot create success.
    if not changed:
        return assemble(
            status=STAGE_FAILED,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=_skipped_gate(
                "stage gates not run: " + ZERO_DIFF_REASON
            ),
            cumulative=_skipped_gate(
                "stage gates not run: " + ZERO_DIFF_REASON
            ),
            commit=None,
            decision=None,
            failure_reason=ZERO_DIFF_REASON,
            replan=replan,
        )

    # =========================================================================
    # 5: stage acceptance + cumulative gate
    # =========================================================================

    stage_acceptance_entry = await run_acceptance()
    cumulative_entry = await run_cumulative()

    failures = deterministic_failures_for(
        violations,
        stage_acceptance_entry,
        cumulative_entry,
    )

    evidence = build_evidence(
        task_results,
        task_verification,
        changed,
        violations,
        stage_acceptance_entry,
        cumulative_entry,
        None,
        failures,
    )

    commit: dict[str, Any] | None = None
    if not failures:
        commit = await commit_stage(
            evidence,
            changed,
            task_results,
            task_verification,
        )
        evidence["commit"] = copy.deepcopy(commit)

    # =========================================================================
    # 8-9: supervisor review + deterministic override
    # =========================================================================

    if supervisor_review is None:
        # No gatekeeper wired: deterministic checks alone decide.
        if failures:
            return assemble(
                status=STAGE_FAILED,
                task_results_local=task_results,
                changed=changed,
                violations=violations,
                acceptance=stage_acceptance_entry,
                cumulative=cumulative_entry,
                commit=commit,
                decision=None,
                failure_reason="; ".join(failures),
                replan=replan,
            )
        return assemble(
            status=STAGE_ACCEPTED,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=stage_acceptance_entry,
            cumulative=cumulative_entry,
            commit=commit,
            decision=None,
            replan=replan,
        )

    raw, _reviewed_evidence = await call_review(evidence)
    decision = _normalize_review(raw, failures)

    if decision["decision"] == "ACCEPT":
        # deterministic_failures was empty (else forced REPAIR).
        return assemble(
            status=STAGE_ACCEPTED,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=stage_acceptance_entry,
            cumulative=cumulative_entry,
            commit=commit,
            decision=decision,
            replan=replan,
        )

    if decision["decision"] == "REJECT":
        return assemble(
            status=STAGE_REJECTED,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=stage_acceptance_entry,
            cumulative=cumulative_entry,
            commit=commit,
            decision=decision,
            failure_reason="; ".join(failures),
            replan=replan,
        )

    # =========================================================================
    # 10: ONE bounded in-stage repair cycle (decision == REPAIR)
    # =========================================================================

    command, expected_exit = failing_command()
    failure_names = [f for f in failures] or [
        "supervisor requested repair: " + _shorten(
            decision.get("rationale", ""), 200
        )
    ]
    if not failures:
        failure_names = [
            _shorten(target, 160)
            for target in decision.get("repair_targets", [])
        ] or ["supervisor requested repair"]

    repair_id = f"{stage_id}-repair-{len(repair_lineage) + 1}"
    repair_lineage.append(repair_id)

    repair_files = [path for path in changed if path in allow_set]
    if not repair_files:
        repair_files = list(allowed)
    repair_files = repair_files[:MAX_REPAIR_TASK_FILES]

    repair_task = WorkerTask(
        id=repair_id,
        goal=_shorten(
            f"Fix failing checks for stage {stage_id}: "
            + ", ".join(failure_names),
            400,
        ),
        files=repair_files,
        steps=[
            "Resolve exactly these failing checks: "
            + "; ".join(failure_names),
            "Re-run the failing verification command and leave it "
            f"passing: {command}",
        ],
        constraints=list(stage.cumulative_invariants),
        verification=TaskVerification(
            command=command,
            expected_exit_code=expected_exit,
        ),
        worker_instruction=(
            f"Stage {stage_id}: repair attempt "
            f"{len(repair_lineage)} — fix ONLY the listed failing "
            "checks inside the stage's allowed files; sibling scope "
            "stays out of bounds."
        ),
        depends_on=list(stage_task_ids),
    )

    repair_run = await run_worker_tasks(
        # The repair task is DEPENDENT on the stage's original tasks
        # (``repair_task.depends_on`` stays verbatim as constructed
        # above), but P2's run_worker_tasks orders only within its
        # own task list — those dependencies are already satisfied
        # (the original tasks ran in this same stage execution), so
        # the execution copy carries no in-run dependency. Same id,
        # same lineage, no re-execution of finished work.
        [repair_task.model_copy(update={"depends_on": []})],
        execute_task=execute_task,
        validate_task=validate_task,
        max_attempts=max_task_attempts,
        collect_changed_files=collect_changed_files,
        session_id_for_task=session_id_for_task,
    )

    task_results.extend(dict(r) for r in repair_run["task_results"])
    task_verification = _verification_map(task_results)
    changed = _aggregate_changed(task_results)
    violations = check_scope(changed)

    # Re-verify: scope + stage acceptance + cumulative gate all re-run.
    stage_acceptance_entry = await run_acceptance()
    cumulative_entry = await run_cumulative()

    failures = deterministic_failures_for(
        violations,
        stage_acceptance_entry,
        cumulative_entry,
    )
    repair_green = (
        bool(changed)
        and not failures
        and repair_run["status"] == "SUCCESS"
    )

    evidence = build_evidence(
        task_results,
        task_verification,
        changed,
        violations,
        stage_acceptance_entry,
        cumulative_entry,
        commit,
        failures,
    )

    if not repair_green:
        # Bounded stop: STAGE_REPAIR with the remaining evidence.
        # No second repair cycle, no re-review, no commit.
        return assemble(
            status=STAGE_REPAIR,
            task_results_local=task_results,
            changed=changed,
            violations=violations,
            acceptance=stage_acceptance_entry,
            cumulative=cumulative_entry,
            commit=commit,
            decision=decision,
            failure_reason="repair cycle did not clear all checks: "
            + "; ".join(failures),
            replan=replan,
        )

    repair_number = len(repair_lineage)
    commit = await commit_stage(
        evidence,
        changed,
        task_results,
        task_verification,
        repair_number=repair_number,
    )
    evidence["commit"] = copy.deepcopy(commit)

    # One more supervisor call, then the final constrained decision.
    raw, _reviewed_evidence = await call_review(evidence)
    final_decision = _normalize_review(raw, failures)

    if final_decision["decision"] == "ACCEPT" and not failures:
        status = STAGE_ACCEPTED
    elif final_decision["decision"] == "REJECT":
        status = STAGE_REJECTED
    else:
        # REPAIR again (or a forced repair) -> bounded stop.
        status = STAGE_REPAIR
        if final_decision["decision"] == "ACCEPT":
            # unreachable via _normalize_review, kept fail-closed
            final_decision["decision"] = "REPAIR"

    return assemble(
        status=status,
        task_results_local=task_results,
        changed=changed,
        violations=violations,
        acceptance=stage_acceptance_entry,
        cumulative=cumulative_entry,
        commit=commit,
        decision=final_decision,
        failure_reason="" if status == STAGE_ACCEPTED
        else "; ".join(failures),
        replan=replan,
    )