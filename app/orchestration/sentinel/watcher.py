"""P7a — adaptive local polling + the deterministic sentinel engine.

CORE INVARIANT: a model NEVER owns a wait loop. This module owns the entire
period between model events: observe -> classify -> (genuinely ambiguous
only) consult advisory layers -> act deterministically. No model is ever
asked "status?".

Polling intervals are fully configurable via WatchPolicy (never hardcoded in
logic):
    STARTING -> starting_interval_s (default 1s)
    stable RUNNING -> stable_interval_s (default 5s)
    approaching suspected-stall or deadline -> urgent_interval_s (default 2s)
    terminal -> stop (None)
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .events import EventLog
from .laya_gate import build_laya_payload
from .metrics import MetricsRegistry
from .observations import observe, read_proc_stat, read_tail
from .process_state import (
    TERMINAL_STATES,
    JobRecord,
    JobState,
    JobStore,
)
from .spark_interpreter import build_incident_packet
from .state_machine import (
    EVENT_DEADLINE_REACHED,
    EVENT_LAYA_DECIDED,
    EVENT_LOST,
    EVENT_PROCESS_STARTED,
    EVENT_SPARK_INTERPRETED,
    EVENT_SUPERVISOR_WAKE,
    EVENT_CANCELLED,
    EVENT_EXITED_FAILURE,
    EVENT_EXITED_SUCCESS,
    Transition,
    allowed_by_gate,
    evaluate,
)


@dataclass
class WatchPolicy:
    """All intervals/thresholds are data, not code — configurable per run."""

    starting_interval_s: float = 1.0
    stable_interval_s: float = 5.0
    urgent_interval_s: float = 2.0
    quiet_after_s: float = 60.0
    urgent_quiet_margin_s: float = 10.0
    urgent_deadline_margin_s: float = 15.0
    active_cpu_pct: float = 50.0
    stall_cpu_pct: float = 5.0
    exit_file_grace_s: float = 5.0
    spark_grace_after_stall_s: float = 45.0
    spark_enabled: bool = True
    spark_wait_extension_cap_s: float = 300.0
    laya_enabled: bool = True
    laya_on_checkpoint: bool = False  # optional: advisory on unrecognized checkpoint
    deadline_policy: str = "keep_watching"  # "keep_watching" | "ask"
    cancel_grace_s: float = 5.0
    capture_tail_bytes: int = 65536
    holder_start_timeout_s: float = 15.0
    capture_max_bytes: int = 10_000_000   # per stream; rotation keeps last N MB
    capture_rotations: int = 1


def next_interval(policy: WatchPolicy, state: str, quiet_s: float,
                  deadline_remaining_s: Optional[float]) -> Optional[float]:
    """Adaptive local polling. Terminal jobs stop (None)."""
    if state in TERMINAL_STATES:
        return None
    if state == JobState.STARTING:
        return float(policy.starting_interval_s)
    urgent = state in (JobState.STALL_SUSPECTED, JobState.DEADLINE_REACHED)
    if not urgent and quiet_s + float(policy.urgent_quiet_margin_s) >= float(policy.quiet_after_s):
        urgent = True  # approaching suspected-stall: poll faster
    if deadline_remaining_s is not None and deadline_remaining_s <= float(policy.urgent_deadline_margin_s):
        urgent = True  # approaching deadline: poll faster
    return float(policy.urgent_interval_s) if urgent else float(policy.stable_interval_s)


@dataclass
class TickResult:
    job_id: str
    observation: Optional[Any] = None
    transition: Optional[Transition] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    laya: Optional[Any] = None
    spark: Optional[Any] = None
    wake: Optional[Dict[str, Any]] = None
    next_interval_s: Optional[float] = None


class SentinelEngine:
    """Observation -> state machine -> (ambiguous only) advisory gates -> wakes.

    Single owner of job state in-process: the daemon loop and the tests both
    drive it through tick(). It never performs I/O beyond the local store,
    event log, metric files, and (config.sock later) nothing.
    """

    def __init__(self, store: JobStore, event_log: EventLog, policy: WatchPolicy, *,
                 clock: Callable[[], float] = time.time,
                 gate: Any = None, spark: Any = None, bridge: Any = None,
                 metrics: Optional[MetricsRegistry] = None,
                 laya_gate_=None, spark_interpreter_=None, wake_bridge_=None):
        self.store = store
        self.event_log = event_log
        self.policy = policy
        self.clock = clock
        self.gate = laya_gate_ if laya_gate_ is not None else gate
        self.spark = spark_interpreter_ if spark_interpreter_ is not None else spark
        self.bridge = wake_bridge_ if wake_bridge_ is not None else bridge
        self.metrics = metrics if metrics is not None else MetricsRegistry(None)
        self.records: Dict[str, JobRecord] = {}
        self.last_obs: Dict[str, Any] = {}
        self.force_urgent: set = set()
        self.due_at: Dict[str, float] = {}
        self.previous_incidents: Dict[str, Dict[str, Any]] = {}

    # -- record lifecycle ---------------------------------------------------
    def _cache(self, record: JobRecord) -> JobRecord:
        self.records[record.job_id] = record
        return record

    def get(self, job_id: str) -> JobRecord:
        rec = self.records.get(job_id)
        if rec is None:
            rec = self.store.load(job_id)
            if rec is None:
                raise KeyError(f"unknown job: {job_id}")
            self.records[job_id] = rec
        return rec

    def emit(self, rec: JobRecord, event_type: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        event = self.event_log.emit(self.clock(), rec.job_id, event_type,
                                    rec.state, evidence)
        return event

    def register(self, rec: JobRecord) -> Dict[str, Any]:
        """Spawn-time registration: exactly one PROCESS_STARTED per job."""
        self._cache(rec)
        events: List[Dict[str, Any]] = []
        if not rec.started_event_emitted:
            rec.started_event_emitted = True
            events.append(self.emit(rec, EVENT_PROCESS_STARTED, {
                "pid": rec.pid, "pgid": rec.pgid,
                "proc_starttime": rec.proc_starttime,
                "command_hash": rec.command_hash,
                "deadline": rec.deadline,
            }))
            rec.bump("transitions")  # existence is a deterministic transition
            self.metrics.transition()
        if not rec.counted_active:
            rec.counted_active = True
            self.metrics.spawn()
        self.store.save(rec, index_entry={"state": rec.state, "kind": "created"})
        return {"events": events}

    # -- transitions --------------------------------------------------------
    def _apply_state(self, rec: JobRecord, tr: Transition, now: float,
                     events_out: List[Dict[str, Any]], *,
                     extra_event_evidence: Optional[Dict[str, Any]] = None) -> None:
        prev = rec.state
        rec.state = str(tr.new_state)
        rec.bump("transitions")
        self.metrics.transition()
        if prev == JobState.STALL_SUSPECTED and rec.state != JobState.STALL_SUSPECTED:
            rec.stall_since = None
        if rec.state == JobState.STALL_SUSPECTED and prev != JobState.STALL_SUSPECTED:
            rec.stall_episodes += 1
            rec.stall_since = now
        if rec.state in TERMINAL_STATES and rec.finished_at is None:
            rec.finished_at = now
            if rec.counted_active and not rec.counted_job:
                rec.counted_job = True
                self.metrics.job_terminal(rec.state)
                self.metrics.save()
        if tr.event_type:
            evidence = dict(tr.evidence)
            if extra_event_evidence:
                evidence.update(extra_event_evidence)
            events_out.append(self.emit(rec, tr.event_type, evidence))

    def cancel_here(self, rec: JobRecord, reason: str = "cancel_process") -> None:
        """Explicit cancellation is a deterministic fact: CANCELLED immediately."""
        if rec.state in TERMINAL_STATES:
            return
        now = self.clock()
        tr = Transition(changed=True, new_state=JobState.CANCELLED,
                        event_type=EVENT_CANCELLED,
                        evidence={"reason": reason, "requested_at": now})
        self._apply_state(rec, tr, now, [])
        self.store.save(rec, index_entry={"state": rec.state})

    def mark_process_lost(self, rec: JobRecord, reason: str,
                          evidence: Optional[Dict[str, Any]] = None) -> None:
        """Recovery path: identity unrecoverable. NEVER signals the pid."""
        if rec.state in TERMINAL_STATES:
            return
        now = self.clock()
        tr = Transition(changed=True, new_state=JobState.LOST,
                        event_type=EVENT_LOST, evidence={"reason": reason, **(evidence or {})})
        self._apply_state(rec, tr, now, [])
        self.store.save(rec, index_entry={"state": rec.state})

    def mark_exited(self, rec: JobRecord, returncode: int, *, recovered: bool = False) -> None:
        """Terminal fact from a recorded exit (e.g. written before a restart)."""
        if rec.state in TERMINAL_STATES:
            return
        now = self.clock()
        state = (JobState.EXITED_SUCCESS if returncode == 0
                 else JobState.EXITED_FAILURE)
        tr = Transition(
            changed=True, new_state=state,
            event_type=(EVENT_EXITED_SUCCESS if returncode == 0 else EVENT_EXITED_FAILURE),
            evidence={"returncode": returncode,
                      "recovered_after_restart": recovered})
        self._apply_state(rec, tr, now, [])
        # NOTE: single emit — tr.evidence already carries recovered_after_restart
        self.store.save(rec, index_entry={"state": rec.state})

    # -- the tick -----------------------------------------------------------
    def tick(self, job_id: str) -> TickResult:
        rec = self.get(job_id)
        now = self.clock()
        result = TickResult(job_id=job_id)

        # terminal jobs: only cancellation housekeeping remains, no observations
        if rec.state in TERMINAL_STATES:
            if rec.cancel_requested and rec.cancel_requested_at is not None:
                still = read_proc_stat(rec.pid) if rec.pid else None
                if (still is not None and still.get("state") != "Z"
                        and now - rec.cancel_requested_at >= self.policy.cancel_grace_s):
                    _kill_pg(rec.pgid, signal.SIGKILL)
            return result

        obs = observe(rec, self.policy, now)
        result.observation = obs
        self.last_obs[job_id] = obs
        self.metrics.observation()
        rec.bump("observations")

        tr = evaluate(rec, obs, self.policy, now)
        result.transition = tr
        if tr.changed:
            self._apply_state(rec, tr, now, result.events)

        # advisory gates — ONLY pre-terminal, ONLY on genuine ambiguity
        if allowed_by_gate(rec.state):
            self._advisory(rec, obs, now, result)

        # deterministic wake policy
        if allowed_by_gate(rec.state):
            if (tr.changed and tr.new_state == JobState.DEADLINE_REACHED
                    and self.policy.deadline_policy == "ask"):
                self._wake(rec, "deadline_policy", rec.stall_episodes, obs, result)
            self._repeated_stall_policy(rec, now, obs, result)
        elif rec.state in TERMINAL_STATES and rec.verify:
            self._verification(rec, obs, result)

        self.store.save(rec, index_entry={"state": rec.state} if tr.changed else None)
        if rec.counted_job or (not self.metrics.counters.get("sentinel_spawns_total")):
            self.metrics.save()
        result.next_interval_s = next_interval(
            self.policy, rec.state, obs.quiet_s, obs.deadline_remaining_s)
        return result

    def tick_due(self) -> List[TickResult]:
        "Tick every job whose adaptive schedule says it is due."
        now = self.clock()
        out: List[TickResult] = []
        for job_id in list(self.records):
            if self.get(job_id).state in TERMINAL_STATES:
                self.due_at.pop(job_id, None)
                continue
            due = self.due_at.get(job_id)
            forced = job_id in self.force_urgent
            if not forced and due is not None and now < due:
                continue
            self.force_urgent.discard(job_id)
            res = self.tick(job_id)
            out.append(res)
            interval = res.next_interval_s
            if interval is None:
                self.due_at.pop(job_id, None)
            else:
                # urgent recomputes next tick anyway; schedule by policy value
                self.due_at[job_id] = self.clock() + max(0.01, interval)
        return out

    # -- advisory layers (Laya P7b / Spark P7c) -----------------------------
    def _advisory(self, rec: JobRecord, obs, now: float, result: TickResult) -> None:
        policy = self.policy

        # P7b gate: entering STALL_SUSPECTED (or configured checkpoint) ONLY
        consulted_stall = False
        entered_stall = bool(result.transition and result.transition.changed
                             and result.transition.new_state == JobState.STALL_SUSPECTED)
        entered_ckpt = bool(result.transition and result.transition.changed
                            and result.transition.new_state == JobState.CHECKPOINT)
        if (policy.laya_enabled and self.gate is not None
                and rec.state not in TERMINAL_STATES):
            if entered_stall and rec.laya_episode < rec.stall_episodes:
                consulted_stall = True
                self._consult_laya(rec, obs, now, result, trigger="stall",
                                   episode=rec.stall_episodes)
            elif entered_ckpt and policy.laya_on_checkpoint and rec.laya_episode < rec.stall_episodes:
                consulted_stall = True
                self._consult_laya(rec, obs, now, result, trigger="checkpoint",
                                   episode=rec.stall_episodes)

        # P7c interpreter: Laya needs_interpretation OR persistent stall grace
        if policy.spark_enabled and self.spark is not None:
            laya_decision = rec.laya_decision or {}
            needs_interp = entered_stall and consulted_stall and bool(
                result.laya and result.laya.needs_interpretation)
            extension_ok = (rec.spark_extension_until is None
                            or now >= rec.spark_extension_until)
            persistent = (
                rec.state == JobState.STALL_SUSPECTED
                and rec.stall_since is not None
                and extension_ok
                and now >= rec.stall_since + policy.spark_grace_after_stall_s
            )
            episode_budget = rec.spark_episode < rec.stall_episodes
            if (needs_interp or persistent) and episode_budget:
                self._interpret_incident(rec, obs, now, result,
                                         episode=rec.stall_episodes)

    def _consult_laya(self, rec: JobRecord, obs, now: float, result: TickResult,
                      *, trigger: str, episode: int) -> None:
        # HARD RULE (P7b): the gate is never consulted on a terminal record —
        # a LayaDecision can NOT change (or even touch) a terminal fact.
        if not allowed_by_gate(rec.state):
            return
        payload = build_laya_payload(rec, obs, now)
        decision = self.gate.decide(payload)
        # re-check after the call: even a hostile decision sees no terminal state
        if not allowed_by_gate(rec.state):
            return
        rec.laya_episode = episode
        rec.laya_decision = decision.to_dict()
        rec.bump("laya_calls")
        self.metrics.laya_call()
        result.laya = decision
        result.events.append(self.emit(rec, EVENT_LAYA_DECIDED, {
            "trigger": trigger,
            "episode": episode,
            "payload_bytes": getattr(self.gate, "last_payload_bytes", None),
            "decision": decision.to_dict(),
        }))

    def _interpret_incident(self, rec: JobRecord, obs, now: float,
                            result: TickResult, *, episode: int) -> None:
        rec.spark_episode = episode
        tail = read_tail(rec.stdout_path, self.policy.capture_tail_bytes)
        tail_lines = [ln for ln in tail.splitlines() if ln.strip()][-100:]
        transitions = [dict(e) for e in self.event_log.tail_for(rec.job_id, n=10)]
        recent_ckpts = ([rec.checkpoint] if rec.checkpoint else [])
        packet = build_incident_packet(
            job_id=rec.job_id, state=rec.state,
            metadata={"command_hash": rec.command_hash, "cwd": rec.cwd},
            evidence=obs.evidence(), laya=rec.laya_decision,
            tail_lines=tail_lines, recent_checkpoints=recent_ckpts,
            transitions=transitions,
            previous_incident=self.previous_incidents.get(rec.job_id),
            metrics=dict(rec.metrics),
        )
        verdict = self.spark.interpret(packet)
        rec.bump("spark_calls")
        self.metrics.spark_call()
        rec.last_spark_verdict = verdict.to_dict()
        result.spark = verdict
        result.events.append(self.emit(rec, EVENT_SPARK_INTERPRETED, {
            "episode": episode,
            "packet_bytes": len(json_dumps(packet)),
            "verdict": verdict.to_dict(),
        }))
        self.previous_incidents[rec.job_id] = verdict.to_dict()

        # Spark is ADVICE ONLY: no state change, no kill, routed via policy
        if not verdict.is_ok:
            return  # malformed/timeout/credential: safe failure, keep watching
        if verdict.recommended_action == "WAIT":
            wait = verdict.wait_seconds or 0
            rec.spark_extension_until = now + min(
                float(wait), policy_cap(self.policy))
        elif verdict.recommended_action == "RECHECK":
            self.force_urgent.add(rec.job_id)
        if verdict.escalates:
            reason = ("spark_intervention")
            self._wake(rec, reason, episode, obs, result,
                       laya=rec.laya_decision, spark=verdict.to_dict())

    # -- wake bridge (P7d) ---------------------------------------------------
    def _pointers(self, rec: JobRecord) -> Dict[str, str]:
        return {
            "event_log": str(self.event_log.path),
            "job_file": str(self.store.record_path(rec.job_id)),
            "stdout": str(rec.stdout_path),
            "stderr": str(rec.stderr_path),
        }

    def _wake(self, rec: JobRecord, reason: str, episode: int, obs,
              result: TickResult, *, slot: int = 0,
              laya: Optional[Dict[str, Any]] = None,
              spark: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if self.bridge is None or obs is None:
            return None
        evidence = obs.evidence()
        packet = self.bridge.request(
            rec, reason=reason, episode=episode, slot=slot,
            evidence=evidence, laya=laya if laya is not None else rec.laya_decision,
            spark=spark if spark is not None else rec.last_spark_verdict,
            pointers=self._pointers(rec))
        if packet is None:
            return None
        rec.bump("wakeups")
        self.metrics.wakeup()
        result.wake = packet
        result.events.append(self.emit(rec, EVENT_SUPERVISOR_WAKE, {
            "reason": reason, "episode": episode, "state": rec.state,
            "evidence_pointers": packet.get("pointers", {}),
        }))
        return packet

    def _repeated_stall_policy(self, rec: JobRecord, now: float, obs,
                               result: TickResult) -> None:
        """Bounded repeated-stall wakeups: 1 immediate + 1 after grace, then ESCALATED."""
        if self.bridge is None:
            return
        wp = self.bridge.policy
        still_stalled = rec.state == JobState.STALL_SUSPECTED
        if still_stalled and not rec.escalated and rec.stall_wakes == 0:
            if rec.stall_episodes >= int(wp.min_stall_episodes):
                if self._wake(rec, "repeated_stall", rec.stall_episodes, obs, result):
                    rec.stall_wakes = 1
                    rec.stall_grace_until = now + float(wp.wake_grace_s)
        elif (still_stalled and not rec.escalated and rec.stall_wakes == 1
              and rec.stall_grace_until is not None
              and now >= rec.stall_grace_until):
            if self._wake(rec, "repeated_stall", rec.stall_episodes, obs, result,
                          slot=1):
                rec.stall_wakes = 2
                rec.escalated = True  # policy stops auto-waking for this job

    def _verification(self, rec: JobRecord, obs, result: TickResult) -> None:
        """Deterministic verification failure is a wake reason (P7d)."""
        verify = rec.verify or {}
        failed: Optional[str] = None
        expect = verify.get("expect_returncode")
        rc = obs.returncode if obs is not None else None
        if expect is not None and rc is not None and rc != expect:
            failed = f"returncode {rc} != expected {expect}"
        for pattern in verify.get("require_patterns") or []:
            text = read_tail(rec.stdout_path, self.policy.capture_tail_bytes)
            import re as _re
            if not _re.search(pattern, text):
                failed = f"pattern not found: {pattern}"
        if failed and rc is not None:
            fake_obs = obs
            self._wake(rec, "verification_failure", 0, fake_obs, result)

    # -- recovery (P7e) -------------------------------------------------------
    def recover(self) -> Dict[str, Any]:
        """Daemon restart recovery: load non-terminal jobs and re-verify identity.

        pid exists? -> /proc start time matches? -> identity confirmed: resume
        watching. start time mismatch = PID recycled -> LOST. pid gone and no
        recorded exit -> LOST. Never assume a matching PID. NO duplicate spawns.
        """
        summary = {"checked": 0, "resumed": 0, "lost": 0, "terminalized": 0,
                   "resumed_jobs": [], "lost_jobs": [], "terminal_jobs": []}
        for rec in self.store.records():
            if rec.state in TERMINAL_STATES:
                continue
            summary["checked"] += 1
            self._cache(rec)
            stat = read_proc_stat(rec.pid) if rec.pid else None
            gone = stat is None or stat.get("state") == "Z"
            if gone:
                exit_rec = self.store.read_exit_record(rec.job_id)
                rc = (exit_rec or {}).get("returncode")
                if rc is not None:
                    self.mark_exited(rec, int(rc), recovered=True)
                    summary["terminalized"] += 1
                    summary["terminal_jobs"].append(rec.job_id)
                else:
                    self.mark_process_lost(rec, "pid_gone_no_exit")
                    summary["lost"] += 1
                    summary["lost_jobs"].append(rec.job_id)
            elif int(stat["starttime"]) != int(rec.proc_starttime or -1):  # type: ignore[arg-type]
                self.mark_process_lost(rec, "pid_recycled",
                                       {"stored_starttime": rec.proc_starttime,
                                        "found_starttime": int(stat["starttime"])})  # type: ignore[arg-type]
                summary["lost"] += 1
                summary["lost_jobs"].append(rec.job_id)
            else:
                # identity confirmed: resume watching the SAME live process
                rec.cpu_prev_ticks = None
                rec.cpu_prev_ts = None
                rec.missing_since = None
                self.store.save(rec, index_entry={"state": rec.state,
                                                  "kind": "resumed_after_restart"})
                summary["resumed"] += 1
                summary["resumed_jobs"].append(rec.job_id)
        self.metrics.save()
        return summary


def policy_cap(policy: WatchPolicy) -> float:
    return float(policy.spark_wait_extension_cap_s)


def json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"), default=str)


def _kill_pg(pgid: Optional[int], sig: int) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError, ValueError, TypeError):
        return False