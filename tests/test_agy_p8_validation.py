"""P8 worker A — validation tests.

Covers validate_checkpoint_plan's eight ordered checks (each error
prefixed with the failing checkpoint/gate id), deterministic
topological_order with tie-breaking, PlanValidationError on cycles /
self-deps / unknown deps, and checkpoint_execution_order.
"""

import unittest

from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointPlan,
    GateExpectations,
    PlannerProvenance,
)
from app.orchestration.checkpoint_planning.validation import (
    PlanValidationError,
    checkpoint_execution_order,
    topological_order,
    validate_checkpoint_plan,
)


def gate(required=True, reason=None, planned_tests=None):
    return GateExpectations(
        required=required,
        reason=reason,
        planned_tests=planned_tests if planned_tests is not None else (
            ["test_a"] if required else []
        ),
    )


def all_gates(**overrides):
    base = {
        "G1_behavior": gate(),
        "G2_ui": gate(required=False, reason="no UI"),
        "G3_adversarial": gate(),
        "G4_human": gate(),
    }
    base.update(overrides)
    return CheckpointGates(**base)


def cp(cid, deps=(), gates=None, **kw):
    return Checkpoint(
        checkpoint_id=cid,
        title=f"t-{cid}",
        objective="o",
        dependencies=list(deps),
        gates=gates if gates is not None else all_gates(),
        **kw,
    )


def mk_plan(checkpoints, provider="gemini", role="cloud", fallback=False, revision=1):
    return CheckpointPlan(
        plan_id="plan-1",
        feature_id="feat-1",
        objective="obj",
        planner=PlannerProvenance(provider=provider, role=role, fallback_used=fallback),
        checkpoints=list(checkpoints),
        created_at="2026-09-28T00:00:00+00:00",
        revision=revision,
    )


class TopologicalOrderTests(unittest.TestCase):
    def test_linear_chain(self):
        self.assertEqual(
            topological_order(["c", "a", "b"], {"b": ["a"], "c": ["b"]}),
            ["a", "b", "c"],
        )

    def test_ties_broken_by_id_sort(self):
        # x and y both ready first; deterministic => sorted order.
        self.assertEqual(
            topological_order(["z", "y", "x", "m"], {"m": ["z", "y", "x"]}),
            ["x", "y", "z", "m"],
        )

    def test_cycle_raises_and_reports_members(self):
        with self.assertRaises(PlanValidationError) as ctx:
            topological_order(["a", "b"], {"a": ["b"], "b": ["a"]})
        self.assertIn("cycle", str(ctx.exception))
        self.assertIn("a", str(ctx.exception))
        self.assertIn("b", str(ctx.exception))

    def test_self_dependency_raises(self):
        with self.assertRaises(PlanValidationError):
            topological_order(["a"], {"a": ["a"]})

    def test_unknown_dependency_raises(self):
        with self.assertRaises(PlanValidationError) as ctx:
            topological_order(["a"], {"a": ["ghost"]})
        self.assertIn("ghost", str(ctx.exception))

    def test_checkpoint_execution_order_stable_repeat(self):
        p = mk_plan([cp("cp-2", deps=["cp-1"]), cp("cp-1"), cp("cp-3", deps=["cp-1"])])
        first = checkpoint_execution_order(p)
        self.assertEqual(first, ["cp-1", "cp-2", "cp-3"])
        self.assertEqual(first, checkpoint_execution_order(p))


