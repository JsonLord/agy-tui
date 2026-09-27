"""P5 explicit opt-in transient fault injection (live-failover evidence).

Small, pure and DEAD BY DEFAULT. This module exists so scenario B can
drive provider-boundary evidence through the REAL runtime failover path
when a genuine transient fault cannot be provoked on demand; the
mandatory dispatch case injects the 429 at the provider boundary
instead (a local 127.0.0.1-only stub + temporary OpenCode provider
swap), and THIS class is the documented composable controller for the
``run_supervised`` seam.

* :class:`TransientFaultInjector` wraps ANY ``run_agy_supervised``-
  shaped callable (``(settings, repo, prompt, *, agent, session) ->
  ExternalResult``). While ARMED (``injections_remaining > 0``) and
  called with the configured ``primary_agent``, it returns a synthetic
  ``ExternalResult(ok=False, ... stderr=transient_text)`` WITHOUT
  touching the wrapped runner, decrements the counter and records the
  injection in ``.history`` (``attempt_index``, ``agent``,
  ``session``). Every other call — the fallback agent, or any call
  after the counter is exhausted — passes straight through to the
  real runner with the arguments forwarded verbatim (same session).
* ``transient_text`` classifies as transient via
  ``failover.is_transient_provider_failure``, which is what makes the
  synthetic failure take the honest failover path (no gate is
  weakened anywhere).
* Safety, each enforced by a test in
  ``tests/test_agy_fault_injection.py``:
  1. NOTHING in ``agy_runtime`` / ``agy_plan`` / ``agy_execute`` /
     ``agy_gatekeeper`` imports or references this module (static
     source scan), and ``build_live_executor()`` without an injected
     ``run_supervised`` still dispatches through ``run_agy_supervised``
     (proven with a patched default binding);
  2. arming requires BOTH the explicit constructor AND passing the
     instance as ``run_supervised=`` to ``build_live_executor`` — no
     production path constructs it;
  3. :func:`assert_injection_not_armed` — the environment guard the
     live driver calls before arming: it raises ``RuntimeError`` when
     ``AGY_FAULT_INJECTION=1`` is set while ``AGY_LIVE`` is NOT set
     (an armed-injection environment outside a live run), and returns
     normally in every other combination. Semantics, precisely:
       - ``AGY_FAULT_INJECTION`` missing/empty/anything != "1":
         not armed, never raises (fault injection is opt-in);
       - ``AGY_FAULT_INJECTION=1`` and ``AGY_LIVE`` unset/empty:
         RAISES (misconfiguration: armed-looking env outside a live
         run);
       - ``AGY_FAULT_INJECTION=1`` and ``AGY_LIVE`` set to anything
         non-empty: allowed (the live driver may arm);
       - ``AGY_LIVE=1`` alone: never raises (the guard only polices
         the FAULT_INJECTION flag, it does not police liveness).

No network, no subprocess, no model call: the ONLY effect of this
module is the synthetic ``ExternalResult`` (or the pass-through).
"""

from __future__ import annotations

import os
from typing import Any, Callable

from app.integrations.external_stack import ExternalResult

FAULT_INJECTION_ENV = "AGY_FAULT_INJECTION"
LIVE_ENV = "AGY_LIVE"

#: Default synthetic failure: a genuine HTTP-429-style rate-limit
#: message. It must classify as transient per
#: ``failover.is_transient_provider_failure`` (asserted by test 5).
DEFAULT_TRANSIENT_TEXT = (
    "HTTP 429 Too Many Requests: provider rate limit exceeded "
    "(rate_limit_exhausted); please retry later."
)

Runner = Callable[..., ExternalResult]


def assert_injection_not_armed(settings: Any = None) -> None:
    """
    Environment guard documented in the module docstring (§3).

    Raises ``RuntimeError`` iff ``AGY_FAULT_INJECTION`` is exactly
    "1" while ``AGY_LIVE`` is unset/empty — i.e. an armed-looking
    environment outside a live run. ``settings`` is accepted for
    call-site symmetry with the runtime seams (not consulted).
    """
    _ = settings
    if os.environ.get(FAULT_INJECTION_ENV) == "1" and not os.environ.get(
        LIVE_ENV
    ):
        raise RuntimeError(
            f"{FAULT_INJECTION_ENV}=1 with {LIVE_ENV} unset: transient "
            "fault injection is only permitted inside a LIVE run "
            f"(set {LIVE_ENV} in the live driver, never elsewhere)"
        )


class TransientFaultInjector:
    """
    Opt-in wrapper for one ``run_supervised``-shaped callable.

    ``max_injections`` transient failures are returned for
    ``primary_agent`` calls before everything (including further
    primary calls) passes through to ``runner``; calls for any other
    agent — the configured fallback — always pass through.
    """

    def __init__(
        self,
        runner: Runner,
        *,
        primary_agent: str,
        max_injections: int = 4,
        transient_text: str = DEFAULT_TRANSIENT_TEXT,
    ) -> None:
        if not callable(runner):
            raise TypeError("runner must be callable")
        if not isinstance(primary_agent, str) or not primary_agent.strip():
            raise ValueError("primary_agent must be a non-empty string")
        if int(max_injections) < 0:
            raise ValueError("max_injections must be >= 0")

        self.runner: Runner = runner
        self.primary_agent = primary_agent
        self.max_injections = int(max_injections)
        self.transient_text = str(transient_text)
        self.injections_remaining = self.max_injections
        self.history: list[dict[str, Any]] = []
        self._calls = 0

    @property
    def exhausted(self) -> bool:
        """True once no transient injection budget remains."""
        return self.injections_remaining <= 0

    def __call__(
        self,
        settings: Any,
        repo: Any,
        prompt: str,
        *,
        agent: str,
        session: str | None = None,
        **kw: Any,
    ) -> ExternalResult:
        self._calls += 1

        armed = self.injections_remaining > 0
        is_primary = agent == self.primary_agent

        if armed and is_primary:
            self.injections_remaining -= 1
            self.history.append(
                {
                    "attempt_index": self._calls,
                    "agent": agent,
                    "session": session,
                }
            )
            # NOTE: ExternalResult's constructor fields are
            # ok/returncode/stdout/stderr (``output`` is a read-only
            # concatenation property), so the synthetic failure uses
            # stderr == transient_text to surface into the executor's
            # failure classification exactly like a real failing run.
            return ExternalResult(
                ok=False,
                returncode=1,
                stdout="",
                stderr=self.transient_text,
            )

        # Pass-through: counter exhausted OR fallback (non-primary)
        # agent — arguments forwarded verbatim (same session).
        return self.runner(
            settings,
            repo,
            prompt,
            agent=agent,
            session=session,
            **kw,
        )