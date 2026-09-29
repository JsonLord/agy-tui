"""P11 — the smallest useful checkpoint viewer (plain text, read-only).

Answers four questions for the human operator: where am I? what
passed? what is waiting for me? what happens if I approve / request
changes / cancel?

Trust rules encoded here:

* Reads CANONICAL AGY state ONLY — the submitted checkpoint-plan
  document (``<state_root>/plans/<plan_id>/checkpoint_plan.json``, with
  the ``agy_runstate`` per-plan locations ``plan_doc.json`` /
  ``plan.json`` as fallbacks) and the durable
  ``checkpoint.gate_records`` inside it. It NEVER reads (let alone
  writes) the OpenCode Todo projection — that is a projection, and in
  1.17.18 it is GET-only; faking persistence there is exactly the trap
  this viewer must not fall into.
* The viewer is READ-ONLY: no writes, no network, no model.
* Fail closed: missing or malformed plan state raises
  :class:`CheckpointViewerError` (the CLI exits non-zero with the
  message) — a corrupt display is never half-rendered.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.orchestration.checkpoint_planning.gate_engine import G4_HUMAN_GATE_ID
from app.orchestration.checkpoint_planning.schemas import (
    GATE_KINDS,
    CheckpointPlan,
)


class CheckpointViewerError(RuntimeError):
    """Canonical plan state is missing or malformed (fail closed)."""


#: candidate canonical locations, tried in order, relative to state_root
_PLAN_CANDIDATES = (
    ("plans", "{plan_id}", "checkpoint_plan.json"),
    ("runs", "{plan_id}", "plan_doc.json"),
    ("runs", "{plan_id}", "plan.json"),
    ("{plan_id}", "plan_doc.json"),
)


def find_plan_document(state_root: Path | str, plan_id: str) -> Path:
    """Locate the canonical plan document under ``state_root``.

    Raises :class:`CheckpointViewerError` when none exists — the viewer
    never invents state and never falls back to a projection.
    """
    root = Path(state_root).expanduser()
    if not root.is_dir():
        raise CheckpointViewerError(
            f"state root does not exist: {root} (canonical AGY state is "
            "the only source this viewer reads)"
        )
    for parts in _PLAN_CANDIDATES:
        candidate = root.joinpath(*(p.replace("{plan_id}", plan_id) for p in parts))
        if candidate.is_file():
            return candidate
    raise CheckpointViewerError(
        f"no canonical plan document for plan {plan_id!r} under {root} "
        f"(looked for: {'; '.join('/'.join(p) for p in _PLAN_CANDIDATES)})"
    )


def load_plan(state_root: Path | str, plan_id: str) -> CheckpointPlan:
    """Load + schema-validate the canonical plan (fail closed)."""
    path = find_plan_document(state_root, plan_id)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointViewerError(f"plan document is unreadable: {path}: {exc}") from exc
    try:
        plan = CheckpointPlan.model_validate(doc)
    except Exception as exc:  # noqa: BLE001 - any malformed state fails closed
        raise CheckpointViewerError(
            f"plan document is malformed (schema validation failed): {path}: {exc}"
        ) from exc
    if plan.plan_id != plan_id:
        raise CheckpointViewerError(
            f"plan document at {path} carries plan_id {plan.plan_id!r}, "
            f"not {plan_id!r} (canonical state is inconsistent — refusing "
            "to render)"
        )
    return plan


def render_plan(plan: CheckpointPlan, state_root: Path | str | None = None) -> str:
    """Plain-text render of the whole plan (where am I / what passed /
    what waits for me / what my options do).

    ``state_root`` (P12) additionally surfaces the planner-replan state
    of parked/repaired G4 gates — read-only, from the canonical
    per-plan runs directory.
    """
    lines: list[str] = []
    lines.append(f"PLAN {plan.plan_id}  status={plan.status}  revision={plan.revision}")
    lines.append(f"  objective: {plan.objective}")
    if not plan.checkpoints:
        lines.append("  (no checkpoints)")
    for cp in plan.checkpoints:
        lines.append("")
        lines.append(
            f"CHECKPOINT {cp.checkpoint_id}  status={cp.status.value}  "
            f"title={cp.title!r}"
        )
        if not cp.gate_records:
            lines.append("  gates: not seeded (plan not submitted/started)")
            continue
        for gate_id in GATE_KINDS:
            record = cp.gate_records.get(gate_id)
            if record is None:
                lines.append(f"  {gate_id:<16} — (absent)")
                continue
            suffix = ""
            if record.applicability != "REQUIRED":
                suffix = " [NOT_APPLICABLE]"
            lines.append(
                f"  {gate_id:<16} {record.status.value:<18}{suffix}"
            )
            lines.append(
                f"      evidence: {len(record.evidence_refs)} ref(s)"
                + (
                    f"; closed_at={record.closed_at} closed_by={record.closed_by}"
                    if record.closed_at
                    else ""
                )
            )
        g4 = cp.gate_records.get(G4_HUMAN_GATE_ID)
        if g4 is not None and g4.status.value == "WAITING_FOR_HUMAN":
            lines.append(_pending_human_block(cp.checkpoint_id))
        if g4 is not None and g4.status.value in ("REPAIR", "WAITING_FOR_HUMAN"):
            lines.extend(_planner_replan_lines(state_root, plan.plan_id, cp.checkpoint_id))
    return "\n".join(lines) + "\n"


def _planner_replan_lines(
    state_root: Path | str | None, plan_id: str, checkpoint_id: str
) -> list[str]:
    """P12: surface planner-input (replan) request status for a parked or
    repaired G4 gate. Read-only; absent state renders nothing."""
    if state_root is None:
        return []
    from app.orchestration.checkpoint_planning.planner_input_consumer import (
        requests_dir,
    )

    out: list[str] = []
    directory = requests_dir(state_root, plan_id, checkpoint_id)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        out.append(
            f"  planner replan: {doc.get('status', 'UNKNOWN')} "
            f"({doc.get('request_id', path.stem)})"
        )
    return out


def _pending_human_block(checkpoint_id: str) -> str:
    return (
        "\n"
        ">>> AWAITING YOUR DECISION (G4 human authority) — checkpoint "
        f"{checkpoint_id}\n"
        ">>> you have three legal decisions:\n"
        ">>>   APPROVE        -> G4 PASSED (terminal; only a real human "
        "approval can do this)\n"
        ">>>   NEEDS_CHANGES  -> G4 REPAIR; your verbatim feedback becomes "
        "planner input (bounded\n"
        ">>>                     repair re-runs the normal gates; no "
        "worker sees your text directly)\n"
        ">>>   CANCEL         -> G4 REJECTED (terminal, deterministic)\n"
        ">>> no model can approve on your behalf — machine evidence never "
        "closes G4."
    )


def view_checkpoint(state_root: Path | str, plan_id: str) -> str:
    """One call: locate, load, validate, render. Raises
    :class:`CheckpointViewerError` on missing/malformed canonical state."""
    root = Path(state_root).expanduser()
    return render_plan(load_plan(root, plan_id), state_root=root)


# ---------------------------------------------------------------------------
# P15-A — cross-surface discovery of EVERY parked human action
# ---------------------------------------------------------------------------


def list_parked_human_actions(state_root: Path | str) -> list[dict]:
    """Every state where a real human is the only thing that can move the
    system forward, discovered from BOTH local surfaces under
    ``state_root`` (read-only; no writes, no network, no model):

    * **G4 parked** (hard fail-closed contract): every canonical
      checkpoint plan (``plans/<plan_id>/checkpoint_plan.json``, resolved
      through :func:`find_plan_document` / :func:`load_plan` — never
      hand-parsed) whose ``G4_human`` gate record is WAITING_FOR_HUMAN.
      The question + legal decisions come from the canonical
      ``G4Executor.wait`` payload itself (single source of truth); the
      ``revision`` is the NEXT decision revision — the decisions-ledger
      max (``runs/<plan_id>/human/<checkpoint_id>/decisions/*.json``) + 1,
      the same rule the decide CLI uses.
    * **P14 parked** (bonus discovery): every
      ``projects/<project_id>/project.json`` whose ``state`` is
      WAITING_FOR_USER (a clarify artifact parks it) or NEEDS_HUMAN (a
      needs_human artifact parks it), paired with the highest-numbered
      matching artifact under ``projects/<project_id>/cloud/``. These
      files are read as PLAIN JSON on purpose: stable schema fields only
      (``state``/``mode``/``questions``/``reasoning``/``question``/
      ``context``/``options``) — the viewer never imports
      ``app.orchestration.cloud_batch`` (that package has its own
      owner). A malformed P14 project/artifact is skipped, never fatal;
      a malformed G4 canonical plan document raises
      :class:`CheckpointViewerError` (fail closed).

    Entry shapes (keys exactly):
      G4:    ``{"kind": "G4_WAITING_FOR_HUMAN", "plan_id", "checkpoint_id",
              "question", "legal_decisions", "revision"}``
      P14:   ``{"kind": "CLOUD_NEEDS_HUMAN", "project_id", "question",
              "context", "options"}`` and
             ``{"kind": "CLOUD_CLARIFY", "project_id", "questions",
              "reasoning", "options": []}``

    Results are ordered deterministically: by kind, then by id.
    """
    root = Path(state_root).expanduser()
    if not root.is_dir():
        raise CheckpointViewerError(
            f"state root does not exist: {root} (canonical AGY state is "
            "the only source this viewer reads)"
        )

    from app.orchestration.agy_runstate import safe_name
    from app.orchestration.checkpoint_planning.g4_executor import (
        build_live_g4_executor,
    )

    entries: list[dict] = []

    # -- G4 parked canonical plans (hard fail-closed contract) --------------
    plans_root = root / "plans"
    if plans_root.is_dir():
        executor = build_live_g4_executor(root)
        for plan_dir in sorted(p for p in plans_root.iterdir() if p.is_dir()):
            if not (plan_dir / "checkpoint_plan.json").is_file():
                continue
            plan = load_plan(root, plan_dir.name)
            for cp in plan.checkpoints:
                g4 = cp.gate_records.get(G4_HUMAN_GATE_ID)
                if g4 is None or g4.status.value != "WAITING_FOR_HUMAN":
                    continue
                # wait() on an already-parked gate performs no transition
                # and no I/O: it is the canonical question/options source.
                payload = executor.wait(
                    cp.model_copy(deep=True), plan_id=plan.plan_id
                )
                decisions_dir = (
                    root
                    / "runs"
                    / safe_name(plan.plan_id)
                    / "human"
                    / safe_name(cp.checkpoint_id)
                    / "decisions"
                )
                ledger_max = 0
                if decisions_dir.is_dir():
                    for path in decisions_dir.glob("*.json"):
                        try:
                            ledger_max = max(ledger_max, int(path.stem))
                        except ValueError:
                            continue
                entries.append(
                    {
                        "kind": "G4_WAITING_FOR_HUMAN",
                        "plan_id": plan.plan_id,
                        "checkpoint_id": cp.checkpoint_id,
                        "question": payload["question"],
                        "legal_decisions": payload["legal_decisions"],
                        "revision": ledger_max + 1,
                    }
                )

    # -- P14 parked cloud-batch projects (bonus discovery, lenient) ---------
    cloud_parked = {
        "WAITING_FOR_USER": ("CLOUD_CLARIFY", "clarify"),
        "NEEDS_HUMAN": ("CLOUD_NEEDS_HUMAN", "needs_human"),
    }
    projects_root = root / "projects"
    if projects_root.is_dir():
        for project_dir in sorted(p for p in projects_root.iterdir() if p.is_dir()):
            project_path = project_dir / "project.json"
            if not project_path.is_file():
                continue
            try:
                project_doc = json.loads(
                    project_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue  # malformed P14 project: skipped, never fatal
            if not isinstance(project_doc, dict):
                continue
            parked = cloud_parked.get(str(project_doc.get("state", "")))
            if parked is None:
                continue
            kind, mode = parked
            # newest matching artifact (highest NNNN among this mode): the
            # ClarificationRequest / NeedsHuman document itself.
            cloud_dir = project_dir / "cloud"
            latest_path = None
            latest_seq = -1
            if cloud_dir.is_dir():
                for artifact_path in sorted(cloud_dir.iterdir()):
                    if not artifact_path.name.endswith(".json"):
                        continue
                    seq_part, sep, mode_part = artifact_path.stem.partition("-")
                    if not sep or mode_part != mode:
                        continue
                    if not seq_part.isdigit():
                        continue
                    if int(seq_part) > latest_seq:
                        latest_seq = int(seq_part)
                        latest_path = artifact_path
            if latest_path is None:
                continue
            try:
                artifact = json.loads(latest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(artifact, dict):
                continue
            project_id = str(project_doc.get("project_id") or project_dir.name)
            if kind == "CLOUD_CLARIFY":
                questions = artifact.get("questions")
                if (
                    not isinstance(questions, list)
                    or not questions
                    or not all(isinstance(q, str) for q in questions)
                ):
                    continue
                entries.append(
                    {
                        "kind": kind,
                        "project_id": project_id,
                        "questions": [str(q) for q in questions],
                        "reasoning": str(artifact.get("reasoning") or ""),
                        "options": [],
                    }
                )
            else:
                question = artifact.get("question")
                if not isinstance(question, str) or not question.strip():
                    continue
                options = artifact.get("options")
                entries.append(
                    {
                        "kind": kind,
                        "project_id": project_id,
                        "question": question,
                        "context": str(artifact.get("context") or ""),
                        "options": (
                            [str(o) for o in options]
                            if isinstance(options, list)
                            else []
                        ),
                    }
                )

    entries.sort(
        key=lambda e: (
            e["kind"],
            e.get("plan_id", e.get("project_id", "")),
            e.get("checkpoint_id", ""),
        )
    )
    return entries
