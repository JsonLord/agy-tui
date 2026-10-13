"""Real ACP with isolated synthetic providers, never production authentication."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.hybrid_local.service import HybridLocal


@pytest.fixture
def live(tmp_path):
    binary = os.environ.get("HYBRID_TEST_OPENCODE")
    if not binary:
        pytest.skip("HYBRID_TEST_OPENCODE required for real synthetic ACP validation")
    calls, control = [], {"tool": None, "failure": False}

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(
                {
                    "model": body["model"],
                    "path": self.path,
                    "tools": [t["function"]["name"] for t in body.get("tools", [])],
                }
            )
            if control["failure"]:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "error": {
                                "message": "synthetic private provider detail must not leak",
                                "type": "invalid_request_error",
                                "code": "model_not_found",
                            }
                        }
                    ).encode()
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            tool = control["tool"]
            has_result = any(m.get("role") == "tool" for m in body.get("messages", []))
            if tool and not has_result:
                deltas = [
                    (
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "fixture-tool",
                                    "type": "function",
                                    "function": {
                                        "name": tool["name"],
                                        "arguments": json.dumps(tool["arguments"]),
                                    },
                                }
                            ],
                        },
                        None,
                    ),
                    ({}, "tool_calls"),
                ]
            else:
                deltas = [
                    ({"role": "assistant", "content": "SYNTHETIC_PROVIDER_OK"}, None),
                    ({}, "stop"),
                ]
            for delta, finish in deltas:
                packet = {
                    "id": "synthetic",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(packet) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    roots, contents, entries = [], [], {}
    for name, model, directory in [
        ("finance", "finance-fixture", "Finance équipe"),
        ("application", "application-fixture", "Application 日本語"),
    ]:
        root = tmp_path / directory
        (root / "src").mkdir(parents=True)
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"synthetic/{model}",
            "provider": {
                "synthetic": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Synthetic isolated test provider",
                    "options": {
                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                        "apiKey": "synthetic-not-real",
                    },
                    "models": {
                        model: {
                            "name": model,
                            "limit": {"context": 32768, "output": 2048},
                        }
                    },
                }
            },
        }
        path = root / "opencode.json"
        path.write_text(json.dumps(config))
        roots.append(root)
        contents.append(path.read_bytes())
        entries[name] = {
            "root": str(root),
            "capabilities": ["read", "edit"],
            "write_paths": ["src"],
        }
    registry = tmp_path / "workspaces.json"
    registry.write_text(json.dumps({"workspaces": entries}))
    environment = dict(os.environ)
    for area in ("DATA", "CONFIG", "CACHE", "STATE"):
        path = tmp_path / "xdg" / area.lower()
        path.mkdir(parents=True)
        environment[f"XDG_{area}_HOME"] = str(path)
    for key in (
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_PERMISSION",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "ARGUS_SKILL_MODEL": "poison/override",
            "ARGUS_SKILL_ENGINEER_MODEL": "poison/engineer",
            "ARGUS_SKILL_OPENCODE_PROVIDER": "poison",
        }
    )
    service = HybridLocal(registry, tmp_path / "state", binary, environment=environment)
    try:
        yield service, calls, control, roots, contents, registry
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_acp_two_inherited_models_resume_no_overrides(live, monkeypatch):
    service, calls, _, roots, contents, _ = live
    launches, original = [], asyncio.create_subprocess_exec

    async def capture(*arguments, **options):
        launches.append((arguments, options))
        return await original(*arguments, **options)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    a = asyncio.run(
        service.run(
            "finance", "Reply with a synthetic confirmation without tools.", timeout=90
        )
    )
    b = asyncio.run(
        service.run(
            "application",
            "Reply with a synthetic confirmation without tools.",
            timeout=90,
        )
    )
    assert a["status"] == b["status"] == "COMPLETED_AWAITING_VALIDATION", (a, b)
    assert (
        a["model"] == "synthetic/finance-fixture"
        and b["model"] == "synthetic/application-fixture"
    )
    assert "SYNTHETIC_PROVIDER_OK" in a["text"]
    resumed = asyncio.run(
        service.run(
            "finance",
            "Confirm again without tools.",
            session_id=a["session_id"],
            timeout=90,
        )
    )
    assert resumed["status"] == "COMPLETED_AWAITING_VALIDATION", resumed
    assert resumed["model"] == a["model"] and resumed["session_id"] == a["session_id"]
    assert {c["model"] for c in calls} == {"finance-fixture", "application-fixture"}
    for argv, options in launches:
        assert "--model" not in argv and options["cwd"] in roots
        assert not any(k.upper().startswith("ARGUS_") for k in options["env"])
        config = json.loads(options["env"]["OPENCODE_CONFIG_CONTENT"])
        assert "model" not in config and "provider" not in config
    assert all(
        (root / "opencode.json").read_bytes() == content
        for root, content in zip(roots, contents)
    )
    assert not a["accepted"]


def test_real_scoped_engineer_and_readonly_review(live):
    service, _, control, roots, _, _ = live
    allowed = roots[0] / "src/allowed.txt"
    control["tool"] = {
        "name": "write",
        "arguments": {"filePath": str(allowed), "content": "synthetic deliverable"},
    }
    result = asyncio.run(
        service.run("finance", "Write the synthetic scoped fixture.", timeout=90)
    )
    assert result["status"] == "COMPLETED_AWAITING_VALIDATION", result
    assert allowed.read_text() == "synthetic deliverable"
    denied = roots[1] / "src/cross.txt"
    control["tool"]["arguments"]["filePath"] = str(denied)
    result = asyncio.run(
        service.run("finance", "Attempt a synthetic cross-root write.", timeout=90)
    )
    assert result["status"] == "COMPLETED_AWAITING_VALIDATION", result
    assert not denied.exists()
    control["tool"]["arguments"]["filePath"] = str(roots[0] / "src/reviewer.txt")
    result = asyncio.run(
        service.run(
            "finance", "Attempt a synthetic reviewer edit.", role="reviewer", timeout=90
        )
    )
    assert result["status"] == "COMPLETED_AWAITING_VALIDATION", result
    assert not (roots[0] / "src/reviewer.txt").exists()


def test_real_model_unavailable_has_no_fallback_or_private_error(live):
    service, calls, control, _, _, _ = live
    control["failure"] = True
    result = asyncio.run(
        service.run("finance", "Synthetic unavailable model turn.", timeout=30)
    )
    assert result["status"] in {"FAILED", "REMOTE_STOP_UNCONFIRMED"}, result
    assert calls and {c["model"] for c in calls} == {"finance-fixture"}
    assert "private provider detail" not in json.dumps(result)
    assert result["error"] in {
        "ACP_REMOTE_ERROR",
        "TURN_TIMEOUT",
        "LOCAL_EXECUTION_ERROR",
        "ACP_TIMEOUT",
    }


def test_real_mcp_restart_and_other_cwd_status(live):
    service, _, _, roots, _, registry = live
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1] / "scripts/hybrid-local.py")],
        cwd=str(roots[1]),
        env={
            **service.environment,
            "HYBRID_WORKSPACES_FILE": str(registry),
            "HYBRID_STATE_ROOT": str(service.store.path.parent),
            "HYBRID_OPENCODE_BIN": service.binary,
        },
    )

    async def call(name, arguments):
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=90) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {
                "hybrid_run",
                "hybrid_status",
                "hybrid_review",
                "hybrid_supervisor_assist",
            } <= {t.name for t in tools.tools}
            result = await session.call_tool(name, arguments)
            assert not result.is_error
            return json.loads(result.content[0].text)

    result = asyncio.run(
        call(
            "hybrid_run",
            {"workspace_id": "finance", "task": "Reply without tools.", "timeout": 90},
        )
    )
    assert result["status"] == "COMPLETED_AWAITING_VALIDATION", result
    status = asyncio.run(call("hybrid_status", {"run_id": result["run_id"]}))
    assert status["run_id"] == result["run_id"] and status["model"] == result["model"]
    assert status["events"] and not status["accepted"]
