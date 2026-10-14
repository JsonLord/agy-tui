"""Offline bridge tests, including actual stdio fake MCP child lifecycle."""

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.argus_local.client import ArgusMcpClient
from app.argus_local.protocol import (
    ArgusPlanImporter,
    ProjectRegistry,
    ReviewEvidence,
    parse_review,
)
from app.argus_local.service import ArgusFactoryBridge
from app.argus_local.store import BridgeStore
from app.factory_local.schemas import (
    FactoryApproval,
    FactoryCompletion,
    FactoryEvidenceRef,
)
from app.factory_local.store import FactoryError, FactoryStore
from tests.test_factory_local import envelope


def registry():
    return ProjectRegistry.model_validate(
        {
            "projects": [
                {
                    "workspace_id": "w",
                    "repository": "owner/repo",
                    "project_path": "C:/Work/Sp äces",
                    "argus_project_id": "argus-p",
                }
            ]
        }
    )


def wrap(value, kind="PLAN"):
    return f"<AGY_FACTORY_{kind}>" + json.dumps(value) + f"</AGY_FACTORY_{kind}>"


class FakeClient:
    def __init__(self):
        self.reply = wrap(envelope())
        self.messages = []

    async def project_list(self):
        return [{"id": "argus-p", "workdir": "C:/Work/Sp äces"}]

    async def message(self, project, prompt):
        self.messages.append((project, prompt))
        return self.reply

    async def status(self, project):
        return {
            "daemon": {"alive": False},
            "active_role": "manager",
            "journal": ["must not export"],
        }

    async def artifacts(self, project):
        return [
            {
                "path": "artifact",
                "kind": "json",
                "preview": "must not export",
                "storage_path": "must not export",
            }
        ]


@pytest.fixture
def bridge(tmp_path):
    client = FakeClient()
    store = FactoryStore(tmp_path / "registry.sqlite")
    return ArgusFactoryBridge(store, registry(), client), client


@pytest.mark.parametrize(
    "mutation",
    [
        "prose",
        "multiple",
        "malformed",
        "outer-prose",
        "extra",
        "duplicate",
        "empty",
        "bad-dep",
        "cycle",
        "workspace",
        "path",
        "repo",
        "project",
        "cross-project",
        "native-id",
        "duplicate-json",
        "nan",
    ],
)
def test_plan_rejects_invalid(mutation):
    data = envelope()
    text = None
    if mutation == "prose":
        text = "here is your plan"
    if mutation == "multiple":
        text = wrap(data) + wrap(data)
    if mutation == "malformed":
        text = "<AGY_FACTORY_PLAN>{x}</AGY_FACTORY_PLAN>"
    if mutation == "outer-prose":
        text = "plan: " + wrap(data)
    if mutation == "extra":
        data["approval"] = "APPROVED"
    if mutation == "duplicate":
        data["tasks"][1]["task_id"] = "A"
    if mutation == "empty":
        data["tasks"] = []
    if mutation == "bad-dep":
        data["dependencies"][0]["requires_task_id"] = "missing"
    if mutation == "cycle":
        data["dependencies"].append({"task_id": "A", "requires_task_id": "C"})
    if mutation == "workspace":
        data["tasks"][0]["workspace_id"] = "unknown"
    if mutation == "path":
        data["tasks"][0]["project_path"] = "C:/private"
    if mutation == "repo":
        data["tasks"][0]["repository"] = "other/repo"
    if mutation == "project":
        data["goal"]["argus_project_id"] = "other"
    if mutation == "cross-project":
        data["tasks"][1]["project_path"] = "C:/other"
    if mutation == "native-id":
        data["tasks"][1]["argus_task_id"] = "argus-A"
    if mutation == "duplicate-json":
        text = wrap(data).replace('"plan_id": "p"', '"plan_id":"p","plan_id":"other"')
    if mutation == "nan":
        text = wrap(data).replace('"plan_id": "p"', '"plan_id":NaN')
    with pytest.raises(FactoryError):
        ArgusPlanImporter(registry()).parse(text or wrap(data), "argus-p")


