"""P13 — continuation admission: the 8 mandated focused scenarios.

Deterministic, model-free, no network, tmp-dir state only. Builds the
exact P12 artifact shape (continuation + human_provenance + CONSUMED
source request) and exercises
``app.orchestration.checkpoint_planning.continuation_admission``:

1. valid continuation + G0 APPROVE -> admitted;
2. no G0 -> rejected;
3. source request not CONSUMED -> rejected;
4. tampered continuation -> rejected;
5. canonical gate records seeded;
6. next gate correct;
7. duplicate identical admission safe/idempotent;
8. conflicting duplicate rejected.

Plus the explicit fail-closed rules: stale G0 revision, missing
source request, missing continuation, planner-fabricated g0.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.orchestration.checkpoint_planning.continuation_admission import (
    AdmissionResult,
    ContinuationAdmissionError,
    admission_record_path,
    admit_continuation,
    continuation_path,
    load_continuation,
)
from app.orchestration.checkpoint_planning.schemas import G0Approval

PLAN_ID = "plan-test"
CHECKPOINT_ID = "cp-a"
REQUEST_ID = "hpir-test-1"
FEEDBACK = "Change Save to Apply."


def _now() -> str:
    return datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc).isoformat()


def _plan_doc(revision: int = 2, **plan_overrides) -> dict:
    doc = {
        "plan_id": PLAN_ID,
        "feature_id": "feat-1",
        "objective": "do the thing",
        "planner": {"provider": "gemini", "role": "cloud"},
        "checkpoints": [
            {
                "checkpoint_id": CHECKPOINT_ID,
                "title": "t",
                "objective": "o",
                "gates": {
                    "G1_behavior": {"required": True, "planned_tests": ["test_a"]},
                    "G2_ui": {"required": False, "reason": "no UI in scope"},
                    "G3_adversarial": {"required": True, "planned_tests": ["test_b"]},
                    "G4_human": {"required": True, "planned_tests": []},
                },
            }
        ],
        "status": "DRAFT",
        "revision": revision,
        "created_at": "2026-09-29T00:00:00+00:00",
    }
    doc.update(plan_overrides)
    return doc


def _request_doc(status: str = "CONSUMED", **overrides) -> dict:
    doc = {
        "request_id": REQUEST_ID,
        "plan_id": PLAN_ID,
        "checkpoint_id": CHECKPOINT_ID,
        "gate_id": "G4_human",
        "decision": "NEEDS_CHANGES",
        "feedback": FEEDBACK,
        "status": status,
        "created_at": _now(),
        "claimed_at": _now(),
        "consumed_at": _now(),
        "continuation_plan_id": PLAN_ID,
        "continuation_revision": 2,
    }
    doc.update(overrides)
    return doc


def _build_state(root: Path, *, request_status: str = "CONSUMED",
                 artifact_plan: dict | None = None,
                 artifact_feedback: str | None = None,
                 request_feedback: str | None = None,
                 with_artifact: bool = True,
                 with_request: bool = True,
                 plan_overrides: dict | None = None) -> None:
    """Lay down the exact P12 artifact shape under ``root``."""
    plan = _plan_doc(**(plan_overrides or {}))
    if artifact_plan is not None:
        plan = artifact_plan
    if with_request:
        request_dir = (
            root / "runs" / PLAN_ID / "human" / CHECKPOINT_ID / "planner_inputs"
        )
        request_dir.mkdir(parents=True, exist_ok=True)
        request = _request_doc(status=request_status)
        if request_feedback is not None:
            request["feedback"] = request_feedback
        (request_dir / f"{REQUEST_ID}.json").write_text(
            json.dumps(request, indent=2), encoding="utf-8"
        )
    if with_artifact:
        continuation_dir = root / "plans" / PLAN_ID / "continuations"
        continuation_dir.mkdir(parents=True, exist_ok=True)
        artifact = {
            "continuation": plan,
            "human_provenance": {
                "source_request_id": REQUEST_ID,
                "plan_id": PLAN_ID,
                "checkpoint_id": CHECKPOINT_ID,
                "gate_id": "G4_human",
                "feedback": FEEDBACK if artifact_feedback is None else artifact_feedback,
                "actor": "human:test",
                "created_at": _now(),
                "consumed_at": _now(),
            },
        }
        (continuation_dir / f"{REQUEST_ID}.json").write_text(
            json.dumps(artifact, indent=2), encoding="utf-8"
        )


def _g0(decision: str = "APPROVE", revision: int = 2,
        approver: str = "human:g0-approver") -> G0Approval:
    return G0Approval(
        decision=decision, revision=revision,
        approver=approver, approved_at=_now(),
    )


class ContinuationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # -- 1. valid continuation + G0 APPROVE -> admitted ----------------------
    def test_valid_continuation_with_g0_approve_is_admitted(self):
        _build_state(self.root)
        result = admit_continuation(
            self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0()
        )
        self.assertIsInstance(result, AdmissionResult)
        self.assertTrue(result.accepted)
        self.assertFalse(result.idempotent)
        self.assertEqual(result.plan_id, PLAN_ID)
        self.assertEqual(result.revision, 2)
        self.assertEqual(result.approver, "human:g0-approver")
        # canonical plan persisted via the existing submission pipeline
        plan_doc = json.loads(
            (self.root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
        )
        self.assertEqual(plan_doc["status"], "SUBMITTED")
        self.assertEqual(plan_doc["revision"], 2)
        self.assertEqual(plan_doc["g0"]["decision"], "APPROVE")
        # admission provenance recorded
        record = json.loads(admission_record_path(self.root, PLAN_ID, REQUEST_ID).read_text())
        self.assertEqual(record["status"], "ADMITTED")
        self.assertEqual(record["request_id"], REQUEST_ID)
        self.assertEqual(record["revision"], 2)
        self.assertEqual(record["continuation_hash"], record["continuation_hash"])
        self.assertTrue(record["continuation_hash"])

    # -- 2. no G0 -> rejected --------------------------------------------------
    def test_missing_g0_rejected(self):
        _build_state(self.root)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=None)
        self.assertTrue(any("G0" in e for e in ctx.exception.errors))
        self.assertFalse(
            (self.root / "plans" / PLAN_ID / "checkpoint_plan.json").exists()
        )

    # -- 3. source request not CONSUMED -> rejected ---------------------------
    def test_source_request_not_consumed_rejected(self):
        _build_state(self.root, request_status="PENDING")
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(
            any("CONSUMED" in e and "PENDING" in e for e in ctx.exception.errors)
        )

    # -- 4. tampered continuation -> rejected ----------------------------------
    def test_tampered_continuation_rejected(self):
        # Artifact feedback no longer matches the verbatim request feedback.
        _build_state(self.root, artifact_feedback="something else")
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(any("feedback provenance mismatch" in e for e in ctx.exception.errors))

    def test_tampered_plan_content_rejected_by_revalidation(self):
        # Break domain validity: drop a required gate's planned tests.
        bad = _plan_doc()
        bad["checkpoints"][0]["gates"]["G1_behavior"]["planned_tests"] = []
        _build_state(self.root, artifact_plan=bad)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(any("domain validation" in e for e in ctx.exception.errors))

    # -- 5. canonical gate records seeded ---------------------------------------
    def test_gate_records_seeded_on_canonical_plan(self):
        _build_state(self.root)
        result = admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(result.accepted)
        plan_doc = json.loads(
            (self.root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
        )
        seeded = plan_doc["checkpoints"][0]["gate_records"]
        self.assertEqual(set(seeded), {"G1_behavior", "G2_ui", "G3_adversarial", "G4_human"})
        self.assertTrue(all(rec["status"] == "PENDING" for rec in seeded.values()))
        self.assertEqual(seeded["G1_behavior"]["applicability"], "REQUIRED")
        self.assertEqual(seeded["G2_ui"]["applicability"], "NOT_APPLICABLE")
        # the result exposes the same deterministic view
        self.assertEqual(result.gate_records[f"{CHECKPOINT_ID}:G1_behavior"]["status"], "PENDING")

    # -- 6. next gate correct ----------------------------------------------------
    def test_next_gate_correct(self):
        _build_state(self.root)
        result = admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertEqual(result.next_gate, f"{CHECKPOINT_ID}:G1_behavior")
        plan_doc = json.loads(
            (self.root / "plans" / PLAN_ID / "checkpoint_plan.json").read_text()
        )
        self.assertEqual(plan_doc["next_gate"], f"{CHECKPOINT_ID}:G1_behavior")

    # -- 7. duplicate identical admission is idempotent ---------------------------
    def test_duplicate_identical_admission_is_idempotent(self):
        _build_state(self.root)
        first = admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(first.accepted)
        self.assertFalse(first.idempotent)
        second = admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(second.accepted)
        self.assertTrue(second.idempotent)
        self.assertEqual(second.plan_id, first.plan_id)
        self.assertEqual(second.revision, first.revision)
        self.assertEqual(second.next_gate, first.next_gate)
        self.assertEqual(second.approver, first.approver)

    # -- 8. conflicting duplicate rejected ------------------------------------------
    def test_conflicting_duplicate_rejected(self):
        _build_state(self.root)
        first = admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(first.accepted)
        # The continuation artifact is tampered AFTER admission: same id,
        # different content.
        artifact_path = continuation_path(self.root, PLAN_ID, REQUEST_ID)
        doc = json.loads(artifact_path.read_text())
        doc["continuation"]["objective"] = "tampered after admission"
        artifact_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(
            any("conflicting duplicate" in e for e in ctx.exception.errors)
        )

    # -- extra fail-closed rules -----------------------------------------------------
    def test_stale_g0_revision_rejected(self):
        _build_state(self.root)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0(revision=1))
        self.assertTrue(any("revision" in e for e in ctx.exception.errors))

    def test_g0_needs_changes_decision_rejected(self):
        _build_state(self.root)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(
                self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0(decision="NEEDS_CHANGES")
            )
        self.assertTrue(any("APPROVE" in e for e in ctx.exception.errors))

    def test_missing_source_request_rejected(self):
        _build_state(self.root, with_request=False)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(any("source request" in e for e in ctx.exception.errors))

    def test_missing_continuation_rejected(self):
        _build_state(self.root, with_artifact=False)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(any("continuation" in e and "missing" in e for e in ctx.exception.errors))

    def test_planner_fabricated_g0_rejected(self):
        # A g0 block embedded in the continuation is never honored.
        bad = _plan_doc()
        bad["g0"] = {"decision": "APPROVE", "revision": 2, "approver": "planner"}
        _build_state(self.root, artifact_plan=bad)
        with self.assertRaises(ContinuationAdmissionError) as ctx:
            admit_continuation(self.root, PLAN_ID, REQUEST_ID, g0_approval=_g0())
        self.assertTrue(any("g0" in e for e in ctx.exception.errors))

    def test_load_continuation_round_trips(self):
        _build_state(self.root)
        plan, provenance = load_continuation(self.root, PLAN_ID, REQUEST_ID)
        self.assertEqual(plan.plan_id, PLAN_ID)
        self.assertEqual(plan.revision, 2)
        self.assertIsNone(plan.g0)
        self.assertEqual(provenance["source_request_id"], REQUEST_ID)
        self.assertEqual(provenance["feedback"], FEEDBACK)


if __name__ == "__main__":
    unittest.main()
