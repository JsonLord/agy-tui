"""Shared helpers for the AGY P7 sentinel tests.

Real local processes (cheap bounded python workers) + injected clock/policy
so compressed scenarios run in well under 5s wall time each.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from app.orchestration.sentinel.laya_gate import LayaDecision, LayaGate
from app.orchestration.sentinel.spark_interpreter import SparkVerdict
from app.orchestration.sentinel.wake_bridge import WakePolicy
from app.orchestration.sentinel.watcher import SentinelEngine, WatchPolicy


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def fast_policy(**over) -> WatchPolicy:
    """Compressed intervals: a 'multi-minute quiet' scenario runs in <5s."""
    base: Dict[str, Any] = dict(
        starting_interval_s=0.02,
        stable_interval_s=0.05,
        urgent_interval_s=0.02,
        quiet_after_s=0.15,
        urgent_quiet_margin_s=0.05,
        urgent_deadline_margin_s=30.0,   # deadline urgency off unless tested
        exit_file_grace_s=0.15,
        spark_grace_after_stall_s=0.4,
        cancel_grace_s=0.1,
        holder_start_timeout_s=20.0,
    )
    base.update(over)
    return WatchPolicy(**base)


# -- tiny local workers (test doubles, bounded lifetime, self-exiting) --------

PY = sys.executable


def worker_sleep(seconds: float, prints: bool = True) -> List[str]:
    tail = ("print('hello', flush=True)\n" if prints else "")
    return [PY, "-c", (
        "import time,sys\n"
        f"{tail}"
        f"time.sleep({seconds})\n"
        + ("print('done', flush=True)\n" if prints else "")
    )]


def worker_exit(code: int, sleep: float = 0.15) -> List[str]:
    return [PY, "-c", (
        "import time,sys\n"
        "print('hello', flush=True)\n"
        f"time.sleep({sleep})\n"
        "print('bye', flush=True)\n"
        f"sys.exit({code})\n"
    )]


def worker_checkpoints(n: int = 3, gap: float = 0.25) -> List[str]:
    script = (
        "import time\n"
        f"n = {n}\n"
        "for i in range(1, n + 1):\n"
        "    print(f'[ckpt] phase {i}/{n}', flush=True)\n"
        f"    time.sleep({gap})\n"
    )
    return [PY, "-c", script]


def worker_spinner(seconds: float) -> List[str]:
    return [PY, "-c", (
        "import time\n"
        "end = time.time() + " + str(seconds) + "\n"
        "x = 0\n"
        "while time.time() < end:\n"
        "    x += x * 2 + 1\n"
    )]


def worker_stall_then_exit(prints: int = 5, gap: float = 0.35) -> List[str]:
    """Prints, then goes fully silent/sleeping several times -> repeated STALL episodes."""
    return [PY, "-c", (
        "import time\n"
        f"for i in range({prints}):\n"
        "    print(f'work unit {i}', flush=True)\n"
        f"    time.sleep({gap})\n"
    )]


def worker_silence_then_print(quiet_s: float) -> List[str]:
    """Output burst, then a long silent sleep, then exits 0 (ambiguous quiet)."""
    script = (
        "import time\n"
        "print('phase 1/2', flush=True)\n"
        f"time.sleep({quiet_s})\n"
        "print('phase 2/2', flush=True)\n"
        "time.sleep(0.15)\n"
    )
    return [PY, "-c", script]


# -- drive helpers -------------------------------------------------------------

def ticks_until(rt, job_id: str, predicate: Callable[[Any], bool],
                *, timeout: float = 3.0, interval: float = 0.02) -> Any:
    """Drive rt.tick() until predicate(rec) or timeout. Returns the record."""
    deadline = time.monotonic() + timeout
    rec = rt.engine.get(job_id)
    while time.monotonic() < deadline:
        rt.tick(job_id)
        rec = rt.engine.get(job_id)
        if predicate(rec):
            return rec
        time.sleep(interval)
    return rec


def wait_terminal(rt, job_id: str, *, timeout: float = 5.0, interval: float = 0.02) -> Any:
    return ticks_until(
        rt, job_id,
        lambda r: r.state in ("EXITED_SUCCESS", "EXITED_FAILURE", "LOST"),
        timeout=timeout, interval=interval)


def safe_cleanup(rt, job_id: str) -> None:
    try:
        rt.cancel_process(job_id)
    except Exception:
        pass


def event_types(rt, job_id: str) -> List[str]:
    return [e["event_type"] for e in rt.event_log.tail_for(job_id, n=10_000)]


def events_of(rt, job_id: str, event_type: str) -> List[Dict[str, Any]]:
    return [e for e in rt.event_log.tail_for(job_id, n=10_000)
            if e.get("event_type") == event_type]


# -- injectable model doubles -------------------------------------------------

class RecordingLaya:
    """In-repo mock of the hosted Laya contract (records every call)."""

    def __init__(self, *, status: str = "ok", choice: str = "WAIT",
                 notify: float = 0.1, interpret: float = 0.1,
                 confidence: float = 0.9, failure: Optional[str] = None):
        self.status = status
        self.choice = choice
        self.notify = notify
        self.interpret = interpret
        self.confidence = confidence
        self.failure = failure
        self.calls = 0
        self.payloads: List[str] = []
        self.questions: List[Dict[str, Any]] = []

    def decide(self, state_text: str, questions) -> Dict[str, Any]:
        self.calls += 1
        self.payloads.append(state_text)
        self.questions.append(questions)
        if self.status != "ok":
            return {"status": "unknown", "answers": None,
                    "failure": self.failure or "mock_failure", "latency_s": 0.0}
        return {
            "status": "ok",
            "model": "laya-rl-agent",
            "latency_s": 0.0,
            "answers": {
                "classification": {"type": "choice", "choice": self.choice,
                                   "probabilities": {self.choice: self.confidence},
                                   "confidence": self.confidence},
                "notify_supervisor": {"type": "noul", "noul": self.notify},
                "needs_interpretation": {"type": "noul", "noul": self.interpret},
            },
        }


def counting_gate(laya: RecordingLaya) -> LayaGate:
    return LayaGate(client=laya)


class RecordingSpark:
    """Mock Spark: returns a pre-canned verdict, records every packet."""

    def __init__(self, verdict: Optional[SparkVerdict] = None,
                 status: str = "ok", action: str = "WAIT",
                 supervisor_needed: bool = False, wait_seconds=None,
                 confidence: float = 0.8):
        self.verdict = verdict or SparkVerdict(
            status=status, likely_state="quiet", confidence=confidence,
            recommended_action=None if status != "ok" else action,
            wait_seconds=wait_seconds, reason="mock",
            supervisor_needed=supervisor_needed,
            latency_s=0.0,
            failure=None if status == "ok" else f"mock_{status}",
        )
        self.packets: List[Dict[str, Any]] = []

    def interpret(self, packet):
        self.packets.append(packet)
        return self.verdict


class WakeRecorder:
    def __init__(self):
        self.packets: List[Dict[str, Any]] = []

    def __call__(self, packet):
        self.packets.append(packet)


def stall_policy(**over) -> WatchPolicy:
    return fast_policy(**over)


def test_stack(tmp_path, *, policy: Optional[WatchPolicy] = None,
               wake_policy: Optional[WakePolicy] = None,
               laya: Any = None, spark: Any = None,
               waker: Optional[Callable] = None, clock=None):
    from app.orchestration.sentinel.api import SentinelRuntime
    kwargs: Dict[str, Any] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if laya is not None:
        kwargs["laya_gate"] = counting_gate(laya)
    if spark is not None:
        kwargs["spark_interpreter"] = spark
    if waker is not None:
        kwargs["waker"] = waker
    return SentinelRuntime(tmp_path, policy or fast_policy(), wake_policy, **kwargs)