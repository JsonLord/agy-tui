"""
P6 CommitGateRecord tests — network-free, everything injected
(contract §6, ``tests/test_agy_commitgate.py``):

* build / persist / load round-trip with EVERY field verified;
* exact field set (``additionalProperties: false`` semantics): an
  unexpected key is rejected, a missing key is rejected;
* FAIL-CLOSED CONSISTENCY (documented choice): ``build`` RAISES
  ``ValueError`` for ``decision == "ACCEPT"`` with deterministic
  failure; the constrained (overridden) decision records fine and
  flips ``repair_required``;
* plan-level record path ``commits/plan-gate.json``;
* duplicate-persist guard: idempotent same-SHA write, ``ValueError``
  on conflicting content for the same SHA, ``ValueError`` naming the
  file on a corrupt record at reload;
* ``evidence_refs`` resolve to REAL files under a tmp run dir.

No network, no subprocess, no model: the stage/plan fixtures are
plain persisted-shaped dicts matching P3/P4 run-state output.
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.orchestration.agy_commitgate import (
    RECORD_FIELDS,
    CommitGateRecord,
    build_commit_gate_record,
    load_commit_gate_records,
    persist_commit_gate_record,
    record_path,
)
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage


def make_plan(plan_id: str = "agp-p6-cg") -> DevelopmentPlan:
    stage = PlanStage(
        stage_id="s1",
        objective="implement the first slice",
        dependencies=[],
        allowed_files=["calculator.py", "tests/test_calculator.py"],
        implementation_context="bounded context",
        acceptance_commands=[
            AcceptanceCommand(
                command="python3 stage-gate s1",
                expected_exit_code=0,
                description="s1 gate",
            )
        ],
        acceptance_conditions=["s1 behaves as specified"],
        cumulative_invariants=["add(a, b) still returns a + b"],
    )
    return DevelopmentPlan(
        plan_id=plan_id,
        objective="extend the calculator with deterministic tests",
        stages=[stage],
        final_acceptance=[
            AcceptanceCommand(
                command="python3 plan-final-acceptance",
                expected_exit_code=0,
                description="plan final acceptance",
            )
        ],
    )


def stage_result_dict(
    *,
    decision: str = "ACCEPT",
    violations: list[str] | None = None,
    validation_ok: bool = True,
    cumulative_ok: bool = True,
) -> dict:
    """One StageRunResult.to_dict()-shaped payload (P3 semantics)."""
    task = {
        "worker_task_id": "s1-t00",
        "task_id": "s1-t00",
        "attempt": 1,
        "status": "SUCCESS" if validation_ok else "FAILED",
        "validation_ok": validation_ok,
        "validation_output": "tests passed",
        "changed_files": ["calculator.py"],
        "session_id": "ses_00000000000000000000000001",
        "session_source": "readback",
        "failure_reason": "",
    }
    return {
        "plan_id": "agp-p6-cg",
        "stage_id": "s1",
        "status": "STAGE_ACCEPTED" if decision == "ACCEPT" else "STAGE_REPAIR",
        "task_results": [task],
        "changed_files": ["calculator.py"],
        "scope_violations": list(violations or []),
        "task_verification": {
            "s1-t00": {
                "attempt": 1,
                "status": task["status"],
                "validation_ok": validation_ok,
                "validation_output": task["validation_output"],
                "changed_files": ["calculator.py"],
                "session_id": task["session_id"],
                "failure_reason": "",
            }
        },
        "stage_acceptance": {
            "ok": decision == "ACCEPT",
            "checks": [
                {
                    "name": "s1 gate",
                    "command": "python3 stage-gate s1",
                    "expected_exit_code": 0,
                    "exit_code": 0 if decision == "ACCEPT" else 1,
                    "ok": decision == "ACCEPT",
                    "output": "green" if decision == "ACCEPT" else "red",
                }
            ],
        },
        "cumulative_gate": {
            "ok": cumulative_ok,
            "checks": [
                {
                    "name": "cumulative-01",
                    "command": "python3 plan-cumulative-suite",
                    "exit_code": 0 if cumulative_ok else 1,
                    "ok": cumulative_ok,
                    "output": "green" if cumulative_ok else "red",
                }
            ],
        },
        "commit": {
            "plan_id": "agp-p6-cg",
            "stage_id": "s1",
            "parent_sha": "b" * 40,
            "commit_sha": "a" * 40,
            "changed_files": ["calculator.py"],
            "message": "AGY stage s1: implement the first slice",
            "task_results": [
                {
                    "worker_task_id": "s1-t00",
                    "attempt": 1,
                    "status": task["status"],
                    "validation_ok": validation_ok,
                }
            ],
            "verification": {"s1-t00": {"validation_ok": validation_ok}},
        },
        "supervisor_decision": {
            "decision": decision,
            "rationale": "all deterministic checks green",
            "repair_targets": [],
            "model": "supervisor-role-model",
            "deterministic_override": False,
            "failover_event": None,
        },
        "repair_lineage": [],
        "failover_events": [],
        "session_ids": [task["session_id"]],
        "failure_reason": "",
        "replan": None,
    }


def write_evidence_files(run_dir: Path) -> None:
    """The files evidence_refs points at must genuinely exist.

    In a real run ``plan.json`` (the PLAN_IN_PROGRESS header) and
    ``plan_doc.json`` are written BEFORE the plan gate, so the
    plan-level refs resolve exactly the same way.
    """
    stage_dir = run_dir / "s1"
    (stage_dir / "tasks").mkdir(parents=True, exist_ok=True)
    result = stage_result_dict()
    (stage_dir / "stage.json").write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )
    (stage_dir / "commit.json").write_text(
        json.dumps(result["commit"], sort_keys=True), encoding="utf-8"
    )
    (stage_dir / "tasks" / "s1-t00.json").write_text(
        json.dumps(result["task_results"][0], sort_keys=True),
        encoding="utf-8",
    )
    (run_dir / "plan.json").write_text(
        json.dumps({"plan_id": "agp-p6-cg", "status": "PLAN_IN_PROGRESS"}),
        encoding="utf-8",
    )
    (run_dir / "plan_doc.json").write_text(
        json.dumps({"plan_id": "agp-p6-cg"}), encoding="utf-8"
    )


class TestBuildRoundTrip(unittest.TestCase):
    def test_11a_build_persist_load_round_trips_every_field(self):
        """Round trip: two records persist, load, field-by-field equal."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg"
            write_evidence_files(run_dir)
            plan = make_plan()
            stage = plan.stages[0]

            record = build_commit_gate_record(
                plan, stage, stage_result_dict()
            )
            record_path_after_persist = persist_commit_gate_record(
                run_dir, record
            )
            self.assertEqual(
                record_path_after_persist.name, f"{'a' * 40}.json"
            )

            second_payload = stage_result_dict()
            second_payload["commit"] = dict(
                second_payload["commit"],
                parent_sha="a" * 40,
                commit_sha="c" * 40,
            )
            second = build_commit_gate_record(
                plan, stage, second_payload
            )
            persist_commit_gate_record(run_dir, second)

            loaded = load_commit_gate_records(run_dir)
            self.assertEqual(len(loaded), 2)
            by_sha = {entry.commit_sha: entry for entry in loaded}
            self.assertEqual(set(by_sha), {"a" * 40, "c" * 40})

            first = by_sha["a" * 40]
            self.assertEqual(first.to_dict(), record.to_dict())
            # Every contract field present with real values.
            payload = first.to_dict()
            self.assertEqual(set(payload), set(RECORD_FIELDS))
            self.assertEqual(payload["plan_id"], "agp-p6-cg")
            self.assertEqual(payload["stage_id"], "s1")
            self.assertEqual(payload["parent_sha"], "b" * 40)
            self.assertEqual(payload["changed_files"], ["calculator.py"])
            self.assertEqual(payload["scope_check"], {
                "violations": [], "ok": True,
            })
            self.assertEqual(
                payload["task_verification_results"][0]["task_id"],
                "s1-t00",
            )
            self.assertEqual(
                payload["task_verification_results"][0]["session_source"],
                "readback",
            )
            self.assertTrue(
                payload["stage_acceptance_results"]["ok"]
            )
            self.assertTrue(payload["cumulative_gate_results"]["ok"])
            self.assertEqual(payload["supervisor_role"], "supervisor")
            self.assertEqual(
                payload["supervisor_session_id"], None
            )  # stateless gate: None is valid, never invented
            self.assertEqual(
                payload["supervisor_decision"]["decision"], "ACCEPT"
            )
            self.assertFalse(
                payload["supervisor_decision"]["deterministic_override"]
            )
            self.assertFalse(payload["repair_required"])
            self.assertIn("T", payload["timestamp"])
            for ref in payload["evidence_refs"]:
                self.assertTrue((run_dir / ref).exists(), ref)

            # Second record round-trips all its fields too.
            self.assertEqual(
                by_sha["c" * 40].to_dict(), second.to_dict()
            )

    def test_11b_exact_field_set_rejects_extra_and_missing(self):
        """additionalProperties=false: extra key or missing key raise."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg"
            write_evidence_files(run_dir)
            plan = make_plan()
            record = build_commit_gate_record(
                plan, plan.stages[0], stage_result_dict()
            )
            payload = record.to_dict()

            with self.assertRaises(ValueError) as extra:
                CommitGateRecord.from_dict(
                    {**payload, "surprise": 1}
                )
            self.assertIn("surprise", str(extra.exception))

            missing = dict(payload)
            missing.pop("evidence_refs")
            with self.assertRaises(ValueError) as missing_error:
                CommitGateRecord.from_dict(missing)
            self.assertIn("evidence_refs", str(missing_error.exception))

    def test_11c_fail_closed_accept_with_deterministic_failure_raises(
        self,
    ):
        """Documented rule: build RAISES on raw ACCEPT + failure."""
        plan = make_plan()

        with self.assertRaises(ValueError) as scope_error:
            build_commit_gate_record(
                plan,
                plan.stages[0],
                stage_result_dict(violations=["other.py"]),
            )
        self.assertIn("FAIL-CLOSED", str(scope_error.exception))

        with self.assertRaises(ValueError) as verification_error:
            build_commit_gate_record(
                plan, plan.stages[0],
                stage_result_dict(validation_ok=False),
            )
        self.assertIn("FAIL-CLOSED", str(verification_error.exception))

        with self.assertRaises(ValueError) as cumulative_error:
            build_commit_gate_record(
                plan, plan.stages[0],
                stage_result_dict(cumulative_ok=False),
            )
        self.assertIn("FAIL-CLOSED", str(cumulative_error.exception))

        # The CONSTRAINED decision (deterministic override applied:
        # ACCEPT becomes REPAIR with deterministic_override=True) is a
        # legal record and flags repair_required.
        payload = stage_result_dict(validation_ok=False)
        payload["status"] = "STAGE_REPAIR"
        payload["supervisor_decision"] = {
            "decision": "REPAIR",
            "rationale": "deterministic checks failed; ACCEPT is not permitted",
            "repair_targets": [],
            "model": "supervisor-role-model",
            "deterministic_override": True,
            "original_decision": "ACCEPT",
        }
        record = build_commit_gate_record(
            plan, plan.stages[0], payload
        )
        self.assertTrue(record.repair_required)
        self.assertEqual(record.supervisor_decision["decision"], "REPAIR")
        self.assertTrue(
            record.supervisor_decision["deterministic_override"]
        )

    def test_plan_level_record_path_and_contents(self):
        """Plan-level record -> commits/plan-gate.json."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg"
            write_evidence_files(run_dir)
            plan = make_plan()

            stage_entry = stage_result_dict()
            plan_payload = {
                "plan_id": "agp-p6-cg",
                "objective": plan.objective,
                "status": "PLAN_ACCEPTED",
                "stage_order": ["s1"],
                "stage_results": {"s1": stage_entry},
                "cumulative_invariants_ok": True,
                "final_acceptance": {
                    "ok": True,
                    "checks": [
                        {
                            "name": "plan final acceptance",
                            "command": "python3 plan-final-acceptance",
                            "expected_exit_code": 0,
                            "exit_code": 0,
                            "ok": True,
                            "output": "all green",
                        }
                    ],
                },
                "cumulative": {
                    "ok": True,
                    "checks": [
                        {
                            "name": "plan-cumulative",
                            "command": "python3 plan-cumulative-suite",
                            "exit_code": 0,
                            "ok": True,
                            "output": "all green",
                        }
                    ],
                },
                "plan_gate": {
                    "decision": "ACCEPT",
                    "rationale": "every gate green",
                    "repair_targets": [],
                    "model": "supervisor-role-model",
                    "deterministic_override": False,
                },
                "repair_lineage": [],
                "failover_events": [],
                "session_ids": [],
                "session_sources": {},
                "stopped_at": None,
                "plan_gate_reviews": [],
                "created": "2026-09-27T00:00:00+00:00",
                "updated": "2026-09-27T00:10:00+00:00",
            }

            record = build_commit_gate_record(
                plan, None, plan_payload, plan_level=True
            )
            path = persist_commit_gate_record(run_dir, record)
            self.assertEqual(path.name, "plan-gate.json")
            self.assertEqual(record.stage_id, "plan")
            self.assertEqual(record.commit_sha, "a" * 40)
            self.assertEqual(record.parent_sha, "b" * 40)
            self.assertFalse(record.repair_required)
            self.assertEqual(
                record.supervisor_decision["decision"], "ACCEPT"
            )
            loaded = load_commit_gate_records(run_dir)
            self.assertEqual(
                [entry.stage_id for entry in loaded], ["plan"]
            )
            for ref in loaded[0].evidence_refs:
                self.assertTrue((run_dir / ref).exists(), ref)

            # Plan-level fail-closed check applies too.
            plan_payload["plan_gate"]["decision"] = "ACCEPT"
            plan_payload["final_acceptance"]["checks"][0]["ok"] = False
            with self.assertRaises(ValueError) as plan_closed:
                build_commit_gate_record(
                    plan, None, plan_payload, plan_level=True
                )
            self.assertIn(
                "FAIL-CLOSED", str(plan_closed.exception)
            )

    def test_duplicate_persist_idempotent_and_conflict(self):
        """Same SHA twice: identical content no-op; conflict raises."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg"
            write_evidence_files(run_dir)
            plan = make_plan()
            record = build_commit_gate_record(
                plan, plan.stages[0], stage_result_dict()
            )
            first = persist_commit_gate_record(run_dir, record)
            second = persist_commit_gate_record(run_dir, record)
            self.assertEqual(first, second)
            self.assertEqual(len(load_commit_gate_records(run_dir)), 1)

            # Different content, SAME sha -> ValueError (a SHA is
            # immutable evidence).
            conflicting = CommitGateRecord.from_dict(record.to_dict())
            conflicting.supervisor_decision = dict(
                conflicting.supervisor_decision,
                rationale="something changed after the fact",
            )
            with self.assertRaises(ValueError) as conflict:
                persist_commit_gate_record(run_dir, conflicting)
            self.assertIn(
                conflicting.commit_sha, str(conflict.exception)
            )
            # The original bytes are untouched.
            on_disk = json.loads(first.read_text("utf-8"))
            self.assertEqual(on_disk, record.to_dict())

    def test_corrupt_file_reload_names_the_file(self):
        """A corrupt record raises ValueError naming the file."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "agp-p6-cg"
            write_evidence_files(run_dir)
            plan = make_plan()
            record = build_commit_gate_record(
                plan, plan.stages[0], stage_result_dict()
            )
            target = persist_commit_gate_record(run_dir, record)
            target.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError) as corrupt:
                load_commit_gate_records(run_dir)
            # The message names the offending file (sha-named record).
            self.assertIn(target.name, str(corrupt.exception))

            # Schema corruption (valid JSON, wrong shape) is caught
            # the same way.
            target.write_text(
                json.dumps({"plan_id": "x"}), encoding="utf-8"
            )
            with self.assertRaises(ValueError) as schema_error:
                load_commit_gate_records(run_dir)
            self.assertIn(target.name, str(schema_error.exception))

    def test_missing_commits_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                load_commit_gate_records(Path(tmp)), []
            )


if __name__ == "__main__":
    unittest.main()