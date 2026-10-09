from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.integrations.jules_api import JulesClient


TERMINAL_STATES = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
}

QUESTION_STATES = {
    "AWAITING_USER_FEEDBACK",
    "AWAITING_PLAN_APPROVAL",
}

SOFT_IDLE_SECONDS = 300
HARD_IDLE_SECONDS = 900


@dataclass(frozen=True)
class JulesPollResult:
    run_id: str
    session_id: str
    jules_state: str | None
    phase: str
    new_activities: int
    pending_question: Any
    verdict: str | None
    verdict_finalized: bool
    report_activity_id: str | None
    idle_seconds: int | None
    idle_state: str
    terminal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "jules_state": self.jules_state,
            "phase": self.phase,
            "new_activities": self.new_activities,
            "pending_question": self.pending_question,
            "verdict": self.verdict,
            "verdict_finalized": self.verdict_finalized,
            "report_activity_id": self.report_activity_id,
            "idle_seconds": self.idle_seconds,
            "idle_state": self.idle_state,
            "terminal": self.terminal,
        }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(
    path: Path,
    value: dict[str, Any],
) -> None:
    path.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=False,
        )
        + "\n"
    )


def _activity_list(
    response: Any,
) -> list[dict[str, Any]]:
    """
    Accept both a direct list and the common wrapped
    Jules API response shapes.
    """

    if isinstance(response, list):
        return [
            item
            for item in response
            if isinstance(item, dict)
        ]

    if not isinstance(response, dict):
        return []

    for key in (
        "activities",
        "items",
        "results",
    ):
        value = response.get(key)

        if isinstance(value, list):
            return [
                item
                for item in value
                if isinstance(item, dict)
            ]

    return []


