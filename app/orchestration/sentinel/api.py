"""P7a/P7b/c/d/e — the cloud-facing sentinel API.

CORE INVARIANT: the cloud-facing surface stays TINY by design —

    spawn_monitored_process(command, cwd, deadline_s, checkpoint_patterns,
                            activity_markers, ...) -> job_id
    get_process_snapshot(job_id)   # diagnostics ONLY (see note below)
    cancel_process(job_id)

get_process_snapshot is read-only, returns a bounded document, and is a
DIAGNOSTIC — the daemon's normal flow is event-out: the model yields after
spawning and is woken by the wake bridge (SUPERVISOR_WAKE_REQUESTED), never
by polling "status?". snapshot() usage is logged/counted so a polling loop is
visible, not silent. spawn/cancel tick nothing themselves: control goes to the
local watcher/engine, which owns the entire period between model events.

No model call is ever made from this module.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .events import EventLog
from .metrics import MetricsRegistry
from .observations import read_proc_stat
from .process_state import JobRecord, JobState, JobStore, command_hash, new_job_id
from .watcher import SentinelEngine, WatchPolicy, _kill_pg
from .wake_bridge import WakeBridge, WakePolicy

REPO_ROOT = Path(__file__).resolve().parents[3]


class SentinelRuntime:
    """Facade: spawn/cancel/snapshot + engine driving + metrics + recovery."""

    def __init__(self, state_root, policy: Optional[WatchPolicy] = None,
                 wake_policy: Optional[WakePolicy] = None, *,
                 clock: Callable[[], float] = time.time,
                 laya_gate: Any = None, spark_interpreter: Any = None,
                 waker: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 snapshot_hint: bool = True):
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or WatchPolicy()
        self.clock = clock
        self.store = JobStore(self.state_root)
        self.event_log = EventLog(self.state_root)
        self.metrics = MetricsRegistry(self.state_root)
        self.bridge: Optional[WakeBridge] = None
        if wake_policy is not False:  # type: ignore[comparison-overlap]
            self.bridge = WakeBridge(self.state_root, wake_policy,
                                     waker=waker, clock=clock)
        self.engine = SentinelEngine(
            self.store, self.event_log, self.policy, clock=clock,
            laya_gate_=laya_gate, spark_interpreter_=spark_interpreter,
            wake_bridge_=self.bridge, metrics=self.metrics)
        self.snapshot_reads = 0   # diagnostics counter (detects misuse)
        self._wake_deliveries_failure_note = ""

    # -- spawn ---------------------------------------------------------------
    def spawn_monitored_process(
        self,
        command: Union[str, Sequence[str]],
        cwd: Optional[str] = None,
        deadline_s: Optional[float] = None,
        checkpoint_patterns: Sequence[str] = (),
        activity_markers: Sequence[str] = (),
        verify: Optional[Dict[str, Any]] = None,
        env_extra: Optional[Dict[str, str]] = None,
    ) -> str:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise ValueError("empty command")
        now = self.clock()
        job_id = new_job_id()
        out_path = self.state_root / "capture" / f"{job_id}.out"
        err_path = self.state_root / "capture" / f"{job_id}.err"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        exit_file = self.store.exits_dir / f"{job_id}.json"
        spawn_file = self.state_root / "spawned" / f"{job_id}.json"
        spawn_file.parent.mkdir(parents=True, exist_ok=True)
        task = {
            "job_id": job_id,
            "argv": argv,
            "cwd": str(cwd or os.getcwd()),
            "stdout_path": str(out_path),
            "stderr_path": str(err_path),
            "exit_file": str(exit_file),
            "capture_max_bytes": self.policy.capture_max_bytes,
            "capture_rotations": self.policy.capture_rotations,
            "env_extra": dict(env_extra or {}),
        }
        with open(spawn_file, "w", encoding="utf-8") as fh:
            json.dump(task, fh)
            fh.flush()

        env = dict(os.environ)
        env["PYTHONPATH"] = (str(REPO_ROOT) + os.pathsep
                             + env.get("PYTHONPATH", "")).rstrip(os.pathsep)
        holder_cmd = [sys.executable, "-u", "-m",
                      "app.orchestration.sentinel.holder", str(spawn_file)]
        holder = subprocess.Popen(
            holder_cmd, cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            startup_line = _read_line_with_timeout(
                holder.stdout, self.policy.holder_start_timeout_s)
        except Exception:
            try:
                holder.kill()
            except OSError:
                pass
            raise
        if startup_line is None:
            err_tail = b""
            try:
                holder.kill()
                err_tail = holder.stderr.read(2000) if holder.stderr else b""
            except OSError:
                pass
            raise RuntimeError(
                f"spawn failed: holder produced no startup line: "
                f"{err_tail.decode('utf-8', 'replace')[:500]}")
        try:
            startup = json.loads(startup_line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"spawn failed: bad holder startup line: {exc}")
        if "error" in startup:
            raise RuntimeError(f"spawn failed: {startup['error']}")

        rec = JobRecord(
            job_id=job_id,
            argv=argv,
            cwd=str(cwd or os.getcwd()),
            command_hash=command_hash(argv),
            pid=int(startup["pid"]),
            pgid=int(startup.get("pgid") or startup["pid"]),
            proc_starttime=int(startup["starttime"]) if startup.get("starttime") is not None else None,
            holder_pid=int(startup.get("holder_pid") or 0) or None,
            stdout_path=str(out_path),
            stderr_path=str(err_path),
            exit_file=str(exit_file),
            spawn_file=str(spawn_file),
            activity_markers=[str(m) for m in activity_markers],
            checkpoint_patterns=[str(p) for p in checkpoint_patterns],
            verify=dict(verify) if verify else None,
            started_at=now,
            deadline=(now + float(deadline_s)) if deadline_s else None,
            state=JobState.STARTING,
            last_output_at=now,
            last_activity_at=now,
            created_at=now,
        )
        if rec.proc_starttime is None:
            stat = read_proc_stat(rec.pid) if rec.pid else None
            rec.proc_starttime = int(stat["starttime"]) if stat else None  # type: ignore[index]
        self.engine.register(rec)  # emits PROCESS_STARTED + counts spawn/active once
        self.metrics.save()
        self.store.save(rec, index_entry={"state": rec.state, "kind": "created"})
        return job_id

    # -- snapshot (DIAGNOSTIC only) -------------------------------------------
    def get_process_snapshot(self, job_id: str) -> Dict[str, Any]:
        """Read-only diagnostics. NOT a control loop: the sentinel is
        event-driven (the model yields and is woken); use the event log for
        truth and the wake bridge for re-engagement."""
        self.snapshot_reads += 1
        rec = self.engine.get(job_id)
        obs = self.engine.last_obs.get(job_id)
        return {
            "job_id": rec.job_id,
            "state": rec.state,
            "pid": rec.pid,
            "pgid": rec.pgid,
            "proc_starttime": rec.proc_starttime,
            "started_at": rec.started_at,
            "deadline": rec.deadline,
            "last_output_at": rec.last_output_at,
            "checkpoint": rec.checkpoint,
            "terminal": rec.state in ("EXITED_SUCCESS", "EXITED_FAILURE", "LOST", "CANCELLED"),
            "metrics": dict(rec.metrics),
            "last_observation": obs.evidence() if obs is not None else None,
            "global_metrics": self.metrics.snapshot(),
            "usage": "diagnostic; event-driven — do not poll for status",
        }

    # -- cancel ----------------------------------------------------------------
    def cancel_process(self, job_id: str) -> Dict[str, Any]:
        """SIGTERM to the process group, grace (driven by ticks), then SIGKILL.
        Idempotent. Explicit cancellation -> CANCELLED (never overridden)."""
        rec = self.engine.get(job_id)
        if rec.state == JobState.CANCELLED:
            return {"ok": True, "job_id": job_id, "state": rec.state,
                    "already_cancelled": True}
        if rec.is_terminal:
            return {"ok": True, "job_id": job_id, "state": rec.state,
                    "already_terminal": True}
        rec.cancel_requested = True
        rec.cancel_requested_at = self.clock()
        if rec.state in ("STARTING", JobState.RUNNING, JobState.QUIET_RUNNING,
                         JobState.CHECKPOINT, JobState.STALL_SUSPECTED,
                         JobState.DEADLINE_REACHED):
            _kill_pg(rec.pgid, signal.SIGTERM)  # may target already-dead pg: ignored
        self.engine.cancel_here(rec, "cancel_process")  # deterministic fact
        if self.engine.bridge is not None:
            pass  # cancelling never wakes the supervisor: it IS the decision
        self.metrics.save()
        return {"ok": True, "job_id": job_id, "state": rec.state,
                "already_cancelled": False}

    # -- summaries / metrics ---------------------------------------------------
    def job_summary(self, job_id: str) -> str:
        rec = self.engine.get(job_id)
        end = rec.finished_at if rec.finished_at is not None else self.clock()
        duration = max(0.0, end - rec.started_at)
        m = rec.metrics
        return (
            f"job_summary job={rec.job_id} state={rec.state} "
            f"duration_s={duration:.1f} observations={int(m.get('observations', 0))} "
            f"transitions={int(m.get('transitions', 0))} "
            f"laya_decisions={int(m.get('laya_calls', 0))} "
            f"spark_interpretations={int(m.get('spark_calls', 0))} "
            f"supervisor_wakeups={int(m.get('wakeups', 0))}"
        )

    def metrics_snapshot(self) -> Dict[str, Any]:
        self.metrics.save()
        return self.metrics.snapshot()

    # -- ticking ----------------------------------------------------------------
    def tick(self, job_id: str):
        return self.engine.tick(job_id)

    def tick_due(self):
        return self.engine.tick_due()

    def active_jobs(self) -> List[str]:
        return [
            rec.job_id for rec in self.store.records()
            if rec.state not in ("EXITED_SUCCESS", "EXITED_FAILURE", "LOST", "CANCELLED")
        ]

    def recover(self) -> Dict[str, Any]:
        summary = self.engine.recover()
        self.recover_note = summary  # type: ignore[attr-defined]
        return summary

    def run_until(self, job_id: str, predicate: Callable[[JobRecord], bool],
                  timeout: float = 5.0, interval: float = 0.02) -> JobRecord:
        """Test/live helper: drive ticks until predicate or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick(job_id)
            rec = self.engine.get(job_id)
            if predicate(rec):
                return rec
            if rec.state in ("EXITED_SUCCESS", "EXITED_FAILURE", "LOST"):
                break
            time.sleep(interval)
        return self.engine.get(job_id)

    def shutdown(self) -> None:
        for rec in list(self.engine.records.values()):
            try:
                self.store.save(rec)
            except OSError:
                pass
        self.metrics.save()


def _read_line_with_timeout(pipe, timeout_s: float) -> Optional[str]:
    """Read one startup line from the holder pipe within a bounded timeout."""
    if pipe is None:
        return None
    fd = pipe.fileno()
    chunks: List[bytes] = []
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None  # caller treats as failure
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if not ready:
            continue
        data = os.read(fd, 4096)
        if not data:
            joined = b"".join(chunks)
            return joined.decode("utf-8", "replace").strip() or None
        chunks.append(data)
        if b"\n" in data:
            joined = b"".join(chunks)
            return joined.split(b"\n", 1)[0].decode("utf-8", "replace").strip()