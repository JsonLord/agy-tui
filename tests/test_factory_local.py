import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.automaker_local.policy import AutomakerError
from app.factory_local.schemas import (
    FactoryApproval,
    FactoryAttemptRef,
    FactoryExecutionState,
    FactoryJobRef,
    FactoryPlan,
)
from app.factory_local.service import FactoryService
from app.factory_local.store import FactoryError, FactoryStore


def envelope():
    return {
        "plan_id": "p",
        "goal": {"goal_id": "g", "argus_project_id": "argus-p"},
        "tasks": [
            {
                "task_id": i,
                "argus_task_id": "argus-" + i,
                "title": i,
                "workspace_id": "w",
                "repository": "owner/repo",
                "project_path": "C:/Work/Sp äces",
            }
            for i in "ABC"
        ],
        "dependencies": [
            {"task_id": "B", "requires_task_id": "A"},
            {"task_id": "C", "requires_task_id": "B"},
        ],
    }


class FakeAutomaker:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.fail = None
        self.after_create = False
        self.auto = False

    def projects_overview(self):
        return {"projects": [{"projectPath": "C:/Work/Sp äces"}]}

    def auto_mode_status(self, path):
        return {"isRunning": self.auto}

    def feature_create(self, path, feature):
        self.calls.append((path, copy.deepcopy(feature)))
        if self.fail == len(self.calls) and not self.after_create:
            raise AutomakerError("AUTOMAKER_CONFLICT")
        job = {
            **copy.deepcopy(feature),
            "id": "automaker-" + str(len(self.jobs) + 1),
            "status": "pending",
        }
        self.jobs[job["id"]] = job
        if self.fail == len(self.calls):
            raise AutomakerError("AUTOMAKER_TIMEOUT")
        return {"feature": copy.deepcopy(job)}

    def feature_get(self, path, job):
        return {"feature": copy.deepcopy(self.jobs.get(job))}

    def features_list(self, path):
        return {"features": copy.deepcopy(list(self.jobs.values()))}


@pytest.fixture
def system(tmp_path):
    api = FakeAutomaker()
    service = FactoryService(FactoryStore(tmp_path / "factory.sqlite"), api)
    plan = FactoryPlan.model_validate(envelope())
    service.register_plan(plan)
    return service, api, plan


def approval(plan, status="APPROVED", **overrides):
    return FactoryApproval.model_validate(
        {
            "approval_id": "a",
            "plan_id": plan.plan_id,
            "plan_revision": plan.revision(),
            "source": "manual",
            "status": status,
            "timestamp": datetime.now(UTC),
            "actor_ref": "operator",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "missing",
        "cycle",
        "self",
        "duplicate_edge",
        "cross_project",
        "empty",
        "extra",
    ],
)
def test_invalid_plan(mutation):
    data = envelope()
    if mutation == "duplicate":
        data["tasks"][1]["task_id"] = "A"
    if mutation == "missing":
        data["dependencies"][0]["requires_task_id"] = "X"
    if mutation == "cycle":
        data["dependencies"].append({"task_id": "A", "requires_task_id": "C"})
    if mutation == "self":
        data["dependencies"][0]["requires_task_id"] = "B"
    if mutation == "duplicate_edge":
        data["dependencies"].append(data["dependencies"][0])
    if mutation == "cross_project":
        data["tasks"][1]["project_path"] = "elsewhere"
    if mutation == "empty":
        data["tasks"] = []
    if mutation == "extra":
        data["model"] = "forbidden"
    with pytest.raises(ValidationError):
        FactoryPlan.model_validate(data)


@pytest.mark.parametrize("state", [None, "PROPOSED", "REJECTED"])
def test_approval_required(system, state):
    svc, api, plan = system
    if state:
        svc.record_approval(approval(plan, state))
    with pytest.raises(FactoryError, match="APPROVAL_REQUIRED"):
        svc.materialize_approved_plan("p")
    assert api.calls == []


def test_materialize_identity_dependencies_idempotency_restart(system):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    result = svc.materialize_approved_plan("p")
    assert not result["workers_started"]
    assert api.calls[1][1]["dependencies"] == ["automaker-1"]
    assert api.calls[2][1]["dependencies"] == ["automaker-2"]
    svc = FactoryService(FactoryStore(svc.store.path), api)
    assert svc.materialize_approved_plan("p") == result
    assert len(api.calls) == 3
    refs = svc.job_map("p")
    assert refs["goal"]["argus_project_id"] == "argus-p"
    assert refs["tasks"][0]["argus_task_id"] == "argus-A"
    assert refs["tasks"][0]["job_ref"]["approval_id"] == "a"
    assert refs["tasks"][0]["job_ref"]["key"] != "automaker-1"


