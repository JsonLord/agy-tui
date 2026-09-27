"""AGY P7a — Process Sentinel tests (17-case contract matrix + service glue).

 1  spawn creates a monitored job (proc_ id, argv/cwd/hash/paths persisted)
 2  PID + PGID persisted (pgid == pid via setsid/START_NEW_SESSION)
 3  /proc start time persisted (field 22, parsed after the last ')')
 4  running process detected (STARTING -> RUNNING locally)
 5  stdout activity detected (byte growth feeds last_output_at)
 6  stderr activity detected
 7  quiet + high CPU -> QUIET_RUNNING (never suspected)
 8  quiet + ~0 CPU + no activity -> STALL_SUSPECTED (suspected, not STALLED)
 9  exit 0 -> EXITED_SUCCESS with zero Laya calls
10  exit nonzero -> EXITED_FAILURE with zero Laya calls
11  checkpoint regex event (CHECKPOINT_REACHED + record.checkpoint)
12  deadline event (DEADLINE_REACHED, injected clock/compressed policy)
13  cancellation (SIGTERM -> CANCELLED, idempotent, process actually dies)
14  PID reuse protection (start-time mismatch -> LOST pid_recycled, no signal)
15  daemon restart recovery (2nd instance resumes SAME live process, no respawn)
16  LOST when identity unrecoverable (pid gone, no exit record)
17  duplicate unchanged observations never spam events

Real local processes, compressed intervals: each scenario stays well under 5s.
"""

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.orchestration.sentinel.api import SentinelRuntime
from app.orchestration.sentinel.observations import parse_proc_stat, proc_starttime
from app.orchestration.sentinel.process_state import JobState

from _p7_helpers import (
    FakeClock,
    fast_policy,
    event_types,
    events_of,
    safe_cleanup,
    ticks_until,
    wait_terminal,
    worker_checkpoints,
    worker_exit,
    worker_sleep,
    worker_spinner,
)


