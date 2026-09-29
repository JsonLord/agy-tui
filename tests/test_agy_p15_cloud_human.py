"""P15-B — Cloud/human bridge: explicit human provenance at the parks.

ZERO live model calls, ZERO network, ZERO sleeps: fake cloud planner,
tmp_path state roots, real protocol/store engines. Covers the milestone
invariants:

 1. a CLARIFY park becomes visible to the human surface via park_view,
    built from the persisted artifact (read back from disk);
 2. a real HumanResponse resumes the clarify park exactly once and the
    durable answer file records the actor + origin provenance;
 3. a duplicate/replayed answer for the same park generation raises
    DuplicateHumanResponse idempotently (state unchanged, no 2nd file);
 4. a NEEDS_HUMAN park (driven through the real protocol + a persisted
    BatchOutcome read from disk) resumes exactly once and rejects
    duplicates the same way;
 5. a model-origin payload can NEVER masquerade as a HumanResponse —
    not through the schema, not through save_human_response, not
    through record_human_response (nothing persisted, state unchanged);
 6. G4 remains human-only and lives in its own runtime: the bridge
    cannot touch it and imports nothing from the checkpoint planning
    package (proof (b): only submit_human_decision from
    WAITING_FOR_HUMAN closes G4 + module-import inspection).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.orchestration.checkpoint_planning.g4_executor import G4_GATE_ID
from app.orchestration.checkpoint_planning.gate_engine import (
    GateEngine,
    GateTransitionError,
)
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointStatus,
    GateExpectations,
    GateStatus,
    HumanDecision,
    HumanGateRecord,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records
from app.orchestration.cloud_batch import (
    CloudProtocol,
    DuplicateHumanResponse,
    HumanBridgeError,
    HumanResponse,
    NotHumanOrigin,
    ProjectStore,
    park_view,
    record_human_response,
    schemas,
)
from app.orchestration.cloud_batch.state import ProjectStoreError

NOW = "2026-09-29T12:00:00+00:00"
PID = "p15-proj"
CLARIFY_QUESTIONS = [
    "What result do you want, and what is out of scope?",
    "Should the deployment target stay pinned?",
]


# ---------------------------------------------------------------------------
# Fakes (patterns reused from tests/test_agy_p14_cloud_batch.py, not copied)
# ---------------------------------------------------------------------------


class FakePlanner:
    """Fake cloud planner port: counts every invocation, returns canned
    actions. The bridge must never add a call to ``calls``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.clarify_action = schemas.ClarificationRequest(
            questions=list(CLARIFY_QUESTIONS)
        )
        self.batch_action: schemas.BatchPlan | None = None
        self.reentry_actions: list[schemas.CloudAction] = []

    def clarify(self, prompt: str) -> schemas.ClarificationRequest:
        self.calls.append("clarify")
        return self.clarify_action

    def batch(self, prompt: str) -> schemas.BatchPlan:
        self.calls.append("batch")
        assert self.batch_action is not None
        return self.batch_action

    def reentry(self, prompt: str) -> schemas.CloudAction:
        self.calls.append("reentry")
        assert self.reentry_actions, "reentry_actions must be pre-set"
        action, self.reentry_actions = self.reentry_actions[0], self.reentry_actions[1:]
        return action


def two_task_plan() -> schemas.BatchPlan:
    return schemas.BatchPlan(
        batch_id="batch-001",
        objective="build the thing",
        delegations=[
            schemas.DelegationSpec(
                id="a", role="implementer", objective="implement core",
                owned_files=["core.py"],
            ),
        ],
    )


def make_project(tmp_path: Path, planner: FakePlanner) -> CloudProtocol:
    store = ProjectStore(tmp_path)
    store.create_project(
        schemas.ProjectContract(
            project_id=PID,
            initial_prompt="Build the thing, please.",
            objective="build the thing",
            base_sha="b" * 40,
        )
    )
    return CloudProtocol(store, PID, planner)


def human(text: str, actor: str = "human:leon", origin: str = "cli") -> HumanResponse:
    return HumanResponse(text=text, actor=actor, origin=origin, submitted_at=NOW)


