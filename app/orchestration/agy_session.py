"""P4 session-capture hardening: stdout first, server read-back after.
from app import runtime_config

The P3 live E2E proved the defect this module fixes: the
``opencode-supervise`` supervisor stdout did not carry the
``Session ID: ses_...`` line, so the extractor returned ``None`` and
the persisted run state carried empty session ids — even though the
dispatch sessions provably existed on the OpenCode server.

Two seams fix it:

* :func:`read_back_session` — plain ``GET`` of the session list
  (``/api/session`` first; ``/session`` only when the primary payload
  shape is not a usable row list). Rows are filtered by ``agent``,
  optionally by directory (the ``location``/``directory``/``path``
  fields, whatever the view carries — verified against real rows) and
  optionally by ``time.updated >= since_ms``; the NEWEST match wins
  (by ``time.updated`` falling back to ``time.created``, tie-broken
  deterministically on ``id``). Every network/parse error returns
  ``None`` — this helper NEVER raises into the execution path.
* :func:`capture_session` — resolution order ``stdout`` -> ``known``
  -> read-back -> ``none``, returning ``(session_id, source)`` with
  ``source`` in ``{"stdout", "known", "readback", "none"}`` so
  run-state can record HOW each session id was established.

:class:`TaskSessionLog` is the tiny per-task ``{session_id, source}``
recorder the live executor carries (``execute_task.session_log``); the
plan driver and the run state read sources from it, which is what
makes the P3 defect auditable.

Everything here is deterministic and injectable: ``capture_session``
takes the read-back callable, ``read_back_session`` takes the URL
opener — tests never touch the network.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any, Callable

from app import runtime_config


def _default_server() -> str:
    """The OpenCode session server, resolved from the configurable runtime
    (P20). Env-overridable via OPENCODE_SERVER_URL; defaults to the loopback
    worker backend. Tests that inject an opener still get a concrete
    (non-string) server URL by default."""
    return os.environ.get(
        "OPENCODE_SERVER_URL",
        str(runtime_config.opencode_server_url()),
    )


# Kept for any legacy consumers that imported the constant.
DEFAULT_SESSION_SERVER = _default_server()

# Session window: the server truncates /api/session responses (50 rows
# without a limit); a wide window guarantees sessions created moments
# ago are always in scope regardless of server-side ordering.
SESSION_LIST_LIMIT = 5000

SOURCE_STDOUT = "stdout"
SOURCE_KNOWN = "known"
SOURCE_READBACK = "readback"
SOURCE_NONE = "none"

# Same loose semantics as the runtime used in P3: prefer the printed
# "Session ID: ses_..." line, accept a bare ses_ token as a fallback.
SESSION_LINE_RE = re.compile(
    r"Session\s+ID\s*:\s*(ses_[A-Za-z0-9]+)",
    re.IGNORECASE,
)
SESSION_ANY_RE = re.compile(r"\b(ses_[A-Za-z0-9]+)\b")

# Row fields that may reference the working directory. Real rows:
# /api/session -> "location": {"directory": "/abs/path"} (a dict);
# /session     -> "directory": "/abs/path" plus a relative "path".
_DIRECTORY_KEYS = ("directory", "path", "location")


# ---------------------------------------------------------------------------
# stdout extraction (shared with the runtime; the P3 table keeps working)
# ---------------------------------------------------------------------------

def extract_session_from_stdout(text: str | None) -> str | None:
    """First OpenCode session id in supervisor output, or ``None``."""
    if not text:
        return None

    match = SESSION_LINE_RE.search(text) or SESSION_ANY_RE.search(text)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# read-back
# ---------------------------------------------------------------------------

def _rows_of(payload: Any) -> list[dict] | None:
    """
    Usable row list from a payload, or ``None`` when the shape differs.

    Accepts a bare list or ``{"data": [...]}``; every entry must be a
    dict (a view that is a list of something else is not the expected
    shape either).
    """
    rows: Any = payload
    if isinstance(payload, dict):
        rows = payload.get("data")

    if not isinstance(rows, list):
        return None
    if any(not isinstance(row, dict) for row in rows):
        return None

    return rows


def _string_leaves(value: Any) -> list[str]:
    """All string leaves of a (possibly nested) JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for nested in value.values():
            out.extend(_string_leaves(nested))
        return out
    if isinstance(value, list):
        out = []
        for nested in value:
            out.extend(_string_leaves(nested))
        return out
    return []


