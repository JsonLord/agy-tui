"""AGY P7c — SPARK INCIDENT INTERPRETER tests (7-case contract matrix).

 1 Spark NOT called during normal execution
 2 Spark receives only a BOUNDED incident packet (size + field allowlist)
 3 strict structured response validation (all fields, enums, ranges, types)
 4 malformed response fails safely (status=INVALID, NO state change)
 5 timeout fails safely (status=TIMEOUT, NO state change)
 6 Spark CANNOT directly mutate job state (no write handle: architectural +
   behavioral assertions)
 7 supervisor_needed drives escalation ONLY through the wake policy

Network-free: local injected transports + mock verdicts. (Live no-token probe
of https://leon4gr45-llama.hf.space/ — GET / -> 200, GET /v1/models and POST
/v1/chat/completions -> 401 {"detail":"Invalid API key"} — was recorded in
scratch/spark_contract.md; the live P7 run uses the PENDING_CREDENTIAL path
because no SPARK_TOKEN_FILE was supplied.)
"""

import ast
import json
import tempfile
import time
import unittest
from pathlib import Path

from app.orchestration.sentinel.api import SentinelRuntime
from app.orchestration.sentinel.process_state import JobState
from app.orchestration.sentinel.spark_interpreter import (
    PACKET_KEYS,
    SparkClient,
    SparkVerdict,
    build_incident_packet,
    validate_response,
)
from app.orchestration.sentinel.wake_bridge import WakePolicy

