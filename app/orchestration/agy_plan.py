"""P4 multi-stage autonomous loop: plan execution + the plan-level gate.

``execute_plan`` drives a whole ``DevelopmentPlan`` through P3's
``execute_stage`` stage by stage, then adds the PLAN-LEVEL GATE and
ONE bounded plan-repair cycle. Every external effect is injected —
the driver itself opens no network, launches no process and calls no
model — and it NEVER raises for repository/test/model outcomes (only
wiring errors such as a bad executor shape or an unorderable stage
DAG propagate), exactly mirroring P3 ``execute_stage``.

Deterministic flow (order matters):

1. ``stage_execution_order(plan)`` (P2 topological order; a bad DAG
   is a wiring error and propagates).
2. Persist ``PLAN_IN_PROGRESS`` ``plan.json`` BEFORE any execution
   (recovery header for the whole run).
3. For EACH stage in order:
   a. ``make_executor(plan, stage)`` builds the P3 live-executor
      quadruple for that stage;
   b. ``execute_stage(...)`` runs it with the injected
      ``run_command``, ``cumulative_suite``, ``make_commit`` and
      ``supervisor_review`` — scope check, zero-diff rule,
      acceptance, cumulative gate, commit, deterministic override
      and the ONE in-stage repair cycle are P3's code, reused
      verbatim, never re-implemented here;
   c. persistence is owned HERE (P3's ``execute_stage`` does not
      persist): ``stage.json`` + one record per worker task (session
      id AND capture source) + ``commit.json`` when the stage
      committed;
   d. ONLY ``STAGE_ACCEPTED`` advances. Otherwise the loop STOPS with
      ``stopped_at`` set — ``STAGE_FAILED`` -> ``PLAN_FAILED``,
      ``STAGE_REJECTED`` -> ``PLAN_REJECTED``, ``STAGE_REPAIR`` ->
      bounded plan repair (step 5). Later stages never run because
      their dependencies are not satisfied;
   e. failover events, session ids and per-task capture sources are
      aggregated into the running ``PlanRunResult``.
4. When ALL stages are ``STAGE_ACCEPTED`` the PLAN-LEVEL GATE runs:
   every ``plan.final_acceptance`` command through ``run_command``,
   then the plan ``cumulative_suite`` re-run ONCE at plan level. The
   evidence bundle ``{plan_id, objective, stage_results (status +
   commit per stage), final_acceptance, cumulative_invariants,
   deterministic_failures}`` goes to ``supervisor_review``. The
   deterministic-override rule is the SAME rule the P3 stage gate
   uses — ``agy_execute._normalize_review`` is imported, not
   duplicated: non-empty ``deterministic_failures`` + model
   ``ACCEPT`` -> forced ``REPAIR`` with ``deterministic_override=True``;
   invalid review output fails closed as ``REJECT``.
   ``ACCEPT`` with green gates -> ``PLAN_ACCEPTED``; ``REJECT`` ->
   ``PLAN_REJECTED``; ``REPAIR`` -> step 5.
5. BOUNDED PLAN REPAIR (``max_plan_repairs``, default 1): a plan-gate
   ``REPAIR`` — or a stage-level ``STAGE_REPAIR`` that stopped the
   loop — builds ONE bounded stage ``plan-repair-<n>`` targeting
   ONLY the concrete failing acceptance/invariant (failing commands
   kept verbatim; a stage stop reuses that stage's scope). It
   preserves ``plan_id`` and the lineage, NEVER restarts the plan
   and NEVER re-runs an already-accepted stage. The repair stage
   executes through the SAME ``execute_stage`` path (its own commit +
   its own stage gate), then the plan-level gate re-runs once more.
   Exhausted budget -> stop at ``PLAN_REPAIR`` / ``PLAN_REJECTED`` as
   appropriate; there is no infinite loop.
6. Persist the terminal ``plan.json`` (all aggregates included) and
   return the ``PlanRunResult``.

Session capture (P4): per stage the driver resolves each task's
``(session_id, source)`` through ``session_capturer`` when injected,
otherwise through the live executor's ``execute_task.session_log``
(``build_live_executor``'s ``TaskSessionLog``, fed by
``capture_session`` with its server read-back). ``on_session`` is a
bridge over that resolver, so ``StageRunResult.session_ids`` and the
per-task records carry the ids the server provably has, and the
per-task ``session_source`` records HOW each id was captured
(``stdout`` / ``known`` / ``readback`` / ``none``).
"""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import AgySettings
from app.orchestration.agy_execute import (
    STAGE_ACCEPTED,
    STAGE_FAILED,
    STAGE_REJECTED,
    STAGE_REPAIR,
    _as_command,
    _invoke_review,
    _normalize_review,
    _skipped_gate,
    execute_stage,
)
from app.orchestration.agy_runstate import PlanRunState, StageRunState
from app.orchestration.agy_session import SOURCE_NONE
from app.orchestration.plan_decompose import stage_execution_order
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage

