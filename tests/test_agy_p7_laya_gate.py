"""AGY P7b — LAYA AMBIGUITY GATE tests (8-case contract matrix).

 1 Laya NOT called on deterministic success
 2 Laya NOT called on deterministic failure
 3 Laya IS called for the configured ambiguous transition (mock counts calls)
 4 payload is COMPACT (bounded bytes, allowlisted keys, no raw log blobs)
 5 Laya timeout -> safe UNKNOWN decision
 6 Laya 5xx   -> safe UNKNOWN decision
 7 deterministic policy CONTINUES during Laya outage (job still terminates)
 8 a Laya decision can NEVER change a terminal deterministic fact

Network-free: hosted contract exercised through injected transports/mocks.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.orchestration.sentinel.api import SentinelRuntime
from app.orchestration.sentinel.laya_gate import (
    PAYLOAD_KEYS,
    LayaClient,
    LayaGate,
    build_laya_payload,
)
from app.orchestration.sentinel.process_state import JobRecord, JobState
from app.orchestration.sentinel.watcher import SentinelEngine, TickResult

from _p7_helpers import (
    RecordingLaya,
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


def _fake_record(**over) -> JobRecord:
    base = dict(job_id="proc_fakerec0001", argv=["x"], cwd="/tmp",
                command_hash="0" * 64)
    base.update(over)
    return JobRecord(**base)


def _fake_obs(**over):
    defaults = dict(ts=time.time(), quiet_s=10.0, cpu_pct=0.0,
                    rss_delta_kb=128, stdout_delta=0, stderr_delta=0,
                    deadline_remaining_s=None, quiet_s_=None)
    defaults.update(over)
    defaults.pop("quiet_s_", None)
    return SimpleNamespace(**defaults)


class P7bLayaGateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-p7b-")
        self.root = Path(self._tmp.name)
        self._spawned = []

    def tearDown(self):
        time.sleep(0.2)
        self._tmp.cleanup()

    def spawn(self, rt, command, **kw):
        job_id = rt.spawn_monitored_process(command, cwd=str(self.root), **kw)
        self._spawned.append((rt, job_id))
        return job_id

    def cleanup_jobs(self):
        for rt, job_id in self._spawned:
            try:
                safe_cleanup(rt, job_id)
            except Exception:
                pass

    # 1 ------------------------------------------------------------------------
    def test_01_laya_not_called_on_deterministic_success(self):
        laya = RecordingLaya()
        policy = fast_policy(quiet_after_s=999, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "s", policy, laya_gate=counting_gate(laya))
        job_id = self.spawn(rt, worker_exit(0, sleep=0.3))
        rec = wait_terminal(rt, job_id, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        self.assertEqual(laya.calls, 0)
        self.assertEqual(rt.metrics.counters["sentinel_laya_calls_total"], 0)
        self.assertEqual(events_of(rt, job_id, "LAYA_DECIDED"), [])

    # 2 ------------------------------------------------------------------------
    def test_02_laya_not_called_on_deterministic_failure(self):
        laya = RecordingLaya()
        policy = fast_policy(quiet_after_s=999, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "s", policy, laya_gate=counting_gate(laya))
        job_id = self.spawn(rt, worker_exit(7, sleep=0.3))
        rec = wait_terminal(rt, job_id, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_FAILURE)
        self.assertEqual(laya.calls, 0)
        self.assertEqual(rt.metrics.counters["sentinel_laya_calls_total"], 0)

    # 3 ------------------------------------------------------------------------
    def test_03_laya_called_exactly_once_per_ambiguous_transition(self):
        laya = RecordingLaya(choice="WAIT")
        rt = SentinelRuntime(self.root / "s", fast_policy(),
                             laya_gate=counting_gate(laya))
        job_id = self.spawn(rt, worker_sleep(3.0))  # silent -> stalls
        rec = ticks_until(rt, job_id,
                          lambda r: r.state == JobState.STALL_SUSPECTED,
                          timeout=4.0, interval=0.03)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        # exactly one call for this episode, even with many further ticks
        for _ in range(15):
            rt.tick(job_id)
            time.sleep(0.03)
        self.assertEqual(laya.calls, 1)
        self.assertEqual(rt.metrics.counters["sentinel_laya_calls_total"], 1)
        ev = events_of(rt, job_id, "LAYA_DECIDED")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence"]["decision"]["classification"], "WAIT")
        self.assertLessEqual(ev[0]["evidence"]["payload_bytes"], 2048)
        self.cleanup_jobs()

    # 4 ------------------------------------------------------------------------
    def test_04_payload_is_compact(self):
        rec = _fake_record(
            state=JobState.STALL_SUSPECTED, checkpoint="Z" * 5000,
            started_at=time.time() - 300, checkpoint_at=time.time() - 10,
            last_marker_mtimes={"/srv/very/long/path/to/marker/bundle.done": 1.0})
        obs = _fake_obs(cpu_pct=0.0, deadline_remaining_s=55.0)
        payload = build_laya_payload(rec, obs, now=time.time())
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        self.assertLessEqual(len(raw.encode()), 2048)
        self.assertEqual(set(payload), set(PAYLOAD_KEYS))
        # no raw log/history blobs: every string stays small and there are no
        # log-ish keys
        for key, value in payload.items():
            self.assertNotIn(key, ("log", "logs", "history", "stdout",
                                   "tail", "content"))
            if isinstance(value, str):
                self.assertLessEqual(len(value), 200)
            if isinstance(value, list):
                for item in value:
                    self.assertIsInstance(item, str)
                    self.assertLessEqual(len(item), 200)
        # gate enforces the same bound end-to-end
        laya = RecordingLaya()
        gate = counting_gate(laya)
        gate.decide(payload)
        self.assertLessEqual(gate.last_payload_bytes, 2048)
        self.assertLessEqual(len(laya.payloads[0].encode()), 2048)

    # 5 ------------------------------------------------------------------------
    def test_05_laya_timeout_is_safe_unknown(self):
        def transport(payload, headers, timeout):
            raise TimeoutError("socket timed out")

        client = LayaClient(api_key="LAYA_TEST_KEY_[REDACTED]", retries=1,
                            transport=transport)
        gate = LayaGate(client=client)
        decision = gate.decide({"state": "STALL_SUSPECTED"})
        self.assertTrue(decision.is_unknown)
        self.assertEqual(decision.status, "unknown")
        self.assertIsNone(decision.classification)
        self.assertFalse(decision.notify_supervisor)
        self.assertFalse(decision.needs_interpretation)
        self.assertIsNotNone(decision.failure)
        # no secret ever leaks into the typed decision
        self.assertNotIn("[REDACTED]", json.dumps(decision.to_dict()))

    # 6 ------------------------------------------------------------------------
    def test_06_laya_5xx_is_safe_unknown(self):
        calls = []

        def transport(payload, headers, timeout):
            calls.append((payload["_url"], headers.get("Authorization")))
            return 500, b'{"detail":"internal server error"}'

        client = LayaClient(api_key="[REDACTED]", retries=1,
                            transport=transport)
        gate = LayaGate(client=client)
        decision = gate.decide({"state": "STALL_SUSPECTED"})
        self.assertTrue(decision.is_unknown)
        self.assertEqual(decision.failure, "server_error")
        self.assertGreaterEqual(client.call_count, 1)  # bounded retry attempted
        for _, auth in calls:
            self.assertTrue(auth.startswith("Bearer "))
            self.assertNotIn("REDACTED-TOKEN", auth)  # key sent, never returned

    # 7 ------------------------------------------------------------------------
    def test_07_policy_continues_during_laya_outage(self):
        laya_client = LayaClient(
            api_key="[REDACTED]", retries=0,
            transport=lambda p, h, t: (_ for _ in ()).throw(
                TimeoutError("down")))
        gate = LayaGate(client=laya_client)
        rt = SentinelRuntime(self.root / "s", fast_policy(), laya_gate=gate)
        job_id = self.spawn(rt, worker_silence_then_print(0.7))
        rec = ticks_until(rt, job_id,
                          lambda r: r.state == JobState.STALL_SUSPECTED,
                          timeout=3.0, interval=0.03)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        self.assertGreaterEqual(laya_client.call_count, 1)
        self.assertEqual(rt.engine.get(job_id).laya_decision["status"], "unknown")
        # deterministic policy still drives the job to a terminal state:
        rec = wait_terminal(rt, job_id, timeout=5.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)

    # 8 ------------------------------------------------------------------------
    def test_08_laya_never_overrides_deterministic_terminal_fact(self):
        laya = RecordingLaya(choice="STALLED", notify=0.99, interpret=0.9)
        rt = SentinelRuntime(self.root / "s", fast_policy(),
                             laya_gate=counting_gate(laya))
        job_id = self.spawn(rt, worker_silence_then_print(0.7))
        rec = wait_terminal(rt, job_id, timeout=5.0)
        # Laya said STALLED (integration part) …
        if laya.calls:
            self.assertEqual(rec.laya_decision.get("classification"), "STALLED")
        # … but exit 0 still wins:
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)

        # hard-rule unit part: consult is refused on a terminal record
        before_calls = laya.calls
        rt.engine._consult_laya(rec, _fake_obs(), time.time(),
                                TickResult(job_id), trigger="stall", episode=99)
        self.assertEqual(laya.calls, before_calls)
        self.assertEqual(rt.engine.get(job_id).state, JobState.EXITED_SUCCESS)
        # and terminal states never enter the advisory path at all
        from app.orchestration.sentinel.state_machine import allowed_by_gate
        for terminal in ("EXITED_SUCCESS", "EXITED_FAILURE", "CANCELLED", "LOST"):
            self.assertFalse(allowed_by_gate(terminal))
        self.assertTrue(allowed_by_gate(JobState.STALL_SUSPECTED))


if __name__ == "__main__":
    unittest.main()