from _p7_helpers import (
    RecordingLaya,
    RecordingSpark,
    WakeRecorder,
    counting_gate,
    events_of,
    fast_policy,
    safe_cleanup,
    ticks_until,
    wait_terminal,
    worker_exit,
    worker_silence_then_print,
    worker_sleep,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class P7cSparkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-p7c-")
        self.root = Path(self._tmp.name)
        self._spawned = []

    def tearDown(self):
        for rt, job_id in self._spawned:
            try:
                safe_cleanup(rt, job_id)
            except Exception:
                pass
        time.sleep(0.2)
        self._tmp.cleanup()

    def spawn(self, rt, command, **kw):
        job_id = rt.spawn_monitored_process(command, cwd=str(self.root), **kw)
        self._spawned.append((rt, job_id))
        return job_id

    def stall_until_event(self, rt, job_id, event_type, timeout=3.0):
        """Tick until the named event exists in the job's event stream."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rt.tick(job_id)
            if events_of(rt, job_id, event_type):
                return events_of(rt, job_id, event_type)
            time.sleep(0.03)
        return events_of(rt, job_id, event_type)

    # 1 ------------------------------------------------------------------------
    def test_01_spark_not_called_during_normal_execution(self):
        spark = RecordingSpark(action="WAIT")
        laya = RecordingLaya()
        policy = fast_policy(quiet_after_s=999, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "s", policy,
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark)
        job_id = self.spawn(rt, worker_exit(0, sleep=0.4))
        rec = wait_terminal(rt, job_id, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assertEqual(len(spark.packets), 0)
        self.assertEqual(laya.calls, 0)
        self.assertEqual(rt.metrics.counters["sentinel_spark_calls_total"], 0)
        self.assertEqual(events_of(rt, job_id, "SPARK_INTERPRETED"), [])

    # 2 ------------------------------------------------------------------------
    def test_02_incident_packet_is_bounded(self):
        big_lines = [f"line {i} " + "x" * 1000 for i in range(500)]
        big_transitions = [{"ts": i, "event_type": "X", "state": "S",
                            "evidence": {"blob": "y" * 500}} for i in range(50)]
        big_ckpts = [f"checkpoint {i} " + "z" * 300 for i in range(9)]
        packet = build_incident_packet(
            job_id="proc_bounded00001", state=JobState.STALL_SUSPECTED,
            metadata={"command_hash": "a" * 64, "cwd": "/srv/" + "w" * 500},
            evidence={"elapsed_s": 61.7, "quiet_s": 40.0, "cpu_pct": 0.0,
                      "deadline_remaining_s": 100.0, "checkpoint": "ckpt " * 400},
            laya={"status": "ok", "classification": "STALLED"},
            tail_lines=big_lines, recent_checkpoints=big_ckpts,
            transitions=big_transitions, previous_incident=None,
            metrics={"observations": 500})
        raw = json.dumps(packet, separators=(",", ":"))
        self.assertLessEqual(len(raw.encode()), 16384)
        self.assertEqual(set(packet.keys()), set(PACKET_KEYS))
        self.assertLessEqual(len(packet["tail_lines"]), 100)
        for line in packet["tail_lines"]:
            self.assertLessEqual(len(line), 400)
        self.assertLessEqual(len(packet["transitions"]), 10)
        self.assertLessEqual(len(packet["recent_checkpoints"]), 5)
        self.assertLessEqual(len(packet["cwd"]), 200)
        self.assertNotIn("history", packet)
        self.assertNotIn("full_log", packet)

    # 3 ------------------------------------------------------------------------
    def test_03_strict_structured_response_validation(self):
        good = {"likely_state": "quiet_but_alive", "confidence": 0.7,
                "recommended_action": "WAIT", "wait_seconds": 30,
                "reason": "long compile phase", "supervisor_needed": False}
        verdict = validate_response(good)
        self.assertEqual(verdict.status, "ok")
        self.assertEqual(verdict.recommended_action, "WAIT")
        self.assertEqual(verdict.wait_seconds, 30)
        self.assertFalse(verdict.supervisor_needed)

        for label, mutated in [
            ("missing field", {k: v for k, v in good.items() if k != "reason"}),
            ("confidence>1", dict(good, confidence=1.5)),
            ("confidence<0", dict(good, confidence=-0.1)),
            ("conf bool", dict(good, confidence=True)),
            ("bad action", dict(good, recommended_action="KILL")),
            ("wait str", dict(good, wait_seconds="30")),
            ("wait bool", dict(good, wait_seconds=True)),
            ("empty reason", dict(good, reason="  ")),
            ("supervisor int", dict(good, supervisor_needed=1)),
            ("empty state", dict(good, likely_state="")),
            ("not an object", ["nope"]),
        ]:
            with self.assertRaises(ValueError, msg=label):
                validate_response(mutated)

    # 4 ------------------------------------------------------------------------
    def test_04_malformed_response_fails_safely_no_state_change(self):
        # client level: 200 with non-JSON content -> INVALID, never raises
        envelope = json.dumps(
            {"choices": [{"message": {"content": "I think it is stuck?"}}]})
        client = SparkClient(
            api_key="SPARK_[REDACTED]", timeout_s=5,
            transport=lambda p, h, t: (200, envelope.encode()))
        verdict = client.interpret({"job_id": "x"})
        self.assertEqual(verdict.status, "invalid")
        self.assertEqual(verdict.failure, "malformed_response")
        self.assertFalse(verdict.escalates)

        # schema-invalid JSON (valid JSON, wrong fields) -> INVALID
        bad = json.dumps({"choices": [{"message": {"content": json.dumps(
            {"likely_state": "x", "confidence": 0.5, "recommended_action": "MELT",
             "wait_seconds": None, "reason": "r", "supervisor_needed": False})}}]})
        client2 = SparkClient(
            api_key="SPARK_[REDACTED]", timeout_s=5,
            transport=lambda p, h, t: (200, bad.encode()))
        v2 = client2.interpret({"job_id": "x"})
        self.assertEqual(v2.status, "invalid")
        self.assertIn("recommended_action", v2.failure or "")

        # engine level: interpreting a stall changes NO state
        spark = RecordingSpark(status="invalid", action="WAIT")
        laya = RecordingLaya(interpret=0.99)
        rt = SentinelRuntime(self.root / "s", fast_policy(),
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark)
        job_id = self.spawn(rt, worker_sleep(3.0))
        events = self.stall_until_event(rt, job_id, "SPARK_INTERPRETED")
        self.assertTrue(events, "spark never interpreted the stall")
        self.assertEqual(events[0]["evidence"]["verdict"]["status"], "invalid")
        rec = rt.engine.get(job_id)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)  # NO state change
        self.assertFalse(rec.cancel_requested)
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 0)

    # 5 ------------------------------------------------------------------------
    def test_05_timeout_fails_safely(self):
        def transport(payload, headers, timeout):
            raise TimeoutError("read timed out")

        client = SparkClient(api_key="SPARK_[REDACTED]", transport=transport)
        verdict = client.interpret({"job_id": "x"})
        self.assertEqual(verdict.status, "timeout")
        self.assertFalse(verdict.escalates)
        self.assertIsNone(verdict.recommended_action)

        # engine-level: a timing-out interpreter leaves the job ticking
        class TimeoutSpark:
            def __init__(self):
                self.packets = []

            def interpret(self, packet):
                self.packets.append(packet)
                return SparkVerdict(status="timeout", failure="timeout",
                                    latency_s=60.0)

        spark = TimeoutSpark()
        laya = RecordingLaya(interpret=0.99)
        rt = SentinelRuntime(self.root / "s", fast_policy(),
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark)
        job_id = self.spawn(rt, worker_sleep(3.0))
        self.stall_until_event(rt, job_id, "SPARK_INTERPRETED")
        rec = rt.engine.get(job_id)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        still_ticking = rt.tick(job_id)
        self.assertIsNotNone(still_ticking.observation)
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 0)

    # 6 ------------------------------------------------------------------------
    def test_06_spark_has_no_write_handle(self):
        # architectural: module imports ONLY stdlib, no sentinel internals,
        # no store/engine/api constructs anywhere in its source
        source = (REPO_ROOT / "app" / "orchestration" / "sentinel" /
                  "spark_interpreter.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(".", alias.name.split(".")[0],
                                     f"package-internal import: {alias.name}")
                    self.assertFalse(alias.name.startswith("app"),
                                     f"app import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative/package-internal import")
                self.assertNotEqual(node.module or "", "")
                self.assertFalse((node.module or "").startswith("app"))
        for forbidden in ("JobStore", "cancel_process", "spawn_monitored_process",
                          "killpg", "SIGKILL", "os.kill", ".state =", "state_machine"):
            self.assertNotIn(forbidden, source, f"write handle leaked: {forbidden}")

        # behavioral: CANCEL recommendation cannot cancel; no writes, no state
        spark = RecordingSpark(action="CANCEL_RECOMMENDED",
                               supervisor_needed=False)
        laya = RecordingLaya(interpret=0.99)
        rt = SentinelRuntime(self.root / "s", fast_policy(),
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark,
                             wake_policy=WakePolicy(enabled_reasons=frozenset()))
        job_id = self.spawn(rt, worker_sleep(3.0))
        self.stall_until_event(rt, job_id, "SPARK_INTERPRETED")
        rec = rt.engine.get(job_id)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        self.assertFalse(rec.cancel_requested)
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 0)
        self.assertNotIn("PROCESS_CANCELLED", events_of(rt, job_id,
                                                        "PROCESS_CANCELLED"))
        self.assertTrue(os_process_alive(rec.pid))

    # 7 ------------------------------------------------------------------------
    def test_07_supervisor_needed_escalates_only_through_policy(self):
        # (a) with the wake bridge enabled: exactly one escalation
        recorder = WakeRecorder()
        spark_a = RecordingSpark(action="RECHECK", supervisor_needed=True)
        laya_a = RecordingLaya(interpret=0.99)
        rt_a = SentinelRuntime(self.root / "wa", fast_policy(),
                               laya_gate=counting_gate(laya_a),
                               spark_interpreter=spark_a, waker=recorder)
        job_a = self.spawn(rt_a, worker_sleep(3.0))
        events_a = self.stall_until_event(rt_a, job_a, "SUPERVISOR_WAKE_REQUESTED")
        self.assertEqual(len(events_a), 1)
        self.assertEqual(events_a[0]["evidence"]["reason"], "spark_intervention")
        self.assertEqual(len(recorder.packets), 1)
        self.assertEqual(rt_a.metrics.counters["sentinel_supervisor_wakeups_total"], 1)
        # queue file holds the packet too (delivery seam + queue = proof)
        queue = (self.root / "wa" / "wake_queue.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(queue), 1)
        self.assertEqual(json.loads(queue[0])["packet"]["reason"], "spark_intervention")

        # (b) with the wake bridge policy blocking spark: zero escalation
        spark_b = RecordingSpark(action="ESCALATE", supervisor_needed=True)
        laya_b = RecordingLaya(interpret=0.99)
        rt_b = SentinelRuntime(self.root / "wb", fast_policy(),
                               laya_gate=counting_gate(laya_b),
                               spark_interpreter=spark_b,
                               wake_policy=WakePolicy(enabled_reasons=frozenset()))
        job_b = self.spawn(rt_b, worker_sleep(3.0))
        self.stall_until_event(rt_b, job_b, "SPARK_INTERPRETED")
        for _ in range(10):
            rt_b.tick(job_b)
            time.sleep(0.03)
        self.assertEqual(rt_b.metrics.counters["sentinel_supervisor_wakeups_total"], 0)
        self.assertEqual(events_of(rt_b, job_b, "SUPERVISOR_WAKE_REQUESTED"), [])
        # job still alive & unstopped: Spark advises, policy decides
        self.assertEqual(rt_b.engine.get(job_b).state, JobState.STALL_SUSPECTED)


def os_process_alive(pid) -> bool:
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError, TypeError, ValueError):
        return False


if __name__ == "__main__":
    unittest.main()