PLAN_IN_PROGRESS = "PLAN_IN_PROGRESS"
PLAN_ACCEPTED = "PLAN_ACCEPTED"
PLAN_REPAIR = "PLAN_REPAIR"
PLAN_REJECTED = "PLAN_REJECTED"
PLAN_FAILED = "PLAN_FAILED"
# Defensive terminal status: no stage ever ran — only reachable with a
# programmatically mangled plan (pydantic rejects empty stage lists at
# construction). Every possible run still gets an explicit marker.
PLAN_INCOMPLETE = "PLAN_INCOMPLETE"

CLIP_CONTEXT_LIMIT = 4000

REPAIR_ID_PREFIX = "plan-repair-"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _event_key(event: Any) -> str:
    """Canonical identity for event dedup (JSON, stable key order)."""
    import json

    try:
        return json.dumps(event, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - identity checks still apply
        return f"<unserializable:{id(event)}>"


def _append_event(target: list, event: Any) -> bool:
    """Dedup append (identity first, canonical JSON second); True when added."""
    if event is None:
        return False
    key = _event_key(event)
    for existing in target:
        if existing is event or _event_key(existing) == key:
            return False
    target.append(event)
    return True


# ---------------------------------------------------------------------------
# Result record
# ---------------------------------------------------------------------------


@dataclass
class PlanRunResult:
    """
    Everything one plan execution produced (the ``plan.json`` content).

    Contract fields: ``plan_id``, ``objective``, ``status``,
    ``stage_order``, ``stage_results`` (stage_id -> StageRunResult
    .to_dict()), ``cumulative_invariants_ok``, ``final_acceptance``,
    ``plan_gate``, ``repair_lineage``, ``failover_events``,
    ``session_ids``, ``created``/``updated``. The remaining fields are
    additive evidence (documented, like P3's ``failure_reason``):
    ``session_sources`` (task_id -> capture source; the P3-defect
    audit trail), ``stopped_at`` (which stage stopped the loop),
    ``plan_gate_reviews`` (every plan-level review in order — the last
    one is also mirrored into ``plan_gate``).
    """

    plan_id: str
    objective: str
    status: str
    stage_order: list[str]
    stage_results: dict[str, dict]
    cumulative_invariants_ok: bool = False
    final_acceptance: dict = field(default_factory=dict)
    plan_gate: dict = field(default_factory=dict)
    repair_lineage: list[str] = field(default_factory=list)
    failover_events: list[dict] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    session_sources: dict[str, str] = field(default_factory=dict)
    stopped_at: str | None = None
    plan_gate_reviews: list[dict] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "objective": self.objective,
            "status": self.status,
            "stage_order": list(self.stage_order),
            "stage_results": {
                stage_id: dict(stage_result)
                for stage_id, stage_result in self.stage_results.items()
            },
            "cumulative_invariants_ok": self.cumulative_invariants_ok,
            "final_acceptance": dict(self.final_acceptance),
            "plan_gate": dict(self.plan_gate),
            "repair_lineage": list(self.repair_lineage),
            "failover_events": list(self.failover_events),
            "session_ids": list(self.session_ids),
            "session_sources": dict(self.session_sources),
            "stopped_at": self.stopped_at,
            "plan_gate_reviews": [dict(review) for review in self.plan_gate_reviews],
            "created": self.created,
            "updated": self.updated,
        }


# ---------------------------------------------------------------------------
# Session-evidence resolution (executor log / injected capturer)
# ---------------------------------------------------------------------------


