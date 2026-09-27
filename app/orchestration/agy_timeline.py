"""P6 observability: the append-only plan timeline + the summary.

Two responsibilities, both deterministic:

* :class:`Timeline` — one compact JSON line per event, appended
  atomically to ``state/runs/<plan_id>/timeline.jsonl``. The event
  set is CLOSED (``EVENT_TYPES``): ``PLAN_STARTED``,
  ``STAGE_STARTED``, ``TASK_DISPATCHED``, ``TASK_COMPLETED``,
  ``TASK_VERIFIED``, ``STAGE_COMMITTED``, ``STAGE_ACCEPTED``,
  ``PLAN_REPAIR_REQUESTED``, ``MODEL_FAILOVER``, ``RUN_INTERRUPTED``
  (written by the live driver on stop), ``RUN_RESUMED``,
  ``PLAN_ACCEPTED``, ``PLAN_REJECTED``, ``PLAN_ALREADY_ACCEPTED``,
  ``SESSION_CONTINUATION``. Every event carries ONLY identity +
  status/SHA fields — ``event_type``, auto-stamped ISO 8601 ``ts``,
  ``plan_id`` and, when applicable, ``stage_id`` / ``task_id`` /
  ``session_id`` / ``status`` / ``sha`` plus explicitly passed
  scalar extras. A nested dict value is rejected: events never
  duplicate the full state document. ``load()`` parses the file
  back and raises ``ValueError`` naming the LINE NUMBER on corrupt
  content (a torn write can never hide in the evidence).

* :func:`observability_summary` — computes the persisted
  ``observability.json``: ``plan`` / ``stages`` / ``tasks`` /
  ``runtime`` sections built ONLY from real artifacts (timeline
  event pairs, ``plan.json`` / ``plan_doc.json``, per-stage
  ``stage.json`` + task files) and an INJECTABLE ``session_usage``
  callable (default: a read of OpenCode ``/api/session`` rows by the
  persisted session ids). Every metric that cannot be established is
  the literal string ``"unknown"`` — never ``0`` / ``""``
  pretending to be real; in particular a missing or ``0``/absent
  cost is ``"unknown"``, never a fabricated zero.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_SESSION_SERVER = "http://127.0.0.1:4096"

UNKNOWN = "unknown"

EVENT_TYPES = frozenset(
    {
        "PLAN_STARTED",
        "STAGE_STARTED",
        "TASK_DISPATCHED",
        "TASK_COMPLETED",
        "TASK_VERIFIED",
        "STAGE_COMMITTED",
        "STAGE_ACCEPTED",
        "PLAN_REPAIR_REQUESTED",
        "MODEL_FAILOVER",
        "RUN_INTERRUPTED",
        "RUN_RESUMED",
        "PLAN_ACCEPTED",
        "PLAN_REJECTED",
        "PLAN_ALREADY_ACCEPTED",
        "SESSION_CONTINUATION",
    }
)

IDENTITY_KEYS = ("stage_id", "task_id", "session_id", "status", "sha")

# Reserved keys callers may not smuggle through **fields.
_RESERVED = ("event_type", "ts", "plan_id", *IDENTITY_KEYS)

_SCALARS = (str, int, float, bool)


def _now() -> str:
    # Microsecond precision keeps the second-level ordering of two
    # events emitted back to back.
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _check_extra_value(key: str, value: Any) -> None:
    """Events carry identity + scalars only — never nested state."""
    if isinstance(value, dict):
        raise ValueError(
            f"timeline field {key!r} must not be a dict: events carry "
            "identity + status/SHA only, never the full state document"
        )
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, _SCALARS) and not isinstance(item, list):
                continue
            raise ValueError(
                f"timeline field {key!r} may only contain scalars"
            )
        return
    if value is not None and not isinstance(value, _SCALARS):
        raise ValueError(
            f"timeline field {key!r} must be a scalar (or list of "
            f"scalars), got {type(value).__name__}"
        )


class Timeline:
    """Append-only JSONL event stream for ONE plan run directory."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- write ---------------------------------------------------------------

    def emit(
        self,
        event_type: str,
        *,
        plan_id: str,
        stage_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        sha: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """
        Append ONE event (auto-stamps ``ts``, validates the event
        type, always includes ``plan_id``). Atomic append semantics:
        open in append mode, one compact JSON line, flush + fsync so
        the event survives a subsequent kill.
        """
        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"unknown timeline event_type {event_type!r} "
                f"(allowed: {', '.join(sorted(EVENT_TYPES))})"
            )
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("timeline events require a non-empty plan_id")
        for key in fields:
            if key in _RESERVED:
                raise ValueError(
                    f"timeline field {key!r} is reserved; pass it as a "
                    "named argument"
                )
        for key, value in fields.items():
            _check_extra_value(key, value)

        event: dict[str, Any] = {
            "event_type": event_type,
            "ts": _now(),
            "plan_id": plan_id,
        }
        for key, value in (
            ("stage_id", stage_id),
            ("task_id", task_id),
            ("session_id", session_id),
            ("status", status),
            ("sha", sha),
            *fields.items(),
        ):
            if value is not None:
                event[key] = value

        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return event

    # -- read ----------------------------------------------------------------

    def load(self) -> list[dict[str, Any]]:
        """
        Parse the stream back. Missing file -> ``[]``; a corrupt line
        raises ``ValueError`` naming the 1-based LINE NUMBER.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for number, raw in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                raise ValueError(
                    f"timeline parse error at line {number} of "
                    f"{self.path.name}: {exc}"
                ) from exc
            if not isinstance(entry, dict):
                raise ValueError(
                    f"timeline parse error at line {number} of "
                    f"{self.path.name}: event is not a JSON object"
                )
            events.append(entry)
        return events

    def events_of(self, event_type: str) -> list[dict[str, Any]]:
        """All events of one type, in order."""
        return [
            event for event in self.load()
            if event.get("event_type") == event_type
        ]


# ---------------------------------------------------------------------------
# Session usage (injectable; default reads the live server)
# ---------------------------------------------------------------------------


def default_session_usage(
    session_ids: Iterable[str],
    *,
    server: str = DEFAULT_SESSION_SERVER,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
) -> dict[str, dict[str, Any]] | None:
    """
    Default ``session_usage``: read OpenCode ``/api/session`` rows by
    the persisted session ids. Any network/parse failure returns
    ``None`` (-> every metric ``"unknown"``). Never raises.
    """
    wanted = {str(sid) for sid in session_ids}
    if not wanted:
        return {}

    def _open(url: str, timeout_: float) -> Any:
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout_) as response:
            return json.loads(response.read().decode("utf-8"))

    open_url: Callable[..., Any] = opener if opener is not None else _open

    rows: list[dict[str, Any]] | None = None
    for url in (
        f"{server}/api/session?limit=5000",
        f"{server}/session?limit=5000",
    ):
        try:
            payload = open_url(url, timeout)
        except Exception:  # noqa: BLE001 - usage must never raise
            continue
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(payload, list):
            data = payload
        if isinstance(data, list) and all(
            isinstance(row, dict) for row in data
        ):
            rows = data
            break
    if rows is None:
        return None

    out: dict[str, dict[str, Any]] = {}
    by_id = {row.get("id"): row for row in rows}
    for session_id in wanted:
        row = by_id.get(session_id)
        if not isinstance(row, dict):
            out[session_id] = {
                "input_tokens": None,
                "output_tokens": None,
                "cost": None,
            }
            continue
        tokens = row.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        cost = row.get("cost")
        out[session_id] = {
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "cost": cost if isinstance(cost, (int, float)) else None,
        }
    return out


def _metric_token(value: Any) -> Any:
    """A reported token count, or ``"unknown"`` when unavailable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    return int(value)