def test_native_identity_and_missing_native_ids():
    parsed = ArgusPlanImporter(registry()).parse(wrap(envelope()), "argus-p")
    assert parsed.tasks[0].argus_task_id == "argus-A"
    data = envelope()
    for task in data["tasks"]:
        task["argus_task_id"] = None
    assert all(
        t.argus_task_id is None
        for t in ArgusPlanImporter(registry()).parse(wrap(data), "argus-p").tasks
    )


def test_import_idempotent_conflict_and_transaction(bridge):
    svc, client = bridge
    first = svc.import_plan("argus-p", "artifact:plan-1", wrap(envelope()))
    assert svc.import_plan("argus-p", "artifact:plan-1", wrap(envelope())) == first
    data = envelope()
    data["tasks"][0]["title"] = "changed"
    with pytest.raises(FactoryError, match="ARGUS_IMPORT_CONFLICT"):
        svc.import_plan("argus-p", "artifact:plan-1", wrap(data))
    data["plan_id"] = "new"
    with pytest.raises(FactoryError):
        svc.import_plan("argus-p", "artifact:plan-1", wrap(data))
    assert svc.store.factory.plan_ids() == ["p"]
    assert first["approval_status"] == "PROPOSED"
    assert svc.store.factory.approval("p") is None
    assert svc.store.factory.links("p") == []
    assert client.messages == []


def test_planning_only_cached_and_revision_bound(bridge):
    svc, client = bridge
    first = asyncio.run(svc.plan_goal("w", "g", "p", "synthetic goal", "request-1"))
    assert (
        asyncio.run(svc.plan_goal("w", "g", "p", "synthetic goal", "request-1"))
        == first
    )
    assert len(client.messages) == 1
    assert "Planning advice only" in client.messages[0][1]
    assert svc.store.factory.approval("p") is None
    assert svc.store.factory.links("p") == []
    with pytest.raises(FactoryError, match="ARGUS_REQUEST_CONFLICT"):
        asyncio.run(svc.plan_goal("w", "g", "p", "altered goal", "request-1"))
    approval = {
        "approval_id": "a",
        "plan_id": "p",
        "plan_revision": "0" * 64,
        "source": "manual",
        "status": "APPROVED",
        "timestamp": datetime.now(UTC),
        "actor_ref": "operator",
    }
    with pytest.raises(FactoryError):
        svc.store.factory.approve(FactoryApproval.model_validate(approval))


def test_uncertain_message_no_replay(bridge):
    svc, client = bridge

    async def uncertain(project, text):
        client.messages.append((project, text))
        raise FactoryError("ARGUS_OUTCOME_UNCERTAIN")

    client.message = uncertain
    with pytest.raises(FactoryError, match="ARGUS_OUTCOME_UNCERTAIN"):
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request-1"))
    recovered = ArgusFactoryBridge(
        FactoryStore(svc.store.factory.path), registry(), client
    )
    with pytest.raises(FactoryError, match="ARGUS_OUTCOME_UNCERTAIN"):
        asyncio.run(recovered.plan_goal("w", "g", "p", "goal", "request-1"))
    assert len(client.messages) == 1
    assert recovered.store.uncertain_requests("argus-p") == ["request-1"]


def test_unknown_workspace_before_call(bridge):
    svc, client = bridge
    with pytest.raises(FactoryError, match="ARGUS_UNKNOWN_WORKSPACE"):
        asyncio.run(svc.plan_goal("unknown", "g", "p", "goal", "request-1"))
    assert not client.messages


def evidence():
    return ReviewEvidence(
        refs=(
            FactoryEvidenceRef(
                evidence_id="test-report",
                owner="Automaker",
                artifact_ref="artifact:v1",
                sha256="1" * 64,
            ),
        ),
        sanitized_summary="Synthetic test results only",
    )


