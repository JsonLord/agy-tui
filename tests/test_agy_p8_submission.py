"""P8 worker A — submission pipeline tests (contract items 28-32).

28  full happy path: validation -> persistence -> handoff -> projection
    -> SUBMITTED, with the canonical next_gate/runtime_owner recorded
29  planner-claimed gate statuses are DISCARDED: every gate record is
    re-seeded PENDING (NOT_APPLICABLE for required=False)
30  G0 required: missing approval / wrong decision / revision mismatch
    all rejected before any write
31  idempotency: same plan_id+revision + identical document is a no-op;
    different document at the same revision is an error
32  todo_sink is projection-only: a raising/failing sink never unwinds
    canonical state (plan stays SUBMITTED)

Plus: invalid plans never touch the filesystem; provenance.json is
written beside checkpoint_plan.json and answers who/when/which-analyses.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.orchestration.checkpoint_planning.planner import _payload_to_plan
from app.orchestration.checkpoint_planning.schemas import (
    AnalysisRef,
    G0Approval,
    GateStatus,
)
from app.orchestration.checkpoint_planning.submission import (
    seed_gate_records,
    submit_checkpoint_plan,
)


def payload(gate_claims=None):
    def gate(required=True, reason=None, tests=None):
        return {
            "required": required,
            "reason": reason,
            "planned_tests": tests if tests is not None else (["test_a"] if required else []),
            "focus": [],
            "test_instructions": [],
        }

    data = {
        "plan_id": "plan-9",
        "feature_id": "feat-9",
        "objective": "submit me",
        "planner": {"role": "cloud", "provider": "gemini", "gate_template_version": "1"},
        "checkpoints": [
            {
                "checkpoint_id": "cp-1",
                "title": "first",
                "objective": "o1",
                "dependencies": [],
                "gates": {
                    "G1_behavior": gate(),
                    "G2_ui": gate(False, "no UI here"),
                    "G3_adversarial": gate(),
                    "G4_human": gate(),
                },
                "gate_records": {},
            },
            {
                "checkpoint_id": "cp-2",
                "title": "second",
                "objective": "o2",
                "dependencies": ["cp-1"],
                "gates": {
                    "G1_behavior": gate(False, "docs only"),
                    "G2_ui": gate(False, "no UI here"),
                    "G3_adversarial": gate(),
                    "G4_human": gate(),
                },
                "gate_records": {},
            },
        ],
        "created_at": "2026-09-28T00:00:00+00:00",
        "revision": 1,
        "analyses": [
            {
                "analysis_id": "an-1",
                "objective": "where are the gates",
                "analyst_session": "sess-42",
                "evidence_ref": "state/analyses/an-1.json",
                "requested_at": "2026-09-27T23:00:00+00:00",
            }
        ],
    }
    if gate_claims:
        data["checkpoints"][0]["gate_records"] = gate_claims
    return data


def approved_plan(claims=None, revision=1):
    plan = _payload_to_plan(payload(claims))
    plan.revision = revision
    plan.g0 = G0Approval(
        decision="APPROVE", approved_at="2026-09-28T01:00:00+00:00",
        approver="human:leon", revision=revision,
    )
    return plan


def _load(root, plan_id="plan-9"):
    doc = root / "plans" / plan_id / "checkpoint_plan.json"
    return json.loads(doc.read_text(encoding="utf-8")) if doc.exists() else None


class HappyPathTests(unittest.TestCase):
    # -- 28 ------------------------------------------------------------------
    def test_full_submission_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            handoffs, sinks = [], []
            result = submit_checkpoint_plan(
                approved_plan(),
                state_root=root,
                runtime_handoff=lambda p: handoffs.append(p.plan_id) or {},
                todo_sink=lambda p, h: sinks.append((p.plan_id, h.get("next_gate"))) or {"projected": True},
            )
            self.assertTrue(result.accepted, result.errors)
            self.assertEqual(result.plan_id, "plan-9")
            self.assertEqual(result.runtime_owner, "agy-local")
            # first checkpoint (exec order) first REQUIRED gate:
            self.assertEqual(result.next_gate, "cp-1:G1_behavior")
            self.assertTrue(result.planner_session_complete)

            doc = _load(root)
            self.assertEqual(doc["status"], "SUBMITTED")
            self.assertEqual(doc["runtime_owner"], "agy-local")
            self.assertEqual(doc["next_gate"], "cp-1:G1_behavior")
            self.assertIn("submitted_at", doc)
            self.assertEqual(doc["todo_projection"], {"projected": True})
            self.assertEqual(handoffs, ["plan-9"])
            self.assertEqual(sinks, [("plan-9", "cp-1:G1_behavior")])

            # provenance written beside the plan doc
            prov_path = root / "plans" / "plan-9" / "provenance.json"
            self.assertTrue(prov_path.exists())
            prov = json.loads(prov_path.read_text(encoding="utf-8"))
            self.assertEqual(prov["who"]["provider"], "gemini")
            self.assertFalse(prov["who"]["fallback_used"])
            self.assertEqual(prov["when"]["g0_approver"], "human:leon")
            self.assertIsNotNone(prov["when"]["submitted_at"])
            self.assertEqual(prov["analyses"][0]["analyst_session"], "sess-42")
            self.assertEqual(prov["revision"], 1)

    def test_next_gate_skips_not_applicable_leads(self):
        with tempfile.TemporaryDirectory() as td:
            plan = approved_plan()
            # cp-1 G1 not required -> first required is cp-1 G3
            cp1 = plan.checkpoints[0]
            cp1.gates.G1_behavior = cp1.gates.G1_behavior.model_copy(
                update={"required": False, "reason": "docs only", "planned_tests": []}
            )
            result = submit_checkpoint_plan(plan, state_root=Path(td))
            self.assertTrue(result.accepted, result.errors)
            self.assertEqual(result.next_gate, "cp-1:G3_adversarial")


class GateDiscardTests(unittest.TestCase):
    # -- 29 ------------------------------------------------------------------
    def test_planner_claimed_statuses_are_discarded(self):
        claims = {
            "G1_behavior": {
                "gate": "G1_behavior",
                "status": "PASSED",  # planner CLAIMS it already passed
                "applicability": "REQUIRED",
                "preseeded": {
                    "required": True,
                    "reason": None,
                    "planned_tests": ["test_a"],
                    "focus": [],
                    "test_instructions": [],
                },
                "evidence_refs": ["fabricated"],
                "closed_at": "now",
                "closed_by": "planner",
            }
        }
        with tempfile.TemporaryDirectory() as td:
            result = submit_checkpoint_plan(approved_plan(claims), state_root=Path(td))
            self.assertTrue(result.accepted, result.errors)
            doc = _load(Path(td))
            records = doc["checkpoints"][0]["gate_records"]
            self.assertEqual(records["G1_behavior"]["status"], "PENDING")
            self.assertEqual(records["G1_behavior"]["evidence_refs"], [])
            self.assertIsNone(records["G1_behavior"]["closed_at"])
            self.assertIsNone(records["G1_behavior"]["closed_by"])
            self.assertEqual(records["G2_ui"]["applicability"], "NOT_APPLICABLE")
            self.assertEqual(records["G2_ui"]["status"], "PENDING")
            for cp in doc["checkpoints"]:
                self.assertEqual(set(cp["gate_records"]), {
                    "G1_behavior", "G2_ui", "G3_adversarial", "G4_human"
                })

    def test_seed_gate_records_pure(self):
        plan = approved_plan()
        records = seed_gate_records(plan.checkpoints[1])
        self.assertEqual(records["G1_behavior"].status, GateStatus.PENDING)
        self.assertEqual(records["G1_behavior"].applicability, "NOT_APPLICABLE")
        self.assertEqual(records["G3_adversarial"].applicability, "REQUIRED")
        self.assertEqual(records["G4_human"].preseeded.required, True)


class G0Tests(unittest.TestCase):
    # -- 30 ------------------------------------------------------------------
    def _assert_rejected_no_write(self, plan, root, fragment):
        before = set(root.rglob("*")) if root.exists() else set()
        result = submit_checkpoint_plan(plan, state_root=root)
        self.assertFalse(result.accepted)
        self.assertTrue(any(fragment in e for e in result.errors), result.errors)
        after = set(root.rglob("*")) if root.exists() else set()
        self.assertEqual(before, after, "rejected submission must not write")

    def test_missing_g0_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            plan = approved_plan()
            plan.g0 = None
            self._assert_rejected_no_write(plan, Path(td), "approval missing")

    def test_wrong_decision_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            plan = approved_plan()
            plan.g0 = plan.g0.model_copy(update={"decision": "REQUEST_REVISION"})
            self._assert_rejected_no_write(plan, Path(td), "APPROVE")

    def test_revision_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            plan = approved_plan()
            plan.g0 = plan.g0.model_copy(update={"revision": 99})
            self._assert_rejected_no_write(plan, Path(td), "does not match")

    def test_g0_argument_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            plan = approved_plan()
            plan.g0 = None
            result = submit_checkpoint_plan(
                plan,
                state_root=Path(td),
                g0_approval=G0Approval(decision="APPROVE", revision=1, approver="a"),
            )
            self.assertTrue(result.accepted, result.errors)


class IdempotencyTests(unittest.TestCase):
    # -- 31 ------------------------------------------------------------------
    def test_identical_resubmit_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = submit_checkpoint_plan(approved_plan(), state_root=root)
            self.assertTrue(first.accepted, first.errors)
            doc = _load(root)
            second = submit_checkpoint_plan(approved_plan(), state_root=root)
            self.assertTrue(second.accepted, second.errors)
            self.assertTrue(second.planner_session_complete)
            self.assertEqual(second.runtime_owner, "agy-local")
            self.assertEqual(second.next_gate, "cp-1:G1_behavior")
            # no-op: persisted document untouched (same submitted_at)
            self.assertEqual(_load(root)["submitted_at"], doc["submitted_at"])

    def test_different_document_same_revision_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertTrue(submit_checkpoint_plan(approved_plan(), state_root=root).accepted)
            other = approved_plan()
            other.checkpoints[0].title = "changed after approval"
            result = submit_checkpoint_plan(other, state_root=root)
            self.assertFalse(result.accepted)
            self.assertTrue(any("different document" in e for e in result.errors), result.errors)

    def test_new_revision_persists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertTrue(submit_checkpoint_plan(approved_plan(), state_root=root).accepted)
            bumped = approved_plan(revision=2)
            result = submit_checkpoint_plan(bumped, state_root=root)
            self.assertTrue(result.accepted, result.errors)
            self.assertEqual(_load(root)["revision"], 2)


class ProjectionOnlyTests(unittest.TestCase):
    # -- 32 ------------------------------------------------------------------
    def test_failing_todo_sink_never_unwinds_state(self):
        def explode(plan, handoff):
            raise RuntimeError("opencode todo endpoint down")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = submit_checkpoint_plan(
                approved_plan(), state_root=root, todo_sink=explode
            )
            self.assertTrue(result.accepted, result.errors)
            doc = _load(root)
            self.assertEqual(doc["status"], "SUBMITTED")
            self.assertFalse(doc["todo_projection"]["projected"])
            self.assertIn("error", doc["todo_projection"])

    def test_failing_runtime_handoff_is_error(self):
        def boom(plan):
            raise RuntimeError("runtime refused handoff")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = submit_checkpoint_plan(
                approved_plan(), state_root=root, runtime_handoff=boom
            )
            self.assertFalse(result.accepted)
            self.assertTrue(any("runtime_handoff failed" in e for e in result.errors), result.errors)


class RejectBeforeWriteTests(unittest.TestCase):
    def test_invalid_plan_never_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = approved_plan()
            plan.checkpoints[1].dependencies = ["ghost"]
            result = submit_checkpoint_plan(plan, state_root=root)
            self.assertFalse(result.accepted)
            self.assertTrue(any("unknown dependency" in e for e in result.errors))
            self.assertIsNone(_load(root))
            self.assertFalse((root / "plans").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