def drive_to_needs_human(tmp_path: Path) -> tuple[ProjectStore, CloudProtocol, FakePlanner]:
    """NEW -> CLARIFY -> answer -> BATCH -> (BatchOutcome persisted to
    disk, required by request_reentry) -> deterministic reentry gate ->
    reentry turn whose fake action is NeedsHuman -> NEEDS_HUMAN park."""
    planner = FakePlanner()
    planner.batch_action = two_task_plan()
    planner.reentry_actions = [
        schemas.NeedsHuman(
            question="Which payment provider should we keep?",
            context="two candidates survived the batch",
            options=["A", "B"],
        )
    ]
    protocol = make_project(tmp_path, planner)
    protocol.request_clarify()
    protocol.record_user_answer("In scope: core only.")
    protocol.request_batch()
    protocol.store.save_batch_outcome(
        PID,
        "batch-001",
        schemas.BatchOutcome(
            batch_id="batch-001",
            objective="build the thing",
            workers=[
                schemas.WorkerOutcome(
                    delegation_id="a", role="implementer", status="VERIFIED"
                )
            ],
            summary="all required delegations complete",
        ),
    )
    protocol.mark_reentry_ready("batch-001", "ALL_REQUIRED_DELEGATIONS_COMPLETE")
    protocol.request_reentry("batch-001")
    return protocol.store, protocol, planner


def answer_files(store: ProjectStore) -> list[str]:
    adir = store.answers_dir(PID)
    return sorted(p.name for p in adir.iterdir()) if adir.is_dir() else []


# ---------------------------------------------------------------------------
# 1 — CLARIFY park visibility (from disk)
# ---------------------------------------------------------------------------


def test_park_view_clarify_shows_questions_from_persisted_artifact(tmp_path):
    planner = FakePlanner()
    protocol = make_project(tmp_path, planner)
    protocol.request_clarify()
    assert protocol.state() is schemas.CloudProjectState.WAITING_FOR_USER

    # The artifact is on disk; the view must come from THAT, not memory.
    artifact_path = protocol.store.cloud_dir(PID) / "0001-clarify.json"
    on_disk = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert on_disk["questions"] == CLARIFY_QUESTIONS

    # Fresh protocol instance (restart-shape): view is persisted-state only.
    restarted = CloudProtocol(ProjectStore(tmp_path), PID, FakePlanner())
    view = park_view(restarted)
    assert view["project_id"] == PID
    assert view["state"] == "WAITING_FOR_USER"
    assert view["kind"] == "clarify"
    assert view["questions"] == on_disk["questions"] == CLARIFY_QUESTIONS
    assert set(view) == {
        "project_id", "state", "kind", "questions", "question", "context",
        "options",
    }
    # park_view is read-only: the new protocol's planner was never called.
    assert restarted.planner.calls == []


# ---------------------------------------------------------------------------
# 2 — real answer resumes once + durable provenance
# ---------------------------------------------------------------------------


def test_clarify_human_response_resumes_once_and_persists_provenance(tmp_path):
    planner = FakePlanner()
    protocol = make_project(tmp_path, planner)
    protocol.request_clarify()
    calls_before = len(planner.calls)

    receipt = record_human_response(
        protocol,
        human("Do the small thing", actor="human:remote", origin="remote"),
        "clarify",
    )
    assert receipt["resumed_to"] == "READY_TO_PLAN"
    assert receipt["actor"] == "human:remote"
    assert protocol.state() is schemas.CloudProjectState.READY_TO_PLAN
    # Stored VERBATIM on the contract as data, never interpreted.
    contract = protocol.store.load_project(PID)
    assert contract.clarification_answer == "Do the small thing"
    # Durable provenance file + a fresh store (restart) still sees it.
    assert answer_files(protocol.store) == ["0001-clarify.json"]
    fresh = ProjectStore(tmp_path)
    persisted = fresh.load_human_responses(PID, "clarify")
    assert len(persisted) == 1
    assert persisted[0]["actor"] == "human:remote"
    assert persisted[0]["origin"] == "remote"
    assert persisted[0]["text"] == "Do the small thing"
    assert persisted[0]["park_generation"] == 1
    # park closes after the resume.
    assert park_view(CloudProtocol(fresh, PID, FakePlanner()))["kind"] is None
    # The bridge performed ZERO cloud calls.
    assert len(planner.calls) == calls_before
    assert Path(receipt["response_path"]).name == "0001-clarify.json"


# ---------------------------------------------------------------------------
# 3 — duplicate answer rejected idempotently
# ---------------------------------------------------------------------------


def test_duplicate_clarify_answer_is_idempotent_rejection(tmp_path):
    planner = FakePlanner()
    protocol = make_project(tmp_path, planner)
    protocol.request_clarify()
    record_human_response(protocol, human("first answer"), "clarify")
    state_after_first = protocol.state()
    files_after_first = answer_files(protocol.store)
    calls_before = len(planner.calls)
    cloud_before = protocol.store.cloud_call_count(PID)

    with pytest.raises(DuplicateHumanResponse):
        record_human_response(protocol, human("replayed answer"), "clarify")

    assert protocol.state() is state_after_first
    assert answer_files(protocol.store) == files_after_first
    assert len(protocol.store.load_human_responses(PID, "clarify")) == 1
    assert len(planner.calls) == calls_before
    assert protocol.store.cloud_call_count(PID) == cloud_before


