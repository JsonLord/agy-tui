"""P7a — deterministic state machine for observed jobs.

INVARIANT: STALL_SUSPECTED != STALLED. The deterministic layer *suspects* a
stall (quiet + ~0 CPU + no FS/checkpoint activity); it never semantically
declares one. Only a genuinely ambiguous transition may later be *advised*
about through the Laya gate (P7b), and no advisory result can override a
deterministic terminal fact.

Events are emitted ONLY on meaningful state changes (dedupe on state change),
so unchanged observations never spam the event log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .observations import Observation
from .process_state import TERMINAL_STATES, JobRecord, JobState

EVENT_PROCESS_STARTED = "PROCESS_STARTED"
EVENT_CHECKPOINT_REACHED = "CHECKPOINT_REACHED"
EVENT_QUIET_RUNNING_ENTERED = "QUIET_RUNNING_ENTERED"
EVENT_STALL_SUSPECTED = "STALL_SUSPECTED"
EVENT_DEADLINE_REACHED = "DEADLINE_REACHED"
EVENT_EXITED_SUCCESS = "PROCESS_EXITED_SUCCESS"
EVENT_EXITED_FAILURE = "PROCESS_EXITED_FAILURE"
EVENT_LOST = "PROCESS_LOST"
EVENT_CANCELLED = "PROCESS_CANCELLED"
# downstream layers (P7b/c/d)
EVENT_LAYA_DECIDED = "LAYA_DECIDED"
EVENT_SPARK_INTERPRETED = "SPARK_INTERPRETED"
EVENT_SUPERVISOR_WAKE = "SUPERVISOR_WAKE_REQUESTED"

_STATE_CHANGE_EVENTS: Dict[str, Optional[str]] = {
    JobState.QUIET_RUNNING: EVENT_QUIET_RUNNING_ENTERED,
    JobState.STALL_SUSPECTED: EVENT_STALL_SUSPECTED,
    JobState.DEADLINE_REACHED: EVENT_DEADLINE_REACHED,
    JobState.EXITED_SUCCESS: EVENT_EXITED_SUCCESS,
    JobState.EXITED_FAILURE: EVENT_EXITED_FAILURE,
    JobState.LOST: EVENT_LOST,
    JobState.CANCELLED: EVENT_CANCELLED,
    # STARTING/CHECKPOINT/RUNNING carry their own dedicated event handling
    JobState.STARTING: None,
    JobState.RUNNING: None,
    JobState.CHECKPOINT: None,
}


@dataclass
class Transition:
    changed: bool
    new_state: Optional[str] = None
    event_type: Optional[str] = None
    evidence: Dict[str, object] = field(default_factory=dict)


def evaluate(record: JobRecord, obs: Observation, policy, now: float) -> Transition:
    """Classify one observation into at most one state transition.

    Precedence (deterministic facts first — no model is ever consulted):
      1. explicit cancellation   -> CANCELLED   (never overridden by advisors)
      2. start-time mismatch     -> LOST (PID recycled; identity is untrusted)
      3. recorded exit           -> EXITED_SUCCESS / EXITED_FAILURE
      4. pid gone, no exit rec   -> LOST (daemon died across the exit)
      5. deadline crossing       -> DEADLINE_REACHED (one-shot; keeps watching)
      6. new checkpoint match    -> CHECKPOINT
      7. quiet classification    -> QUIET_RUNNING (quiet + high CPU)
                                   STALL_SUSPECTED (quiet + ~0 CPU + no activity)
      8. otherwise               -> RUNNING (or stays STARTING on first tick)
    """
    alive = record.state not in TERMINAL_STATES

    # 1. explicit cancellation — a deterministic fact, highest precedence
    if alive and record.cancel_requested:
        return _change(record, JobState.CANCELLED, EVENT_CANCELLED,
                       {"reason": "cancelled_requested", **obs.evidence()})

    if not alive:
        return Transition(changed=False)

    # 2. identity (PID reuse protection): never assume a matching PID
    if obs.identity_ok is False and obs.pid_exists:
        return _change(record, JobState.LOST, EVENT_LOST,
                       {"reason": "pid_recycled",
                        "stored_starttime": record.proc_starttime,
                        **obs.evidence()})

    # 3. recorded exit — exit status beats everything else
    if obs.exited:
        rc = obs.returncode
        code = "success" if rc == 0 else "failure"
        return _change(
            record,
            JobState.EXITED_SUCCESS if rc == 0 else JobState.EXITED_FAILURE,
            EVENT_EXITED_SUCCESS if rc == 0 else EVENT_EXITED_FAILURE,
            {"returncode": rc, "verification": code, **obs.evidence()},
        )

    # 4. pid gone with no exit record anywhere
    if obs.lost_reason:
        return _change(record, JobState.LOST, EVENT_LOST,
                       {"reason": obs.lost_reason, **obs.evidence()})

    # 5. deadline crossing (one-shot event; monitoring continues pre-terminal)
    if obs.deadline_exceeded and record.state != JobState.DEADLINE_REACHED:
        return Transition(
            changed=True,
            new_state=JobState.DEADLINE_REACHED,
            event_type=EVENT_DEADLINE_REACHED,
            evidence={"deadline_remaining_s": obs.deadline_remaining_s, **obs.evidence()},
        )

    # 6. new checkpoint (dedupe: only a NEW match transitions/events)
    if obs.checkpoint_match and obs.checkpoint_match != record.checkpoint:
        record.checkpoint = obs.checkpoint_match
        record.checkpoint_at = now
        record.last_activity_at = now
        record.quiet_since = None
        return Transition(
            changed=True,
            new_state=JobState.CHECKPOINT,
            event_type=EVENT_CHECKPOINT_REACHED,
            evidence={"checkpoint": obs.checkpoint_match[:200]},
        )

    # 7. quiet classification — the ambiguity lives ONLY here
    quiet = obs.quiet_s >= float(policy.quiet_after_s)
    activity = (
        obs.stdout_delta > 0
        or obs.stderr_delta > 0
        or obs.markers_changed
    )
    if not quiet:
        target = JobState.RUNNING
    elif obs.cpu_pct is None:
        # CPU baseline not yet known: be conservative, never suspect on no data
        target = JobState.RUNNING if record.state == JobState.STARTING else record.state
    elif obs.cpu_pct >= float(policy.active_cpu_pct):
        target = JobState.QUIET_RUNNING  # quiet + high CPU is NOT a stall
    elif obs.cpu_pct <= float(policy.stall_cpu_pct) and not activity:
        # quiet + ~0 CPU + no FS/output/checkpoint activity -> SUSPECTED, not STALLED
        target = JobState.STALL_SUSPECTED
    else:
        target = JobState.QUIET_RUNNING

    if target == record.state:
        return Transition(changed=False)
    return _change(record, target, _STATE_CHANGE_EVENTS.get(target), {"quiet": quiet, **obs.evidence()})


def _change(record: JobRecord, new_state: str, event_type: Optional[str],
            evidence: Dict[str, object]) -> Transition:
    if new_state not in TERMINAL_STATES:
        # same-state re-emission is already excluded by callers comparing state
        pass
    if event_type is None and new_state == record.state:
        return Transition(changed=False)
    return Transition(changed=True, new_state=new_state, event_type=event_type,
                      evidence=evidence)


def allowed_by_gate(state: str) -> bool:
    """The ambiguity gate is only ever consulted PRE-terminal.

    Hard rule (P7b): a LayaDecision can never affect a terminal deterministic
    state — the gate is simply not consultable there.
    """
    return state not in TERMINAL_STATES