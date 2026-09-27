"""AGY P7d — SUPERVISOR WAKE BRIDGE tests (8-case contract matrix).

 1 normal running -> ZERO wakeups
 2 success -> ZERO wakeups
 3 ambiguous wait resolved by Laya -> no wakeup
 4 Spark recommends WAIT -> no wakeup
 5 Spark requests intervention -> exactly ONE wakeup (event + queue + seam)
 6 repeated stall -> BOUNDED wakeups (1 immediate + 1 after grace, then
   ESCALATED stops auto-waking)
 7 no duplicate wakeup spam (dedupe on (job, reason, episode[, slot]))
 8 wake packet bounded (size cap + field allowlist)

Network-free, real local processes, compressed intervals.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.orchestration.sentinel.api import SentinelRuntime
from app.orchestration.sentinel.process_state import JobRecord, JobState
from app.orchestration.sentinel.wake_bridge import (
    WAKE_PACKET_FIELDS,
    WakeBridge,
    WakePolicy,
)

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
    worker_stall_then_exit,
)


class P7dWakeBridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-p7d-")
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

    def assert_zero_wakes(self, rt, job_id, recorder=None):
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 0)
        self.assertEqual(events_of(rt, job_id, "SUPERVISOR_WAKE_REQUESTED"), [])
        if recorder is not None:
            self.assertEqual(len(recorder.packets), 0)
        queue = rt.bridge.queue_path
        self.assertTrue(not queue.exists() or queue.read_text().strip() == "")

    # 1 ------------------------------------------------------------------------
    def test_01_normal_running_zero_wakeups(self):
        recorder = WakeRecorder()
        policy = fast_policy(quiet_after_s=999, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "a", policy, WakePolicy(), waker=recorder)
        job = self.spawn(rt, worker_exit(0, sleep=0.6))
        # keep a job observed as normal RUNNING for a while
        ticks_until(rt, job, lambda r: r.state == JobState.RUNNING, timeout=3.0)
        for _ in range(10):
            rt.tick(job)
            time.sleep(0.03)
        self.assertNotIn(rt.engine.get(job).state,
                         (JobState.STALL_SUSPECTED, JobState.DEADLINE_REACHED))
        rec = wait_terminal(rt, job, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assert_zero_wakes(rt, job, recorder)

    # 2 ------------------------------------------------------------------------
    def test_02_success_produces_zero_wakeups(self):
        recorder = WakeRecorder()
        policy = fast_policy(quiet_after_s=999, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "b", policy, WakePolicy(), waker=recorder)
        job = self.spawn(rt, worker_exit(0, sleep=0.2))
        rec = wait_terminal(rt, job, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assert_zero_wakes(rt, job, recorder)

    # 3 ------------------------------------------------------------------------
    def test_03_laya_resolved_ambiguity_no_wakeup(self):
        recorder = WakeRecorder()
        laya = RecordingLaya(choice="WAIT", interpret=0.1)
        rt = SentinelRuntime(self.root / "c", fast_policy(),
                             WakePolicy(min_stall_episodes=99),
                             laya_gate=counting_gate(laya), waker=recorder)
        job = self.spawn(rt, worker_silence_then_print(0.7))
        rec = ticks_until(rt, job,
                          lambda r: r.state == JobState.STALL_SUSPECTED,
                          timeout=3.0, interval=0.03)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        self.assertGreaterEqual(laya.calls, 1)  # gate WAS consulted …
        rec = wait_terminal(rt, job, timeout=5.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assert_zero_wakes(rt, job, recorder)  # … and resolved locally

    # 4 ------------------------------------------------------------------------
    def test_04_spark_wait_recommendation_no_wakeup(self):
        recorder = WakeRecorder()
        laya = RecordingLaya(interpret=0.99)
        spark = RecordingSpark(action="WAIT", wait_seconds=10)
        rt = SentinelRuntime(self.root / "d", fast_policy(), WakePolicy(),
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark, waker=recorder)
        job = self.spawn(rt, worker_silence_then_print(0.7))
        events = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not events:
            rt.tick(job)
            events = events_of(rt, job, "SPARK_INTERPRETED")
            time.sleep(0.03)
        self.assertTrue(events, "spark never interpreted the stall")
        self.assertEqual(events[0]["evidence"]["verdict"]["recommended_action"], "WAIT")
        self.assertEqual(len(spark.packets), 1)
        rec = wait_terminal(rt, job, timeout=5.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assert_zero_wakes(rt, job, recorder)

    # 5 ------------------------------------------------------------------------
    def test_05_spark_intervention_exactly_one_wakeup(self):
        recorder = WakeRecorder()
        laya = RecordingLaya(interpret=0.99)
        spark = RecordingSpark(action="ESCALATE", supervisor_needed=False)
        rt = SentinelRuntime(self.root / "e", fast_policy(), WakePolicy(),
                             laya_gate=counting_gate(laya),
                             spark_interpreter=spark, waker=recorder)
        job = self.spawn(rt, worker_sleep(3.0))
        # wait for the wake itself
        deadline = time.monotonic() + 3.0
        wakes = []
        while time.monotonic() < deadline and not wakes:
            rt.tick(job)
            wakes = events_of(rt, job, "SUPERVISOR_WAKE_REQUESTED")
            time.sleep(0.03)
        self.assertEqual(len(wakes), 1)
        self.assertEqual(len(recorder.packets), 1)
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 1)
        queue = (self.root / "e" / "wake_queue.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(queue), 1)
        packet = json.loads(queue[0])["packet"]
        self.assertEqual(packet["job_id"], job)
        self.assertEqual(packet["reason"], "spark_intervention")
        # bounded evidence pointers, never full logs
        self.assertIn("event_log", packet["pointers"])
        self.assertLessEqual(len(json.dumps(packet).encode()), 8192)
        # keep ticking: episode budget + dedupe keep it at exactly 1
        for _ in range(15):
            rt.tick(job)
            time.sleep(0.03)
        self.assertEqual(len(events_of(rt, job, "SUPERVISOR_WAKE_REQUESTED")), 1)
        self.assertEqual(len(recorder.packets), 1)

    # 6 ------------------------------------------------------------------------
    def test_06_repeated_stall_wakeups_bounded(self):
        recorder = WakeRecorder()
        rt = SentinelRuntime(self.root / "f", fast_policy(),
                             WakePolicy(min_stall_episodes=2, wake_grace_s=0.0),
                             waker=recorder)
        job = self.spawn(rt, worker_stall_then_exit(prints=5, gap=0.35))
        rec = wait_terminal(rt, job, timeout=8.0, interval=0.03)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        # bounded: 1 immediate + 1 after grace (grace 0 => next tick), then
        # the policy marks the job ESCALATED and stops auto-waking entirely
        self.assertEqual(rt.metrics.counters["sentinel_supervisor_wakeups_total"], 2)
        self.assertEqual(len(recorder.packets), 2)
        self.assertTrue(rec.escalated)
        self.assertEqual(rec.stall_wakes, 2)
        self.assertGreaterEqual(rec.stall_episodes, 2)
        queue = (self.root / "f" / "wake_queue.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(queue), 2)
        wakes = events_of(rt, job, "SUPERVISOR_WAKE_REQUESTED")
        self.assertEqual(len(wakes), 2)
        self.assertEqual({w["evidence"]["reason"] for w in wakes}, {"repeated_stall"})

    # 7 ------------------------------------------------------------------------
    def test_07_no_duplicate_wakeup_spam_bridge_level(self):
        from _p7_helpers import FakeClock
        clock = FakeClock()
        bridge = WakeBridge(self.root / "g", WakePolicy(), clock=clock)
        rec = JobRecord(job_id="proc_dedupe00001", argv=["x"], cwd="/tmp",
                        command_hash="f" * 64, started_at=clock())
        fat_evidence = {f"k{i}": f"v{i}" for i in range(60)}
        first = bridge.request(rec, reason="repeated_stall", episode=1,
                               evidence=fat_evidence)
        self.assertIsNotNone(first)
        dup = bridge.request(rec, reason="repeated_stall", episode=1,
                             evidence=fat_evidence)
        self.assertIsNone(dup, "duplicate (job, reason, episode) not deduped")
        # same reason, different episode: allowed (distinct episode)
        second_ep = bridge.request(rec, reason="repeated_stall", episode=2,
                                   evidence=fat_evidence)
        self.assertIsNotNone(second_ep)
        # bounded slot-1 wake (the after-grace second wake) is a distinct key
        slot1 = bridge.request(rec, reason="repeated_stall", episode=1,
                               evidence=fat_evidence, slot=1)
        self.assertIsNotNone(slot1)
        dup_slot1 = bridge.request(rec, reason="repeated_stall", episode=1,
                                   evidence=fat_evidence, slot=1)
        self.assertIsNone(dup_slot1)
        # disabled reason: never wakes
        disabled = bridge.request(rec, reason="replan_needed", episode=1,
                                  evidence=fat_evidence)  # policy allows it…
        # (replan_needed IS in the default set; a custom restricted policy must refuse)
        restricted = WakeBridge(self.root / "h",
                                WakePolicy(enabled_reasons=frozenset({"repeated_stall"})),
                                clock=clock)
        refused = restricted.request(rec, reason="replan_needed", episode=1,
                                     evidence=fat_evidence)
        self.assertIsNone(refused)
        # per-job absolute cap: bounded disk, bounded wake spam
        capped_rec = JobRecord(job_id="proc_cap00000001", argv=["x"], cwd="/tmp",
                               command_hash="c" * 64, started_at=clock())
        accepted = 0
        for episode in range(30):
            if bridge.request(capped_rec, reason="repeated_stall",
                              evidence=fat_evidence, episode=episode):
                accepted += 1
        self.assertEqual(accepted, WakePolicy().max_total_wakes_per_job)
        lines = (self.root / "g" / "wake_queue.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 4 + accepted)  # dedupe/cap honored exactly

    # 8 ------------------------------------------------------------------------
    def test_08_wake_packet_bounded_and_allowlisted(self):
        from _p7_helpers import FakeClock
        clock = FakeClock()
        policy = WakePolicy(max_packet_bytes=8192)
        bridge = WakeBridge(self.root / "i", policy, clock=clock)
        rec = JobRecord(job_id="proc_packet00001", argv=["x"], cwd="/tmp",
                        command_hash="d" * 64, started_at=clock(),
                        checkpoint="C" * 10_000, state=JobState.STALL_SUSPECTED)
        fat = {f"key{i}": ("value " + str(i)) * 30 for i in range(50)}
        packet = bridge.request(rec, reason="deadline_policy", episode=0,
                                evidence=fat,
                                laya={"status": "ok", "classification": "STALLED"},
                                spark={"status": "ok", "recommended_action": "WAIT"},
                                pointers={"event_log": "/tmp/events.jsonl",
                                          "job_file": "/tmp/job.json",
                                          "stdout": "/tmp/o", "stderr": "/tmp/e"})
        self.assertIsNotNone(packet)
        self.assertEqual(set(packet.keys()), set(WAKE_PACKET_FIELDS))
        self.assertLessEqual(len(json.dumps(packet).encode()), 8192)
        self.assertLessEqual(len(packet["evidence"]), 16)
        self.assertLessEqual(len(packet["checkpoint"] or ""), 200)
        self.assertEqual(packet["reason"], "deadline_policy")
        # queue line wraps it but the PACKETS stay bounded
        lines = (self.root / "i" / "wake_queue.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        queued = json.loads(lines[0])["packet"]
        self.assertLessEqual(len(json.dumps(queued).encode()), 8192)


if __name__ == "__main__":
    unittest.main()