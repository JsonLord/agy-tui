"""P14 — Project-level durable state (one bounded cloud artifact per turn).

Layout (under ``<state_root>/projects/<project_id>/``)::

    project.json                    canonical ProjectContract (state of record)
    cloud/
      0001-clarify.json             exactly one persisted cloud action per
      0002-batch.json               invocation; the counter is monotonic and
      0003-batch.json               never reused -> restarts cannot replay a
      0004-final.json               turn, and the call count is auditable
    batches/
      batch-<seq>/
        plan.json                   validated BatchPlan for this batch
        workers/
          <delegation>.json         compact WorkerOutcome (cloud-facing)
          <delegation>.raw.json     raw local evidence (NEVER cloud-facing)
        outcome.json                compact BatchOutcome (<= 16 KB)
        reentry.json                deterministic reentry decision + the
                                    persisted generation/call counter that
                                    enforces at-most-one cloud call per
                                    reentry generation across restarts

All writes go through the existing atomic helper
``app.orchestration.agy_runstate._atomic_write_json`` (reused, not
reimplemented). The store is synchronous, stdlib + pydantic only, and
fail-closed on malformed documents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.cloud_batch.schemas import (
    BatchOutcome,
    BatchPlan,
    CloudAction,
    CloudProjectState,
    ProjectContract,
    WorkerOutcome,
)

_CLOUD_FILE_RE = re.compile(r"^(\d{4})-(clarify|batch|needs_human|final)\.json$")


def _jsonable(payload: Any) -> Any:
    """Pydantic models -> JSON-serializable (the shared atomic writer does
    plain ``json.dumps``; P8-P13 hand it dicts)."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return payload


def _write(path: Path, payload: Any) -> Path:
    """Atomic JSON write through the shared helper, with models dumped."""
    return _atomic_write_json(path, _jsonable(payload))


class ProjectStoreError(ValueError):
    """Fail-closed error for malformed/missing canonical project state."""


