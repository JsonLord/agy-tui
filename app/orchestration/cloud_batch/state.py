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
    authority/
      <batch>-lowering.json       P16 lowering record (ONE per batch,
                                  reused idempotently on resume — never
                                  re-lowered; carries plan_id + the
                                  canonical CheckpointPlan intent dict)

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

from pydantic import BaseModel, ValidationError

from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.cloud_batch.schemas import (
    BatchOutcome,
    BatchPlan,
    CloudAction,
    CloudProjectState,
    HumanResponse,
    ProjectContract,
    WorkerOutcome,
)

_CLOUD_FILE_RE = re.compile(r"^(\d{4})-(clarify|batch|needs_human|final)\.json$")
_P15_ANSWER_FILE_RE = re.compile(r"^(\d{4})-(clarify|needs_human)\.json$")
_HUMAN_ANSWER_KINDS = ("clarify", "needs_human")


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

    # -- P16: authority lowering records (checkpoint-routed intents) ------------

    def authority_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "authority"

    def save_lowered_intent(
        self, project_id: str, batch_id: str, record: dict
    ) -> Path:
        """Atomically persist ONE lowering record per batch at
        ``authority/<batch_id>-lowering.json``. The record is the durable
        lowering intent (carries ``plan_id`` + the checkpoint plan dict);
        resume reuses it and NEVER re-lowers.

        Conflict rule (review H-6): a lowering is per-batch-CONTENT. If a
        record already exists with a DIFFERENT ``content_hash`` or
        ``plan_id``, the save is refused (:class:`ProjectStoreError`)
        rather than silently overwriting an intent that may have already
        been human-submitted. An identical re-save is an idempotent
        refresh."""
        adir = self.authority_dir(project_id)
        adir.mkdir(parents=True, exist_ok=True)
        path = adir / f"{safe_name(batch_id)}-lowering.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProjectStoreError(f"lowered intent invalid: {exc}") from exc
            if not isinstance(existing, dict):
                raise ProjectStoreError("lowered intent must be an object")
            if (
                existing.get("content_hash")
                and record.get("content_hash")
                and existing.get("content_hash") != record.get("content_hash")
            ):
                raise ProjectStoreError(
                    "lowered intent content mismatch — refusing to "
                    "overwrite a stale intent"
                )
            if (
                existing.get("plan_id")
                and record.get("plan_id")
                and existing.get("plan_id") != record.get("plan_id")
            ):
                raise ProjectStoreError(
                    "lowered intent plan_id mismatch — refusing to "
                    "overwrite a stale intent"
                )
        return _write(path, record)

    def load_lowered_intent(self, project_id: str, batch_id: str) -> dict:
        """The persisted lowering record, or ``{}`` when absent. Malformed
        documents raise :class:`ProjectStoreError` (fail closed — a
        corrupt lowering record must never be silently re-lowered)."""
        path = self.authority_dir(project_id) / f"{safe_name(batch_id)}-lowering.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectStoreError(f"lowered intent invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise ProjectStoreError("lowered intent must be an object")
        return data

    # -- P15: durable human answers (provenance of record) ---------------------

    def answers_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "answers"

    def next_answer_seq(self, project_id: str) -> int:
        """1-based next answer artifact number (restart-safe: derived from
        existing files, never from process-local state — same convention
        as :meth:`next_cloud_seq`)."""
        adir = self.answers_dir(project_id)
        if not adir.is_dir():
            return 1
        seqs: list[int] = []
        for p in adir.iterdir():
            m = _P15_ANSWER_FILE_RE.fullmatch(p.name)
            if m:
                seqs.append(int(m.group(1)))
        return max(seqs, default=0) + 1

    def latest_cloud_seq_for_mode(self, project_id: str, mode: str) -> int:
        """Highest persisted ``NNNN-<mode>.json`` cloud artifact sequence
        (0 when none exists). The NEEDS_HUMAN park generation for P15
        answer binding is exactly this number."""
        cdir = self.cloud_dir(project_id)
        if not cdir.is_dir():
            return 0
        seqs: list[int] = []
        for p in cdir.iterdir():
            m = _CLOUD_FILE_RE.fullmatch(p.name)
            if m and m.group(2) == mode:
                seqs.append(int(m.group(1)))
        return max(seqs, default=0)

    def save_human_response(
        self, project_id: str, response: "HumanResponse", kind: str
    ) -> Path:
        """Persist ONE durable human response at
        ``projects/<id>/answers/<seq:04d>-<kind>.json``.

        ``kind`` is "clarify" | "needs_human". Monotonic seq is derived
        from existing answer files (restart-safe, same convention as
        ``next_cloud_seq``). The payload is (re)validated as a
        :class:`HumanResponse` here — fail closed: hand-rolled JSON
        without the ``human:`` actor never reaches disk. The written
        file is the DURABLE record of human provenance (actor + origin
        survive restarts) plus the park generation the response is
        bound to (clarify: the single WAITING_FOR_USER episode == 1;
        needs_human: the latest needs_human cloud artifact seq).
        """
        if kind not in _HUMAN_ANSWER_KINDS:
            raise ProjectStoreError(f"unknown human answer kind {kind!r}")
        try:
            if isinstance(response, HumanResponse):
                validated = HumanResponse.model_validate(response.model_dump())
            elif isinstance(response, dict):
                validated = HumanResponse.model_validate(response)
            else:
                raise ValueError(
                    f"payload is not a HumanResponse: {type(response).__name__}"
                )
        except ValidationError as exc:
            raise ProjectStoreError(f"human response invalid: {exc}") from exc
        adir = self.answers_dir(project_id)
        adir.mkdir(parents=True, exist_ok=True)
        seq = self.next_answer_seq(project_id)
        path = adir / f"{seq:04d}-{kind}.json"
        payload = validated.model_dump(mode="json")
        payload["kind"] = kind
        payload["park_generation"] = (
            1 if kind == "clarify"
            else self.latest_cloud_seq_for_mode(project_id, "needs_human")
        )
        return _write(path, payload)

    def load_human_responses(self, project_id: str, kind: str) -> list[dict]:
        """All persisted human responses for (project, kind), oldest
        first; ``[]`` when none. Malformed files are skipped (never
        crash discovery)."""
        if kind not in _HUMAN_ANSWER_KINDS:
            raise ProjectStoreError(f"unknown human answer kind {kind!r}")
        adir = self.answers_dir(project_id)
        if not adir.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(adir.iterdir()):
            m = _P15_ANSWER_FILE_RE.fullmatch(p.name)
            if m is None or m.group(2) != kind:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # partial/garbage write is skipped, not fatal
            if isinstance(data, dict):
                out.append(data)
        return out

    # -- enumeration helpers ------------------------------------------------------

    def list_projects(self) -> list[str]:
        if not self.projects_root.is_dir():
            return []
        return sorted(p.name for p in self.projects_root.iterdir() if p.is_dir())


__all__ = ["ProjectStore", "ProjectStoreError"]
