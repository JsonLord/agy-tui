"""P21 — Project supervisor HTTP frontend (the ONE exposed external API).

Runs the canonical :class:`ProjectSupervisor` lifecycle over HTTP so a
deployment (e.g. a Hugging Face Space) can expose a single authenticated
frontend on its public port. Design invariants (P21-C, non-negotiable):

  * EVERY mutating request requires the API key (Authorization: Bearer
    <key> or X-API-Key header), compared with ``secrets.compare_digest``.
    No anonymous project actions.
  * The worker-control backend (OpenCode) is NEVER proxied here — it
    stays on loopback inside the runtime. This frontend speaks only the
    supervisor's canonical, gated API (the worker surface is not
    reachable from the public port).
  * ``POST /project/advance`` performs ONE deterministic supervisor
    advance and returns. There are NO model wait loops in this process —
    the request ends as soon as the supervisor parks (human input
    required / workers running / cloud reentry required / terminal).
  * Auth failure returns 401 and NOTHING else (no project data leaks).

Stdlib only (``http.server``) — no new dependency.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# A project's durable state lives under the release root.
_PROJECT_ROOT = "projects"


class SpaceApiError(Exception):
    """HTTP-level failure with a status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class SpaceServer:
    """The authenticated project-supervisor frontend.

    ``supervisor_factory`` builds (or reopens) a supervisor per project —
    the SAME production wiring as the CLI (``app.main`` builds it there);
    tests inject a scripted planner via the factory.
    """

    def __init__(
        self,
        *,
        api_key: str,
        supervisor_factory: Callable[[str, str], Any],
        release_info: Callable[[], dict] | None = None,
    ) -> None:
        self._api_key = api_key
        self._factory = supervisor_factory
        self._release_info = release_info
        self._lock = threading.Lock()  # one advance at a time per process
        self._server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ #
    # HTTP plumbing
    # ------------------------------------------------------------------ #
    def serve_forever(self, host: str = "0.0.0.0", port: int = 7860) -> None:
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _check_auth(self, headers: dict[str, str]) -> None:
        provided = headers.get("Authorization", "")
        if provided.startswith("Bearer "):
            provided = provided[len("Bearer ") :].strip()
        provided = provided or headers.get("X-API-Key", "")
        if not self._api_key or not secrets.compare_digest(
            provided.encode("utf-8"), self._api_key.encode("utf-8")
        ):
            raise SpaceApiError(401, "unauthorized: a valid API key is required")

    # ------------------------------------------------------------------ #
    # Endpoint handlers (each: request dict -> response dict | None)
    # ------------------------------------------------------------------ #
    def _info(self) -> dict:
        from app import runtime_config
        from app.release_info import collect

        rel = collect()
        return {
            "release": {
                "release_sha": rel.release_sha,
                "schema_version": rel.schema_version,
            },
            "capabilities": {
                "project_lifecycle": True,
                "cloud_planner_configured": runtime_config.cloud_planner_configured(),
                "opencode_reachable": runtime_config.opencode_reachable(),
                "opencode_public": False,  # NEVER proxied — loopback only
            },
        }

    def _health(self) -> dict:
        import os

        from app import health as _health_mod
        from app.space_persistence import (
            PersistenceError,
            readiness_stamp,
            resolve_persistence,
        )

        payload = _health_mod.health()
        # P21-B: stamp persistence so no client can mistake ephemeral
        # state for durable (demo mode is visible in the health payload).
        state_root = os.environ.get("AGY_STATE_ROOT", "")
        stamp: dict = {}
        if state_root:
            try:
                mode = resolve_persistence(
                    state_root,
                    require_persistent=os.environ.get("AGY_SPACE_MODE", "production").strip() != "demo",
                    persistent_marker=os.environ.get("AGY_PERSISTENT_MARKER", "/data"),
                )
                stamp = readiness_stamp(mode)
            except PersistenceError as exc:
                stamp = {"state_persistence": "UNKNOWN", "error": str(exc)}
        return {"ok": payload["status"] == "ok", "status": payload["status"],
                "persistence": stamp}

    def _start(self, body: dict) -> dict:
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise SpaceApiError(400, "prompt is required")
        pid = str(body.get("project_id", "")).strip() or _new_project_id()
        if not _ID_RE.match(pid):
            raise SpaceApiError(400, "project_id must be lowercase [a-z0-9-]")
        sup = self._factory("projects", pid)
        sup.create_project(initial_prompt=prompt, objective=str(body.get("objective", prompt)))
        return {"project_id": pid, "state": sup.state().value}

    def _status(self, body: dict) -> dict:
        pid = self._project_id(body)
        sup = self._factory("projects", pid)
        return sup.get_project()

    def _advance(self, body: dict) -> dict:
        pid = self._project_id(body)
        # One deterministic advance, serialized process-wide (no
        # concurrent state mutations; no wait loops — returns immediately).
        with self._lock:
            sup = self._factory("projects", pid)
            result = sup.advance_project()
        return {"project_id": pid, "advance": result, "state": sup.state().value}

    def _answer(self, body: dict) -> dict:
        pid = self._project_id(body)
        text = str(body.get("text", "")).strip()
        if not text:
            raise SpaceApiError(400, "text is required")
        # Actor is ALWAYS a human on this surface — a non-human (model /
        # planner) actor is REJECTED, never coerced to a human identity
        # (P15 non-negotiable: model-originated responses cannot
        # masquerade as human).
        actor = str(body.get("actor", "human:web"))
        if not actor.startswith("human:"):
            raise SpaceApiError(403, "actor must be a human:* identity (model/planner actors are rejected)")
        sup = self._factory("projects", pid)
        result = sup.submit_human_input(text=text, actor=actor)
        return {"project_id": pid, "answer": result}

    def _cancel(self, body: dict) -> dict:
        pid = self._project_id(body)
        sup = self._factory("projects", pid)
        reason = str(body.get("reason", "cancelled by user"))
        sup.cancel_project(reason=reason)
        return {"project_id": pid, "state": sup.state().value}

    def _approve(self, body: dict) -> dict:
        """G4 human decision — the canonical P15/P18 authority
        (``submit_g4_decision`` enforces human-only actors; a
        model/planner actor is rejected before any gate moves)."""
        pid = self._project_id(body)
        decision = str(body.get("decision", "")).upper().strip()
        if decision not in ("APPROVE", "NEEDS_CHANGES", "CANCEL"):
            raise SpaceApiError(400, "decision must be APPROVE|NEEDS_CHANGES|CANCEL")
        actor = str(body.get("actor", "human:web"))
        if not actor.startswith("human:"):
            raise SpaceApiError(403, "actor must be a human:* identity (model/planner actors are rejected)")
        sup = self._factory("projects", pid)
        status = sup.submit_g4_decision(
            decision=decision, feedback=str(body.get("feedback", "")),
            actor=actor, batch_id=body.get("batch_id"),
        )
        return {"project_id": pid, "g4_status": status, "state": sup.state().value}

    def _events(self, body: dict) -> dict:
        """Compact project event feed (P18-B: events feed UI/observability;
        they are NOT authority — canonical state is)."""
        pid = self._project_id(body)
        sup = self._factory("projects", pid)
        return {"project_id": pid, "events": sup.get_project().get("events", [])}

    # ------------------------------------------------------------------ #
    def _project_id(self, body: dict) -> str:
        pid = str(body.get("project_id", "")).strip()
        if not pid or not _ID_RE.match(pid):
            raise SpaceApiError(400, "project_id is required and must be [a-z0-9-]")
        return pid


