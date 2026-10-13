"""Reconstructed Phase 1 safety contracts; no provider credentials required."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.hybrid_local.acp import effective_model
from app.hybrid_local.policy import (
    HybridError,
    WorkspaceRegistry,
    native_environment,
    permission_response,
)
from app.hybrid_local.service import HybridLocal
from app.hybrid_local.store import RunStore


@pytest.fixture
def registered(tmp_path):
    roots = [tmp_path / "Finance équipe", tmp_path / "Application 日本語"]
    for root in roots:
        (root / "src").mkdir(parents=True)
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "workspaces": {
                    name: {
                        "root": str(root),
                        "capabilities": ["read", "edit"],
                        "write_paths": ["src"],
                    }
                    for name, root in zip(["finance", "application"], roots)
                }
            }
        )
    )
    return path, WorkspaceRegistry(path), roots


class FakeACP:
    def __init__(self, binary, workspace, role, **_):
        self.workspace, self.role, self.prompting = workspace, role, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def session(self, session_id="", expected_model=""):
        return {
            "session_id": session_id
            or "session-" + self.workspace.id + "-" + self.role,
            "model": expected_model or "synthetic/" + self.workspace.id,
            "root": str(self.workspace.root),
            "role": self.role,
        }

    async def prompt(self, text, timeout):
        return {"text": "unit synthetic completion", "stop_reason": "end_turn"}


def test_unknown_workspace_rejected(registered):
    with pytest.raises(HybridError, match="UNKNOWN_WORKSPACE"):
        registered[1].get("unregistered")


def test_overlapping_roots_rejected(registered):
    path, _, roots = registered
    document = json.loads(path.read_text())
    document["workspaces"]["nested"] = {"root": str(roots[0] / "src")}
    path.write_text(json.dumps(document))
    with pytest.raises(HybridError, match="INVALID_WORKSPACE_REGISTRY"):
        WorkspaceRegistry(path)


def test_traversal_denied(registered):
    with pytest.raises(HybridError, match="PATH_OUTSIDE_WORKSPACE"):
        registered[1].get("finance").file("../escape.txt")


def test_symlink_escape_denied(registered, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (registered[2][0] / "src/link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(HybridError, match="PATH_OUTSIDE_WORKSPACE"):
        registered[1].get("finance").file("src/link/private.txt", write=True)


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".env",
        ".env.production",
        "auth.json",
        ".opencode/agents/a.md",
        ".gemini/state",
        "OPENCODE.JSON",
    ],
)
def test_protected_paths_denied(registered, path):
    with pytest.raises(HybridError, match="PROTECTED_PATH"):
        registered[1].get("finance").file(path)


def test_scoped_write_and_sibling_denial(registered):
    workspace = registered[1].get("finance")
    assert (
        workspace.file("src/result.txt", write=True)
        == workspace.root / "src/result.txt"
    )
    with pytest.raises(HybridError, match="WRITE_SCOPE_DENIED"):
        workspace.file("result.txt", write=True)


def test_reviewer_and_supervisor_are_readonly(registered):
    workspace = registered[1].get("finance")
    for role in ("reviewer", "supervisor"):
        environment = native_environment(workspace, role, {})
        agent = json.loads(environment["OPENCODE_CONFIG_CONTENT"])["agent"][
            "hybrid-local-" + role
        ]
        assert (
            agent["permission"]["edit"] == "deny"
            and agent["permission"]["bash"] == "deny"
        )
        assert "model" not in agent


def test_native_credentials_and_settings_preserved_in_memory(registered):
    inherited = {
        "ARGUS_SKILL_MODEL": "poison/override",
        "argus_skill_engineer_model": "poison/engineer",
        "OPENAI_API_KEY": "synthetic-private-env",
        "OPENCODE_CONFIG": "native-config-path",
        "OPENCODE_CONFIG_CONTENT": '{"model":"native/selected","agent":{"existing":{"mode":"primary"}}}',
    }
    result = native_environment(registered[1].get("finance"), "engineer", inherited)
    assert (
        result["OPENAI_API_KEY"] == inherited["OPENAI_API_KEY"]
        and result["OPENCODE_CONFIG"] == inherited["OPENCODE_CONFIG"]
    )
    config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "native/selected" and "existing" in config["agent"]
    assert not any(k.upper().startswith("ARGUS_") for k in result)
    assert "ARGUS_SKILL_MODEL" in inherited


def test_invalid_inline_native_configuration_fails(registered):
    with pytest.raises(HybridError, match="INVALID_NATIVE_CONFIG_CONTENT"):
        native_environment(
            registered[1].get("finance"), "engineer", {"OPENCODE_CONFIG_CONTENT": "[]"}
        )


def test_workspace_lease_serializes_and_uncertain_stop_blocks(registered, tmp_path):
    store = RunStore(tmp_path / "state.sqlite3")
    workspace = registered[1].get("finance")
    key = store.begin(workspace, "engineer", 30)
    for status in ("STARTING", "RUNNING", "REMOTE_STOP_UNCONFIRMED"):
        store.finish(key, status)
        with pytest.raises(HybridError, match="WORKSPACE_BUSY_OR_RECOVERY_REQUIRED"):
            store.begin(workspace, "engineer", 30)
    store.finish(key, "FAILED")
    assert store.begin(workspace, "engineer", 30) != key


def test_resume_wrong_root_role_and_unknown_session_denied(registered, tmp_path):
    store = RunStore(tmp_path / "state.sqlite3")
    workspace = registered[1].get("finance")
    key = store.begin(workspace, "engineer", 30)
    store.bind(
        key,
        workspace,
        {
            "session_id": "s1",
            "root": str(workspace.root),
            "role": "engineer",
            "model": "synthetic/finance",
        },
    )
    for workspace_id, role, session_id in [
        ("application", "engineer", "s1"),
        ("finance", "reviewer", "s1"),
        ("finance", "engineer", "desktop-unowned"),
    ]:
        with pytest.raises(HybridError, match="SESSION_WORKSPACE_OR_ROLE_MISMATCH"):
            store.verify_session(session_id, registered[1].get(workspace_id), role)


def test_restart_retains_uncertainty_and_versioned_events(registered, tmp_path):
    path = tmp_path / "state.sqlite3"
    store = RunStore(path)
    key = store.begin(registered[1].get("finance"), "engineer", 30)
    store.finish(key, "REMOTE_STOP_UNCONFIRMED", "ACP_TIMEOUT")
    status = RunStore(path).status(key)
    assert status["recovery_required"] and not status["accepted"]
    assert [e["seq"] for e in status["events"]] == sorted(
        e["seq"] for e in status["events"]
    )
    assert status["error"] == "ACP_TIMEOUT"


def test_supervisor_assistance_bounded_and_no_recursion(registered, tmp_path):
    service = HybridLocal(registered[0], tmp_path / "state", adapter=FakeACP)
    original = asyncio.run(service.run("finance", "Original unit fixture"))
    a = asyncio.run(service.assist(original["run_id"], "Review only"))
    asyncio.run(service.assist(original["run_id"], "Second bounded review"))
    with pytest.raises(HybridError, match="ASSISTANCE_LIMIT"):
        asyncio.run(service.assist(original["run_id"], "Third forbidden review"))
    with pytest.raises(HybridError, match="INVALID_ASSISTANCE_PARENT"):
        asyncio.run(service.assist(a["run_id"], "Recursive assistance"))


def test_eight_hour_budget_and_completed_is_not_accepted(registered, tmp_path):
    service = HybridLocal(registered[0], tmp_path / "state", adapter=FakeACP)
    for value in (0, -1, 28801, float("nan"), float("inf")):
        with pytest.raises(HybridError, match="INVALID_RUN_REQUEST"):
            asyncio.run(service.run("finance", "Unit fixture", timeout=value))
    result = asyncio.run(service.run("finance", "Unit fixture", timeout=28800))
    assert (
        result["status"] == "COMPLETED_AWAITING_VALIDATION" and not result["accepted"]
    )


def test_effective_model_observation_never_guesses():
    assert (
        effective_model(
            {"configOptions": [{"id": "model", "currentValue": "synthetic/one"}]}
        )
        == "synthetic/one"
    )
    assert (
        effective_model({"models": {"currentModelId": "synthetic/two"}})
        == "synthetic/two"
    )
    with pytest.raises(HybridError, match="MODEL_UNOBSERVABLE"):
        effective_model({"model": "unobservable"})


def test_permission_one_time_and_unknown_shell_denied(registered):
    workspace = registered[1].get("finance")
    params = {
        "sessionId": "s1",
        "toolCall": {
            "kind": "edit",
            "locations": [{"path": str(workspace.root / "src/new.txt")}],
        },
        "options": [
            {"kind": "allow_once", "optionId": "once"},
            {"kind": "allow_always", "optionId": "forever"},
        ],
    }
    result, allowed = permission_response(workspace, "engineer", params, "s1")
    assert allowed and result["outcome"]["optionId"] == "once"
    assert not permission_response(workspace, "reviewer", params, "s1")[1]
    assert not permission_response(workspace, "engineer", params, "other-session")[1]
    params["toolCall"]["kind"] = "execute"
    assert not permission_response(workspace, "engineer", params, "s1")[1]
