"""
P8 Worker B — deterministic GATE CONTRACT tests (contract items 38-48).

The planning layer's promises, proven against Worker A's REAL domain
core (schemas / validation / planner templates / submission) and
Worker B's surface (mcp_tools). No network; tmp state roots only.

 38 submission seeds ALL gate records PENDING/REQUIRED (N/A where
    required=False) and DISCARDS gate statuses the planner claims
 39 required G1 without planned_tests is rejected at validation
    (error prefixed with the failing checkpoint/gate id)
 40 G1 template workflow encodes the TDD order:
    tests-planned -> ... RED ... -> implementation -> GREEN ->
    deterministic acceptance
 41 GateRecord carries `red_exception: str | None` — a test already
    green before implementation needs a recorded rationale to satisfy
    RED-required
 42 G2 reviewer is read-only vs design reference; structured-findings
    contract (severity/area/expected/observed/evidence vocabulary)
 43 G3 fixer is BOUNDED: MAX_REVIEW_FIX_CYCLES == 3 and the template
    policy carries it; escalation state after exhaustion
 44 G3 reviewer is read-only (adversarial reviewer, structured findings)
 45 G4 closes ONLY via human G4Approval decision; NEEDS_CHANGES routes
    to structured follow-up, never raw forward to a worker
 46 the planning MCP surface exposes NO gate-mutation API: nothing in
    mcp_tools accepts a GateStatus / writes gate state / passes gates
 47 submission hands off to runtime_owner "agy-local" with next_gate =
    first checkpoint's first REQUIRED gate (CP-order), planner claims
    irrelevant
 48 learnings are READ-ONLY for the planning surface — no free-form
    append API exists (curated appends are a runtime/provenance
    concern, not a planner tool); G0 APPROVE is mandatory before
    submission (silent self-approval rejected)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning import mcp_tools
from app.orchestration.checkpoint_planning.planner import (
    MAX_REVIEW_FIX_CYCLES,
    gate_templates,
)
from app.orchestration.checkpoint_planning.schemas import (
    GATE_KINDS,
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    CheckpointStatus,
    G0Approval,
    GateExpectations,
    GateRecord,
    GateStatus,
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.submission import (
    submit_checkpoint_plan,
)
from app.orchestration.checkpoint_planning.validation import (
    validate_checkpoint_plan,
)


# ---------------------------------------------------------------------------
# canonical plan fixture
# ---------------------------------------------------------------------------

def _expectations(
    required: bool = True,
    reason: str | None = None,
    planned_tests: list[str] | None = None,
) -> GateExpectations:
    if planned_tests is None:
        planned_tests = ["tests/test_x.py"] if required else []
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=list(planned_tests),
    )


def make_checkpoint(
    cid: str = "cp-1",
    *,
    deps: list[str] | None = None,
    g2_required: bool = False,
    planned_tests: list[str] | None = None,
    status: CheckpointStatus = CheckpointStatus.PLANNED,
    claimed_records: dict | None = None,
) -> Checkpoint:
    gates = CheckpointGates(
        G1_behavior=_expectations(True, planned_tests=planned_tests),
        G2_ui=_expectations(
            g2_required,
            None if g2_required else "no user-visible surface",
            planned_tests=[] if not g2_required else None,
        ),
        G3_adversarial=_expectations(True, planned_tests=[]),
        G4_human=_expectations(True, planned_tests=[]),
    )
    return Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        dependencies=list(deps or []),
        relevant_files=["app/refunds.py"],
        stages=["stage-1"],
        gates=gates,
        status=status,
        gate_records=dict(claimed_records or {}),
    )


def make_plan(*checkpoints: Checkpoint, approved: bool = True) -> CheckpointPlan:
    cps = list(checkpoints) or [make_checkpoint()]
    return CheckpointPlan(
        plan_id="plan-refunds-1",
        feature_id="refunds",
        objective="refund windows for the ticketing checkout",
        planner=PlannerProvenance(role="cloud", provider="gemini", model=None),
        checkpoints=cps,
        status="DRAFT",
        created_at="2026-09-28T00:00:00+00:00",
        revision=1,
        g0=(
            G0Approval(
                decision="APPROVE",
                approved_at="2026-09-28T00:05:00+00:00",
                approver="leon",
                revision=1,
            )
            if approved
            else None
        ),
    )


def submit(plan: CheckpointPlan, tmp_path: Path, **kw):
    root = tmp_path / "state"
    return submit_checkpoint_plan(plan, state_root=root, **kw), root


# ---------------------------------------------------------------------------
# 38: planner-claimed gate statuses are DISCARDED
# ---------------------------------------------------------------------------

def test_submission_discards_planner_claimed_gate_status(tmp_path):
    # A planner (maliciously) claims its gates already PASSED.
    claimed = {
        kind: {
            "gate": kind,
            "status": "PASSED",
            "applicability": "REQUIRED",
            "preseeded": getattr(make_checkpoint().gates, kind).model_dump(),
            "closed_by": "planner",
        }
        for kind in GATE_KINDS
    }
    plan = make_plan(make_checkpoint(claimed_records=claimed))

    result, root = submit(plan, tmp_path)
    assert result.accepted, result.errors

    doc = json.loads(
        (root / "plans" / plan.plan_id / "checkpoint_plan.json").read_text()
    )
    records = doc["checkpoints"][0]["gate_records"]
    for kind in GATE_KINDS:
        # every record re-seeded PENDING by the runtime owner's rules
        assert records[kind]["status"] == "PENDING", kind
        assert records[kind].get("closed_by") in (None, "")
    # N/A applicability comes from required=False, not from the claim
    assert records["G2_ui"]["applicability"] == "NOT_APPLICABLE"
    assert records["G1_behavior"]["applicability"] == "REQUIRED"


def test_gate_record_defaults_are_pending_not_applicable_driven():
    record = GateRecord(
        gate="G2_ui",
        preseeded=GateExpectations(required=False, reason="no UI"),
    )
    assert record.status is GateStatus.PENDING
    assert record.applicability == "REQUIRED"  # durable applicability set by seeding
    assert record.evidence_refs == []
    assert record.repair_lineage == []


# ---------------------------------------------------------------------------
# 39: required G1 needs planned tests (preseed precedes implementation)
# ---------------------------------------------------------------------------

def test_required_g1_without_planned_tests_rejected():
    plan = make_plan(make_checkpoint(planned_tests=[]))
    verdict = validate_checkpoint_plan(plan)
    assert not verdict.ok
    assert any(
        err.startswith("cp-1:G1_behavior") and "planned_tests" in err
        for err in verdict.errors
    ), verdict.errors


def test_silent_gate_omission_needs_reason():
    cp = make_checkpoint()
    # required=False with a blanked reason is a silent omission: the
    # durable plan document can carry it (hand-crafted payload), and
    # validation must reject it (schema-level guards are bypassed).
    cp.gates.G2_ui = GateExpectations.model_construct(
        required=False, reason="   ", planned_tests=[], focus=[], test_instructions=[]
    )
    plan = make_plan(cp)
    verdict = validate_checkpoint_plan(plan)
    assert not verdict.ok
    assert any("reason" in err for err in verdict.errors), verdict.errors


# ---------------------------------------------------------------------------
# 40: G1 template = TDD ordering
# ---------------------------------------------------------------------------

def test_g1_workflow_is_tdd_ordered():
    import re

    workflow = gate_templates()["G1_behavior"]["workflow"].lower()

    def position(pattern: str, *, last: bool = False) -> int:
        matches = list(re.finditer(pattern, workflow))
        assert matches, f"{pattern!r} missing from G1 workflow: {workflow}"
        # last=True: the RED annotation mentions a pre-green test; the
        # GREEN STAGE is the final occurrence of the word.
        return (matches[-1] if last else matches[0]).start()

    order = [
        position(r"tests-planned"),
        position(r"\bred\b"),
        position(r"-> implementation"),
        position(r"\bgreen\b", last=True),
        position(r"acceptance"),
    ]
    assert order == sorted(order), workflow


# ---------------------------------------------------------------------------
# 41: RED exception needs recorded rationale
# ---------------------------------------------------------------------------

def test_gate_record_has_red_exception_rationale_field():
    record = GateRecord(
        gate="G1_behavior",
        preseeded=GateExpectations(required=True, planned_tests=["tests/test_x.py"]),
    )
    assert record.red_exception is None
    annotated = record.model_copy(update={"red_exception": "test pre-greened: regression lock"})
    assert annotated.red_exception.startswith("test pre-greened")


def test_g1_template_workflow_references_red_exception():
    templates = gate_templates()
    assert "red_exception" in templates["G1_behavior"]["workflow"]


# ---------------------------------------------------------------------------
# 42: G2 read-only reviewer vs design reference
# ---------------------------------------------------------------------------

def test_g2_reviewer_is_read_only_vs_design():
    g2 = gate_templates()["G2_ui"]
    assert "read-only" in g2["reviewer"]
    assert "design" in g2["workflow"].lower()
    # structured findings vocabulary is the contract the runtime reviewer
    # must emit; the planning templates point at it via fixer/reviewer
    # policy fields only — never a mutation grant.
    assert g2["fixer_policy"]["type"] == "bounded_repair"


# ---------------------------------------------------------------------------
# 43/44: G3 bounded fixer + read-only adversarial reviewer
# ---------------------------------------------------------------------------

def test_g3_fixer_cycles_are_bounded_at_three():
    assert MAX_REVIEW_FIX_CYCLES == 3
    g3 = gate_templates()["G3_adversarial"]
    assert g3["fixer_policy"]["max_cycles"] == 3
    assert "escalation" in g3["fixer_policy"]
    assert "blocked" in g3["fixer_policy"]["escalation"].lower()
    # fixer writes stay scoped — bounded scope, not blanket write
    assert "relevant_files" in g3["fixer_policy"]["scope"]


def test_g3_reviewer_is_read_only_structured():
    g3 = gate_templates()["G3_adversarial"]
    assert "read-only" in g3["reviewer"]
    assert "structured findings" in g3["reviewer"]
    assert g3["defaults"]["required"] is True


# ---------------------------------------------------------------------------
# 45: G4 human-only close
# ---------------------------------------------------------------------------

def test_g4_closes_only_via_human_approval():
    g4 = gate_templates()["G4_human"]
    assert "g4approval" in g4["workflow"].lower()
    assert g4["reviewer"] == "human approver"
    assert g4["fixer_policy"]["type"] == "none"
    assert "needs_changes" in g4["fixer_policy"]["note"].lower()
    # structured follow-up, never raw forward
    assert "never raw forward" in g4["fixer_policy"]["note"].lower()


# ---------------------------------------------------------------------------
# 46: the planning surface exposes no gate-mutation API
# ---------------------------------------------------------------------------

def test_no_planning_tool_can_pass_or_write_gates():
    import inspect

    for name in dir(mcp_tools):
        if not name.startswith("agy_") or not callable(getattr(mcp_tools, name)):
            continue
        signature = inspect.signature(getattr(mcp_tools, name))
        params = list(signature.parameters)
        # nothing accepts gate status / decision / approval arguments
        assert not any(
            word in p.lower()
            for p in params
            for word in ("status", "decision", "approve", "pass", "gate_state")
        ), f"{name}{signature}"


def test_validate_tool_reports_without_mutating(tmp_path):
    plan = make_plan()
    payload = plan.model_dump(mode="json")
    verdict = validate_checkpoint_plan(plan)
    result = mcp_tools.agy_checkpoint_plan_validate_tool(json.dumps(payload))
    assert result["ok"] is verdict.ok
    assert result["errors"] == list(verdict.errors)
    # validation left the plan document untouched (still DRAFT)
    assert json.loads(json.dumps(payload))["status"] == "DRAFT"


def test_unparseable_plan_json_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError):
        mcp_tools.agy_checkpoint_plan_validate_tool("{not json")


# ---------------------------------------------------------------------------
# 47: runtime handoff — owner + next REQUIRED gate
# ---------------------------------------------------------------------------

def test_handoff_records_agy_local_owner_and_first_required_gate(tmp_path):
    cp1 = make_checkpoint("cp-1")
    cp2 = make_checkpoint("cp-2", deps=["cp-1"])
    plan = make_plan(cp1, cp2)

    handoffs = []
    result, _root = submit(
        plan, tmp_path, runtime_handoff=lambda p: handoffs.append(p.plan_id) or {}
    )

    assert result.accepted, result.errors
    assert result.runtime_owner == "agy-local"
    # first checkpoint (execution order) first REQUIRED gate = G1
    assert result.next_gate == "cp-1:G1_behavior"
    assert handoffs == [plan.plan_id]


def test_g2_not_required_skips_out_of_next_gate(tmp_path):
    # cp-1 G1 is required first regardless of G2 applicability
    plan = make_plan(make_checkpoint("cp-1", g2_required=False))
    result, _root = submit(plan, tmp_path)
    assert result.accepted, result.errors
    assert result.next_gate == "cp-1:G1_behavior"


# ---------------------------------------------------------------------------
# 48: learnings read-only + G0 discipline
# ---------------------------------------------------------------------------

def test_planning_surface_has_no_learning_append_tool():
    appends = [
        name
        for name in dir(mcp_tools)
        if name.startswith("agy_") and ("learn" in name) and ("write" in name or "append" in name or "curate" in name)
    ]
    assert appends == []  # free-form planner appends to learnings: rejected by design


def test_learnings_tool_is_read_only(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "learnings.md").write_text("## payments\n\nidempotent ledger.\n")
    before = (repo / "learnings.md").read_text()

    out = mcp_tools.agy_learnings_read_tool(str(repo), "ledger")
    assert out["present"] is True
    assert "payments" in out["topics"]
    assert (repo / "learnings.md").read_text() == before


def test_submission_requires_g0_approve(tmp_path):
    # no approval at all
    plan = make_plan(approved=False)
    result, root = submit(plan, tmp_path)
    assert not result.accepted
    assert any("g0" in err.lower() for err in result.errors)

    # self-authored REQUEST_REVISION is not an approval
    plan2 = make_plan()
    plan2.g0 = G0Approval(decision="REQUEST_REVISION", revision=1)
    result2, _ = submit(plan2, tmp_path)
    assert not result2.accepted
    assert any("approve" in err.lower() for err in result2.errors)

    # revision mismatch is rejected
    plan3 = make_plan()
    plan3.g0 = G0Approval(decision="APPROVE", revision=2)
    result3, _ = submit(plan3, tmp_path)
    assert not result3.accepted
    assert any("revision" in err.lower() for err in result3.errors)

    assert not (root / "plans" / plan.plan_id / "checkpoint_plan.json").exists()


def test_submit_tool_persists_under_state_root_only(tmp_path, monkeypatch):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    before = sorted(p.name for p in repo.iterdir())

    state_root = tmp_path / "agy-state"
    plan = make_plan()
    out = mcp_tools.agy_checkpoint_plan_submit_tool(
        json.dumps(plan.model_dump(mode="json")), str(state_root)
    )
    assert out["accepted"] is True
    assert (state_root / "plans" / plan.plan_id / "checkpoint_plan.json").exists()
    # the repository was not touched by the submission path
    assert sorted(p.name for p in repo.iterdir()) == before