# ---------------------------------------------------------------------------
# 4 — NEEDS_HUMAN park: resume once, duplicates rejected
# ---------------------------------------------------------------------------


def test_needs_human_park_resumes_once_and_rejects_duplicate(tmp_path):
    store, protocol, planner = drive_to_needs_human(tmp_path)
    assert protocol.state() is schemas.CloudProjectState.NEEDS_HUMAN
    calls_at_park = len(planner.calls)
    artifact = json.loads(
        (store.cloud_dir(PID) / "0003-needs_human.json").read_text(encoding="utf-8")
    )
    view = park_view(protocol)
    assert view["kind"] == "needs_human"
    assert view["question"] == artifact["question"]
    assert view["context"] == artifact["context"]
    assert view["options"] == artifact["options"]

    receipt = record_human_response(
        protocol, human("Keep provider A", origin="tui"), "needs_human"
    )
    assert receipt["resumed_to"] == "READY_TO_PLAN"
    assert protocol.state() is schemas.CloudProjectState.READY_TO_PLAN
    assert answer_files(store) == ["0001-needs_human.json"]
    persisted = store.load_human_responses(PID, "needs_human")[0]
    assert persisted["actor"] == "human:leon"
    assert persisted["origin"] == "tui"
    # Bound to the NEEDS_HUMAN artifact generation (cloud seq 3).
    assert persisted["park_generation"] == 3

    with pytest.raises(DuplicateHumanResponse):
        record_human_response(protocol, human("replay"), "needs_human")
    assert protocol.state() is schemas.CloudProjectState.READY_TO_PLAN
    assert answer_files(store) == ["0001-needs_human.json"]
    # The entire park -> resume -> duplicate path made ZERO cloud calls.
    assert len(planner.calls) == calls_at_park


# ---------------------------------------------------------------------------
# 5 — model origin can never masquerade
# ---------------------------------------------------------------------------


def test_model_origin_can_never_masquerade_as_human(tmp_path):
    # Schema level: every non-human actor shape fails, fail closed.
    for bad_actor in ("model:opus", "assistant:opus", "human:", "human",
                      "Human:leon", "human-leon"):
        with pytest.raises(ValidationError):
            HumanResponse(text="x", actor=bad_actor, submitted_at=NOW)
    with pytest.raises(ValidationError):  # blank text
        HumanResponse(text="   ", actor="human:leon", submitted_at=NOW)
    with pytest.raises(ValidationError):  # blank / non-ISO submitted_at
        HumanResponse(text="x", actor="human:leon", submitted_at="  ")
    with pytest.raises(ValidationError):
        HumanResponse(text="x", actor="human:leon", submitted_at="yesterday")
    # origin cannot launder a model actor through a human surface name.
    with pytest.raises(ValidationError):
        HumanResponse(text="x", actor="model:opus", origin="remote",
                      submitted_at=NOW)

    # Bridge level: dict payloads (any origin) never reach state changes.
    planner = FakePlanner()
    protocol = make_project(tmp_path, planner)
    protocol.request_clarify()
    state_before = protocol.state()
    cloud_before = protocol.store.cloud_call_count(PID)
    calls_before = len(planner.calls)

    with pytest.raises(NotHumanOrigin):
        record_human_response(
            protocol,
            {"text": "ship it", "actor": "assistant", "origin": "remote",
             "submitted_at": NOW},
            "clarify",
        )
    # Even a constructed-but-invalid instance (bypassed __init__) is
    # caught by the revalidation before anything is touched.
    smuggled = HumanResponse.model_construct(
        text="x", actor="model:opus", origin="cli", submitted_at=NOW
    )
    with pytest.raises(NotHumanOrigin):
        record_human_response(protocol, smuggled, "clarify", )

    assert protocol.state() is state_before
    assert answer_files(protocol.store) == []
    assert len(planner.calls) == calls_before
    assert protocol.store.cloud_call_count(PID) == cloud_before

    # Store level: hand-rolled JSON cannot be saved as provenance.
    with pytest.raises(ProjectStoreError):
        protocol.store.save_human_response(
            PID,
            {"text": "x", "actor": "model:opus", "origin": "cli",
             "submitted_at": NOW},
            "clarify",
        )
    with pytest.raises(ProjectStoreError):
        protocol.store.save_human_response(
            PID,
            {"text": "x", "actor": "human", "origin": "tui",
             "submitted_at": NOW},
            "clarify",
        )
    assert answer_files(protocol.store) == []