def _activity_id(
    activity: dict[str, Any],
) -> str:
    for key in (
        "id",
        "name",
        "activityId",
    ):
        value = activity.get(key)

        if value:
            return str(value)

    canonical = json.dumps(
        activity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    return (
        "sha256:"
        + hashlib.sha256(canonical).hexdigest()
    )


def _activity_time(
    activity: dict[str, Any],
) -> str | None:
    for key in (
        "updateTime",
        "createTime",
        "timestamp",
        "time",
    ):
        value = activity.get(key)

        if value:
            return str(value)

    return None


def _find_text(
    value: Any,
) -> str | None:
    """
    Conservative recursive extraction for a Jules
    question/message payload.

    Raw activities remain authoritative and are always
    persisted separately.
    """

    if isinstance(value, str):
        text = value.strip()
        return text or None

    if isinstance(value, dict):
        # Prefer semantically useful fields.
        for key in (
            "question",
            "message",
            "text",
            "content",
            "prompt",
        ):
            if key in value:
                found = _find_text(value[key])

                if found:
                    return found

        for child in value.values():
            found = _find_text(child)

            if found:
                return found

    if isinstance(value, list):
        for child in value:
            found = _find_text(child)

            if found:
                return found

    return None



def _agent_message(
    activity: dict[str, Any],
) -> str | None:
    event = activity.get(
        "agentMessaged"
    )

    if not isinstance(event, dict):
        return None

    value = event.get(
        "agentMessage"
    )

    if not isinstance(value, str):
        return None

    value = value.strip()

    return value or None


def _verdict_from_message(
    message: str | None,
) -> str | None:
    if not message:
        return None

    lines = [
        line.strip().upper()
        for line in message.splitlines()
        if line.strip()
    ]

    if not lines:
        return None

    verdict = lines[-1]

    if verdict in {
        "PASS",
        "FAIL",
        "BLOCKED",
    }:
        return verdict

    return None


def _parse_timestamp(
    value: str | None,
) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def _idle_status(
    *,
    terminal: bool,
    awaiting_input: bool,
    session_update: str | None,
    activity_update: str | None,
) -> tuple[int | None, str]:

    if terminal:
        return None, "TERMINAL"

    if awaiting_input:
        return None, "AWAITING_INPUT"

    timestamps = [
        value
        for value in (
            _parse_timestamp(
                session_update
            ),
            _parse_timestamp(
                activity_update
            ),
        )
        if value is not None
    ]

    if not timestamps:
        return None, "UNKNOWN"

    latest = max(timestamps)

    seconds = max(
        0,
        int(
            (
                datetime.now(
                    timezone.utc
                )
                - latest
            ).total_seconds()
        ),
    )

    if seconds >= HARD_IDLE_SECONDS:
        return seconds, "HARD_IDLE"

    if seconds >= SOFT_IDLE_SECONDS:
        return seconds, "SOFT_IDLE"

    return seconds, "ACTIVE"



def _phase_for_state(
    state: str | None,
) -> str:
    if state == "COMPLETED":
        return "JULES_COMPLETED"

    if state in {
        "FAILED",
        "CANCELLED",
    }:
        return "JULES_FAILED"

    if state == "AWAITING_USER_FEEDBACK":
        return "AWAITING_USER_FEEDBACK"

    if state == "AWAITING_PLAN_APPROVAL":
        return "AWAITING_PLAN_APPROVAL"

    if state == "PAUSED":
        return "JULES_PAUSED"

    return "JULES_RUNNING"


def poll_once(
    run_dir: str | Path,
    *,
    client: JulesClient | None = None,
) -> JulesPollResult:
    root = Path(
        run_dir
    ).expanduser().resolve()

    state_path = root / "state.json"

    if not state_path.is_file():
        raise FileNotFoundError(
            f"Missing Jules state: {state_path}"
        )

    state = _read_json(state_path)

    run_id = str(
        state.get("run_id")
        or root.name
    )

    session_id = str(
        state["jules_session_id"]
    )

    client = client or JulesClient()

    session = client.get_session(
        session_id
    )

    raw_activities = client.list_activities(
        session_id
    )

    activities = _activity_list(
        raw_activities
    )

    seen = {
        str(value)
        for value in state.get(
            "seen_activity_ids",
            [],
        )
    }

    new_activities: list[
        dict[str, Any]
    ] = []

    for activity in activities:
        activity_id = _activity_id(
            activity
        )

        if activity_id in seen:
            continue

        new_activities.append(
            activity
        )

        seen.add(activity_id)

    if new_activities:
        activities_path = (
            root / "activities.jsonl"
        )

        with activities_path.open(
            "a",
            encoding="utf-8",
        ) as handle:
            for activity in new_activities:
                handle.write(
                    json.dumps(
                        activity,
                        sort_keys=True,
                    )
                    + "\n"
                )

    jules_state = session.get(
        "state"
    )

    if jules_state is not None:
        jules_state = str(
            jules_state
        )

    phase = _phase_for_state(
        jules_state
    )

    terminal = (
        jules_state
        in TERMINAL_STATES
    )

    latest_agent_message = None
    report_activity_id = None
    verdict = None

    for activity in reversed(
        activities
    ):
        message = _agent_message(
            activity
        )

        if message is None:
            continue

        latest_agent_message = message
        report_activity_id = _activity_id(
            activity
        )
        verdict = _verdict_from_message(
            message
        )
        break

    pending_question = None

    if jules_state in QUESTION_STATES:
        # Prefer the newest activity.
        for activity in reversed(
            activities
        ):
            text = _find_text(
                activity
            )

            if text:
                pending_question = {
                    "text": text,
                    "activity_id": (
                        _activity_id(
                            activity
                        )
                    ),
                }
                break

        if pending_question is None:
            pending_question = {
                "text": None,
                "reason": (
                    "Jules is awaiting input; "
                    "inspect raw activities."
                ),
            }

    last_activity_time = (
        state.get(
            "last_activity_time"
        )
    )

    for activity in activities:
        timestamp = _activity_time(
            activity
        )

        if timestamp:
            last_activity_time = timestamp

    idle_seconds, idle_state = (
        _idle_status(
            terminal=terminal,
            awaiting_input=(
                jules_state
                in QUESTION_STATES
            ),
            session_update=(
                session.get(
                    "updateTime"
                )
            ),
            activity_update=(
                last_activity_time
            ),
        )
    )

    if latest_agent_message is not None:
        (
            root / "report-latest.md"
        ).write_text(
            latest_agent_message.rstrip()
            + "\n"
        )

    state.update(
        {
            "phase": phase,
            "jules_state": jules_state,
            "last_session_update": (
                session.get("updateTime")
            ),
            "last_activity_time": (
                last_activity_time
            ),
            "seen_activity_ids": sorted(
                seen
            ),
            "pending_question": (
                pending_question
            ),
            "verdict": verdict,
            "verdict_finalized": (
                terminal
                and verdict is not None
            ),
            "report_activity_id": (
                report_activity_id
            ),
            "idle_seconds": idle_seconds,
            "idle_state": idle_state,
        }
    )

    _write_json(
        state_path,
        state,
    )

    _write_json(
        root / "session-latest.json",
        session,
    )

    _write_json(
        root / "activities-latest.json",
        {
            "activities": activities,
        },
    )

    result = JulesPollResult(
        run_id=run_id,
        session_id=session_id,
        jules_state=jules_state,
        phase=phase,
        new_activities=len(
            new_activities
        ),
        pending_question=(
            pending_question
        ),
        verdict=verdict,
        verdict_finalized=(
            terminal
            and verdict is not None
        ),
        report_activity_id=(
            report_activity_id
        ),
        idle_seconds=idle_seconds,
        idle_state=idle_state,
        terminal=terminal,
    )

    _write_json(
        root / "poll-latest.json",
        result.to_dict(),
    )

    return result