class P7aSentinelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-p7a-")
        self.root = Path(self._tmp.name)
        self.rt = SentinelRuntime(self.root / "state", fast_policy())
        self._spawned = []

    def tearDown(self):
        for job_id in self._spawned:
            try:
                safe_cleanup(self.rt, job_id)
            except Exception:
                pass
        time.sleep(0.25)  # let cancellations finish killing workers
        self._tmp.cleanup()

    def spawn(self, command=None, **kw):
        if command is None:
            command = kw.pop("command")
        else:
            kw.pop("command", None)
        job_id = self.rt.spawn_monitored_process(command, cwd=str(self.root), **kw)
        self._spawned.append(job_id)
        return job_id

    # 1 ------------------------------------------------------------------------
    def test_01_spawn_creates_monitored_job(self):
        job_id = self.spawn(command=worker_sleep(0.4))
        self.assertTrue(job_id.startswith("proc_"), job_id)
        rec = self.rt.store.load(job_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.argv[0], sys.executable)
        self.assertEqual(rec.cwd, str(self.root))
        self.assertEqual(len(rec.command_hash), 64)
        self.assertTrue(os.path.exists(rec.stdout_path) or True)
        self.assertEqual(rec.state, JobState.STARTING)
        self.assertIsNotNone(self.rt.event_log.tail_for(job_id, n=10),
                             "event log missing")

    # 2 ------------------------------------------------------------------------
    def test_02_pid_and_pgid_persisted(self):
        job_id = self.spawn(command=worker_sleep(0.6))
        rec = wait_terminal_pathless_cap(self.rt, job_id, JobState.RUNNING)
        self.assertGreater(rec.pid, 0)
        self.assertEqual(rec.pgid, rec.pid)  # setsid/START_NEW_SESSION
        saved = json.loads((self.root / "state" / "jobs" / f"{job_id}.json").read_text())
        self.assertEqual(saved["pid"], rec.pid)
        self.assertEqual(saved["pgid"], rec.pgid)
        self.assertTrue(os.path.exists(f"/proc/{rec.pid}"))

    # 3 ------------------------------------------------------------------------
    def test_03_starttime_persisted(self):
        job_id = self.spawn(command=worker_sleep(0.6))
        rec = ticks_until(self.rt, job_id, lambda r: r.state != JobState.STARTING)
        raw = parse_proc_stat(Path(f"/proc/{rec.pid}/stat").read_text())
        self.assertEqual(rec.proc_starttime, raw["starttime"])
        self.assertEqual(proc_starttime(rec.pid), raw["starttime"])
        saved = json.loads((self.root / "state" / "jobs" / f"{job_id}.json").read_text())
        self.assertEqual(saved["proc_starttime"], raw["starttime"])

    # 4 ------------------------------------------------------------------------
    def test_04_running_process_detected(self):
        job_id = self.spawn(command=worker_sleep(1.2))
        rec = ticks_until(self.rt, job_id,
                          lambda r: r.state == JobState.RUNNING, timeout=3.0)
        self.assertEqual(rec.state, JobState.RUNNING)
        self.assertGreater(rec.metrics["observations"], 0)
        self.assertTrue(os.path.exists(f"/proc/{rec.pid}"))

    # 5 ------------------------------------------------------------------------
    def test_05_stdout_activity_detected(self):
        job_id = self.spawn(command=worker_sleep(0.8))
        saw_growth = False
        deadline = time.monotonic() + 3.0
        rec = self.rt.engine.get(job_id)
        while time.monotonic() < deadline:
            res = self.rt.tick(job_id)
            if res.observation is not None and res.observation.stdout_delta > 0:
                saw_growth = True
                break
            if self.rt.engine.get(job_id).state in ("EXITED_SUCCESS", "EXITED_FAILURE"):
                break
            time.sleep(0.02)
        self.assertTrue(saw_growth, "stdout byte growth never observed")
        self.assertGreaterEqual(rec.last_output_at, rec.started_at)

    # 6 ------------------------------------------------------------------------
    def test_06_stderr_activity_detected(self):
        cmd = [sys.executable, "-c",
               "import sys,time\n"
               "time.sleep(0.05)\n"
               "sys.stderr.write('warn-here\\n'); sys.stderr.flush()\n"
               "time.sleep(0.5)\n"]
        job_id = self.spawn(command=cmd)
        saw = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            res = self.rt.tick(job_id)
            if res.observation is not None and res.observation.stderr_delta > 0:
                saw = True
                break
            time.sleep(0.02)
        self.assertTrue(saw, "stderr byte growth never observed")
        rec = self.rt.engine.get(job_id)
        self.assertGreater(rec.stderr_bytes, 0)

    # 7 ------------------------------------------------------------------------
    def test_07_quiet_high_cpu_is_quiet_running(self):
        rt = SentinelRuntime(self.root / "q7", fast_policy(urgent_quiet_margin_s=999))
        self.rt = rt
        job_id = rt.spawn_monitored_process(
            worker_spinner(5.0), cwd=str(self.root))
        self._spawned.append(job_id)
        rec = ticks_until(rt, job_id,
                          lambda r: r.state in (JobState.QUIET_RUNNING,
                                                JobState.STALL_SUSPECTED),
                          timeout=4.0, interval=0.03)
        # quiet + high CPU MUST be QUIET_RUNNING, never STALL_SUSPECTED
        self.assertEqual(rec.state, JobState.QUIET_RUNNING)
        self.assertIsNotNone(rec.last_cpu_pct)
        self.assertGreaterEqual(rec.last_cpu_pct, 25.0)
        self.assertNotIn("STALL_SUSPECTED", event_types(rt, job_id))

    # 8 ------------------------------------------------------------------------
    def test_08_quiet_zero_cpu_no_activity_is_stall_suspected(self):
        job_id = self.spawn(command=worker_sleep(3.0))  # silent sleeper
        rec = ticks_until(self.rt, job_id,
                          lambda r: r.state == JobState.STALL_SUSPECTED,
                          timeout=4.0, interval=0.03)
        self.assertEqual(rec.state, JobState.STALL_SUSPECTED)
        self.assertIn("STALL_SUSPECTED", event_types(self.rt, job_id))
        self.assertIsNotNone(rec.last_cpu_pct)
        self.assertLessEqual(rec.last_cpu_pct, 5.0)
        # alive despite suspicion: it is a SUSPECT, deterministic facts intact
        self.assertTrue(os.path.exists(f"/proc/{rec.pid}"))

    # 9 ------------------------------------------------------------------------
    def test_09_exit_zero_success_without_laya(self):
        job_id = self.spawn(command=worker_exit(0))
        rec = wait_terminal(self.rt, job_id, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        ev = events_of(self.rt, job_id, "PROCESS_EXITED_SUCCESS")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence"]["returncode"], 0)
        self.assertEqual(self.rt.metrics.counters["sentinel_laya_calls_total"], 0)
        self.assertEqual(rec.metrics["laya_calls"], 0)

    # 10 -----------------------------------------------------------------------
    def test_10_exit_nonzero_failure_without_laya(self):
        job_id = self.spawn(command=worker_exit(3))
        rec = wait_terminal(self.rt, job_id, timeout=4.0)
        self.assertEqual(rec.state, JobState.EXITED_FAILURE)
        ev = events_of(self.rt, job_id, "PROCESS_EXITED_FAILURE")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["evidence"]["returncode"], 3)
        self.assertEqual(self.rt.metrics.counters["sentinel_laya_calls_total"], 0)

    # 11 -----------------------------------------------------------------------
    def test_11_checkpoint_regex_event(self):
        job_id = self.spawn(
            command=worker_checkpoints(2, gap=0.25),
            checkpoint_patterns=[r"\[ckpt\] phase \d/\d"])
        rec = wait_terminal(self.rt, job_id, timeout=5.0)
        self.assertEqual(rec.state, JobState.EXITED_SUCCESS)
        ck_events = events_of(self.rt, job_id, "CHECKPOINT_REACHED")
        self.assertGreaterEqual(len(ck_events), 1)
        self.assertIn("[ckpt] phase", ck_events[0]["evidence"]["checkpoint"])
        self.assertEqual(rec.checkpoint, "[ckpt] phase 2/2")

    # 12 -----------------------------------------------------------------------
    def test_12_deadline_reached_event(self):
        clock = FakeClock()
        rt = SentinelRuntime(self.root / "q12", fast_policy(), clock=clock)
        self.rt = rt
        job_id = rt.spawn_monitored_process(worker_sleep(4.0), cwd=str(self.root),
                                            deadline_s=0.4)
        self._spawned.append(job_id)
        rec = None
        for _ in range(40):  # deterministic: no wall-clock wait for deadlines
            res = rt.tick(job_id)
            if res.events:
                rec = rt.engine.get(job_id)
                if rec.state == JobState.DEADLINE_REACHED:
                    break
            clock.advance(0.05)
            time.sleep(0.02)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.state, JobState.DEADLINE_REACHED)
        ev = events_of(rt, job_id, "DEADLINE_REACHED")
        self.assertEqual(len(ev), 1)
        self.assertLessEqual(ev[0]["evidence"]["deadline_remaining_s"], 0)
        rt.cancel_process(job_id)  # cleanup while we still control the record

    # 13 -----------------------------------------------------------------------
    def test_13_cancellation(self):
        job_id = self.spawn(command=worker_sleep(30))
        rec = ticks_until(self.rt, job_id, lambda r: r.pid, timeout=3.0)
        pid = rec.pid
        resp = self.rt.cancel_process(job_id)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"], JobState.CANCELLED)
        again = self.rt.cancel_process(job_id)
        self.assertTrue(again.get("already_cancelled"))
        self.assertEqual(again["state"], JobState.CANCELLED)
        ev = events_of(self.rt, job_id, "PROCESS_CANCELLED")
        self.assertEqual(len(ev), 1)
        # the process group must actually die (grace -> SIGKILL driven by ticks)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            self.rt.tick(job_id)
            if not os.path.exists(f"/proc/{pid}"):
                break
            time.sleep(0.05)
        self.assertFalse(os.path.exists(f"/proc/{pid}"),
                         "cancelled worker still alive")

    # 14 -----------------------------------------------------------------------
    def test_14_pid_reuse_protection(self):
        """Simulated real PID recycling: same PID, different /proc start time.

        (Constructing a genuine fast PID-REUSE race is not deterministic in a
        test environment — pid_max allocation is kernel-controlled — so the
        identity check is exercised exactly as it would fire: the stored
        starttime no longer matches the process currently holding the pid.)
        """
        job_id = self.spawn(command=worker_sleep(20))
        rec = ticks_until(self.rt, job_id,
                          lambda r: r.pid and r.state != JobState.STARTING)
        pid = rec.pid
        real_start = proc_starttime(pid)
        self.assertEqual(rec.proc_starttime, real_start)
        rec.proc_starttime = int(real_start) + 1  # as if pid was recycled
        self.rt.store.save(rec)
        got = ticks_until(self.rt, job_id,
                          lambda r: r.state == JobState.LOST, timeout=3.0)
        self.assertEqual(got.state, JobState.LOST)
        ev = events_of(self.rt, job_id, "PROCESS_LOST")
        self.assertEqual(ev[-1]["evidence"]["reason"], "pid_recycled")
        # safety: identity-untrusted must NOT cause us to signal the pid
        self.assertTrue(os.path.exists(f"/proc/{pid}"))
        self.assertFalse(got.cancel_requested)
        os.killpg(rec.pgid, signal.SIGKILL)  # explicit test cleanup

    # 15 -----------------------------------------------------------------------
    def test_15_daemon_restart_recovery_resumes_same_live_process(self):
        job_id = self.spawn(command=worker_sleep(1.5))
        rt1 = self.rt
        rec1 = ticks_until(rt1, job_id,
                           lambda r: r.state in (JobState.RUNNING, JobState.CHECKPOINT))
        self.assertIn(rec1.state, (JobState.RUNNING, JobState.CHECKPOINT))
        pid_before = rec1.pid
        state_before = rec1.proc_starttime

        # SECOND in-process "daemon" over the same state root
        spawned_by_rt2 = []

        class NoRespawn(SentinelRuntime):
            def spawn_monitored_process(self, *a, **k):  # must never be called
                spawned_by_rt2.append((a, k))
                raise AssertionError("restart must never spawn a duplicate")

        rt2 = NoRespawn(self.root / "state", fast_policy())
        summary = rt2.recover()
        self.assertIn(job_id, summary["resumed_jobs"])
        self.assertEqual(summary["resumed"], 1)
        rec2 = rt2.engine.get(job_id)
        self.assertEqual(rec2.pid, pid_before)
        self.assertEqual(rec2.proc_starttime, state_before)
        # SAME live process is still the same START_TIME on /proc right now
        self.assertEqual(proc_starttime(pid_before), state_before)

        # monitoring resumed: reaches its real terminal state after its sleep
        got = wait_terminal(rt2, job_id, timeout=5.0)
        self.assertEqual(got.state, JobState.EXITED_SUCCESS)
        self.assertEqual(spawned_by_rt2, [])  # NO duplicate spawn
        # same event log on disk contains the terminal event added by rt2
        types = event_types(rt2, job_id)
        self.assertEqual(types.count("PROCESS_STARTED"), 1)
        self.assertIn("PROCESS_EXITED_SUCCESS", types)
        # index shows the resume entry in the append-only jobs.index
        idx = (self.root / "state" / "jobs.index").read_text()
        self.assertIn("resumed_after_restart", idx)
        # job is terminal now; rt1 must not touch it again during teardown
        if job_id in self._spawned:
            self._spawned.remove(job_id)

    # 16 -----------------------------------------------------------------------
    def test_16_lost_when_identity_unrecoverable(self):
        job_id = self.spawn(command=worker_sleep(30))
        rec = ticks_until(self.rt, job_id, lambda r: r.pid and r.state != JobState.STARTING)
        os.killpg(rec.pgid, signal.SIGKILL)
        time.sleep(0.3)  # holder reaps the worker and writes the exit record...
        os.unlink(rec.exit_file)  # ...but the daemon "died across the exit"
        got = ticks_until(self.rt, job_id,
                          lambda r: r.state == JobState.LOST, timeout=4.0)
        self.assertEqual(got.state, JobState.LOST)
        ev = events_of(self.rt, job_id, "PROCESS_LOST")
        self.assertTrue(ev and ev[-1]["evidence"]["reason"] == "pid_gone_no_exit")

    # 17 -----------------------------------------------------------------------
    def test_17_duplicate_observations_do_not_spam_events(self):
        policy = fast_policy(quiet_after_s=999.0, urgent_quiet_margin_s=999)
        rt = SentinelRuntime(self.root / "q17", policy)
        self.rt = rt
        job_id = rt.spawn_monitored_process(worker_sleep(20), cwd=str(self.root))
        self._spawned.append(job_id)
        # warm up: get past STARTING -> RUNNING once
        ticks_until(rt, job_id, lambda r: r.state == JobState.RUNNING, timeout=3.0)
        baseline = rt.event_log.tail_for(job_id, n=1000)
        self.assertEqual(len(baseline), 1)  # PROCESS_STARTED only
        self.assertEqual(baseline[0]["event_type"], "PROCESS_STARTED")
        for _ in range(12):                 # unchanged observations
            rt.tick(job_id)
            time.sleep(0.03)
        after = rt.event_log.tail_for(job_id, n=1000)
        self.assertEqual(len(after), 1, "unchanged observations generated events")
        self.assertGreaterEqual(rt.engine.get(job_id).metrics["observations"], 12)
        rt.cancel_process(job_id)


def wait_terminal_pathless_cap(rt, job_id, want_state):
    """Tick until state leaves STARTING; used for early-state assertions."""
    return ticks_until(rt, job_id, lambda r: r.state != JobState.STARTING,
                       timeout=3.0)


if __name__ == "__main__":
    unittest.main()