def _as_session_record(value: Any) -> dict[str, Any] | None:
    """
    Normalize one capturer value to ``{session_id, source}``.

    Accepted shapes: a dict with ``session_id``/``source`` keys, a
    ``(session_id, source)`` tuple, or a bare source string (the
    ``{task_id: source}`` convenience map).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        session_id = value.get("session_id")
        source = value.get("source") or SOURCE_NONE
        return {
            "session_id": session_id if session_id else None,
            "source": str(source),
        }
    if isinstance(value, (tuple, list)) and 1 <= len(value) <= 2:
        session_id = value[0]
        source = value[1] if len(value) == 2 else SOURCE_NONE
        return {
            "session_id": session_id if session_id else None,
            "source": str(source or SOURCE_NONE),
        }
    if isinstance(value, str):
        return {"session_id": None, "source": value}
    return None


def _session_record(resolver: Any, task_id: str | None) -> dict[str, Any] | None:
    """The task's ``(session_id, source)`` from the active resolver."""
    if resolver is None or not task_id:
        return None

    raw: Any = None
    if isinstance(resolver, dict):
        raw = resolver.get(task_id)
    else:
        getter = getattr(resolver, "get", None)
        if callable(getter):
            raw = getter(task_id)
        elif callable(resolver):
            raw = resolver(task_id)

    return _as_session_record(raw)


class _SessionBridge:
    """
    ``on_session`` for ``execute_stage``: reads the executor-captured
    per-task evidence so stage results carry the real session ids,
    while collection stays local to one stage.
    """

    def __init__(self, resolver: Any) -> None:
        self.sessions: list[str] = []
        self.resolver = resolver

    def __call__(self, session_id: str) -> None:
        if session_id and session_id not in self.sessions:
            self.sessions.append(session_id)

    def last_for(self, task_id: str) -> str | None:
        record = _session_record(self.resolver, task_id)
        return record["session_id"] if record else None

    def source_for(self, task_id: str) -> str:
        record = _session_record(self.resolver, task_id)
        return str(record["source"]) if record else SOURCE_NONE


class _EventSink:
    """Plan-gate ``on_failover_event`` sink: collect once, forward once."""

    def __init__(self, forward: Callable[[dict], None] | None) -> None:
        self.events: list[dict] = []
        self._forward = forward

    def __call__(self, event: dict) -> None:
        if event is None:
            return
        if _append_event(self.events, event) and self._forward is not None:
            self._forward(event)


# ---------------------------------------------------------------------------
# Repair-stage construction (bounded: ONE concrete failing target)
# ---------------------------------------------------------------------------


def _commands_from_checks(
    gate: dict | None,
    fallback_commands: list[AcceptanceCommand],
) -> list[AcceptanceCommand]:
    """
    ``AcceptanceCommand``s for the checks a gate actually FAILED.

    Acceptance checks carry ``expected_exit_code``; cumulative checks
    do not, so the expected code is recovered from the suite list by
    command string (falling back to 0 — what every suite uses).
    """
    expected_by_command = {
        command.command: command.expected_exit_code
        for command in fallback_commands
    }

    out: list[AcceptanceCommand] = []
    seen: set[tuple[str, int]] = set()
    for check in (gate or {}).get("checks", []) or []:
        if check.get("ok", False):
            continue
        command = str(check.get("command", ""))
        if not command:
            continue
        expected = check.get("expected_exit_code")
        if expected is None:
            expected = expected_by_command.get(command, 0)
        key = (command, int(expected))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            AcceptanceCommand(
                command=command, expected_exit_code=int(expected)
            )
        )

    return out


def _union_stage_scope(
    plan: DevelopmentPlan,
) -> tuple[list[str], list[str]]:
    """``(allowed_files, cumulative_invariants)`` unioned over all stages."""
    files: list[str] = []
    invariants: list[str] = []
    for stage in plan.stages:
        for path in stage.allowed_files:
            if path not in files:
                files.append(path)
        for invariant in stage.cumulative_invariants:
            if invariant not in invariants:
                invariants.append(invariant)
    return files, invariants


