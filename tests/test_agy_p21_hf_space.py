"""P21 — Hugging Face Space profile.

Zero live models, zero secrets, zero remote publishing. Proves:

 1. The auth-gated frontend: EVERY project action requires the API key
    (401 without; 200 with; health is the only unauthenticated read).
 2. The lifecycle is driven over HTTP with a SCRIPTED planner (0 cloud
    calls): start → status → advance → answer → advance → … → COMPLETE.
 3. Model-originated responses CANNOT masquerade as human: a non-
    ``human:*`` actor is rejected (403), never coerced.
 4. The worker-control backend is NEVER exposed: no opencode proxy route
    exists on the frontend (P21-A/C).
 5. P21-B persistence gate: persistent → durable; absent + not-demo →
    FATAL; absent + demo → explicit demo mode (never silently ephemeral).
 6. The space image (built on the P20 image) boots and serves /health.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.space_server import SpaceServer

DOCKER = shutil.which("docker")
ROOT = Path(__file__).resolve().parents[1]
API_KEY = "test-space-key"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_space(tmp_path: Path, *, api_key: str = API_KEY,
                 planner_script: tuple | None = None) -> tuple:
    """Boot a SpaceServer on an ephemeral port with a SCRIPTED planner
    (0 live cloud calls). Returns (space, port, planner)."""
    # Reuse the P18 supervisor-test fakes (the proven P14 planner API
    # shapes) — the space frontend is a thin HTTP layer over the SAME
    # ProjectSupervisor, so it gets the SAME deterministic fakes.
    from app.orchestration.cloud_batch import schemas as cb_schemas
    from tests.test_agy_p18_supervisor import (
        BASE, FakeRunner, ProjectSupervisor, SupervisorConfig,
        ScriptedPlanner, ro_plan)

    planner = ScriptedPlanner(
        batch_action=ro_plan(),
        reentry_script=[cb_schemas.FinalResult(success=True, summary="done")],
    )
    runner = FakeRunner()

    def factory(prefix: str, pid: str):
        cfg = SupervisorConfig(
            state_root=tmp_path, project_id=pid,
            planner_factory=lambda: planner, runner=runner, base_sha=BASE,
        )
        return ProjectSupervisor(cfg)

    space = SpaceServer(api_key=api_key, supervisor_factory=factory)
    port = _free_port()
    t = threading.Thread(target=space.serve_forever,
                         args=("127.0.0.1", port), daemon=True)
    t.start()
    # Wait until the listener is actually bound (thread start is async).
    import socket as _socket

    for _ in range(50):
        try:
            probe = _socket.create_connection(("127.0.0.1", port), timeout=0.5)
            probe.close()
            break
        except OSError:
            time.sleep(0.1)
    return space, port, planner


class _Client:
    def __init__(self, port: int, key: str | None = API_KEY):
        self.port = port
        self.key = key

    def req(self, method: str, path: str, body: dict | None = None,
            key: str | None = None, use_default: bool = True) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if use_default and key is None:
            key = API_KEY
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = json.dumps(body or {})
        conn.request(method, path, body=payload, headers=headers)
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = {"raw": data}
        return r.status, parsed


# --- 1. Auth gate --------------------------------------------------------- #

def test_no_auth_401_project_actions(tmp_path):
    space, port, _ = _start_space(tmp_path)
    c = _Client(port)
    try:
        # /health is the ONLY unauthenticated read.
        st, body = c.req("GET", "/health", key=None, use_default=False)
        assert st == 200, body
        # Every project action requires the key.
        for path in ("/project/start", "/project/status",
                     "/project/advance", "/project/answer",
                     "/project/approve", "/project/cancel",
                     "/project/events"):
            st, _ = c.req("POST", path, {"project_id": "x"},
                          key=None, use_default=False)
            assert st == 401, (path, st)
        st, _ = c.req("GET", "/info", key=None, use_default=False)
        assert st == 401
    finally:
        space.shutdown()


def test_correct_key_allowed_wrong_key_rejected(tmp_path):
    space, port, _ = _start_space(tmp_path)
    c = _Client(port)
    try:
        st, _ = c.req("GET", "/info")  # default correct key
        assert st == 200
        st, _ = c.req("GET", "/info", key="wrong-key")
        assert st == 401
    finally:
        space.shutdown()


# --- 2. Lifecycle over HTTP (0 cloud) ------------------------------------ #

def test_full_lifecycle_over_http_scripted(tmp_path):
    space, port, planner = _start_space(tmp_path)
    c = _Client(port)
    try:
        st, body = c.req("POST", "/project/start",
                         {"prompt": "build the thing",
                          "project_id": "web-proj"})
        assert st == 201, body
        assert body["project_id"] == "web-proj"

        st, body = c.req("POST", "/project/status", {"project_id": "web-proj"})
        assert st == 200, body

        # NEW -> CLARIFY (1 scripted cloud call).
        st, body = c.req("POST", "/project/advance", {"project_id": "web-proj"})
        assert st == 200, body

        # Human answer over HTTP.
        st, body = c.req("POST", "/project/answer",
                         {"project_id": "web-proj", "text": "yes, build"})
        assert st == 200, body

        # READY_TO_PLAN -> BATCH -> BATCH_RUNNING (scripted batch).
        st, body = c.req("POST", "/project/advance", {"project_id": "web-proj"})
        assert st == 200, body

        # Read-only batch runs (fake workers) -> REENTRY_READY.
        st, body = c.req("POST", "/project/advance", {"project_id": "web-proj"})
        assert st == 200, body

        # REENTRY_READY -> FINAL -> COMPLETE (scripted reentry).
        st, body = c.req("POST", "/project/advance", {"project_id": "web-proj"})
        assert st == 200, body
        assert body["state"] == "COMPLETE", body
    finally:
        space.shutdown()


# --- 3. Model cannot masquerade as human --------------------------------- #

def test_model_actor_rejected_not_coerced(tmp_path):
    space, port, _ = _start_space(tmp_path)
    c = _Client(port)
    try:
        c.req("POST", "/project/start", {"prompt": "p",
                                         "project_id": "masq"})
        c.req("POST", "/project/advance", {"project_id": "masq"})  # -> CLARIFY
        # A model/planner actor must be REJECTED (403), not coerced to human.
        st, body = c.req("POST", "/project/answer",
                         {"project_id": "masq", "text": "yes",
                          "actor": "planner"})
        assert st == 403, (st, body)
        st, body = c.req("POST", "/project/approve",
                         {"project_id": "masq", "decision": "APPROVE",
                          "actor": "model:opus"})
        assert st == 403, (st, body)
    finally:
        space.shutdown()


# --- 4. Worker backend never exposed ------------------------------------- #

def test_opencode_worker_backend_not_exposed(tmp_path):
    space, port, _ = _start_space(tmp_path)
    c = _Client(port)
    try:
        # No opencode / worker-control route exists on the frontend.
        for path in ("/opencode", "/worker", "/session", "/api/session",
                     "/opencode/session"):
            st, _ = c.req("GET", path)
            assert st == 404, (path, st)
        # /info explicitly reports opencode is not public.
        st, body = c.req("GET", "/info")
        assert body["capabilities"]["opencode_public"] is False, body
    finally:
        space.shutdown()


# --- 5. P21-B persistence gate ------------------------------------------- #

def test_persistence_gate_durable_demo_and_fatal(tmp_path):
    from app.space_persistence import (
        PersistenceError, readiness_stamp, resolve_persistence)

    # Persistent marker present + writable -> durable mode.
    data = tmp_path / "data"
    data.mkdir()
    durable_root = data / "agy"
    durable_root.mkdir(parents=True)
    m = resolve_persistence(str(durable_root), require_persistent=True,
                            persistent_marker=str(data))
    assert m.mode == "durable" and m.persistent
    stamp = readiness_stamp(m)
    assert stamp["state_persistence"] == "durable"
    assert stamp["persists_across_restarts"] is True

    # Absent + demo (require_persistent=False) -> explicit disposable
    # mode, stamped DISPOSABLE (never silently durable).
    demo = tmp_path / "ephemeral-state"
    m = resolve_persistence(str(demo), require_persistent=False,
                            persistent_marker=str(tmp_path / "no-such-mount"))
    assert m.mode == "disposable-demo" and not m.persistent
    assert readiness_stamp(m)["state_persistence"] == "DISPOSABLE"

    # Production (require_persistent=True) on a disposable root -> FATAL.
    with pytest.raises(PersistenceError):
        resolve_persistence(str(tmp_path / "disposable"),
                            require_persistent=True,
                            persistent_marker=str(tmp_path / "no-such-mount"))
    # Production with no durable mount at all -> FATAL.
    with pytest.raises(PersistenceError):
        resolve_persistence(str(tmp_path / "missing"),
                            require_persistent=True,
                            persistent_marker=str(tmp_path / "no-such-mount"))


# --- 6. Space image boots + serves /health ------------------------------- #

@pytest.mark.skipif(not DOCKER, reason="docker unavailable")
def test_space_image_boots_and_serves_health():
    from app.release_info import collect
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    img = "agy-hybrid:space-test"
    b = subprocess.run(
        ["docker", "build", "-t", img,
         "--build-arg", f"AGY_RELEASE_SHA={head}",
         "-f", str(ROOT / "container/Dockerfile.space"), str(ROOT)],
        capture_output=True, text=True, timeout=1800)
    assert b.returncode == 0, b.stderr[-2000:]

    vol = Path("/tmp/agy-space-smoke")
    shutil.rmtree(vol, ignore_errors=True)
    vol.mkdir(parents=True)
    name = "agy-space-boot"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-v", f"{vol}:/data",
         "-e", "AGY_STATE_ROOT=/data/agy",
         "-e", "SPACE_API_KEY=space-test-key", "-p", "0:7860", img],
        check=True, capture_output=True)
    try:
        port = None
        for _ in range(40):
            m = subprocess.run(["docker", "port", name, "7860/tcp"],
                               capture_output=True, text=True)
            if m.stdout.strip():
                port = m.stdout.strip().splitlines()[-1].rsplit(":", 1)[-1]
                break
            time.sleep(2)
        assert port, "frontend port never published"
        import urllib.request
        payload = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=10) as r:
                    payload = json.loads(r.read())
                if payload is not None:
                    break
            except Exception:
                pass
            time.sleep(2)
        assert payload and payload.get("ok"), payload
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
