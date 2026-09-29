"""P14 — Cloud protocol: the state machine that enforces cloud dormancy.

This module is the ONLY place a project-level cloud invocation can
happen. It enforces, deterministically and fail-closed:

* Cloud may be invoked only from NEW (CLARIFY), READY_TO_PLAN (BATCH)
  and REENTRY_READY (reentry). Any other state raises
  :class:`CloudInvocationBlocked` BEFORE the model is invoked — worker
  completion, progress events, logs and restarts cannot wake Gemini.
* Exactly one bounded cloud artifact is persisted per invocation
  (``cloud/NNNN-<mode>.json``), so the call count is auditable and a
  restart cannot replay a turn.
* One reentry generation invokes the cloud at most ONCE: the reentry
  record persists ``cloud_calls`` and the reservation is written BEFORE
  the model call (a crash can consume a call, never duplicate one).
* The cloud output is a structured :data:`CloudAction`; any tool-event
  pattern in the raw response is a protocol violation (fail closed) —
  the Classification-B interim defense while native tool schemas remain
  visible to the model (see package docstring).

The prompt builders live in :mod:`planner` and are imported LAZILY
inside methods to keep the module import graph acyclic
(planner -> protocol is the only static edge).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Protocol

from app.orchestration.cloud_batch.schemas import (
    BatchOutcome,
    BatchPlan,
    ClarificationRequest,
    CloudAction,
    CloudProjectState,
    FinalResult,
    MAX_REENTRY_CALLS_PER_GENERATION,
    NeedsHuman,
    ProjectContract,
    REENTRY_REASONS,
)
from app.orchestration.cloud_batch.state import ProjectStore


class CloudProtocolError(Exception):
    """Base error for P14 cloud protocol failures (fail closed)."""


class CloudInvocationBlocked(CloudProtocolError):
    """A cloud invocation was attempted from a state where it is not
    allowed. Raised BEFORE any model call."""


class CloudProtocolViolation(CloudProtocolError):
    """The cloud produced a malformed action or a tool-use event — the
    turn is voided (fail closed; a plan is never fabricated)."""


#: Conservative, high-precision markers for native/MCP tool invocation
#: leaking into a structured CloudAction response. Matched against the
#: RAW response text before JSON extraction.
_TOOL_EVENT_PATTERNS = (
    re.compile(r'"tool_calls"\s*:', re.I),
    re.compile(r'"toolCalls"\s*:', re.I),
    re.compile(r'"tool_use"\s*:', re.I),
    re.compile(r'"toolUse"\s*:', re.I),
    re.compile(r'"functionCall"\s*:', re.I),
    re.compile(r'"function_call"\s*:', re.I),
    re.compile(
        r'"name"\s*:\s*"(run_shell_command|shell|exec_command|bash|'
        r'write_file|edit_file|patch_file|read_file|hybrid_start|'
        r'hybrid_resume|agy_[a-z0-9_]+|serena_[a-z_]+)"',
        re.I,
    ),
)


def scan_for_tool_events(text: str) -> list[str]:
    """Return the matched tool-event markers in ``text`` (empty = clean).

    Conservative by design: false positives fail closed (safe), false
    negatives are covered by the protocol-level mode guard (an action
    that is not one of the four CloudAction modes is rejected).
    """
    return [
        pattern.search(text).group(0)
        for pattern in _TOOL_EVENT_PATTERNS
        if pattern.search(text)
    ]


class CloudPlannerPort(Protocol):
    """The injected cloud planner (tests use fakes; production uses
    :class:`app.orchestration.cloud_batch.planner.CloudPlanner`)."""

    def clarify(self, prompt: str) -> ClarificationRequest: ...

    def batch(self, prompt: str) -> BatchPlan: ...

    def reentry(self, prompt: str) -> CloudAction: ...


class CloudProtocol:
    """One project's cloud protocol. All state transitions persist to the
    :class:`ProjectStore` before being reported (crash-safe ordering).
    """

    def __init__(
        self,
        store: ProjectStore,
        project_id: str,
        planner: CloudPlannerPort,
        *,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.project_id = project_id
        self.planner = planner
        self._now = now

    # -- state access --------------------------------------------------------

    def contract(self) -> ProjectContract:
        return self.store.load_project(self.project_id)

    def state(self) -> CloudProjectState:
        return self.contract().state

    def _set_state(self, state: CloudProjectState, **fields: Any) -> ProjectContract:
        contract = self.store.load_project(self.project_id)
        contract.state = state
        for key, value in fields.items():
            setattr(contract, key, value)
        self.store.save_project(contract)
        return contract

    # -- guards ----------------------------------------------------------------

    @staticmethod
    def _require(current: CloudProjectState, allowed: tuple[str, ...], what: str) -> None:
        if current not in allowed:
            raise CloudInvocationBlocked(
                f"cloud invocation {what!r} is not allowed from state "
                f"{current.value} (allowed: {', '.join(allowed)})"
            )

    # -- cloud call 1: CLARIFY (NEW only) ----------------------------------------

    def request_clarify(self) -> ClarificationRequest:
        """Exactly one clarification turn. Zero worker dispatch before it."""
        from app.orchestration.cloud_batch.planner import build_clarify_prompt

        self._require(self.state(), ("NEW",), "CLARIFY")
        contract = self.contract()
        prompt = build_clarify_prompt(contract)
        try:
            action = self.planner.clarify(prompt)
        except CloudProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(f"clarify turn failed: {exc}") from exc
        if not isinstance(action, ClarificationRequest):
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(
                f"clarify turn returned {type(action).__name__}, expected "
                "ClarificationRequest"
            )
        self.store.save_cloud_action(self.project_id, action, "clarify")
        self._set_state(CloudProjectState.WAITING_FOR_USER)
        return action

    # -- deterministic: user answer (NO cloud) ----------------------------------

    def record_user_answer(self, answer: str) -> ProjectContract:
        """WAITING_FOR_USER -> READY_TO_PLAN. The answer is stored
        VERBATIM as data — never an instruction channel."""
        self._require(self.state(), ("WAITING_FOR_USER",), "record_user_answer")
        if not answer.strip():
            raise CloudProtocolError("user answer must not be blank")
        return self._set_state(CloudProjectState.READY_TO_PLAN, clarification_answer=answer)

    # -- cloud call 2: BATCH (READY_TO_PLAN only) ---------------------------------

    def request_batch(self) -> BatchPlan:
        """One BATCH turn from the full ProjectContract. After a valid
        BatchPlan the cloud is DORMANT (state BATCH_RUNNING) — no further
        invocation is possible until the deterministic reentry gate."""
        from app.orchestration.cloud_batch.planner import build_batch_prompt
        from app.orchestration.cloud_batch.roles import render_role_manifest

        self._require(self.state(), ("READY_TO_PLAN",), "BATCH")
        contract = self.contract()
        prompt = build_batch_prompt(
            contract, role_manifest=render_role_manifest()
        )
        try:
            plan = self.planner.batch(prompt)
        except CloudProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(f"batch turn failed: {exc}") from exc
        if not isinstance(plan, BatchPlan):
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(
                f"batch turn returned {type(plan).__name__}, expected BatchPlan"
            )
        # Persist the call + plan, then move state (crash-safe order).
        self.store.save_cloud_action(self.project_id, plan, "batch")
        self.store.save_batch_plan(self.project_id, plan.batch_id, plan)
        self._set_state(
            CloudProjectState.BATCH_RUNNING, generation=contract.generation + 1
        )
        return plan

    # -- deterministic: reentry gate (NO cloud) ------------------------------------

    def mark_reentry_ready(self, batch_id: str, reason: str) -> None:
        """BATCH_RUNNING -> REENTRY_READY. Deterministic; the dispatcher
        (or the human-continuation supervisor) is the only caller.
        ``reason`` must be one of REENTRY_REASONS (validated here and by
        the ReentryGate schema at plan time)."""
        self._require(self.state(), ("BATCH_RUNNING",), "mark_reentry_ready")
        if reason not in REENTRY_REASONS:
            raise CloudProtocolError(f"unknown reentry reason {reason!r}")
        contract = self.contract()
        self.store.save_reentry(
            self.project_id,
            batch_id,
            {
                "batch_id": batch_id,
                "reason": reason,
                "generation": contract.generation,
                "cloud_calls": 0,
                "max_cloud_calls": MAX_REENTRY_CALLS_PER_GENERATION,
            },
        )
        self._set_state(CloudProjectState.REENTRY_READY)

    # -- cloud call 3: reentry (REENTRY_READY only, once per generation) -----------

    def request_reentry(self, batch_id: str) -> CloudAction:
        """Supervisor reentry: at most ONE cloud call per generation.

        The call reservation is persisted BEFORE the model call, so a
        crash can consume the generation's call but never duplicate it.
        """
        from app.orchestration.cloud_batch.planner import build_reentry_prompt
        from app.orchestration.cloud_batch.roles import render_role_manifest

        self._require(self.state(), ("REENTRY_READY",), "reentry")
        record = self.store.load_reentry(self.project_id, batch_id)
        if not record:
            raise CloudProtocolError(
                f"no reentry record for batch {batch_id!r} — "
                "mark_reentry_ready must run first (deterministic gate)"
            )
        if int(record.get("cloud_calls", 0)) >= MAX_REENTRY_CALLS_PER_GENERATION:
            raise CloudInvocationBlocked(
                f"reentry generation {record.get('generation')} already "
                "consumed its single cloud call — refusing duplicate invocation"
            )
        # Reserve the call BEFORE invoking (persisted first).
        record = dict(record)
        record["cloud_calls"] = int(record.get("cloud_calls", 0)) + 1
        record["call_in_progress"] = True
        self.store.save_reentry(self.project_id, batch_id, record)

        outcome_path = self.store.batch_dir(self.project_id, batch_id) / "outcome.json"
        outcome = BatchOutcome.model_validate(
            json.loads(outcome_path.read_text(encoding="utf-8"))
        )
        contract = self.contract()
        prompt = build_reentry_prompt(contract, outcome, render_role_manifest())
        try:
            action = self.planner.reentry(prompt)
        except CloudProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(f"reentry turn failed: {exc}") from exc
        record = dict(record)
        record["call_in_progress"] = False
        self.store.save_reentry(self.project_id, batch_id, record)

        if isinstance(action, BatchPlan):
            self.store.save_cloud_action(self.project_id, action, "batch")
            self.store.save_batch_plan(self.project_id, action.batch_id, action)
            self._set_state(
                CloudProjectState.BATCH_RUNNING, generation=contract.generation + 1
            )
        elif isinstance(action, NeedsHuman):
            self.store.save_cloud_action(self.project_id, action, "needs_human")
            self._set_state(CloudProjectState.NEEDS_HUMAN)
        elif isinstance(action, FinalResult):
            self.store.save_cloud_action(self.project_id, action, "final")
            target = (
                CloudProjectState.COMPLETE
                if action.success
                else CloudProjectState.FAILED
            )
            self._set_state(target)
        elif isinstance(action, ClarificationRequest):
            # Clarify is only legal from NEW — mid-project clarify is a
            # protocol violation (fail closed).
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(
                "CLARIFY is only legal from state NEW; mid-project "
                "clarify is a protocol violation"
            )
        else:  # pragma: no cover - TypeAdapter guarantees the union
            self._set_state(CloudProjectState.FAILED)
            raise CloudProtocolViolation(
                f"unexpected cloud action type {type(action).__name__}"
            )
        return action

    # -- deterministic: NEEDS_HUMAN continuation entry ------------------------------

    def enter_needs_human_from_state(self) -> None:
        """For callers that park a human request outside a reentry turn
        (e.g. the supervisor deciding NEEDS_HUMAN before invoking the
        cloud). NEEDS_HUMAN is a park: no cloud call, no dispatch."""
        self._require(
            self.state(), ("BATCH_RUNNING", "REENTRY_READY"), "enter_needs_human"
        )
        self._set_state(CloudProjectState.NEEDS_HUMAN)

    def resume_from_needs_human(self) -> None:
        """Human decision + (where applicable) P13-admitted continuation
        has been applied deterministically: back to READY_TO_PLAN for the
        next single BATCH turn."""
        self._require(self.state(), ("NEEDS_HUMAN",), "resume_from_needs_human")
        self._set_state(CloudProjectState.READY_TO_PLAN)

    # -- deterministic terminal ------------------------------------------------------

    def fail(self, reason: str) -> None:
        """Deterministic terminal failure (malformed artifact, operator
        cancel). Legal from any non-terminal state."""
        if self.state() in (CloudProjectState.COMPLETE, CloudProjectState.FAILED):
            raise CloudProtocolError("project is already terminal")
        self._set_state(CloudProjectState.FAILED)
        del reason  # callers log the reason themselves; state stays canonical
