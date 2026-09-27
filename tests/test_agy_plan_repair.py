"""
P5 addition: the ONE genuinely-absent plan-repair coverage item.

Requirement-item mapping (P5 contract, "PLAN-REPAIR COVERAGE"):

  9  repair preserves plan_id          -> NEW here
      (P4's test_agy_plan.py asserts lineage, staged stages, commit
      SHAs and review sequences across a repair, but NEVER that the
      plan_id itself flows through every repair artifact unchanged —
      no existing test inspects plan_id at all.)
 10  accepted stages not rerun         -> test_agy_plan.py
      test_10_no_whole_plan_restart_commits_unchanged (existing)
 11  only one bounded plan repair      -> test_agy_plan.py
      test_16_max_plan_repairs_enforced (existing)
 12  repair lineage persisted          -> test_agy_plan.py
      test_14_full_round_trip_and_p3_stage_regression (asserts
      recovered plan.json repair_lineage == ['plan-repair-1']) (existing)
 13  PLAN_ACCEPTED only after gate
     rerun                              -> test_agy_plan.py
      test_09_plan_repair_stage_own_commit_and_gate_then_accept
      (asserts plan_gate_reviews == [REPAIR, ACCEPT] AND final
      status PLAN_ACCEPTED) (existing); test_16 additionally proves
      no PLAN_ACCEPTED without a green second review (existing)

This file therefore contains EXACTLY one new test (item 9), reusing
P4's fixtures — no P4 test is duplicated or modified.
"""

import json
import tempfile
import unittest
from pathlib import Path

# P4 fixtures reused verbatim (pytest imports sibling test modules by
# plain name; tests/ is not a package).
from test_agy_plan import PlanHarness, make_linear_plan

from app.orchestration.agy_plan import PLAN_ACCEPTED
from app.orchestration.agy_runstate import PlanRunState


class TestRequirement9RepairPreservesPlanId(unittest.TestCase):
    def test_plan_id_preserved_across_bounded_plan_repair(self):
        """Item 9: ONE plan_id everywhere: memory, plan.json, the
        repair stage result, its commit record and the whole on-disk
        run-state tree."""
        plan = make_linear_plan(2)
        original_id = plan.plan_id
        harness = PlanHarness(
            decisions=[
                "ACCEPT",   # stage s1 gate
                "ACCEPT",   # stage s2 gate
                "REPAIR",   # plan gate #1 (bounded repair requested)
                "ACCEPT",   # plan-repair-1 stage gate
                "ACCEPT",   # plan gate #2 (re-review)
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            run_state = PlanRunState(tmp, original_id)
            result = harness.run(plan, run_state=run_state)

            # The bounded repair really happened for this plan.
            self.assertEqual(result.status, PLAN_ACCEPTED)
            self.assertEqual(result.repair_lineage, ["plan-repair-1"])

            # (a) In-memory: the plan result AND every stage result
            # (original stages AND the repair stage) carry the same
            # plan_id.
            self.assertEqual(result.plan_id, original_id)
            for stage_id, stage_result in result.stage_results.items():
                with self.subTest(stage=stage_id):
                    self.assertEqual(stage_result["plan_id"], original_id)

            # (b) Persisted plan.json: same plan_id after the repair.
            persisted = run_state.load_plan()
            assert persisted is not None  # narrowing (unittest-checked)
            self.assertEqual(persisted["plan_id"], original_id)
            self.assertEqual(persisted["status"], PLAN_ACCEPTED)
            self.assertEqual(persisted["repair_lineage"], ["plan-repair-1"])

            # (c) The repair's own commit record kept the plan_id.
            repair_result = persisted["stage_results"]["plan-repair-1"]
            self.assertEqual(repair_result["commit"]["plan_id"],
                             original_id)
            repair_state = Path(tmp) / original_id / "plan-repair-1"
            commit_on_disk = json.loads(
                (repair_state / "commit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(commit_on_disk["plan_id"], original_id)

            # (d) Every commit EVIDENCE the driver handed to the
            # committer across the whole run names this plan_id.
            self.assertTrue(harness.commit_evidence)
            for evidence in harness.commit_evidence:
                self.assertEqual(evidence["plan_id"], original_id)

            # (e) Single plan directory (no restart/re-plan: one
            # plan.json, its repair stage nested BELOW it).
            plan_dir = Path(tmp) / original_id
            plan_jsons = sorted(
                path.relative_to(Path(tmp)).as_posix()
                for path in Path(tmp).rglob("plan.json")
            )
            self.assertEqual(
                plan_jsons, [f"{original_id}/plan.json"]
            )
            self.assertTrue(
                (plan_dir / "plan-repair-1" / "stage.json").exists()
            )
            # stage.json of every recorded stage names the plan too.
            for stage_path in sorted(plan_dir.glob("*/stage.json")):
                with self.subTest(stage_file=str(stage_path)):
                    stage_doc = json.loads(
                        stage_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(stage_doc["plan_id"], original_id)


if __name__ == "__main__":
    unittest.main()