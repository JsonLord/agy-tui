"""P20-D — Health / readiness surface.

A dependency-free HTTP endpoint (stdlib ``http.server``) that reports the
container-friendly health of the AGY runtime. It NEVER exposes secrets —
credential presence is reported only as a boolean, never a value.

    AGY runtime:          import of the production supervisor + release sha
    OpenCode:             reachability of the worker backend
    state-root:           writable check
    cloud planner:        tool-free transport configured? (boolean only)
    worker provider:      OpenCode backend configured (url + reachability)
    release sha:          from release_info

Run:
    python -m app.health --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import runtime_config
from app.release_info import collect


def health() -> dict:
    """Compute the health payload (safe to serialize to JSON)."""
    # Compute each probe ONCE (some are network probes — a health endpoint
    # must not multiply its own latency).
    opencode_ok = runtime_config.opencode_reachable(timeout=2.0)
    planner_cfg = runtime_config.cloud_planner_configured()
    payload = {
        "status": "ok",
        "release": None,
        "runtime": {"supervisor_importable": False, "error": None},
        "opencode": {
            "url": runtime_config.opencode_server_url(),
            "reachable": opencode_ok,
        },
        "state_root": {
            "path": str(runtime_config.state_root()),
            "writable": False,
        },
        "cloud_planner_configured": planner_cfg,
        "capabilities": [],
        "secrets": {
            "cloud_planner_configured": planner_cfg,
            "opencode_reachable": opencode_ok,
        },
    }

    # release identity
    try:
        rel = collect()
        payload["release"] = {
            "release_sha": rel.release_sha,
            "schema_version": rel.schema_version,
            "root": str(rel.root),
            "dirty": rel.dirty,
        }
        payload["capabilities"] = list(rel.capabilities)
    except Exception as exc:  # pragma: no cover - defensive
        payload["release"] = {"error": str(exc)}

    # supervisor import
    try:
        import app.orchestration.project_supervisor  # noqa: F401

        payload["runtime"]["supervisor_importable"] = True
    except Exception as exc:
        payload["runtime"]["error"] = str(exc)

    # state root writable
    try:
        sr = runtime_config.state_root()
        sr.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=sr, delete=True)
        probe.write(b"probe")
        probe.close()
        payload["state_root"]["writable"] = True
    except Exception:
        payload["state_root"]["writable"] = False

    # overall status
    if not payload["runtime"]["supervisor_importable"] or not payload["state_root"]["writable"]:
        payload["status"] = "degraded"
    return payload


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if self.path in ("/health", "/healthz", "/readyz", "/"):
            payload = health()
            code = 200 if payload["status"] == "ok" else 503
            self._send(code, payload)
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, format, *args) -> None:  # silence default logging
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AGY health endpoint")
    ap.add_argument("--host", default=os.environ.get("AGY_HEALTH_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("AGY_HEALTH_PORT", "8080")))
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"AGY health on {args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
