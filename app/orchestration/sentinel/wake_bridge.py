"""P7d — SUPERVISOR WAKE BRIDGE (MiMo Pro wake contract).

MiMo Pro is woken ONLY for genuine intervention. Normal running, success,
Laya-resolved ambiguity and Spark WAIT produce ZERO wakeups.

Wake reasons (exhaustive default set, configurable): repair_required,
scope_decision, permission_needed, replan_needed, repeated_stall,
deadline_policy, verification_failure, spark_intervention.

Dedup/bounds: at most 1 wakeup per distinct (job, reason, episode), plus a
per-job absolute cap; repeated-stall wakeups are bounded by the engine's
wake policy (1 immediate + 1 after grace, then ESCALATED -> stop auto-waking).

Delivery: the "waker" is an injectable callback seam (the supervisor adapter).
EVERY accepted wakeup is ALSO persisted to the wake queue file and emitted as
a SUPERVISOR_WAKE_REQUESTED event by the engine — event/queue correctness is
the proof; a real MiMo Pro call consuming a packet is optional.

Packet: compact, allowlisted fields only, never full logs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

WAKE_REASONS = frozenset({
    "repair_required",
    "scope_decision",
    "permission_needed",
    "replan_needed",
    "repeated_stall",
    "deadline_policy",
    "verification_failure",
    "spark_intervention",
})

WAKE_PACKET_FIELDS = (
    "job_id",
    "state",
    "reason",
    "episode",
    "elapsed_s",
    "last_activity_at",
    "checkpoint",
    "evidence",
    "laya",
    "spark",
    "pointers",
)


@dataclass
class WakePolicy:
    enabled_reasons: frozenset = WAKE_REASONS
    min_stall_episodes: int = 3      # repeated stall: >= N episodes without giving up
    wake_grace_s: float = 60.0       # grace before the (bounded) second stall wakeup
    max_stall_wakes: int = 2         # 1 immediate + 1 after grace, then ESCALATED
    max_total_wakes_per_job: int = 8
    max_packet_bytes: int = 8192


def build_wake_packet(record, *, reason: str, episode: int, now: float,
                      evidence: Dict[str, Any], laya: Optional[Dict[str, Any]],
                      spark: Optional[Dict[str, Any]],
                      pointers: Dict[str, str],
                      policy: WakePolicy) -> Dict[str, Any]:
    """Compact, allowlisted wake packet (bounded size)."""
    packet: Dict[str, Any] = {
        "job_id": record.job_id,
        "state": record.state,
        "reason": reason,
        "episode": int(episode),
        "elapsed_s": round(max(0.0, now - record.started_at), 3),
        "last_activity_at": record.last_activity_at,
        "checkpoint": (str(record.checkpoint)[:200] if record.checkpoint else None),
        "evidence": {k: evidence.get(k) for k in list(evidence)[:16]},
        "laya": laya,
        "spark": spark,
        "pointers": {k: str(v) for k, v in pointers.items()},
    }
    # bounded by construction; trim evidence progressively if ever oversized
    while len(json.dumps(packet, separators=(",", ":")).encode("utf-8")) > policy.max_packet_bytes:
        ev = packet["evidence"]
        if ev:
            ev.pop(next(iter(ev)), None)
        else:
            packet["checkpoint"] = (packet["checkpoint"] or "")[:64]
            break
    return packet


class WakeBridge:
    """Persists wake packets to a queue file, dedupes per (job, reason,
    episode), and delivers through an injectable callback seam."""

    def __init__(self, root, policy: Optional[WakePolicy] = None, *,
                 waker: Optional[Callable[[Dict[str, Any]], Any]] = None,
                 clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.root / "wake_queue.jsonl"
        self.policy = policy or WakePolicy()
        self.waker = waker
        self.clock = clock
        self.delivered: List[Dict[str, Any]] = []
        self.delivery_failures: List[str] = []
        self.pending = self._load_queue()

    def _load_queue(self) -> List[Dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with open(self.queue_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return out

    def request(self, record, *, reason: str, episode: int,
                evidence: Dict[str, Any], laya: Optional[Dict[str, Any]] = None,
                spark: Optional[Dict[str, Any]] = None,
                pointers: Optional[Dict[str, str]] = None,
                slot: int = 0) -> Optional[Dict[str, Any]]:
        """Returns the accepted wake packet, or None (disabled / deduped / capped).

        dedupe key = (job_id, reason, episode, slot): max 1 per distinct
        (job, reason, episode) — slot 0 is the normal spot; the contract's
        bounded second repeated-stall wakeup after grace uses slot 1.
        """
        policy = self.policy
        if reason not in policy.enabled_reasons:
            return None
        key = f"{record.job_id}:{reason}:{int(episode)}:{int(slot)}"
        if key in record.wake_keys:
            return None  # max 1 wakeup per distinct (job, reason, episode)
        if len(record.wake_keys) >= policy.max_total_wakes_per_job:
            return None  # global per-job bound: bounded disk, bounded wake spam

        packet = build_wake_packet(
            record, reason=reason, episode=episode, now=self.clock(),
            evidence=evidence, laya=laya, spark=spark,
            pointers=pointers or {}, policy=policy)
        record.wake_keys.append(key)  # mutate BEFORE persist: accepted exactly once

        entry = {"ts": round(self.clock(), 3),
                 "queue_len": len(self.pending) + 1,
                 "packet": packet}
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        try:
            with open(self.queue_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
        except OSError:
            pass
        self.pending.append(entry)

        if self.waker is not None:
            try:
                self.waker(packet)
                self.delivered.append(packet)
            except Exception as exc:  # delivery seam must never break sentinel
                self.delivery_failures.append(f"{reason}:{type(exc).__name__}")
        return packet