@pytest.mark.parametrize("position", [1, 2, 3])
def test_partial_ambiguous_create_reconciles(system, position):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    api.fail, api.after_create = position, True
    with pytest.raises(AutomakerError, match="AUTOMAKER_TIMEOUT"):
        svc.materialize_approved_plan("p")
    api.fail = None
    svc = FactoryService(FactoryStore(svc.store.path), api)
    assert len(svc.materialize_approved_plan("p")["job_map"]) == 3
    assert len(api.calls) == 3


def test_failed_create_never_blindly_retries(system):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    api.fail = 2
    with pytest.raises(AutomakerError):
        svc.materialize_approved_plan("p")
    api.fail = None
    with pytest.raises(FactoryError, match="MATERIALIZATION_UNCERTAIN"):
        svc.materialize_approved_plan("p")
    assert len(api.calls) == 2
    assert svc.plan_status("p")["blockers"][0]["code"] == "MATERIALIZATION_UNCERTAIN"


@pytest.mark.parametrize("change", ["duplicate", "edit", "delete"])
def test_remote_conflict(system, change):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    api.fail, api.after_create = 1, True
    with pytest.raises(AutomakerError):
        svc.materialize_approved_plan("p")
    if change == "duplicate":
        api.jobs["another"] = {**api.jobs["automaker-1"], "id": "another"}
    if change == "edit":
        api.jobs["automaker-1"]["title"] = "edited"
    if change == "delete":
        api.jobs.clear()
    with pytest.raises(FactoryError):
        svc.materialize_approved_plan("p")
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pending", "PENDING"),
        ("running", "RUNNING"),
        ("completed", "VERIFYING"),
        ("verified", "COMPLETED"),
        ("failed", "FAILED"),
        ("unknown", "BLOCKED"),
    ],
)
def test_live_execution_authority(system, raw, expected):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    svc.materialize_approved_plan("p")
    api.jobs["automaker-1"]["status"] = raw
    read = svc.plan_status("p")
    assert read["approval_status"] == "APPROVED"
    assert read["jobs"][0]["execution_state"] == expected
    assert read["jobs"][0]["raw_status"] == raw
    assert read["jobs"][1]["dependency_ready"] == (raw == "verified")
    with svc.store.connect() as db:
        assert all("status" not in r[1] for r in db.execute("PRAGMA table_info(links)"))


def test_revision_approval_immutability(system):
    svc, _api, plan = system
    with pytest.raises(FactoryError):
        svc.record_approval(approval(plan, plan_revision="0" * 64))
    a = approval(plan)
    svc.record_approval(a)
    svc.record_approval(a)
    with pytest.raises(FactoryError):
        svc.record_approval(approval(plan, actor_ref="other"))
    changed = envelope()
    changed["tasks"][0]["title"] = "revision"
    with pytest.raises(FactoryError):
        svc.register_plan(FactoryPlan.model_validate(changed))
    svc.materialize_approved_plan("p")
    with pytest.raises(FactoryError):
        svc.record_approval(approval(plan, approval_id="new"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "clickup-future"),
        ("actor_ref", ""),
        ("timestamp", datetime(2026, 1, 1)),
    ],
)
def test_invalid_approval(system, field, value):
    with pytest.raises(ValidationError):
        approval(system[2], **{field: value})


def test_autonomy_blocks(system):
    svc, api, plan = system
    svc.record_approval(approval(plan))
    api.auto = True
    with pytest.raises(FactoryError, match="AUTOMAKER_AUTONOMY_NOT_IDLE"):
        svc.materialize_approved_plan("p")
    assert not api.calls


def test_store_rollback(system):
    svc, _api, plan = system
    with pytest.raises(RuntimeError), svc.store.connect() as db:
        db.execute("DELETE FROM plans")
        raise RuntimeError("crash")
    assert FactoryStore(svc.store.path).plan("p") == plan


def test_future_executor_fake():
    class FakeExecutor:
        def start(self, job, workspace):
            return FactoryAttemptRef(attempt_id="attempt", foreman_run_id="future-run")

        def status(self, run):
            return FactoryExecutionState.PENDING

        def cancel(self, run):
            self.cancelled = run.attempt_id

    fake = FakeExecutor()
    run = fake.start(
        FactoryJobRef(
            task_id="A", automaker_job_id="automaker-1", idempotency_key="key"
        ),
        "w",
    )
    assert fake.status(run) == FactoryExecutionState.PENDING
    fake.cancel(run)
    assert fake.cancelled == "attempt"


def test_mcp_surface_controls_reads_errors(system, monkeypatch):
    asyncio.run(check_mcp(system, monkeypatch))