class _RepairContext:
    """
    Everything ONE plan-repair cycle needs: the concrete failing
    commands/conditions, the repair stage's scope (allowed files) and
    the invariants it must re-establish. Captured at the moment a
    repair is requested (plan gate or stage stop) so the repair stage
    targets ONLY what failed.
    """

    def __init__(
        self,
        *,
        failures: list[str],
        conditions: list[str],
        failing_commands: list[AcceptanceCommand],
        allowed_files: list[str],
        invariants: list[str],
        reason: str,
    ) -> None:
        self.failures = list(failures)
        self.conditions = list(conditions)
        self.failing_commands = list(failing_commands)
        self.allowed_files = list(allowed_files)
        self.invariants = list(invariants)
        self.reason = reason


def _gate_repair_context(
    plan: DevelopmentPlan,
    gate: dict[str, Any],
    decision: dict[str, Any],
    cumulative_commands: list[AcceptanceCommand],
) -> _RepairContext:
    """Repair context from a plan-gate decision (failing checks only)."""
    failures = list(gate.get("failures", []))
    conditions = [
        str(target)
        for target in decision.get("repair_targets", [])
        if str(target).strip()
    ] or failures or ["plan-level gate requested a bounded repair"]

    failing_commands = _commands_from_checks(
        gate.get("final_acceptance"), list(plan.final_acceptance)
    ) + _commands_from_checks(gate.get("cumulative"), cumulative_commands)

    files, invariants = _union_stage_scope(plan)
    rationale = _clip(str(decision.get("rationale") or ""), 200)

    return _RepairContext(
        failures=failures or conditions,
        conditions=conditions,
        failing_commands=failing_commands,
        allowed_files=files,
        invariants=invariants,
        reason=(
            "the plan-level gate requested repair"
            + (f": {rationale}" if rationale else "")
        ),
    )


def _stage_stop_context(
    plan: DevelopmentPlan,
    stage_result,
    cumulative_commands: list[AcceptanceCommand],
) -> _RepairContext:
    """Repair context from a stage-level STAGE_REPAIR stop."""
    stage = next(
        (
            candidate
            for candidate in plan.stages
            if candidate.stage_id == stage_result.stage_id
        ),
        None,
    )
    if stage is None:  # not reachable for plan stages; fail closed
        raise ValueError(
            "repair context requested for unknown stage "
            f"{stage_result.stage_id!r}"
        )

    acceptance_gate = stage_result.stage_acceptance or {}
    cumulative_gate = stage_result.cumulative_gate or {}

    failures = []
    for check in acceptance_gate.get("checks", []):
        if not check.get("ok", False):
            failures.append(
                f"stage acceptance failed: {check.get('command')} "
                f"exited {check.get('exit_code')}"
            )
    for check in cumulative_gate.get("checks", []):
        if not check.get("ok", False):
            failures.append(
                f"cumulative gate failed: {check.get('command')} "
                f"exited {check.get('exit_code')}"
            )
    if not failures:
        failures = [
            _clip(stage_result.failure_reason or "stage repair requested", 200)
        ]

    failing_commands = _commands_from_checks(
        acceptance_gate, list(stage.acceptance_commands)
    ) + _commands_from_checks(cumulative_gate, cumulative_commands)

    return _RepairContext(
        failures=failures,
        conditions=failures,
        failing_commands=failing_commands,
        allowed_files=list(stage.allowed_files),
        invariants=list(stage.cumulative_invariants),
        reason=(
            f"stage {stage.stage_id} stopped the plan with "
            f"{stage_result.status}"
        ),
    )


def _build_repair_stage(
    plan: DevelopmentPlan,
    context: _RepairContext,
    repair_id: str,
) -> PlanStage:
    """The bounded ``plan-repair-<n>`` stage (same plan, no restart)."""
    failing_commands = context.failing_commands or list(
        plan.final_acceptance
    )
    conditions = [
        condition for condition in context.conditions if condition.strip()
    ] or [f"repair {repair_id} leaves the plan-level checks passing"]

    detail = "; ".join(
        _clip(failure, 200) for failure in context.failures[:8]
    ) or context.reason

    implementation_context = (
        f"Repair cycle {repair_id} for plan {plan.plan_id}: the plan "
        f"stopped because {context.reason}.\nFailing checks:\n"
        + "\n".join(f"- {condition}" for condition in conditions)
        + "\nFailing commands (run them until green):\n"
        + "\n".join(
            f"- exit {command.expected_exit_code}: {command.command}"
            for command in failing_commands
        )
        + "\nConstraints that must hold afterwards:\n"
        + (
            "\n".join(f"- {invariant}" for invariant in context.invariants)
            or "- (no declared invariants)"
        )
    )

    return PlanStage(
        stage_id=repair_id,
        objective=_clip(f"Repair plan {plan.plan_id}: {detail}", 400),
        dependencies=[],
        allowed_files=list(context.allowed_files),
        implementation_context=_clip(
            implementation_context, CLIP_CONTEXT_LIMIT
        ),
        acceptance_commands=list(failing_commands),
        acceptance_conditions=conditions,
        cumulative_invariants=list(context.invariants),
    )


