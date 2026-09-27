"""AGY P7 process sentinel — local event-driven runtime.

CORE INVARIANT: a model never owns a wait loop. Deterministic facts (exit
code, PID/pgid, /proc start time, deadline, stdout/stderr growth, CPU,
checkpoint regex, FS markers, cancellation) NEVER go through Laya. Laya is a
fail-safe ambiguity gate, Spark an advice-only incident interpreter, MiMo Pro
wakes only for genuine intervention.

Layers: process_state / observations / state_machine / watcher / events /
laya_gate / spark_interpreter / wake_bridge / metrics / api / daemon / cli.
"""

from .api import SentinelRuntime
from .process_state import JobRecord, JobState, TERMINAL_STATES
from .wake_bridge import WakePolicy
from .watcher import WatchPolicy

__all__ = [
    "SentinelRuntime",
    "JobRecord",
    "JobState",
    "TERMINAL_STATES",
    "WatchPolicy",
    "WakePolicy",
]