async def check_mcp(system, monkeypatch):
    from app.factory_local import mcp as module

    svc, _api, plan = system
    monkeypatch.setattr(module, "_service", svc)
    tools = await module.mcp.list_tools()
    assert {t.name for t in tools} == set(module.TOOL_CLASSES)
    result = await module.factory_register_plan(envelope())
    assert result["plan_revision"] == plan.revision()
    rejected = await module.factory_materialize_approved_plan("p")
    assert rejected["error_code"] == "APPROVAL_REQUIRED"
    assert len(rejected["correlation_id"]) == 32
    await module.factory_record_approval(approval(plan).model_dump(mode="json"))
    assert (await module.factory_materialize_approved_plan("p"))[
        "workers_started"
    ] is False
    assert (await module.factory_plan_status("p"))["approval_status"] == "APPROVED"
    assert len((await module.factory_job_map("p"))["tasks"]) == 3
    assert (await module.factory_blockers("p"))["blockers"] == []
    assert len((await module.factory_status())["plans"]) == 1
    assert (await module.factory_register_plan({"secret": "private"}))[
        "error_code"
    ] == "FACTORY_INVALID_INPUT"

    def broken():
        raise RuntimeError("private secret")

    redacted = await module.response(broken)
    assert "private" not in json.dumps(redacted)
    assert redacted["error_code"] == "INTERNAL_ERROR"


def test_concurrent_materialization_one_create_per_task(system):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    svc, api, plan = system
    svc.record_approval(approval(plan))
    entered, release = threading.Event(), threading.Event()
    original = api.feature_create

    def delayed(path, feature):
        entered.set()
        assert release.wait(5)
        return original(path, feature)

    api.feature_create = delayed
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(svc.materialize_approved_plan, "p")
        assert entered.wait(5)
        with pytest.raises(FactoryError, match="MATERIALIZATION_UNCERTAIN"):
            svc.materialize_approved_plan("p")
        release.set()
        assert len(first.result()["job_map"]) == 3
    assert len(api.calls) == 3


def test_claim_without_external_call_blocks_safe_recovery(system):
    svc, api, plan = system
    a = approval(plan)
    svc.record_approval(a)
    svc.store.claim(plan, plan.tasks[0], svc.key(plan, plan.tasks[0]), a)
    recovered = FactoryService(FactoryStore(svc.store.path), api)
    with pytest.raises(FactoryError, match="MATERIALIZATION_UNCERTAIN"):
        recovered.materialize_approved_plan("p")
    assert not api.calls


def test_stdio_restart_real_client_boundary(tmp_path):
    import os
    import sys
    import threading

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from tests.test_automaker_local import _Handler, _Server

    api = FakeAutomaker()
    server = _Server(("127.0.0.1", 0), _Handler)
    server.records = []
    server.routes = {
        ("GET", "/api/projects/overview"): lambda h, b, q: (
            200,
            api.projects_overview(),
        ),
        ("POST", "/api/auto-mode/status"): lambda h, b, q: (
            200,
            api.auto_mode_status(b["projectPath"]),
        ),
        ("POST", "/api/features/create"): lambda h, b, q: (
            200,
            api.feature_create(b["projectPath"], b["feature"]),
        ),
        ("POST", "/api/features/get"): lambda h, b, q: (
            200,
            api.feature_get(b["projectPath"], b["featureId"]),
        ),
        ("POST", "/api/features/list"): lambda h, b, q: (
            200,
            api.features_list(b["projectPath"]),
        ),
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "FACTORY_STATE_PATH": str(tmp_path / "Sp äces" / "registry.sqlite"),
        "AUTOMAKER_BASE_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "AUTOMAKER_API_KEY": "synthetic-only",
    }
    launcher = Path(__file__).resolve().parents[1] / "scripts/factory-local.py"
    params = StdioServerParameters(
        command=sys.executable, args=[str(launcher)], env=env, cwd=str(tmp_path)
    )

    async def session(first):
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            assert len((await client.list_tools()).tools) == 7

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                return json.loads(result.content[0].text)

            if first:
                data = envelope()
                registered = await call("factory_register_plan", {"plan": data})
                p = FactoryPlan.model_validate(data)
                assert registered["plan_revision"] == p.revision()
                assert (
                    await call("factory_materialize_approved_plan", {"plan_id": "p"})
                )["error_code"] == "APPROVAL_REQUIRED"
                await call(
                    "factory_record_approval",
                    {"approval": approval(p).model_dump(mode="json")},
                )
            assert (
                len(
                    (await call("factory_materialize_approved_plan", {"plan_id": "p"}))[
                        "job_map"
                    ]
                )
                == 3
            )
            read = await call("factory_plan_status", {"plan_id": "p"})
            assert read["approval_status"] == "APPROVED"
            assert read["jobs"][0]["raw_status"] == ("pending" if first else "verified")

    try:
        asyncio.run(session(True))
        api.jobs["automaker-1"]["status"] = "verified"
        asyncio.run(session(False))
        assert len(api.calls) == 3
        assert {record["path"] for record in server.records} <= {
            "/api/projects/overview",
            "/api/auto-mode/status",
            "/api/features/create",
            "/api/features/get",
            "/api/features/list",
        }
        assert "synthetic-only" not in (
            tmp_path / "Sp äces" / "registry.sqlite"
        ).read_bytes().decode(errors="ignore")
    finally:
        server.shutdown()
        server.server_close()