# ---------------------------------------------------------------------------
# The plan-level loop
# ---------------------------------------------------------------------------


async def execute_plan(
    plan: DevelopmentPlan,
    *,
    settings: AgySettings,
    repo,
    make_executor,
    run_command,
    cumulative_suite,
    make_commit,
    supervisor_review,
    run_state: StageRunState | PlanRunState | None = None,
    session_capturer=None,
    on_stage=None,
    on_failover_event=None,
    max_plan_repairs: int = 1,
) -> PlanRunResult:
    """
    Execute a full development plan; see the module docstring's
    pinned 6-step flow. Returns the :class:`PlanRunResult`; never
    raises for repository/test/model outcomes (only wiring errors in
    the injected callables propagate, mirroring P3).

    * ``make_executor(plan, stage)`` -> the P3 live-executor
      quadruple ``(execute_task, validate_task,
      collect_changed_files, run_command)`` for that stage; the
      driver uses the single top-level ``run_command`` for every
      acceptance/plan-gate execution so there is ONE command seam;
    * ``cumulative_suite`` -> plan-level suite list, used per stage
      AND re-run once by the plan gate;
    * ``supervisor_review`` -> reviewer for stage gates AND the
      plan-level gate (same injected callable, deterministic call
      order);
    * ``run_state`` -> plan-level persistor (``plan.json`` one level
      above the stage directories); per-stage state is derived from
      the same ``root`` automatically;
    * ``session_capturer`` -> optional per-task session-evidence
      resolver (``{task_id: record|source}``, ``.get(task_id)``,
      ``source_for(task_id)`` or a callable); default reads the live
      executor's ``execute_task.session_log``;
    * ``on_stage(stage_id, StageRunResult)`` runs after every executed
      stage (may be async); ``on_failover_event`` receives plan-gate
      failover events directly — stage events arrive with their stage
      results;
    * ``max_plan_repairs`` bounds the plan-repair cycles (default 1).
    """
    _ = (settings, repo)  # carried for live wiring symmetry

    # 1: topological stage order (a bad DAG is a wiring error: raise)
    order = stage_execution_order(plan)
    stage_ids = [stage.stage_id for stage in order]

    # 3-style shared state (created before the header is written).
    result = PlanRunResult(
        plan_id=plan.plan_id,
        objective=plan.objective,
        status=PLAN_IN_PROGRESS,
        stage_order=list(stage_ids),
        stage_results={},
        created=_now(),
        updated=_now(),
    )
    gate_events = _EventSink(on_failover_event)
    cumulative_commands = [
        _as_command(command) for command in (cumulative_suite or [])
    ]
    repair_context: _RepairContext | None = None
    stopped_stage_result = None

    # -- persistence helpers -------------------------------------------------

    def persist_plan() -> None:
        if run_state is None:
            return
        result.updated = _now()
        run_state.save_plan(result)

    def stage_state_for(stage_id: str) -> StageRunState | None:
        if run_state is None:
            return None
        if getattr(run_state, "stage_id", None) == stage_id:
            return run_state
        return StageRunState(run_state.root, plan.plan_id, stage_id)

    def task_record(task_result: dict, resolver: Any) -> dict:
        """Per-task run-state record WITH the session source (P4)."""
        record = dict(task_result)
        captured = _session_record(
            resolver, record.get("worker_task_id")
        ) or {}
        record["session_id"] = record.get("session_id") or captured.get(
            "session_id"
        )
        record["session_source"] = captured.get("source") or SOURCE_NONE
        record.setdefault("task_id", record.get("worker_task_id"))
        return record

    def persist_stage(stage_result, resolver: Any) -> None:
        state = stage_state_for(stage_result.stage_id)
        if state is None:
            return
        state.save_stage(stage_result)
        for task_result in stage_result.task_results:
            state.save_task(task_record(task_result, resolver))
        if stage_result.commit:
            state.save_commit(stage_result.commit)

    def aggregate(stage_result, resolver: Any) -> None:
        for event in stage_result.failover_events:
            _append_event(result.failover_events, event)
        for session_id in stage_result.session_ids:
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)
        for task_result in stage_result.task_results:
            task_id = task_result.get("worker_task_id")
            captured = _session_record(resolver, task_id) or {}
            session_id = task_result.get("session_id") or captured.get(
                "session_id"
            )
            if task_id:
                result.session_sources[task_id] = str(
                    captured.get("source") or SOURCE_NONE
                )
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)

    # -- one stage through the P3 path --------------------------------------

    async def run_one_stage(stage: PlanStage):
        executor = make_executor(plan, stage)
        if not isinstance(executor, (tuple, list)) or len(executor) != 4:
            length = len(executor) if hasattr(executor, "__len__") else "?"
            raise TypeError(
                "make_executor must return the 4-element executor "
                f"quadruple, got {type(executor).__name__} of length {length}"
            )

        (
            execute_task,
            validate_task,
            collect_changed_files,
            executor_run_command,
        ) = executor
        # The driver's single injected run_command is THE command seam
        # (documented in the signature docstring).
        _ = executor_run_command

        resolver = (
            session_capturer
            if session_capturer is not None
            else getattr(execute_task, "session_log", None)
        )
        bridge = _SessionBridge(resolver)

        stage_result = await execute_stage(
            plan,
            stage,
            settings=settings,
            execute_task=execute_task,
            validate_task=validate_task,
            collect_changed_files=collect_changed_files,
            run_command=run_command,
            cumulative_suite=cumulative_commands,
            make_commit=make_commit,
            supervisor_review=supervisor_review,
            on_session=bridge,
            on_failover_event=on_failover_event,
        )

        aggregate(stage_result, resolver)
        persist_stage(stage_result, resolver)
        result.stage_results[stage.stage_id] = stage_result.to_dict()

        if on_stage is not None:
            await _maybe_await(on_stage(stage.stage_id, stage_result))

        return stage_result, resolver

    # -- plan-level gate -----------------------------------------------------

    async def run_command_list(commands, name_prefix: str) -> dict[str, Any]:
        """Plan gate runner: every AcceptanceCommand verbatim, recorded."""
        checks: list[dict[str, Any]] = []
        normalized = [_as_command(command) for command in (commands or [])]

        for index, command in enumerate(normalized, start=1):
            exit_code, output = await run_command(command)
            checks.append(
                {
                    "name": command.description
                    or f"{name_prefix}-{index:02d}",
                    "command": command.command,
                    "expected_exit_code": command.expected_exit_code,
                    "exit_code": exit_code,
                    "ok": exit_code == command.expected_exit_code,
                    "output": output,
                }
            )

        return {"ok": all(check["ok"] for check in checks), "checks": checks}

    async def plan_gate(extra_failures: list[str]) -> dict[str, Any]:
        """Step 4/5: final acceptance + one cumulative run + review."""
        final_entry = await run_command_list(
            plan.final_acceptance, "final-acceptance"
        )
        cumulative_entry = await run_command_list(
            cumulative_commands, "plan-cumulative"
        )

        failures = list(extra_failures)
        for check in final_entry["checks"]:
            if not check["ok"]:
                failures.append(
                    "final acceptance failed: "
                    f"{check['command']} exited {check['exit_code']}, "
                    f"expected {check['expected_exit_code']}"
                )
        for check in cumulative_entry["checks"]:
            if not check["ok"]:
                failures.append(
                    "plan-level cumulative gate failed: "
                    f"{check['command']} exited {check['exit_code']}"
                )

        evidence = {
            "plan_id": plan.plan_id,
            "objective": plan.objective,
            "stage_results": {
                stage_id: {
                    "status": stage_result.get("status"),
                    "commit": copy.deepcopy(stage_result.get("commit")),
                }
                for stage_id, stage_result in result.stage_results.items()
            },
            "final_acceptance": final_entry,
            "cumulative_invariants": cumulative_entry,
            "deterministic_failures": list(failures),
        }

        if supervisor_review is None:
            raw = {
                "decision": "ACCEPT" if not failures else "REPAIR",
                "rationale": "no supervisor wired: deterministic checks decide",
                "repair_targets": [],
                "model": None,
                "deterministic_override": False,
            }
        else:
            snapshot = copy.deepcopy(evidence)
            raw = await _invoke_review(
                supervisor_review, snapshot, gate_events
            )
            if (
                isinstance(raw, dict)
                and raw.get("failover_event") is not None
            ):
                gate_events(raw["failover_event"])

        decision = _normalize_review(raw, failures)

        result.final_acceptance = final_entry
        result.cumulative_invariants_ok = bool(cumulative_entry["ok"])
        result.plan_gate = decision
        result.plan_gate_reviews.append(decision)
        for event in gate_events.events:
            _append_event(result.failover_events, event)
        persist_plan()

        return {
            "decision": decision,
            "failures": list(failures),
            "final_acceptance": final_entry,
            "cumulative": cumulative_entry,
        }

    # -- step 5: bounded plan repair -----------------------------------------

    async def repair_cycle() -> str:
        nonlocal repair_context
        while True:
            budget_left = len(result.repair_lineage) < max(
                0, int(max_plan_repairs)
            )
            if repair_context is None or not budget_left:
                # Budget exhausted (or nothing concrete to target):
                # stop at PLAN_REPAIR; the loop can never spin.
                return PLAN_REPAIR

            repair_id = (
                f"{REPAIR_ID_PREFIX}{len(result.repair_lineage) + 1}"
            )
            repair_stage = _build_repair_stage(
                plan, repair_context, repair_id
            )
            result.repair_lineage.append(repair_id)

            repair_result, _resolver = await run_one_stage(repair_stage)

            extra_failures: list[str] = []
            if repair_result.status != STAGE_ACCEPTED:
                extra_failures.append(
                    f"plan repair stage {repair_id} did not reach "
                    f"STAGE_ACCEPTED: {repair_result.status}"
                )

            gate = await plan_gate(extra_failures)
            decision = gate["decision"]

            if decision["decision"] == "REJECT":
                return PLAN_REJECTED
            if (
                decision["decision"] == "ACCEPT"
                and repair_result.status == STAGE_ACCEPTED
            ):
                return PLAN_ACCEPTED

            # REPAIR again (or an ACCEPT that cannot stand): refresh
            # the context to THIS gate so a permitted second cycle
            # targets what still fails; the budget check above ends
            # the cycle at PLAN_REPAIR when max_plan_repairs is met.
            repair_context = _gate_repair_context(
                plan, gate, decision, cumulative_commands
            )

    # -- 2: PLAN_IN_PROGRESS header BEFORE execution -------------------------

    persist_plan()

    # -- 3: per-stage loop ----------------------------------------------------

    if not order:
        result.status = PLAN_INCOMPLETE  # defensive: nothing to run
        persist_plan()
        return result

    for stage in order:
        stage_result, _resolver = await run_one_stage(stage)
        if stage_result.status != STAGE_ACCEPTED:
            result.stopped_at = stage.stage_id
            stopped_stage_result = stage_result
            break

    # -- 4/5: plan-level gate + bounded repair --------------------------------

    gate_never_ran = _skipped_gate(
        "plan gate not run: stage loop stopped at "
        f"{result.stopped_at or '(unknown stage)'}"
    )

    if stopped_stage_result is None:
        # All stages accepted -> the plan-level gate decides.
        gate = await plan_gate([])
        decision = gate["decision"]
        if decision["decision"] == "ACCEPT":
            result.status = PLAN_ACCEPTED
        elif decision["decision"] == "REJECT":
            result.status = PLAN_REJECTED
        else:
            repair_context = _gate_repair_context(
                plan, gate, decision, cumulative_commands
            )
            result.status = await repair_cycle()
    elif stopped_stage_result.status == STAGE_FAILED:
        result.status = PLAN_FAILED
        result.final_acceptance = gate_never_ran
    elif stopped_stage_result.status == STAGE_REJECTED:
        result.status = PLAN_REJECTED
        result.final_acceptance = gate_never_ran
    else:  # STAGE_REPAIR -> bounded plan repair (step 5)
        repair_context = _stage_stop_context(
            plan, stopped_stage_result, cumulative_commands
        )
        result.status = await repair_cycle()

    result.updated = _now()
    persist_plan()
    return result