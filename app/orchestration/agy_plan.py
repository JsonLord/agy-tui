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

P6 RESUME + EVIDENCE (contract §2 — all three seams are OPTIONAL and
default to ``False``/``None``, so the P5 call path is unchanged):

* ``resume=false`` -> as before. ``resume=True`` -> before each
  stage, ``agy_resume.recovery_state`` consults the persisted
  artifacts: ``STAGE_ACCEPTED`` stages are SKIPPED ENTIRELY (the
  executor is never even built — ``make_executor`` is not called for
  them) and their persisted ``StageRunResult`` is carried verbatim
  into the result; ``STAGE_COMMITTED`` / ``TASK_VERIFIED`` re-enter
  at their boundary through ``agy_resume.run_stage_boundary_gate``
  (supervisor-only / gates-without-dispatch; ``TASK_VERIFIED`` never
  re-dispatches tasks — its stage acceptance + cumulative gate rerun
  through ``run_command``, which is also how P3's "verification
  always reruns" holds — matching contract test item 4); every other
  state runs the stage normally through P3 (verification never
  weakens on an incomplete boundary). A seed step first restores the
  persisted aggregates (failover events, repair lineage, sessions,
  gate reviews, stage results) so the run re-enters instead of
  restarting, and the bounded plan-repair budget keeps counting from
  the persisted ``repair_lineage``.
  A persistently ``PLAN_ACCEPTED`` run state returns the persisted
  result idempotently — no model calls, no new commits — with the
  ``PLAN_ALREADY_ACCEPTED`` event.
* ``timeline`` (an ``agy_timeline.Timeline`` or ``None``) — when
  attached, the run append-only emits plan/stage/task/gate events
  (``PLAN_STARTED`` once per fresh process — resumed runs emit
  ``RUN_RESUMED`` instead of ``PLAN_STARTED`` — ``STAGE_STARTED``,
  ``TASK_DISPATCHED``/``TASK_COMPLETED``/``TASK_VERIFIED`` at the
  injected dispatch/verify seams so a killed run's stream is true,
  ``STAGE_COMMITTED``, ``STAGE_ACCEPTED``,
  ``PLAN_REPAIR_REQUESTED`` on each bounded repair cycle,
  ``MODEL_FAILOVER`` once per newly aggregated failover event, and
  terminal ``PLAN_ACCEPTED``/``PLAN_REJECTED``), then writes
  ``observability.json`` at plan end. With ``timeline=None`` no
  event logic runs.
* ``commit_gate_writer`` — ``callable(record) -> Path | None`` for
  the first-class ``agy_commitgate.CommitGateRecord`` evidence. The
  stage-commit gate point writes one record per stage commit and the
  plan-gate point writes ``commits/plan-gate.json``; with no writer
  injected (but a ``run_state`` present) the records are persisted
  into the run dir by ``agy_commitgate.persist_commit_gate_record``,
  with the CONSTRAINED decision (P4 deterministic override applied)
  as its input.

Plan restarts must not re-type a plan: plan start also persists the
ORIGINAL document via ``run_state.save_plan_doc(plan)`` to
``state/runs/<plan_id>/plan_doc.json`` (idempotent; a different
document for the same plan_id is a hard error).
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
from app.orchestration.agy_session import SOURCE_KNOWN, SOURCE_NONE
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


def _scalar(value: Any) -> Any:
    """
    Timeline-safe scalar for event fields: plain scalars pass,
    enum-like values unwrap to their ``value``, anything else
    stringifies (events never carry nested state).
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raw = getattr(value, "value", None)
    if isinstance(raw, (str, int, float, bool)):
        return raw
    return str(value)


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


class _ResumeResolver:
    """
    P6 session resolver for a resumed stage: the injected resume
    policy (``agy_resume.ResumeSessionPolicy``) composed with the
    executor's own session log.

    * a REUSE hint resolves the stage's dispatched tasks to the
      recovered ``"known"`` session (the persisted boundary is the
      record of the task's session);
    * everything else (continuation, unhinted tasks) resolves to the
      session THIS run's executor actually observed FIRST — so a
      fresh session can never be reported as anything else — and
      finally to the caller's capturer.

    ``hint_for`` exposes the raw hint so task records can carry the
    explicit ``parent_session_id``/``continuation_reason`` lineage.
    """

    is_resume_resolver = True

    def __init__(self, policy: Any, executor_log: Any, owner: Any) -> None:
        self.policy = policy
        self.executor_log = executor_log
        self.owner = owner

    def hint_for(self, task_id: str) -> dict[str, Any] | None:
        return self.policy.hint_for(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        hint = self.policy.hint_for(task_id)
        if isinstance(hint, dict) and hint.get("kind") == "reuse":
            return {
                "session_id": hint.get("session_id"),
                "source": SOURCE_KNOWN,
            }
        if self.executor_log is not None:
            getter = getattr(self.executor_log, "get", None)
            if callable(getter):
                record = getter(task_id)
                if isinstance(record, dict) and record.get("session_id"):
                    return {
                        "session_id": record.get("session_id"),
                        "source": record.get("source") or SOURCE_NONE,
                    }
        owner_record = self.policy.get(task_id)
        if isinstance(hint, dict) and hint.get("kind") == "continuation":
            # The recovered (stale) id is NEVER reported as the
            # task's session — only its lineage is preserved (task
            # records + SESSION_CONTINUATION event carry the parent).
            if (
                isinstance(owner_record, dict)
                and owner_record.get("session_id")
                == hint.get("parent_session_id")
            ):
                return None
        return owner_record


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
    resume: bool = False,
    timeline=None,
    commit_gate_writer=None,
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
    * ``max_plan_repairs`` bounds the plan-repair cycles (default 1);
    * ``resume=False`` (P5 behavior with no change), ``timeline=None``
      (no events), ``commit_gate_writer=None`` (default evidence
      writer into the run dir) — the three P6 seams documented in the
      module docstring.
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

    # -- P6 resume preamble: terminal check + timeline seam ------------------
    persisted_plan: dict[str, Any] | None = None
    if resume:
        if run_state is None:
            raise ValueError(
                "execute_plan(resume=True) requires a persisted run_state"
            )
        persisted_plan = run_state.load_plan()
        if (
            persisted_plan is not None
            and persisted_plan.get("status") == PLAN_ACCEPTED
        ):
            # Terminal persistence-only return: NO executor, no gate
            # commands, no supervisor, no writes — the accepted plan
            # stands (contract §2 terminal branch).
            if timeline is not None:
                timeline.emit(
                    "PLAN_ALREADY_ACCEPTED",
                    plan_id=plan.plan_id,
                    status=PLAN_ACCEPTED,
                )
            from app.orchestration.agy_resume import (
                plan_run_result_from_persisted,
            )

            return plan_run_result_from_persisted(persisted_plan)

    def emit(event_type: str, **fields: Any) -> None:
        """Timeline seam: fires ONLY when a Timeline is attached."""
        if timeline is None:
            return
        timeline.emit(event_type, plan_id=plan.plan_id, **fields)

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
        task_ref = record.get("worker_task_id") or record.get("task_id")
        captured = _session_record(resolver, task_ref) or {}
        record["session_id"] = record.get("session_id") or captured.get(
            "session_id"
        )
        # Captured source first, then a source the record already
        # carries (P6 boundary re-entries reload task files verbatim),
        # then "none" — every task record still has an explicit source.
        record["session_source"] = (
            captured.get("source")
            or record.get("session_source")
            or SOURCE_NONE
        )
        record.setdefault("task_id", record.get("worker_task_id"))
        # P6 session-continuation lineage: the task record carries the
        # parent session + the explicit reason (never silent continuity).
        hint_getter = getattr(resolver, "hint_for", None)
        if callable(hint_getter) and task_ref:
            hint = hint_getter(task_ref)
            if isinstance(hint, dict) and hint.get("kind") == "continuation":
                record["parent_session_id"] = hint.get("parent_session_id")
                record["continuation_reason"] = hint.get(
                    "continuation_reason"
                )
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
            if _append_event(result.failover_events, event):
                emit(
                    "MODEL_FAILOVER",
                    stage_id=stage_result.stage_id,
                    session_id=_scalar(event.get("session_id")) if isinstance(event, dict) else None,
                    role=_scalar(event.get("role")) if isinstance(event, dict) else None,
                    reason=_clip(
                        str(
                            (event or {}).get("fallback_reason")
                            if isinstance(event, dict)
                            else ""
                        ),
                        160,
                    ) or None,
                )
        for session_id in stage_result.session_ids:
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)
        for task_result in stage_result.task_results:
            task_id = (
                task_result.get("worker_task_id")
                or task_result.get("task_id")
            )
            captured = _session_record(resolver, task_id) or {}
            session_id = task_result.get("session_id") or captured.get(
                "session_id"
            )
            if task_id:
                result.session_sources[task_id] = str(
                    captured.get("source")
                    or task_result.get("session_source")
                    or SOURCE_NONE
                )
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)

    # -- P6 resume carry-forward + first-class gate evidence -----------------

    def carried_stage_result(stage_id: str) -> dict:
        """
        The persisted STAGE_ACCEPTED result — fail-closed: it must say
        ``STAGE_ACCEPTED`` (plan.json aggregate or the stage file).
        """
        entry = ((persisted_plan or {}).get("stage_results") or {}).get(
            stage_id
        )
        if isinstance(entry, dict) and entry.get("status") == STAGE_ACCEPTED:
            return dict(entry)
        view = stage_state_for(stage_id)
        if view is None:
            raise ValueError(
                f"cannot carry stage {stage_id!r}: run_state is absent"
            )
        loaded = view.load_stage()
        if loaded.get("status") != STAGE_ACCEPTED:
            raise ValueError(
                f"cannot carry stage {stage_id!r}: persisted verdict is "
                f"{loaded.get('status')!r}, not STAGE_ACCEPTED"
            )
        return loaded

    def carry_forward(stage_id: str, carried: dict) -> None:
        """
        Carry an accepted stage INTO the resumed result without
        touching the executor: result entry verbatim + aggregates
        (failover events, session ids, per-task sources).
        """
        entry = copy.deepcopy(dict(carried))
        result.stage_results[stage_id] = entry
        for event in entry.get("failover_events") or []:
            _append_event(result.failover_events, event)
        for session_id in entry.get("session_ids") or []:
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)
        for task_result in entry.get("task_results") or []:
            task_id = (
                task_result.get("worker_task_id")
                or task_result.get("task_id")
            )
            source = task_result.get("session_source")
            if task_id and source and task_id not in result.session_sources:
                result.session_sources[task_id] = str(source)
            session_id = task_result.get("session_id")
            if session_id and session_id not in result.session_ids:
                result.session_ids.append(session_id)

    def write_stage_gate_record(stage, stage_result, resolver) -> None:
        """Commit-gate evidence at the stage-commit gate point (§3)."""
        if not stage_result.commit:
            return  # only committed stages have immutable evidence
        if commit_gate_writer is None and run_state is None:
            return
        from app.orchestration.agy_commitgate import (
            build_commit_gate_record,
            persist_commit_gate_record,
        )

        data = stage_result.to_dict()
        data["task_results"] = [
            task_record(task_result, resolver)
            for task_result in stage_result.task_results
        ]
        record = build_commit_gate_record(plan, stage, data)
        if commit_gate_writer is not None:
            commit_gate_writer(record)
        elif run_state is not None:
            persist_commit_gate_record(run_state.plan_dir, record)

    def _with_stage_task_files(data: dict) -> dict:
        """Plan-record input: task results from the persisted files."""
        if run_state is None:
            return data
        out = dict(data)
        rebuilt: dict[str, Any] = {}
        for stage_id, entry in (out.get("stage_results") or {}).items():
            entry = dict(entry)
            try:
                file_tasks = StageRunState(
                    run_state.root, plan.plan_id, stage_id
                ).load_tasks()
            except (OSError, ValueError):
                file_tasks = []
            if file_tasks:
                entry["task_results"] = file_tasks
            rebuilt[stage_id] = entry
        out["stage_results"] = rebuilt
        return out

    def write_plan_gate_record(final_entry, cumulative_entry) -> None:
        """Plan-level CommitGateRecord -> commits/plan-gate.json."""
        if commit_gate_writer is None and run_state is None:
            return
        from app.orchestration.agy_commitgate import (
            build_commit_gate_record,
            persist_commit_gate_record,
        )

        data = result.to_dict()
        data["final_acceptance"] = final_entry
        data["cumulative"] = cumulative_entry
        data = _with_stage_task_files(data)
        record = build_commit_gate_record(plan, None, data, plan_level=True)
        if commit_gate_writer is not None:
            commit_gate_writer(record)
        elif run_state is not None:
            persist_commit_gate_record(run_state.plan_dir, record)

    def record_stage_outcome(stage, stage_result, resolver) -> None:
        """P6: stage gate events + commit-gate record (executed stages)."""
        commit_sha = (stage_result.commit or {}).get("commit_sha")
        if stage_result.commit:
            emit(
                "STAGE_COMMITTED",
                stage_id=stage.stage_id,
                sha=_scalar(commit_sha),
            )
        if stage_result.status == STAGE_ACCEPTED:
            emit(
                "STAGE_ACCEPTED",
                stage_id=stage.stage_id,
                status=STAGE_ACCEPTED,
                sha=_scalar(commit_sha),
            )
        write_stage_gate_record(stage, stage_result, resolver)

    # -- one stage through the P3 path --------------------------------------

    async def run_one_stage(stage: PlanStage):
        # P6: the stage run starts here (timeline-attached runs only).
        emit("STAGE_STARTED", stage_id=stage.stage_id)

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

        executor_log = getattr(execute_task, "session_log", None)
        is_policy = (
            session_capturer is not None
            and getattr(session_capturer, "is_resume_session_policy", False)
        )
        if is_policy:
            resolver = _ResumeResolver(
                session_capturer, executor_log, session_capturer
            )
        elif session_capturer is not None:
            resolver = session_capturer
        else:
            resolver = executor_log
        bridge = _SessionBridge(resolver)

        # P6 per-task events at the INJECTED seams: dispatch/verify
        # ARE these callables, so a run killed mid-stage still shows
        # DISPATCHED without COMPLETED — the stream never lies.
        if timeline is not None:
            original_execute = execute_task
            original_validate = validate_task

            async def traced_execute_task(task, attempt, repair_context):
                emit(
                    "TASK_DISPATCHED",
                    stage_id=stage.stage_id,
                    task_id=task.id,
                    attempt=int(attempt),
                )
                output = await original_execute(
                    task, attempt, repair_context
                )
                emit(
                    "TASK_COMPLETED",
                    stage_id=stage.stage_id,
                    task_id=task.id,
                )
                return output

            async def traced_validate_task(task):
                ok, output = await original_validate(task)
                emit(
                    "TASK_VERIFIED",
                    stage_id=stage.stage_id,
                    task_id=task.id,
                    status="ok" if ok else "failed",
                )
                return ok, output

            execute_task = traced_execute_task
            validate_task = traced_validate_task

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

        # P6: gate events + the stage-commit CommitGateRecord.
        record_stage_outcome(stage, stage_result, resolver)

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
            if _append_event(result.failover_events, event):
                emit(
                    "MODEL_FAILOVER",
                    stage_id="plan",
                    session_id=_scalar(
                        event.get("session_id")
                        if isinstance(event, dict)
                        else None
                    ),
                    role=_scalar(
                        event.get("role")
                        if isinstance(event, dict)
                        else None
                    ),
                    reason=_clip(
                        str(
                            (event or {}).get("fallback_reason")
                            if isinstance(event, dict)
                            else ""
                        ),
                        160,
                    ) or None,
                )
        persist_plan()

        # P6: the plan-level CommitGateRecord at the plan gate point
        # (the CONSTRAINED decision is the recorded input).
        write_plan_gate_record(final_entry, cumulative_entry)

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

            # P6: one event per actually-entered repair cycle.
            emit(
                "PLAN_REPAIR_REQUESTED",
                stage_id=repair_id,
                reason=_clip(str(repair_context.reason), 200),
            )

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

    # -- P6 resume seeding: re-ENTER the run, never restart it ---------------
    if resume and persisted_plan:
        # Reconstruct the aggregates persisted so far: failover
        # events, bounded repair lineage (so max_plan_repairs keeps
        # counting across the restart), session ids/sources and plan
        # gate reviews. Accepted stage results are carried verbatim;
        # non-accepted stage entries are simply overwritten when the
        # stage re-runs.
        result.created = persisted_plan.get("created") or result.created
        result.failover_events = copy.deepcopy(
            list(persisted_plan.get("failover_events") or [])
        )
        result.repair_lineage = list(
            persisted_plan.get("repair_lineage") or []
        )
        result.session_ids = list(persisted_plan.get("session_ids") or [])
        result.session_sources = dict(
            persisted_plan.get("session_sources") or {}
        )
        result.plan_gate_reviews = copy.deepcopy(
            list(persisted_plan.get("plan_gate_reviews") or [])
        )
        result.stage_results = copy.deepcopy(
            dict(persisted_plan.get("stage_results") or {})
        )
        # stopped_at stays unset: this run is actively continuing.

    # -- 2: PLAN_IN_PROGRESS header BEFORE execution -------------------------
    # P6: persist the ORIGINAL plan document at plan start (the exact
    # plan a fresh process can reload from plan_doc.json).
    if run_state is not None:
        plan_doc_saver = getattr(run_state, "save_plan_doc", None)
        if callable(plan_doc_saver):
            plan_doc_saver(plan)
    if not resume:
        # A fresh run starts the timeline span; a resumed run already
        # carries RUN_INTERRUPTED/RUN_RESUMED entries from before.
        emit("PLAN_STARTED", status=PLAN_IN_PROGRESS)

    persist_plan()

    # -- 3: per-stage loop ----------------------------------------------------

    if not order:
        result.status = PLAN_INCOMPLETE  # defensive: nothing to run
        persist_plan()
        return result

    for stage in order:
        boundary: str | None = None
        if resume:
            # P6: consult the persisted boundary before doing ANY work
            # — an accepted stage must not even build an executor.
            from app.orchestration.agy_resume import (
                STAGE_COMMITTED as RECOVERY_STAGE_COMMITTED,
                TASK_VERIFIED as RECOVERY_TASK_VERIFIED,
                recovery_state,
            )

            state = recovery_state(stage.stage_id, run_state)
            if state == STAGE_ACCEPTED:
                carry_forward(
                    stage.stage_id, carried_stage_result(stage.stage_id)
                )
                # NO executor, NO dispatch, NO commit, NO supervisor.
                continue
            if state in (RECOVERY_STAGE_COMMITTED, RECOVERY_TASK_VERIFIED):
                boundary = state

        if boundary is not None:
            # Boundary re-entry: gates/supervisor ONLY (agy_resume).
            from app.orchestration.agy_resume import (
                run_stage_boundary_gate,
            )

            stage_result, resolver = await run_stage_boundary_gate(
                plan,
                stage,
                boundary,
                settings=settings,
                run_state=run_state,
                run_command=run_command,
                cumulative_suite=cumulative_commands,
                make_commit=make_commit,
                supervisor_review=supervisor_review,
                on_failover_event=on_failover_event,
            )
            aggregate(stage_result, resolver)
            view = stage_state_for(stage.stage_id)
            if view is not None:
                view.save_stage(stage_result)
                if stage_result.commit:
                    view.save_commit(stage_result.commit)
            result.stage_results[stage.stage_id] = stage_result.to_dict()
            record_stage_outcome(stage, stage_result, resolver)
            if on_stage is not None:
                await _maybe_await(on_stage(stage.stage_id, stage_result))
        else:
            stage_result, resolver = await run_one_stage(stage)

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

    # -- P6 plan-end observability --------------------------------------------
    terminal_state = result.status
    last_commit_sha = None
    if result.stage_order:
        last_entry = result.stage_results.get(
            result.stage_order[-1]
        ) or {}
        last_commit_sha = (last_entry.get("commit") or {}).get("commit_sha")
    if terminal_state == PLAN_ACCEPTED:
        emit(
            "PLAN_ACCEPTED",
            status=PLAN_ACCEPTED,
            sha=_scalar(last_commit_sha),
        )
    elif terminal_state == PLAN_REJECTED:
        emit("PLAN_REJECTED", status=PLAN_REJECTED)
    if timeline is not None:
        from app.orchestration.agy_timeline import write_observability
        from pathlib import Path as _Path

        obs_dir: Any = None
        if run_state is not None:
            obs_dir = run_state.plan_dir
        elif getattr(timeline, "path", None) is not None:
            obs_dir = _Path(timeline.path).parent
        if obs_dir is not None:
            write_observability(obs_dir, timeline)

    return result