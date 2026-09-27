"""Detached holder process: the PERSISTENT PARENT of a monitored worker.

Why this exists (P7e restart recovery): the sentinel daemon may be restarted
(systemd) while the worker keeps running. If the daemon were the worker's
parent, a restart would orphan the worker and its exit code would be lost
(across daemon death reaping has nowhere reliable to land). The holder is a
tiny process OUTSIDE the daemon that:

  * spawns the worker in a NEW SESSION (setsid) so pgid == worker pid,
  * starts capped/rotating capture of worker stdout/stderr,
  * reports {pid, pgid, starttime, holder_pid} on ITS stdout exactly once
    (starttime captured from /proc BEFORE the worker could be reaped),
  * waits for the worker and writes the deterministic exit record
    (exits/<job_id>.json: pid, returncode, exited_at) — atomically, so
    even a daemon that died and came back can recover the true exit code.

Usage: python3 -m app.orchestration.sentinel.holder <spawn_file.json>
spawn_file: {"job_id","argv","cwd","stdout_path","stderr_path","exit_file",
             "capture_max_bytes","capture_rotations"}
Runs with no API keys in its environment-visible config; prints nothing after
the startup line (worker output is pumped by threads into the capped files).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


class _CappedWriter:
    """Pump worker output to a file with size cap + rotation (keep last N MB)."""

    def __init__(self, src_pipe, path: str, cap_bytes: int, rotations: int):
        self.src = src_pipe
        self.path = Path(path)
        self.cap = max(1024, int(cap_bytes))
        self.rotations = max(0, int(rotations))
        self.written = 0
        self._fh = None

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")
        self.written = 0

    def _rotate(self):
        try:
            if self._fh:
                self._fh.close()
        finally:
            self._fh = None
        if self.rotations >= 1:
            prev = self.path.with_name(self.path.name + ".1")
            try:
                if prev.exists():
                    prev.unlink()
                self.path.replace(prev)
            except OSError:
                pass
        self._open()

    def run(self):
        self._open()
        fh = self._fh
        try:
            # read1: return whatever the pipe already has (never buffer-block
            # until 64KB/EOF — checkpoint/activity detection must see output NOW)
            reader = getattr(self.src, "read1", None) or self.src.read
            while fh is not None and True:
                chunk = reader(65536)
                if not chunk:
                    break
                if self.written + len(chunk) > self.cap and self.rotations >= 1:
                    self._rotate()
                    fh = self._fh
                    if fh is None:
                        break
                elif self.written > self.cap + len(chunk) and self.rotations == 0:
                    # no rotation configured: keep only the tail
                    self._open()
                    fh = self._fh
                    if fh is None:
                        break
                fh.write(chunk)
                fh.flush()
                self.written += len(chunk)
        finally:
            try:
                if self._fh:
                    self._fh.close()
            except OSError:
                pass
            try:
                self.src.close()
            except OSError:
                pass


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        sys.stderr.write("usage: holder <spawn_file.json>\n")
        return 2
    with open(argv[0], "r", encoding="utf-8") as fh:
        task = json.load(fh)

    env = dict(os.environ)
    for k, v in (task.get("env_extra") or {}).items():
        env[str(k)] = str(v)

    stdout_p = subprocess.PIPE
    stderr_p = subprocess.PIPE
    try:
        child = subprocess.Popen(
            [str(a) for a in task["argv"]],
            cwd=task.get("cwd") or None,
            stdout=stdout_p,
            stderr=stderr_p,
            env=env,
            start_new_session=True,  # pgid == pid  (deterministic pgid identity)
        )
    except (OSError, ValueError) as exc:
        # startup failure must be visible to the spawning daemon as data
        sys.stdout.write(json.dumps({"error": f"spawn_failed:{type(exc).__name__}"}) + "\n")
        sys.stdout.flush()
        return 1

    # capture /proc start time BEFORE the child could possibly be reaped
    starttime = None
    for _ in range(50):
        try:
            with open(f"/proc/{child.pid}/stat", "r", encoding="utf-8",
                      errors="replace") as fh:
                raw = fh.read()
            starttime = int(raw[raw.rindex(")") + 2:].split()[19])  # field 22
            break
        except (FileNotFoundError, ValueError, OSError):
            time.sleep(0.01)

    out_w = _CappedWriter(child.stdout, task["stdout_path"],
                           task.get("capture_max_bytes", 10_000_000),
                           task.get("capture_rotations", 1))
    err_w = _CappedWriter(child.stderr, task["stderr_path"],
                           task.get("capture_max_bytes", 10_000_000),
                           task.get("capture_rotations", 1))
    t_out = threading.Thread(target=out_w.run, daemon=True)
    t_err = threading.Thread(target=err_w.run, daemon=True)
    t_out.start()
    t_err.start()

    startup = {
        "pid": child.pid,
        "pgid": os.getpgid(child.pid),
        "starttime": starttime,
        "holder_pid": os.getpid(),
    }
    sys.stdout.write(json.dumps(startup, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    try:
        sys.stdout.close()
    except OSError:
        pass

    returncode = child.wait()
    t_out.join(timeout=30)
    t_err.join(timeout=30)

    exit_file = Path(task["exit_file"])
    exit_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "job_id": task.get("job_id"),
        "pid": child.pid,
        "returncode": int(returncode),
        "exited_at": round(time.time(), 3),
    }, separators=(",", ":"))
    tmp = exit_file.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, exit_file)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)