def review_data(plan, status="ACCEPTED"):
    return {
        "review_id": "review-1",
        "plan_id": plan.plan_id,
        "plan_revision": plan.revision(),
        "task_id": "A",
        "argus_project_id": "argus-p",
        "status": status,
        "summary": "Synthetic assessment",
        "remaining_gaps": [] if status == "ACCEPTED" else ["needs additional evidence"],
        "replan_recommended": status != "ACCEPTED",
        "evidence_refs": ["test-report"],
        "reviewed_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.parametrize(
    "status", ["ACCEPTED", "MORE_WORK_REQUIRED", "BLOCKED", "ESCALATE"]
)
def test_review_verdict_and_no_replan(bridge, status):
    svc, client = bridge
    svc.import_plan("argus-p", "ref", wrap(envelope()))
    plan = svc.store.factory.plan("p")
    before = plan.revision()
    client.reply = wrap(review_data(plan, status), "REVIEW")
    refs = evidence()
    completion = FactoryCompletion(
        task_id="A", reviewer_ref="submitter", accepted=True, evidence=refs.refs
    )
    result = asyncio.run(svc.review_completion("p", completion, refs, "review-1"))
    assert result["completion"]["accepted"] == (status == "ACCEPTED")
    assert result["review"]["replan_recommended"] == (status != "ACCEPTED")
    assert result["review_role"] == "REVIEW_ROLE_IS_LOGICAL_NOT_TRANSPORT_SEPARATE"
    assert svc.store.factory.plan_ids() == ["p"]
    assert svc.store.factory.plan("p").revision() == before
    assert svc.store.factory.approval("p") is None
    assert svc.store.factory.links("p") == []
    assert (
        asyncio.run(svc.review_completion("p", completion, refs, "review-1")) == result
    )
    assert len(client.messages) == 1
    assert (
        BridgeStore(FactoryStore(svc.store.factory.path)).reviews("p")[0]["status"]
        == status
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "prose",
        "unknown-evidence",
        "missing-evidence",
        "extra",
        "revision",
        "task",
        "project",
        "timestamp",
        "accept-gap",
        "duplicate-evidence",
    ],
)
def test_review_invalid(bridge, mutation):
    svc, _ = bridge
    svc.import_plan("argus-p", "ref", wrap(envelope()))
    plan = svc.store.factory.plan("p")
    data = review_data(plan)
    text = None
    if mutation == "prose":
        text = "looks good"
    if mutation == "unknown-evidence":
        data["evidence_refs"] = ["unknown"]
    if mutation == "missing-evidence":
        data["evidence_refs"] = []
    if mutation == "extra":
        data["accepted"] = True
    if mutation == "revision":
        data["plan_revision"] = "0" * 64
    if mutation == "task":
        data["task_id"] = "B"
    if mutation == "project":
        data["argus_project_id"] = "other"
    if mutation == "timestamp":
        data["reviewed_at"] = "2026-10-14T00:00:00"
    if mutation == "accept-gap":
        data["remaining_gaps"] = ["missing test"]
    if mutation == "duplicate-evidence":
        data["evidence_refs"] *= 2
    with pytest.raises(FactoryError, match="ARGUS_REVIEW_INVALID"):
        parse_review(text or wrap(data, "REVIEW"), plan, "A", evidence())


@pytest.fixture
def external(tmp_path):
    state = tmp_path / "Space ä" / "fake.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "projects": [{"id": "argus-p", "workdir": "C:/Work/Sp äces"}],
                "reply": wrap(envelope()),
            }
        )
    )
    launcher = Path(__file__).parent / "fixtures/fake_argus_mcp.py"
    client = ArgusMcpClient(
        sys.executable,
        args=(str(launcher.resolve()), str(state)),
        timeout=0.5,
        startup_timeout=3,
    )
    return client, state


def configure(state, **kwargs):
    value = json.loads(state.read_text())
    value.update(kwargs)
    state.write_text(json.dumps(value))