def _row_references_directory(row: dict, directory: str) -> bool:
    """Does the row's directory/path/location reference this directory?"""
    wanted = directory.rstrip("/")

    for key in _DIRECTORY_KEYS:
        if key not in row:
            continue
        for candidate in _string_leaves(row[key]):
            if candidate.rstrip("/") == wanted:
                return True

    return False


def _row_time_ms(row: dict, key: str) -> int | None:
    """``time.<key>`` as an int, ``None`` when absent/not a number."""
    time_block = row.get("time")
    if not isinstance(time_block, dict):
        return None

    value = time_block.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return int(value)


def _select_newest(
    rows: list[dict],
    *,
    agent: str,
    directory: str | None,
    since_ms: int | None,
) -> str | None:
    """
    Newest qualifying session id, or ``None``.

    Qualifying: ``agent`` matches exactly; when ``directory`` is given
    the row references it; when ``since_ms`` is given the row's
    update time (``time.updated``, falling back to ``time.created``)
    is at/after it — a row without a usable timestamp cannot confirm
    recency and is therefore excluded. Deterministic tie-break: the
    higher ``id`` wins.
    """
    candidates: list[tuple[int, str]] = []

    for row in rows:
        if row.get("agent") != agent:
            continue
        if directory is not None and not _row_references_directory(
            row, directory
        ):
            continue

        updated = _row_time_ms(row, "updated")
        time_key = (
            updated
            if updated is not None
            else _row_time_ms(row, "created")
        )
        if since_ms is not None and (
            time_key is None or time_key < since_ms
        ):
            continue
        if time_key is None:
            time_key = 0

        session_id = row.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue

        candidates.append((time_key, session_id))

    if not candidates:
        return None

    candidates.sort(reverse=True)  # newest first, id breaks ties
    return candidates[0][1]