# ---------------------------------------------------------------------------
# 6 — G4 stays human-only and out of the bridge's reach (proof (b))
# ---------------------------------------------------------------------------


def _parked_g4_checkpoint() -> Checkpoint:
    """Minimal G4 parked WAITING_FOR_HUMAN (P13 fixture pattern: real
    GateEngine transitions; ACTIVE -> WAITING_FOR_HUMAN is the legal
    executor park step on G4_human)."""
    cp = Checkpoint(
        checkpoint_id="cp-p15",
        title="checkpoint cp-p15",
        objective="refund window behaviour",
        gates=CheckpointGates(
            G1_behavior=GateExpectations(
                required=True, planned_tests=["tests/test_x.py"]
            ),
            G2_ui=GateExpectations(required=False, reason="no UI surface"),
            G3_adversarial=GateExpectations(required=True),
            G4_human=GateExpectations(required=True),
        ),
        status=CheckpointStatus.PLANNED,
    )
    cp.gate_records = seed_gate_records(cp)
    engine = GateEngine(cp)
    engine.advance(G4_GATE_ID, GateStatus.ACTIVE, actor="runtime")
    engine.advance(G4_GATE_ID, GateStatus.WAITING_FOR_HUMAN, actor="executor")
    cp.gate_records = dict(engine.records)
    return cp


def test_g4_remains_human_only_and_bridge_cannot_reach_it(tmp_path):
    # (i) Once parked, NO machine transition may resolve G4 — every
    # target is refused and the machine-facing view offers no targets.
    cp = _parked_g4_checkpoint()
    engine = GateEngine(cp)
    assert engine.record(G4_GATE_ID).status == GateStatus.WAITING_FOR_HUMAN
    assert engine.legal_target_states(G4_GATE_ID) == set()
    for target in (GateStatus.PASSED, GateStatus.REJECTED, GateStatus.REPAIR,
                   GateStatus.ACTIVE):
        with pytest.raises(GateTransitionError):
            engine.advance(G4_GATE_ID, target, actor="model:rogue")
    # submit_human_decision is only valid WHILE parked:
    fresh_cp = Checkpoint.model_validate(cp.model_dump(mode="json"))
    fresh_cp.gate_records = seed_gate_records(fresh_cp)  # back to PENDING
    unparked = GateEngine(fresh_cp)
    with pytest.raises(GateTransitionError):
        unparked.submit_human_decision(
            G4_GATE_ID,
            HumanGateRecord(
                plan_id="plan-p15", checkpoint_id="cp-p15",
                gate_id=G4_GATE_ID, decision=HumanDecision.APPROVE,
                submitted_at=NOW, actor="human:leon", revision=1,
            ),
        )
    # ...and the engine's ONLY G4-closing path is submit from the park
    # (canonical engine has no actor-prefix check — that is exactly why
    # this proof (b) is used; provenance is enforced at the P15 schema).
    engine.submit_human_decision(
        G4_GATE_ID,
        HumanGateRecord(
            plan_id="plan-p15", checkpoint_id="cp-p15",
            gate_id=G4_GATE_ID, decision=HumanDecision.APPROVE,
            submitted_at=NOW, actor="human:leon", revision=1,
        ),
    )
    assert engine.record(G4_GATE_ID).status == GateStatus.PASSED
    assert engine.record(G4_GATE_ID).closed_by == "human:leon"

    # (ii) NO path from the cloud bridge to the gate runtime.
    import app.orchestration.cloud_batch.human_bridge as human_bridge

    source = Path(human_bridge.__file__).read_text(encoding="utf-8")
    assert "checkpoint_planning" not in source
    assert "gate_engine" not in source
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m.startswith(
        ("app.orchestration.checkpoint_planning",))], imported
    assert not hasattr(human_bridge, "GateEngine")
    assert not hasattr(human_bridge, "HumanGateRecord")
    # Runtime type gate: a gate engineer object has no path through the
    # bridge — both entry points refuse anything but a CloudProtocol.
    for call, args in (
        (park_view, (engine,)),
        (record_human_response, (engine, human("x"), "clarify")),
    ):
        with pytest.raises(HumanBridgeError):
            call(*args)
    # park_view is cloud-project scoped: its entire surface is the fixed
    # project receipt (shape asserted in test 1) — no gate api exists.
    assert not any(
        n for n in dir(human_bridge) if "Gate" in n or "gate" in n
    )