def test_real_stdio_typed_calls_and_owned_shutdown(external):
    client, state = external
    assert asyncio.run(client.project_list())[0]["id"] == "argus-p"
    assert asyncio.run(client.status("argus-p"))["daemon"]["alive"] is False
    assert asyncio.run(client.artifacts("argus-p"))[0]["path"] == "plan.json"
    assert asyncio.run(client.message("argus-p", "Planning advice only")) == wrap(
        envelope()
    )
    calls = [
        json.loads(line)
        for line in Path(str(state) + ".calls").read_text().splitlines()
    ]
    assert len(calls) == 1 and calls[0]["text"].startswith("/ask ")
    pid = int(Path(str(state) + ".pid").read_text())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not hasattr(client, "call_any_tool")


@pytest.mark.parametrize(
    "mode,error,mutating",
    [
        ("missing-tool", "ARGUS_PROTOCOL_ERROR", False),
        ("malformed-stdio", "ARGUS_PROTOCOL_ERROR", False),
        ("timeout-read", "ARGUS_TIMEOUT", False),
        ("not-found", "ARGUS_PROJECT_NOT_FOUND", False),
        ("timeout-message", "ARGUS_OUTCOME_UNCERTAIN", True),
        ("crash-message", "ARGUS_OUTCOME_UNCERTAIN", True),
        ("task", "ARGUS_OUTCOME_UNCERTAIN", True),
        ("bad-payload", "ARGUS_OUTCOME_UNCERTAIN", True),
    ],
)
def test_stdio_failures(external, mode, error, mutating):
    client, state = external
    configure(state, mode=mode)
    with pytest.raises(FactoryError, match=error):
        asyncio.run(
            client.message("argus-p", "Planning advice only")
            if mutating
            else client.status("argus-p")
            if mode == "not-found"
            else client.project_list()
        )
    if mutating:
        assert len(Path(str(state) + ".calls").read_text().splitlines()) == 1


def test_missing_executable_and_failed_start(tmp_path):
    client = ArgusMcpClient(str(tmp_path / "absent"))
    with pytest.raises(FactoryError, match="ARGUS_NOT_INSTALLED"):
        asyncio.run(client.project_list())
    unusable = tmp_path / "not-executable.exe"
    unusable.write_text("not executable")
    with pytest.raises(FactoryError, match="ARGUS_START_FAILED"):
        asyncio.run(ArgusMcpClient(str(unusable)).project_list())


def test_bridge_stdio_plan_review_restart(external, tmp_path):
    client, state = external
    store = FactoryStore(tmp_path / "factory.sqlite")
    svc = ArgusFactoryBridge(store, registry(), client)
    planned = asyncio.run(
        svc.plan_goal("w", "g", "p", "bounded synthetic report", "plan-1")
    )
    assert planned["approval_status"] == "PROPOSED"
    p = store.plan("p")
    configure(state, reply=wrap(review_data(p, "MORE_WORK_REQUIRED"), "REVIEW"))
    refs = evidence()
    completion = FactoryCompletion(
        task_id="A", reviewer_ref="submitter", accepted=False, evidence=refs.refs
    )
    result = asyncio.run(svc.review_completion("p", completion, refs, "review-1"))
    recovered = ArgusFactoryBridge(FactoryStore(store.path), registry(), client)
    assert (
        asyncio.run(recovered.review_completion("p", completion, refs, "review-1"))
        == result
    )
    assert len(Path(str(state) + ".calls").read_text().splitlines()) == 2
    status = asyncio.run(recovered.status("w"))
    assert "private" not in json.dumps(status)
    assert store.approval("p") is None and store.links("p") == []