def _default_opener(url: str, timeout: float) -> Any:
    """urllib-backed opener: returns the decoded JSON payload."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def read_back_session(
    server: str = DEFAULT_SESSION_SERVER,
    *,
    agent: str,
    directory: str | None = None,
    since_ms: int | None = None,
    timeout: float = 10.0,
    opener: Callable[..., Any] | None = None,
) -> str | None:
    """
    Newest server-side session for ``agent`` (read-back fallback).

    ``GET <server>/api/session`` first (``?limit=`` wide window so a
    truncated view can never hide a just-created session); when that
    payload's shape is not a usable row list, fall back to ``GET
    <server>/session``. Filters: agent, directory (``location`` /
    ``directory`` / ``path``, dict or string forms), ``since_ms``.

    ``opener(url, timeout) -> payload`` is the test seam (default:
    urllib GET + JSON decode). ALL errors — network, HTTP, JSON,
    unexpected shapes — return ``None``. This function never raises.
    """
    open_url = opener if opener is not None else _default_opener
    limit = SESSION_LIST_LIMIT
    urls = (
        f"{server}/api/session?limit={limit}",
        f"{server}/session?limit={limit}",
    )

    for url in urls:
        try:
            payload = open_url(url, timeout)
        except Exception:  # noqa: BLE001 - read-back must never raise
            continue

        try:
            rows = _rows_of(payload)
        except Exception:  # noqa: BLE001 - defensive; treat as shape miss
            rows = None
        if rows is None:
            continue  # shape differs -> try the other view

        return _select_newest(
            rows,
            agent=agent,
            directory=directory,
            since_ms=since_ms,
        )

    return None


# ---------------------------------------------------------------------------
# the capture used by the executor
# ---------------------------------------------------------------------------

def capture_session(
    stdout: str | None,
    *,
    known_session: str | None,
    agent: str,
    directory: str | None = None,
    since_ms: int | None = None,
    server: str = DEFAULT_SESSION_SERVER,
    read_back: Callable[..., str | None] | None = None,
) -> tuple[str | None, str]:
    """
    Resolve a task's ``(session_id, source)`` without losing evidence.

    Order: stdout match -> ``known_session`` -> server read-back -> no
    session. ``source`` is one of ``"stdout"``, ``"known"``,
    ``"readback"``, ``"none"`` and is persisted alongside the id so
    run-state can prove the P3-defect fallback fired.

    ``read_back`` defaults to :func:`read_back_session`; it is called
    as ``read_back(server=..., agent=..., directory=...,
    since_ms=...)`` and must return a session id or ``None``. Any
    exception the injected read-back raises is swallowed to
    ``(None, "none")`` — capture never fails the execution path.
    """
    session_id = extract_session_from_stdout(stdout)
    if session_id:
        return session_id, SOURCE_STDOUT

    if known_session:
        return known_session, SOURCE_KNOWN

    resolve = read_back if read_back is not None else read_back_session
    try:
        session_id = resolve(
            server=server,
            agent=agent,
            directory=directory,
            since_ms=since_ms,
        )
    except Exception:  # noqa: BLE001 - capture never raises
        return None, SOURCE_NONE

    if session_id:
        return str(session_id), SOURCE_READBACK

    return None, SOURCE_NONE


# ---------------------------------------------------------------------------
# per-task record
# ---------------------------------------------------------------------------

class TaskSessionLog:
    """
    Tiny per-task ``{session_id, source}`` recorder.

    The live executor records into one of these and exposes it as
    ``execute_task.session_log``; the plan driver resolves it as the
    default session capturer, and ``source_for`` is what the run state
    persists per task (``stdout`` / ``readback`` / ``known`` /
    ``none``). Later captures for the same task overwrite earlier
    ones (the most recent capture is the authoritative one).
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def record(
        self,
        task_id: str | None,
        session_id: str | None,
        source: str | None,
    ) -> dict[str, Any] | None:
        """Remember one task's ``(session_id, source)``; id required."""
        if not task_id or not session_id:
            return None

        entry = {
            "task_id": task_id,
            "session_id": session_id,
            "source": source or SOURCE_NONE,
        }
        self._records[task_id] = entry
        return entry

    def get(self, task_id: str) -> dict[str, Any] | None:
        """The record for ``task_id`` (``None`` when never captured)."""
        entry = self._records.get(task_id)
        return dict(entry) if entry is not None else None

    def session_id_for(self, task_id: str) -> str | None:
        """The task's session id, or ``None``."""
        entry = self._records.get(task_id)
        return entry["session_id"] if entry else None

    def source_for(self, task_id: str) -> str:
        """The task's capture source; ``"none"`` when unrecorded."""
        entry = self._records.get(task_id)
        return str(entry["source"]) if entry else SOURCE_NONE

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        """Copy of every task record, keyed by task id."""
        return {task: dict(entry) for task, entry in self._records.items()}

    @property
    def sources(self) -> dict[str, str]:
        """``{task_id: source}`` — the auditable capture map."""
        return {
            task: str(entry["source"])
            for task, entry in self._records.items()
        }

    def session_ids(self) -> list[str]:
        """Deduplicated session ids in capture order."""
        seen: set[str] = set()
        order: list[str] = []
        for entry in self._records.values():
            session_id = entry["session_id"]
            if session_id and session_id not in seen:
                seen.add(session_id)
                order.append(session_id)
        return order