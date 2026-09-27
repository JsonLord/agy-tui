"""AGY P7e — service lifecycle tests: Unix-socket daemon + CLI + metrics +
systemd USER unit file + adapter seam.

Covers: Type=simple unit shipped at deploy/sentinel/agy-sentinel.service with
Restart=on-failure and NO public port; daemon spawn/snapshot/cancel/list/
health/metrics/summary/events/shutdown over a Unix socket; required metric
names; job summary line (duration/observations/laya/spark/wakeups); graceful
shutdown persistence; and the deferred execute-path adapter seam.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.orchestration.sentinel.adapter import make_executor_waker, wait_for_external_work
from app.orchestration.sentinel.api import SentinelRuntime
from app.orchestration.sentinel.cli import send_request
from app.orchestration.sentinel.metrics import COUNTER_NAMES

from _p7_helpers import (
    WakeRecorder,
    fast_policy,
    safe_cleanup,
    wait_terminal,
    worker_exit,
    worker_sleep,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_METRICS = (
    "sentinel_observations_total",
    "sentinel_state_transitions_total",
    "sentinel_laya_calls_total",
    "sentinel_spark_calls_total",
    "sentinel_supervisor_wakeups_total",
    "sentinel_jobs_active",
    "sentinel_jobs_completed",
    "sentinel_jobs_failed",
)
SUMMARY_RE = re.compile(
    r"job_summary job=proc_\w+ state=\w+ duration_s=[\d.]+ observations=\d+ "
    r"transitions=\d+ laya_decisions=\d+ spark_interpretations=\d+ "
    r"supervisor_wakeups=\d+")


class P7eServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agy-p7e-")
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        self.sock = str(self.root / "sentinel.sock")
        self.daemon = None

    def tearDown(self):
        if self.daemon is not None and self.daemon.poll() is None:
            try:
                send_request(self.sock, {"op": "shutdown"}, timeout=5)
            except Exception:
                pass
            try:
                self.daemon.wait(timeout=8)
            except Exception:
                self.daemon.kill()
        time.sleep(0.2)
        self._tmp.cleanup()

    def start_daemon(self, *extra_flags) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [sys.executable, "-u", "-m", "app.orchestration.sentinel", "daemon",
               "--state-root", str(self.state),
               "--socket", self.sock,
               *extra_flags]
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                resp = send_request(self.sock, {"op": "health"}, timeout=1)
                if resp.get("ok"):
                    return proc
            except Exception:
                time.sleep(0.1)
        out = ""
        try:
            out = proc.stdout.read(4000) if proc.poll() is not None else ""
        except Exception:
            pass
        self.fail(f"daemon did not become healthy: {out}")

    # 1 ------------------------------------------------------------------------
    def test_01_daemon_lifecycle_over_unix_socket(self):
        # compressed watching over the socket-driven daemon
        self.daemon = self.start_daemon(
            "--starting-interval", "0.05", "--stable-interval", "0.1",
            "--urgent-interval", "0.05", "--quiet-after", "9",
            "--exit-file-grace", "0.3", "--cancel-grace", "0.2")
        env = os.fspath(self.state)
        self.assertTrue(os.path.exists(self.sock), "no unix socket")
        # no public port: socket only (AF_UNIX); nothing listens on TCP here
        resp = send_request(self.sock, {"op": "spawn",
                                        "command": f"{sys.executable} -c \"import time; print('hi', flush=True); time.sleep(0.6)\"",
                                        "cwd": env})
        self.assertTrue(resp["ok"], resp)
        job = resp["job_id"]

        # snapshot (diagnostic) until terminal
        state = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            snap = send_request(self.sock, {"op": "snapshot", "job_id": job})
            self.assertTrue(snap["ok"], snap)
            state = snap["snapshot"]["state"]
            self.assertEqual(snap["snapshot"]["usage"],
                             "diagnostic; event-driven — do not poll for status")
            self.assertIn("metrics", snap["snapshot"])
            if state in ("EXITED_SUCCESS", "EXITED_FAILURE", "LOST", "CANCELLED"):
                break
            time.sleep(0.1)
        self.assertEqual(state, "EXITED_SUCCESS")

        # metrics: every required counter exists and is continuous
        metrics = send_request(self.sock, {"op": "metrics"})["metrics"]
        for name in REQUIRED_METRICS:
            self.assertIn(name, metrics)
            self.assertIsInstance(metrics[name], int)
        self.assertGreaterEqual(metrics["sentinel_observations_total"], 1)
        self.assertGreaterEqual(metrics["sentinel_jobs_completed"], 1)
        self.assertEqual(metrics["sentinel_laya_calls_total"], 0)

        # summary line(s) are parseable and complete
        summaries = send_request(self.sock, {"op": "summary"})["summaries"]
        self.assertEqual(len(summaries), 1)
        self.assertRegex(summaries[0], SUMMARY_RE)

        # list + events
        jobs = send_request(self.sock, {"op": "list"})["jobs"]
        self.assertEqual([j["job_id"] for j in jobs], [job])
        events = send_request(self.sock, {"op": "events", "job_id": job,
                                          "n": 100})["events"]
        types = [e["event_type"] for e in events]
        self.assertIn("PROCESS_STARTED", types)
        self.assertIn("PROCESS_EXITED_SUCCESS", types)
        self.assertEqual(types.count("PROCESS_DIAGNOSTIC_POLL" if False else "PROCESS_STARTED"), 1)

        # cancel path: second job SIGTERMed -> CANCELLED (idempotent)
        resp2 = send_request(self.sock, {"op": "spawn",
                                         "command": f"{sys.executable} -c \"import time; time.sleep(30)\"",
                                         "cwd": env})
        job2 = resp2["job_id"]
        # let it reach RUNNING so the signal hits a live group
        time.sleep(0.5)
        cancel = send_request(self.sock, {"op": "cancel", "job_id": job2})
        self.assertTrue(cancel["ok"])
        self.assertEqual(cancel["state"], "CANCELLED")
        again = send_request(self.sock, {"op": "cancel", "job_id": job2})
        self.assertTrue(again.get("already_cancelled") or again.get("ok"))

        # health marker file exists with a heartbeat timestamp
        health = send_request(self.sock, {"op": "health"})["health"]
        self.assertTrue(health["ok"])
        marker = json.loads((self.state / "health.json").read_text())
        self.assertGreater(marker["last_heartbeat_ts"], 0)

        # recover query answers from the daemon
        rec_op = send_request(self.sock, {"op": "recover"})
        self.assertTrue(rec_op["ok"])
        self.assertIn("checked", rec_op["recover"])

        # graceful shutdown: exits 0, socket removed, states persisted
        send_request(self.sock, {"op": "shutdown"}, timeout=5)
        rc = self.daemon.wait(timeout=10)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.sock), "socket not cleaned up")
        saved = json.loads((self.state / "jobs" / f"{job}.json").read_text())
        self.assertEqual(saved["state"], "EXITED_SUCCESS")

    # 2 ------------------------------------------------------------------------
    def test_02_second_daemon_is_refused_while_one_holds_the_lock(self):
        self.daemon = self.start_daemon(
            "--starting-interval", "0.05", "--stable-interval", "0.1")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        second = subprocess.run(
            [sys.executable, "-u", "-m", "app.orchestration.sentinel", "daemon",
             "--state-root", str(self.state), "--socket", str(self.root / "s2.sock")],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(second.returncode, 3)  # single-instance lock refuses
        self.assertIn("another sentinel daemon", second.stdout + second.stderr)

    # 3 ------------------------------------------------------------------------
    def test_03_systemd_user_unit_file_contract(self):
        unit = (REPO_ROOT / "deploy" / "sentinel" / "agy-sentinel.service")
        self.assertTrue(unit.exists(), "unit file not shipped")
        text = unit.read_text()
        self.assertIn("Type=simple", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("StandardOutput=journal", text)
        self.assertIn("StandardError=journal", text)
        self.assertIn("SENTINEL_LOG_FILE", text)          # explicit log path
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("python3 -m app.orchestration.sentinel daemon", text)
        self.assertIn("KillMode=process", text)           # workers survive restart
        lower = text.lower()
        # NO public port: no socket-activation/listen directives, no port keys
        self.assertNotIn("listenstream", lower)
        self.assertNotIn("listendatagram", lower)
        self.assertIsNone(re.search(r"(?im)^\s*port\s*=", text))
        self.assertNotIn("caddy", lower)
        self.assertNotIn("tailscale", lower)
        self.assertNotIn("execstart=/usr/sbin", lower)

    # 4 ------------------------------------------------------------------------
    def test_04_job_summary_line_and_metric_names(self):
        rt = SentinelRuntime(self.root / "s4", fast_policy())
        job = rt.spawn_monitored_process(worker_exit(0, sleep=0.3),
                                         cwd=str(self.root))
        rec = wait_terminal(rt, job, timeout=4.0)
        self.assertEqual(rec.state, "EXITED_SUCCESS")
        line = rt.job_summary(job)
        self.assertRegex(line, SUMMARY_RE)
        snap = rt.metrics_snapshot()
        for name in COUNTER_NAMES:
            self.assertIn(name, snap)
        self.assertEqual(snap["sentinel_jobs_completed"], 1)
        self.assertEqual(snap["sentinel_jobs_active"], 0)
        # zero-model local execution by default: only deterministic counters moved
        self.assertEqual(snap["sentinel_laya_calls_total"], 0)
        self.assertEqual(snap["sentinel_spark_calls_total"], 0)
        self.assertEqual(snap["sentinel_supervisor_wakeups_total"], 0)
        self.assertGreater(snap["sentinel_observations_total"], 0)

    # 5 ------------------------------------------------------------------------
    def test_05_adapter_seam_spawns_and_waker_seam_delivers(self):
        rt = SentinelRuntime(self.root / "s5", fast_policy())
        job = wait_for_external_work(rt, worker_sleep(0.5), cwd=str(self.root))
        self.assertTrue(job.startswith("proc_"))
        rec = wait_terminal(rt, job, timeout=4.0)
        self.assertEqual(rec.state, "EXITED_SUCCESS")

        delivered = []
        waker = make_executor_waker(rt, dispatch=delivered.append)
        waker({"job_id": job, "reason": "smoke"})  # seam wiring only
        self.assertEqual(delivered, [{"job_id": job, "reason": "smoke"}])


if __name__ == "__main__":
    unittest.main()