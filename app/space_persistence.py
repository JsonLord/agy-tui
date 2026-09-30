"""P21-B — Persistence gate for disposable-runtime deployments (HF Space).

A Space container may or may not offer persistent storage (``/data``).
The rule (non-negotiable):

  * persistent storage present + writable  → durable mode (state survives)
  * absent + mode=production               → FAIL CLEARLY (never run
    silently on a disposable filesystem as if it were durable)
  * absent + mode=demo                     → demo mode: the process
    starts, but EVERY readiness/health payload is stamped
    ``state_persistence: "DISPOSABLE"`` so no client can mistake the
    state for durable.

Stdlib only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class PersistenceError(RuntimeError):
    """Raised when production mode is required but no durable state root
    is available."""


@dataclass(frozen=True)
class PersistenceMode:
    mode: str  # "durable" | "disposable-demo"
    state_root: str
    persistent: bool
    reason: str


def resolve_persistence(
    state_root: str,
    *,
    require_persistent: bool,
    persistent_marker: str = "/data",
) -> PersistenceMode:
    """Decide durable vs demo mode for the given state root.

    A state root counts as PERSISTENT when it lives under the platform's
    persistent mount (``persistent_marker``, default ``/data``) or when
    the env ``AGY_STATE_PERSISTENT=1`` explicitly asserts it. Everything
    else is disposable.
    """
    root = Path(state_root)
    marker = persistent_marker or ""

    env_asserted = os.environ.get("AGY_STATE_PERSISTENT", "").strip() == "1"
    under_mount = bool(marker) and str(root).startswith(marker.rstrip("/") + "/")
    persistent = env_asserted or under_mount

    reason = (
        "AGY_STATE_PERSISTENT asserted"
        if env_asserted
        else f"state root under persistent mount {marker}"
        if under_mount
        else "state root is on the (disposable) container filesystem"
    )

    if not root.exists():
        # The durable mount must exist to be usable.
        if require_persistent:
            raise PersistenceError(
                f"production requires a persistent state root, but "
                f"'{root}' does not exist (expected under '{marker}')"
            )
        root.mkdir(parents=True, exist_ok=True)

    if not root.is_dir():
        raise PersistenceError(f"state root '{root}' is not a directory")

    if not os.access(root, os.W_OK):
        raise PersistenceError(f"state root '{root}' is not writable")

    if not persistent and require_persistent:
        raise PersistenceError(
            f"production mode requires durable state; '{root}' is "
            f"DISPOSABLE ({reason}). Mount '{marker}' at the state root "
            f"or run in demo mode (AGY_SPACE_MODE=demo) — demo state "
            f"does NOT survive container restarts."
        )

    return PersistenceMode(
        mode="durable" if persistent else "disposable-demo",
        state_root=str(root),
        persistent=persistent,
        reason=reason,
    )


def readiness_stamp(mode: PersistenceMode) -> dict:
    """The stamp every readiness/health payload carries in a
    disposable deployment (P21-B: never silently pretend ephemeral
    state is durable)."""
    return {
        "state_persistence": "durable" if mode.persistent else "DISPOSABLE",
        "mode": mode.mode,
        "state_root": mode.state_root,
        "persists_across_restarts": bool(mode.persistent),
    }
