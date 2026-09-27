"""
P4 plan-level run-state tests — network-free, filesystem via tmp dirs.

Coverage (contract §5, third test file):

* save_plan/load_plan round-trip of a FULL PlanRunResult (stage
  results, repair lineage, failover events, session ids + sources);
* plan.json LOCATION: ``state/runs/<plan_id>/plan.json`` — exactly one
  level ABOVE the per-stage dirs ``state/runs/<plan_id>/<stage_id>/``
  (with ``root`` = ``<repo>/state/runs``);
* per-task records persist the session source (and get ``"none"``
  defaulted when a P3 caller omits it);
* P3 stage persistence UNCHANGED (regression): same three-argument
  constructor, same file shapes, deterministic atomic writes;
* PlanRunState shares one state system: plan writes work, stage
  writes without a stage_id raise instead of writing somewhere
  surprising.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.orchestration.agy_plan import (
    PLAN_ACCEPTED,
    PlanRunResult,
)
from app.orchestration.agy_runstate import (
    PlanRunState,
    StageRunState,
    safe_name,
)

PLAN_ID = "agp-p4-runstate"

STAGE_DICT = {
    "plan_id": PLAN_ID,
    "stage_id": "subtract",
    "status": "STAGE_ACCEPTED",
    "task_results": [
        {
            "worker_task_id": "subtract-t00",
            "attempt": 1,
            "status": "SUCCESS",
            "validation_ok": True,
            "changed_files": ["calculator.py"],
            "session_id": "ses_sub1",
            "failure_reason": "",
        }
    ],
    "changed_files": ["calculator.py"],
    "scope_violations": [],
    "task_verification": {},
    "stage_acceptance": {"ok": True, "checks": []},
    "cumulative_gate": {"ok": True, "checks": []},
    "commit": {
        "plan_id": PLAN_ID,
        "stage_id": "subtract",
        "parent_sha": "0" * 40,
        "commit_sha": "1" * 40,
        "changed_files": ["calculator.py"],
        "task_results": [],
        "verification": {},
        "message": "AGY stage subtract: x",
    },
    "supervisor_decision": {"decision": "ACCEPT"},
    "repair_lineage": [],
    "failover_events": [],
    "session_ids": ["ses_sub1"],
    "failure_reason": "",
    "replan": None,
}


def full_plan_result() -> PlanRunResult:
    """A complete PlanRunResult exercising every aggregate field."""
    result = PlanRunResult(
        plan_id=PLAN_ID,
        objective="extend the calculator",
        status=PLAN_ACCEPTED,
        stage_order=["subtract", "safe_divide"],
        stage_results={"subtract": dict(STAGE_DICT)},
        cumulative_invariants_ok=True,
        final_acceptance={"ok": True, "checks": [{"command": "t", "ok": True}]},
        plan_gate={
            "decision": "ACCEPT",
            "rationale": "all green",
            "repair_targets": [],
            "model": "supervisor-role",
            "deterministic_override": False,
        },
        repair_lineage=["plan-repair-1"],
        failover_events=[
            {"role": "dispatch", "fallback_reason": "rate_limit_exhausted"}
        ],
        session_ids=["ses_sub1", "ses_div2"],
        session_sources={"subtract-t00": "readback", "safe_divide-t00": "stdout"},
        stopped_at=None,
        plan_gate_reviews=[
            {"decision": "REPAIR"}, {"decision": "ACCEPT"}
        ],
        created="2026-09-27T00:00:00+00:00",
        updated="2026-09-27T00:10:00+00:00",
    )
    return result


class TestPlanJson(unittest.TestCase):
    def test_save_load_round_trip_full_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = PlanRunState(tmp, PLAN_ID)
            path = state.save_plan(full_plan_result())

            self.assertTrue(path.exists())
            self.assertEqual(path.name, "plan.json")
            self.assertEqual(path.parent, Path(tmp) / PLAN_ID)

            loaded = state.load_plan()
            self.assertEqual(loaded, full_plan_result().to_dict())
            # Every aggregate survives the disk trip.
            self.assertEqual(loaded["repair_lineage"], ["plan-repair-1"])
            self.assertEqual(len(loaded["failover_events"]), 1)
            self.assertEqual(loaded["session_sources"]["subtract-t00"],
                             "readback")
            self.assertEqual(loaded["stage_results"]["subtract"]["commit"]
                             ["commit_sha"], "1" * 40)
            self.assertEqual(len(loaded["plan_gate_reviews"]), 2)

    def test_plan_json_is_one_level_above_stage_dirs(self):
        """state/runs/<plan>/plan.json vs state/runs/<plan>/<stage>/."""
        with tempfile.TemporaryDirectory() as tmp:
            runs_root = Path(tmp) / "state" / "runs"
            plan_state = PlanRunState(runs_root, PLAN_ID)
            plan_path = plan_state.save_plan(full_plan_result())

            stage_result: dict = json.loads(json.dumps(STAGE_DICT))
            stage_state = StageRunState(runs_root, PLAN_ID, "subtract")
            stage_path = stage_state.save_stage(stage_result)

            self.assertEqual(
                plan_path,
                runs_root / PLAN_ID / "plan.json",
            )
            # plan.json's parent is the stage dir's parent: exactly
            # one level above, never inside a stage directory.
            self.assertEqual(plan_path.parent, stage_path.parent.parent)
            # Round-trip both granularities together.
            self.assertEqual(plan_state.load_plan(),
                             full_plan_result().to_dict())
            self.assertEqual(stage_state.load_stage(), stage_result)

    def test_load_plan_absent_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(PlanRunState(tmp, PLAN_ID).load_plan())

    def test_save_plan_rejects_non_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TypeError):
                PlanRunState(tmp, PLAN_ID).save_plan(42)

    def test_stage_driven_plan_writer_works_too(self):
        """A StageRunState (contract's per-stage instance) writes plan.json."""
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(tmp, PLAN_ID, "subtract")
            path = state.save_plan({"status": "PLAN_IN_PROGRESS"})
            self.assertEqual(path, Path(tmp) / PLAN_ID / "plan.json")
            self.assertEqual(state.load_plan(),
                             {"status": "PLAN_IN_PROGRESS"})


class TestPerTaskSessionSource(unittest.TestCase):
    def test_session_source_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(tmp, PLAN_ID, "subtract")
            path = state.save_task(
                {
                    "task_id": "subtract-t00",
                    "worker_task_id": "subtract-t00",
                    "attempt": 1,
                    "status": "SUCCESS",
                    "validation_ok": True,
                    "changed_files": ["calculator.py"],
                    "session_id": "ses_rec9",
                    "session_source": "readback",
                    "failure_reason": "",
                }
            )
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["session_id"], "ses_rec9")
            self.assertEqual(record["session_source"], "readback")
            # Load-back keeps both fields.
            loaded = state.load_tasks()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["session_source"], "readback")

    def test_missing_session_source_defaults_to_none(self):
        """A P3-era caller's record still gets an explicit source."""
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(tmp, PLAN_ID, "subtract")
            path = state.save_task(
                {
                    "task_id": "t1",
                    "session_id": None,
                    "status": "FAILED",
                    "validation_ok": False,
                    "changed_files": [],
                    "failure_reason": "x",
                }
            )
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["session_source"], "none")
            self.assertEqual(record["task_id"], "t1")