class ValidationCheckTests(unittest.TestCase):
    def test_valid_plan_passes(self):
        result = validate_checkpoint_plan(mk_plan([cp("cp-1")]))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.errors, [])

    def test_check1_duplicate_ids(self):
        result = validate_checkpoint_plan(mk_plan([cp("cp-1"), cp("cp-1")]))
        self.assertFalse(result.ok)
        self.assertTrue(any(
            e.startswith("cp-1:") and "duplicate" in e for e in result.errors
        ), result.errors)

    def test_check2_unknown_dependency_prefixed(self):
        result = validate_checkpoint_plan(mk_plan([cp("cp-1", deps=["ghost"])]))
        self.assertFalse(result.ok)
        self.assertIn("cp-1: unknown dependency ghost", result.errors)

    def test_check3_self_dependency(self):
        result = validate_checkpoint_plan(mk_plan([cp("cp-1", deps=["cp-1"])]))
        self.assertFalse(result.ok)
        self.assertTrue(any(
            e.startswith("cp-1:") and "self-dependency" in e for e in result.errors
        ), result.errors)

    def test_check3_cycle_reported(self):
        plan_ = mk_plan([cp("cp-1", deps=["cp-2"]), cp("cp-2", deps=["cp-1"])])
        result = validate_checkpoint_plan(plan_)
        self.assertFalse(result.ok)
        self.assertTrue(any("cycle" in e for e in result.errors), result.errors)

    def test_check5_missing_reason_for_not_required(self):
        # Schema guards construction; simulate a structurally-broken
        # expectation by swapping the stored instance AFTER validation.
        broken = GateExpectations.model_construct(
            required=False, reason=None, planned_tests=[], focus=[],
            test_instructions=[],
        )
        plan_ = mk_plan([cp("cp-1")])
        plan_.checkpoints[0].gates.__dict__["G2_ui"] = broken
        result = validate_checkpoint_plan(plan_)
        self.assertFalse(result.ok)
        self.assertTrue(any(
            e.startswith("cp-1:G2_ui:") and "reason" in e for e in result.errors
        ), result.errors)

    def test_check5_missing_gate_attribute(self):
        plan_ = mk_plan([cp("cp-1")])
        # Structural tampering: drop a stored attribute the validator expects.
        del plan_.checkpoints[0].gates.__dict__["G3_adversarial"]
        result = validate_checkpoint_plan(plan_)
        self.assertFalse(result.ok)
        self.assertTrue(any(
            "G3_adversarial" in e and "missing" in e for e in result.errors
        ), result.errors)

    def test_check6_required_g1_needs_planned_tests(self):
        plan_ = mk_plan([cp("cp-1", gates=all_gates(
            G1_behavior=gate(required=True, planned_tests=[]),
        ))])
        result = validate_checkpoint_plan(plan_)
        self.assertFalse(result.ok)
        self.assertIn(
            "cp-1:G1_behavior: required G1 needs non-empty planned_tests",
            [e for e in result.errors if "planned_tests" in e][0],
        )
        self.assertTrue(result.errors[0].startswith("cp-1"))

    def test_check7_provenance_valid(self):
        for provider, role, fb in [
            ("gemini", "cloud", False),
            ("mimo", "fallback", True),
        ]:
            with self.subTest(provider=provider):
                res = validate_checkpoint_plan(
                    mk_plan([cp("cp-1")], provider=provider, role=role, fallback=fb)
                )
                self.assertTrue(res.ok, res.errors)

        bad_cases = [
            dict(provider="openai", role="cloud", fallback=False),
            dict(provider="gemini", role="worker", fallback=False),
            dict(provider="gemini", role="cloud", fallback=True),
            dict(provider="mimo", role="fallback", fallback=False),
        ]
        for kwargs in bad_cases:
            with self.subTest(**kwargs):
                res = validate_checkpoint_plan(mk_plan([cp("cp-1")], **kwargs))
                self.assertFalse(res.ok)
                self.assertTrue(any("planner" in e for e in res.errors), res.errors)

    def test_check8_revision_must_be_positive_int(self):
        res = validate_checkpoint_plan(mk_plan([cp("cp-1")], revision=0))
        self.assertFalse(res.ok)
        self.assertTrue(any("revision" in e for e in res.errors), res.errors)

    def test_errors_ordered_by_check(self):
        # A plan failing checks 2 and 6 reports check-2 error first.
        plan_ = mk_plan([
            cp("cp-1", deps=["ghost"], gates=all_gates(
                G1_behavior=gate(required=True, planned_tests=[]),
            )),
        ])
        result = validate_checkpoint_plan(plan_)
        self.assertFalse(result.ok)
        idx_dep = next(i for i, e in enumerate(result.errors) if "unknown dependency" in e)
        idx_g1 = next(i for i, e in enumerate(result.errors) if "planned_tests" in e)
        self.assertLess(idx_dep, idx_g1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
