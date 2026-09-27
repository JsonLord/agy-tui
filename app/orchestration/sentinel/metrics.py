"""P7e — per-job + global sentinel metrics.

These counters are THE measured proof that the local event runtime reduces
expensive model usage: observations/transitions are deterministic and free;
laya/spark/wakeups are counted, never estimated. Persisted under the state
root so a daemon restart keeps continuity.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

COUNTER_NAMES = (
    "sentinel_observations_total",
    "sentinel_state_transitions_total",
    "sentinel_laya_calls_total",
    "sentinel_spark_calls_total",
    "sentinel_supervisor_wakeups_total",
    "sentinel_jobs_active",
    "sentinel_jobs_completed",
    "sentinel_jobs_failed",
    # extras (allowed superset): honest accounting of the other terminal states
    "sentinel_jobs_cancelled",
    "sentinel_jobs_lost",
    "sentinel_spawns_total",
)


class MetricsRegistry:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else None
        self.counters: Dict[str, int] = {name: 0 for name in COUNTER_NAMES}
        if self.root is not None:
            self._load()

    # -- persistence --------------------------------------------------------
    @property
    def path(self):
        return self.root / "metrics.json" if self.root else None

    def _load(self) -> None:
        p = self.path
        if p is None or not p.exists():
            return
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for name in COUNTER_NAMES:
                if isinstance(data.get(name), int):
                    self.counters[name] = int(data[name])
        except (json.JSONDecodeError, OSError):
            pass

    def save(self) -> None:
        if self.root is None:
            return
        p = self.root / "metrics.json"
        tmp = p.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.counters, fh, sort_keys=True, indent=1)
                fh.flush()
            os.replace(tmp, p)
        except OSError:
            pass

    # -- increments ---------------------------------------------------------
    def observation(self) -> None:
        self.counters["sentinel_observations_total"] += 1

    def transition(self) -> None:
        self.counters["sentinel_state_transitions_total"] += 1

    def laya_call(self) -> None:
        self.counters["sentinel_laya_calls_total"] += 1

    def spark_call(self) -> None:
        self.counters["sentinel_spark_calls_total"] += 1

    def wakeup(self) -> None:
        self.counters["sentinel_supervisor_wakeups_total"] += 1

    def spawn(self) -> None:
        self.counters["sentinel_spawns_total"] += 1
        self.counters["sentinel_jobs_active"] += 1

    def job_terminal(self, state: str) -> None:
        from .process_state import JobState

        active = self.counters["sentinel_jobs_active"]
        if active > 0:
            self.counters["sentinel_jobs_active"] = active - 1
        if state == JobState.EXITED_SUCCESS:
            self.counters["sentinel_jobs_completed"] += 1
        elif state == JobState.EXITED_FAILURE:
            self.counters["sentinel_jobs_failed"] += 1
        elif state == JobState.CANCELLED:
            self.counters["sentinel_jobs_cancelled"] += 1
        elif state == JobState.LOST:
            self.counters["sentinel_jobs_lost"] += 1

    def snapshot(self) -> Dict[str, Any]:
        snap: Dict[str, Any] = dict(self.counters)
        snap["ts"] = round(time.time(), 3)
        return snap