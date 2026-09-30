"""P8 Todo PROJECTION — OpenCode session todos are a view, never a source.

Hard rule 5: AGY durable state is authoritative. This module renders
the canonical ``CheckpointPlan`` state into the OpenCode session todo
list; nothing here ever writes back into the plan. The projection is a
PURE function of the plan document: re-loading the plan doc alone
reproduces an identical todo list (contract test 33-37).

``render_checkpoint_todos(plan)``
    ordered items: PATH first, then per checkpoint (in execution
    order) per gate (G1..G4):
    ``{"id": "CP1:G1_behavior", "content": "CP1 · G1 Behavior — REQUIRED|N/A",
       "status": "completed"|"in_progress"|"pending"|"skipped"}``
    completed: gate PASSED or NOT_APPLICABLE; in_progress: ACTIVE;
    pending: PENDING. A REPAIR/APPROVED-but-unstarted gate whose
    checkpoint is BLOCKED renders "skipped".
``sync_checkpoint_todos(session_id, plan, server=..., http=...)``
    PUT the rendered list to the OpenCode session todo API. On any
    4xx/5xx/transport error return ``{"synced": False, "error": ...}``
    — canonical state never changes either way.

Worker A's validation module supplies the deterministic execution
order; the import is seam-guarded for parallel development with an
identical pure fallback (stable topo sort, ties by checkpoint_id).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

from app import runtime_config

try:  # Worker A's validation seam (authoritative at integration time)
    from app.orchestration.checkpoint_planning.validation import (
        checkpoint_execution_order,
    )
except ImportError:  # pragma: no cover - seam-guard for parallel development
    from collections import defaultdict, deque

    def checkpoint_execution_order(plan: Any) -> list[str]:  # type: ignore[misc]
        """Fallback stable topo sort (ties broken by checkpoint_id)."""
        ids = [c.checkpoint_id for c in plan.checkpoints]
        deps = {c.checkpoint_id: list(c.dependencies) for c in plan.checkpoints}

        indegree = {cid: 0 for cid in ids}
        children: dict[str, list[str]] = defaultdict(list)
        for cid in ids:
            for dep in deps[cid]:
                if dep in indegree:
                    children[dep].append(cid)
                    indegree[cid] += 1

        ready = sorted(cid for cid, deg in indegree.items() if deg == 0)
        order: list[str] = []
        queue = deque(ready)
        while queue:
            cid = queue.popleft()
            order.append(cid)
            for child in children[cid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    # insert keeping the frontier sorted (deterministic)
                    queue.append(child)
                    queue = deque(sorted(queue))
        if len(order) != len(ids):  # cycle: fall back to id order
            order.extend(sorted(set(ids) - set(order)))
        return order


DEFAULT_SERVER = str(runtime_config.opencode_server_url())

GATE_ORDER = ("G1_behavior", "G2_ui", "G3_adversarial", "G4_human")
GATE_LABELS = {
    "G1_behavior": "G1 Behavior",
    "G2_ui": "G2 UI",
    "G3_adversarial": "G3 Adversarial",
    "G4_human": "G4 Human",
}

STATUS_COMPLETED = "completed"
STATUS_IN_PROGRESS = "in_progress"
STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"

_TODO_PATH = "/session/{session_id}/todo"


# ---------------------------------------------------------------------------
# rendering (pure function of canonical state)
# ---------------------------------------------------------------------------

def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _applicability_label(gate: Any) -> str:
    return "REQUIRED" if getattr(gate, "required", False) else "N/A"


def _checkpoint_by_id(plan: Any) -> dict[str, Any]:
    return {c.checkpoint_id: c for c in plan.checkpoints}


def _gate_projection_status(gate_record: Any, expectations: Any, checkpoint_blocked: bool) -> str:
    """Map durable gate state -> todo status (projection rules, never back)."""
    applicability = _enum_value(getattr(gate_record, "applicability", "REQUIRED"))
    status = _enum_value(getattr(gate_record, "status", "PENDING"))

    if applicability == "NOT_APPLICABLE" or status == "PASSED":
        return STATUS_COMPLETED
    if status == "ACTIVE":
        return STATUS_IN_PROGRESS
    if status == "PENDING":
        return STATUS_SKIPPED if checkpoint_blocked else STATUS_PENDING
    # REPAIR keeps the gate visible as in progress; REJECTED -> skipped.
    if status == "REPAIR":
        return STATUS_IN_PROGRESS
    if status == "REJECTED":
        return STATUS_SKIPPED
    return STATUS_PENDING  # defensive default


def _path_content(plan: Any, checkpoints: dict[str, Any]) -> str:
    """``PATH: CPx → Gk → <state>`` — next open gate of the first
    not-yet-accepted checkpoint; terminal state when everything closed."""
    for cid in checkpoint_execution_order(plan):
        checkpoint = checkpoints[cid]
        if _enum_value(getattr(checkpoint, "status", "PLANNED")) == "ACCEPTED":
            continue
        records = getattr(checkpoint, "gate_records", {}) or {}
        for gate_kind in GATE_ORDER:
            record = records.get(gate_kind)
            if record is None:
                continue
            status = _enum_value(getattr(record, "status", "PENDING"))
            applicability = _enum_value(getattr(record, "applicability", "REQUIRED"))
            if applicability == "NOT_APPLICABLE" or status == "PASSED":
                continue
            return f"PATH: {cid} → {gate_kind} → {status}"
        # all gates closed but checkpoint not accepted: show checkpoint state
        return f"PATH: {cid} → gates closed → {_enum_value(getattr(checkpoint, 'status', 'PLANNED'))}"
    return "PATH: all checkpoints accepted"


def render_checkpoint_todos(plan: Any) -> list[dict]:
    """
    Deterministic Todo projection of ``plan`` (canonical state read
    only). PATH first, then execution-order checkpoints, gates G1..G4.
    """
    checkpoints = _checkpoint_by_id(plan)
    items: list[dict] = [
        {
            "id": "PATH",
            "content": _path_content(plan, checkpoints),
            "status": STATUS_IN_PROGRESS,
        }
    ]

    for cid in checkpoint_execution_order(plan):
        checkpoint = checkpoints[cid]
        blocked = _enum_value(getattr(checkpoint, "status", "PLANNED")) == "BLOCKED"
        records = getattr(checkpoint, "gate_records", {}) or {}

        for gate_kind in GATE_ORDER:
            record = records.get(gate_kind)
            expectations = None
            gates = getattr(checkpoint, "gates", None)
            if gates is not None:
                expectations = getattr(gates, gate_kind, None)

            if record is None:
                # Pre-seeding (before submission) has no durable gate
                # records: project from the preseeded expectations.
                required = bool(getattr(expectations, "required", True))
                status = (
                    STATUS_SKIPPED if blocked else STATUS_PENDING
                )
                label = _applicability_label(expectations if expectations is not None else _Required(required))
            else:
                status = _gate_projection_status(record, expectations, blocked)
                label = (
                    "N/A"
                    if _enum_value(getattr(record, "applicability", "REQUIRED"))
                    == "NOT_APPLICABLE"
                    else "REQUIRED"
                )

            items.append(
                {
                    "id": f"{cid}:{gate_kind}",
                    "content": (
                        f"{cid} · {GATE_LABELS[gate_kind]} — {label}"
                    ),
                    "status": status,
                }
            )

    return items


class _Required:  # tiny shim so _applicability_label works on raw bools
    def __init__(self, required: bool) -> None:
        self.required = required


# ---------------------------------------------------------------------------
# sync (projection only; canonical state never changes)
# ---------------------------------------------------------------------------

def _default_http(url: str, payload: bytes, timeout: float) -> tuple[int, str]:
    """PUT JSON to ``url``; return (status_code, body_text)."""
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return (
                getattr(response, "status", 200),
                body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        return (
            exc.code,
            body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body),
        )


def sync_checkpoint_todos(
    session_id: str,
    plan: Any,
    *,
    server: str = DEFAULT_SERVER,
    http: Callable[[str, bytes, float], tuple[int, str]] | None = None,
    timeout: float = 10.0,
) -> dict:
    """
    Push the rendered Todo projection to the OpenCode session todo API.

    ``http(url, payload_bytes, timeout) -> (status, body)`` is the test
    seam. Any non-2xx (4xx/5xx) or transport failure returns
    ``{"synced": False, "error": ...}`` — and in NO case does this
    function touch the canonical plan state (it never writes to the
    plan object or to disk; the Todo list is a one-way projection).
    """
    if not session_id or not str(session_id).strip():
        return {"synced": False, "error": "session_id is required"}

    todos = render_checkpoint_todos(plan)
    url = server.rstrip("/") + _TODO_PATH.format(session_id=session_id)
    payload = json.dumps(todos).encode("utf-8")

    call = http if http is not None else _default_http
    try:
        status, body = call(url, payload, timeout)
    except Exception as exc:  # noqa: BLE001 - sync must never raise
        return {"synced": False, "error": f"todo sync transport error: {exc}"}

    if isinstance(status, int) and 200 <= status < 300:
        return {
            "synced": True,
            "session_id": session_id,
            "item_count": len(todos),
            "status": status,
        }

    return {
        "synced": False,
        "error": f"todo API returned HTTP {status}: {str(body)[:300]}",
        "session_id": session_id,
    }
