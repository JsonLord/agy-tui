"""P7a — Tier-0 deterministic observations. ZERO model calls.

EVERYTHING observed here (pid existence, pgid, /proc start-time identity,
exit status, cpu, rss, output growth, markers, checkpoint regex, deadline,
cancellation) is a deterministic fact that NEVER goes through Laya/Spark.

All time and /proc access is injectable (now(), stat_reader, rss_reader) so
tests can compress multi-minute scenarios into sub-second wall time.

/proc/<pid>/stat parsing: comm (field 2) can contain spaces and parentheses
(e.g. comm="next-server (v1"), so fields are parsed AFTER the LAST ')'.
Verified on this machine with a python probe: field 22 (starttime) is index
19 after the comm separator, utime/stime are indexes 11/12, rss is 21.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    CLK_TCK = float(os.sysconf("SC_CLK_TCK"))
except (ValueError, OSError, AttributeError):  # pragma: no cover - exotic platforms
    CLK_TCK = 100.0

_STAT_VMRSS_RE = re.compile(r"^VmRSS:\s+(\d+)\s+kB", re.M)


def parse_proc_stat(raw: str) -> Dict[str, object]:
    """Parse /proc/<pid>/stat safely: everything AFTER the last ')' is fixed-width."""
    comm_end = raw.rindex(")")  # ValueError => malformed: caller treats as missing
    comm_start = raw.rindex("(", 0, comm_end)
    pid_str = raw[: raw.find(" ")].strip()
    fields = raw[comm_end + 2 :].split()
    # fields[0] == field 3 (state); field N -> index N-3
    return {
        "pid": int(pid_str),
        "comm": raw[comm_start + 1 : comm_end],
        "state": fields[0],
        "ppid": int(fields[1]),
        "pgid": int(fields[2]),
        "utime": int(fields[11]),   # field 14
        "stime": int(fields[12]),   # field 15
        "starttime": int(fields[19]),  # field 22
        "rss_pages": int(fields[21]),  # field 24
    }


def read_proc_stat(pid: int) -> Optional[Dict[str, object]]:
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, OSError):
        return None
    try:
        return parse_proc_stat(raw)
    except (ValueError, IndexError):
        return None


def read_rss_kb(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    m = _STAT_VMRSS_RE.search(text)
    return int(m.group(1)) if m else None


def proc_starttime(pid: int) -> Optional[int]:
    stat = read_proc_stat(pid)
    return int(stat["starttime"]) if stat else None  # type: ignore[index]


def pgid_exists(pgid: int) -> bool:
    if not pgid:
        return False
    try:
        os.kill(-int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError, OSError):
        return False
    return True


def pid_exists(pid: int) -> bool:
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace"):
            pass
        return True
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError):
        return False


def read_tail(path, n_bytes: int) -> str:
    """Read only the LAST n_bytes of a file (never the full log)."""
    if not path:
        return ""
    p = Path(path)
    if not p.exists() or n_bytes <= 0:
        return ""
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            fh.seek(max(0, size - n_bytes))
            chunk = fh.read(n_bytes)
    except OSError:
        return ""
    return chunk.decode("utf-8", "replace")


def _file_size_mtime(path) -> tuple:
    try:
        st = os.stat(path)
        return int(st.st_size), float(st.st_mtime)
    except (FileNotFoundError, OSError):
        return 0, 0.0


@dataclass
class Observation:
    ts: float
    pid_exists: bool = False
    pgrp_exists: Optional[bool] = None
    proc_state: Optional[str] = None
    identity_ok: Optional[bool] = None  # None = not evaluable this tick
    returncode: Optional[int] = None
    exited: bool = False
    lost_reason: Optional[str] = None
    elapsed_s: float = 0.0
    cpu_pct: Optional[float] = None
    rss_kb: Optional[int] = None
    rss_delta_kb: Optional[int] = None
    stdout_bytes: int = 0
    stdout_delta: int = 0
    stderr_bytes: int = 0
    stderr_delta: int = 0
    markers_changed: bool = False
    markers: Dict[str, float] = field(default_factory=dict)
    checkpoint_match: Optional[str] = None
    deadline_exceeded: bool = False
    deadline_remaining_s: Optional[float] = None
    quiet_s: float = 0.0

    def evidence(self) -> Dict[str, object]:
        """Compact deterministic evidence dict (reused by gates/packets)."""
        return {
            "pid": None,
            "pid_exists": self.pid_exists,
            "elapsed_s": round(self.elapsed_s, 3),
            "quiet_s": round(self.quiet_s, 3),
            "cpu_pct": None if self.cpu_pct is None else round(self.cpu_pct, 1),
            "rss_kb": self.rss_kb,
            "stdout_delta": self.stdout_delta,
            "stderr_delta": self.stderr_delta,
            "markers_changed": self.markers_changed,
            "checkpoint": self.checkpoint_match,
            "deadline_remaining_s": None
            if self.deadline_remaining_s is None
            else round(self.deadline_remaining_s, 3),
            "exited": self.exited,
            "returncode": self.returncode,
        }


def observe(record, policy, now: float) -> Observation:
    """One deterministic observation of a job. Mutates only bookkeeping fields
    on the record (activity timestamps, cpu baseline, byte counters).

    Returns next-tick-carrying facts; NEVER calls a model.
    """
    obs = Observation(ts=now, elapsed_s=max(0.0, now - record.started_at))

    stat = read_proc_stat(record.pid) if record.pid else None
    exit_rec = None

    if stat is None or stat.get("state") == "Z":  # type: ignore[union-attr]
        # pid gone, or zombie awaiting its holder's reap -> consult exit record
        exit_rec = None
        if record.exit_file:
            try:
                import json as _json

                with open(record.exit_file, "r", encoding="utf-8") as fh:
                    exit_rec = _json.load(fh)
            except Exception:
                exit_rec = None
        rc = None
        if isinstance(exit_rec, dict) and exit_rec.get("returncode") is not None:
            rc = int(exit_rec["returncode"])
        obs.pid_exists = stat is not None
        if rc is not None:
            obs.exited = True
            obs.returncode = rc
            obs.proc_state = "Z" if stat else "gone"
            record.missing_since = None
        else:
            # exit record not (yet) available: hold; escalate to LOST after grace
            if record.missing_since is None:
                record.missing_since = now
            if now - record.missing_since >= policy.exit_file_grace_s:
                obs.lost_reason = "pid_gone_no_exit"
            obs.proc_state = "Z" if stat else "gone"
    else:
        obs.pid_exists = True
        obs.proc_state = str(stat.get("state"))
        obs.identity_ok = int(stat["starttime"]) == int(record.proc_starttime or -1)  # type: ignore[arg-type]
        obs.pgrp_exists = pgid_exists(record.pgid or 0)
        record.missing_since = None

        # cpu delta over the observation window (utime+stime, fields 14/15)
        ticks = int(stat["utime"]) + int(stat["stime"])  # type: ignore[arg-type]
        if record.cpu_prev_ts is not None and now > record.cpu_prev_ts:
            window = now - record.cpu_prev_ts
            delta = ticks - int(record.cpu_prev_ticks or 0)
            obs.cpu_pct = max(0.0, delta * 100.0 / (CLK_TCK * window))
        elif obs.elapsed_s > 0.001:
            obs.cpu_pct = max(0.0, ticks * 100.0 / (CLK_TCK * obs.elapsed_s))
        record.cpu_prev_ticks = ticks
        record.cpu_prev_ts = now
        record.last_cpu_pct = obs.cpu_pct

        # rss
        obs.rss_kb = read_rss_kb(record.pid) if record.pid else None
        if obs.rss_kb is not None:
            if record.prev_rss_kb is not None:
                obs.rss_delta_kb = obs.rss_kb - record.prev_rss_kb
            record.prev_rss_kb = obs.rss_kb
            record.rss_kb = obs.rss_kb

    # stdout/stderr growth (byte counts + mtimes, last 64KB tails only read for checkpoints)
    size_o, mtime_o = _file_size_mtime(record.stdout_path)
    size_e, mtime_e = _file_size_mtime(record.stderr_path)
    obs.stdout_bytes = size_o
    obs.stderr_bytes = size_e
    obs.stdout_delta = max(0, size_o - int(record.stdout_bytes or 0))
    obs.stderr_delta = max(0, size_e - int(record.stderr_bytes or 0))
    grew = (
        obs.stdout_delta > 0
        or obs.stderr_delta > 0
        or mtime_o > float(record.stdout_mtime or 0.0)
        or mtime_e > float(record.stderr_mtime or 0.0)
    )
    record.stdout_bytes, record.stdout_mtime = size_o, mtime_o
    record.stderr_bytes, record.stderr_mtime = size_e, mtime_e
    if grew and (obs.stdout_delta or obs.stderr_delta or mtime_o or mtime_e):
        record.last_output_at = now
        record.last_activity_at = now
        record.quiet_since = None

    # filesystem/activity markers (config: watched file mtimes)
    for marker in record.activity_markers or []:
        _, mt = _file_size_mtime(marker)
        obs.markers[marker] = mt
        if mt and mt != float(record.last_marker_mtimes.get(marker, 0.0)):
            record.last_marker_mtimes[marker] = mt
            obs.markers_changed = True
            record.last_activity_at = now
            record.quiet_since = None

    # checkpoint regex against the recent 64KB tail of stdout+stderr
    tail_n = int(getattr(policy, "capture_tail_bytes", 65536))
    text = read_tail(record.stdout_path, tail_n) + "\n" + read_tail(record.stderr_path, tail_n)
    best: Optional[str] = None
    for pattern in record.checkpoint_patterns or []:
        try:
            for m in re.finditer(pattern, text):
                best = m.group(0)
        except re.error:
            continue
    obs.checkpoint_match = best

    # deadline
    if record.deadline is not None:
        obs.deadline_remaining_s = record.deadline - now
        obs.deadline_exceeded = now >= record.deadline

    # quiet age (since the last deterministic activity)
    since = record.last_activity_at or record.started_at
    obs.quiet_s = max(0.0, now - since)
    if obs.quiet_s > 0.0 and record.quiet_since is None:
        record.quiet_since = since

    return obs