"""
P6 timeline + observability tests — network-free, everything injected
(contract §6, ``tests/test_agy_timeline.py``):

* event validation: unknown ``event_type`` rejected, reserved keys
  and nested-dict fields rejected (NO full-state duplication: every
  line stays under a small bound and never contains the plan text);
* identity fields present on every event, ``ts`` auto-stamped and
  monotonic (non-decreasing);
* ``load()`` parse error -> ``ValueError`` naming the LINE NUMBER;
* ``observability_summary`` with an injected fake ``session_usage``:
  real tokens pass through, an empty/failing fake yields the literal
  ``"unknown"`` — and NO fabricated 0-cost anywhere;
* ``RUN_INTERRUPTED`` + ``RUN_RESUMED`` pair parses back in order.

No network (the default server-backed usage reader is never used),
no subprocess, no model, no sleep.
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.orchestration.agy_timeline import (
    EVENT_TYPES,
    Timeline,
    observability_summary,
    save_observability_summary,
    write_observability,
)

PLAN_ID = "agp-p6-timeline"


def write_artifacts(run_dir: Path) -> None:
    """Minimal real persisted artifacts (plan + one accepted stage)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan_doc.json").write_text(
        json.dumps(
            {
                "plan_id": PLAN_ID,
                "objective": "the plan objective MARKER_TEXT_42",
                "stages": [
                    {
                        "stage_id": "s1",
                        "objective": "first slice",
                        "dependencies": [],
                        "allowed_files": ["calc.py"],
                        "implementation_context": "context",
                        "acceptance_commands": [
                            {
                                "command": "python3 stage-gate s1",
                                "expected_exit_code": 0,
                            }
                        ],
                        "acceptance_conditions": ["s1 ok"],
                        "cumulative_invariants": [],
                    }
                ],
                "final_acceptance": [
                    {
                        "command": "python3 plan-final-acceptance",
                        "expected_exit_code": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "plan.json").write_text(
        json.dumps(
            {
                "plan_id": PLAN_ID,
                "objective": "the plan objective MARKER_TEXT_42",
                "status": "PLAN_ACCEPTED",
                "stage_order": ["s1"],
                "created": "2026-09-27T10:00:00+00:00",
                "updated": "2026-09-27T10:20:00+00:00",
                "session_ids": ["ses_real", "ses_absent"],
                "session_sources": {"s1-t00": "readback"},
                "failover_events": [],
                "repair_lineage": [],
                "cumulative_invariants_ok": True,
            }
        ),
        encoding="utf-8",
    )
    stage_dir = run_dir / "s1"
    (stage_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (stage_dir / "stage.json").write_text(
        json.dumps(
            {
                "plan_id": PLAN_ID,
                "stage_id": "s1",
                "status": "STAGE_ACCEPTED",
                "task_results": [
                    {
                        "worker_task_id": "s1-t00",
                        "task_id": "s1-t00",
                        "attempt": 2,
                        "status": "SUCCESS",
                        "validation_ok": True,
                    }
                ],
                "commit": {"commit_sha": "f" * 40},
                "supervisor_decision": {
                    "decision": "ACCEPT",
                    "rationale": "green",
                    "model": "supervisor-role-model",
                    "deterministic_override": False,
                    "repair_targets": [],
                },
                "repair_lineage": [],
                "failover_events": [],
                "session_ids": ["ses_real"],
                "changed_files": ["calc.py"],
                "scope_violations": [],
                "stage_acceptance": {"ok": True, "checks": []},
                "cumulative_gate": {"ok": True, "checks": []},
                "task_verification": {},
                "failure_reason": "",
                "replan": None,
            }
        ),
        encoding="utf-8",
    )
    (stage_dir / "commit.json").write_text(
        json.dumps({"commit_sha": "f" * 40}), encoding="utf-8"
    )
    (stage_dir / "tasks" / "s1-t00.json").write_text(
        json.dumps(
            {
                "task_id": "s1-t00",
                "worker_task_id": "s1-t00",
                "attempt": 2,
                "status": "SUCCESS",
                "validation_ok": True,
                "validation_output": "ok",
                "changed_files": ["calc.py"],
                "session_id": "ses_real",
                "session_source": "readback",
                "failure_reason": "",
            }
        ),
        encoding="utf-8",
    )


class TestEventValidation(unittest.TestCase):
    def test_unknown_event_type_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Timeline(Path(tmp) / "timeline.jsonl")
            with self.assertRaises(ValueError) as error:
                timeline.emit("STAGE_EXODE", plan_id=PLAN_ID)
            self.assertIn(
                "unknown timeline event_type", str(error.exception)
            )
            self.assertIn("STAGE_EXODE", str(error.exception))
            self.assertFalse(timeline.path.exists())

    def test_allowed_event_set_matches_contract(self):
        expected = {
            "PLAN_STARTED",
            "STAGE_STARTED",
            "TASK_DISPATCHED",
            "TASK_COMPLETED",
            "TASK_VERIFIED",
            "STAGE_COMMITTED",
            "STAGE_ACCEPTED",
            "PLAN_REPAIR_REQUESTED",
            "MODEL_FAILOVER",
            "RUN_INTERRUPTED",
            "RUN_RESUMED",
            "PLAN_ACCEPTED",
            "PLAN_REJECTED",
            "PLAN_ALREADY_ACCEPTED",
            "SESSION_CONTINUATION",
        }
        self.assertTrue(expected.issubset(EVENT_TYPES))

    def test_identity_fields_present_and_ts_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Timeline(Path(tmp) / "timeline.jsonl")
            timeline.emit("PLAN_STARTED", plan_id=PLAN_ID)
            timeline.emit(
                "TASK_DISPATCHED",
                plan_id=PLAN_ID,
                stage_id="s1",
                task_id="s1-t00",
                attempt=1,
            )
            timeline.emit(
                "STAGE_ACCEPTED",
                plan_id=PLAN_ID,
                stage_id="s1",
                status="STAGE_ACCEPTED",
                sha="a" * 40,
            )
            events = timeline.load()
            self.assertEqual(len(events), 3)
            for event in events:
                self.assertEqual(event["plan_id"], PLAN_ID)
                self.assertIn("event_type", event)
                self.assertIn("ts", event)
                datetime.fromisoformat(event["ts"])  # parses
            self.assertEqual(events[1]["stage_id"], "s1")
            self.assertEqual(events[1]["task_id"], "s1-t00")
            self.assertEqual(events[1]["attempt"], 1)
            self.assertEqual(events[2]["sha"], "a" * 40)
            stamps = [event["ts"] for event in events]
            self.assertEqual(stamps, sorted(stamps), stamps)

    def test_reserved_keys_and_nested_state_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Timeline(Path(tmp) / "timeline.jsonl")
            with self.assertRaises(ValueError):
                timeline.emit(
                    "PLAN_STARTED", plan_id=PLAN_ID, ts="forged"
                )
            with self.assertRaises(ValueError) as nested:
                timeline.emit(
                    "STAGE_STARTED",
                    plan_id=PLAN_ID,
                    stage_payload={"full": "stage document"},
                )
            self.assertIn("never", str(nested.exception))
            self.assertFalse(timeline.path.exists())

    def test_no_full_state_duplication_small_lines(self):
        """No event line carries the objective/full state (bound)."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_artifacts(run_dir)
            timeline = Timeline(run_dir / "timeline.jsonl")
            timeline.emit(
                "PLAN_STARTED",
                plan_id=PLAN_ID,
                status="PLAN_IN_PROGRESS",
            )
            timeline.emit(
                "STAGE_STARTED", plan_id=PLAN_ID, stage_id="s1"
            )
            timeline.emit(
                "TASK_DISPATCHED",
                plan_id=PLAN_ID,
                stage_id="s1",
                task_id="s1-t00",
                attempt=1,
            )
            timeline.emit(
                "STAGE_ACCEPTED",
                plan_id=PLAN_ID,
                stage_id="s1",
                status="STAGE_ACCEPTED",
                sha="f" * 40,
            )
            raw_lines = timeline.path.read_text("utf-8").splitlines()
            self.assertTrue(raw_lines)
            for line in raw_lines:
                self.assertLess(len(line), 512, line)
                self.assertNotIn("MARKER_TEXT_42", line)
                self.assertNotIn("stage document", line)

    def test_load_parse_error_names_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeline.jsonl"
            good = json.dumps(
                {"event_type": "PLAN_STARTED", "plan_id": PLAN_ID}
            )
            path.write_text(
                good + "\n" + '{"event_type": "STAGE_STA\n',
                encoding="utf-8",
            )
            timeline = Timeline(path)
            with self.assertRaises(ValueError) as error:
                timeline.load()
            self.assertIn("line 2", str(error.exception))
            # A missing file loads as an empty stream.
            self.assertEqual(
                Timeline(Path(tmp) / "absent.jsonl").load(), []
            )

    def test_run_interrupted_run_resumed_pair_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            timeline = Timeline(Path(tmp) / "timeline.jsonl")
            timeline.emit(
                "RUN_INTERRUPTED",
                plan_id=PLAN_ID,
                stage_id="bulk-import",
                status="STAGE_ACCEPTED",
            )
            interrupted_ts = timeline.load()[-1]["ts"]
            timeline.emit(
                "RUN_RESUMED",
                plan_id=PLAN_ID,
                stage_id="reporting-surface",
                status="PLAN_RUNNING",
            )
            events = timeline.load()
            kinds = [event["event_type"] for event in events]
            self.assertEqual(
                kinds, ["RUN_INTERRUPTED", "RUN_RESUMED"]
            )
            self.assertLessEqual(interrupted_ts, events[1]["ts"])
            self.assertEqual(events[0]["stage_id"], "bulk-import")
            self.assertEqual(events[1]["plan_id"], PLAN_ID)


FAKE_USAGE_REAL = {
    "ses_real": {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cost": 0.0042,
    },
    "ses_absent": None,
}


def fake_usage_returning(mapping):
    def fake(session_ids):
        return {
            sid: mapping.get(sid)
            for sid in session_ids
        }

    return fake


class TestObservabilitySummary(unittest.TestCase):
    def emit_full_stream(self, timeline: Timeline) -> None:
        timeline.emit("PLAN_STARTED", plan_id=PLAN_ID)
        timeline.emit(
            "RUN_INTERRUPTED",
            plan_id=PLAN_ID,
            stage_id="s1",
            status="STAGE_ACCEPTED",
        )
        timeline.emit("RUN_RESUMED", plan_id=PLAN_ID)
        timeline.emit("STAGE_STARTED", plan_id=PLAN_ID, stage_id="s1")
        timeline.emit(
            "TASK_DISPATCHED",
            plan_id=PLAN_ID,
            stage_id="s1",
            task_id="s1-t00",
            attempt=1,
        )
        timeline.emit(
            "TASK_COMPLETED",
            plan_id=PLAN_ID,
            stage_id="s1",
            task_id="s1-t00",
        )
        timeline.emit(
            "TASK_VERIFIED",
            plan_id=PLAN_ID,
            stage_id="s1",
            task_id="s1-t00",
            status="ok",
        )
        timeline.emit(
            "STAGE_ACCEPTED",
            plan_id=PLAN_ID,
            stage_id="s1",
            status="STAGE_ACCEPTED",
            sha="f" * 40,
        )
        timeline.emit(
            "PLAN_ACCEPTED",
            plan_id=PLAN_ID,
            status="PLAN_ACCEPTED",
            sha="f" * 40,
        )

    def collect(self, tmp: str, session_usage):
        run_dir = Path(tmp) / "run"
        write_artifacts(run_dir)
        timeline = Timeline(run_dir / "timeline.jsonl")
        self.emit_full_stream(timeline)
        summary = observability_summary(
            run_dir, timeline, session_usage=session_usage
        )
        return run_dir, summary

    def test_real_tokens_pass_through_and_plan_sections_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_dir, summary = self.collect(
                tmp, fake_usage_returning(FAKE_USAGE_REAL)
            )
            plan_section = summary["plan"]
            self.assertEqual(plan_section["status"], "PLAN_ACCEPTED")
            self.assertEqual(plan_section["stages_total"], 1)
            self.assertEqual(plan_section["accepted"], 1)
            self.assertIsInstance(plan_section["elapsed_s"], float)
            # First PLAN_STARTED .. terminal PLAN_ACCEPTED span.
            self.assertEqual(
                plan_section["start"],
                json.loads(
                    Path(_run_dir, "timeline.jsonl").read_text("utf-8")
                    .splitlines()[0]
                )["ts"],
            )
            self.assertGreater(plan_section["elapsed_s"], -1)

            stage = summary["stages"]["s1"]
            self.assertEqual(stage["commit_sha"], "f" * 40)
            self.assertEqual(
                stage["supervisor_decision"]["decision"], "ACCEPT"
            )
            self.assertIsInstance(stage["elapsed_s"], float)
            self.assertEqual(stage["attempts"], 1)

            task = summary["tasks"]["s1-t00"]
            self.assertEqual(task["model_role"], "dispatch")
            self.assertEqual(task["session_id"], "ses_real")
            self.assertEqual(task["session_source"], "readback")
            self.assertEqual(task["verification_command"],
                             "python3 stage-gate s1")
            self.assertEqual(task["verification_result"], "ok")
            self.assertEqual(task["changed_files"], ["calc.py"])
            self.assertNotIn("unknown", (task["start"], task["end"]))

            runtime = summary["runtime"]
            self.assertEqual(runtime["failover_count"], 0)
            usage = runtime["model_usage"]
            self.assertEqual(
                usage["ses_real"],
                {
                    "input_tokens": 1200,
                    "output_tokens": 340,
                    "cost": 0.0042,
                },
            )
            # Absent session -> unknown, never 0.
            self.assertEqual(usage["ses_absent"], {
                "input_tokens": "unknown",
                "output_tokens": "unknown",
                "cost": "unknown",
            })

    def test_empty_usage_is_unknown_never_fabricated_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_dir, summary = self.collect(
                tmp, lambda session_ids: {}
            )
            usage = summary["runtime"]["model_usage"]
            self.assertTrue(usage)
            for session_id, entry in usage.items():
                for key, value in entry.items():
                    self.assertEqual(
                        value, "unknown",
                        f"{session_id}.{key} = {value!r}",
                    )
                    self.assertNotEqual(value, 0)

            # A raising usage callable also degrades to unknown.
            def boom(session_ids):
                raise RuntimeError("server unreachable")

            with tempfile.TemporaryDirectory() as tmp2:
                _run_dir2, summary2 = self.collect(tmp2, boom)
                for entry in summary2["runtime"][
                    "model_usage"
                ].values():
                    self.assertTrue(
                        all(
                            value == "unknown"
                            for value in entry.values()
                        )
                    )

            # Zero/absent cost is "unknown": never a fabricated 0.
            zero_cost = {
                "ses_real": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost": 0,
                },
            }
            with tempfile.TemporaryDirectory() as tmp3:
                _run_dir3, summary3 = self.collect(
                    tmp3, fake_usage_returning(zero_cost)
                )
                entry = summary3["runtime"]["model_usage"]["ses_real"]
                self.assertEqual(entry["input_tokens"], 10)
                self.assertEqual(entry["output_tokens"], 5)
                self.assertEqual(entry["cost"], "unknown")
                flat = json.dumps(summary3)
                self.assertNotIn('"cost": 0', flat)

    def test_unknown_for_unavailable_stage_metrics(self):
        """Stage with no timeline pair/task files -> 'unknown'/'{}'."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_artifacts(run_dir)
            import shutil

            # A gate stage crashed before its verdict fields landed:
            # stage.json exists (so the stage is visible) but carries
            # no commit/decision and the task files are gone.
            shutil.rmtree(run_dir / "s1" / "tasks")
            (run_dir / "s1" / "commit.json").unlink()
            (run_dir / "s1" / "stage.json").write_text(
                json.dumps(
                    {"plan_id": PLAN_ID, "stage_id": "s1",
                     "task_results": [], "repair_lineage": []}
                ),
                encoding="utf-8",
            )
            timeline = Timeline(run_dir / "timeline.jsonl")
            summary = observability_summary(
                run_dir,
                timeline,
                session_usage=lambda session_ids: {},
            )
            stage = summary["stages"]["s1"]
            self.assertEqual(stage["elapsed_s"], "unknown")
            self.assertEqual(stage["attempts"], "unknown")
            self.assertEqual(stage["commit_sha"], "unknown")
            self.assertEqual(stage["supervisor_decision"], "unknown")
            # No task files -> tasks section empty, but the plan
            # status still comes from the real plan.json.
            self.assertEqual(summary["tasks"], {})
            self.assertEqual(
                summary["plan"]["status"], "PLAN_ACCEPTED"
            )

    def test_write_observability_persists_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_artifacts(run_dir)
            timeline = Timeline(run_dir / "timeline.jsonl")
            self.emit_full_stream(timeline)
            path = write_observability(run_dir, timeline)
            self.assertEqual(path.name, "observability.json")
            on_disk = json.loads(path.read_text("utf-8"))
            self.assertEqual(
                on_disk["plan"]["status"], "PLAN_ACCEPTED"
            )
            self.assertEqual(
                set(on_disk), {"plan", "stages", "tasks", "runtime"}
            )
            # save_observability_summary rewrites atomically.
            save_observability_summary(run_dir, on_disk)
            again = json.loads(path.read_text("utf-8"))
            self.assertEqual(on_disk, again)


if __name__ == "__main__":
    unittest.main()