class ProjectStore:
    """Bounded, atomic, restart-safe persistence for one project root.

    ``state_root`` is the directory that will contain ``projects/``
    (mirrors the P8-P13 ``state_root`` convention; tests use tmp roots).
    """

    def __init__(self, state_root: str | Path) -> None:
        self.state_root = Path(state_root).expanduser()
        self.projects_root = self.state_root / "projects"

    # -- paths ---------------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        return self.projects_root / safe_name(project_id)

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def cloud_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "cloud"

    def batches_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "batches"

    @staticmethod
    def batch_dir_name(batch_id: str) -> str:
        """``batch-001`` — zero-padded sequence derived from the batch id
        (which is ``batch-001``-shaped per the schema regex)."""
        return safe_name(batch_id)

    def batch_dir(self, project_id: str, batch_id: str) -> Path:
        return self.batches_dir(project_id) / self.batch_dir_name(batch_id)

    # -- project record ------------------------------------------------------

    def create_project(self, contract: ProjectContract) -> Path:
        if self.project_path(contract.project_id).exists():
            raise ProjectStoreError(
                f"project {contract.project_id!r} already exists"
            )
        self.project_dir(contract.project_id).mkdir(parents=True, exist_ok=True)
        self.cloud_dir(contract.project_id).mkdir(parents=True, exist_ok=True)
        self.batches_dir(contract.project_id).mkdir(parents=True, exist_ok=True)
        return _write(self.project_path(contract.project_id), contract)

    def load_project(self, project_id: str) -> ProjectContract:
        path = self.project_path(project_id)
        if not path.exists():
            raise ProjectStoreError(f"no project {project_id!r} at {path.parent}")
        try:
            return ProjectContract.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ProjectStoreError(f"project doc invalid: {exc}") from exc

    def save_project(self, contract: ProjectContract) -> None:
        _write(self.project_path(contract.project_id), contract)

    # -- cloud artifacts (one per invocation, monotonic counter) --------------

    def next_cloud_seq(self, project_id: str) -> int:
        """1-based next cloud artifact number (restart-safe: derived from
        existing files, never from process-local state)."""
        cdir = self.cloud_dir(project_id)
        if not cdir.is_dir():
            return 1
        seqs: list[int] = []
        for p in cdir.iterdir():
            m = _CLOUD_FILE_RE.fullmatch(p.name)
            if m:
                seqs.append(int(m.group(1)))
        return max(seqs, default=0) + 1

    def cloud_call_count(self, project_id: str) -> int:
        return self.next_cloud_seq(project_id) - 1

    def save_cloud_action(
        self, project_id: str, action: CloudAction, mode: str
    ) -> Path:
        """Persist exactly ONE cloud action artifact for this invocation.

        ``mode`` is the CloudMode value (clarify/batch/needs_human/final);
        the action's own ``mode`` field must match it (double-checked,
        fail closed)."""
        actual = str(getattr(action, "mode", "")).rsplit(".", 1)[-1].lower()
        if actual != mode:
            raise ProjectStoreError(
                f"cloud artifact mode mismatch: file says {mode!r}, "
                f"action says {actual!r}"
            )
        seq = self.next_cloud_seq(project_id)
        path = self.cloud_dir(project_id) / f"{seq:04d}-{mode}.json"
        if path.exists():
            raise ProjectStoreError(f"cloud artifact already exists: {path.name}")
        _write(path, action)
        return path

    # -- batches ---------------------------------------------------------------

    def save_batch_plan(
        self, project_id: str, batch_id: str, plan: BatchPlan
    ) -> Path:
        bdir = self.batch_dir(project_id, batch_id)
        (bdir / "workers").mkdir(parents=True, exist_ok=True)
        return _write(bdir / "plan.json", plan)

    def load_batch_plan(self, project_id: str, batch_id: str) -> BatchPlan:
        path = self.batch_dir(project_id, batch_id) / "plan.json"
        if not path.exists():
            raise ProjectStoreError(f"no batch plan for {batch_id!r}")
        try:
            return BatchPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise ProjectStoreError(f"batch plan invalid: {exc}") from exc

    def save_worker_outcome(
        self, project_id: str, batch_id: str, outcome: WorkerOutcome
    ) -> Path:
        path = self.batch_dir(project_id, batch_id) / "workers" / (
            safe_name(outcome.delegation_id) + ".json"
        )
        return _write(path, outcome)

    def save_worker_raw(
        self, project_id: str, batch_id: str, delegation_id: str, raw: Any
    ) -> str:
        """Raw evidence stays local-only; returns the relative ref path."""
        path = (
            self.batch_dir(project_id, batch_id)
            / "workers"
            / (safe_name(delegation_id) + ".raw.json")
        )
        _write(path, raw)
        return str(path)

    def load_worker_outcomes(self, project_id: str, batch_id: str) -> dict[str, WorkerOutcome]:
        wdir = self.batch_dir(project_id, batch_id) / "workers"
        out: dict[str, WorkerOutcome] = {}
        if not wdir.is_dir():
            return out
        for p in sorted(wdir.iterdir()):
            if p.name.endswith(".raw.json") or not p.name.endswith(".json"):
                continue
            try:
                wo = WorkerOutcome.model_validate(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue  # partial write of an in-flight worker is not loadable
            out[wo.delegation_id] = wo
        return out

    def save_batch_outcome(
        self, project_id: str, batch_id: str, outcome: BatchOutcome
    ) -> Path:
        return _write(
            self.batch_dir(project_id, batch_id) / "outcome.json", outcome
        )

    # -- reentry records --------------------------------------------------------

    def load_reentry(self, project_id: str, batch_id: str) -> dict:
        path = self.batch_dir(project_id, batch_id) / "reentry.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectStoreError(f"reentry record invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectStoreError("reentry record must be an object")
        return data

    def save_reentry(self, project_id: str, batch_id: str, record: dict) -> None:
        _write(
            self.batch_dir(project_id, batch_id) / "reentry.json", record
        )

    # -- enumeration helpers ------------------------------------------------------

    def list_projects(self) -> list[str]:
        if not self.projects_root.is_dir():
            return []
        return sorted(p.name for p in self.projects_root.iterdir() if p.is_dir())


__all__ = ["ProjectStore", "ProjectStoreError"]
