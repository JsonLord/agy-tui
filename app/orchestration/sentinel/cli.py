"""sentinel-cli — thin Unix-socket client for the AGY process sentinel.

This CLI/socket is the primary interface (humans + AGY): spawn, yield to the
local watcher, and re-engage only via events / wake packets — never via a
status-polling loop.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional


def send_request(socket_path: str, payload: Dict[str, Any],
                 timeout: float = 30.0) -> Dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        chunks = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    finally:
        try:
            client.close()
        except OSError:
            pass
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("empty response from sentinel daemon")
    return json.loads(raw.decode("utf-8"))


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="sentinel-cli",
                                  description="AGY process sentinel CLI (Unix socket)")
    ap.add_argument("--socket", required=True, help="daemon Unix socket path")
    ap.add_argument("--json", action="store_true", help="print raw JSON response")
    sub = ap.add_subparsers(dest="op", required=True)

    sp = sub.add_parser("spawn", help="spawn a monitored process, print job_id")
    sp.add_argument("--cwd", default=None)
    sp.add_argument("--deadline", type=float, default=None, dest="deadline_s")
    sp.add_argument("--checkpoint", action="append", default=[],
                    help="checkpoint regex over stdout/stderr tails (repeatable)")
    sp.add_argument("--marker", action="append", default=[],
                    help="filesystem activity marker path (repeatable)")
    sp.add_argument("--verify-returncode", type=int, default=None)
    sp.add_argument("--command", required=True,
                    help="shell-style command string (shlex-split)")

    sn = sub.add_parser("snapshot", help="read-only diagnostic snapshot")
    sn.add_argument("job_id")
    cn = sub.add_parser("cancel", help="cancel a job (SIGTERM -> grace -> SIGKILL)")
    cn.add_argument("job_id")
    sm = sub.add_parser("summary", help="job metrics summary line(s)")
    sm.add_argument("job_id", nargs="?", default=None)
    sub.add_parser("list", help="list jobs")
    sub.add_parser("health", help="daemon health document")
    sub.add_parser("metrics", help="global sentinel metrics")
    ev = sub.add_parser("events", help="tail the event JSONL")
    ev.add_argument("--job", default=None)
    ev.add_argument("-n", type=int, default=50)
    sub.add_parser("recover", help="last restart-recovery summary")
    sub.add_parser("shutdown", help="graceful daemon shutdown")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    payload: Dict[str, Any] = {"op": args.op}
    if args.op == "spawn":
        verify = None
        if args.verify_returncode is not None:
            verify = {"expect_returncode": args.verify_returncode}
        payload.update({
            "command": args.command,
            "cwd": args.cwd,
            "deadline_s": args.deadline_s,
            "checkpoint_patterns": args.checkpoint,
            "activity_markers": args.marker,
            "verify": verify,
        })
    elif args.op in ("snapshot", "cancel"):
        payload["job_id"] = args.job_id
    elif args.op == "summary" and args.job_id:
        payload["job_id"] = args.job_id
    elif args.op == "events":
        payload["job_id"] = args.job
        payload["n"] = args.n
    try:
        resp = send_request(args.socket, payload)
    except (FileNotFoundError, ConnectionRefusedError, RuntimeError) as exc:
        print(f"sentinel-cli: cannot reach daemon at {args.socket}: {exc}",
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(resp, indent=1, sort_keys=True))
        return 0 if resp.get("ok") else 1
    if not resp.get("ok"):
        print(f"sentinel-cli: {resp.get('error')}", file=sys.stderr)
        return 1
    if args.op == "spawn":
        print(resp["job_id"])
    elif args.op == "summary":
        if "summary" in resp:
            print(resp["summary"])
        else:
            for line in resp.get("summaries", []):
                print(line)
    elif args.op == "list":
        for job in resp.get("jobs", []):
            print(f"{job['job_id']} state={job['state']} pid={job['pid']}")
    elif args.op == "events":
        for event in resp.get("events", []):
            print(json.dumps(event, separators=(",", ":")))
    elif args.op == "metrics":
        for key in sorted(resp.get("metrics", {})):
            print(f"{key}={resp['metrics'][key]}")
    elif args.op in ("recover", "snapshot", "cancel", "health"):
        print(json.dumps(resp, indent=1, sort_keys=True))
    else:
        print(json.dumps(resp))
    return 0


if __name__ == "__main__":
    sys.exit(main())