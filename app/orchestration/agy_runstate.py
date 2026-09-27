"""P3/P4 run-state persistence: one stage, now plus the plan level.

Structured JSON under ``state/runs/<plan_id>/`` — the ``spec.md``
canonical location (this extends it; it does not create a second
state system):

* ``plan.json``            — P4 plan-level record (``save_plan`` /
  ``load_plan``): ``PlanRunResult.to_dict()`` written one level ABOVE
  the per-stage directories, at ``<root>/<plan_id>/plan.json`` (with
  ``<root>`` = ``<repo>/state/runs`` that is
  ``<repo>/state/runs/<plan_id>/plan.json``).
* ``<stage_id>/stage.json``       — ``StageRunResult.to_dict()``:
  task results, verification evidence, stage acceptance, cumulative
  gate, commit record, supervisor decision, repair lineage, failover
  events and session ids (P3 shape, unchanged).
* ``<stage_id>/tasks/<task_id>.json`` — one record per worker task:
  ``{task_id, attempt, status, validation_ok, changed_files,
  session_id, session_source, failure_reason}`` — the P4 addition is
  ``session_source`` (``stdout`` | ``known`` | ``readback`` |
  ``none``) so empty/absent session ids are auditable and the P3
  capture defect can never recur silently.
* ``<stage_id>/commit.json``      — ``{plan_id, stage_id, parent_sha,
  commit_sha, changed_files, task_results, verification, message}``.

Writes are atomic (temp file + ``os.replace``) and deterministic
(``json.dumps(sort_keys=True, indent=2)``). ``load_plan`` /
``load_stage`` / ``load_tasks`` / ``load_commit`` provide load-back
recovery: a run can be reconstructed from disk alone after a crash.

The P3 API is unchanged: ``StageRunState(root, plan_id, stage_id)``
still addressed the stage directories; ``stage_id`` is now optional
only so the plan-level writer (:class:`PlanRunState`) can share the
same class — stage operations without a ``stage_id`` raise
``ValueError`` instead of writing somewhere surprising.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str) -> str:
    """Filesystem-safe single path segment (ids stay recognizable)."""
    cleaned = _UNSAFE_NAME_RE.sub("_", value.strip()).strip("._")

    return cleaned or "unnamed"


def _atomic_write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

    return path


class StageRunState:
    """Read/write run state under ``root/<plan_id>/[<stage_id>/]``."""

    def __init__(
        self,
        root: Path | str,
        plan_id: str,
        stage_id: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.plan_id = plan_id
        self.stage_id = stage_id
        self.plan_dir = self.root / safe_name(plan_id)
        self.stage_dir = (
            self.plan_dir / safe_name(stage_id)
            if stage_id is not None
            else None
        )
        self.tasks_dir = (
            self.stage_dir / "tasks" if self.stage_dir is not None else None
        )

    # -- plan-level (P4) ---------------------------------------------------

    def save_plan(self, plan_result: Any) -> Path:
        """
        Persist the plan-level record (``plan.json``), one level above
        the per-stage directories.

        Accepts a ``PlanRunResult`` (``.to_dict()``) or an already
        built dict; the P4 driver writes an ``PLAN_IN_PROGRESS``
        record before execution and the terminal record at the end.
        """
        if hasattr(plan_result, "to_dict"):
            payload = plan_result.to_dict()
        elif isinstance(plan_result, dict):
            payload = plan_result
        else:
            raise TypeError(
                "save_plan expects a PlanRunResult or dict, got "
                f"{type(plan_result).__name__}"
            )

        return _atomic_write_json(self.plan_dir / "plan.json", payload)

    def load_plan(self) -> dict[str, Any] | None:
        """Recovery: read ``plan.json`` back (``None`` when absent)."""
        path = self.plan_dir / "plan.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- plan document (P6) ---------------------------------------------------

    PLAN_DOC_NAME = "plan_doc.json"

    def save_plan_doc(self, plan: Any) -> Path:
        """
        Persist the ORIGINAL plan document at plan start (P6): the
        ``DevelopmentPlan`` (or its plain dict form) exactly as the run
        began, at ``<plan_dir>/plan_doc.json``, so a fresh process on
        restart can load the EXACT plan that ran — resume drivers load
        ``plan_doc.json`` and never re-type the plan.

        Idempotent on rerun: writing the identical document is a
        no-op; a DIFFERENT document for the same plan_id raises
        ``ValueError`` (the original plan of record is immutable
        evidence and must never be silently overwritten).
        """
        if hasattr(plan, "model_dump"):
            payload = plan.model_dump(mode="json")
        elif isinstance(plan, dict):
            payload = plan
        else:
            raise TypeError(
                "save_plan_doc expects a DevelopmentPlan or dict, got "
                f"{type(plan).__name__}"
            )

        path = self.plan_dir / self.PLAN_DOC_NAME
        text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                return path
            raise ValueError(
                "plan document already persisted with different "
                f"content: {path} (the original plan of record is "
                "immutable; refusing to overwrite)"
            )

        self.plan_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load_plan_doc(self) -> dict[str, Any] | None:
        """Recovery: read ``plan_doc.json`` back (``None`` when absent)."""
        path = self.plan_dir / self.PLAN_DOC_NAME
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- stage-level (P3 API, unchanged) -----------------------------------

    def require_stage_dir(self) -> Path:
        """The stage directory, or ``ValueError`` without a stage_id."""
        if self.stage_dir is None:
            raise ValueError(
                "this run-state instance has no stage_id; stage files "
                "need StageRunState(root, plan_id, stage_id)"
            )
        return self.stage_dir

    def save_stage(self, result: Any) -> Path:
        """Persist the full ``StageRunResult`` (``stage.json``)."""
        stage_dir = self.require_stage_dir()
        if hasattr(result, "to_dict"):
            payload = result.to_dict()
        elif isinstance(result, dict):
            payload = result
        else:
            raise TypeError(
                "save_stage expects a StageRunResult or dict, got "
                f"{type(result).__name__}"
            )

        return _atomic_write_json(stage_dir / "stage.json", payload)

    def save_task(self, task_record: dict[str, Any]) -> Path:
        """
        Persist ONE per-task record under ``tasks/<task_id>.json``.

        Every record carries ``session_source`` (P4): an explicit
        value when the driver supplies one, otherwise ``"none"`` —
        a task record can never be missing the capture source.
        """
        if not isinstance(task_record, dict):
            raise TypeError("task records must be dicts")

        task_dir = self.require_stage_dir() / "tasks"
        task_id = (
            task_record.get("task_id")
            or task_record.get("worker_task_id")
        )

        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task record needs a task_id/worker_task_id")

        record = dict(task_record)
        record.setdefault("task_id", task_id)
        record.setdefault("session_source", "none")

        return _atomic_write_json(
            task_dir / f"{safe_name(task_id)}.json",
            record,
        )

    def save_commit(self, commit_record: dict[str, Any]) -> Path:
        """Persist the stage commit record (``commit.json``)."""
        if not isinstance(commit_record, dict):
            raise TypeError("commit records must be dicts")

        return _atomic_write_json(
            self.require_stage_dir() / "commit.json",
            commit_record,
        )

    # -- recovery reads -----------------------------------------------------

    def load_stage(self) -> dict[str, Any]:
        """Recovery: read ``stage.json`` back (raises if absent)."""
        path = self.require_stage_dir() / "stage.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def load_tasks(self) -> list[dict[str, Any]]:
        """Recovery: every per-task record, sorted by file name."""
        tasks_dir = self.require_stage_dir() / "tasks"
        if not tasks_dir.is_dir():
            return []

        records: list[dict[str, Any]] = []
        for path in sorted(tasks_dir.glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))

        return records

    def load_commit(self) -> dict[str, Any] | None:
        """Recovery: read ``commit.json`` back (None when no commit)."""
        path = self.require_stage_dir() / "commit.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


class PlanRunState(StageRunState):
    """
    Plan-level writer: same class, no ``stage_id``.

    ``PlanRunState(root, plan_id)`` persists ``plan.json`` at
    ``<root>/<plan_id>/plan.json``. Stage files are still addressed
    through a per-stage ``StageRunState`` (``execute_plan`` derives
    one from the same ``root`` automatically), so there is exactly ONE
    state system with two granularities.
    """

    def __init__(self, root: Path | str, plan_id: str) -> None:
        super().__init__(root, plan_id, None)