"""P5 control-plane failover-state persistence (schema-valid, atomic).

Appends schema-valid failover records to the control-plane FILE at
``<AGY_STATE_ROOT>/autopilot/agy-failover-state.json`` (P20: resolved from
the configurable state root; the canonical default is the main checkout's
state tree) — a JSON ARRAY of records, read/written on disk only through
the injectable ``path`` and never staged into a feature worktree.

Schema source of truth: ``state/autopilot/agy-failover-state.schema.json``
(control-plane, gitignored). It lives outside the importable
package tree, so the ``MIRRORED_*`` constants below MIRROR it and
every mirror field points back at the schema file by comment;
``tests/test_agy_failover_state.py`` parses the real schema file at an
injectable path and fails if this mirror ever drifts (skip gracefully
when the control plane is absent).

Validation rules (exactly the schema's ``items``: ``type=object``,
``additionalProperties=false``, 9 REQUIRED keys + 2 optional):

* required: ``role``, ``primary_model``, ``fallback_model``,
  ``active_model``, ``primary_attempts``, ``fallback_reason``,
  ``session_id``, ``fallback_count``, ``parent_session_id``;
* optional (also allowed, schema ``properties``): ``probe_derived``
  (bool), ``ts`` (ISO-8601 string);
* ``role`` in {"dispatch", "supervisor"}; ``primary_attempts`` int
  1..4; ``fallback_reason`` in ``failover.FAILOVER_REASONS`` (the
  4-value enum, IMPORTED — never re-declared); ``session_id`` must
  match ``^ses_``; ``fallback_count`` int >= 1;
  ``parent_session_id`` null-or-``^ses_`` string;
* any missing required key or any unknown/extra key or any
  type/enum/range/pattern violation raises ``ValueError`` naming the
  offending record index AND field — a single atomic write happens
  only after every record validates (never a partial file).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from app.orchestration.failover import FAILOVER_REASONS
from app.runtime_config import state_root as _state_root

# P20: resolved from the configurable state root (AGY_STATE_ROOT), not a
# hardcoded host path.
DEFAULT_STATE_PATH = Path(_state_root()) / "autopilot" / "agy-failover-state.json"
SCHEMA_PATH = DEFAULT_STATE_PATH.with_name(
    "agy-failover-state.schema.json"
)

# --- mirrored from agy-failover-state.schema.json (source of truth) ---
# items.required (exact order preserved):
MIRRORED_REQUIRED_FIELDS = (
    "role",
    "primary_model",
    "fallback_model",
    "active_model",
    "primary_attempts",
    "fallback_reason",
    "session_id",
    "fallback_count",
    "parent_session_id",
)
# items.properties keys (= required + optional; additionalProperties
# is false in the schema):
MIRRORED_ALLOWED_FIELDS = frozenset(
    MIRRORED_REQUIRED_FIELDS + ("probe_derived", "ts")
)
# properties.role.enum:
MIRRORED_ROLE_ENUM = ("dispatch", "supervisor")
# properties.fallback_reason.enum == failover.FAILOVER_REASONS
# (uniquely imported from there, asserted against the schema in tests).
# properties.primary_attempts.minimum / .maximum:
MIRRORED_PRIMARY_ATTEMPTS_RANGE = (1, 4)
# properties.session_id.pattern (and parent_session_id.pattern):
MIRRORED_SES_PATTERN = re.compile(r"^ses_")
# properties.fallback_count.minimum:
MIRRORED_FALLBACK_COUNT_MIN = 1
# items.additionalProperties:
MIRRORED_ADDITIONAL_PROPERTIES = False
# ----------------------------------------------------------------------


def _violation(index: int, field: str, detail: str) -> ValueError:
    """ValueError naming the offending record (index) and field."""
    return ValueError(
        f"failover record [{index}] field {field!r}: {detail}"
    )


def _is_int(value: Any) -> bool:
    """Exact int (bool is NOT an int here — schema type is integer)."""
    return isinstance(value, int) and not isinstance(value, bool)


def validate_failover_record(record: Any, index: int = 0) -> dict:
    """
    Validate ONE record against the mirrored rules above.

    Returns the record unchanged on success; raises ``ValueError``
    (naming ``[index]`` + the offending field) on any violation.
    """
    if not isinstance(record, dict):
        raise _violation(index, "<record>", "must be an object")

    missing = [
        key for key in MIRRORED_REQUIRED_FIELDS if key not in record
    ]
    if missing:
        raise _violation(
            index, missing[0], "required key missing"
        )

    unknown = sorted(
        key for key in record if key not in MIRRORED_ALLOWED_FIELDS
    )
    if unknown:
        raise _violation(
            index,
            unknown[0],
            "additional property not allowed by the schema "
            "(additionalProperties=false)",
        )

    role = record["role"]
    if role not in MIRRORED_ROLE_ENUM:
        raise _violation(
            index,
            "role",
            f"{role!r} not in enum {list(MIRRORED_ROLE_ENUM)}",
        )

    for model_key in ("primary_model", "fallback_model", "active_model"):
        value = record[model_key]
        if not isinstance(value, str) or not value.strip():
            raise _violation(
                index, model_key, f"must be a non-empty string, got {value!r}"
            )

    attempts = record["primary_attempts"]
    low, high = MIRRORED_PRIMARY_ATTEMPTS_RANGE
    if not _is_int(attempts) or not (low <= attempts <= high):
        raise _violation(
            index,
            "primary_attempts",
            f"must be an integer in {low}..{high}, got {attempts!r}",
        )

    reason = record["fallback_reason"]
    if reason not in FAILOVER_REASONS:
        raise _violation(
            index,
            "fallback_reason",
            f"{reason!r} not in enum {list(FAILOVER_REASONS)}",
        )

    session_id = record["session_id"]
    if not isinstance(session_id, str) or not MIRRORED_SES_PATTERN.match(
        session_id
    ):
        raise _violation(
            index,
            "session_id",
            f"must match ^ses_, got {session_id!r}",
        )

    count = record["fallback_count"]
    if not _is_int(count) or count < MIRRORED_FALLBACK_COUNT_MIN:
        raise _violation(
            index,
            "fallback_count",
            "must be an integer >= "
            f"{MIRRORED_FALLBACK_COUNT_MIN}, got {count!r}",
        )

    parent = record["parent_session_id"]
    if parent is not None and (
        not isinstance(parent, str)
        or not MIRRORED_SES_PATTERN.match(parent)
    ):
        raise _violation(
            index,
            "parent_session_id",
            f"must be null or match ^ses_, got {parent!r}",
        )

    if "probe_derived" in record and not isinstance(
        record["probe_derived"], bool
    ):
        raise _violation(
            index,
            "probe_derived",
            f"must be a boolean, got {record['probe_derived']!r}",
        )

    if "ts" in record and not isinstance(record["ts"], str):
        raise _violation(
            index, "ts", f"must be a string, got {record['ts']!r}"
        )

    return record


def load_failover_events(path: Path | str = DEFAULT_STATE_PATH) -> list[Any]:
    """
    Read the existing JSON array at ``path`` (empty list when the
    file does not exist yet; ``ValueError`` when it is not an array).
    """
    target = Path(path)
    if not target.exists():
        return []
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(
            f"failover state at {target} must be a JSON ARRAY, got "
            f"{type(payload).__name__}"
        )
    return payload


def persist_failover_events(
    path: Path | str = DEFAULT_STATE_PATH,
    events: list[dict] | None = None,
    *,
    merge: bool = True,
) -> Path:
    """
    Validate, append (or replace) and atomically persist failover
    records at ``path`` (default: the control-plane file; inject a
    path for tests). Signature follows the contract's
    ``(path, events, *, merge=True)`` — ``events`` is required in
    practice and may also be passed by keyword.

    * EVERY record going into the file validates first (existing
      records when ``merge=True``, plus each new event); on ANY
      violation a ``ValueError`` naming the offending record+field is
      raised and NOTHING is written.
    * ``merge=True`` preserves existing records and appends the new
      ones; ``merge=False`` replaces the file with ``events``.
    * The write is atomic: temp file in the same directory +
      ``os.replace``.
    """
    if events is None:
        raise TypeError(
            "persist_failover_events() missing required argument: "
            "'events' (call persist_failover_events(path, events))"
        )
    if not isinstance(events, list):
        raise TypeError("events must be a list of failover record dicts")

    target = Path(path)
    existing: list[Any] = load_failover_events(target) if merge else []

    # Validation first (nothing is written on failure): existing
    # records are indexed by their position in the file, NEW events
    # by their position within the submitted `events` list — ValueError
    # messages name record index + field either way.
    for position, old in enumerate(existing):
        validate_failover_record(old, index=position)
    for position, event in enumerate(events):
        validate_failover_record(event, index=position)

    final_payload = [*existing, *events] if merge else list(events)

    # All valid => single atomic write. A failure above never reaches
    # this line, so the target file is never written partially.
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    text = json.dumps(final_payload, sort_keys=True, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)

    return target