def test_mcp_new_surface_and_redaction(bridge, monkeypatch):
    from app.factory_local import mcp

    svc, _client = bridge
    monkeypatch.setattr(mcp, "_bridge", svc)

    async def run():
        tools = await mcp.mcp.list_tools()
        assert {t.name for t in tools} == set(mcp.TOOL_CLASSES)
        result = await mcp.factory_plan_goal("w", "g", "p", "goal", "request")
        assert result["approval_status"] == "PROPOSED"
        assert len(result["correlation_id"]) == 32
        assert (await mcp.factory_argus_plan_preview("p"))["plan"]["plan_id"] == "p"
        assert (await mcp.factory_argus_reviews("p"))["reviews"] == []
        assert (await mcp.factory_argus_status("w"))["transport"] == "stdio"
        bad = await mcp.factory_import_argus_plan("argus-p", "bad", "private prose")
        assert bad[
            "error_code"
        ] == "ARGUS_PLAN_INVALID" and "private prose" not in json.dumps(bad)

    asyncio.run(run())


def test_registry_readonly_and_rejects_private_paths(tmp_path):
    from app.argus_local.config import load_registry

    own = tmp_path / "projects.json"
    own.write_text(registry().model_dump_json())
    before = own.read_bytes()
    assert load_registry(str(own)) == registry() and own.read_bytes() == before
    private = tmp_path / ".argus-skill" / "projects.json"
    private.parent.mkdir()
    private.write_text("private")
    with pytest.raises(FactoryError):
        load_registry(str(private))


def test_packaging_includes_bridge_not_argus_runtime():
    from app.hybrid_local.installation import portable_files

    source = Path(__file__).resolve().parents[1]
    paths = [str(p.relative_to(source)) for p in portable_files(source)]
    assert "scripts/factory-local.py" in paths
    assert "app/argus_local/client.py" in paths and "app/factory_local/mcp.py" in paths
    assert "app/automaker_local/client.py" in paths
    assert all("argus_skill" not in p for p in paths)


def test_live_project_binding_rejected(bridge):
    svc, client = bridge

    async def wrong_project():
        return [{"id": "argus-p", "workdir": "C:/Other"}]

    client.project_list = wrong_project
    with pytest.raises(FactoryError, match="ARGUS_PROJECT_MISMATCH"):
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request"))
    assert client.messages == []


def test_invalid_plan_delivery_not_replayed(bridge):
    svc, client = bridge
    client.reply = "prose"
    with pytest.raises(FactoryError, match="ARGUS_PLAN_INVALID"):
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request"))
    with pytest.raises(FactoryError, match="ARGUS_OUTCOME_UNCERTAIN"):
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request"))
    assert len(client.messages) == 1 and svc.store.factory.plan_ids() == []


def test_review_requires_evidence_before_message(bridge):
    svc, client = bridge
    svc.import_plan("argus-p", "ref", wrap(envelope()))
    completion = FactoryCompletion(
        task_id="A", reviewer_ref="submitter", accepted=True, evidence=()
    )
    with pytest.raises(FactoryError, match="ARGUS_REVIEW_INVALID"):
        asyncio.run(svc.review_completion("p", completion, evidence(), "review-1"))
    assert not client.messages


def test_revised_plan_requires_new_id_and_approval(bridge):
    svc, _client = bridge
    svc.import_plan("argus-p", "ref", wrap(envelope()))
    p = svc.store.factory.plan("p")
    svc.store.factory.approve(
        FactoryApproval(
            approval_id="a",
            plan_id="p",
            plan_revision=p.revision(),
            source="manual",
            status="APPROVED",
            timestamp=datetime.now(UTC),
            actor_ref="operator",
        )
    )
    changed = envelope()
    changed["tasks"][0]["title"] = "revised"
    with pytest.raises(FactoryError):
        svc.import_plan("argus-p", "revision-ref", wrap(changed))
    changed["plan_id"] = "p-revision-2"
    result = svc.import_plan("argus-p", "revision-ref", wrap(changed))
    assert result["approval_status"] == "PROPOSED"
    assert svc.store.factory.approval("p-revision-2") is None
    assert svc.store.factory.plan("p").revision() == p.revision()


