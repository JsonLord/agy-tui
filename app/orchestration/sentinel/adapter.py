"""Thin adapter SEAM for AGY's execute path (explicitly DEFERRED, P7 contract).

The P1-P6 execution modules (execute_plan / execute_stage / agy_plan /
agy_resume / agy_commitgate / agy_timeline) are intentionally NOT rewired in
P7: the process-wait delegation stays a documented seam. Wiring this into
execute_stage is deferred to a later phase.

The seam itself is real and usable: `wait_for_external_work()` maps "AGY needs
a worker to run" onto spawn_monitored_process + deterministic local watching;
the executor then YIELDS (it never polls) and would consume events/wake
packets instead of blocking:

    runtime = SentinelRuntime(state_root, policy)
    job_id = wait_for_external_work(...)          # spawn (seam)
    ... yield: watcher owns the whole wait period (event-out, no polling)
    # on wake: adapter.handle_wake_packet(packet)  -> repair/scope/replan
    # terminal state comes from get_process_snapshot() only for bookkeeping.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .api import SentinelRuntime


def wait_for_external_work(
    runtime: SentinelRuntime,
    command,
    cwd: Optional[str] = None,
    deadline_s: Optional[float] = None,
    checkpoint_patterns=(),
    activity_markers=(),
    verify: Optional[Dict[str, Any]] = None,
) -> str:
    """SEAM: spawn a monitored process. Returns job_id; the caller must NOT
    poll — the deterministic watcher owns the wait and the wake bridge
    re-engages the supervisor only on genuine intervention."""
    return runtime.spawn_monitored_process(
        command, cwd=cwd, deadline_s=deadline_s,
        checkpoint_patterns=checkpoint_patterns,
        activity_markers=activity_markers, verify=verify)


def make_executor_waker(runtime: SentinelRuntime,
                        dispatch: Optional[Callable[[Dict[str, Any]], Any]] = None
                        ) -> Callable[[Dict[str, Any]], Any]:
    """SEAM: build the injectable supervisor-wake callback the daemon accepts.
    dispatch maps a compact wake packet onto a MiMo Pro event later; in P7 the
    queue file + SUPERVISOR_WAKE_REQUESTED event are the proof."""

    def _waker(packet: Dict[str, Any]) -> None:
        if dispatch is not None:
            dispatch(packet)

    return _waker