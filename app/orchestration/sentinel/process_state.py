"""P7a — persistent job records and store for the AGY process sentinel.

A JobRecord is the durable identity + state of one monitored process. It is
written atomically (tmp + os.replace) per job under the sentinel state root
plus an append-only index of created/state-change entries.

CORE INVARIANT: nothing in this module talks to a model. All facts here are
deterministic (pids, timestamps, paths, counters).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class JobState:
    """Sentinel state machine states (P7a contract)."""

    STARTING = "STARTING"
    RUNNING = "RUNNING"
    QUIET_RUNNING = "QUIET_RUNNING"
    CHECKPOINT = "CHECKPOINT"
    STALL_SUSPECTED = "STALL_SUSPECTED"
    DEADLINE_REACHED = "DEADLINE_REACHED"
    EXITED_SUCCESS = "EXITED_SUCCESS"
    EXITED_FAILURE = "EXITED_FAILURE"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


ALL_STATES = frozenset(
    {
        JobState.STARTING,
        JobState.RUNNING,
        JobState.QUIET_RUNNING,
        JobState.CHECKPOINT,
        JobState.STALL_SUSPECTED,
        JobState.DEADLINE_REACHED,
        JobState.EXITED_SUCCESS,
        JobState.EXITED_FAILURE,
        JobState.LOST,
        JobState.CANCELLED,
    }
)

TERMINAL_STATES = frozenset(
    {
        JobState.EXITED_SUCCESS,
        JobState.EXITED_FAILURE,
        JobState.LOST,
        JobState.CANCELLED,
    }
)

METRIC_KEYS = (
    "observations",
    "transitions",
    "laya_calls",
    "spark_calls",
    "wakeups",
)


def command_hash(argv: List[str]) -> str:
    return hashlib.sha256("\0".join(argv).encode("utf-8", "replace")).hexdigest()


def new_job_id() -> str:
    return "proc_" + uuid.uuid4().hex[:12]


@dataclass
class JobRecord:
    job_id: str
    argv: List[str]
    cwd: str
    command_hash: str
    pid: Optional[int] = None
    pgid: Optional[int] = None
    proc_starttime: Optional[int] = None
    holder_pid: Optional[int] = None
    stdout_path: str = ""
    stderr_path: str = ""
    exit_file: str = ""
    spawn_file: str = ""
    activity_markers: List[str] = field(default_factory=list)
    checkpoint_patterns: List[str] = field(default_factory=list)
    verify: Optional[Dict[str, Any]] = None
    started_at: float = 0.0
    finished_at: Optional[float] = None
    deadline: Optional[float] = None
    state: str = JobState.STARTING
    last_output_at: float = 0.0
    last_activity_at: float = 0.0
    last_marker_mtimes: Dict[str, float] = field(default_factory=dict)
    checkpoint: Optional[str] = None
    checkpoint_at: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    quiet_since: Optional[float] = None
    # deterministic observation bookkeeping (all injectable-time based)
    cpu_prev_ticks: Optional[int] = None
    cpu_prev_ts: Optional[float] = None
    prev_rss_kb: Optional[int] = None
    rss_kb: Optional[int] = None
    stdout_bytes: int = 0
    stdout_mtime: float = 0.0
    stderr_bytes: int = 0
    stderr_mtime: float = 0.0
    missing_since: Optional[float] = None
    last_cpu_pct: Optional[float] = None
    # cancellation
    cancel_requested: bool = False
    cancel_requested_at: Optional[float] = None
    # event/counter bookkeeping
    started_event_emitted: bool = False
    counted_active: bool = False
    counted_job: bool = False
    # ambiguity / advisory bookkeeping (models are ADVISORY only)
    stall_episodes: int = 0
    stall_since: Optional[float] = None
    stall_wakes: int = 0
    stall_grace_until: Optional[float] = None
    escalated: bool = False
    laya_episode: int = -1
    laya_decision: Optional[Dict[str, Any]] = None
    spark_episode: int = -1
    spark_extension_until: Optional[float] = None
    last_spark_verdict: Optional[Dict[str, Any]] = None
    wake_keys: List[str] = field(default_factory=list)
    metrics: Dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in METRIC_KEYS}
    )

    # -- helpers -----------------------------------------------------------
    def bump(self, key: str, n: int = 1) -> None:
        self.metrics[key] = int(self.metrics.get(key, 0)) + n

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRecord":
        known = {f for f in cls.__dataclass_fields__}  # tolerate unknown keys
        clean = {k: v for k, v in data.items() if k in known}
        rec = cls(**clean)
        merged = {k: 0 for k in METRIC_KEYS}
        merged.update(rec.metrics or {})
        rec.metrics = merged
        return rec


class JobStore:
    """Atomic per-job JSON + append-only index, all under one state root."""

    def __init__(self, root):
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"
        self.exits_dir = self.root / "exits"
        self.index_path = self.root / "jobs.index"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.exits_dir.mkdir(parents=True, exist_ok=True)

    def record_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord, *, index_entry: Optional[Dict[str, Any]] = None) -> None:
        record.updated_at = time.time()
        path = self.record_path(record.job_id)
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if index_entry is not None:
            entry = {"ts": record.updated_at, "job_id": record.job_id}
            entry.update(index_entry)
            self.append_index(entry)

    def load(self, job_id: str) -> Optional[JobRecord]:
        path = self.record_path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        return JobRecord.from_dict(data)

    def records(self) -> List[JobRecord]:
        out: List[JobRecord] = []
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            out.append(JobRecord.from_dict(data))
        return out

    def append_index(self, entry: Dict[str, Any]) -> None:
        try:
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
                fh.flush()
        except OSError:
            pass

    def read_exit_record(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Exit record written by the detached holder process (survives daemon restart)."""
        path = self.exits_dir / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None