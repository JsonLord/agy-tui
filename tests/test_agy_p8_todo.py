"""
P8 Worker B — Todo PROJECTION tests (contract items 33-37).

 33 render is deterministic and ordered: PATH first, then checkpoints
    in execution order, gates G1..G4 each; ids and content exact
 34 status mapping: PASSED/NOT_APPLICABLE -> completed, ACTIVE ->
    in_progress, PENDING -> pending; blocked checkpoint gates skipped
 35 sync posts the rendered list to the OpenCode session todo API
    (injectable http); success shape
 36 sync failure (4xx/5xx/transport error) -> {"synced": False,
    "error": ...} — canonical plan state NEVER changes
 37 projection is a pure function of the plan document: a plan
    reconstructed from its serialized dict alone renders an
    identical todo list

The plan objects are duck-typed fixtures (Worker A's pydantic models
serialize/deserialize to the same shapes; guarded import exercises the
real ones at integration without changing a single assertion).
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# duck-typed plan fixtures (same field shapes as A's CheckpointPlan)
# ---------------------------------------------------------------------------

GATE_KINDS = ("G1_behavior", "G2_ui", "G3_adversarial", "G4_human")


def _gate(required: bool = True):
    return SimpleNamespace(required=required, reason=None if required else "no UI")


def _gates(**overrides):
    base = {kind: _gate(True) for kind in GATE_KINDS}
    for kind, gate in overrides.items():
        base[kind] = gate
    return SimpleNamespace(**base)


def _records(states, applicability=None):
    """states: {gate_kind: (status, applicability)} -> gate_records dict."""
    records = {}
    for kind in GATE_KINDS:
        status, appl = states.get(kind, ("PENDING", "REQUIRED"))
        records[kind] = SimpleNamespace(
            gate=kind,
            status=SimpleNamespace(value=status) if not isinstance(status, str) else status,
            applicability=appl,
        )
    return records


def _checkpoint(cid, *, deps=(), gates=None, records=None, status="PLANNED"):
    return SimpleNamespace(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="objective",
        dependencies=list(deps),
        gates=gates if gates is not None else _gates(),
        gate_records=records if records is not None else {},
        status=status,
    )


def make_plan():
    """Two-checkpoint plan: CP2 depends on CP1; mixed gate states."""
    cp1 = _checkpoint(
        "cp-1",
        records=_records(
            {
                "G1_behavior": ("PASSED", "REQUIRED"),
                "G2_ui": ("NOT_APPLICABLE_STATUS_UNUSED", "NOT_APPLICABLE"),
                "G3_adversarial": ("ACTIVE", "REQUIRED"),
            }
        ),
        gates=_gates(G2_ui=_gate(False)),
        status="ACTIVE",
    )
    cp2 = _checkpoint("cp-2", deps=["cp-1"])
    return SimpleNamespace(
        plan_id="plan-1",
        feature_id="refunds",
        objective="refund windows",
        checkpoints=[cp1, cp2],
        status="SUBMITTED",
        revision=1,
    )


def render_todos(plan):
    from app.orchestration.checkpoint_planning.todo_projection import (
        render_checkpoint_todos,
    )

    return render_checkpoint_todos(plan)


def sync_todos(session_id, plan, **kw):
    from app.orchestration.checkpoint_planning.todo_projection import (
        sync_checkpoint_todos,
    )

    return sync_checkpoint_todos(session_id, plan, **kw)


# ---------------------------------------------------------------------------
# 33: deterministic ordering + exact id/content shapes
# ---------------------------------------------------------------------------

def test_render_paths_and_order():
    plan = make_plan()
    todos = render_todos(plan)

    assert todos[0]["id"] == "PATH"
    assert todos[0]["content"].startswith("PATH: ")

    gate_ids = [t["id"] for t in todos if t["id"] != "PATH"]
    assert gate_ids == [f"{cp}:{g}" for cp in ("cp-1", "cp-2") for g in GATE_KINDS]

    # determinism: same input -> identical output
    assert render_todos(copy.deepcopy(plan)) == todos


def test_render_content_format():
    plan = make_plan()
    todos = {t["id"]: t for t in render_todos(plan)}

    assert todos["cp-1:G1_behavior"]["content"] == "cp-1 · G1 Behavior — REQUIRED"
    assert todos["cp-1:G2_ui"]["content"] == "cp-1 · G2 UI — N/A"
    for kind, label in zip(
        GATE_KINDS, ("G1 Behavior", "G2 UI", "G3 Adversarial", "G4 Human")
    ):
        assert todos[f"cp-2:{kind}"]["content"] == f"cp-2 · {label} — REQUIRED"


def test_render_path_item_tracks_next_open_gate():
    plan = make_plan()
    todos = {t["id"]: t for t in render_todos(plan)}
    # first open gate: cp-1 G3 ACTIVE
    assert todos["PATH"]["content"] == "PATH: cp-1 → G3_adversarial → ACTIVE"


def test_render_execution_order_respects_dependencies():
    plan = make_plan()
    # invert the list order; topo order must still put cp-1 first
    plan.checkpoints.reverse()
    gate_ids = [t["id"] for t in render_todos(plan) if t["id"] != "PATH"]
    assert gate_ids.index("cp-1:G1_behavior") < gate_ids.index("cp-2:G1_behavior")


# ---------------------------------------------------------------------------
# 34: status mapping
# ---------------------------------------------------------------------------

def test_status_mapping_rules():
    plan = make_plan()
    todos = {t["id"]: t["status"] for t in render_todos(plan)}

    assert todos["cp-1:G1_behavior"] == "completed"   # PASSED
    assert todos["cp-1:G2_ui"] == "completed"          # NOT_APPLICABLE
    assert todos["cp-1:G3_adversarial"] == "in_progress"  # ACTIVE
    assert todos["cp-1:G4_human"] == "pending"         # PENDING
    assert todos["cp-2:G1_behavior"] == "pending"


def test_blocked_checkpoint_gates_are_skipped():
    plan = make_plan()
    plan.checkpoints[1].status = "BLOCKED"
    todos = {t["id"]: t["status"] for t in render_todos(plan)}
    assert all(todos[f"cp-2:{g}"] == "skipped" for g in GATE_KINDS)


def test_rejected_gate_is_skipped():
    plan = make_plan()
    cp1 = plan.checkpoints[0]
    cp1.gate_records["G3_adversarial"].status = "REJECTED"
    todos = {t["id"]: t["status"] for t in render_todos(plan)}
    assert todos["cp-1:G3_adversarial"] == "skipped"


def test_preseeded_plan_without_records_renders_pending():
    plan = SimpleNamespace(
        plan_id="p",
        checkpoints=[_checkpoint("cp-1", records=None)],
    )
    # gate_records missing entirely (pre-submission DRAFT): still renders
    plan.checkpoints[0].gate_records = {}
    todos = render_todos(plan)
    statuses = {t["id"]: t["status"] for t in todos if t["id"] != "PATH"}
    assert set(statuses.values()) == {"pending"}


# ---------------------------------------------------------------------------
# 35: sync posts the projection to the todo API
# ---------------------------------------------------------------------------

def test_sync_puts_rendered_projection():
    calls = []

    def fake_http(url, payload, timeout):
        calls.append((url, json.loads(payload), timeout))
        return (200, "[]")

    plan = make_plan()
    result = sync_todos("ses_abc", plan, http=fake_http, server="http://x:1/")

    assert result["synced"] is True
    assert result["session_id"] == "ses_abc"
    url, body, _timeout = calls[0]
    assert url == "http://x:1/session/ses_abc/todo"
    assert body == render_todos(plan)


def test_sync_default_server_constant():
    from app.orchestration.checkpoint_planning.todo_projection import (
        DEFAULT_SERVER,
    )

    assert DEFAULT_SERVER == "http://127.0.0.1:4096"


def test_sync_requires_session_id():
    result = sync_todos("", make_plan(), http=lambda *a: (200, ""))
    assert result == {"synced": False, "error": "session_id is required"}


# ---------------------------------------------------------------------------
# 36: sync failure NEVER touches canonical state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_sync_http_error_returns_not_synced(status):
    plan = make_plan()
    snapshot_before = json.dumps(
        [c.checkpoint_id for c in plan.checkpoints], default=str
    )

    result = sync_todos(
        "ses_abc", plan, http=lambda url, payload, timeout: (status, "boom")
    )

    assert result["synced"] is False
    assert str(status) in result["error"]
    # canonical plan object untouched by the failed sync
    assert (
        json.dumps([c.checkpoint_id for c in plan.checkpoints], default=str)
        == snapshot_before
    )
    assert plan.status == "SUBMITTED"


def test_sync_transport_error_never_raises():
    def exploding_http(url, payload, timeout):
        raise ConnectionError("opencode server down")

    result = sync_todos("ses_abc", make_plan(), http=exploding_http)
    assert result["synced"] is False
    assert "transport error" in result["error"]


# ---------------------------------------------------------------------------
# 37: projection reconstructible from the plan document alone
# ---------------------------------------------------------------------------

def _plan_from_dict(payload):
    """Rebuild a duck-typed plan from the serialized dict alone."""
    gates = {
        kind: _gate(rec["applicability"] != "NOT_APPLICABLE")
        for kind, rec in payload["gates"].items()
    }
    return _checkpoint(
        payload["checkpoint_id"],
        deps=payload["dependencies"],
        gates=SimpleNamespace(**gates),
        records={
            kind: SimpleNamespace(
                gate=kind,
                status=rec["status"],
                applicability=rec["applicability"],
            )
            for kind, rec in payload["gates"].items()
        },
        status=payload["status"],
    )


def test_projection_reconstructible_from_plan_document():
    plan = make_plan()
    original = render_todos(plan)

    # serialize the canonical state to a document (what submission persists)
    doc = {
        "plan_id": plan.plan_id,
        "checkpoints": [
            {
                "checkpoint_id": cp.checkpoint_id,
                "dependencies": cp.dependencies,
                "status": cp.status,
                "gates": {
                    kind: {
                        "status": str(
                            getattr(rec.status, "value", rec.status)
                        ),
                        "applicability": rec.applicability,
                    }
                    for kind, rec in cp.gate_records.items()
                },
            }
            for cp in plan.checkpoints
        ],
    }
    text = json.dumps(doc)

    # rebuild plan objects FROM THE DOCUMENT ALONE
    rebuilt = json.loads(text)
    rebuilt_plan = SimpleNamespace(
        plan_id=rebuilt["plan_id"],
        checkpoints=[_plan_from_dict(cp) for cp in rebuilt["checkpoints"]],
    )

    assert render_todos(rebuilt_plan) == original