def test_cancelled_message_and_unrelated_process_survives(external):
    import subprocess

    client, state = external
    configure(state, mode="timeout-message")
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])

    async def run():
        task = asyncio.create_task(client.message("argus-p", "planning advice"))
        for _ in range(100):
            if Path(str(state) + ".calls").exists():
                break
            await asyncio.sleep(0.02)
        assert Path(str(state) + ".calls").exists()
        task.cancel()
        with pytest.raises(FactoryError, match="ARGUS_OUTCOME_UNCERTAIN"):
            await task

    try:
        asyncio.run(run())
        assert unrelated.poll() is None
        if os.name != "nt":
            with pytest.raises(ProcessLookupError):
                os.kill(int(Path(str(state) + ".pid").read_text()), 0)
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_transport_schema_mismatch(external, monkeypatch):
    from app.argus_local import client as module

    monkeypatch.setitem(
        module.TOOLS, "argus_message", {"project_id": "integer", "text": "string"}
    )
    with pytest.raises(FactoryError, match="ARGUS_PROTOCOL_ERROR"):
        asyncio.run(external[0].project_list())


def test_client_payload_redaction_and_ambiguity():
    from types import SimpleNamespace

    class Result:
        is_error = False
        content = (SimpleNamespace(type="text", text='{"ok":true,"ok":false}'),)

        def model_dump(self, by_alias):
            return {"isError": False, "structuredContent": None}

    with pytest.raises(FactoryError, match="ARGUS_BAD_RESPONSE"):
        ArgusMcpClient._payload(Result())
    Result.content[0].text = '{"ok":true}'
    Result.model_dump = lambda self, by_alias: {
        "isError": False,
        "structuredContent": {"ok": False},
    }
    with pytest.raises(FactoryError):
        ArgusMcpClient._payload(Result())


def test_sensitive_content_not_sent_or_persisted(bridge):
    svc, client = bridge
    token = "ghp_" + "x" * 32  # synthesized fixture, not a credential
    with pytest.raises(FactoryError, match="ARGUS_SENSITIVE_CONTENT"):
        asyncio.run(svc.plan_goal("w", "g", "p", token, "sensitive"))
    data = envelope()
    data["tasks"][0]["description"] = token
    with pytest.raises(FactoryError, match="ARGUS_SENSITIVE_CONTENT"):
        svc.import_plan("argus-p", "sensitive-ref", wrap(data))
    assert not client.messages and svc.store.factory.plan_ids() == []
    assert token.encode() not in svc.store.factory.path.read_bytes()


def test_failed_preflight_does_not_poison_request(bridge):
    svc, client = bridge
    original = client.project_list

    async def unavailable():
        raise FactoryError("ARGUS_PROTOCOL_ERROR")

    client.project_list = unavailable
    with pytest.raises(FactoryError):
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request"))
    client.project_list = original
    assert (
        asyncio.run(svc.plan_goal("w", "g", "p", "goal", "request"))["approval_status"]
        == "PROPOSED"
    )
    assert len(client.messages) == 1


def test_packaged_stdio_launch_without_argus_or_opencode(tmp_path):
    import shutil

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from app.hybrid_local.installation import portable_files

    source = Path(__file__).resolve().parents[1]
    release = tmp_path / "Release Sp äces" / "source"
    for original in portable_files(source):
        target = release / original.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
    reg = tmp_path / "projects.json"
    reg.write_text(registry().model_dump_json())
    env = {
        **os.environ,
        "FACTORY_STATE_PATH": str(tmp_path / "state.sqlite"),
        "FACTORY_PROJECT_REGISTRY": str(reg),
        "ARGUS_MCP_EXECUTABLE": str(tmp_path / "absent-argus.exe"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(release / "scripts/factory-local.py")],
        env=env,
        cwd=str(tmp_path),
    )

    async def run():
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 13
            read = await session.call_tool("factory_status", {})
            assert json.loads(read.content[0].text)["plans"] == []
            absent = await session.call_tool(
                "factory_argus_status", {"workspace_id": "w"}
            )
            assert (
                json.loads(absent.content[0].text)["error_code"]
                == "ARGUS_NOT_INSTALLED"
            )

    asyncio.run(run())
