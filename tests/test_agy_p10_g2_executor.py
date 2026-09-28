"""P10 — G2 UI read-only reviewer executor tests (fakes only).

Every external effect is behind an injectable seam: dispatch_worker,
gatekeeper, repo_snapshot, ui_path_snapshot, session_capturer, clock.
No network, no OpenCode, no subprocess (the only real git use is in the
prompt-boundary test, which feeds the gatekeeper a hand-built record).
"""

from __future__ import annotations

import json

import pytest

from app.orchestration.checkpoint_planning import g2_executor as g2x
from app.orchestration.checkpoint_planning.g1_executor import (
    GatekeeperDecision,
    GatekeeperUnavailable,
    MiMoProGatekeeper,
)
from app.orchestration.checkpoint_planning.g2_executor import (
    G2Executor,
    GateConfig2,
    MiMoProG2Gatekeeper,
    UIReviewUnavailable,
    build_live_g2_executor,
)
from app.orchestration.checkpoint_planning.gate_engine import GateEngine
from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
    evidence_is_sufficient,
)
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointStatus,
    GateExpectations,
    GateStatus,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records


# ---------------------------------------------------------------------------
# fakes / fixtures
# ---------------------------------------------------------------------------

PASS_VERDICT = json.dumps(
    {"verdict": "PASS", "findings": [], "summary": "matches the design"}
)
FAIL_VERDICT = json.dumps(
    {
        "verdict": "FAIL",
        "findings": [
            {"severity": "major", "file": "ui/index.html",
             "detail": "primary button color deviates from design.md"}
        ],
        "summary": "button off-spec",
    }
)


def make_checkpoint(cid: str = "cp-ui") -> Checkpoint:
    gates = CheckpointGates(
        G1_behavior=GateExpectations(required=False, reason="docs-only slice"),
        G2_ui=GateExpectations(
            required=True,
            focus=["visual fidelity vs design reference"],
        ),
        G3_adversarial=GateExpectations(required=True, planned_tests=[]),
        G4_human=GateExpectations(required=True, planned_tests=[]),
    )
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund settings page matches design.md",
        relevant_files=["ui/index.html"],
        implementation_context="reuse the settings layout",
        gates=gates,
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


class FakeReviewerResult:
    def __init__(self, ok: bool = True, output: str = PASS_VERDICT):
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.output = output


class ScriptedG2Gatekeeper:
    """Returns scripted decisions; captures every record handed to it."""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.seen: list[GateExecutionRecord] = []

    def decide(self, evidence, prior_repairs):
        self.seen.append(evidence)
        item = self._decisions.pop(0) if self._decisions else "ACCEPT"
        if isinstance(item, Exception):
            raise item
        if isinstance(item, GatekeeperDecision):
            return item
        return GatekeeperDecision(
            decision=item,
            reason=f"scripted {item}",
            repair_instructions="",
            session_id=f"sup-{len(self.seen)}",
        )


def _repo_view(**files):
    return dict(files)


def build_executor(
    *,
    repo="/tmp/fake-g2-repo",
    verdict_output=PASS_VERDICT,
    reviewer_ok=True,
    repo_snapshots=None,
    ui_snapshots=None,
    gatekeeper=None,
    dispatch=None,
    **kwargs,
):
    if repo_snapshots is None:
        # two calls per run: before + after the reviewer dispatch (equal
        # => the read-only guard held)
        repo_snapshots = [
            _repo_view(**{"ui/index.html": "h1"}),
            _repo_view(**{"ui/index.html": "h1"}),
        ]
    rq = list(repo_snapshots)
    tail_view = repo_snapshots[-1]

    def repo_snapshot(_repo):
        return rq.pop(0) if rq else dict(tail_view)

    if ui_snapshots is None:
        ui_snapshots = [{"ui/index.html": "h1"}]
    uq = list(ui_snapshots)
    ui_tail = ui_snapshots[-1]

    def ui_path_snapshot(_repo):
        return uq.pop(0) if uq else dict(ui_tail)

    calls: list[str] = []

    def default_dispatch(role, prompt, repo_arg):
        calls.append(role)
        default_dispatch.prompts.append(prompt)
        return FakeReviewerResult(ok=reviewer_ok, output=verdict_output)

    default_dispatch.prompts = []

    executor = G2Executor(
        kwargs.pop("repo", repo),
        ui_path=kwargs.pop("ui_path", "ui"),
        design_ref=kwargs.pop("design_ref", "design.md"),
        dispatch_worker=dispatch or default_dispatch,
        gatekeeper=gatekeeper
        if gatekeeper is not None
        else ScriptedG2Gatekeeper(["ACCEPT"]),
        session_capturer=lambda role, result, since_ms: (f"sess-{role}", "stdout"),
        clock=kwargs.pop("clock", lambda: 1_760_000_000.0),
        repo_snapshot=repo_snapshot,
        ui_path_snapshot=ui_path_snapshot,
        **kwargs,
    )
    executor.dispatch_calls = calls
    executor.dispatch_prompts = default_dispatch.prompts
    return executor


