"""P3 minimal run-state persistence for one stage execution.

Smallest recoverable representation of a stage run, structured JSON
under ``state/runs/<plan_id>/<stage_id>/`` (the ``spec.md``
canonical location — this extends it, it does not create a second
state system):

* ``stage.json``       — ``StageRunResult.to_dict()``: task results,
  verification evidence, stage acceptance, cumulative gate, commit
  record, supervisor decision, repair lineage, failover events and
  session ids.
* ``tasks/<task_id>.json`` — one record per worker task:
  ``{task_id, attempt, status, validation_ok, changed_files,
  session_id, failure_reason}``.
* ``commit.json``      — ``{plan_id, stage_id, parent_sha,
  commit_sha, changed_files, task_results, verification, message}``.

Writes are atomic (temp file + ``os.replace``) and deterministic
(``json.dumps(sort_keys=True, indent=2)``). ``load_stage`` /
``load_tasks`` provide load-back recovery: a run can be reconstructed
from disk alone after a crash.
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
    """Read/write one stage's run state under ``root/plan/stage``."""

    def __init__(self, root: Path | str, plan_id: str, stage_id: str) -> None:
        self.root = Path(root)
        self.plan_id = plan_id
        self.stage_id = stage_id
        self.stage_dir = (
            self.root / safe_name(plan_id) / safe_name(stage_id)
        )
        self.tasks_dir = self.stage_dir / "tasks"

    # -- writes ------------------------------------------------------------

    def save_stage(self, result: Any) -> Path:
        """Persist the full ``StageRunResult`` (``stage.json``)."""
        if hasattr(result, "to_dict"):
            payload = result.to_dict()
        elif isinstance(result, dict):
            payload = result
        else:
            raise TypeError(
                "save_stage expects a StageRunResult or dict, got "
                f"{type(result).__name__}"
            )

        return _atomic_write_json(self.stage_dir / "stage.json", payload)

    def save_task(self, task_record: dict[str, Any]) -> Path:
        """Persist ONE per-task record under ``tasks/<task_id>.json``."""
        if not isinstance(task_record, dict):
            raise TypeError("task records must be dicts")

        task_id = (
            task_record.get("task_id")
            or task_record.get("worker_task_id")
        )

        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task record needs a task_id/worker_task_id")

        record = dict(task_record)
        record.setdefault("task_id", task_id)

        return _atomic_write_json(
            self.tasks_dir / f"{safe_name(task_id)}.json",
            record,
        )

    def save_commit(self, commit_record: dict[str, Any]) -> Path:
        """Persist the stage commit record (``commit.json``)."""
        if not isinstance(commit_record, dict):
            raise TypeError("commit records must be dicts")

        return _atomic_write_json(
            self.stage_dir / "commit.json",
            commit_record,
        )

    # -- recovery reads -----------------------------------------------------

    def load_stage(self) -> dict[str, Any]:
        """Recovery: read ``stage.json`` back (raises if absent)."""
        path = self.stage_dir / "stage.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def load_tasks(self) -> list[dict[str, Any]]:
        """Recovery: every per-task record, sorted by file name."""
        if not self.tasks_dir.is_dir():
            return []

        records: list[dict[str, Any]] = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))

        return records

    def load_commit(self) -> dict[str, Any] | None:
        """Recovery: read ``commit.json`` back (None when no commit)."""
        path = self.stage_dir / "commit.json"

        if not path.exists():
            return None

        return json.loads(path.read_text(encoding="utf-8"))