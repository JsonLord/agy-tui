"""P6 first-class commit-gate evidence: :class:`CommitGateRecord`.

One durable, machine-validatable record per meaningful commit — the
stage-commit gate of P3 (``execute_stage``) and the plan-level gate of
P4 (``execute_plan``) — persisted under the run directory so that a
restart (or an auditor) can prove what was checked before a commit was
accepted, without replaying the run.

Schema (EXACT field set, ``additionalProperties: false`` semantics —
``CommitGateRecord.from_dict`` rejects any extra or missing key):

``plan_id``, ``stage_id`` (string; ``"plan"`` for the plan-level
record), ``commit_sha``, ``parent_sha``, ``changed_files``,
``scope_check`` (``{"violations": [...], "ok": bool}``),
``task_verification_results`` (list of dicts verbatim from the task
results: ``task_id``, ``status``, ``validation_ok``,
``changed_files``, ``session_id``, ``session_source``),
``stage_acceptance_results`` (dict verbatim),
``cumulative_gate_results`` (dict verbatim), ``supervisor_role``
(``"supervisor"``), ``supervisor_session_id`` (``str | None`` —
``None`` is valid when the gate ran stateless; never invented),
``supervisor_decision`` (``decision``, ``rationale``, ``model``,
``deterministic_override``, ``repair_targets``),
``repair_required`` (bool), ``timestamp`` (ISO 8601),
``evidence_refs`` (paths to ``stage.json`` / ``commit.json`` / task
files relative to the run dir).

Two deterministic constraints make the record fail-closed:

* FAIL-CLOSED CONSISTENCY (documented rule): :func:`build_commit_gate_record`
  **RAISES ``ValueError``** when a record would carry ``decision ==
  "ACCEPT"`` while deterministic evidence failed (scope violations, any
  failed task verification, or failed acceptance/cumulative checks).
  The constrained decision (after the P4 deterministic override —
  ``agy_execute._normalize_review``) is the input to the builder, so
  a raw model ACCEPT with deterministic failure can never reach a
  persisted record. Building never silently "fixes" a decision: the
  writer refuses impossible evidence instead of recording it.
* SHA IMMUTABILITY (duplicate-persist guard):
  :func:`persist_commit_gate_record` writes ``commits/<sha>.json`` —
  writing the same ``commit_sha`` with identical content is a no-op;
  the same ``commit_sha`` with DIFFERENT content raises ``ValueError``
  (a SHA is immutable evidence). The plan-level record lives at
  ``commits/plan-gate.json`` and follows the same rule keyed by its
  ``commit_sha`` (a later plan gate over a NEW last-stage commit may
  supersede the file; the same plan-gate SHA may never change
  content).

Reload (:func:`load_commit_gate_records`) validates every file and
raises ``ValueError`` naming the offending file on any corrupt or
schema-violating record — never a silent skip.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RECORD_FIELDS = (
    "plan_id",
    "stage_id",
    "commit_sha",
    "parent_sha",
    "changed_files",
    "scope_check",
    "task_verification_results",
    "stage_acceptance_results",
    "cumulative_gate_results",
    "supervisor_role",
    "supervisor_session_id",
    "supervisor_decision",
    "repair_required",
    "timestamp",
    "evidence_refs",
)

SUPERVISOR_ROLE = "supervisor"
PLAN_LEVEL_STAGE_ID = "plan"
PLAN_GATE_FILE_NAME = "plan-gate.json"
COMMITS_DIR_NAME = "commits"

DECISION_FIELDS = (
    "decision",
    "rationale",
    "model",
    "deterministic_override",
    "repair_targets",
)

TASK_RESULT_FIELDS = (
    "task_id",
    "status",
    "validation_ok",
    "changed_files",
    "session_id",
    "session_source",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: Any) -> Path:
    """Same atomic pattern as ``agy_runstate`` (tmp + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


@dataclass
class CommitGateRecord:
    """Exact-schema evidence entry; see the module docstring."""

    plan_id: str
    stage_id: str
    commit_sha: str
    parent_sha: str
    changed_files: list[str]
    scope_check: dict[str, Any]
    task_verification_results: list[dict[str, Any]]
    stage_acceptance_results: dict[str, Any]
    cumulative_gate_results: dict[str, Any]
    supervisor_role: str
    supervisor_session_id: str | None
    supervisor_decision: dict[str, Any]
    repair_required: bool
    timestamp: str
    evidence_refs: list[str] = field(default_factory=list)

    # -- schema-strict (de)serialization -------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """The exact field set — nothing added, nothing removed."""
        return {
            "plan_id": self.plan_id,
            "stage_id": self.stage_id,
            "commit_sha": self.commit_sha,
            "parent_sha": self.parent_sha,
            "changed_files": list(self.changed_files),
            "scope_check": dict(self.scope_check),
            "task_verification_results": [
                dict(entry) for entry in self.task_verification_results
            ],
            "stage_acceptance_results": dict(self.stage_acceptance_results),
            "cumulative_gate_results": dict(self.cumulative_gate_results),
            "supervisor_role": self.supervisor_role,
            "supervisor_session_id": self.supervisor_session_id,
            "supervisor_decision": dict(self.supervisor_decision),
            "repair_required": bool(self.repair_required),
            "timestamp": self.timestamp,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "CommitGateRecord":
        """
        Validate + rebuild one record. ``additionalProperties: false``
        semantics: an unexpected key OR a missing key raises
        ``ValueError`` (corrupt evidence is never silently repaired).
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"commit-gate record must be a JSON object, got "
                f"{type(data).__name__}"
            )
        keys = set(data.keys())
        expected = set(RECORD_FIELDS)
        extra = sorted(keys - expected)
        missing = sorted(expected - keys)
        if extra:
            raise ValueError(
                "commit-gate record has unexpected field(s): "
                + ", ".join(extra)
            )
        if missing:
            raise ValueError(
                "commit-gate record is missing field(s): "
                + ", ".join(missing)
            )

        if not isinstance(data["changed_files"], list):
            raise ValueError("changed_files must be a list")
        if not isinstance(data["task_verification_results"], list):
            raise ValueError("task_verification_results must be a list")
        if not isinstance(data["evidence_refs"], list):
            raise ValueError("evidence_refs must be a list")
        for key in ("scope_check", "stage_acceptance_results",
                    "cumulative_gate_results", "supervisor_decision"):
            if not isinstance(data[key], dict):
                raise ValueError(f"{key} must be a dict")
        if not isinstance(data["repair_required"], bool):
            raise ValueError("repair_required must be a bool")
        if not isinstance(data["supervisor_session_id"], (str, type(None))):
            raise ValueError("supervisor_session_id must be a str or None")
        if data["supervisor_role"] != SUPERVISOR_ROLE:
            raise ValueError(
                "supervisor_role must be 'supervisor', got "
                f"{data['supervisor_role']!r}"
            )

        return cls(
            plan_id=str(data["plan_id"]),
            stage_id=str(data["stage_id"]),
            commit_sha=str(data["commit_sha"]),
            parent_sha=str(data["parent_sha"]),
            changed_files=[str(p) for p in data["changed_files"]],
            scope_check=dict(data["scope_check"]),
            task_verification_results=[
                dict(entry) for entry in data["task_verification_results"]
            ],
            stage_acceptance_results=dict(data["stage_acceptance_results"]),
            cumulative_gate_results=dict(data["cumulative_gate_results"]),
            supervisor_role=data["supervisor_role"],
            supervisor_session_id=data["supervisor_session_id"],
            supervisor_decision=dict(data["supervisor_decision"]),
            repair_required=data["repair_required"],
            timestamp=str(data["timestamp"]),
            evidence_refs=[str(r) for r in data["evidence_refs"]],
        )


# ---------------------------------------------------------------------------
# Building (pure)
# ---------------------------------------------------------------------------


def _gate_failures(*,
                   violations: list[str],
                   task_results: list[dict],
                   acceptance: dict,
                   cumulative: dict) -> list[str]:
    """Deterministic failed checks of a gate (P4 override parity)."""
    failures = [
        f"scope violation: {path}"
        for path in violations
    ]
    for entry in task_results:
        if not entry.get("validation_ok", False):
            failures.append(
                f"task verification failed: "
                f"{entry.get('task_id') or entry.get('worker_task_id')}"
            )
    for label, gate in (
        ("stage acceptance failed", acceptance),
        ("cumulative gate failed", cumulative),
    ):
        for check in gate.get("checks", []) if isinstance(gate, dict) else []:
            if isinstance(check, dict) and not check.get("ok", False):
                failures.append(
                    f"{label}: {check.get('command')} exited "
                    f"{check.get('exit_code')}"
                )
        if isinstance(gate, dict) and gate.get("ok") is False and (
            not gate.get("checks")
        ):
            failures.append(f"{label} (ok=false)")
    return failures


def _verification_projection(task_results: list[dict]) -> list[dict]:
    """Verbatim subset per contract: the six TASK_RESULT_FIELDS keys."""
    out: list[dict] = []
    for entry in task_results:
        out.append(
            {
                "task_id": entry.get("task_id")
                or entry.get("worker_task_id"),
                "status": entry.get("status"),
                "validation_ok": entry.get("validation_ok"),
                "changed_files": list(entry.get("changed_files", []) or []),
                "session_id": entry.get("session_id"),
                "session_source": entry.get("session_source"),
            }
        )
    return out


def _decision_subset(decision: Any) -> dict[str, Any]:
    """The five contract decision keys (never raw model prose dumps)."""
    if not isinstance(decision, dict):
        return {
            "decision": None,
            "rationale": "",
            "model": None,
            "deterministic_override": False,
            "repair_targets": [],
        }
    return {
        "decision": decision.get("decision"),
        "rationale": str(decision.get("rationale") or ""),
        "model": decision.get("model")
        if isinstance(decision.get("model"), str)
        else None,
        "deterministic_override": bool(
            decision.get("deterministic_override", False)
        ),
        "repair_targets": [
            str(t) for t in decision.get("repair_targets", []) or []
        ],
    }


def _stage_evidence_refs(stage_id: str, task_results: list[dict]) -> list[str]:
    refs = [f"{stage_id}/stage.json", f"{stage_id}/commit.json"]
    for entry in task_results:
        task_id = entry.get("task_id") or entry.get("worker_task_id")
        if task_id:
            refs.append(f"{stage_id}/tasks/{task_id}.json")
    return refs


def build_commit_gate_record(
    plan: Any,
    stage: Any,
    stage_result: Any,
    *,
    plan_level: bool = False,
) -> CommitGateRecord:
    """
    Build the gate record (pure). The ``supervisor_decision`` passed
    in MUST be the CONSTRAINED decision (P4 deterministic override
    already applied).

    FAIL-CLOSED CONSISTENCY (documented rule, chosen variant): this
    builder **raises ``ValueError``** when ``decision == "ACCEPT"``
    while deterministic evidence failed (``scope_check.violations``
    non-empty, any failed task verification, or any failed
    acceptance/cumulative check). ``build`` refuses impossible
    evidence rather than rewriting a decision — mirroring the P4
    override rule whose whole job is that state-machine state making a
    raw ACCEPT with deterministic failure unreachable.

    Stage level (``plan_level=False``): ``stage`` is the ``PlanStage``,
    ``stage_result`` the ``StageRunResult.to_dict()``-shaped mapping
    (``commit`` P3 record included). Plan level: ``stage_result`` is
    the ``PlanRunResult.to_dict()`` mapping augmented by the driver
    with the raw gate entries (``final_acceptance``, ``cumulative``),
    ``stage`` may be ``None`` (the last ordered stage's commit is
    derived from ``stage_result["stage_results"]``); ``stage_id`` is
    always ``"plan"``.
    """
    plan_id = getattr(plan, "plan_id", None) or (
        stage_result.get("plan_id") if isinstance(stage_result, dict) else None
    )
    if not plan_id:
        raise ValueError("cannot build a commit-gate record without plan_id")

    if plan_level:
        return _build_plan_level_record(
            plan, stage, stage_result, str(plan_id)
        )

    if stage is None or not isinstance(stage_result, dict):
        raise ValueError(
            "stage-level build needs a PlanStage and a stage-result dict"
        )

    commit = stage_result.get("commit") or {}
    commit_sha = str(commit.get("commit_sha") or "")
    parent_sha = str(commit.get("parent_sha") or "")
    violations = [str(v) for v in stage_result.get("scope_violations", [])]
    task_results = list(stage_result.get("task_results", []) or [])
    acceptance = dict(stage_result.get("stage_acceptance") or {})
    cumulative = dict(stage_result.get("cumulative_gate") or {})
    decision = _decision_subset(stage_result.get("supervisor_decision"))

    scope_check = {"violations": violations, "ok": not violations}
    failures = _gate_failures(
        violations=violations,
        task_results=task_results,
        acceptance=acceptance,
        cumulative=cumulative,
    )

    if decision.get("decision") == "ACCEPT" and failures:
        raise ValueError(
            "FAIL-CLOSED: refusing to build a commit-gate record with "
            "decision='ACCEPT' while deterministic checks failed "
            "(the constrained decision — deterministic_override applied "
            "— must be recorded): " + "; ".join(failures)
        )

    repair_required = bool(failures) or decision.get("decision") != "ACCEPT"

    # supervisor_session_id: only ever what the gate actually carried.
    raw_decision = stage_result.get("supervisor_decision") or {}
    supervisor_session_id = raw_decision.get("session_id") if isinstance(
        raw_decision, dict
    ) else None
    if supervisor_session_id is not None:
        supervisor_session_id = str(supervisor_session_id)

    stage_id = getattr(stage, "stage_id", None) or stage_result.get(
        "stage_id"
    )
    if not stage_id:
        raise ValueError("stage-level record needs a stage_id")

    return CommitGateRecord(
        plan_id=str(plan_id),
        stage_id=str(stage_id),
        commit_sha=commit_sha,
        parent_sha=parent_sha,
        changed_files=[str(p) for p in stage_result.get("changed_files", [])],
        scope_check=scope_check,
        task_verification_results=_verification_projection(task_results),
        stage_acceptance_results=acceptance,
        cumulative_gate_results=cumulative,
        supervisor_role=SUPERVISOR_ROLE,
        supervisor_session_id=supervisor_session_id,
        supervisor_decision=decision,
        repair_required=repair_required,
        timestamp=_now(),
        evidence_refs=_stage_evidence_refs(str(stage_id), task_results),
    )


def _build_plan_level_record(
    plan: Any,
    stage: Any,
    stage_result: dict,
    plan_id: str,
) -> CommitGateRecord:
    """Plan-level record keyed by the LAST stage commit (see above)."""
    order = [
        s.stage_id
        for s in (stage_execution_order_of(plan))
    ]
    results = stage_result.get("stage_results") or {}
    last_stage_id = None
    for candidate in order:
        entry = results.get(candidate) or {}
        if entry.get("commit"):
            last_stage_id = candidate
    if last_stage_id is None and stage is not None:
        last_stage_id = getattr(stage, "stage_id", stage)

    last_entry = dict(results.get(last_stage_id) or {}) if last_stage_id else {}
    last_commit = dict(last_entry.get("commit") or {})

    final_acceptance = dict(
        stage_result.get("final_acceptance") or {}
    )
    cumulative_input = stage_result.get("cumulative")
    if isinstance(cumulative_input, dict):
        cumulative = dict(cumulative_input)
    else:
        cumulative = {
            "ok": bool(stage_result.get("cumulative_invariants_ok")),
        }
    decision = _decision_subset(stage_result.get("plan_gate"))

    violations = [str(v) for v in last_entry.get("scope_violations", [])]
    scope_check = {"violations": violations, "ok": not violations}

    # Plan-level gate checks are the final-acceptance + cumulative
    # checks; task verification at plan level is the last stage's
    # persisted verification (plan-level gate runs no new tasks).
    task_results = list(last_entry.get("task_results", []) or [])
    failures = _gate_failures(
        violations=[],
        task_results=task_results,
        acceptance=final_acceptance,
        cumulative=cumulative,
    )

    if decision.get("decision") == "ACCEPT" and (
        failures or violations
    ):
        raise ValueError(
            "FAIL-CLOSED: refusing to build a plan-level commit-gate "
            "record with decision='ACCEPT' while deterministic checks "
            "failed: " + "; ".join(failures or violations)
        )

    repair_required = bool(failures) or decision.get("decision") != "ACCEPT"

    evidence_refs = ["plan.json", "plan_doc.json"]
    if last_stage_id:
        evidence_refs = [f"{last_stage_id}/stage.json",
                        f"{last_stage_id}/commit.json"] + evidence_refs
    for entry in task_results:
        task_id = entry.get("task_id") or entry.get("worker_task_id")
        if task_id and last_stage_id:
            evidence_refs.append(f"{last_stage_id}/tasks/{task_id}.json")

    return CommitGateRecord(
        plan_id=plan_id,
        stage_id=PLAN_LEVEL_STAGE_ID,
        commit_sha=str(last_commit.get("commit_sha") or ""),
        parent_sha=str(last_commit.get("parent_sha") or ""),
        changed_files=[str(p) for p in last_commit.get("changed_files", [])],
        scope_check=scope_check,
        task_verification_results=_verification_projection(task_results),
        stage_acceptance_results=final_acceptance,
        cumulative_gate_results=cumulative,
        supervisor_role=SUPERVISOR_ROLE,
        supervisor_session_id=None,
        supervisor_decision=decision,
        repair_required=repair_required,
        timestamp=_now(),
        evidence_refs=evidence_refs,
    )


def stage_execution_order_of(plan: Any) -> list[Any]:
    """Topological stage order (schema-owned algorithm)."""
    getter = getattr(plan, "stage_execution_order", None)
    if callable(getter):
        ordered: Any = getter()
        return list(ordered)
    from app.orchestration.plan_decompose import stage_execution_order

    return list(stage_execution_order(plan))


# ---------------------------------------------------------------------------
# Persistence + reload
# ---------------------------------------------------------------------------


def record_path(run_state_dir: Path, record: CommitGateRecord) -> Path:
    """``commits/plan-gate.json`` for the plan level, else ``commits/<sha>.json``."""
    commits_dir = Path(run_state_dir) / COMMITS_DIR_NAME
    if record.stage_id == PLAN_LEVEL_STAGE_ID:
        return commits_dir / PLAN_GATE_FILE_NAME
    sha = record.commit_sha.strip()
    if not sha:
        raise ValueError(
            f"commit-gate record for stage {record.stage_id!r} has no "
            "commit_sha; nothing immutable to name the file after"
        )
    return commits_dir / f"{sha}.json"


def _evidence_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Record content WITHOUT ``timestamp``: two builds of the SAME
    evidence at different wall-clock moments are the same evidence,
    while every evidence-bearing field must match exactly.
    """
    return {
        key: value for key, value in payload.items() if key != "timestamp"
    }


def persist_commit_gate_record(
    run_state_dir: Path,
    record: CommitGateRecord,
) -> Path:
    """
    Atomically persist one record under ``<run dir>/commits/``.

    Duplicate-persist guard (SHA immutability): writing the same
    ``commit_sha`` twice with EQUIVALENT content (ignoring only the
    wall-clock ``timestamp``) is a no-op; with DIFFERENT evidence it
    raises ``ValueError``. The plan-level file
    (``plan-gate.json``) follows a documented exception: a re-run
    plan gate may supersede it even over the same last-stage commit
    (the plan gate is not a git object; full history lives in
    ``plan.json``'s ``plan_gate_reviews``).
    """
    run_state_dir = Path(run_state_dir)
    path = record_path(run_state_dir, record)
    payload = record.to_dict()

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(
                f"corrupt commit-gate record at {path.name}: {exc}"
            ) from exc
        if not isinstance(existing, dict):
            raise ValueError(
                f"corrupt commit-gate record at {path.name}: "
                "record is not a JSON object"
            )

        if record.stage_id == PLAN_LEVEL_STAGE_ID:
            # Plan-level exception (documented): the plan gate is NOT
            # a git object — a re-run gate (bounded repair cycles)
            # legitimately supersedes plan-gate.json, even over the
            # same last-stage commit. Every historical verdict remains
            # auditable in plan.json's plan_gate_reviews.
            if _evidence_fingerprint(existing) == _evidence_fingerprint(
                payload
            ):
                return path  # equivalent re-persist -> no-op
            return _atomic_write_json(path, payload)

        if _evidence_fingerprint(existing) == _evidence_fingerprint(
            payload
        ):
            return path  # idempotent: same SHA + same evidence

        if str(existing.get("commit_sha")) == payload["commit_sha"]:
            raise ValueError(
                "conflicting content for immutable commit "
                f"{payload['commit_sha']!r} at {path} (a SHA is "
                "immutable evidence; refusing to overwrite)"
            )
        raise ValueError(
            "refusing to overwrite an existing different record at "
            f"{path} (commit {payload['commit_sha']!r})"
        )

    return _atomic_write_json(path, payload)


def load_commit_gate_records(run_state_dir: Path) -> list[CommitGateRecord]:
    """
    Reload + validate every persisted record, sorted by file name.
    A corrupt file (bad JSON, schema violation, unexpected field)
    raises ``ValueError`` naming the file — never a silent skip.
    """
    commits_dir = Path(run_state_dir) / COMMITS_DIR_NAME
    if not commits_dir.is_dir():
        return []

    records: list[CommitGateRecord] = []
    for path in sorted(commits_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(
                f"corrupt commit-gate record {path.name}: invalid JSON "
                f"({exc})"
            ) from exc
        try:
            records.append(CommitGateRecord.from_dict(payload))
        except ValueError as exc:
            raise ValueError(
                f"corrupt commit-gate record {path.name}: {exc}"
            ) from exc
    return records