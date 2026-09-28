"""P8 worker A — schema tests (contract items 1-10).

1  checkpoint gate model shape (all four kinds present, iter_gates)
2  GateExpectations required=False needs reason
3  checkpoint_id regex enforcement
4  GateStatus / CheckpointStatus / Complexity enums
5  GateRecord defaults (PENDING, applicability validation, red_exception)
6  PlannerProvenance model-id-as-string (no enum of model ids)
7  CheckpointPlan defaults (DRAFT, revision 1, optional g0/analyses)
8  AnalysisEvidence default source
9  G0Approval / AnalysisRequest shapes
10 CheckpointPlan round-trips through model_dump / model_validate
"""

import unittest

from pydantic import ValidationError

from app.orchestration.checkpoint_planning.schemas import (
    GATE_KINDS,
    AnalysisEvidence,
    AnalysisRef,
    AnalysisRequest,
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    CheckpointStatus,
    Complexity,
    G0Approval,
    GateApplicability,
    GateExpectations,
    GateRecord,
    GateStatus,
    PlannerProvenance,
    PlannerUnavailable,
)


def gate(required=True, reason=None, planned_tests=None, **kw):
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=planned_tests or (["test_x"] if required else []),
        **kw,
    )


def gates():
    return CheckpointGates(
        G1_behavior=gate(),
        G2_ui=gate(required=False, reason="no UI in scope"),
        G3_adversarial=gate(),
        G4_human=gate(),
    )


def checkpoint(checkpoint_id="cp-1", **kw):
    return Checkpoint(
        checkpoint_id=checkpoint_id,
        title="t",
        objective="o",
        gates=gates(),
        **kw,
    )


def plan(checkpoints=None, **kw):
    return CheckpointPlan(
        plan_id="plan-1",
        feature_id="feat-1",
        objective="do the thing",
        planner=PlannerProvenance(provider="gemini"),
        checkpoints=checkpoints if checkpoints is not None else [checkpoint()],
        created_at="2026-09-28T00:00:00+00:00",
        **kw,
    )


class SchemaTests(unittest.TestCase):
    # -- 1 ------------------------------------------------------------------
    def test_checkpoint_gates_have_all_four_kinds_and_iterate_in_order(self):
        g = gates()
        self.assertEqual(
            [kind for kind, _ in g.iter_gates()], list(GATE_KINDS)
        )
        for kind in GATE_KINDS:
            self.assertIsInstance(getattr(g, kind), GateExpectations)

    # -- 2 ------------------------------------------------------------------
    def test_not_required_gate_without_reason_rejected(self):
        with self.assertRaises(ValidationError):
            GateExpectations(required=False)
        with self.assertRaises(ValidationError):
            GateExpectations(required=False, reason="   ")
        ok = GateExpectations(required=False, reason="pure refactor, no UI")
        self.assertFalse(ok.required)

    def test_gate_applicability_mirrors_rule(self):
        self.assertEqual(
            GateExpectations(required=True).applicability().required, True
        )
        with self.assertRaises(ValidationError):
            GateApplicability(required=False)
        self.assertEqual(
            GateApplicability(required=False, reason="none").reason, "none"
        )

    # -- 3 ------------------------------------------------------------------
    def test_checkpoint_id_regex(self):
        checkpoint("cp-2b")  # valid
        checkpoint("a")  # valid
        checkpoint("9lives")  # digit start allowed
        for bad in ("CP-1", "-cp1", "cp_1", "cp 1", "", "cp-1!"):
            with self.subTest(cid=bad), self.assertRaises(ValidationError):
                checkpoint(bad)

    # -- 4 ------------------------------------------------------------------
    def test_enums_exact(self):
        self.assertEqual(
            [s.value for s in GateStatus],
            ["PENDING", "ACTIVE", "PASSED", "REPAIR", "REJECTED"],
        )
        self.assertEqual(
            [s.value for s in CheckpointStatus],
            ["PLANNED", "ACTIVE", "ACCEPTED", "REPAIR", "BLOCKED"],
        )
        self.assertEqual(
            [c.value for c in Complexity],
            ["TRIVIAL", "SMALL", "FEATURE", "LARGE_FEATURE"],
        )

    # -- 5 ------------------------------------------------------------------
    def test_gate_record_defaults_and_applicability_guard(self):
        rec = GateRecord(gate="G1_behavior", preseeded=gate())
        self.assertEqual(rec.status, GateStatus.PENDING)
        self.assertEqual(rec.applicability, "REQUIRED")
        self.assertEqual(rec.evidence_refs, [])
        self.assertIsNone(rec.red_exception)
        self.assertIsNone(rec.closed_at)
        self.assertEqual(rec.repair_lineage, [])
        with self.assertRaises(ValidationError):
            GateRecord(gate="G1_behavior", preseeded=gate(), applicability="MAYBE")

    # -- 6 ------------------------------------------------------------------
    def test_provenance_provider_model_are_free_strings(self):
        prov = PlannerProvenance(
            provider="gemini", model="any-future-model-id", role="cloud"
        )
        self.assertEqual(prov.model, "any-future-model-id")
        self.assertEqual(prov.gate_template_version, "1")
        self.assertFalse(prov.fallback_used)
        # No hard-coded model-id enum anywhere in the schema domain:
        self.assertNotIn("Literal", repr(PlannerProvenance.model_fields["provider"].annotation))
        self.assertNotIn("Literal", repr(PlannerProvenance.model_fields["model"].annotation))
        self.assertNotIn("Literal", repr(PlannerProvenance.model_fields["role"].annotation))

    # -- 7 ------------------------------------------------------------------
    def test_plan_defaults(self):
        p = plan()
        self.assertEqual(p.status, "DRAFT")
        self.assertEqual(p.revision, 1)
        self.assertIsNone(p.g0)
        self.assertEqual(p.analyses, [])
        self.assertEqual(p.design_context_refs, [])
        self.assertEqual(p.learning_refs, [])
        self.assertEqual(p.checkpoints[0].status, CheckpointStatus.PLANNED)
        with self.assertRaises(ValidationError):
            plan(status="NOT_A_STATUS")

    # -- 8 ------------------------------------------------------------------
    def test_analysis_evidence_default_source(self):
        ev = AnalysisEvidence(summary="s")
        self.assertEqual(ev.source, "agy-planning-analyst")
        self.assertEqual(ev.relevant_symbols, [])
        self.assertEqual(ev.open_questions, [])

    # -- 9 ------------------------------------------------------------------
    def test_g0_and_analysis_request_shapes(self):
        g0 = G0Approval(decision="APPROVE", approved_at="now", approver="l", revision=2)
        self.assertEqual(g0.revision, 2)
        req = AnalysisRequest(objective="o", questions=["q"], focus=["f"])
        self.assertEqual(req.questions, ["q"])
        ref = AnalysisRef(analysis_id="a1", objective="o")
        self.assertEqual(ref.analyst_session, None)

    # -- 10 -----------------------------------------------------------------
    def test_plan_json_round_trip(self):
        p = plan(checkpoints=[checkpoint("cp-1"), checkpoint("cp-2", dependencies=["cp-1"])])
        payload = p.model_dump(mode="json")
        restored = CheckpointPlan.model_validate(payload)
        self.assertEqual(restored, p)
        self.assertIsInstance(restored.checkpoints[0].gates.G1_behavior, GateExpectations)

    def test_planner_unavailable_is_exception(self):
        self.assertTrue(issubclass(PlannerUnavailable, Exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
