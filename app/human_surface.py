"""P15-A — the thin local human surface (TUI/CLI) over canonical authority.

This module is the human-facing boundary: it renders what waits for a
human, collects a typed verdict, and turns it into exactly one canonical
call. It owns NO gate/admission transition logic — that lives in the
canonical executors/services, which validate everything again.

Trust rules encoded here:

* PROVENANCE GUARD (:func:`require_human_actor`): every actor/approver
  leaving this surface must be a non-empty string starting with exactly
  ``human:``. The canonical engine does not police the prefix itself —
  the SURFACE is the authority boundary for where human decisions may
  come from — so this check fails closed (:class:`HumanAuthorityError`)
  BEFORE any state is read or written.
* DELEGATION: the G4 transition is executed ENTIRELY by the canonical
  ``G4Executor.apply_human_decision`` (via ``build_live_g4_executor``);
  admission ENTIRELY by the canonical ``continuation_admission.
  admit_continuation``. This module loads canonical state (fail closed),
  defaults the revision (decisions-ledger max + 1 — the exact rule in
  ``app/main.py::_decisions_ledger_max``), builds the records with
  ``provenance="tui"``, and mirrors the durable gate state back into the
  plan document exactly like ``_checkpoint_decide`` does.
* The human's NEEDS_CHANGES feedback travels VERBATIM — never rewritten,
  never interpreted — from the reader to the canonical executor (which
  turns it into a ``HumanPlannerInputRequest``).

No network, no model calls, no sleeps, no polling — anywhere in this
module (the only default I/O is the injectable ``input`` reader).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.checkpoint_planning.checkpoint_viewer import (
    CheckpointViewerError,
    find_plan_document,
    load_plan,
)
from app.orchestration.checkpoint_planning.g4_executor import (
    G4_GATE_ID,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.schemas import (
    G0Approval,
    HumanDecision,
    HumanGateRecord,
)

#: Every actor minted through a human surface MUST carry this prefix.
HUMAN_ACTOR_PREFIX = "human:"

#: What the collector accepts (case-insensitive), both the CLI-hyphen and
#: the enum-underscore spellings of NEEDS_CHANGES.
_DECISION_TOKENS = {
    "approve": HumanDecision.APPROVE,
    "needs-changes": HumanDecision.NEEDS_CHANGES,
    "needs_changes": HumanDecision.NEEDS_CHANGES,
    "cancel": HumanDecision.CANCEL,
}


class HumanAuthorityError(RuntimeError):
    """An actor/approver is not human-sourced (fail closed, no state touched)."""


def require_human_actor(actor: str) -> str:
    """The PROVENANCE GUARD: return ``actor`` iff it is human-sourced.

    A human-sourced actor is a non-empty string starting with exactly
    ``"human:"`` (``human:cli``, ``human:tui``, ``human:remote`` pass;
    ``model``, ``assistant:opus``, ``agent:agy-dispatch``, ``""`` fail).
    Raises :class:`HumanAuthorityError` — never persists, never trusts.
    """
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or not actor.startswith(HUMAN_ACTOR_PREFIX)
    ):
        raise HumanAuthorityError(
            f"non-human authority rejected: actor {actor!r} must be a "
            f"non-empty string prefixed {HUMAN_ACTOR_PREFIX!r} — only a "
            "real human may drive a decision through this surface"
        )
    return actor


# ---------------------------------------------------------------------------
# Rendering (read-only)
# ---------------------------------------------------------------------------


def render_pending(entries: list[dict]) -> str:
    """Plain-text render of ``list_parked_human_actions`` output: what
    waits for the human, and what their options are. Read-only."""
    if not entries:
        return "nothing waits for a human: no parked G4 gates, no parked cloud projects\n"
    lines: list[str] = [f"PARKED HUMAN ACTIONS ({len(entries)})"]
    for entry in entries:
        kind = str(entry.get("kind", "UNKNOWN"))
        if kind == "G4_WAITING_FOR_HUMAN":
            lines.append("")
            lines.append(
                f"[G4] plan={entry.get('plan_id')} "
                f"checkpoint={entry.get('checkpoint_id')} "
                f"next revision={entry.get('revision')}"
            )
            lines.append(f"  question: {entry.get('question', '')}")
            lines.append("  your decision (one of):")
            for option in entry.get("legal_decisions", []):
                choice = str(option.get("decision", "")).lower().replace(
                    "_", "-"
                )
                lines.append(f"    {choice:<14} {option.get('effect', '')}")
        elif kind == "CLOUD_NEEDS_HUMAN":
            lines.append("")
            lines.append(
                f"[CLOUD] project {entry.get('project_id')} needs a human"
            )
            lines.append(f"  question: {entry.get('question', '')}")
            if entry.get("context"):
                lines.append(f"  context: {entry['context']}")
            options = entry.get("options") or []
            if options:
                lines.append("  options: " + " | ".join(str(o) for o in options))
        elif kind == "CLOUD_CLARIFY":
            lines.append("")
            lines.append(
                f"[CLOUD] project {entry.get('project_id')} awaits "
                "clarification"
            )
            for question in entry.get("questions", []):
                lines.append(f"  - {question}")
            if entry.get("reasoning"):
                lines.append(f"  reasoning: {entry['reasoning']}")
        else:  # pragma: no cover - defensive forward-compat render
            lines.append("")
            lines.append(f"[?] unrecognized parked entry: {kind}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Collecting (the ONLY reader-driven piece; no other I/O)
# ---------------------------------------------------------------------------


def collect_g4_decision(
    reader: Callable[[str], str],
    question: str,
    legal: list[dict],
) -> tuple[HumanDecision, str]:
    """Read one G4 verdict (plus VERBATIM feedback for needs-changes).

    ``reader`` is injectable (default call sites pass ``input``); it is
    called with a prompt string and must return one line. The verdict is
    ``approve``/``needs-changes``/``cancel`` (case-insensitive); anything
    else raises ``ValueError``. For ``needs-changes`` the reader is called
    repeatedly until an empty line, and the collected feedback is joined
    with newlines VERBATIM (never rewritten, never interpreted). Returns
    ``(decision, feedback)`` — feedback is ``""`` for approve/cancel.
    """
    choices = [
        str(option.get("decision", "")).lower().replace("_", "-")
        for option in (legal or [])
        if isinstance(option, dict) and option.get("decision")
    ] or ["approve", "needs-changes", "cancel"]
    raw = str(reader(f"{question}\nyour decision ({' | '.join(choices)}): ") or "")
    token = raw.strip().lower()
    decision = _DECISION_TOKENS.get(token)
    if decision is None:
        raise ValueError(
            f"invalid decision {raw.strip()!r} — legal inputs are "
            f"{', '.join(choices)} (case-insensitive)"
        )
    if decision != HumanDecision.NEEDS_CHANGES:
        return decision, ""

    feedback_lines: list[str] = []
    prompt = (
        "feedback (stored VERBATIM as planner input; empty line finishes) "
        "> "
    )
    while True:
        line = str(reader(prompt))
        if line.strip() == "":
            break
        feedback_lines.append(line)
    feedback = "\n".join(feedback_lines)
    if not feedback.strip():
        raise ValueError(
            "needs-changes without feedback fails closed — the planner "
            "has nothing to interpret (canonical rule)"
        )
    return decision, feedback


# ---------------------------------------------------------------------------
# Applying (thin wrappers: guard -> canonical state -> canonical delegate)
# ---------------------------------------------------------------------------


def _coerce_decision(decision: HumanDecision | str) -> HumanDecision:
    if isinstance(decision, HumanDecision):
        return decision
    if isinstance(decision, str):
        return HumanDecision(decision.strip().replace("-", "_").upper())
    raise ValueError(f"invalid decision: {decision!r}")


def _decisions_ledger_max(human_dir: Path) -> int:
    """Highest revision present in the per-checkpoint decisions ledger
    (``decisions/<revision>.json`` file names); 0 when no ledger. The
    exact rule of ``app/main.py::_decisions_ledger_max`` (kept as a local
    copy: importing the CLI module from the library layer would invert
    the dependency, and the CLI owns its own copy)."""
    latest = 0
    if human_dir.is_dir():
        for path in (human_dir / "decisions").glob("*.json"):
            try:
                latest = max(latest, int(path.stem))
            except ValueError:
                continue
    return latest


def apply_g4_decision(
    state_root: Path | str,
    plan_id: str,
    checkpoint_id: str,
    decision: HumanDecision | str,
    feedback: str,
    actor: str = "human:tui",
    revision: int | None = None,
) -> dict:
    """Submit one real human G4 decision through the canonical executor.

    THIN by contract: provenance guard FIRST (nothing is even read for a
    non-human actor), then canonical plan load (fail closed via
    :class:`CheckpointViewerError`), revision default = decisions-ledger
    max + 1, the WHOLE transition delegated to
    ``build_live_g4_executor(...).apply_human_decision`` (the canonical
    authority — on ``GateTransitionError`` it persisted nothing and the
    error propagates; the CLI maps it to exit 1), then the durable gate
    records are reflected back into the canonical plan doc exactly like
    ``_checkpoint_decide`` does so the viewer never reads a stale
    snapshot. Returns the executor's result summary.
    """
    actor = require_human_actor(actor)
    decision = _coerce_decision(decision)
    feedback = "" if feedback is None else str(feedback)

    plan = load_plan(state_root, plan_id)
    checkpoint = next(
        (cp for cp in plan.checkpoints if cp.checkpoint_id == checkpoint_id),
        None,
    )
    if checkpoint is None:
        raise CheckpointViewerError(
            f"plan {plan_id!r} has no checkpoint {checkpoint_id!r} "
            "(canonical state is the only source — refusing to invent one)"
        )

    if revision is None:
        human_dir = (
            Path(state_root).expanduser()
            / "runs"
            / safe_name(plan.plan_id)
            / "human"
            / safe_name(checkpoint.checkpoint_id)
        )
        revision = _decisions_ledger_max(human_dir) + 1

    record = HumanGateRecord(
        plan_id=plan.plan_id,
        checkpoint_id=checkpoint.checkpoint_id,
        gate_id=G4_GATE_ID,
        decision=decision,
        feedback=feedback,  # VERBATIM human text
        submitted_at=datetime.now(timezone.utc).isoformat(),
        actor=actor,
        provenance="tui",
        revision=revision,
    )

    # The canonical G4 transition — ALL validation/ledger/request
    # creation lives in the executor + engine, not in this surface.
    executor = build_live_g4_executor(state_root)
    result = executor.apply_human_decision(checkpoint, record)

    _reflect_gate_records(state_root, plan.plan_id, checkpoint)
    return result


def _reflect_gate_records(
    state_root: Path | str, plan_id: str, checkpoint
) -> None:
    """Mirror ``app/main.py::_checkpoint_decide``'s plan-doc reflection:
    write the durable gate records the executor produced back into the
    canonical plan document."""
    plan_path = find_plan_document(state_root, plan_id)
    doc = json.loads(plan_path.read_text(encoding="utf-8"))
    gate_records = {
        gate_id: rec.model_dump(mode="json")
        for gate_id, rec in checkpoint.gate_records.items()
    }
    for cp in doc.get("checkpoints", []):
        if cp.get("checkpoint_id") == checkpoint.checkpoint_id:
            cp["gate_records"] = gate_records
    _atomic_write_json(plan_path, doc)


def admit_via_surface(
    state_root: Path | str,
    plan_id: str,
    request_id: str,
    revision: int,
    approver: str = "human:tui",
) -> dict:
    """Admit one validated continuation through the canonical G0 path.

    THIN by contract: :func:`require_human_actor` first — a non-human
    approver raises :class:`HumanAuthorityError` before anything is
    persisted — then the ENTIRE admission is delegated to the canonical
    ``continuation_admission.admit_continuation`` (provenance,
    re-validation, revision match, duplicate detection, submission). Any
    ``ContinuationAdmissionError`` propagates unchanged (the CLI maps it
    to exit 1). Returns the admission result summary dict.
    """
    from app.orchestration.checkpoint_planning.continuation_admission import (
        admit_continuation,
    )

    approver = require_human_actor(approver)
    approval = G0Approval(
        decision="APPROVE",
        revision=revision,
        approver=approver,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    result = admit_continuation(
        state_root, plan_id, request_id, g0_approval=approval
    )
    return {
        "accepted": result.accepted,
        "idempotent": result.idempotent,
        "plan_id": result.plan_id,
        "revision": result.revision,
        "next_gate": result.next_gate,
        "runtime_owner": result.runtime_owner,
        "approver": result.approver,
        "admissions_record": str(result.admissions_record or ""),
    }
