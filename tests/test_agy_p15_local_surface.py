"""P15-A — local human surface: discovery + guarded application over the
canonical authorities. Deterministic, zero model calls, zero network,
tmp_path state roots only; P14 documents are hand-written plain JSON (no
cloud_batch import in the product code or in these tests).

Six focused proofs:

1. discovery: ``list_parked_human_actions`` finds EXACTLY the parked
   surfaces (G4 WAITING_FOR_HUMAN + P14 NEEDS_HUMAN/WAITING_FOR_USER),
   skips malformed P14 files, fails closed on malformed G4 canonical
   state, and renders through ``render_pending``;
2. APPROVE through the surface -> canonical PASSED + durable ledger entry
   (actor human:tui) + plan doc reflected;
3. NEEDS_CHANGES -> REPAIR + planner-input request with VERBATIM feedback;
4. non-human actors raise HumanAuthorityError and persist NOTHING;
5. ``collect_g4_decision`` with a scripted reader (verbatim, ValueError);
6. ``admit_via_surface`` against the real P13 admission service
   (accepted / stale revision / non-human approver).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.continuation_admission import (
    ContinuationAdmissionError,
    admission_record_path,
)
from app.orchestration.checkpoint_planning.checkpoint_viewer import (
    CheckpointViewerError,
    list_parked_human_actions,
)
from app.orchestration.checkpoint_planning.schemas import HumanDecision
from app.human_surface import (
    HumanAuthorityError,
    admit_via_surface,
    apply_g4_decision,
    collect_g4_decision,
    render_pending,
    require_human_actor,
)

PLAN_ID = "plan-a"
CP_ID = "cp-1"
TITLE = "Park A"
OBJECTIVE = "prove the surface"

_LEGAL = [
    {"decision": "APPROVE", "effect": "G4 -> PASSED"},
    {"decision": "NEEDS_CHANGES", "effect": "G4 -> REPAIR"},
    {"decision": "CANCEL", "effect": "G4 -> REJECTED"},
]


# ---------------------------------------------------------------------------
# fixtures (hand-written JSON — the stable canonical/P14 schemas)
# ---------------------------------------------------------------------------


def _plan_doc_dict(plan_id: str, g4_status: str) -> dict:
    """A schema-valid canonical plan document whose G4_human gate record
    carries ``g4_status`` (G1-G3 already PASSED)."""
    gates = {
        "G1_behavior": {"required": True, "planned_tests": ["test_x"]},
        "G2_ui": {"required": False, "reason": "no UI in scope"},
        "G3_adversarial": {"required": True, "planned_tests": ["test_y"]},
        "G4_human": {"required": True, "planned_tests": []},
    }

    def rec(gate: str, status: str, applicability: str = "REQUIRED") -> dict:
        return {
            "gate": gate,
            "status": status,
            "applicability": applicability,
            "preseeded": gates[gate],
        }

    return {
        "plan_id": plan_id,
        "feature_id": "p15",
        "objective": OBJECTIVE,
        "planner": {"role": "cloud", "provider": "gemini"},
        "checkpoints": [
            {
                "checkpoint_id": CP_ID,
                "title": TITLE,
                "objective": OBJECTIVE,
                "gates": gates,
                "gate_records": {
                    "G1_behavior": rec("G1_behavior", "PASSED"),
                    "G2_ui": rec(
                        "G2_ui", "PASSED", applicability="NOT_APPLICABLE"
                    ),
                    "G3_adversarial": rec("G3_adversarial", "PASSED"),
                    "G4_human": rec("G4_human", g4_status),
                },
            }
        ],
        "status": "SUBMITTED",
        "created_at": "2026-09-29T00:00:00+00:00",
        "revision": 1,
    }


def _write_plan(state_root: Path, plan_id: str, g4_status: str) -> Path:
    path = state_root / "plans" / plan_id / "checkpoint_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_plan_doc_dict(plan_id, g4_status), indent=2),
        encoding="utf-8",
    )
    return path


def _write_project(
    state_root: Path, project_id: str, state: str, artifacts: dict[str, dict]
) -> None:
    """P14 project record + cloud artifacts as PLAIN JSON (stable schema
    fields only — mirrors the ProjectStore layout without importing it)."""
    project_dir = state_root / "projects" / project_id
    (project_dir / "cloud").mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(
        json.dumps(
            {
                "project_id": project_id,
                "initial_prompt": "build the thing",
                "objective": "the thing",
                "state": state,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    for name, artifact in artifacts.items():
        (project_dir / "cloud" / name).write_text(
            json.dumps(artifact, indent=2), encoding="utf-8"
        )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` (path -> bytes): proves zero persistence."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# 1. discovery across BOTH surfaces
# ---------------------------------------------------------------------------


def test_list_parked_human_actions_discovers_every_parked_state(tmp_path):
    # G4 surface: plan-a parked, plan-b fully passed.
    _write_plan(tmp_path, PLAN_ID, "WAITING_FOR_HUMAN")
    _write_plan(tmp_path, "plan-b", "PASSED")
    # decisions ledger -> next revision must be ledger max + 1 = 3.
    ledger = tmp_path / "runs" / PLAN_ID / "human" / CP_ID / "decisions"
    ledger.mkdir(parents=True)
    for rev in (1, 2):
        (ledger / f"{rev}.json").write_text("{}", encoding="utf-8")

    # P14 surface (hand-written JSON — NO cloud_batch import).
    _write_project(
        tmp_path,
        "proj-help",
        "NEEDS_HUMAN",
        {
            "0001-clarify.json": {
                "mode": "CLARIFY",
                "questions": ["stale question"],
                "reasoning": "old",
            },
            "0002-needs_human.json": {
                "mode": "NEEDS_HUMAN",
                "question": "Pick a migration target",
                "context": "two viable paths",
                "options": ["postgres", "sqlite"],
            },
        },
    )
    _write_project(
        tmp_path,
        "proj-ask",
        "WAITING_FOR_USER",
        {
            "0001-clarify.json": {
                "mode": "CLARIFY",
                "questions": ["Which theme?", "Deploy where?"],
                "reasoning": "intent unclear",
            },
        },
    )
    _write_project(tmp_path, "proj-running", "BATCH_RUNNING", {})
    # malformed P14 project: skipped without crashing, discovery continues.
    bad_project = tmp_path / "projects" / "proj-junk"
    (bad_project).mkdir(parents=True)
    (bad_project / "project.json").write_text("{not json", encoding="utf-8")

    entries = list_parked_human_actions(tmp_path)

    assert [e["kind"] for e in entries] == [
        "CLOUD_CLARIFY",
        "CLOUD_NEEDS_HUMAN",
        "G4_WAITING_FOR_HUMAN",
    ]  # deterministic: kind, then id
    clarify, needs_human, g4 = entries
    assert set(clarify) == {
        "kind", "project_id", "questions", "reasoning", "options",
    }
    assert clarify["project_id"] == "proj-ask"
    assert clarify["questions"] == ["Which theme?", "Deploy where?"]
    assert clarify["reasoning"] == "intent unclear" and clarify["options"] == []
    assert "question" not in clarify
    assert set(needs_human) == {
        "kind", "project_id", "question", "context", "options",
    }
    assert needs_human["project_id"] == "proj-help"
    assert needs_human["question"] == "Pick a migration target"
    assert needs_human["context"] == "two viable paths"
    assert needs_human["options"] == ["postgres", "sqlite"]
    assert set(g4) == {
        "kind", "plan_id", "checkpoint_id", "question", "legal_decisions",
        "revision",
    }
    assert (g4["plan_id"], g4["checkpoint_id"], g4["revision"]) == (
        PLAN_ID, CP_ID, 3,
    )
    # question built EXACTLY like G4Executor.wait() (single source: wait)
    assert g4["question"] == (
        f"Approve checkpoint {CP_ID} ({TITLE}) — objective: {OBJECTIVE}?"
    )
    assert [d["decision"] for d in g4["legal_decisions"]] == [
        "APPROVE", "NEEDS_CHANGES", "CANCEL",
    ]

    rendered = render_pending(entries)
    for needle in (PLAN_ID, CP_ID, "Pick a migration target", "Which theme?"):
        assert needle in rendered
    assert "plan-b" not in rendered and "proj-running" not in rendered

    # clean roots -> nothing parked (G4 fail-closed contract untouched).
    (tmp_path / "clean").mkdir()
    assert list_parked_human_actions(tmp_path / "clean") == []
    assert render_pending([]).strip() != ""
    # a missing state root fails closed (same contract as the viewer's
    # find_plan_document): a typo'd --state root must not render "all clear"
    with pytest.raises(CheckpointViewerError):
        list_parked_human_actions(tmp_path / "no-such-root")

    # malformed G4 canonical document -> hard fail closed.
    broken = tmp_path / "plans" / "plan-broken" / "checkpoint_plan.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{oops", encoding="utf-8")
    with pytest.raises(CheckpointViewerError):
        list_parked_human_actions(tmp_path)


# ---------------------------------------------------------------------------
# 2. G4 APPROVE through the surface (canonical executor + reflection)
# ---------------------------------------------------------------------------


def test_apply_g4_decision_approve_closes_gate_durably(tmp_path):
    _write_plan(tmp_path, PLAN_ID, "WAITING_FOR_HUMAN")

    result = apply_g4_decision(
        tmp_path, PLAN_ID, CP_ID, HumanDecision.APPROVE, "",
        actor="human:tui",
    )

    assert result["status"] == "PASSED" and result["revision"] == 1
    decision = json.loads(
        (tmp_path / "runs" / PLAN_ID / "human" / CP_ID / "decisions" / "1.json")
        .read_text(encoding="utf-8")
    )
    assert decision["actor"] == "human:tui"
    assert decision["provenance"] == "tui"
    assert decision["decision"] == "APPROVE"
    plan_doc = json.loads(
        (tmp_path / "plans" / PLAN_ID / "checkpoint_plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        plan_doc["checkpoints"][0]["gate_records"]["G4_human"]["status"]
        == "PASSED"
    )  # durable state reflected into the canonical plan doc


# ---------------------------------------------------------------------------
# 3. G4 NEEDS_CHANGES: REPAIR + VERBATIM planner input
# ---------------------------------------------------------------------------


def test_apply_g4_decision_needs_changes_persists_verbatim_feedback(tmp_path):
    _write_plan(tmp_path, PLAN_ID, "WAITING_FOR_HUMAN")

    result = apply_g4_decision(
        tmp_path, PLAN_ID, CP_ID, HumanDecision.NEEDS_CHANGES,
        "make it smaller", actor="human:tui",
    )

    assert result["status"] == "REPAIR"
    requests = sorted(
        (tmp_path / "runs" / PLAN_ID / "human" / CP_ID / "planner_inputs")
        .glob("*.json")
    )
    assert [p.name for p in requests] == ["hpir-cp-1-1.json"]
    doc = json.loads(requests[0].read_text(encoding="utf-8"))
    assert doc["feedback"] == "make it smaller"  # VERBATIM, never rewritten
    assert doc["status"] == "PENDING"
    plan_doc = json.loads(
        (tmp_path / "plans" / PLAN_ID / "checkpoint_plan.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        plan_doc["checkpoints"][0]["gate_records"]["G4_human"]["status"]
        == "REPAIR"
    )


# ---------------------------------------------------------------------------
# 4. provenance guard: non-human actors rejected, NOTHING persisted
# ---------------------------------------------------------------------------


def test_non_human_actor_rejected_and_persists_nothing(tmp_path):
    _write_plan(tmp_path, PLAN_ID, "WAITING_FOR_HUMAN")
    from tests.test_agy_p13_admission import (  # P13 fixture pattern
        _build_state,
    )

    _build_state(tmp_path)  # continuation + CONSUMED source request
    before = _snapshot(tmp_path)

    with pytest.raises(HumanAuthorityError):
        apply_g4_decision(
            tmp_path, PLAN_ID, CP_ID, HumanDecision.APPROVE, "",
            actor="model:opus",
        )
    with pytest.raises(HumanAuthorityError):
        admit_via_surface(tmp_path, "plan-test", "hpir-test-1", 2,
                          approver="agent:agy-dispatch")

    assert _snapshot(tmp_path) == before  # zero bytes changed
    assert not (
        tmp_path / "plans" / "plan-test" / "checkpoint_plan.json"
    ).exists()

    # the guard itself: human:* surfaces pass, everything else fails closed
    assert require_human_actor("human:cli") == "human:cli"
    assert require_human_actor("human:tui") == "human:tui"
    assert require_human_actor("human:remote") == "human:remote"
    for impostor in ("model", "assistant:opus", "agent:agy-dispatch", "", None):
        with pytest.raises(HumanAuthorityError):
            require_human_actor(impostor)


# ---------------------------------------------------------------------------
# 5. collect_g4_decision: scripted reader, verbatim feedback, ValueError
# ---------------------------------------------------------------------------


class _ScriptedReader:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)

    def __call__(self, prompt: str) -> str:  # noqa: ARG002 - prompt unused
        return next(self._lines)


def test_collect_g4_decision_reads_verbatim_and_rejects_garbage():
    decision, feedback = collect_g4_decision(
        _ScriptedReader(["approve"]), "Q?", _LEGAL
    )
    assert decision is HumanDecision.APPROVE and feedback == ""

    decision, feedback = collect_g4_decision(
        _ScriptedReader(["NEEDS-CHANGES", "too big", ""]), "Q?", _LEGAL
    )
    assert decision is HumanDecision.NEEDS_CHANGES
    assert feedback == "too big"  # VERBATIM

    decision, feedback = collect_g4_decision(
        _ScriptedReader(["needs-changes", "first ", "second", "", "ignored"]),
        "Q?", _LEGAL,
    )
    assert decision is HumanDecision.NEEDS_CHANGES
    assert feedback == "first \nsecond"  # multi-line, trailing space verbatim

    with pytest.raises(ValueError):
        collect_g4_decision(_ScriptedReader(["nonsense"]), "Q?", _LEGAL)
    with pytest.raises(ValueError):
        collect_g4_decision(_ScriptedReader(["needs-changes", ""]), "Q?", _LEGAL)


# ---------------------------------------------------------------------------
# 6. admit_via_surface over the real P13 admission service
# ---------------------------------------------------------------------------


def test_admit_via_surface_canonical_accept_stale_and_impostor(tmp_path):
    from tests.test_agy_p13_admission import (
        PLAN_ID as P13_PLAN,
        REQUEST_ID as P13_REQUEST,
        _build_state,
    )

    _build_state(tmp_path)
    result = admit_via_surface(tmp_path, P13_PLAN, P13_REQUEST, 2,
                               approver="human:tui")
    assert result["accepted"] is True
    assert result["approver"] == "human:tui"
    record_path = admission_record_path(tmp_path, P13_PLAN, P13_REQUEST)
    assert json.loads(record_path.read_text(encoding="utf-8"))["status"] == (
        "ADMITTED"
    )
    assert (tmp_path / "plans" / P13_PLAN / "checkpoint_plan.json").is_file()

    # stale revision -> canonical ContinuationAdmissionError propagates.
    stale_root = tmp_path / "stale"
    _build_state(stale_root)
    with pytest.raises(ContinuationAdmissionError):
        admit_via_surface(stale_root, P13_PLAN, P13_REQUEST, 1,
                          approver="human:tui")
    assert not (stale_root / "plans" / P13_PLAN / "checkpoint_plan.json").exists()

    # non-human approver -> HumanAuthorityError BEFORE any persistence.
    impostor_root = tmp_path / "impostor"
    _build_state(impostor_root)
    before = _snapshot(impostor_root)
    with pytest.raises(HumanAuthorityError):
        admit_via_surface(impostor_root, P13_PLAN, P13_REQUEST, 2,
                          approver="assistant:opus")
    assert _snapshot(impostor_root) == before
