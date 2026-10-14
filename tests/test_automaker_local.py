"""Isolated automaker-local tests against a fake localhost HTTP server.

No live Automaker instance is required; nothing here is live-device proof.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from app.automaker_local import mcp as mcp_mod
from app.automaker_local.client import AutomakerClient
from app.automaker_local.policy import (
    CONTROL,
    READ,
    AutomakerError,
    environment,
    resolve_api_key,
    resolve_base_url,
    tool_class,
)

KEY = "am-test-secret-4f7b1c9d2e"
EXPECTED_TOOLS = {
    "automaker_health",
    "automaker_projects",
    "automaker_jobs",
    "automaker_job_status",
    "automaker_running_agents",
    "automaker_autonomy_status",
    "automaker_backlog_status",
    "automaker_job_create",
    "automaker_job_update",
    "automaker_worker_start",
    "automaker_worker_stop",
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        headers = {name.casefold(): value for name, value in self.headers.items()}
        body = json.loads(raw) if raw else None
        self.server.records.append(
            {
                "method": method,
                "path": parsed.path,
                "query": parsed.query,
                "headers": headers,
                "body": body,
            }
        )
        route = self.server.routes.get((method, parsed.path))
        if route is None:
            self._respond(404, {"success": False, "error": "not found"})
            return
        result = route(headers, body, parsed.query)
        if callable(result):
            result()
            result = (200, {"success": True})
        status, payload = result[0], result[1]
        kwargs = result[2] if len(result) > 2 else {}
        self._respond(status, payload, **kwargs)

    def _respond(self, status, payload, raw=None, content_type="application/json"):
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(ThreadingHTTPServer):
    def handle_error(self, *args):
        pass


class Fake:
    def __init__(self, server, base_url):
        self.server = server
        self.base_url = base_url

    def route(self, method, path, payload, status=200, **kwargs):
        self.server.routes[(method, path)] = lambda h, b, q: (status, payload, kwargs)

    def action(self, method, path, fn):
        self.server.routes[(method, path)] = fn

    @property
    def records(self):
        return self.server.records

    def client(self, **kwargs):
        kwargs.setdefault("timeout", 5.0)
        return AutomakerClient(self.base_url, KEY, **kwargs)


@pytest.fixture
def fake():
    server = _Server(("127.0.0.1", 0), _Handler)
    server.routes = {}
    server.records = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Fake(server, f"http://127.0.0.1:{server.server_address[1]}")
    server.shutdown()
    server.server_close()


@pytest.fixture
def tool_client(fake):
    mcp_mod._client = fake.client()
    yield fake
    mcp_mod._client = None


def call_tool(name, **args):
    return asyncio.run(getattr(mcp_mod, name)(**args))


def codes(result):
    return json.dumps(result)


# --- base URL policy ---------------------------------------------------------


def test_default_base_url():
    assert resolve_base_url("") == "http://127.0.0.1:3008"
    assert resolve_base_url(None) == "http://127.0.0.1:3008"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3008",
        "http://localhost:3008",
        "http://[::1]:3008",
        "http://127.0.0.1",
    ],
)
def test_loopback_hosts_accepted(url):
    assert resolve_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.10:3008",  # LAN
        "http://10.0.0.5:3008",
        "http://0.0.0.0:3008",  # wildcard
        "https://automaker.example.com",  # public https
        "http://user:pass@127.0.0.1:3008",  # credentials
        "http://127.0.0.1:3008/proxy",  # path
        "http://127.0.0.1:3008?a=b",  # query
        "http://[::2]:3008",  # non-loopback v6
        "127.0.0.1:3008",  # missing scheme
        "ftp://localhost:3008",  # wrong scheme
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(AutomakerError) as error:
        resolve_base_url(url)
    assert error.value.code == "LOCAL_CONFIGURATION_REQUIRED"


def test_missing_api_key_fails():
    with pytest.raises(AutomakerError) as error:
        resolve_api_key("")
    assert error.value.code == "LOCAL_CONFIGURATION_REQUIRED"
    with pytest.raises(AutomakerError):
        resolve_api_key("x" * 500)


def test_missing_api_key_blocks_tool(monkeypatch):
    monkeypatch.delenv("AUTOMAKER_BASE_URL", raising=False)
    monkeypatch.delenv("AUTOMAKER_API_KEY", raising=False)
    mcp_mod._client = None
    result = call_tool("automaker_health")
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "LOCAL_CONFIGURATION_REQUIRED"
    mcp_mod._client = None


def test_environment_requires_both(monkeypatch):
    monkeypatch.setenv("AUTOMAKER_BASE_URL", "http://0.0.0.0:3008")
    monkeypatch.setenv("AUTOMAKER_API_KEY", KEY)
    with pytest.raises(AutomakerError):
        environment()


# --- secret hygiene ----------------------------------------------------------


def test_key_never_in_error_responses(tool_client):
    tool_client.route("GET", "/api/health", {"error": "denied"}, status=401)
    result = call_tool("automaker_health")
    assert result["error_code"] == "AUTOMAKER_AUTH_FAILED"
    assert KEY not in codes(result)


def test_key_never_in_success_responses(tool_client):
    tool_client.route(
        "GET", "/api/health", {"status": "ok", "version": "1.0.0", "timestamp": "now"}
    )
    result = call_tool("automaker_health")
    assert KEY not in codes(result)
    assert result["status"] == "ok"


def test_both_auth_headers_sent(tool_client):
    tool_client.route(
        "POST", "/api/auto-mode/status", {"success": True, "isRunning": False}
    )
    call_tool("automaker_autonomy_status")
    record = tool_client.records[-1]
    assert record["headers"]["x-api-key"] == KEY
    assert record["headers"]["authorization"] == "Bearer " + KEY
    assert record["headers"]["content-type"] == "application/json"
    assert KEY not in json.dumps(dict(tool_client.records[-1]["body"] or {}))


# --- READ tools --------------------------------------------------------------


def test_health_success(tool_client):
    tool_client.route(
        "GET",
        "/api/health",
        {"status": "ok", "version": "1.0.0", "timestamp": "2026-10-14T00:00:00Z"},
    )
    result = call_tool("automaker_health")
    assert result["status"] == "ok"
    assert result["version"] == "1.0.0"
    assert "correlation_id" in result
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("GET", "/api/health")


def test_projects_normalization(tool_client):
    tool_client.route(
        "GET",
        "/api/projects/overview",
        {
            "success": True,
            "projects": [
                {
                    "projectId": "p1",
                    "projectName": "demo",
                    "projectPath": "C:\\repos\\demo",
                    "healthStatus": "active",
                    "featureCounts": {"pending": 2, "running": 1, "total": 3},
                    "totalFeatures": 3,
                    "isAutoModeRunning": True,
                    "activeBranch": "feat/x",
                    "unreadNotificationCount": 7,
                    "rawBlob": {"leak": "x" * 9000},
                }
            ],
            "aggregate": {"huge": "blob"},
            "generatedAt": "2026-10-14T00:00:00Z",
        },
    )
    result = call_tool("automaker_projects")
    project = result["projects"][0]
    assert set(project) == {
        "project_id",
        "project_name",
        "project_path",
        "health_status",
        "total_features",
        "feature_counts",
        "is_auto_mode_running",
        "active_branch",
        "last_activity_at",
    }
    assert project["feature_counts"] == {"pending": 2, "running": 1, "total": 3}
    assert result["truncated"] is False
    assert "x" * 9000 not in codes(result)


def test_jobs_normalization_and_endpoint(tool_client):
    tool_client.route(
        "POST",
        "/api/features/list",
        {
            "success": True,
            "features": [
                {
                    "id": "F001",
                    "title": "Build API",
                    "status": "pending",
                    "priority": 2,
                    "category": "backend",
                    "dependencies": ["F000"],
                    "branchName": "feat/api",
                    "passes": False,
                    "description": "long" * 5000,
                }
            ],
        },
    )
    result = call_tool("automaker_jobs", project_path="C:\\repos\\demo")
    job = result["jobs"][0]
    assert job["id"] == "F001"
    assert job["status"] == "pending"
    assert job["priority"] == 2
    assert job["dependencies"] == ["F000"]
    assert job["branch_name"] == "feat/api"
    assert "description" not in job
    assert "long" * 5000 not in codes(result)
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("POST", "/api/features/list")
    assert record["body"] == {"projectPath": "C:\\repos\\demo"}


def test_job_status_single(tool_client):
    tool_client.route(
        "POST",
        "/api/features/get",
        {
            "success": True,
            "feature": {
                "id": "F002",
                "title": "Fix UI",
                "status": "failed",
                "error": "boom",
            },
        },
    )
    result = call_tool(
        "automaker_job_status", project_path="/srv/app", feature_id="F002"
    )
    assert result["job"]["id"] == "F002"
    assert result["job"]["error"] == "boom"
    assert result["job"]["description"] is None
    record = tool_client.records[-1]
    assert record["body"] == {"projectPath": "/srv/app", "featureId": "F002"}


def test_running_agents(tool_client):
    tool_client.route(
        "GET",
        "/api/running-agents",
        {
            "success": True,
            "runningAgents": [
                {
                    "featureId": "F003",
                    "projectPath": "/srv/app",
                    "projectName": "app",
                    "isAutoMode": True,
                    "title": "Implement auth",
                    "description": "x" * 9000,
                }
            ],
            "totalCount": 1,
        },
    )
    result = call_tool("automaker_running_agents")
    assert result["running_agents"][0]["feature_id"] == "F003"
    assert result["running_agents"][0]["is_auto_mode"] is True
    assert result["total_count"] == 1
    assert "x" * 9000 not in codes(result)


def test_autonomy_status_post_but_read(tool_client):
    tool_client.route(
        "POST",
        "/api/auto-mode/status",
        {
            "success": True,
            "isRunning": True,
            "runningFeatures": ["F9"],
            "runningCount": 1,
        },
    )
    assert tool_class("automaker_autonomy_status") is READ
    result = call_tool("automaker_autonomy_status")
    assert result["is_running"] is True
    assert result["running_features"] == ["F9"]
    assert tool_client.records[-1]["method"] == "POST"
    assert tool_client.records[-1]["path"] == "/api/auto-mode/status"


def test_backlog_status(tool_client):
    tool_client.route(
        "GET",
        "/api/backlog-plan/status",
        {
            "success": True,
            "isRunning": False,
            "savedPlan": {
                "savedAt": "2026-10-14T00:00:00Z",
                "prompt": "huge" * 5000,
                "result": {"modifications": [{}, {}, {}]},
            },
        },
    )
    result = call_tool("automaker_backlog_status", project_path="/srv/app")
    assert result["is_running"] is False
    assert result["has_saved_plan"] is True
    assert result["saved_plan"]["feature_count"] == 3
    assert "huge" * 5000 not in codes(result)
    record = tool_client.records[-1]
    assert record["method"] == "GET"
    assert "projectPath=%2Fsrv%2Fapp" in record["query"]


# --- CONTROL tools -----------------------------------------------------------


def test_job_create_correct_endpoint_and_schema(tool_client):
    tool_client.route(
        "POST",
        "/api/features/create",
        {"success": True, "feature": {"id": "N1", "status": "pending"}},
    )
    result = call_tool(
        "automaker_job_create",
        project_path="/srv/app",
        job={
            "title": "New job",
            "description": "do it",
            "priority": 3,
            "dependencies": ["F1"],
        },
    )
    assert result["job"]["id"] == "N1"
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("POST", "/api/features/create")
    assert record["body"] == {
        "projectPath": "/srv/app",
        "feature": {
            "title": "New job",
            "description": "do it",
            "category": "Uncategorized",
            "priority": 3,
            "dependencies": ["F1"],
        },
    }
    assert tool_class("automaker_job_create") is CONTROL


@pytest.mark.parametrize(
    "extra", [{"model": "gpt-x"}, {"providerId": "p"}, {"id": "hijack"}]
)
def test_job_create_rejects_model_and_extra_fields(tool_client, extra):
    job = {"title": "t", **extra}
    result = call_tool("automaker_job_create", project_path="/srv/app", job=job)
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "AUTOMAKER_REQUEST_REJECTED"
    assert len(tool_client.records) == 0


def test_job_update_correct_endpoint_and_fields(tool_client):
    tool_client.route(
        "POST",
        "/api/features/update",
        {"success": True, "feature": {"id": "F1", "status": "verified"}},
    )
    result = call_tool(
        "automaker_job_update",
        project_path="/srv/app",
        feature_id="F1",
        updates={"status": "verified", "priority": 1},
    )
    assert result["job"]["status"] == "verified"
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("POST", "/api/features/update")
    assert record["body"] == {
        "projectPath": "/srv/app",
        "featureId": "F1",
        "updates": {"status": "verified", "priority": 1},
    }


def test_job_update_rejects_bad_status_and_empty(tool_client):
    assert (
        call_tool(
            "automaker_job_update",
            project_path="/srv",
            feature_id="F",
            updates={"status": "deleted"},
        )["error_code"]
        == "AUTOMAKER_REQUEST_REJECTED"
    )
    assert (
        call_tool(
            "automaker_job_update", project_path="/srv", feature_id="F", updates={}
        )["error_code"]
        == "AUTOMAKER_REQUEST_REJECTED"
    )


def test_worker_start_correct_endpoint(tool_client):
    tool_client.route(
        "POST",
        "/api/agent/start",
        {"success": True, "sessionId": "S1", "messages": [{"leak": "y" * 9000}]},
    )
    result = call_tool(
        "automaker_worker_start", session_id="S1", working_directory="/srv/app"
    )
    assert (
        result["result"] == {"result": {"success": True, "sessionId": "S1"}}["result"]
    )
    assert "y" * 9000 not in codes(result)
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("POST", "/api/agent/start")
    assert record["body"] == {"sessionId": "S1", "workingDirectory": "/srv/app"}


def test_worker_stop_requires_explicit_session(tool_client):
    tool_client.route("POST", "/api/agent/stop", {"success": True})
    assert call_tool("automaker_worker_stop", session_id="S2")["result"] == {
        "success": True
    }
    record = tool_client.records[-1]
    assert (record["method"], record["path"]) == ("POST", "/api/agent/stop")
    assert record["body"] == {"sessionId": "S2"}
    before = len(tool_client.records)
    assert (
        call_tool("automaker_worker_stop", session_id="  ")["error_code"]
        == "AUTOMAKER_REQUEST_REJECTED"
    )
    assert len(tool_client.records) == before


# --- surface lockdown --------------------------------------------------------


def test_stdio_lists_exactly_intended_tools():
    tools = asyncio.run(mcp_mod.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


def test_no_delete_or_model_tools_exposed():
    names = {name.casefold() for name in EXPECTED_TOOLS}
    for banned in (
        "delete",
        "bulk",
        "model",
        "queue",
        "send",
        "apply",
        "merge",
        "clear",
    ):
        assert not any(banned in name for name in names)


def test_no_arbitrary_endpoint_escape():
    tools = asyncio.run(mcp_mod.mcp.list_tools())
    banned_params = {
        "path",
        "url",
        "endpoint",
        "target",
        "method",
        "body",
        "query",
        "headers",
        "raw",
    }
    for tool in tools:
        params = set(tool.input_schema.get("properties", {}))
        assert not params & banned_params, tool.name
    public = {name for name in dir(AutomakerClient) if not name.startswith("_")}
    assert not public & {
        "request",
        "fetch",
        "send",
        "http",
        "get",
        "post",
        "call",
        "url",
    }


# --- failure mapping ---------------------------------------------------------


def _client_error(fake, name="health", **kwargs):
    client = fake.client(**kwargs)
    with pytest.raises(AutomakerError) as error:
        getattr(client, name)()
    return error.value.code


def test_timeout_mapping(fake):
    fake.action("GET", "/api/health", lambda h, b, q: (time.sleep(0.5), (200, {}))[1])
    assert _client_error(fake, "health", timeout=0.15) == "AUTOMAKER_TIMEOUT"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_mapping(fake, status):
    fake.route("GET", "/api/health", {"error": "denied"}, status=status)
    assert _client_error(fake, "health") == "AUTOMAKER_AUTH_FAILED"


def test_malformed_json_mapping(fake):
    fake.route("GET", "/api/health", None, raw=b"{not json")
    assert _client_error(fake, "health") == "AUTOMAKER_BAD_RESPONSE"


def test_wrong_content_type_mapping(fake):
    fake.route(
        "GET", "/api/health", None, raw=b"<html>ok</html>", content_type="text/html"
    )
    assert _client_error(fake, "health") == "AUTOMAKER_BAD_RESPONSE"


def test_oversized_response_handling(fake):
    fake.route("GET", "/api/health", {"status": "ok", "blob": "z" * 200_000})
    assert (
        _client_error(fake, "health", max_response_bytes=1000)
        == "AUTOMAKER_BAD_RESPONSE"
    )


def test_not_found_mapping(fake):
    fake.route("GET", "/api/health", {"error": "gone"}, status=404)
    assert _client_error(fake, "health") == "AUTOMAKER_NOT_FOUND"


def test_conflict_mapping(fake):
    fake.route("GET", "/api/health", {"error": "conflict"}, status=409)
    assert _client_error(fake, "health") == "AUTOMAKER_CONFLICT"


def test_request_rejected_mapping(fake):
    fake.route("GET", "/api/health", {"error": "bad"}, status=400)
    assert _client_error(fake, "health") == "AUTOMAKER_REQUEST_REJECTED"


def test_unreachable_maps_to_unavailable():
    # Refusal is injected at the transport seam: Windows firewalls on this host
    # may silently drop unroutable loopback SYNs instead of refusing them.
    client = AutomakerClient("http://127.0.0.1:3008", KEY, timeout=2.0)

    def refused(*args, **kwargs):
        raise urllib.error.URLError(ConnectionRefusedError())

    client._opener.open = refused
    with pytest.raises(AutomakerError) as error:
        client.health()
    assert error.value.code == "AUTOMAKER_UNAVAILABLE"


def test_server_exception_body_not_leaked(fake):
    body = json.dumps(
        {"error": "Traceback (most recent call last): secret " + KEY}
    ).encode()
    fake.route("GET", "/api/health", None, status=500, raw=body)
    client = fake.client()
    with pytest.raises(AutomakerError) as error:
        client.health()
    assert error.value.code == "AUTOMAKER_UNAVAILABLE"
    assert KEY not in str(error.value)


def test_control_never_retries_reads_retry_once(fake):
    fake.route("GET", "/api/health", {"status": "ok"})
    fake.route("POST", "/api/agent/stop", {"success": True})
    client = fake.client()
    calls = {"n": 0}
    original = client._execute

    def flaky(method, path, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AutomakerError("AUTOMAKER_UNAVAILABLE")
        return original(method, path, body)

    client._execute = flaky
    assert client.health()["status"] == "ok"
    assert calls["n"] == 2

    def counting(*args):
        calls["n"] += 1
        raise AutomakerError("AUTOMAKER_UNAVAILABLE")

    client._execute = counting
    calls["n"] = 0
    with pytest.raises(AutomakerError):
        client.agent_stop("S1")
    assert calls["n"] == 1


# --- documentation guards ----------------------------------------------------


def test_registration_example_contains_no_real_secret():
    from pathlib import Path

    text = Path("docs/AUTOMAKER-MCP.md").read_text(encoding="utf-8")
    assert '"automaker-local"' in text
    assert "<set locally, never commit>" in text
    assert KEY not in text
    import re

    assert not re.search(r"\bghp_[A-Za-z0-9]{20,}\b", text)
    assert not re.search(r"\"AUTOMAKER_API_KEY\":\s\"[^\s<\"]{16,}\"", text)
