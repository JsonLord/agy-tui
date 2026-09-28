"""P9 — G1 Behavior/TDD executor tests (fakes only).

Every external effect is behind an injectable seam: run_test,
dispatch_worker, gatekeeper, session_capturer, repo_snapshot,
test_path_snapshot, sentinel. No network, no OpenCode, no subprocess,
no real sentinel daemon.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.orchestration.checkpoint_planning import g1_executor as g1x
from app.orchestration.checkpoint_planning.g1_executor import (
    CumulativeRegression,
    GateRuntimeError,
    GatekeeperDecision,
    GatekeeperUnavailable,
    G1Executor,
    GreenNotAchieved,
    MiMoProGatekeeper,
    RedNotDemonstrated,
)
# The two guard exceptions start with "Test" by contract, so they are only
# ever referenced module-qualified (g1x.Test...) — binding them as bare
# module-level names would make pytest try to collect them as test classes.
from app.orchestration.checkpoint_planning.gate_evidence import (
    GateExecutionRecord,
    g1_evidence_is_sufficient,
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


def make_checkpoint(cid: str = "cp-1") -> Checkpoint:
    gates = CheckpointGates(
        G1_behavior=GateExpectations(
            required=True,
            planned_tests=["tests/test_refund_window.py"],
            focus=["expired window rejected"],
            test_instructions=["cover boundary at window expiry"],
        ),
        G2_ui=GateExpectations(required=False, reason="no UI surface"),
        G3_adversarial=GateExpectations(required=True, planned_tests=[]),
        G4_human=GateExpectations(required=True, planned_tests=[]),
    )
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        relevant_files=["app/refunds.py"],
        implementation_context="keep the existing RefundPolicy API",
        gates=gates,
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


class FakeWorkerResult:
    def __init__(self, ok: bool = True, output: str = "session: worker-1"):
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = output
        self.stderr = ""
        self.output = output


class ScriptedGatekeeper:
    """Returns scripted decisions; captures every evidence handed to it."""

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
            repair_instructions="make the boundary case pass"
            if item == "REPAIR"
            else "",
            session_id=f"sup-{len(self.seen)}",
        )


class ExplodingGatekeeper:
    def decide(self, evidence, prior_repairs):
        raise GatekeeperUnavailable("pro model down")


def build_executor(
    *,
    test_results=None,          # list of (rc, stdout, stderr) per run_test call
    repo_snapshots=None,        # list of dict snapshots for repo_snapshot calls
    test_snapshots=None,        # list of dict snapshots for test_path_snapshot
    gatekeeper=None,
    max_attempts=3,
    sentinel=None,
    clock=None,
    use_default_run_test=False,
    **kwargs,
):
    run_test = None
    if not use_default_run_test:
        results = list(test_results or [(1, "FAIL: test_window", ""),
                                        (0, "ok", ""), (0, "suite ok", "")])
        idx = {"i": 0}

        def run_test(cmd, cwd):  # noqa: F811
            rc, out, err = results[min(idx["i"], len(results) - 1)]
            idx["i"] += 1
            return rc, out, err

    if repo_snapshots is None:
        repo_snapshots = [
            {"app/refunds.py": "h1"},
            {"app/refunds.py": "h1", "tests/test_refund_window.py": "h2"},
        ]
    rq = list(repo_snapshots)

    def repo_snapshot(_repo):
        return rq.pop(0) if rq else dict(rq_state)

    rq_state = repo_snapshots[-1]

    if test_snapshots is None:
        test_snapshots = [{"tests/test_refund_window.py": "h2"}] * 8
    tq = list(test_snapshots)

    def test_path_snapshot(_repo):
        return tq.pop(0) if tq else dict(test_snapshots[-1])

    calls: list[str] = []

    def dispatch_worker(role, prompt, repo):
        calls.append(role)
        return FakeWorkerResult()

    executor = G1Executor(
        kwargs.pop("repo", "/tmp/fake-repo"),
        test_path=kwargs.pop("test_path", "tests"),
        test_command=kwargs.pop("test_command", ["pytest", "-q"]),
        full_suite_command=kwargs.pop(
            "full_suite_command", ["pytest", "-q", "tests/"]
        ),
        max_attempts=max_attempts,
        run_test=run_test,
        dispatch_worker=dispatch_worker,
        gatekeeper=gatekeeper
        if gatekeeper is not None
        else ScriptedGatekeeper(["ACCEPT"]),
        sentinel=sentinel,
        session_capturer=lambda role, text: f"sess-{role}",
        clock=clock or (lambda: 1_760_000_000.0),
        test_path_snapshot=test_path_snapshot,
        repo_snapshot=repo_snapshot,
        **kwargs,
    )
    executor.dispatch_calls = calls
    return executor


def status_of(checkpoint, gate="G1_behavior"):
    return checkpoint.gate_records[gate].status


# ---------------------------------------------------------------------------
# HAPPY PATH
# ---------------------------------------------------------------------------


def test_happy_path_passes_the_gate_on_evidence_plus_accept():
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["ACCEPT"])
    executor = build_executor(gatekeeper=gatekeeper)

    rec = executor.execute_g1(checkpoint, plan_id="plan-1")

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.is_passed() is True
    assert g1_evidence_is_sufficient(rec) is True
    assert rec.supervisor_decision == "ACCEPT"
    assert rec.supervisor_session_id == "sup-1"
    assert rec.red_evidence is not None and rec.green_evidence is not None
    assert rec.cumulative_gate is not None

    roles = {entry["role"] for entry in rec.worker_sessions}
    assert roles == {"test-author", "implementer"}
    assert all(entry["session_id"] for entry in rec.worker_sessions)

    check_names = {c["name"] for c in rec.deterministic_checks}
    assert {
        "author_guard", "red", "implement_guard", "green", "cumulative",
    } == check_names
    assert all(c["ok"] for c in rec.deterministic_checks)

    record = checkpoint.gate_records["G1_behavior"]
    assert record.status == GateStatus.PASSED
    assert record.closed_by == "gatekeeper"
    assert record.closed_at is not None

    # lifecycle: PENDING -> ACTIVE -> PASSED (never PENDING -> PASSED)
    log = [(frm, to) for (_g, frm, to, _a) in executor.transition_log]
    assert log == [
        (GateStatus.PENDING, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.PASSED),
    ]

    # the gatekeeper reviewed a record carrying RED+GREEN+cumulative
    reviewed = gatekeeper.seen[0]
    assert reviewed.red_evidence is not None
    assert reviewed.supervisor_decision is None  # decision is stamped after


def test_gate_never_passes_without_accept_even_with_full_green_runs():
    """Hard invariant: deterministic RED+GREEN+cumulative alone is NOT
    enough — a REJECT decision still REJECTs the gate."""
    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=ScriptedGatekeeper(["REJECT"]))
    rec = executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.REJECTED
    assert rec.supervisor_decision == "REJECT"
    assert rec.is_passed() is False


# ---------------------------------------------------------------------------
# guards + deterministic failures
# ---------------------------------------------------------------------------


def _expect_rejected_failure(run, exc_type, **executor_kwargs):
    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=ScriptedGatekeeper(["ACCEPT"]),
                              **executor_kwargs)
    with pytest.raises(exc_type):
        run(checkpoint, executor)
    assert status_of(checkpoint) == GateStatus.REJECTED
    terminal = [r for r in executor.emitted_records]
    assert terminal, "a REJECTED evidence record must be persisted"
    assert all(r.supervisor_decision is None for r in terminal)
    return checkpoint, executor


def test_test_author_escaping_test_paths_rejects_gate():
    def run(checkpoint, executor):
        return executor.execute_g1(checkpoint, plan_id="plan-1")

    _expect_rejected_failure(
        run,
        g1x.TestAuthorEscapedTestPaths,
        repo_snapshots=[
            {"app/refunds.py": "h1", "tests/test_refund_window.py": "h0"},
            # author "changed" production code + added a test
            {"app/refunds.py": "CHANGED", "tests/test_refund_window.py": "h1"},
        ],
    )


def test_tooling_artifacts_do_not_trigger_author_guard():
    """Live-discovered (P9 smoke): running ANY OpenCode agent in a fresh
    repo materializes Serena/Graphify/bytecode dirs as a side effect. The
    author guard must ignore these while still seeing real test files."""
    executor = build_executor(
        repo_snapshots=[
            {"app/refunds.py": "h1"},
            {
                "app/refunds.py": "h1",
                "tests/test_refund_window.py": "h1",
                "__pycache__/refunds.cpython-313.pyc": "pyc",
                ".serena/project.yml": "serena",
                "graphify-out/graph.json": "graph",
            },
        ],
    )
    record = executor.execute_g1(make_checkpoint(), plan_id="plan-1")
    assert record.changed_files == ["tests/test_refund_window.py"]
    assert record.supervisor_decision == "ACCEPT"
    # the guard's detail must not name the artifact dirs
    guard = [c for c in record.deterministic_checks if c["name"] == "author_guard"]
    assert guard and "serena" not in guard[0]["detail"].lower()


def test_tooling_artifacts_do_not_mask_a_real_escape():
    """Artifacts are filtered, but a REAL production-file change alongside
    them must still be rejected."""
    _expect_rejected_failure(
        lambda cp, ex: ex.execute_g1(cp, plan_id="plan-1"),
        g1x.TestAuthorEscapedTestPaths,
        repo_snapshots=[
            {"app/refunds.py": "h1"},
            {
                "app/refunds.py": "CHANGED",  # real escape, next to artifacts
                "tests/test_refund_window.py": "h1",
                "__pycache__/x.pyc": "pyc",
                ".serena/project.yml": "serena",
            },
        ],
    )


def test_is_tooling_artifact_only_matches_top_level():
    from app.orchestration.checkpoint_planning.g1_executor import (
        _is_tooling_artifact,
    )

    assert _is_tooling_artifact("__pycache__/x.pyc")
    assert _is_tooling_artifact(".serena/project.yml")
    assert _is_tooling_artifact("./graphify-out/graph.json")
    # a file merely NAMED like an artifact dir under a real source tree is
    # NOT filtered — real content still counts
    assert not _is_tooling_artifact("app/__pycache__/x.pyc")
    assert not _is_tooling_artifact("tests/test_refund_window.py")
    assert not _is_tooling_artifact("my__pycache__.py")


def test_red_not_demonstrated_rejects_gate():
    _expect_rejected_failure(
        lambda cp, ex: ex.execute_g1(cp, plan_id="plan-1"),
        RedNotDemonstrated,
        # first run_test (RED) exits 0 -> nothing was missing
        test_results=[(0, "all passed already", ""), (0, "", ""), (0, "", "")],
    )


def test_test_file_tampered_by_implementer_rejects_gate():
    _expect_rejected_failure(
        lambda cp, ex: ex.execute_g1(cp, plan_id="plan-1"),
        g1x.TestFileTampered,
        test_results=[(1, "FAIL", ""), (0, "ok", ""), (0, "ok", "")],
        # calls: frozen (after RED), implementer_before, implementer_after
        test_snapshots=[
            {"tests/test_refund_window.py": "h2"},
            {"tests/test_refund_window.py": "h2"},
            {"tests/test_refund_window.py": "TAMPERED"},
        ],
    )


def test_green_not_achieved_rejects_gate():
    _expect_rejected_failure(
        lambda cp, ex: ex.execute_g1(cp, plan_id="plan-1"),
        GreenNotAchieved,
        test_results=[(1, "FAIL", ""), (1, "still failing", ""), (0, "", "")],
    )


def test_cumulative_regression_rejects_gate():
    _expect_rejected_failure(
        lambda cp, ex: ex.execute_g1(cp, plan_id="plan-1"),
        CumulativeRegression,
        test_results=[(1, "FAIL", ""), (0, "ok", ""), (1, "suite broke", "")],
    )


def test_contract_violations_raise_before_anything_runs():
    # G1 NOT_APPLICABLE in the plan -> executor refuses
    checkpoint = make_checkpoint()
    checkpoint.gates.G1_behavior = GateExpectations(
        required=False, reason="docs only", planned_tests=[]
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    executor = build_executor()
    with pytest.raises(GateRuntimeError):
        executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.PENDING

    # required G1 without planned_tests -> refuse
    checkpoint2 = make_checkpoint()
    checkpoint2.gates.G1_behavior = GateExpectations(required=True)
    checkpoint2.gate_records = seed_gate_records(checkpoint2)
    with pytest.raises(GateRuntimeError):
        build_executor().execute_g1(checkpoint2, plan_id="plan-1")

    # unseeded checkpoint -> refuse
    checkpoint3 = make_checkpoint()
    checkpoint3.gate_records = {}
    with pytest.raises(GateRuntimeError):
        build_executor().execute_g1(checkpoint3, plan_id="plan-1")


# ---------------------------------------------------------------------------
# bounded repair loop
# ---------------------------------------------------------------------------


def test_repair_loop_is_bounded_and_freezes_the_tests():
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["REPAIR", "REPAIR", "ACCEPT"])
    executor = build_executor(
        gatekeeper=gatekeeper,
        max_attempts=3,
        # enough results for 3 attempts: (RED, GREEN, CUM) x3
        test_results=[(1, "F", "")] + [(0, "ok", "")] * 8,
        repo_snapshots=[{"app/refunds.py": "h1"},
                        {"app/refunds.py": "h1",
                         "tests/test_refund_window.py": "h2"}],
        test_snapshots=[{"tests/test_refund_window.py": "h2"}] * 20,
    )

    rec = executor.execute_g1(checkpoint, plan_id="plan-1")

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.attempt == 3
    assert rec.repair_lineage == [1, 2]
    assert g1_evidence_is_sufficient(rec) is True

    roles = executor.dispatch_calls
    # TEST_AUTHOR ran exactly ONCE; only the implementer re-runs per repair
    assert roles.count("TEST_AUTHOR") == 1
    assert roles.count("IMPLEMENTER") == 3

    # lifecycle through the repairs
    log = [(frm, to) for (_g, frm, to, _a) in executor.transition_log]
    assert log == [
        (GateStatus.PENDING, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.REPAIR),
        (GateStatus.REPAIR, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.REPAIR),
        (GateStatus.REPAIR, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.PASSED),
    ]
    # every repair attempt left its own evidence record behind
    attempts = [r.attempt for r in executor.emitted_records]
    assert attempts == [1, 2, 3]
    assert [r.supervisor_decision for r in executor.emitted_records] == [
        "REPAIR", "REPAIR", "ACCEPT",
    ]


def test_repair_exhaustion_rejects_gate():
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["REPAIR", "REPAIR", "REPAIR", "REPAIR"])
    executor = build_executor(
        gatekeeper=gatekeeper,
        max_attempts=2,
        test_results=[(1, "F", "")] + [(0, "ok", "")] * 8,
        test_snapshots=[{"tests/test_refund_window.py": "h2"}] * 20,
    )
    rec = executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.REJECTED
    assert rec.attempt == 2
    assert rec.supervisor_decision == "REPAIR"  # last review, gate still closed
    assert executor.dispatch_calls.count("IMPLEMENTER") == 2  # bounded


def test_gatekeeper_reject_persists_record_and_rejects_gate():
    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=ScriptedGatekeeper(["REJECT"]))
    rec = executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.REJECTED
    assert rec.supervisor_decision == "REJECT"
    assert rec in executor.emitted_records
    assert checkpoint.gate_records["G1_behavior"].closed_by == "gatekeeper"


# ---------------------------------------------------------------------------
# fail-closed gatekeeper states
# ---------------------------------------------------------------------------


def test_gatekeeper_unavailable_leaves_gate_active_and_propagates():
    checkpoint = make_checkpoint()
    executor = build_executor(gatekeeper=ExplodingGatekeeper())
    with pytest.raises(GatekeeperUnavailable):
        executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.ACTIVE
    log = [(frm, to) for (_g, frm, to, _a) in executor.transition_log]
    assert log == [(GateStatus.PENDING, GateStatus.ACTIVE)]  # nothing written
    assert executor.emitted_records == []  # no terminal record persisted


def test_missing_gatekeeper_fails_closed_at_review():
    """No gatekeeper and no gatekeeper_config: the executor raises
    GatekeeperUnavailable at step 9 — never a silent skip."""
    checkpoint = make_checkpoint()
    flips = {"n": 0}

    def repo_snapshot(_r):
        flips["n"] += 1
        if flips["n"] == 1:
            return {"app/x.py": "1"}
        return {"app/x.py": "1", "tests/test_x.py": "1"}

    results = {"i": 0}

    def run_test(_cmd, _cwd):
        results["i"] += 1
        return (1, "F", "") if results["i"] == 1 else (0, "ok", "")

    executor = G1Executor(
        "/tmp/fake-repo",
        test_path="tests",
        test_command=["pytest", "-q"],
        gatekeeper=None,               # <-- no gatekeeper
        gatekeeper_config=None,        # <-- and no live config
        run_test=run_test,
        dispatch_worker=lambda role, prompt, repo: FakeWorkerResult(),
        session_capturer=lambda role, text: f"sess-{role}",
        clock=lambda: 1_760_000_000.0,
        repo_snapshot=repo_snapshot,
        test_path_snapshot=lambda r: {"tests/test_x.py": "1"},
    )
    with pytest.raises(GatekeeperUnavailable):
        executor.execute_g1(checkpoint, plan_id="plan-1")
    assert status_of(checkpoint) == GateStatus.ACTIVE
    assert executor.emitted_records == []


# ---------------------------------------------------------------------------
# long-run sentinel seam (fake sentinel; no daemon, no subprocess)
# ---------------------------------------------------------------------------


class FakeSentinel:
    def __init__(self):
        self.spawn_calls = 0
        self.snapshot_calls = 0

    def spawn_monitored_process(self, command, cwd, deadline_s=None, **kw):
        self.spawn_calls += 1
        self.last_command = command
        return "job-1"

    def run_until(self, job_id, predicate, **kw):
        # models never busy-poll: the runtime asks once, on the event.
        return SimpleNamespace(state="EXITED_SUCCESS")

    def get_process_snapshot(self, job_id):
        self.snapshot_calls += 1
        return {
            "job_id": job_id,
            "state": "EXITED_SUCCESS",
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
        }


def test_long_run_default_path_hands_off_to_sentinel_exactly_once():
    sentinel = FakeSentinel()
    ticks = iter([0.0, 45.0, 60.0])
    executor = G1Executor(
        "/tmp/fake-repo",
        test_path="tests",
        test_command=["pytest", "-q"],
        long_run_threshold_s=30.0,
        sentinel=sentinel,
        clock=lambda: next(ticks),
    )
    rc, out, err = executor._default_run_test(["pytest", "-q"], "/tmp/fake-repo")
    assert (rc, out, err) == (0, "done", "")
    assert sentinel.spawn_calls == 1
    assert sentinel.snapshot_calls <= 1  # no busy-polling loop


# ---------------------------------------------------------------------------
# MiMoProGatekeeper transport (fake http only)
# ---------------------------------------------------------------------------


def _envelope(content, env_id="chatcmpl-1"):
    return json.dumps(
        {"id": env_id, "choices": [{"message": {"content": content}}]}
    )


def _evidence_for_gatekeeper() -> GateExecutionRecord:
    return GateExecutionRecord(
        plan_id="plan-1",
        checkpoint_id="cp-1",
        gate_id="G1_behavior",
        attempt=1,
        requirements={"required": True, "planned_tests": ["tests/test_x.py"]},
        worker_sessions=[{"role": "test-author", "session_id": "s1", "ok": True}],
        changed_files=["tests/test_x.py"],
        deterministic_checks=[{"name": "red", "ok": True, "detail": "exit 1"}],
        red_evidence={"ok": True, "returncode": 1},
        green_evidence={"ok": True, "returncode": 0},
        cumulative_gate={"ok": True, "returncode": 0},
        supervisor_session_id=None,
        supervisor_decision=None,
        repair_lineage=[],
        timestamps={"started_at": "t0", "finished_at": "t1"},
    )


def test_mimo_gatekeeper_accept_round_trip_with_fake_http():
    captured: dict = {}

    def http(url, headers, payload, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return 200, _envelope('{"decision": "ACCEPT", "reason": "solid"}')

    keeper = MiMoProGatekeeper(
        base_url="https://example.invalid/v1",
        model="helmholtz/alias-mimo-v2.6",
        http=http,
    )
    decision = keeper.decide(_evidence_for_gatekeeper(), [])
    assert decision.decision == "ACCEPT"
    assert decision.reason == "solid"
    assert decision.session_id == "chatcmpl-1"


def test_mimo_gatekeeper_malformed_answer_fails_closed():
    def http(url, headers, payload, timeout):
        return 200, _envelope("I think it looks fine to me, no JSON here")

    keeper = MiMoProGatekeeper(
        base_url="https://example.invalid/v1", model="m", http=http
    )
    with pytest.raises(GatekeeperUnavailable):
        keeper.decide(_evidence_for_gatekeeper(), [])


def test_mimo_gatekeeper_http_500_fails_closed():
    def http(url, headers, payload, timeout):
        return 500, "upstream exploded"

    keeper = MiMoProGatekeeper(
        base_url="https://example.invalid/v1", model="m", http=http
    )
    with pytest.raises(GatekeeperUnavailable):
        keeper.decide(_evidence_for_gatekeeper(), [])


def test_mimo_gatekeeper_prompt_is_compact_structured_json():
    captured: dict = {}

    def http(url, headers, payload, timeout):
        captured["payload"] = payload
        return 200, _envelope('{"decision": "REPAIR", "reason": "r", '
                              '"repair_instructions": "do x"}')

    keeper = MiMoProGatekeeper(
        base_url="https://example.invalid/v1", model="m", http=http
    )
    evidence = _evidence_for_gatekeeper()
    # even with oversized run output, the prompt stays compact (bounded tails)
    fat = GateExecutionRecord(
        **{
            **evidence.to_dict(),
            "red_evidence": {"ok": True, "returncode": 1,
                             "stdout_tail": "F" * 200_000},
            "green_evidence": {"ok": True, "returncode": 0,
                               "stdout_tail": "." * 200_000},
            "cumulative_gate": {"ok": True, "returncode": 0,
                                "stderr": "S" * 200_000},
        }
    )
    decision = keeper.decide(fat, [evidence])
    assert decision.decision == "REPAIR"
    assert decision.repair_instructions == "do x"

    messages = captured["payload"]["messages"]
    size = sum(len(str(m.get("content", ""))) for m in messages)
    assert size < 15_000  # generous cap — the Pro timeout needs a small prompt
    user_prompt = messages[-1]["content"]
    assert "G1_behavior" in user_prompt
    assert "cp-1" in user_prompt
    assert captured["payload"]["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Live-discovered (P9 smoke #2): running the suite rewrites
# tests/__pycache__/*.pyc; the implement guard compares the TEST-PATH
# snapshot, so bytecode must not read as a test-file change.
# ---------------------------------------------------------------------------


def test_default_test_path_snapshot_ignores_bytecode(tmp_path):
    from app.orchestration.checkpoint_planning.g1_executor import (
        _default_test_path_snapshot,
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_a.py").write_text("x = 1\n")
    (tests / "__pycache__").mkdir()
    (tests / "__pycache__" / "test_a.cpython-313.pyc").write_bytes(b"pyc1")
    (tmp_path / "test_top.py").write_text("y = 2\n")
    (tmp_path / "test_top.pyc").write_bytes(b"pyc2")
    (tmp_path / "top_test.py").write_text("w = 0  # not a test by convention\n")
    (tmp_path / "prod.py").write_text("z = 3\n")

    snap = _default_test_path_snapshot(tmp_path)
    assert set(snap) == {"tests/test_a.py", "test_top.py"}

    # a test run rewriting bytecode does NOT change the snapshot...
    (tests / "__pycache__" / "test_a.cpython-313.pyc").write_bytes(b"pyc1-rewritten")
    (tests / "__pycache__" / "test_a.cpython-313-pytest-9.1.1.pyc").write_bytes(b"new")
    assert _default_test_path_snapshot(tmp_path) == snap
    # ...but a change to the test SOURCE is still caught
    (tests / "test_a.py").write_text("x = 1  # tampered\n")
    assert _default_test_path_snapshot(tmp_path) != snap


def test_implement_guard_allows_bytecode_but_catches_source_tamper(tmp_path):
    """End-to-end through the executor with the REAL default test snapshot:
    the implementer runs the suite (bytecode appears) -> guard passes;
    a source edit of the frozen test -> TestFileTampered."""
    from app.orchestration.checkpoint_planning.g1_executor import (
        G1Executor,
        TestFileTampered,
    )
    from tests.test_agy_p9_g1_executor import (
        ScriptedGatekeeper,
        make_checkpoint,
    )

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_frozen.py").write_text("t = 1\n")
    (repo / "prod.py").write_text("v = 0\n")

    frozen = (
        repo / "tests" / "__pycache__"
    )
    frozen.mkdir()

    def worker(role, prompt, _repo):
        # implementer "runs the tests": bytecode appears/rewrites, and in
        # the tamper case also edits the frozen test source
        (frozen / "test_frozen.cpython-313.pyc").write_bytes(
            b"bytecode-" + role.encode()
        )
        if role == "IMPLEMENTER":
            (repo / "prod.py").write_text("v = 1  # implemented\n")
            if executor_mode["tamper"]:
                (repo / "tests" / "test_frozen.py").write_text("t = 1  # gutted\n")
        return type("R", (), {"ok": True, "output": "Session ID: ses-x"})()

    run_calls = {"n": 0}

    def run_test(_cmd, _cwd):
        run_calls["n"] += 1
        return (1, "FAIL", "") if run_calls["n"] == 1 else (0, "ok", "")

    from app.orchestration.checkpoint_planning.g1_executor import (
        _default_test_path_snapshot,
    )

    executor_mode = {"tamper": False}
    cp = make_checkpoint()
    ex = G1Executor(
        repo,
        test_path="tests",
        test_command=["python3", "-m", "unittest"],
        dispatch_worker=worker,
        run_test=run_test,
        gatekeeper=ScriptedGatekeeper(["ACCEPT"]),
        test_path_snapshot=_default_test_path_snapshot,
    )
    rec = ex.execute_g1(cp, plan_id="p-1")  # bytecode rewrite must NOT trip the guard
    assert rec.supervisor_decision == "ACCEPT"

    # now the same run with a real test-source edit -> must be rejected
    executor_mode["tamper"] = True
    run_calls["n"] = 0
    (repo / "prod.py").write_text("v = 0\n")
    (frozen / "test_frozen.cpython-313.pyc").unlink()
    cp2 = make_checkpoint()
    ex2 = G1Executor(
        repo,
        test_path="tests",
        test_command=["python3", "-m", "unittest"],
        dispatch_worker=worker,
        run_test=run_test,
        gatekeeper=ScriptedGatekeeper(["ACCEPT"]),
        test_path_snapshot=_default_test_path_snapshot,
    )
    try:
        ex2.execute_g1(cp2, plan_id="p-1")
        assert False, "expected TestFileTampered"
    except TestFileTampered as e:
        assert "tests/test_frozen.py" in str(e)
