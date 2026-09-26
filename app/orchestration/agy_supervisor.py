"""
AGY supervisor dispatch seam.

Retains the opencode-supervise process/session lifecycle while
keeping the AGY execution path model-neutral: callers pass the
coding agent explicitly (no model-specific default), and the
supervisor lifecycle values come from AgySettings (stall/repeat/
restart), never from the legacy 120s factory default that
false-stalled slow generations.
"""

from __future__ import annotations

from pathlib import Path

from app.config import AgySettings, SupervisorLifecycle
from app.integrations.external_stack import (
    ExternalResult,
    run_supervised,
)


def default_timeout(lifecycle: SupervisorLifecycle) -> int:
    """
    Wall-clock budget sized for the configured stall policy: one
    full stall window per allowed attempt (initial + repeats +
    restarts) plus one window of headroom.
    """
    return lifecycle.stall_seconds * (
        lifecycle.repeat_limit + lifecycle.max_restarts + 1
    )


def run_agy_supervised(
    settings: AgySettings,
    repo: str | Path,
    task: str,
    *,
    agent: str,
    session: str | None = None,
    timeout: int | None = None,
) -> ExternalResult:
    """
    Run one bounded AGY work unit through opencode-supervise with
    explicit lifecycle values and an explicit agent.

    Model identity for the work unit is bound by the caller at
    agent selection; this seam carries no Gemma, Hermes, or other
    model-specific assumptions.
    """
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError(
            "agent must be supplied explicitly; AGY has no "
            "model-specific default agent"
        )

    lifecycle = settings.lifecycle

    return run_supervised(
        repo,
        task,
        agent=agent.strip(),
        session=session,
        repeat_limit=lifecycle.repeat_limit,
        stall_seconds=lifecycle.stall_seconds,
        max_restarts=lifecycle.max_restarts,
        timeout=(
            timeout if timeout is not None else default_timeout(lifecycle)
        ),
        cloud_led=True,
    )