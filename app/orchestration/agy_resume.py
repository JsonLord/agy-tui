"""P6 durable resume/recovery: recovery states + the resume entrypoint.

A killed controller must be able to restart from DISK ALONE — the
persisted run state (``stage.json`` / ``tasks/*.json`` /
``commit.json`` / ``plan.json`` / ``plan_doc.json``) is the only
source of truth. Two rules govern this module:

* ARTIFACT-CONSERVATIVE BOUNDARIES: :func:`recovery_state` is a PURE
  derivation from persisted artifacts and always reports the
  last DETERMINISTICALLY-PROVEN boundary — never an optimistic one.
  The mapping, verified against the real P5 C run-state
  (``scratch/agy-p5-c-larger/state/runs/agp-p5-c-larger``):

  ====================================  ======================  ==========================================
  persisted shape                       recovery state          resume action
  ====================================  ======================  ==========================================
  no ``plan.json``                      PLAN_PENDING            run everything (nothing proven yet)
  ``plan.json`` ``PLAN_ACCEPTED``       PLAN_ACCEPTED           terminal — idempotent, zero model calls
  ``plan.json`` ``PLAN_REJECTED``       PLAN_REJECTED           terminal — return the persisted result
  ``plan.json`` ``PLAN_REPAIR``         PLAN_REPAIR             re-enter the BOUNDED repair path
  ``plan.json`` ``PLAN_IN_PROGRESS``    PLAN_RUNNING            continue from the per-stage boundaries
  ``plan.json`` ``PLAN_FAILED`` /         PLAN_RUNNING          non-terminal — re-enter from stage
  ``PLAN_INCOMPLETE``                                                             boundaries, never restart
  no stage dir                          PLAN_PENDING            run the stage normally (nothing proven)
  stage dir, no task/stage/commit yet   STAGE_RUNNING           run the stage normally (P3 re-verifies)
  tasks present, verification           TASK_RUNNING            run the stage normally — verification
  missing/incomplete                                               ALWAYS reruns on this boundary
  tasks with full verification,         TASK_VERIFIED           rerun stage acceptance + cumulative gate +
  no stage verdict                                                supervisor + the ONE stage commit — WITHOUT
                                                                  re-dispatching tasks (no executor call)
  ``commit.json`` present, supervisor   STAGE_COMMITTED         rerun ONLY the supervisor gate against
  decision missing                     (incl. ``stage.json``       persisted evidence — no executor, no
  / ``stage.json`` absent-but-            absent shape)          ``run_command``, no new commit
  committed
  ``stage.json`` ``STAGE_ACCEPTED``     STAGE_ACCEPTED          NEVER re-run implementation/executor/commit
  ``stage.json`` ``STAGE_REPAIR`` /       PLAN_REPAIR            bounded plan repair context
  plan repair lineage present
  ``stage.json`` ``STAGE_FAILED`` /       STAGE_RUNNING         verdict exists but nothing durable was
  ``STAGE_REJECTED``                                            accepted — resume runs the stage normally
                                                                  (P3 verification never weakens)
  ====================================  ======================  ==========================================

  ``STAGE_FAILED``/``STAGE_REJECTED`` are non-accepted,
  non-committed verdicts: at plan level P4 already stopped with
  ``PLAN_FAILED``/``PLAN_REJECTED`` (checked FIRST above); at stage
  level nothing durable was accepted, so the stage resumes from its
  evidenced task boundary with every gate re-checked.

* :func:`resume_plan` — the resume entrypoint: loads
  :class:`PlanRecovery`, returns the persisted
  ``PLAN_ACCEPTED``/``PLAN_REJECTED`` run idempotently (no model
  calls, no new commits; ``PLAN_ALREADY_ACCEPTED`` emitted for the
  accepted case), performs session recovery for incomplete stages and
  then calls P4 ``execute_plan`` with ``resume=True`` so accepted
  stages are SKIPPED entirely (their executor is never even built).

Session recovery (per incomplete stage with persisted sessions):
the session must still exist on the server AND belong to the same
directory/agent family — checked with the INJECTABLE
``session_reusable(session_id)`` callable (default: a read of
``GET /api/session``, built with the live run's repository directory;
tests inject fakes so they never touch the network).

* reusable -> the persisted id is passed as the KNOWN session (task
  records show ``session_source == "known"``);
* not reusable / absent -> NO silent continuity: an explicit
  ``SESSION_CONTINUATION`` timeline event plus
  ``parent_session_id`` + ``continuation_reason`` in the resumed task
  records, and execution proceeds with a fresh session.

The full-state aggregation contract lives in
:func:`plan_recovery`; ``execute_plan``'s P6 resume/slips support is
in ``agy_plan``.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from app.orchestration.agy_execute import (
    STAGE_ACCEPTED,
    STAGE_FAILED,
    STAGE_REJECTED,
    STAGE_REPAIR,
    StageRunResult,
    _as_command,
    _invoke_review,
    _normalize_review,
    _task_result_projection,
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
from app.orchestration.agy_runstate import StageRunState
from app.orchestration.agy_session import SOURCE_NONE
from app.orchestration.decompose import run_stage_acceptance
from app.schemas import DevelopmentPlan, PlanStage

# ---------------------------------------------------------------------------
# The 10 recovery states
# ---------------------------------------------------------------------------

RECOVERY_STATES = (
    "PLAN_PENDING",
    "PLAN_RUNNING",
    "STAGE_RUNNING",
    "TASK_RUNNING",
    "TASK_VERIFIED",
    "STAGE_COMMITTED",
    "STAGE_ACCEPTED",
    "PLAN_REPAIR",
    "PLAN_ACCEPTED",
    "PLAN_REJECTED",
)

PLAN_PENDING = "PLAN_PENDING"
PLAN_RUNNING = "PLAN_RUNNING"
STAGE_RUNNING = "STAGE_RUNNING"
TASK_RUNNING = "TASK_RUNNING"
TASK_VERIFIED = "TASK_VERIFIED"
STAGE_COMMITTED = "STAGE_COMMITTED"
# STAGE_ACCEPTED imported from agy_execute (same value).

TERMINAL_PLAN_STATES = (PLAN_ACCEPTED, PLAN_REJECTED)

DEFAULT_SESSION_SERVER = "http://127.0.0.1:4096"
DISPATCH_AGENT_PREFIX = "agy-"


def _stage_view(run_state: Any, stage_id: str) -> StageRunState:
    """A stage-addressing view over any plan/stage run-state object."""
    if getattr(run_state, "stage_id", None) == stage_id:
        return run_state  # type: ignore[return-value]
    return StageRunState(run_state.root, run_state.plan_id, stage_id)


def _verification_complete(record: dict[str, Any]) -> bool:
    """
    Is one persisted task record complete verification evidence?
    Complete = boolean ``validation_ok`` + a ``validation_output``
    entry + a ``status``. Anything less is the
    ``TASK_RUNNING`` case ("model said done but verification not
    persisted").
    """
    return (
        isinstance(record.get("validation_ok"), bool)
        and "validation_output" in record
        and isinstance(record.get("status"), str)
        and bool(record.get("status"))
    )


# ---------------------------------------------------------------------------
# Pure derivation
# ---------------------------------------------------------------------------


def recovery_state(stage_id: str | None, run_state: Any) -> str:
    """
    DERIVE (never re-derive from the model) the recovery state of
    ``stage_id`` (or the plan itself when ``stage_id is None``) from
    persisted artifacts only — a pure function of disk. The mapping
    table lives in the module docstring; branch order matters:
    terminal plan verdicts win, then stage verdicts, then the durable
    commit / task-verification boundaries, conservative last.
    """
    plan_payload = run_state.load_plan() if hasattr(
        run_state, "load_plan"
    ) else None
    plan_status = (plan_payload or {}).get("status")

    # 1/2: terminal plan verdicts are terminal for every stage too.
    if plan_status == PLAN_ACCEPTED:
        return PLAN_ACCEPTED
    if plan_status == PLAN_REJECTED:
        return PLAN_REJECTED

    # 3: plan-level query.
    if stage_id is None:
        if plan_status == PLAN_REPAIR:
            return PLAN_REPAIR
        if plan_status in (PLAN_IN_PROGRESS, PLAN_FAILED,
                           PLAN_INCOMPLETE, None):
            # None = no plan.json yet (fresh state); FAILED and
            # INCOMPLETE are NON-terminal: work remains and resumes
            # from the per-stage boundaries.
            return PLAN_PENDING if plan_status is None else PLAN_RUNNING
        return PLAN_RUNNING  # unrecognized -> fail toward re-entry

    view = _stage_view(run_state, stage_id)
    stage_dir = view.stage_dir

    # 4: nothing has touched this stage.
    if stage_dir is None or not stage_dir.exists():
        return PLAN_PENDING

    stage_payload: dict[str, Any] | None = None
    stage_json_path = stage_dir / "stage.json"
    if stage_json_path.exists():
        stage_payload = json.loads(stage_json_path.read_text("utf-8"))

    # 5: persisted stage verdict.
    if stage_payload is not None:
        status = stage_payload.get("status")
        if status == STAGE_ACCEPTED:
            # NEVER re-run implementation for an accepted stage.
            return STAGE_ACCEPTED
        if status == STAGE_REPAIR:
            return PLAN_REPAIR  # repair lineage context
        if status in (STAGE_FAILED, STAGE_REJECTED):
            # Durable verdict, but nothing was accepted/committed:
            # resume runs the stage normally (all gates re-check).
            return STAGE_RUNNING

    # 6: committed but the supervisor gate never (durably) decided.
    commit = view.load_commit()
    decision = (
        stage_payload.get("supervisor_decision")
        if stage_payload is not None
        else None
    )
    if commit is not None and not decision:
        return STAGE_COMMITTED

    # 7: task-level boundaries.
    tasks = view.load_tasks()
    if tasks:
        if all(_verification_complete(record) for record in tasks):
            return TASK_VERIFIED
        return TASK_RUNNING

    # 8: stage directory exists but nothing is persisted yet.
    if stage_payload is None:
        return STAGE_RUNNING

    # stage.json exists without a recognized verdict and without
    # commit/tasks: nothing durable -> run the stage normally.
    return STAGE_RUNNING


@dataclass
class PlanRecovery:
    """Aggregated recovery view built ONLY from persisted artifacts."""

    plan_id: str
    plan_state: str
    stage_states: dict[str, str]
    resume_at: str | None
    accepted_stages: list[str]
    accepted_results: dict[str, dict] = field(default_factory=dict)
    stage_order: list[str] = field(default_factory=list)
    failover_events: list[dict] = field(default_factory=list)
    repair_lineage: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    session_sources: dict[str, str] = field(default_factory=dict)
    persisted_plan: dict | None = None

    @property
    def terminal(self) -> bool:
        return self.plan_state in TERMINAL_PLAN_STATES


def plan_recovery(run_state: Any, plan: DevelopmentPlan) -> PlanRecovery:
    """
    Aggregate per-stage :func:`recovery_state` + the plan-level state
    + ``resume_at`` (the FIRST non-accepted stage in
    ``stage_execution_order``) + the accepted stage results to carry
    forward (commit SHAs, task/session ids, decisions, failover
    events, repair lineage) — all RECONSTRUCTED from disk, never
    re-derived from the model.
    """
    order = plan.stage_execution_order()
    stage_ids = [stage.stage_id for stage in order]

    plan_state = recovery_state(None, run_state)
    stage_states = {
        stage_id: recovery_state(stage_id, run_state)
        for stage_id in stage_ids
    }
    accepted_stages = [
        stage_id for stage_id in stage_ids
        if stage_states[stage_id] == STAGE_ACCEPTED
    ]

    accepted_results: dict[str, dict] = {}
    for stage_id in accepted_stages:
        view = _stage_view(run_state, stage_id)
        loaded = view.load_stage()
        accepted_results[stage_id] = loaded

    resume_at: str | None = None
    if plan_state not in TERMINAL_PLAN_STATES:
        for stage_id in stage_ids:
            if stage_states[stage_id] != STAGE_ACCEPTED:
                resume_at = stage_id
                break

    persisted = run_state.load_plan() if hasattr(run_state, "load_plan") \
        else None
    persisted = persisted if isinstance(persisted, dict) else None

    return PlanRecovery(
        plan_id=plan.plan_id,
        plan_state=plan_state,
        stage_states=stage_states,
        resume_at=resume_at,
        accepted_stages=accepted_stages,
        accepted_results=accepted_results,
        stage_order=list(stage_ids),
        failover_events=list((persisted or {}).get("failover_events") or []),
        repair_lineage=list((persisted or {}).get("repair_lineage") or []),
        session_ids=list((persisted or {}).get("session_ids") or []),
        session_sources=dict(
            (persisted or {}).get("session_sources") or {}
        ),
        persisted_plan=persisted,
    )


def plan_run_result_from_persisted(
    persisted: dict[str, Any],
) -> PlanRunResult:
    """Rebuild the persisted ``PlanRunResult`` verbatim (idempotent)."""
    result = PlanRunResult(
        plan_id=str(persisted["plan_id"]),
        objective=str(persisted.get("objective") or ""),
        status=str(persisted.get("status") or ""),
        stage_order=list(persisted.get("stage_order") or []),
        stage_results=dict(persisted.get("stage_results") or {}),
    )
    for key in (
        "cumulative_invariants_ok",
        "final_acceptance",
        "plan_gate",
        "repair_lineage",
        "failover_events",
        "session_ids",
        "session_sources",
        "stopped_at",
        "plan_gate_reviews",
        "created",
        "updated",
    ):
        if key in persisted:
            setattr(result, key, persisted[key])
    return result


def load_plan_document(run_state: Any) -> DevelopmentPlan:
    """
    Load the EXACT plan that ran from ``plan_doc.json`` — fresh
    resume processes must never re-type the plan.
    """
    document = run_state.load_plan_doc()
    if not isinstance(document, dict):
        raise ValueError(
            "plan_doc.json missing (or not an object) under "
            f"{getattr(run_state, 'plan_dir', run_state)}; a resume "
            "process must load the original plan document"
        )
    return DevelopmentPlan(**document)


# ---------------------------------------------------------------------------
# Session recovery (injectable reusability check)
# ---------------------------------------------------------------------------


def make_server_session_reusable(
    *,
    server: str = DEFAULT_SESSION_SERVER,
    directory: str | None = None,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
) -> Callable[[str], bool]:
    """
    Default ``session_reusable`` check for live runs: the session
    must still EXIST on the server (``GET /api/session`` row with
    exactly this id) and belong to the same directory and the
    ``agy-*`` dispatch agent family. Any failure -> ``False``; this
    callable never raises.
    """

    def _open(url: str, timeout_: float) -> Any:
        with urllib.request.urlopen(url, timeout=timeout_) as response:
            return json.loads(response.read().decode("utf-8"))

    open_url = opener if opener is not None else _open
    wanted_directory = directory.rstrip("/") if directory else None

    def check(session_id: str) -> bool:
        if not session_id:
            return False
        for url in (
            f"{server}/api/session?limit=5000",
            f"{server}/session?limit=5000",
        ):
            try:
                payload = open_url(url, timeout)
            except Exception:  # noqa: BLE001 - never raise into resume
                continue
            rows = payload.get("data") if isinstance(payload, dict) \
                else payload
            if isinstance(payload, list):
                rows = payload
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or row.get("id") != session_id:
                    continue
                agent = str(row.get("agent") or "")
                if not agent.startswith(DISPATCH_AGENT_PREFIX):
                    return False
                location = row.get("location")
                row_dir = (
                    location.get("directory") if isinstance(location, dict)
                    else (location or row.get("directory"))
                )
                row_dir = str(row_dir).rstrip("/") if row_dir else None
                if (
                    wanted_directory is not None
                    and row_dir is not None
                    and row_dir != wanted_directory
                ):
                    return False
                return True
        return False

    return check


def build_session_hints(
    run_state: Any,
    recovery: PlanRecovery,
    plan: DevelopmentPlan,
    *,
    session_reusable: Callable[[str], bool],
    on_continuation: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    For every NOT-yet-accepted stage that has a persisted session
    id, decide reuse vs continuation. Reusable -> ``kind="reuse"``
    (the persisted id becomes the KNOWN session); otherwise
    ``kind="continuation"`` with the explicit parent lineage and a
    ``continuation_reason`` — never silent continuity. The check is
    injectable so tests never hit the network.
    """
    hints: dict[str, dict[str, Any]] = {}
    for stage in plan.stage_execution_order():
        state = recovery.stage_states.get(stage.stage_id, PLAN_PENDING)
        if state == STAGE_ACCEPTED:
            continue
        view = _stage_view(run_state, stage.stage_id)
        try:
            records = view.load_tasks()
        except OSError:
            records = []
        persisted_session = next(
            (
                str(record.get("session_id"))
                for record in sorted(
                    records,
                    key=lambda r: str(r.get("task_id") or ""),
                )
                if record.get("session_id")
            ),
            None,
        )
        if not persisted_session:
            continue  # stage never dispatched -> fresh session later

        try:
            reusable = bool(session_reusable(persisted_session))
        except Exception:  # noqa: BLE001 - a failed check means "absent"
            reusable = False

        if reusable:
            hints[stage.stage_id] = {
                "kind": "reuse",
                "stage_id": stage.stage_id,
                "session_id": persisted_session,
            }
            continue

        reason = (
            "persisted session no longer reusable at resume: absent "
            "from the server or outside the same directory/agent "
            "family; continuing with a fresh session"
        )
        hints[stage.stage_id] = {
            "kind": "continuation",
            "stage_id": stage.stage_id,
            "parent_session_id": persisted_session,
            "continuation_reason": reason,
        }
        if on_continuation is not None:
            on_continuation(
                {
                    "stage_id": stage.stage_id,
                    "session_id": persisted_session,
                    "parent_session_id": persisted_session,
                    "continuation_reason": reason,
                }
            )
    return hints


class ResumeSessionPolicy:
    """
    The ``session_capturer``-shaped policy ``resume_plan`` injects
    into ``execute_plan`` when session hints exist.

    ``agy_plan`` checks ``is_resume_session_policy`` and composes:
    a REUSE hint resolves to ``{session_id, source: "known"}`` for
    the stage's dispatched tasks; anything else falls back to the
    executor's own session log first (the session THIS run really
    observed), then to the caller's capturer. ``hint_for(task_id)``
    exposes the raw hint so task records can carry the explicit
    ``SESSION_CONTINUATION`` lineage.
    """

    is_resume_session_policy = True

    def __init__(
        self,
        stage_hints: dict[str, dict[str, Any]],
        task_hints: dict[str, dict[str, Any]],
        owner: Any = None,
    ) -> None:
        self.stage_hints = stage_hints
        self.task_hints = task_hints
        self.owner = owner

    def hint_for(self, task_id: str) -> dict[str, Any] | None:
        return self.task_hints.get(task_id)

    def get(self, task_id: str) -> dict[str, Any] | None:
        if self.owner is None:
            return None
        if isinstance(self.owner, dict):
            raw = self.owner.get(task_id)
        else:
            getter = getattr(self.owner, "get", None)
            if callable(getter):
                raw = getter(task_id)
            elif callable(self.owner):
                raw = self.owner(task_id)
            else:
                raw = None
        if isinstance(raw, dict):
            return {
                "session_id": raw.get("session_id"),
                "source": raw.get("source") or SOURCE_NONE,
            }
        if isinstance(raw, (tuple, list)) and 1 <= len(raw) <= 2:
            return {
                "session_id": raw[0],
                "source": (raw[1] if len(raw) == 2 else SOURCE_NONE)
                or SOURCE_NONE,
            }
        return None


# ---------------------------------------------------------------------------
# Boundary re-entry gates (coordinates of the recovery_model_state)
# ---------------------------------------------------------------------------


def _stage_gate_failures(
    violations: list[str],
    acceptance: dict[str, Any],
    cumulative: dict[str, Any],
) -> list[str]:
    """execute_stage.deterministic_failures_for, replayed (P3 rules)."""
    failures = [
        f"scope violation: {path} is not in stage.allowed_files"
        for path in violations
    ]
    for check in acceptance.get("checks", []) or []:
        if not check.get("ok", False):
            failures.append(
                "stage acceptance failed: "
                f"{check.get('command')} exited {check.get('exit_code')}, "
                f"expected {check.get('expected_exit_code')}"
            )
    for check in cumulative.get("checks", []) or []:
        if not check.get("ok", False):
            failures.append(
                f"cumulative gate failed: {check.get('command')} "
                f"exited {check.get('exit_code')}"
            )
    return failures


async def run_stage_boundary_gate(
    plan: DevelopmentPlan,
    stage: PlanStage,
    boundary: str,
    *,
    settings: Any,
    run_state: Any,
    run_command: Callable,
    cumulative_suite: list,
    make_commit: Callable | None,
    supervisor_review: Callable | None,
    on_failover_event: Callable[[dict], None] | None = None,
) -> tuple[StageRunResult, None]:
    """
    Re-enter ONE incomplete stage AT its persisted boundary — the
    executor (dispatch) is NEVER part of this path:

    * ``STAGE_COMMITTED``: rerun ONLY the supervisor gate against the
      persisted acceptance/cumulative/commit evidence. No
      ``run_command``, no dispatch, no new commit. (When the kill
      happened between the commit and ``stage.json`` — the realistic
      shape: ``commit.json`` exists, ``stage.json`` does not — P3's
      commit rule guarantees the gates were green at commit time, so
      the gate evidence is DERIVED from the commit and marked as
      such: ``derived_from_commit: true``, never invented check
      output.)
    * ``TASK_VERIFIED``: re-run the stage acceptance + cumulative
      gates through ``run_command`` (verification always reruns on an
      incomplete boundary), then — green — the ONE stage commit, then
      the supervisor gate. Tasks are NOT re-dispatched: their
      persisted results (``tasks/*.json``) are the evidence.

    Returns ``(StageRunResult, None)``; like P3/P4 it never raises
    for repository/test/model outcomes.
    """
    _ = settings
    view = _stage_view(run_state, stage.stage_id)
    if view.stage_dir is None:
        raise ValueError(
            f"boundary gate requires a stage directory for "
            f"{stage.stage_id!r}"
        )
    stage_json_path = view.stage_dir / "stage.json"
    carried = (
        json.loads(stage_json_path.read_text("utf-8"))
        if stage_json_path.exists()
        else None
    )
    commit = view.load_commit()
    task_records = view.load_tasks()

    if carried is None and commit is None and not task_records:
        raise ValueError(
            f"boundary gate requested for stage {stage.stage_id!r} but "
            "nothing is persisted to resume from"
        )

    task_results = [dict(record) for record in task_records]
    for record in task_results:
        record.setdefault("worker_task_id", record.get("task_id"))

    # Verification evidence (persisted; never re-derived from a model)
    if carried is not None and isinstance(
        carried.get("task_verification"), dict
    ):
        verification = dict(carried["task_verification"])
    else:
        verification = {
            record.get("worker_task_id"): {
                "attempt": record.get("attempt"),
                "status": record.get("status"),
                "validation_ok": record.get("validation_ok"),
                "validation_output": record.get(
                    "validation_output", ""
                ),
                "changed_files": list(
                    record.get("changed_files", []) or []
                ),
                "session_id": record.get("session_id"),
                "failure_reason": record.get("failure_reason", ""),
            }
            for record in task_results
        }

    changed: list[str] = []
    seen: set[str] = set()
    source_changed = (
        carried.get("changed_files") if carried else None
    ) or [path for record in task_results for path in (
        record.get("changed_files", []) or []
    )]
    for path in source_changed:
        if path not in seen:
            seen.add(path)
            changed.append(str(path))

    allow_set = set(stage.allowed_files)
    violations = [path for path in changed if path not in allow_set]

    sessions = [
        str(record.get("session_id"))
        for record in task_results
        if record.get("session_id")
    ]

    gates_rerun = boundary == TASK_VERIFIED
    if gates_rerun:
        stage_acceptance = await run_stage_acceptance(
            stage, run_command=run_command
        )
        cumulative_gate: dict[str, Any] = {"checks": []}
        for index, command in enumerate(cumulative_suite or [], start=1):
            command = _as_command(command)
            exit_code, output = await run_command(command)
            cumulative_gate["checks"].append(
                {
                    "name": command.description
                    or f"cumulative-{index:02d}",
                    "command": command.command,
                    "exit_code": exit_code,
                    "ok": exit_code == command.expected_exit_code,
                    "output": output,
                }
            )
        cumulative_gate["ok"] = all(
            check["ok"] for check in cumulative_gate["checks"]
        )
    elif carried is not None:
        stage_acceptance = dict(carried.get("stage_acceptance") or {})
        cumulative_gate = dict(carried.get("cumulative_gate") or {})
    else:
        # commit.json exists but stage.json does not (killed between
        # the commit and the stage verdict): P3 commits ONLY when
        # every gate was green, so the commit itself is the durable
        # proof. Explicitly marked — never presented as fresh checks.
        stage_acceptance = {
            "ok": True,
            "checks": [],
            "derived_from_commit": True,
        }
        cumulative_gate = {
            "ok": True,
            "checks": [],
            "derived_from_commit": True,
        }

    if not changed and not commit:
        # P3's zero-diff rule still applies at a verified boundary:
        # a fresh stage commit needs a real diff.
        failures = ["zero-diff with implementation required"]
        raw_failures = list(failures)
    else:
        raw_failures = _stage_gate_failures(
            violations, stage_acceptance, cumulative_gate
        )
        failures = list(raw_failures)

    evidence: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "stage_id": stage.stage_id,
        "stage_objective": stage.objective,
        "tasks": [dict(record) for record in task_results],
        "verification": verification,
        "changed_files": list(changed),
        "scope_violations": list(violations),
        "acceptance": stage_acceptance,
        "cumulative": cumulative_gate,
        "commit": copy_of(commit),
        "invariants": list(stage.cumulative_invariants),
        "deterministic_failures": list(failures),
    }

    # -- the ONE stage commit for a TASK_VERIFIED boundary --------------------
    new_commit: dict[str, Any] | None = None
    if gates_rerun and not failures and make_commit is not None:
        payload = dict(evidence)
        raw = make_commit(payload)
        import inspect as _inspect

        if _inspect.isawaitable(raw):
            raw = await raw
        record = dict(raw) if isinstance(raw, dict) else {}
        record.setdefault("plan_id", plan.plan_id)
        record.setdefault("stage_id", stage.stage_id)
        record.setdefault(
            "message", f"AGY stage {stage.stage_id}: {stage.objective}"
        )
        record.setdefault("changed_files", list(changed))
        record["task_results"] = _task_result_projection(task_results)
        record["verification"] = copy_of(verification)
        new_commit = record
        evidence["commit"] = copy_of(record)
    elif not gates_rerun:
        new_commit = commit  # STAGE_COMMITTED: reuse, never re-commit

    # -- supervisor gate ------------------------------------------------------
    events: list[dict[str, Any]] = []

    def record_event(event: dict[str, Any]) -> None:
        if event is None:
            return
        events.append(event)
        if on_failover_event is not None:
            on_failover_event(event)

    if supervisor_review is None:
        decision: dict[str, Any] | None = None
        if failures:
            status = STAGE_FAILED
        else:
            status = STAGE_ACCEPTED
    else:
        raw = await _invoke_review(supervisor_review, evidence, record_event)
        if isinstance(raw, dict) and raw.get("failover_event") is not None:
            record_event(raw["failover_event"])
        decision = _normalize_review(raw, list(failures))
        if decision["decision"] == "ACCEPT":
            status = STAGE_ACCEPTED  # impossible with failures (forced)
        elif decision["decision"] == "REJECT":
            status = STAGE_REJECTED
        else:
            # REPAIR: this boundary re-entry may NEVER dispatch a
            # repair task itself (no executor here) — stop bounded at
            # STAGE_REPAIR; the plan loop's bounded plan-repair cycle
            # owns any further dispatch.
            status = STAGE_REPAIR

    carried_lineage = (
        list(carried.get("repair_lineage") or []) if carried else []
    )
    failure_reason = "" if status == STAGE_ACCEPTED else "; ".join(failures)

    result = StageRunResult(
        plan_id=plan.plan_id,
        stage_id=stage.stage_id,
        status=status,
        task_results=[dict(record) for record in task_results],
        changed_files=list(changed),
        scope_violations=list(violations),
        task_verification=verification,
        stage_acceptance=stage_acceptance,
        cumulative_gate=cumulative_gate,
        commit=new_commit,
        supervisor_decision=decision,
        repair_lineage=carried_lineage,
        failover_events=events,
        session_ids=sessions,
        failure_reason=failure_reason,
        replan=None,
    )
    return result, None


