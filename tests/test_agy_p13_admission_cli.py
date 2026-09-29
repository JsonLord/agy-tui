"""P13 — CLI tests: `checkpoint admit` (thin wrapper over the admission
service). Model-free, tmp-dir state only. Three cases:

* valid continuation + matching G0 revision -> exit 0, canonical plan
  submitted, next gate reported;
* idempotent re-admit -> exit 0 with the idempotency note;
* fail-closed rejection (stale G0 revision) -> exit 1, nothing persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import checkpoint_main

PLAN_ID = "plan-cli-admit"
CP_ID = "cp-a"
REQUEST_ID = "hpir-cli-1"


def _state(root: Path, **kwargs) -> Path:
    """Lay down the P12 artifact shape under ``root`` with CLI-test ids."""
    from tests.test_agy_p13_admission import (
        FEEDBACK,
        REQUEST_ID as SERVICE_REQUEST_ID,
        _now,
        _plan_doc,
        _request_doc,
    )

    doc = _plan_doc()
    doc["plan_id"] = PLAN_ID
    doc["checkpoints"][0]["checkpoint_id"] = CP_ID
    request = _request_doc()
    request.update(
        request_id=REQUEST_ID,
        plan_id=PLAN_ID,
        checkpoint_id=CP_ID,
        continuation_plan_id=PLAN_ID,
    )
    request_dir = root / "runs" / PLAN_ID / "human" / CP_ID / "planner_inputs"
    request_dir.mkdir(parents=True)
    (request_dir / f"{REQUEST_ID}.json").write_text(
        json.dumps(request, indent=2), encoding="utf-8"
    )
    continuation_dir = root / "plans" / PLAN_ID / "continuations"
    continuation_dir.mkdir(parents=True)
    artifact = {
        "continuation": doc,
        "human_provenance": {
            "source_request_id": REQUEST_ID,
            "plan_id": PLAN_ID,
            "checkpoint_id": CP_ID,
            "gate_id": "G4_human",
            "feedback": FEEDBACK,
            "actor": "human:test",
            "created_at": _now(),
            "consumed_at": _now(),
        },
    }
    (continuation_dir / f"{REQUEST_ID}.json").write_text(
        json.dumps(artifact, indent=2), encoding="utf-8"
    )
    return root


def test_cli_admit_success(tmp_path, capsys):
    state = _state(tmp_path)
    code = checkpoint_main(
        [
            "admit",
            "--plan-id", PLAN_ID,
            "--request-id", REQUEST_ID,
            "--revision", "2",
            "--approver", "human:cli-approver",
            "--state-root", str(state),
        ]
    )
    assert code == 0
    plan_doc = json.loads(
        (state / "plans" / PLAN_ID / "checkpoint_plan.json").read_text(encoding="utf-8")
    )
    assert plan_doc["status"] == "SUBMITTED"
    assert plan_doc["g0"]["decision"] == "APPROVE"
    assert plan_doc["g0"]["approver"] == "human:cli-approver"
    out = capsys.readouterr().out
    assert f"admitted {PLAN_ID}" in out
    assert f"next gate {CP_ID}:G1_behavior" in out
    record = json.loads(
        (state / "plans" / PLAN_ID / "continuations" / "admissions"
         / f"{REQUEST_ID}.json").read_text(encoding="utf-8")
    )
    assert record["status"] == "ADMITTED"
    assert record["approver"] == "human:cli-approver"


def test_cli_admit_idempotent(tmp_path, capsys):
    state = _state(tmp_path)
    first = checkpoint_main(
        ["admit", "--plan-id", PLAN_ID, "--request-id", REQUEST_ID,
         "--revision", "2", "--state-root", str(state)]
    )
    assert first == 0
    capsys.readouterr()
    second = checkpoint_main(
        ["admit", "--plan-id", PLAN_ID, "--request-id", REQUEST_ID,
         "--revision", "2", "--state-root", str(state)]
    )
    assert second == 0
    assert "idempotent" in capsys.readouterr().out


def test_cli_admit_stale_revision_rejected(tmp_path, capsys):
    state = _state(tmp_path)
    code = checkpoint_main(
        ["admit", "--plan-id", PLAN_ID, "--request-id", REQUEST_ID,
         "--revision", "1", "--state-root", str(state)]
    )
    assert code == 1
    assert "revision" in capsys.readouterr().err
    # nothing was persisted — the canonical plan document does not exist
    assert not (state / "plans" / PLAN_ID / "checkpoint_plan.json").exists()
    assert not (
        state / "plans" / PLAN_ID / "continuations" / "admissions"
    ).exists()