def status_of(checkpoint, gate="G2_ui"):
    return checkpoint.gate_records[gate].status


# ---------------------------------------------------------------------------
# 1. applicability
# ---------------------------------------------------------------------------


def test_g2_not_applicable_leaves_gate_pending():
    checkpoint = make_checkpoint()
    checkpoint.gates.G2_ui = GateExpectations(
        required=False, reason="no user-visible surface"
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    executor = build_executor()

    with pytest.raises(g2x.GateRuntimeError):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PENDING
    assert executor.transition_log == []  # engine never touched
    assert executor.emitted_records == []
    assert executor.dispatch_calls == []  # reviewer never dispatched


# ---------------------------------------------------------------------------
# 2. happy path
# ---------------------------------------------------------------------------


def test_g2_accept_passes_gate_on_verdict_plus_pro_accept():
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedG2Gatekeeper(["ACCEPT"])
    executor = build_executor(gatekeeper=gatekeeper)

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.is_passed() is True
    assert evidence_is_sufficient("G2_ui", rec) is True
    assert rec.supervisor_decision == "ACCEPT"
    assert rec.supervisor_session_id == "sup-1"
    assert rec.gate_id == "G2_ui"
    assert rec.changed_files == []  # read-only held

    roles = {entry["role"] for entry in rec.worker_sessions}
    assert roles == {"ui-reviewer"}
    assert all(entry["session_id"] for entry in rec.worker_sessions)
    assert executor.dispatch_calls == [g2x.ROLE_UI_REVIEWER]

    check_names = {c["name"] for c in rec.deterministic_checks}
    assert {"reviewer_guard", "ui_review", "reviewer_findings"} <= check_names
    assert all(c["ok"] for c in rec.deterministic_checks)

    # lifecycle: PENDING -> ACTIVE -> PASSED (never PENDING -> PASSED)
    log = [(frm, to, actor) for (_g, frm, to, actor) in executor.transition_log]
    assert log == [
        (GateStatus.PENDING, GateStatus.ACTIVE, "executor"),
        (GateStatus.ACTIVE, GateStatus.PASSED, "gatekeeper"),
    ]

    record = checkpoint.gate_records["G2_ui"]
    assert record.closed_by == "gatekeeper"
    assert record.evidence_refs == ["gate_evidence:G2_ui:attempt:1"]
    assert rec in executor.emitted_records

    # the gatekeeper reviewed a record whose verdict is evidence, stamped
    # only afterwards
    reviewed = gatekeeper.seen[0]
    assert reviewed.supervisor_decision is None
    ui_check = [c for c in reviewed.deterministic_checks if c["name"] == "ui_review"]
    assert ui_check and ui_check[0]["ok"] is True
    assert "matches the design" in ui_check[0]["detail"]


# ---------------------------------------------------------------------------
# 3. failing review rejects
# ---------------------------------------------------------------------------


def test_g2_reject_rejects_with_findings():
    checkpoint = make_checkpoint()
    executor = build_executor(
        verdict_output=FAIL_VERDICT,
        gatekeeper=ScriptedG2Gatekeeper(
            [
                GatekeeperDecision(
                    decision="REJECT",
                    reason="button off-spec",
                    repair_instructions="fix the primary button color",
                    session_id="sup-r",
                )
            ]
        ),
    )

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert rec.supervisor_decision == "REJECT"
    assert rec.is_passed() is False
    assert evidence_is_sufficient("G2_ui", rec) is False
    assert rec in executor.emitted_records
    assert checkpoint.gate_records["G2_ui"].closed_by == "gatekeeper"

    # findings are preserved in the record's check details
    blob = json.dumps(rec.to_dict(), default=str)
    assert "primary button color deviates" in blob
    assert "fix the primary button color" in blob  # gatekeeper_reject detail
    failed = [c for c in rec.deterministic_checks if c["ok"] is False]
    assert {c["name"] for c in failed} == {"ui_review", "reviewer_findings",
                                           "gatekeeper_reject"}


# ---------------------------------------------------------------------------
# 4. repo-mutation guard
# ---------------------------------------------------------------------------


def test_g2_reviewer_mutation_rejected():
    checkpoint = make_checkpoint()
    executor = build_executor(
        repo_snapshots=[
            _repo_view(**{"ui/index.html": "h1"}),
            # reviewer "read" a file into existence -> real change
            _repo_view(**{"ui/index.html": "h1", "ui/index.html.bak": "h2"}),
        ],
    )

    with pytest.raises(g2x.UIReviewerEscapedReadonly):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert checkpoint.gate_records["G2_ui"].closed_by == "reviewer_guard"
    terminal = executor.emitted_records[-1]
    guard = [c for c in terminal.deterministic_checks if c["name"] == "reviewer_guard"]
    assert guard and guard[0]["ok"] is False
    assert "ui/index.html.bak" in guard[0]["detail"]
    assert all(r.supervisor_decision is None for r in executor.emitted_records)
    # no PASSED anywhere, gatekeeper never consulted
    assert executor.transition_log[-1][2] == GateStatus.REJECTED
    assert terminal.gate_id == "G2_ui"


def test_g2_tooling_artifacts_do_not_trip_the_guard():
    checkpoint = make_checkpoint()
    executor = build_executor(
        repo_snapshots=[
            _repo_view(**{"ui/index.html": "h1"}),
            _repo_view(**{
                "ui/index.html": "h1",
                "__pycache__/noise.pyc": "pyc",
                ".serena/project.yml": "serena",
            }),
        ],
    )
    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert rec.supervisor_decision == "ACCEPT"
    assert rec.changed_files == []


# ---------------------------------------------------------------------------
# 5. invalid verdict fails closed
# ---------------------------------------------------------------------------


def test_g2_invalid_verdict_fail_closed():
    checkpoint = make_checkpoint()
    executor = build_executor(verdict_output="the page looks fine to me!")

    with pytest.raises(UIReviewUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.ACTIVE  # resumable
    assert executor.emitted_records == []  # nothing terminal written
    log = [(frm, to) for (_g, frm, to, _a) in executor.transition_log]
    assert log == [(GateStatus.PENDING, GateStatus.ACTIVE)]


def test_g2_wrong_verdict_shape_fails_closed():
    checkpoint = make_checkpoint()
    executor = build_executor(
        verdict_output=json.dumps({"verdict": "MAYBE", "findings": []})
    )
    with pytest.raises(UIReviewUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.ACTIVE


# ---------------------------------------------------------------------------
# 6. NEEDS_CONTEXT: one bounded slice, then fail closed
# ---------------------------------------------------------------------------


def _needs_context(request):
    return GatekeeperDecision(
        decision="NEEDS_CONTEXT", reason="need the design file",
        context_request=request,
    )


def _ui_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ui").mkdir(parents=True)
    (repo / "ui" / "index.html").write_text(
        "<html><body><button class='primary'>Refund</button></body></html>\n"
    )
    (repo / "design.md").write_text("# Design\nPrimary button: brand blue.\n")
    return repo


def test_g2_needs_context_one_slice_then_accept(tmp_path):
    repo = _ui_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedG2Gatekeeper(
        [_needs_context("ui/index.html:full"), "ACCEPT"]
    )
    executor = build_executor(repo=repo, gatekeeper=gatekeeper)

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.supervisor_decision == "ACCEPT"
    slice_ = rec.implementation_evidence["context_slice"]
    assert slice_["request"] == "ui/index.html:full"
    assert "<button class='primary'>Refund</button>" in slice_["content"]
    assert slice_["truncated"] is False
    assert len(gatekeeper.seen) == 2  # exactly one re-review
    assert gatekeeper.seen[1].implementation_evidence["context_slice"]["request"] == (
        "ui/index.html:full"
    )


def test_g2_second_needs_context_rejects(tmp_path):
    repo = _ui_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedG2Gatekeeper(
        [_needs_context("ui/index.html:full"), _needs_context("design.md:full")]
    )
    executor = build_executor(repo=repo, gatekeeper=gatekeeper)

    with pytest.raises(g2x.GatekeeperContextUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert len(gatekeeper.seen) == 2  # exactly one slice re-review
    terminal = executor.emitted_records[-1]
    assert terminal.supervisor_decision == "NEEDS_CONTEXT"
    assert evidence_is_sufficient("G2_ui", terminal) is False
    failed = [c for c in terminal.deterministic_checks
              if c["name"] == "context_request"]
    assert failed and failed[0]["ok"] is False


def test_g2_escaping_context_request_fails_closed(tmp_path):
    repo = _ui_repo(tmp_path)
    (tmp_path / "secret.md").write_text("SECRET = 1\n")
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedG2Gatekeeper([_needs_context("../secret.md:full")])
    executor = build_executor(repo=repo, gatekeeper=gatekeeper)

    with pytest.raises(g2x.GatekeeperContextUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert len(gatekeeper.seen) == 1  # invalid request -> NO re-review
    assert "SECRET" not in json.dumps(
        executor.emitted_records[-1].to_dict(), default=str)


# ---------------------------------------------------------------------------
# 7. Pro prompt is compact structured JSON (bounded)
# ---------------------------------------------------------------------------


def _envelope(content, env_id="chatcmpl-g2"):
    return json.dumps({"id": env_id, "choices": [{"message": {"content": content}}]})


def _g2_record_for_gatekeeper(**overrides) -> GateExecutionRecord:
    doc = {
        "plan_id": "plan-1",
        "checkpoint_id": "cp-ui",
        "gate_id": "G2_ui",
        "attempt": 1,
        "requirements": {
            "required": True,
            "objective": "refund settings page matches design.md",
            "design_ref": "design.md",
            "ui_path": "ui",
            "ui_files": ["ui/index.html"],
            "focus": ["visual fidelity vs design reference"],
        },
        "worker_sessions": [{"role": "ui-reviewer", "session_id": "s1", "ok": True}],
        "changed_files": [],
        "deterministic_checks": [{"name": "reviewer_guard", "ok": True,
                                  "detail": "read-only held"}],
        "red_evidence": None,
        "green_evidence": {"ok": True, "returncode": 0, "stdout_tail": "PASS"},
        "cumulative_gate": {"ok": True, "returncode": 0},
        "implementation_evidence": {
            "git_stat": " ui/index.html | 2 ++\n",
            "product_diff": "MARKER_G2_DIFF +<button class='primary'>",
            "test_files": [],
            "changed_symbols": [],
            "truncated": False,
        },
        "supervisor_session_id": None,
        "supervisor_decision": None,
        "repair_lineage": [],
        "timestamps": {"started_at": "t0", "finished_at": "t1"},
    }
    doc.update(overrides)
    return GateExecutionRecord(**doc)


def test_g2_prompt_bounded():
    captured: dict = {}

    def http(url, headers, payload, timeout):
        captured["payload"] = payload
        return 200, _envelope('{"decision": "ACCEPT", "reason": "consistent"}')

    keeper = MiMoProG2Gatekeeper(
        base_url="https://example.invalid/v1", model="m", http=http
    )
    fat = _g2_record_for_gatekeeper(
        implementation_evidence={
            "git_stat": "s" * 200_000,
            "product_diff": "d" * 200_000 + " MARKER_G2_DIFF",
            "test_files": [],
            "changed_symbols": [],
            "truncated": True,
        },
    )
    decision = keeper.decide(fat, [])
    assert decision.decision == "ACCEPT"
    assert decision.session_id == "chatcmpl-g2"

    messages = captured["payload"]["messages"]
    size = sum(len(str(m.get("content", ""))) for m in messages)
    assert size < 15_000  # the Pro timeout needs a small prompt
    user_prompt = messages[-1]["content"]
    system_prompt = messages[0]["content"]
    assert "refund settings page matches design.md" in user_prompt  # objective
    assert "design.md" in user_prompt                                # design_ref
    assert "MARKER_G2_DIFF" in user_prompt                            # bounded diff
    assert "G2_ui" in user_prompt
    assert "d" * 5_000 not in user_prompt  # the fat diff was capped to the tail
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert "REPAIR is NOT a valid G2 decision" in system_prompt


def test_g2_gatekeeper_rejects_repair_decision_fail_closed():
    keeper = MiMoProG2Gatekeeper(
        base_url="https://example.invalid/v1", model="m",
        http=lambda *a: (200, _envelope('{"decision": "REPAIR", "reason": "r"}')),
    )
    with pytest.raises(GatekeeperUnavailable):
        keeper.decide(_g2_record_for_gatekeeper(), [])
    # it is the G1 gatekeeper class with the G2 prompt authority
    assert isinstance(keeper, MiMoProGatekeeper)


def test_g2_malformed_gatekeeper_answer_fails_closed():
    keeper = MiMoProG2Gatekeeper(
        base_url="https://example.invalid/v1", model="m",
        http=lambda *a: (200, _envelope("looks fine to me, no JSON")),
    )
    with pytest.raises(GatekeeperUnavailable):
        keeper.decide(_g2_record_for_gatekeeper(), [])


# ---------------------------------------------------------------------------
# executor-level fail-closed + dispatch shape
# ---------------------------------------------------------------------------


def test_g2_missing_gatekeeper_fails_closed_at_review():
    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=None)
    executor._gatekeeper = None
    executor._gatekeeper_config = None
    with pytest.raises(GatekeeperUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.ACTIVE
    assert executor.emitted_records == []


def test_g2_gatekeeper_unavailable_leaves_gate_active():
    class Exploding:
        def decide(self, evidence, prior_repairs):
            raise GatekeeperUnavailable("pro model down")

    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=Exploding())
    with pytest.raises(GatekeeperUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.ACTIVE
    assert executor.emitted_records == []


def test_g2_reviewer_dispatch_prompt_is_read_only_contract():
    executor = build_executor()
    executor.execute_checkpoint(
        "plan-1", make_checkpoint(), GateEngine(make_checkpoint())
    )
    prompt = executor.dispatch_prompts[0]
    assert "design.md" in prompt
    assert "ui" in prompt
    assert '"verdict":"PASS|FAIL"' in prompt
    assert "You are read-only." in prompt


def test_g2_engine_default_constructed_from_checkpoint():
    """execute_checkpoint works with no explicit engine (fresh engine over
    seeded records), and a re-run on a terminal gate refuses."""
    checkpoint = make_checkpoint()
    rec = build_executor().execute_checkpoint("plan-1", checkpoint)
    assert rec.supervisor_decision == "ACCEPT"
    with pytest.raises(g2x.GateRuntimeError):
        build_executor().execute_checkpoint("plan-1", checkpoint)


# ---------------------------------------------------------------------------
# 8. live factory
# ---------------------------------------------------------------------------


def test_g2_live_factory_builds():
    executor = build_live_g2_executor(
        "/tmp/fake-g2-repo",
        ui_path="ui",
        design_ref="design.md",
        gatekeeper_base_url="https://example.invalid/v1",
        gatekeeper_model="helmholtz/alias-mimo-v2.6",
    )
    assert isinstance(executor, G2Executor)
    assert isinstance(executor.config, GateConfig2)
    assert executor.config.ui_path == "ui"
    assert executor.config.design_ref == "design.md"
    assert isinstance(executor._gatekeeper, MiMoProG2Gatekeeper)
    # real seams: default live dispatch + default read-back capturer +
    # real git snapshot are wired (methods, not injected fakes)
    assert executor._dispatch_worker == executor._default_dispatch_worker
    assert executor._session_capturer == executor._default_session_capturer
    assert executor._repo_snapshot is g2x._default_repo_snapshot
    assert executor._ui_path_snapshot == executor._default_ui_path_snapshot


def test_g2_default_path_snapshot_hashes_ui_tree(tmp_path):
    repo = _ui_repo(tmp_path)
    (repo / "ui" / "__pycache__").mkdir()
    (repo / "ui" / "__pycache__" / "x.pyc").write_bytes(b"noise")
    snap = g2x._default_path_snapshot(repo, "ui")
    assert set(snap) == {"ui/index.html"}
    # content-addressed: an edit changes only that entry
    (repo / "ui" / "index.html").write_text("<html>2</html>\n")
    snap2 = g2x._default_path_snapshot(repo, "ui")
    assert set(snap2) == set(snap)
    assert snap2["ui/index.html"] != snap["ui/index.html"]
    # a missing prefix is simply empty (never crashes)
    assert g2x._default_path_snapshot(repo, "nope") == {}
