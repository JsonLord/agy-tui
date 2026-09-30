"""P18 — Canonical project supervisor (the single production lifecycle
coordinator).

``ProjectSupervisor`` owns the project-level states

    NEW -> WAITING_FOR_USER -> READY_TO_PLAN -> BATCH_RUNNING
        -> REENTRY_READY -> (BATCH_RUNNING | NEEDS_HUMAN | COMPLETE
        | FAILED)
    (+ NEEDS_HUMAN parked from reentry)

It COORDINATES the accepted components — it does NOT replace them:

* P14 ``CloudProtocol`` — the ONLY cloud-invocation seam (CLARIFY from
  NEW, BATCH from READY_TO_PLAN, at most one reentry call per
  REENTRY_READY generation). The supervisor never invokes the model
  directly and never waits in a loop.
* P14/P16 ``authority.dispatch_batch`` — the only batch execution path
  (P16 guard: read-only runs directly, mutating lowers to canonical
  checkpoint intent, cloud authors no repo writes).
* P15 ``human_bridge`` — durable human responses with explicit
  provenance (a model payload can never resume a project).
* P11–P13 checkpoint runtime — gate execution authority (G0/G1/G2/G3
  via an injected :class:`CheckpointExecutor`; G4 strictly human-only
  through the canonical engine). ``submit_g4_decision`` is the only
  G4 path and it calls the engine, never fabricates a gate record.

``advance_project`` performs DETERMINISTIC work only, until one of:

* human input required      (WAITING_FOR_USER / NEEDS_HUMAN / G4 park)
* workers are running       (BATCH_RUNNING — call advance again after
                             the batch's deterministic run completes)
* cloud reentry required    (REENTRY_READY)
* a terminal state          (COMPLETE / FAILED)

At most ONE cloud call is made per ``advance_project`` invocation —
there are no model wait loops, no polling, and no recursive advance.
Restart recovery is inherited: every resume-unsafe unit (a CLARIFY
turn, a BATCH turn, a reentry turn, a worker run, a human response, a
gate record) is already idempotent in the component that owns it, and
the supervisor re-derives its next step from persisted canonical state
alone.

Project events (``events.jsonl``) are an OBSERVABILITY FEED, never
authority: canonical state (the P14 contract + batch artifacts +
checkpoint gate records) remains the single source of truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.orchestration.checkpoint_planning.schemas import (
        CheckpointPlan,
    )
    from app.orchestration.cloud_batch.planner import CloudPlanner
    from app.orchestration.cloud_batch.protocol import CloudProtocol

# Reused atomic pattern (P7 runstate convention).
from app.orchestration.agy_runstate import _atomic_write_json, safe_name
from app.orchestration.cloud_batch.human_bridge import (
    HumanBridgeError,
    park_view,
    record_human_response,
)
from app.orchestration.cloud_batch.planner import (
    CloudPlannerUnavailable,
    CloudTierCapacityError,
    QuotaRouter,
)
from app.orchestration.cloud_batch.protocol import (
    CloudInvocationBlocked,
    CloudPlannerPort,
    CloudProtocol,
    CloudProtocolError,
    CloudProtocolViolation,
)
from app.orchestration.cloud_batch.schemas import (
    BatchPlan,
    CloudProjectState,
    FinalResult,
    HumanResponse,
    NeedsHuman,
    ProjectContract,
)
from app.orchestration.cloud_batch.state import ProjectStore


class ProjectEventKind(str, Enum):
    PROJECT_CREATED = "PROJECT_CREATED"
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    HUMAN_RESPONSE_RECEIVED = "HUMAN_RESPONSE_RECEIVED"
    BATCH_CREATED = "BATCH_CREATED"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_TERMINAL = "WORKER_TERMINAL"
    REENTRY_READY = "REENTRY_READY"
    CLOUD_REENTERED = "CLOUD_REENTERED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    GATE_STATE_CHANGED = "GATE_STATE_CHANGED"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    PROJECT_COMPLETE = "PROJECT_COMPLETE"
    PROJECT_FAILED = "PROJECT_FAILED"
    PROJECT_CANCELLED = "PROJECT_CANCELLED"


@dataclass(frozen=True)
class ProjectEvent:
    """One compact, append-only observability event. Events are NOT
    authority — they never modify canonical state and are never read
    back to make a decision."""

    kind: ProjectEventKind
    project_id: str
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"kind": self.kind.value, "project_id": self.project_id,
             "detail": self.detail},
            sort_keys=True,
        )


@dataclass
class SupervisorConfig:
    """Wiring for a supervisor instance. All cloud/worker/gate seams
    are injectable so tests run on fakes with ZERO live models."""

    state_root: str | Path
    project_id: str = "p1"
    #: Builds the CloudPlanner (production default = tool-free P17
    #: transport with the P14 quota router). Injectable for tests.
    planner_factory: Any = None
    #: Batch worker runner (WorkerRunner protocol; tests use fakes).
    runner: Any = None
    #: P11–P13 checkpoint gate executor (injected; see
    #: :class:`CheckpointExecutor`). Production builds the canonical
    #: g1/g2/g3/g4 executor stack; tests pass a fake.
    checkpoint_executor: Any = None
    base_sha: str = "0" * 40
    workdir: str = "."


class CheckpointExecutor(Protocol):
    """Injected checkpoint gate runtime (P11–P13 authority). The
    supervisor calls this — it never drives the gate engine itself, so
    there is exactly ONE checkpoint authority and the supervisor
    cannot pre-mark gates (it holds no GateEngine)."""

    def run_checkpoint(
        self, plan: "CheckpointPlan", checkpoint_id: str, *, plan_id: str
    ) -> str:
        """Deterministically execute the checkpoint's gates
        (G0 admission already applied; G1/G2/G3 as applicable) until
        the checkpoint is terminal or parked at G4. Returns the
        checkpoint's terminal/park status string
        ("PASSED" | "WAITING_FOR_HUMAN" | "REJECTED" | "REPAIR")."""
        ...

    def g4_status(self, plan: "CheckpointPlan", checkpoint_id: str) -> str:
        """Current G4 gate status (read-only)."""
        ...

    def apply_g4_decision(
        self,
        plan: "CheckpointPlan",
        checkpoint_id: str,
        *,
        decision: str,
        feedback: str,
        actor: str,
    ) -> str:
        """Apply a HUMAN G4 decision through the canonical engine
        (the only G4 path). Returns the new status. ``actor`` must be
        human-* — the engine enforces it."""
        ...


# ---------------------------------------------------------------------------
# Events (append-only observability feed; never authority)
# ---------------------------------------------------------------------------


def _events_path(store: ProjectStore, project_id: str) -> Path:
    return store.project_dir(project_id) / "events.jsonl"


def append_event(store: ProjectStore, project_id: str, event: ProjectEvent) -> None:
    path = _events_path(store, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event.to_json() + "\n")


def load_events(store: ProjectStore, project_id: str) -> list[ProjectEvent]:
    path = _events_path(store, project_id)
    if not path.exists():
        return []
    events: list[ProjectEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            events.append(
                ProjectEvent(
                    kind=ProjectEventKind(data["kind"]),
                    project_id=data["project_id"],
                    detail=data.get("detail", ""),
                )
            )
        except (ValueError, KeyError):
            continue  # observability feed: a torn line never crashes the
            # coordinator; canonical state is the authority.
    return events


# ---------------------------------------------------------------------------
# Checkpoint plan persistence for P16-lowered mutation intent
# ---------------------------------------------------------------------------


def _checkpoint_plan_path(store: ProjectStore, project_id: str, batch_id: str) -> Path:
    return (
        store.authority_dir(project_id)
        / f"{safe_name(batch_id)}.checkpoint-plan.json"
    )


def save_checkpoint_plan(
    store: ProjectStore, project_id: str, batch_id: str, plan: "CheckpointPlan"
) -> Path:
    """Persist a P16-lowered CheckpointPlan at the canonical authority
    location so checkpoint execution (and restart recovery) can find it.
    Atomic; deterministic bytes."""
    return _atomic_write_json(
        _checkpoint_plan_path(store, project_id, batch_id),
        plan.model_dump(mode="json"),
    )


def load_checkpoint_plan(
    store: ProjectStore, project_id: str, batch_id: str
) -> "CheckpointPlan":
    from app.orchestration.checkpoint_planning.schemas import CheckpointPlan

    path = _checkpoint_plan_path(store, project_id, batch_id)
    if not path.exists():
        raise ProjectSupervisorError(
            f"no checkpoint plan persisted for batch {batch_id!r}"
        )
    return CheckpointPlan.model_validate(json.loads(path.read_text(encoding="utf-8")))


class ProjectSupervisorError(RuntimeError):
    """Fail-closed supervisor error (malformed state, missing executor,
    unknown park)."""


class ProjectSupervisor:
    """The single production lifecycle coordinator (P18).

    Construct with :meth:`create_project` (fresh) or
    :meth:`open_project` (restart recovery). All methods read
    persisted canonical state first — the supervisor holds no state of
    its own beyond wiring.
    """

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.store = ProjectStore(config.state_root)
        self._planner: CloudPlanner | None = None

    # -- construction / recovery ------------------------------------------

    def _make_planner(self) -> "CloudPlanner":
        if self.config.planner_factory is not None:
            return self.config.planner_factory()
        from app.orchestration.cloud_batch.planner import CloudPlanner

        return CloudPlanner(
            quota_router=QuotaRouter(
                state_root=self.store.state_root,
                project_id=self.config.project_id,
            )
        )

    def create_project(
        self,
        *,
        initial_prompt: str,
        objective: str,
        base_sha: str | None = None,
        project_id: str | None = None,
    ) -> ProjectContract:
        """NEW: create the project + persist its contract. NO cloud call
        (CLARIFY happens on the first advance)."""
        from datetime import datetime, timezone

        project_id = project_id or self.config.project_id
        contract = ProjectContract(
            project_id=project_id,
            initial_prompt=initial_prompt,
            objective=objective,
            created_at=datetime.now(timezone.utc).isoformat(),
            state=CloudProjectState.NEW,
            base_sha=base_sha or self.config.base_sha,
        )
        self.store.create_project(contract)
        self._emit(ProjectEventKind.PROJECT_CREATED, f"objective={objective[:120]}")
        return contract

    def open_project(self, project_id: str | None = None) -> "ProjectSupervisor":
        """Restart recovery: bind this supervisor to an existing
        project. The next advance_project() re-derives everything from
        persisted state (no duplicated cloud calls / workers / human
        decisions — see each state's handler)."""
        self.config.project_id = project_id or self.config.project_id
        self.store.load_project(self.config.project_id)  # raises if absent
        return self

    # -- the API ------------------------------------------------------------

    def _planner_instance(self) -> "CloudPlanner":
        if self._planner is None:
            self._planner = self._make_planner()
        return self._planner

    def _protocol(self) -> CloudProtocol:
        return CloudProtocol(
            self.store, self.config.project_id, self._planner_instance()
        )

    def state(self) -> CloudProjectState:
        return self.store.load_project(self.config.project_id).state

    def _emit(self, kind: ProjectEventKind, detail: str = "") -> None:
        append_event(
            self.store,
            self.config.project_id,
            ProjectEvent(kind, self.config.project_id, detail),
        )

    def advance_project(self) -> dict:
        """Perform deterministic work only, until one of:
        human input required / workers running / cloud reentry required
        / terminal state. At most ONE cloud call. No wait loops."""
        from app.orchestration.cloud_batch import authority as p16
        from app.orchestration.cloud_batch.dispatcher import BatchDispatcher

        project_id = self.config.project_id
        contract = self.store.load_project(project_id)
        state = contract.state
        protocol = self._protocol()

        if state in (CloudProjectState.COMPLETE, CloudProjectState.FAILED):
            return {"state": state, "action": "terminal"}

        if state is CloudProjectState.NEW:
            # ONE cloud call: the CLARIFY turn. Parks at WAITING_FOR_USER.
            request = protocol.request_clarify()
            self._emit(ProjectEventKind.CLARIFICATION_REQUIRED,
                       f"questions={len(request.questions)}")
            return {"state": self.state(), "action": "clarify_requested",
                    "park": park_view(protocol)}

        if state is CloudProjectState.WAITING_FOR_USER:
            # Parked: human input required. (The answer arrives via
            # submit_human_input; advance does NOT call the cloud.)
            return {"state": state, "action": "waiting_for_user",
                    "park": park_view(protocol)}

        if state is CloudProjectState.NEEDS_HUMAN:
            return {"state": state, "action": "needs_human",
                    "park": park_view(protocol)}

        if state is CloudProjectState.READY_TO_PLAN:
            # ONE cloud call: the BATCH turn. -> BATCH_RUNNING.
            plan = protocol.request_batch()
            self._emit(ProjectEventKind.BATCH_CREATED,
                       f"batch_id={plan.batch_id} delegations={len(plan.delegations)}")
            return {"state": self.state(), "action": "batch_created",
                    "batch_id": plan.batch_id}

        if state is CloudProjectState.BATCH_RUNNING:
            # Deterministic only: dispatch the current batch (P16-guarded).
            # NO cloud while workers execute. dispatch_batch is
            # idempotent across restarts (terminal workers rehydrated,
            # routed markers not re-emitted).
            batch_id = self._current_batch_id(contract)
            result = p16.dispatch_batch(
                store=self.store,
                project_id=project_id,
                protocol=protocol,
                batch_id=batch_id,
                runner=self.config.runner,
                base_sha=contract.base_sha,
                workdir=self.config.workdir,
            )
            reason = result["reason"]
            self._emit_workers(batch_id)
            self._maybe_persist_checkpoint_plan(batch_id)
            new_state = self.state()
            if new_state is CloudProjectState.REENTRY_READY:
                self._emit(ProjectEventKind.REENTRY_READY, f"reason={reason}")
            return {"state": new_state, "action": "batch_executed",
                    "batch_id": batch_id, "reentry_reason": reason,
                    "routed": result["routed"]}

        if state is CloudProjectState.REENTRY_READY:
            # ONE cloud call: the reentry turn (at most one per
            # generation — the protocol enforces the reservation).
            batch_id = self._current_batch_id(contract)
            action = protocol.request_reentry(batch_id)
            if isinstance(action, BatchPlan):
                self._emit(ProjectEventKind.CLOUD_REENTERED, "action=BATCH")
                self._emit(ProjectEventKind.BATCH_CREATED,
                           f"batch_id={action.batch_id}")
                return {"state": self.state(), "action": "reentered",
                        "batch_id": action.batch_id}
            if isinstance(action, FinalResult):
                kind = (ProjectEventKind.PROJECT_COMPLETE
                        if action.success else ProjectEventKind.PROJECT_FAILED)
                self._emit(kind, f"success={action.success}")
                return {"state": self.state(), "action": "final",
                        "success": action.success}
            if isinstance(action, NeedsHuman):
                self._emit(ProjectEventKind.HUMAN_APPROVAL_REQUIRED,
                           "reentry=parked NEEDS_HUMAN")
                return {"state": self.state(), "action": "needs_human",
                        "park": park_view(protocol)}
            # ClarificationRequest from reentry is a protocol violation —
            # the protocol already raised + set FAILED.
            return {"state": self.state(), "action": "reentered"}

        raise ProjectSupervisorError(f"unhandled state {state!r}")

    def _emit_workers(self, batch_id: str) -> None:
        """Emit WORKER_STARTED/WORKER_TERMINAL for the batch's persisted
        worker outcomes (deterministic read; the feed, not authority)."""
        try:
            outcomes = self.store.load_worker_outcomes(self.config.project_id, batch_id)
        except (OSError, ValueError, KeyError):
            return
        for task_id, outcome in sorted(outcomes.items()):
            status = getattr(outcome, "status", None)
            if status in ("running", "dispatched", "started"):
                self._emit(ProjectEventKind.WORKER_STARTED, f"task_id={task_id}")
            else:
                self._emit(ProjectEventKind.WORKER_TERMINAL,
                           f"task_id={task_id} status={status}")

    def _current_batch_id(self, contract: ProjectContract) -> str:
        """The batch the project is currently executing/re-entering.
        Derived from persisted artifacts (the latest batch plan dir)."""
        batches_dir = self.store.batches_dir(self.config.project_id)
        if not batches_dir.exists():
            raise ProjectSupervisorError("BATCH_RUNNING/REENTRY_READY but no batch dirs")
        batch_ids = sorted(
            d.name for d in batches_dir.iterdir() if d.is_dir()
        )
        if not batch_ids:
            raise ProjectSupervisorError("no batch directories found")
        return batch_ids[-1]

    def _maybe_persist_checkpoint_plan(self, batch_id: str) -> None:
        """If P16 lowered this batch's mutation into a checkpoint plan,
        persist it at the canonical authority location (idempotent)."""
        lowered = self.store.load_lowered_intent(self.config.project_id, batch_id)
        if not lowered:
            return
        plan = lowered.get("checkpoint_plan")
        if plan is None:
            return
        path = _checkpoint_plan_path(self.store, self.config.project_id, batch_id)
        if not path.exists():
            from app.orchestration.checkpoint_planning.schemas import CheckpointPlan

            save_checkpoint_plan(
                self.store,
                self.config.project_id,
                batch_id,
                CheckpointPlan.model_validate(plan),
            )
            self._emit(ProjectEventKind.CHECKPOINT_CREATED, f"batch_id={batch_id}")

    def checkpoint_batch(self) -> str | None:
        """The most recent batch that lowered a checkpoint plan (or None).
        Deterministic scan of persisted authority intent."""
        authority_dir = self.store.authority_dir(self.config.project_id)
        if not authority_dir.exists():
            return None
        for path in sorted(authority_dir.glob("*.checkpoint-plan.json")):
            return path.name.replace(".checkpoint-plan.json", "")
        return None

    def run_checkpoint(self, *, batch_id: str | None = None) -> str:
        """Deterministically execute a P16-lowered checkpoint's gates
        (via the injected P11–P13 executor — the supervisor never drives
        the gate engine itself). NO cloud. Returns the checkpoint's
        terminal/park status."""
        batch_id = batch_id or self.checkpoint_batch()
        if batch_id is None:
            raise ProjectSupervisorError("no checkpoint plan to execute")
        if self.config.checkpoint_executor is None:
            raise ProjectSupervisorError(
                "checkpoint execution requires an injected CheckpointExecutor"
            )
        plan = load_checkpoint_plan(self.store, self.config.project_id, batch_id)
        plan_id = f"p18-{batch_id}"
        checkpoint_id = plan.checkpoints[0].checkpoint_id if plan.checkpoints else ""
        try:
            status = self.config.checkpoint_executor.run_checkpoint(
                plan, checkpoint_id, plan_id=plan_id
            )
        except Exception as exc:  # fail closed at the executor boundary
            raise ProjectSupervisorError(
                f"checkpoint execution failed: {exc}"
            ) from exc
        self._emit_gate(batch_id, status)
        return status

    def _emit_gate(self, batch_id: str, status: str) -> None:
        if status == "WAITING_FOR_HUMAN":
            self._emit(ProjectEventKind.HUMAN_APPROVAL_REQUIRED,
                       f"checkpoint={batch_id}")
        else:
            self._emit(ProjectEventKind.GATE_STATE_CHANGED,
                       f"checkpoint={batch_id} status={status}")

    def g4_status(self, *, batch_id: str | None = None) -> str:
        batch_id = batch_id or self.checkpoint_batch()
        if batch_id is None or self.config.checkpoint_executor is None:
            return "NONE"
        plan = load_checkpoint_plan(self.store, self.config.project_id, batch_id)
        checkpoint_id = plan.checkpoints[0].checkpoint_id if plan.checkpoints else ""
        return self.config.checkpoint_executor.g4_status(plan, checkpoint_id)

    def submit_human_input(self, *, text: str, actor: str = "human:cli") -> dict:
        """Answer the parked human surface (CLARIFY / NEEDS_HUMAN) with
        EXPLICIT human provenance. Delegates to the P15 bridge — the
        supervisor adds no authority. Idempotent per park generation."""
        protocol = self._protocol()
        view = park_view(protocol)
        kind = view.get("kind")
        if kind is None:
            raise ProjectSupervisorError(
                "no parked human surface to answer (state is not "
                f"WAITING_FOR_USER/NEEDS_HUMAN: {self.state()})"
            )
        from datetime import datetime, timezone

        response = HumanResponse(
            text=text,
            actor=actor,
            origin="cli",
            submitted_at=datetime.now(timezone.utc).isoformat(),
        )
        result = record_human_response(protocol, response, kind)
        self._emit(ProjectEventKind.HUMAN_RESPONSE_RECEIVED,
                   f"kind={kind} actor={actor}")
        return result

    def submit_g4_decision(
        self,
        *,
        decision: str,
        feedback: str = "",
        actor: str = "human:cli",
        batch_id: str | None = None,
    ) -> str:
        """The ONLY G4 path: a HUMAN decision through the injected
        canonical executor (the engine enforces human-only + monotonic
        revisions). The supervisor cannot pre-mark G4 — it holds no
        GateEngine and has no other route."""
        if self.config.checkpoint_executor is None:
            raise ProjectSupervisorError(
                "G4 decision requires an injected CheckpointExecutor"
            )
        batch_id = batch_id or self.checkpoint_batch()
        if batch_id is None:
            raise ProjectSupervisorError("no checkpoint plan for G4 decision")
        plan = load_checkpoint_plan(self.store, self.config.project_id, batch_id)
        checkpoint_id = plan.checkpoints[0].checkpoint_id if plan.checkpoints else ""
        try:
            status = self.config.checkpoint_executor.apply_g4_decision(
                plan, checkpoint_id, decision=decision,
                feedback=feedback, actor=actor,
            )
        except Exception as exc:  # fail closed at the executor boundary
            raise ProjectSupervisorError(f"G4 decision failed: {exc}") from exc
        self._emit_gate(batch_id, status)
        return status

    def cancel_project(self, *, reason: str = "cancelled") -> None:
        """Operator cancel: deterministic terminal failure (legal from
        any non-terminal state — the protocol enforces that)."""
        self._protocol().fail(reason)
        self._emit(ProjectEventKind.PROJECT_CANCELLED, f"reason={reason}")

    def get_project(self) -> dict:
        """Full supervisor view: canonical state + park + event feed +
        checkpoint status. Read-only; never invokes the cloud."""
        contract = self.store.load_project(self.config.project_id)
        view: dict[str, Any] = {
            "project_id": self.config.project_id,
            "state": contract.state.value,
            "generation": contract.generation,
            "objective": contract.objective,
            "cloud_calls": self.store.cloud_call_count(self.config.project_id),
            "events": [e.to_json() for e in load_events(self.store, self.config.project_id)],
        }
        try:
            view["park"] = park_view(self._protocol())
        except HumanBridgeError:
            view["park"] = None
        view["checkpoint_batch"] = self.checkpoint_batch()
        if self.config.checkpoint_executor is not None:
            view["g4_status"] = self.g4_status()
        return view


__all__ = [
    "CheckpointExecutor",
    "ProjectEvent",
    "ProjectEventKind",
    "ProjectSupervisor",
    "ProjectSupervisorError",
    "SupervisorConfig",
    "append_event",
    "load_events",
    "load_checkpoint_plan",
    "save_checkpoint_plan",
]