def _metric_cost(value: Any) -> Any:
    """A REAL cost, or ``"unknown"`` — never a fabricated 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN
    if value == 0:
        return UNKNOWN
    return value


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _stage_dirs(run_dir: Path) -> list[Path]:
    if not run_dir.is_dir():
        return []
    return sorted(
        path
        for path in run_dir.iterdir()
        if path.is_dir()
        and (path / "stage.json").exists()
        and path.name != "commits"
    )


def _first_use(
    events: list[dict[str, Any]],
    event_type: str,
    **match: str,
) -> dict[str, Any] | None:
    for event in events:
        if event.get("event_type") != event_type:
            continue
        if all(event.get(key) == value for key, value in match.items()):
            return event
    return None


def observability_summary(
    run_dir: Path | str,
    timeline: Any = None,
    *,
    session_usage: Callable[[list[str]], Any] | None = None,
) -> dict[str, Any]:
    """
    Compute the observability summary from artifacts ONLY.

    ``timeline`` is a :class:`Timeline` or a path (default: the run
    dir's ``timeline.jsonl``); ``session_usage(session_ids)`` is the
    injectable model-usage seam (default reads the live server via
    :func:`default_session_usage`).

    Unavailable metrics are the literal ``"unknown"``.
    """
    run_dir = Path(run_dir)
    if timeline is None:
        timeline = Timeline(run_dir / "timeline.jsonl")
    events = timeline.load()

    plan_json = _load_json(run_dir / "plan.json") or {}
    plan_doc = _load_json(run_dir / "plan_doc.json") or {}
    stage_dirs = _stage_dirs(run_dir)

    # -- plan ----------------------------------------------------------------
    started = _first_use(events, "PLAN_STARTED")
    if started is None:
        started = events[0] if events else None
    terminal = None
    for event in events:
        if event.get("event_type") in ("PLAN_ACCEPTED", "PLAN_REJECTED",
                                       "PLAN_ALREADY_ACCEPTED"):
            terminal = event

    status = plan_json.get("status")
    start_ts = started.get("ts") if started else plan_json.get("created")
    if terminal is not None:
        end_ts = terminal.get("ts")
    elif status in ("PLAN_ACCEPTED", "PLAN_REJECTED"):
        end_ts = plan_json.get("updated")
    else:
        end_ts = None  # still running (or unknown) -> never invented

    start_dt = _parse_ts(start_ts)
    end_dt = _parse_ts(end_ts)
    elapsed = (
        (end_dt - start_dt).total_seconds()
        if start_dt is not None and end_dt is not None
        else UNKNOWN
    )

    stage_order = plan_json.get("stage_order") or [
        stage.get("stage_id")
        for stage in plan_doc.get("stages", [])
        if isinstance(stage, dict)
    ]
    stage_states: list[dict[str, Any]] = []
    for stage_dir in stage_dirs:
        stage_states.append(_load_json(stage_dir / "stage.json") or {})

    accepted = sum(
        1 for entry in stage_states
        if entry.get("status") == "STAGE_ACCEPTED"
    )
    rejected = sum(
        1 for entry in stage_states
        if entry.get("status") == "STAGE_REJECTED"
    )
    repaired = sum(
        1 for entry in stage_states
        if entry.get("repair_lineage")
    ) + len(plan_json.get("repair_lineage") or [])

    plan_section: dict[str, Any] = {
        "start": start_ts if start_ts else UNKNOWN,
        "end": end_ts if end_ts else UNKNOWN,
        "elapsed_s": elapsed,
        "status": status if isinstance(status, str) else UNKNOWN,
        "stages_total": (
            len(stage_order) if stage_order
            else (len(stage_dirs) if stage_dirs else UNKNOWN)
        ),
        # Stage verdict counts are real counts over the persisted
        # stage directories: 0 means "provably none accepted yet",
        # ""unknown"" only when no artifacts exist at all.
        "accepted": (accepted if stage_dirs else UNKNOWN),
        "repaired": (repaired if (stage_dirs or plan_json) else UNKNOWN),
        "rejected": (rejected if stage_dirs else UNKNOWN),
    }

    # -- stages --------------------------------------------------------------
    stages: dict[str, Any] = {}
    for stage_dir in stage_dirs:
        entry = _load_json(stage_dir / "stage.json") or {}
        stage_id = entry.get("stage_id") or stage_dir.name
        first_start = _first_use(
            events, "STAGE_STARTED", stage_id=stage_id
        )
        last_accept = None
        for event in events:
            if (
                event.get("event_type") == "STAGE_ACCEPTED"
                and event.get("stage_id") == stage_id
            ):
                last_accept = event
        start_dt_ = _parse_ts(first_start.get("ts")) if first_start else None
        end_dt_ = _parse_ts(last_accept.get("ts")) if last_accept else None
        dispatched = [
            event for event in events
            if event.get("event_type") == "TASK_DISPATCHED"
            and event.get("stage_id") == stage_id
        ]
        if dispatched:
            attempts: Any = len(dispatched)
        else:
            task_attempts = [
                int(task.get("attempt") or 0)
                for task in entry.get("task_results", []) or []
                if isinstance(task.get("attempt"), (int, float))
            ]
            attempts = max(task_attempts) if task_attempts else UNKNOWN
        decision = entry.get("supervisor_decision")
        stages[stage_id] = {
            "elapsed_s": (
                (end_dt_ - start_dt_).total_seconds()
                if start_dt_ is not None and end_dt_ is not None
                else UNKNOWN
            ),
            "attempts": attempts,
            "commit_sha": (
                (entry.get("commit") or {}).get("commit_sha")
                or UNKNOWN
            ),
            "supervisor_decision": decision if isinstance(decision, dict)
            else UNKNOWN,
        }

    # -- tasks ---------------------------------------------------------------
    tasks: dict[str, Any] = {}

    def verification_command_for(task_id: str, stage_id: str) -> str:
        stage_meta = next(
            (
                stage for stage in plan_doc.get("stages", [])
                if isinstance(stage, dict)
                and stage.get("stage_id") == stage_id
            ),
            None,
        )
        if stage_meta is None:
            return UNKNOWN
        try:
            from app.orchestration.plan_decompose import decompose_stage
            from app.schemas import PlanStage

            stage = PlanStage(**stage_meta)
            for worker_task in decompose_stage(
                stage, objective=str(plan_doc.get("objective") or "")
            ):
                if worker_task.id == task_id:
                    return worker_task.verification.command
        except Exception:  # noqa: BLE001 - summary must never raise
            return UNKNOWN
        return UNKNOWN

    for stage_dir in stage_dirs:
        stage_id = stage_dir.name
        tasks_dir = stage_dir / "tasks"
        records = []
        if tasks_dir.is_dir():
            for path in sorted(tasks_dir.glob("*.json")):
                record = _load_json(path)
                if isinstance(record, dict):
                    records.append(record)
        for record in records:
            task_id = record.get("task_id") or record.get("worker_task_id")
            if not task_id:
                continue
            dispatched = _first_use(
                events, "TASK_DISPATCHED", task_id=task_id
            )
            completed = _first_use(
                events, "TASK_COMPLETED", task_id=task_id
            )
            verified_ts = _first_use(
                events, "TASK_VERIFIED", task_id=task_id
            )
            validation_ok = record.get("validation_ok")
            tasks[task_id] = {
                "model_role": "dispatch",
                "session_id": record.get("session_id") or UNKNOWN,
                "session_source": record.get("session_source") or UNKNOWN,
                "start": (
                    dispatched.get("ts") if dispatched else UNKNOWN
                ),
                "end": (
                    completed.get("ts")
                    if completed
                    else (
                        verified_ts.get("ts") if verified_ts else UNKNOWN
                    )
                ),
                "attempts": (
                    len([
                        event for event in events
                        if event.get("event_type") == "TASK_DISPATCHED"
                        and event.get("task_id") == task_id
                    ])
                    or record.get("attempt")
                    or UNKNOWN
                ),
                "verification_command": verification_command_for(
                    str(task_id), stage_id
                ),
                "verification_result": (
                    "ok" if validation_ok is True
                    else ("failed" if validation_ok is False else UNKNOWN)
                ),
                "changed_files": list(record.get("changed_files") or []),
            }

    # -- runtime -------------------------------------------------------------
    failover_events = plan_json.get("failover_events")
    classifications: Any
    if isinstance(failover_events, list):
        failover_count: Any = len(failover_events)
        classifications = {}
        for event in failover_events:
            reason = (
                str(event.get("fallback_reason"))
                if isinstance(event, dict) and event.get("fallback_reason")
                else UNKNOWN
            )
            classifications[reason] = classifications.get(reason, 0) + 1
    else:
        failover_count = UNKNOWN
        classifications = UNKNOWN

    session_ids = plan_json.get("session_ids")
    if not isinstance(session_ids, list) and isinstance(plan_json, dict):
        session_ids = []

    model_usage: dict[str, dict[str, Any]] = {}
    usage_source = "unavailable"
    if session_usage is None and session_ids:
        session_usage = default_session_usage
    if session_usage is not None and session_ids:
        try:
            raw_usage = session_usage(list(session_ids))
        except Exception:  # noqa: BLE001 - unknown, never fabricated
            raw_usage = None
        if isinstance(raw_usage, dict):
            usage_source = "session_usage"
            for session_id in session_ids:
                entry = raw_usage.get(session_id)
                entry = entry if isinstance(entry, dict) else {}
                model_usage[str(session_id)] = {
                    "input_tokens": _metric_token(
                        entry.get("input_tokens")
                    ),
                    "output_tokens": _metric_token(
                        entry.get("output_tokens")
                    ),
                    "cost": _metric_cost(entry.get("cost")),
                }
        else:
            for session_id in session_ids:
                model_usage[str(session_id)] = {
                    "input_tokens": UNKNOWN,
                    "output_tokens": UNKNOWN,
                    "cost": UNKNOWN,
                }

    try:
        from app.orchestration.agy_runtime import (
            FALLBACK_DISPATCH_AGENT,
            PRIMARY_DISPATCH_AGENT,
        )

        primary_agent = PRIMARY_DISPATCH_AGENT
        fallback_agent = FALLBACK_DISPATCH_AGENT
    except Exception:  # noqa: BLE001 - constants unavailable -> unknown
        primary_agent = UNKNOWN
        fallback_agent = UNKNOWN

    runtime_section = {
        "primary_agent": primary_agent,
        "fallback_agent": fallback_agent,
        "failover_count": failover_count,
        "transient_classifications": classifications,
        "model_usage": model_usage,
        "model_usage_source": usage_source,
    }
    return {
        "plan": plan_section,
        "stages": stages,
        "tasks": tasks,
        "runtime": runtime_section,
    }


def save_observability_summary(
    run_dir: Path | str, summary: dict[str, Any]
) -> Path:
    """Atomically persist ``state/runs/<plan_id>/observability.json``."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "observability.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def write_observability(
    run_dir: Path | str,
    timeline: Any = None,
    *,
    session_usage: Callable[[list[str]], Any] | None = None,
) -> Path:
    """Compute + persist the summary (plan end / idempotent resume)."""
    run_dir = Path(run_dir)
    summary = observability_summary(
        run_dir, timeline, session_usage=session_usage
    )
    return save_observability_summary(run_dir, summary)