def _new_project_id() -> str:
    import time
    return "proj-" + time.strftime("%Y%m%d-%H%M%S")


def _make_handler(space: SpaceServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args) -> None:  # noqa: A002 — base class signature
            pass

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SpaceApiError(400, f"invalid JSON body: {exc}")
            if not isinstance(parsed, dict):
                raise SpaceApiError(400, "JSON body must be an object")
            return parsed

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self._send(200, {"service": "agy-hybrid", "endpoints": [
                        "GET /health", "GET /info",
                        "POST /project/start", "POST /project/status",
                        "POST /project/advance", "POST /project/answer",
                        "POST /project/approve", "POST /project/cancel",
                        "POST /project/events",
                    ]})
                    return
                if path == "/health":
                    # Health is the ONLY unauthenticated endpoint (it
                    # reveals no project data, only capability booleans).
                    self._send(200, space._health())
                    return
                # Everything else requires auth.
                space._check_auth(dict(self.headers))
                if path == "/info":
                    self._send(200, space._info())
                    return
                raise SpaceApiError(404, f"unknown endpoint {path}")
            except SpaceApiError as exc:
                self._send(exc.status, {"error": exc.message})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                space._check_auth(dict(self.headers))
                body = self._read_body()
                if path == "/project/start":
                    self._send(201, space._start(body))
                elif path == "/project/status":
                    self._send(200, space._status(body))
                elif path == "/project/advance":
                    self._send(200, space._advance(body))
                elif path == "/project/answer":
                    self._send(200, space._answer(body))
                elif path == "/project/approve":
                    self._send(200, space._approve(body))
                elif path == "/project/events":
                    self._send(200, space._events(body))
                elif path == "/project/cancel":
                    self._send(200, space._cancel(body))
                else:
                    raise SpaceApiError(404, f"unknown endpoint {path}")
            except SpaceApiError as exc:
                self._send(exc.status, {"error": exc.message})
            except Exception as exc:  # surface canonical errors as 4xx/5xx
                from app.orchestration.project_supervisor import ProjectSupervisorError
                if isinstance(exc, ProjectSupervisorError):
                    self._send(409, {"error": f"project error: {exc}"})
                else:
                    self._send(500, {"error": f"internal error: {type(exc).__name__}"})

    return Handler


def main() -> None:
    """``python3 -m app.space_server`` — run the frontend."""
    import os

    api_key = os.environ.get("AGY_SPACE_API_KEY", "")
    host = os.environ.get("AGY_SPACE_HOST", "0.0.0.0")
    port = int(os.environ.get("AGY_SPACE_PORT", "7860"))
    state_root = os.environ.get("AGY_STATE_ROOT", "state")
    workdir = os.environ.get("AGY_RELEASE_ROOT", ".")

    def factory(prefix: str, pid: str):
        from app.main import _build_production_supervisor
        return _build_production_supervisor(state_root, pid, workdir)

    space = SpaceServer(
        api_key=api_key,
        supervisor_factory=factory,
    )
    print(f"AGY Space frontend on {host}:{port} (auth={'on' if api_key else 'OFF'})")
    space.serve_forever(host=host, port=port)


if __name__ == "__main__":
    main()
