"""P7e — the single-process sentinel daemon.

* job registry + adaptive per-job due-scheduling (the local event loop; the
  loop owns the ENTIRE observation period — no model is contacted by it
  unless the ambiguity gate / interpreter policy says so),
* Unix-socket JSON API (spawn / snapshot / cancel / list / health / metrics /
  summary / events / shutdown) — NO public port of any kind,
* signal handling with graceful shutdown (persist states, drop socket),
* health marker file (last-heartbeat ts) for systemd HealthCheck-style checks,
* bounded disk: rotating sentinel log (journald via stdout + explicit file),
  rotating event log, capped/rotated per-job stdout/stderr capture, tail-only
  reads,
* restart recovery on start: persisted non-terminal jobs re-verified via
  PID + /proc start time (identity), never assumed; no duplicate spawns.

Logging: stdout -> journald (unit StandardOutput=journal) AND an explicit
rotating log file (SENTINEL_LOG_FILE or <state-root>/sentinel.log).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from .api import SentinelRuntime
from .laya_gate import LayaClient, LayaGate
from .spark_interpreter import SparkClient, SparkInterpreter
from .watcher import WatchPolicy
from .wake_bridge import WakePolicy

LOG = logging.getLogger("agy_sentinel")


def _setup_logging(log_file: Optional[str], verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    LOG.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if log_file:
        try:
            Path(os.path.dirname(log_file) or ".").mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
            fh.setFormatter(fmt)
            LOG.addHandler(fh)
        except OSError as exc:  # journald stream still works
            sys.stderr.write(f"sentinel: cannot open log file {log_file}: {exc}\n")
    sh = logging.StreamHandler(sys.stdout)  # -> journald via the unit
    sh.setFormatter(fmt)
    LOG.addHandler(sh)


def _policy_from_args(args) -> WatchPolicy:
    p = WatchPolicy()
    for field_name in (
        "starting_interval_s", "stable_interval_s", "urgent_interval_s",
        "quiet_after_s", "urgent_quiet_margin_s", "urgent_deadline_margin_s",
        "active_cpu_pct", "stall_cpu_pct", "exit_file_grace_s",
        "spark_grace_after_stall_s", "cancel_grace_s",
    ):
        value = getattr(args, field_name, None)
        if value is not None:
            setattr(p, field_name, float(value))
    if args.deadline_policy:
        p.deadline_policy = args.deadline_policy
    if args.no_spark:
        p.spark_enabled = False
    if args.no_laya:
        p.laya_enabled = False
    if args.laya_on_checkpoint:
        p.laya_on_checkpoint = True
    if args.capture_max_bytes is not None:
        p.capture_max_bytes = int(args.capture_max_bytes)
    return p


class SentinelDaemon:
    def __init__(self, runtime: SentinelRuntime, socket_path: str):
        self.runtime = runtime
        self.socket_path = str(socket_path)
        self.stop = False
        self.started_at = time.time()
        self.recover_summary: Dict[str, Any] = {}
        self._lock_fh = None
        self._server: Optional[socket.socket] = None
        self._last_heartbeat = 0.0
        self._last_metrics_save = 0.0

    # -- single-instance lock + unix socket --------------------------------
    def _acquire_lock(self) -> bool:
        lock_path = self.runtime.state_root / "daemon.lock"
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()) + "\n")
        fh.flush()
        self._lock_fh = fh
        return True

    def _bind_socket(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                path.unlink()  # stale socket from a dead daemon (lock held above)
            except OSError:
                pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.socket_path)
        srv.listen(8)
        srv.settimeout(0.25)
        self._server = srv

    # -- request handling ---------------------------------------------------
    def _dispatch(self, req: Dict[str, Any]) -> Dict[str, Any]:
        op = req.get("op")
        rt = self.runtime
        try:
            if op == "spawn":
                if not req.get("command"):
                    return {"ok": False, "error": "missing field: command"}
                job_id = rt.spawn_monitored_process(
                    req.get("command"),
                    cwd=req.get("cwd"),
                    deadline_s=req.get("deadline_s"),
                    checkpoint_patterns=req.get("checkpoint_patterns") or (),
                    activity_markers=req.get("activity_markers") or (),
                    verify=req.get("verify"),
                )
                return {"ok": True, "job_id": job_id,
                        "state": rt.engine.get(job_id).state}
            if op == "snapshot":
                return {"ok": True, "snapshot": rt.get_process_snapshot(req["job_id"])}
            if op == "cancel":
                return {"ok": True, **rt.cancel_process(req["job_id"])}
            if op == "list":
                jobs = [
                    {"job_id": r.job_id, "state": r.state, "pid": r.pid}
                    for r in rt.store.records()
                ]
                return {"ok": True, "jobs": jobs}
            if op == "health":
                return {"ok": True, "health": self.health_doc()}
            if op == "metrics":
                return {"ok": True, "metrics": rt.metrics_snapshot()}
            if op == "summary":
                if req.get("job_id"):
                    return {"ok": True, "summary": rt.job_summary(req["job_id"])}
                return {"ok": True,
                        "summaries": [rt.job_summary(r.job_id)
                                      for r in sorted(rt.store.records(),
                                                      key=lambda x: x.created_at)]}
            if op == "events":
                n = int(req.get("n") or 50)
                return {"ok": True,
                        "events": rt.event_log.tail_for(req.get("job_id"), n=n)}
            if op == "recover":
                return {"ok": True, "recover": self.recover_summary}
            if op == "shutdown":
                self.stop = True
                return {"ok": True, "shutting_down": True}
            return {"ok": False, "error": f"unknown op: {op}"}
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc}"}
        except Exception as exc:  # keep the daemon alive on request errors
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _serve_once(self) -> None:
        assert self._server is not None
        try:
            conn, _ = self._server.accept()
        except socket.timeout:
            return
        except OSError:
            return
        with conn:
            conn.settimeout(5.0)
            buf = b""
            try:
                while b"\n" not in buf and len(buf) < 1_000_000:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            except (socket.timeout, OSError):
                pass
            line = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
            try:
                req = json.loads(line) if line else {}
            except json.JSONDecodeError:
                resp = {"ok": False, "error": "invalid json request"}
            else:
                resp = self._dispatch(req)
            try:
                conn.sendall((json.dumps(resp, separators=(",", ":")) + "\n").encode("utf-8"))
            except OSError:
                pass

    # -- health / heartbeat ---------------------------------------------------
    def health_doc(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "ok": True,
            "ts": now,
            "pid": os.getpid(),
            "uptime_s": round(now - self.started_at, 1),
            "socket": self.socket_path,
            "jobs_active": int(self.runtime.metrics.counters["sentinel_jobs_active"]),
            "last_heartbeat_ts": self._last_heartbeat or now,
        }

    def _heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat < 1.0:
            return
        self._last_heartbeat = now
        path = self.runtime.state_root / "health.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.health_doc(), fh)
            os.replace(tmp, path)
        except OSError:
            pass
        if now - self._last_metrics_save > 5.0:
            self._last_metrics_save = now
            self.runtime.metrics.save()

    # -- main loop ------------------------------------------------------------
    def run(self) -> int:
        if not self._acquire_lock():
            LOG.error("another sentinel daemon holds %s — refusing to start",
                      self.runtime.state_root / "daemon.lock")
            return 3
        self._bind_socket()
        # RESTART RECOVERY before serving: re-verify identity, never respawn
        self.recover_summary = self.runtime.recover()
        LOG.info("daemon started pid=%s socket=%s recovery=%s",
                 os.getpid(), self.socket_path,
                 json.dumps({k: v for k, v in self.recover_summary.items()
                             if k.endswith(("resumed", "lost", "terminalized", "checked"))}))

        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        exit_code = 0
        try:
            while not self.stop:
                self._serve_once()
                try:
                    self.runtime.tick_due()
                except Exception:
                    LOG.exception("tick loop error (daemon continues)")
                self._heartbeat()
        except Exception:
            LOG.exception("fatal daemon loop error")
            exit_code = 1
        finally:
            try:
                self.runtime.shutdown()
            except Exception:
                LOG.exception("shutdown persistence error")
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
            if self._lock_fh is not None:
                try:
                    fcntl.flock(self._lock_fh, fcntl.LOCK_UN)
                    self._lock_fh.close()
                except OSError:
                    pass
            LOG.info("shutdown clean (exit=%s)", exit_code)
        return exit_code

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        LOG.info("signal %s: graceful shutdown", signum)
        self.stop = True


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agy-sentinel",
                                  description="AGY local event-driven process sentinel daemon")
    ap.add_argument("--state-root", default=os.environ.get(
        "SENTINEL_STATE_ROOT",
        str(Path.home() / ".local" / "state" / "agy-sentinel")))
    ap.add_argument("--socket", default=None,
                    help="Unix socket path (default: <state-root>/sentinel.sock)")
    ap.add_argument("--log-file", default=os.environ.get("SENTINEL_LOG_FILE"),
                    help="explicit rotating log file (stdout still goes to journald)")
    ap.add_argument("--verbose", action="store_true")
    # policy overrides (all intervals configurable, never hardcoded in logic)
    ap.add_argument("--starting-interval", dest="starting_interval_s", type=float, default=None)
    ap.add_argument("--stable-interval", dest="stable_interval_s", type=float, default=None)
    ap.add_argument("--urgent-interval", dest="urgent_interval_s", type=float, default=None)
    ap.add_argument("--quiet-after", dest="quiet_after_s", type=float, default=None)
    ap.add_argument("--urgent-quiet-margin", dest="urgent_quiet_margin_s", type=float, default=None)
    ap.add_argument("--urgent-deadline-margin", dest="urgent_deadline_margin_s", type=float, default=None)
    ap.add_argument("--active-cpu-pct", dest="active_cpu_pct", type=float, default=None)
    ap.add_argument("--stall-cpu-pct", dest="stall_cpu_pct", type=float, default=None)
    ap.add_argument("--exit-file-grace", dest="exit_file_grace_s", type=float, default=None)
    ap.add_argument("--spark-grace", dest="spark_grace_after_stall_s", type=float, default=None)
    ap.add_argument("--cancel-grace", dest="cancel_grace_s", type=float, default=None)
    ap.add_argument("--deadline-policy", dest="deadline_policy",
                    choices=["keep_watching", "ask"], default=None)
    ap.add_argument("--capture-max-bytes", dest="capture_max_bytes", type=int, default=None)
    ap.add_argument("--no-spark", action="store_true")
    ap.add_argument("--no-laya", action="store_true")
    ap.add_argument("--laya-on-checkpoint", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    state_root = Path(args.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    log_file = args.log_file or str(state_root / "sentinel.log")
    _setup_logging(log_file, args.verbose)
    policy = _policy_from_args(args)
    wake_policy = WakePolicy()
    if os.environ.get("SENTINEL_MIN_STALL_EPISODES"):
        wake_policy.min_stall_episodes = int(os.environ["SENTINEL_MIN_STALL_EPISODES"])
    runtime = SentinelRuntime(
        state_root, policy, wake_policy,
        laya_gate=(LayaGate(LayaClient()) if policy.laya_enabled else None),
        spark_interpreter=(SparkInterpreter(SparkClient())
                           if policy.spark_enabled else None))
    socket_path = args.socket or str(state_root / "sentinel.sock")
    daemon = SentinelDaemon(runtime, socket_path)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())