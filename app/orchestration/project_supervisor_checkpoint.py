"""P18 — Production checkpoint executor (the P11–P13 gate runtime as the
supervisor's injected :class:`CheckpointExecutor`).

This adapter is the ONLY place in the supervisor stack that drives the
canonical checkpoint authorities. It does not re-implement any gate
logic — it sequences the canonical executors and persists their durable
gate records back into the canonical plan document:

* ``submit_checkpoint_plan`` — G0 admission + FRESH gate records
  (planner claims are discarded, P11 rule). G0 is HUMAN authority:
  :meth:`CanonicalCheckpointExecutor.admit` is the only route, and it
  requires a ``human:*`` actor. ``run_checkpoint`` NEVER auto-admits —
  an unadmitted plan fails closed.
* ``G1Executor.execute_g1`` — behavior/TDD, when G1 is REQUIRED;
* ``G2Executor.execute_checkpoint`` — UI review, when REQUIRED;
* ``G3Executor.execute_checkpoint`` — adversarial review, when REQUIRED;
* ``G4Executor.open/wait/apply_human_decision`` — the human gate
  (WAITING_FOR_HUMAN is the only exit to PASSED).

``run_checkpoint`` is DETERMINISTIC: no cloud, no model wait loops, no
polling. It runs until a terminal gate state or until G4 parks at
WAITING_FOR_HUMAN (returns the park status; the human decision arrives
later via :meth:`apply_g4_decision`, after which a re-invocation of
``run_checkpoint`` confirms terminality — G4 is never re-opened).

Restart recovery: gate state is re-derived from the persisted plan
document (the canonical source) — a gate already terminal on disk is
never re-executed, an admitted plan is never re-submitted, and a parked
G4 is re-reported as parked, not re-asked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.orchestration.agy_runstate import _atomic_write_json
from app.orchestration.checkpoint_planning.g1_executor import (
    G1Executor,
)
from app.orchestration.checkpoint_planning.g2_executor import (
    G2Executor,
)
from app.orchestration.checkpoint_planning.g3_executor import (
    G3Executor,
    build_live_g3_executor,
)
from app.orchestration.checkpoint_planning.g4_executor import (
    G4Executor,
    build_live_g4_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateStatus,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointPlan,
    G0Approval,
    GateRecord,
    HumanDecision,
    HumanGateRecord,
)
from app.orchestration.checkpoint_planning.submission import (
    seed_gate_records,
    submit_checkpoint_plan,
)

G4_GATE_ID = "G4_human"
G1_GATE_ID = "G1_behavior"
G2_GATE_ID = "G2_ui"
G3_GATE_ID = "G3_adversarial"


class CheckpointExecutorError(RuntimeError):
    """Fail-closed checkpoint executor error."""


class _NotHumanActor(CheckpointExecutorError):
    """A non-human actor attempted a human-authority action (G0 admit,
    G4 decision). Raised before any canonical state is touched."""


def _is_human_actor(actor: Any) -> bool:
    return isinstance(actor, str) and actor.startswith("human:")


class CanonicalCheckpointExecutor:
    """The P11–P13 checkpoint gate runtime, as a :class:`CheckpointExecutor`.

    Inject this into :class:`ProjectSupervisor` for production. The gate
    executors are built lazily so a plan that needs no workers (e.g. a
    G4-only approval) never requires a test harness or UI path.

    Protocol implemented: ``admit`` (HUMAN G0), ``run_checkpoint``,
    ``g4_status``, ``apply_g4_decision``.
    """

    def __init__(
        self,
        *,
        repo: str | Path,
        state_root: str | Path,
        test_path: str = "tests",
        test_command: list[str] | None = None,
        full_suite_command: list[str] | None = None,
        ui_path: str | None = None,
        design_ref: str = "",
        g4_executor: G4Executor | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.state_root = Path(state_root)
        self.test_path = test_path
        self.test_command = list(test_command or ["pytest", "-q"])
        self.full_suite_command = list(full_suite_command or self.test_command)
        self.ui_path = ui_path
        self.design_ref = design_ref
        self._g4 = g4_executor or build_live_g4_executor(self.state_root)
        self._g1: G1Executor | None = None
        self._g2: G2Executor | None = None
        self._g3: G3Executor | None = None

    # -- lazy executor construction ----------------------------------------

    def _g1_executor(self) -> G1Executor:
        if self._g1 is None:
            self._g1 = G1Executor(
                self.repo,
                test_path=self.test_path,
                test_command=self.test_command,
                full_suite_command=self.full_suite_command,
            )
        return self._g1

    def _g2_executor(self) -> G2Executor:
        if self._g2 is None:
            if not self.ui_path:
                raise CheckpointExecutorError(
                    "G2 is REQUIRED but no ui_path was configured"
                )
            self._g2 = G2Executor(
                self.repo, ui_path=self.ui_path, design_ref=self.design_ref
            )
        return self._g2

    def _g3_executor(self) -> G3Executor:
        if self._g3 is None:
            self._g3 = build_live_g3_executor(
                repo=self.repo,
                test_path=self.test_path,
                test_command=self.test_command,
                full_suite_command=self.full_suite_command,
            )
        return self._g3

    # -- durable plan document (the canonical checkpoint state) -------------

    def _plan_doc_path(self, plan_id: str) -> Path:
        return self.state_root / "plans" / plan_id / "checkpoint_plan.json"

    def _load_plan_doc(self, plan_id: str) -> dict | None:
        import json as _json

        path = self._plan_doc_path(plan_id)
        if not path.exists():
            return None
        try:
            doc = _json.loads(path.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
        except (OSError, ValueError):
            return None

    def _save_plan_doc(self, plan_id: str, doc: dict) -> None:
        _atomic_write_json(self._plan_doc_path(plan_id), doc)

    def _checkpoint_in_doc(self, doc: dict, checkpoint_id: str) -> dict:
        for cp in doc.get("checkpoints", []):
            if cp.get("checkpoint_id") == checkpoint_id:
                return cp
        raise CheckpointExecutorError(
            f"checkpoint {checkpoint_id!r} not found in plan doc"
        )

    def _load_live_checkpoint(
        self, plan: CheckpointPlan, checkpoint_id: str
    ) -> Checkpoint:
        """The checkpoint with DURABLE gate records: read back from the
        persisted plan document when present (the restart-recovery
        source of truth), else the in-memory plan's records."""
        target = None
        for cp in plan.checkpoints:
            if cp.checkpoint_id == checkpoint_id:
                target = cp
                break
        if target is None:
            raise CheckpointExecutorError(
                f"checkpoint {checkpoint_id!r} not in plan {plan.plan_id!r}"
            )
        doc = self._load_plan_doc(plan.plan_id)
        if doc is None:
            return target
        live = self._checkpoint_in_doc(doc, checkpoint_id)
        return Checkpoint.model_validate(live)

    def _is_submitted(self, plan: CheckpointPlan) -> bool:
        """True when the canonical plan document exists and is marked
        SUBMITTED (a human admitted G0 and submission persisted)."""
        doc = self._load_plan_doc(plan.plan_id)
        return bool(doc) and doc.get("status") == "SUBMITTED"

    def _sync_checkpoint_to_doc(
        self, doc: dict, checkpoint_id: str, records: dict[str, GateRecord]
    ) -> None:
        """Reflect the gate records' state into the checkpoint's durable
        STATUS. Note: ``Checkpoint.status`` is the ``CheckpointStatus``
        enum (PLANNED/ACTIVE/ACCEPTED/REPAIR/BLOCKED) — a GATE status
        like WAITING_FOR_HUMAN is NOT a checkpoint status, so map it."""
        cp = self._checkpoint_in_doc(doc, checkpoint_id)
        cp["gate_records"] = {
            gate_id: rec.model_dump(mode="json")
            for gate_id, rec in records.items()
        }
        statuses = {rec.status.value for rec in records.values()}
        if GateStatus.REJECTED.value in statuses:
            cp["status"] = "BLOCKED"
        elif GateStatus.REPAIR.value in statuses:
            cp["status"] = "REPAIR"
        elif all(s == GateStatus.PASSED.value for s in statuses):
            cp["status"] = "ACCEPTED"
        else:
            # Includes WAITING_FOR_HUMAN (parked for the human) and any
            # in-flight gate — the checkpoint is actively being worked.
            cp["status"] = "ACTIVE"

    def _save_records(
        self,
        plan: CheckpointPlan,
        checkpoint_id: str,
        records: dict[str, GateRecord],
    ) -> None:
        doc = self._load_plan_doc(plan.plan_id)
        if doc is None:
            return  # not yet admitted: nothing durable to sync
        self._sync_checkpoint_to_doc(doc, checkpoint_id, records)
        self._save_plan_doc(plan.plan_id, doc)

    # -- the CheckpointExecutor protocol ------------------------------------

    def admit(self, plan: CheckpointPlan, *, actor: str) -> dict:
        """HUMAN G0 admission — the ONLY route to a submitted plan.

        Fails closed on a non-human actor (before any canonical state is
        touched) and on any submission error. Idempotent: re-admitting
        an already-submitted plan is a no-op.
        """
        if not _is_human_actor(actor):
            raise _NotHumanActor(
                f"G0 admission requires a human actor, got {actor!r}"
            )
        if self._is_submitted(plan):
            return {"plan_id": plan.plan_id, "idempotent": True,
                    "status": "SUBMITTED"}
        approval = G0Approval(
            decision="APPROVE",
            revision=plan.revision,
            approver=actor,
            approved_at=datetime.now(timezone.utc).isoformat(),
        )
        result = submit_checkpoint_plan(
            plan, state_root=self.state_root, g0_approval=approval
        )
        if not result.accepted:
            raise CheckpointExecutorError(
                "canonical G0 admission failed: " + "; ".join(result.errors[:5])
            )
        return {
            "plan_id": plan.plan_id,
            "idempotent": False,
            "status": "SUBMITTED",
            "next_gate": result.next_gate,
        }

    def run_checkpoint(
        self,
        plan: CheckpointPlan,
        checkpoint_id: str,
        *,
        plan_id: str,
    ) -> str:
        """Deterministically execute the checkpoint's gates until a
        terminal state or the G4 human park. NO cloud, NO wait loops.

        Requires a prior HUMAN admission (:meth:`admit`) — an unadmitted
        plan fails closed; this method never self-approves.

        Returns the checkpoint status: "PASSED" | "WAITING_FOR_HUMAN" |
        "REJECTED" | "REPAIR".
        """
        if not self._is_submitted(plan):
            raise CheckpointExecutorError(
                "checkpoint plan is not admitted (G0 human approval "
                f"required before execution): {plan.plan_id!r}"
            )

        cp = self._load_live_checkpoint(plan, checkpoint_id)
        gates = cp.gates

        def recs() -> dict[str, GateRecord]:
            # The executors mutate the IN-MEMORY checkpoint (
            # ``cp.gate_records = dict(engine.records)``), never disk —
            # so read the live object, and persist it after each step.
            return cp.gate_records

        def persist() -> None:
            self._save_records(plan, checkpoint_id, cp.gate_records)

        def status_of(gate_id: str) -> GateStatus | None:
            r = cp.gate_records.get(gate_id)
            return r.status if r is not None else None

        # G1 (behavior/TDD) — REQUIRED only; a terminal result stops.
        if self._required(gates.G1_behavior):
            if self._terminal(recs().get(G1_GATE_ID)):
                return recs()[G1_GATE_ID].status.value  # restart-safe
            self._g1_executor().execute_g1(cp, plan_id=plan.plan_id)
            persist()
            if self._terminal(recs().get(G1_GATE_ID)):
                return recs()[G1_GATE_ID].status.value

        # G2 (UI review) — REQUIRED only.
        if self._required(gates.G2_ui):
            if self._terminal(recs().get(G2_GATE_ID)):
                return recs()[G2_GATE_ID].status.value
            self._g2_executor().execute_checkpoint(plan.plan_id, cp)
            persist()
            if self._terminal(recs().get(G2_GATE_ID)):
                return recs()[G2_GATE_ID].status.value

        # G3 (adversarial review) — REQUIRED only.
        if self._required(gates.G3_adversarial):
            if self._terminal(recs().get(G3_GATE_ID)):
                return recs()[G3_GATE_ID].status.value
            self._g3_executor().execute_checkpoint(plan.plan_id, cp)
            persist()
            if self._terminal(recs().get(G3_GATE_ID)):
                return recs()[G3_GATE_ID].status.value

        # G4 (human) — REQUIRED only. Park it; never auto-decide.
        if self._required(gates.G4_human):
            g4 = recs().get(G4_GATE_ID)
            if g4 is not None and g4.status == GateStatus.WAITING_FOR_HUMAN:
                return "WAITING_FOR_HUMAN"  # already parked (restart-safe)
            if g4 is not None and g4.status in (
                GateStatus.PASSED, GateStatus.REJECTED, GateStatus.REPAIR,
            ):
                return g4.status.value  # human already decided
            self._g4.open(cp)
            self._g4.wait(cp, plan_id=plan.plan_id)
            persist()
            g4 = recs().get(G4_GATE_ID)
            return g4.status.value if g4 else "WAITING_FOR_HUMAN"

        return "PASSED"

    def g4_status(self, plan: CheckpointPlan, checkpoint_id: str) -> str:
        cp = self._load_live_checkpoint(plan, checkpoint_id)
        g4 = cp.gate_records.get(G4_GATE_ID)
        return g4.status.value if g4 is not None else "NONE"

    def apply_g4_decision(
        self,
        plan: CheckpointPlan,
        checkpoint_id: str,
        *,
        decision: str,
        feedback: str,
        actor: str,
    ) -> str:
        """The ONLY G4 path: a HUMAN decision through the canonical
        G4Executor + engine. The engine enforces human-only provenance
        and the monotonic revision rule; this adapter persists the
        resulting durable state and returns the new gate status. A
        non-human actor raises before any gate moves."""
        if not _is_human_actor(actor):
            raise _NotHumanActor(
                f"G4 decision requires a human actor, got {actor!r}"
            )
        cp = self._load_live_checkpoint(plan, checkpoint_id)
        engine = GateEngine(cp)
        g4 = engine.record(G4_GATE_ID)
        accepted = [
            ref for ref in g4.evidence_refs
            if ref.startswith("human_decision:rev")
        ]
        last_rev = 0
        for ref in accepted:
            try:
                last_rev = max(last_rev, int(ref.split(":")[2]))
            except (IndexError, ValueError):
                continue
        record = HumanGateRecord(
            plan_id=plan.plan_id,
            checkpoint_id=checkpoint_id,
            gate_id=G4_GATE_ID,
            decision=HumanDecision(decision),
            feedback=feedback,
            submitted_at=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            provenance="supervisor",
            revision=last_rev + 1,
        )
        try:
            self._g4.apply_human_decision(cp, record)
        except GateTransitionError as exc:
            raise CheckpointExecutorError(f"G4 decision rejected: {exc}") from exc
        # apply_human_decision mutates the IN-MEMORY checkpoint (via
        # _sync); persist that object — reloading from disk would read
        # the stale pre-decision records.
        self._save_records(plan, checkpoint_id, cp.gate_records)
        g4 = GateEngine(cp).record(G4_GATE_ID)
        return g4.status.value

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _required(expectations: Any) -> bool:
        return bool(getattr(expectations, "required", False))

    @staticmethod
    def _terminal(record: GateRecord | None) -> bool:
        return (
            record is not None
            and record.status
            in (GateStatus.PASSED, GateStatus.REJECTED, GateStatus.REPAIR)
        )


def build_canonical_checkpoint_executor(
    *,
    repo: str | Path,
    state_root: str | Path,
    test_path: str = "tests",
    test_command: list[str] | None = None,
    ui_path: str | None = None,
    design_ref: str = "",
) -> CanonicalCheckpointExecutor:
    """Convenience constructor for the production wiring."""
    return CanonicalCheckpointExecutor(
        repo=repo,
        state_root=state_root,
        test_path=test_path,
        test_command=test_command,
        ui_path=ui_path,
        design_ref=design_ref,
    )


__all__ = [
    "CanonicalCheckpointExecutor",
    "CheckpointExecutorError",
    "build_canonical_checkpoint_executor",
]