class TestP3StagePersistenceRegression(unittest.TestCase):
    """The P3 per-stage API and file shape must be unchanged."""

    def test_three_arg_constructor_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(Path(tmp) / "runs",
                                  PLAN_ID, "subtract")
            self.assertEqual(
                str(state.stage_dir),
                f"{tmp}/runs/{PLAN_ID}/subtract",
            )
            self.assertEqual(state.plan_dir,
                             Path(tmp) / "runs" / PLAN_ID)
            self.assertEqual(state.tasks_dir,
                             state.stage_dir / "tasks")

    def test_stage_commit_task_round_trip_and_atomicity(self):
        result = json.loads(json.dumps(STAGE_DICT))
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(Path(tmp) / "runs",
                                  PLAN_ID, "subtract")

            stage_path = state.save_stage(result)
            self.assertEqual(stage_path.name, "stage.json")
            # Deterministic serialization + no temp files survive.
            first = stage_path.read_bytes()
            self.assertEqual(stage_path.read_bytes(), first)
            self.assertIn(b'\n  "repair_lineage"', first)
            self.assertEqual(
                sorted(p.name for p in state.stage_dir.glob("*.tmp")), []
            )
            self.assertEqual(state.load_stage(), result)

            for task_record in result["task_results"]:
                task_path = state.save_task(task_record)
                self.assertEqual(task_path.parent.name, "tasks")
                self.assertTrue(task_path.exists())

            commit = state.save_commit(result["commit"])
            self.assertEqual(commit.name, "commit.json")
            loaded_commit = state.load_commit()
            self.assertEqual(loaded_commit["parent_sha"], "0" * 40)
            self.assertEqual(loaded_commit["commit_sha"], "1" * 40)
            for key in ("plan_id", "stage_id", "changed_files",
                        "task_results", "verification", "message"):
                self.assertIn(key, loaded_commit)

            # Fresh instance reads everything back from disk alone.
            recovered = StageRunState(Path(tmp) / "runs",
                                      PLAN_ID, "subtract")
            self.assertEqual(recovered.load_stage(), result)
            self.assertEqual(len(recovered.load_tasks()), 1)
            self.assertIsNotNone(recovered.load_commit())

    def test_missing_commit_file_loads_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StageRunState(tmp, PLAN_ID, "subtract")
            self.assertIsNone(state.load_commit())
            self.assertEqual(state.load_tasks(), [])


class TestPlanRunStateGuards(unittest.TestCase):
    def test_stage_writes_require_a_stage_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_state = PlanRunState(tmp, PLAN_ID)
            for writer in (
                lambda: plan_state.save_stage(STAGE_DICT),
                lambda: plan_state.save_task({"task_id": "t1"}),
                lambda: plan_state.save_commit({"plan_id": PLAN_ID}),
                lambda: plan_state.load_stage(),
                lambda: plan_state.load_tasks(),
                lambda: plan_state.load_commit(),
            ):
                with self.assertRaises(ValueError):
                    writer()

    def test_safe_name_keeps_ids_safe(self):
        self.assertEqual(safe_name("agp-p4-e2e"), "agp-p4-e2e")
        # Path separators become underscores, edge dots are stripped.
        self.assertEqual(safe_name("../evil/../x"), "evil_.._x")
        self.assertEqual(safe_name("plan s1"), "plan_s1")


if __name__ == "__main__":
    unittest.main()