def copy_of(value: Any) -> Any:
    """Deep-ish copy for JSON-shaped payloads."""
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# The resume entrypoint
# ---------------------------------------------------------------------------


async def resume_plan(
    plan: DevelopmentPlan,
    *,
    settings: Any,
    repo: Any,
    make_executor: Callable,
    run_command: Callable,
    cumulative_suite: Any,
    make_commit: Callable | None,
    supervisor_review: Callable | None,
    run_state: Any,
    session_capturer: Any = None,
    on_stage: Any = None,
    on_failover_event: Any = None,
    max_plan_repairs: int = 1,
    timeline: Any = None,
    commit_gate_writer: Callable | None = None,
    session_reusable: Callable[[str], bool] | None = None,
) -> PlanRunResult:
    """
    Resume entrypoint (P6). Same injected-callable surface as P4
    ``execute_plan`` plus the resume-specific seams:

    * ``run_state`` is REQUIRED — resume works from disk only;
    * ``timeline`` — attached event stream; ``RUN_RESUMED`` is
      emitted on start (process 1's ``RUN_INTERRUPTED`` was written
      by the driver itself; a plain SIGKILL runs no handler code);
    * ``session_reusable(session_id) -> bool`` — the injectable
      session-reusability check (default: live ``GET /api/session``
      read against ``repo``'s directory + the dispatch agent family).

    Flow: load :class:`PlanRecovery`; if the plan is terminal return
    the persisted ``PlanRunResult`` idempotently (accepted:
    ``PLAN_ALREADY_ACCEPTED`` emitted, ZERO executor/supervisor/
    ``run_command`` calls, no new commits, observability summary
    rewritten); otherwise resolve session hints and hand off to P4
    ``execute_plan(..., resume=True)`` which skips ``STAGE_ACCEPTED``
    stages entirely and re-enters incomplete stages at their
    recovered boundaries.
    """
    if run_state is None:
        raise ValueError("resume_plan requires the persisted run_state")

    def emit(event_type: str, **fields: Any) -> None:
        if timeline is not None:
            timeline.emit(event_type, plan_id=plan.plan_id, **fields)

    recovery = plan_recovery(run_state, plan)

    # -- terminal idempotence -------------------------------------------------
    if recovery.plan_state == PLAN_ACCEPTED:
        emit("PLAN_ALREADY_ACCEPTED", status=PLAN_ACCEPTED)
        assert recovery.persisted_plan is not None
        result = plan_run_result_from_persisted(recovery.persisted_plan)
        write_observability_for(timeline, run_state)
        return result
    if recovery.plan_state == PLAN_REJECTED:
        # Terminal failure verdict: same persistence-only return,
        # no model work (the rejection was already gated + recorded).
        assert recovery.persisted_plan is not None
        return plan_run_result_from_persisted(recovery.persisted_plan)

    # -- session recovery -----------------------------------------------------
    if session_reusable is None:
        session_reusable = make_server_session_reusable(
            directory=str(repo) if repo is not None else None,
        )

    continuations: list[dict[str, Any]] = []

    def on_continuation(item: dict[str, Any]) -> None:
        continuations.append(item)
        emit(
            "SESSION_CONTINUATION",
            stage_id=str(item.get("stage_id")),
            session_id=str(item.get("session_id")),
            parent_session_id=str(item.get("parent_session_id")),
            continuation_reason=str(item.get("continuation_reason")),
        )

    hints = build_session_hints(
        run_state,
        recovery,
        plan,
        session_reusable=session_reusable,
        on_continuation=on_continuation,
    )

    task_hints: dict[str, dict[str, Any]] = {}
    for stage in plan.stage_execution_order():
        hint = hints.get(stage.stage_id)
        if hint is None:
            continue
        view = _stage_view(run_state, stage.stage_id)
        for record in view.load_tasks():
            task_id = record.get("task_id") or record.get("worker_task_id")
            if task_id:
                task_hints[str(task_id)] = hint

    effective_capturer = session_capturer
    if hints:
        effective_capturer = ResumeSessionPolicy(
            hints, task_hints, owner=session_capturer
        )

    # -- RUN_RESUMED (process 2's start marker) -------------------------------
    emit(
        "RUN_RESUMED",
        stage_id=recovery.resume_at,
        status=recovery.plan_state,
    )

    return await execute_plan(
        plan,
        settings=settings,
        repo=repo,
        make_executor=make_executor,
        run_command=run_command,
        cumulative_suite=cumulative_suite,
        make_commit=make_commit,
        supervisor_review=supervisor_review,
        run_state=run_state,
        session_capturer=effective_capturer,
        on_stage=on_stage,
        on_failover_event=on_failover_event,
        max_plan_repairs=max_plan_repairs,
        resume=True,
        timeline=timeline,
        commit_gate_writer=commit_gate_writer,
    )


def write_observability_for(timeline: Any, run_state: Any) -> None:
    """Refresh ``observability.json`` on an idempotent resume return."""
    if timeline is None:
        return
    try:
        from app.orchestration.agy_timeline import write_observability

        write_observability(run_state.plan_dir, timeline)
    except Exception:  # noqa: BLE001 - summary must never block resume
        return


__all__ = [
    "RECOVERY_STATES",
    "PLAN_PENDING",
    "PLAN_RUNNING",
    "STAGE_RUNNING",
    "TASK_RUNNING",
    "TASK_VERIFIED",
    "STAGE_COMMITTED",
    "STAGE_ACCEPTED",
    "PLAN_REPAIR",
    "PLAN_ACCEPTED",
    "PLAN_REJECTED",
    "PlanRecovery",
    "ResumeSessionPolicy",
    "build_session_hints",
    "load_plan_document",
    "make_server_session_reusable",
    "plan_recovery",
    "plan_run_result_from_persisted",
    "recovery_state",
    "resume_plan",
    "run_stage_boundary_gate",
]