"""P10 — G3 Adversarial executor tests (fakes only).

Rig mirrors tests/test_agy_p9_g1_executor.py: every external effect is
behind an injectable seam (run_test, dispatch_worker, gatekeeper,
session_capturer, repo_snapshot, test_path_snapshot). The scripted
snapshots are STATEFUL views updated by the fake workers' actual writes,
so guards see exactly what a real git snapshot would. No network, no
OpenCode, no subprocess (build_implementation_evidence tolerates the
non-repo path; the tmp git repos below make the diff evidence real).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app.orchestration.checkpoint_planning import g3_executor as g3x
# TestFileTampered starts with "Test" by contract, so it is only ever
# referenced module-qualified (g3x.TestFileTampered) — binding it as a
# bare name would make pytest try to collect it as a test class.
from app.orchestration.checkpoint_planning.g1_executor import (
    GateRuntimeError,
    GatekeeperContextUnavailable,
    GatekeeperDecision,
    GatekeeperUnavailable,
)
from app.orchestration.checkpoint_planning.gate_engine import GateEngine
from app.orchestration.checkpoint_planning.gate_evidence import GateExecutionRecord
from app.orchestration.checkpoint_planning.g3_executor import (
    AdversarialReviewUnavailable,
    FixerEscapedScope,
    G3Executor,
    MAX_FIX_CYCLES,
    MiMoProG3Gatekeeper,
    ReviewerMutatedRepo,
    build_live_g3_executor,
)
from app.orchestration.checkpoint_planning.schemas import (
    Checkpoint,
    CheckpointGates,
    CheckpointStatus,
    GateExpectations,
    GateStatus,
)
from app.orchestration.checkpoint_planning.submission import seed_gate_records

REVIEWER = g3x.ROLE_ADVERSARIAL_REVIEWER
FIXER = g3x.ROLE_FIXER


# ---------------------------------------------------------------------------
# fakes / fixtures
# ---------------------------------------------------------------------------


def make_checkpoint(cid: str = "cp-1", *, relevant=("app/refunds.py",)) -> Checkpoint:
    gates = CheckpointGates(
        G1_behavior=GateExpectations(
            required=True, planned_tests=["tests/test_refund_window.py"]
        ),
        G2_ui=GateExpectations(required=False, reason="no UI surface"),
        G3_adversarial=GateExpectations(
            required=True, focus=["failure modes", "edge cases", "security"]
        ),
        G4_human=GateExpectations(required=True, planned_tests=[]),
    )
    checkpoint = Checkpoint(
        checkpoint_id=cid,
        title=f"checkpoint {cid}",
        objective="refund window behaviour",
        relevant_files=list(relevant),
        implementation_context="keep the existing RefundPolicy API",
        gates=gates,
        status=CheckpointStatus.PLANNED,
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    return checkpoint


def review_doc(verdict: str, findings=(), summary="reviewed") -> str:
    return json.dumps(
        {"verdict": verdict, "findings": list(findings), "summary": summary}
    )


CRITICAL_FINDING = {
    "severity": "critical", "file": "app/refunds.py", "line": 12,
    "detail": "negative days accepted",
}
MINOR_FINDING = {
    "severity": "minor", "file": "app/refunds.py", "line": None,
    "detail": "naming could be clearer",
}


class WorkerResult:
    def __init__(self, output: str = "", ok: bool = True):
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.output = output
        self.stdout = output
        self.stderr = ""


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
            repair_instructions="fix the critical finding in app/refunds.py"
            if item == "REPAIR"
            else "",
            session_id=f"sup-{len(self.seen)}",
        )


def _needs_context(request: str) -> GatekeeperDecision:
    return GatekeeperDecision(
        decision="NEEDS_CONTEXT", reason="need the source", context_request=request
    )


def make_repo(tmp_path):
    """Real throwaway git repo with the product file + a test file."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "refunds.py").write_text(
        "class RefundPolicy:\n"
        "    def check_window(self, days):\n"
        "        return days <= 30\n"
        "\n"
        "    def unused_helper(self):\n"
        "        return 1\n"
    )
    (repo / "tests" / "test_refund_window.py").write_text(
        "def test_window():\n    assert True\n"
    )

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            check=True,
        )

    git("init", "-q")
    git("config", "user.email", "agy-test@localhost")
    git("config", "user.name", "AGY Test")
    git("add", "-A")
    git("commit", "-qm", "base")
    return repo


class StatefulSnapshots:
    """repo/test views as dicts updated by the fake workers' actual writes."""

    def __init__(self):
        self.repo_view: dict[str, str] = {}
        self.test_view: dict[str, str] = {}

    def repo_snapshot(self, _repo):
        return dict(self.repo_view)

    def test_path_snapshot(self, _repo):
        return dict(self.test_view)


def build_executor(
    *,
    tmp_repo=None,
    reviewer_outputs=None,   # list of str (JSON docs); exhausted -> last
    reviewer_bad=False,       # produce prose instead of JSON
    reviewer_mutates=False,
    fixer_effect=None,        # callable(): mutate snapshots (and files)
    gatekeeper=None,
    run_test_results=None,    # list of (rc, out, err) per call; exhausted -> last
    max_cycles=MAX_FIX_CYCLES,
    snapshots=None,
    **kwargs,
):
    snaps = snapshots or StatefulSnapshots()
    if tmp_repo is not None:
        snaps.repo_view.setdefault("app/refunds.py", "h1")
        snaps.repo_view.setdefault("tests/test_refund_window.py", "t1")
        snaps.test_view.setdefault("tests/test_refund_window.py", "t1")

    outputs = list(reviewer_outputs or [review_doc("PASS", [], "clean")])
    last_output = outputs[-1]
    idx = {"t": 0}

    def dispatch_worker(role, prompt, repo):
        if role == REVIEWER:
            if reviewer_bad:
                return WorkerResult("I looked around and it seems fine, honestly")
            out = outputs.pop(0) if outputs else last_output
            if reviewer_mutates:
                snaps.repo_view["app/refunds.py"] = "MUTATED-BY-REVIEWER"
            return WorkerResult(out)
        if role == FIXER:
            if fixer_effect is not None:
                fixer_effect()
            return WorkerResult("fixed")
        raise AssertionError(f"unexpected role {role!r}")

    results = list(run_test_results or [(0, "ok", "")])

    def run_test(_cmd, _cwd):
        item = results[min(idx["t"], len(results) - 1)]
        idx["t"] += 1
        return item

    executor = G3Executor(
        kwargs.pop("repo", tmp_repo if tmp_repo is not None else "/tmp/fake-repo"),
        test_path=kwargs.pop("test_path", "tests"),
        test_command=kwargs.pop("test_command", ["pytest", "-q"]),
        full_suite_command=kwargs.pop(
            "full_suite_command", ["pytest", "-q", "tests/"]
        ),
        max_cycles=max_cycles,
        run_test=run_test,
        dispatch_worker=dispatch_worker,
        gatekeeper=gatekeeper
        if gatekeeper is not None
        else ScriptedGatekeeper(["ACCEPT"]),
        session_capturer=lambda role, result, since_ms: (f"sess-{role}-{since_ms}", "stdout"),
        clock=kwargs.pop("clock", lambda: 1_760_000_000.0),
        test_path_snapshot=snaps.test_path_snapshot,
        repo_snapshot=snaps.repo_snapshot,
        **kwargs,
    )
    executor.snaps = snaps
    return executor


def status_of(checkpoint, gate="G3_adversarial"):
    return checkpoint.gate_records[gate].status


def transitions(executor):
    return [(frm, to) for (_g, frm, to, _a) in executor.transition_log]


# ---------------------------------------------------------------------------
# 1. applicability
# ---------------------------------------------------------------------------


def test_g3_not_applicable_leaves_gate_pending():
    checkpoint = make_checkpoint()
    checkpoint.gates.G3_adversarial = GateExpectations(
        required=False, reason="review waived by the plan"
    )
    checkpoint.gate_records = seed_gate_records(checkpoint)
    executor = build_executor()
    with pytest.raises(GateRuntimeError):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.PENDING
    assert executor.emitted_records == []
    assert executor.transition_log == []  # engine untouched


# ---------------------------------------------------------------------------
# 2. accept
# ---------------------------------------------------------------------------


def test_g3_accept_passes(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["ACCEPT"])
    executor = build_executor(tmp_repo=repo, gatekeeper=gatekeeper)

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.is_passed() is True
    assert rec.supervisor_decision == "ACCEPT"
    assert rec.supervisor_session_id == "sup-1"
    names = {c["name"] for c in rec.deterministic_checks}
    assert {"reviewer_guard", "regression", "adversarial_review"} == names
    assert all(c["ok"] for c in rec.deterministic_checks)
    assert rec.red_evidence is None
    assert rec.green_evidence is not None and rec.cumulative_gate is not None
    assert {e["role"] for e in rec.worker_sessions} == {"adversarial-reviewer"}
    assert rec.implementation_evidence["review_verdict"] == "PASS"
    assert transitions(executor) == [
        (GateStatus.PENDING, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.PASSED),
    ]
    assert checkpoint.gate_records["G3_adversarial"].closed_by == "gatekeeper"
    assert checkpoint.gate_records["G3_adversarial"].evidence_refs  # evidence attached


# ---------------------------------------------------------------------------
# 3. repair cycle then accept
# ---------------------------------------------------------------------------


def test_g3_repair_cycle_then_accept(tmp_path):
    repo = make_repo(tmp_path)

    def in_scope_fix():
        (repo / "app" / "refunds.py").write_text(
            "class RefundPolicy:\n"
            "    def check_window(self, days):\n"
            "        return 0 <= days <= 30\n"
        )
        executor.snaps.repo_view["app/refunds.py"] = "h2"

    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["REPAIR", "ACCEPT"])
    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[
            review_doc("FAIL", [CRITICAL_FINDING], "negative days accepted"),
            review_doc("PASS", [], "fixed"),
        ],
        fixer_effect=in_scope_fix,
        gatekeeper=gatekeeper,
    )

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.attempt == 2
    assert rec.repair_lineage == [1]
    assert rec.supervisor_decision == "ACCEPT"
    roles = [(e["role"], e.get("cycle")) for e in rec.worker_sessions]
    assert ("fixer", 1) in roles and ("adversarial-reviewer", 2) in roles
    assert "app/refunds.py" in rec.changed_files
    assert "+        return 0 <= days <= 30" in rec.implementation_evidence["product_diff"]
    # every cycle left a record; the first one was the REPAIR review
    assert [r.supervisor_decision for r in executor.emitted_records] == [
        "REPAIR", "ACCEPT",
    ]
    assert executor.emitted_records[0].implementation_evidence[
        "adversarial_findings"
    ][0]["severity"] == "critical"
    assert transitions(executor) == [
        (GateStatus.PENDING, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.REPAIR),
        (GateStatus.REPAIR, GateStatus.ACTIVE),
        (GateStatus.ACTIVE, GateStatus.PASSED),
    ]
    # the fixer prompt carried the gatekeeper instructions + blocking findings
    reviewed_2 = gatekeeper.seen[1]
    assert reviewed_2.requirements["last_repair_instructions"]


# ---------------------------------------------------------------------------
# 4. cycle budget exhaustion -> REJECTED with escalation intent
# ---------------------------------------------------------------------------


def test_g3_cycle_budget_exhausted_rejects(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["REPAIR", "REPAIR", "REPAIR"])
    fix_calls = {"n": 0}

    def counting_fix():
        fix_calls["n"] += 1

    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[review_doc("FAIL", [CRITICAL_FINDING], "still broken")] * 5,
        fixer_effect=counting_fix,
        gatekeeper=gatekeeper,
    )

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert rec.attempt == 3
    assert rec.supervisor_decision == "REJECT"  # exhausted budget never passes
    assert rec.repair_lineage == [1, 2]
    assert fix_calls["n"] == 2  # MAX=3 reviews -> 2 fixes, bounded
    failed = [c for c in rec.deterministic_checks if c["name"] == "fixer_cycles"]
    assert failed and failed[0]["ok"] is False
    assert "fixer cycle budget exhausted (3)" in failed[0]["detail"]
    assert "escalation pending (P11)" in failed[0]["detail"]
    assert rec.requirements["escalation"] == "pending (P11)"
    assert checkpoint.gate_records["G3_adversarial"].closed_by == "gatekeeper"


# ---------------------------------------------------------------------------
# 5. fixer touches tests -> TestFileTampered
# ---------------------------------------------------------------------------


def test_g3_fixer_touches_test_rejects(tmp_path):
    repo = make_repo(tmp_path)

    def tamper():
        (repo / "tests" / "test_refund_window.py").write_text("def test_window():\n    assert 1 == 1\n")
        executor.snaps.test_view["tests/test_refund_window.py"] = "TAMPERED"
        executor.snaps.repo_view["tests/test_refund_window.py"] = "TAMPERED"

    checkpoint = make_checkpoint()
    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[
            review_doc("FAIL", [CRITICAL_FINDING], "broken"),
            review_doc("PASS", [], "ok"),
        ],
        fixer_effect=tamper,
        gatekeeper=ScriptedGatekeeper(["REPAIR", "ACCEPT"]),
    )

    with pytest.raises(g3x.TestFileTampered):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    terminal = executor.emitted_records[-1]
    guard = [c for c in terminal.deterministic_checks if c["name"] == "fixer_test_integrity"]
    assert guard and guard[0]["ok"] is False
    assert "tests/test_refund_window.py" in guard[0]["detail"]


# ---------------------------------------------------------------------------
# 6. fixer escapes relevant_files -> FixerEscapedScope
# ---------------------------------------------------------------------------


def test_g3_fixer_escapes_scope_rejects(tmp_path):
    repo = make_repo(tmp_path)

    def escape():
        (repo / "app" / "other.py").write_text("ESCAPED = 1\n")
        executor.snaps.repo_view["app/other.py"] = "e1"

    checkpoint = make_checkpoint()  # relevant_files = ["app/refunds.py"]
    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[
            review_doc("FAIL", [CRITICAL_FINDING], "broken"),
            review_doc("PASS", [], "ok"),
        ],
        fixer_effect=escape,
        gatekeeper=ScriptedGatekeeper(["REPAIR", "ACCEPT"]),
    )

    with pytest.raises(FixerEscapedScope):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    terminal = executor.emitted_records[-1]
    scope = [c for c in terminal.deterministic_checks if c["name"] == "fixer_scope"]
    assert scope and scope[0]["ok"] is False
    assert "app/other.py" in scope[0]["detail"]


# ---------------------------------------------------------------------------
# 7. regression failure is impossible-to-ACCEPT (invariant)
# ---------------------------------------------------------------------------


def test_g3_regression_failure_blocks_accept(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["ACCEPT"])  # Pro accepts anyway
    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[review_doc("PASS", [], "looks fine")],
        run_test_results=[(1, "FAILED: 3 tests", "boom")],
        gatekeeper=gatekeeper,
    )

    # the invariant makes a scripted ACCEPT over a failed regression impossible
    with pytest.raises(GateRuntimeError, match="invariant violated"):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    # fail-closed: nothing terminal was written; gate stays ACTIVE, resumable
    assert status_of(checkpoint) == GateStatus.ACTIVE
    assert executor.emitted_records == []

    # the reviewed evidence carried the failed check (evidence, not approval)
    reviewed = gatekeeper.seen[0]
    regression = [c for c in reviewed.deterministic_checks if c["name"] == "regression"]
    assert regression and regression[0]["ok"] is False


def test_g3_regression_failure_repair_instead(tmp_path):
    """Pro behaves: sees the failed regression check, REPAIRs; second cycle
    green + PASS -> gate ACCEPTs."""
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    executor = build_executor(
        tmp_repo=repo,
        reviewer_outputs=[review_doc("PASS", [])],
        run_test_results=[(1, "FAILED", ""), (0, "ok", ""), (0, "ok", "")],
        gatekeeper=ScriptedGatekeeper(["REPAIR", "ACCEPT"]),
    )
    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.PASSED
    first = executor.emitted_records[0]
    reg = [c for c in first.deterministic_checks if c["name"] == "regression"]
    assert reg[0]["ok"] is False  # the failed run was evidence handed to Pro
    assert rec.attempt == 2


# ---------------------------------------------------------------------------
# 8. invalid findings fail closed + resumable
# ---------------------------------------------------------------------------


def test_g3_invalid_findings_fail_closed(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(["ACCEPT"])

    snaps = StatefulSnapshots()
    snaps.repo_view.update({"app/refunds.py": "h1", "tests/test_refund_window.py": "t1"})
    snaps.test_view.update({"tests/test_refund_window.py": "t1"})

    mode = {"bad": True}

    def dispatch_worker(role, prompt, repo_str):
        assert role == REVIEWER  # never reached the fixer
        if mode["bad"]:
            return WorkerResult("The code looks fine to me — prose, no JSON.")
        return WorkerResult(review_doc("PASS", [], "clean"))

    executor = G3Executor(
        repo,
        test_path="tests",
        test_command=["pytest", "-q"],
        full_suite_command=["pytest", "-q", "tests/"],
        run_test=lambda cmd, cwd: (0, "ok", ""),
        dispatch_worker=dispatch_worker,
        gatekeeper=gatekeeper,
        session_capturer=lambda role, result, since_ms: (f"sess-{role}", "stdout"),
        clock=lambda: 1_760_000_000.0,
        repo_snapshot=snaps.repo_snapshot,
        test_path_snapshot=snaps.test_path_snapshot,
    )

    with pytest.raises(AdversarialReviewUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.ACTIVE  # stays ACTIVE
    assert executor.emitted_records == []             # nothing terminal written

    # resume: a second execute call on the SAME checkpoint continues
    mode["bad"] = False
    engine = GateEngine(checkpoint)
    rec = executor.execute_checkpoint("plan-1", checkpoint, engine)
    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.attempt == 1  # the failed attempt was never consumed as a cycle


# ---------------------------------------------------------------------------
# 9. needs-context: one bounded slice per cycle
# ---------------------------------------------------------------------------


def test_g3_needs_context_one_slice_then_accept(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(
        [_needs_context("app/refunds.py:full"), "ACCEPT"]
    )
    executor = build_executor(tmp_repo=repo, gatekeeper=gatekeeper)

    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.PASSED
    assert rec.supervisor_decision == "ACCEPT"
    slice_ = rec.implementation_evidence["context_slice"]
    assert slice_["request"] == "app/refunds.py:full"
    assert "class RefundPolicy" in slice_["content"]
    assert len(gatekeeper.seen) == 2  # exactly one re-review


def test_g3_needs_context_symbol_slice_capped(tmp_path):
    repo = make_repo(tmp_path)
    big = (repo / "app" / "refunds.py")
    body = big.read_text()
    big.write_text(body + "\n\ndef pad():\n    return '" + "x" * 5000 + "'\n")
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(
        [_needs_context("app/refunds.py:symbol pad"), "ACCEPT"]
    )
    executor = build_executor(tmp_repo=repo, gatekeeper=gatekeeper)
    rec = executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    content = rec.implementation_evidence["context_slice"]["content"]
    assert "def pad" in content
    assert len(content) <= g3x.CONTEXT_SLICE_MAX_CHARS  # 4000, not G1's 8000
    assert rec.implementation_evidence["context_slice"]["truncated"] is True


def test_g3_needs_context_second_slice_rejects(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    gatekeeper = ScriptedGatekeeper(
        [_needs_context("app/refunds.py:full"), _needs_context("tests/test_refund_window.py:full")]
    )
    executor = build_executor(tmp_repo=repo, gatekeeper=gatekeeper)

    with pytest.raises(GatekeeperContextUnavailable):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))

    assert status_of(checkpoint) == GateStatus.REJECTED
    assert len(gatekeeper.seen) == 2  # exactly one slice re-review
    terminal = executor.emitted_records[-1]
    assert terminal.supervisor_decision == "NEEDS_CONTEXT"
    ctx = [c for c in terminal.deterministic_checks if c["name"] == "context_request"]
    assert ctx and ctx[0]["ok"] is False


# ---------------------------------------------------------------------------
# 10. reviewer guard + Pro prompt bounded / findings sorted
# ---------------------------------------------------------------------------


def test_g3_reviewer_mutation_rejects(tmp_path):
    repo = make_repo(tmp_path)
    checkpoint = make_checkpoint()
    executor = build_executor(
        tmp_repo=repo,
        reviewer_mutates=True,
        gatekeeper=ScriptedGatekeeper(["ACCEPT"]),
    )
    with pytest.raises(ReviewerMutatedRepo):
        executor.execute_checkpoint("plan-1", checkpoint, GateEngine(checkpoint))
    assert status_of(checkpoint) == GateStatus.REJECTED
    guard = [c for c in executor.emitted_records[-1].deterministic_checks
             if c["name"] == "reviewer_guard"]
    assert guard and guard[0]["ok"] is False


def _g3_evidence_for_prompt() -> GateExecutionRecord:
    return GateExecutionRecord(
        plan_id="plan-1",
        checkpoint_id="cp-1",
        gate_id="G3_adversarial",
        attempt=2,
        requirements={"required": True, "objective": "refund window behaviour"},
        worker_sessions=[{"role": "adversarial-reviewer", "session_id": "s1", "ok": True}],
        changed_files=["app/refunds.py"],
        deterministic_checks=[
            {"name": "reviewer_guard", "ok": True, "detail": "clean"},
            {"name": "regression", "ok": True, "detail": "exit 0"},
        ],
        green_evidence={"ok": True, "returncode": 0, "stdout_tail": "y" * 200_000},
        cumulative_gate={"ok": True, "returncode": 0, "stderr": "s" * 200_000},
        implementation_evidence={
            "git_stat": " app/refunds.py | 2 ++\n",
            "product_diff": "MARKER_P10_DIFF " + ("+code\n" * 40_000),
            "test_files": ["tests/test_refund_window.py"],
            "changed_symbols": ["RefundPolicy"],
            "truncated": True,
            "review_verdict": "FAIL",
            "review_summary": "s" * 20_000,
            "adversarial_findings": [
                dict(MINOR_FINDING),
                {"severity": "major", "file": "app/m.py", "line": 4,
                 "detail": "race" * 500},
                dict(CRITICAL_FINDING),
            ],
        },
        supervisor_session_id=None,
        supervisor_decision=None,
        repair_lineage=[1],
        timestamps={"started_at": "t0", "finished_at": "t1"},
    )


def test_g3_prompt_bounded_and_findings_sorted():
    captured: dict = {}

    def http(url, headers, payload, timeout):
        captured["payload"] = payload
        return 200, json.dumps(
            {"id": "chatcmpl-g3",
             "choices": [{"message": {"content":
                 '{"decision": "REPAIR", "reason": "open critical",'
                 ' "repair_instructions": "fix it", "context_request": ""}'}}]}
        )

    keeper = MiMoProG3Gatekeeper(
        base_url="https://example.invalid/v1",
        model="helmholtz/alias-mimo-v2.6",
        http=http,
    )
    decision = keeper.decide(_g3_evidence_for_prompt(), [])
    assert decision.decision == "REPAIR"
    assert decision.repair_instructions == "fix it"
    assert decision.session_id == "chatcmpl-g3"

    messages = captured["payload"]["messages"]
    assert "G3 Adversarial gatekeeper" in messages[0]["content"]
    prompt = messages[-1]["content"]
    size = sum(len(str(m.get("content", ""))) for m in messages)
    assert size < 15_000  # Pro timeout needs a small prompt even with fat evidence
    assert "MARKER_P10_DIFF" in prompt          # bounded diff present
    assert "refund window behaviour" in prompt  # objective
    assert "review_verdict" in prompt
    # findings severity-sorted: critical before major before minor
    assert prompt.index("negative days") < prompt.index("race") < prompt.index("naming")
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    # never leaks raw transcripts
    assert "y" * 2000 not in prompt


# ---------------------------------------------------------------------------
# gatekeeper transport fail-closed (shape parity with G1)
# ---------------------------------------------------------------------------


def test_g3_mimo_gatekeeper_malformed_and_needs_context_contract():
    keeper = MiMoProG3Gatekeeper(
        base_url="https://example.invalid/v1", model="m",
        http=lambda *a: (200, json.dumps(
            {"choices": [{"message": {"content": "no json here"}}]})),
    )
    with pytest.raises(GatekeeperUnavailable):
        keeper.decide(_g3_evidence_for_prompt(), [])

    keeper2 = MiMoProG3Gatekeeper(
        base_url="https://example.invalid/v1", model="m",
        http=lambda *a: (200, json.dumps(
            {"choices": [{"message": {"content":
                '{"decision": "NEEDS_CONTEXT", "reason": "r"}'}}]})),
    )
    with pytest.raises(GatekeeperUnavailable):  # NEEDS_CONTEXT without request
        keeper2.decide(_g3_evidence_for_prompt(), [])


# ---------------------------------------------------------------------------
# 11. live factory
# ---------------------------------------------------------------------------


def test_g3_live_factory_builds(tmp_path):
    from app.orchestration.checkpoint_planning import planner

    assert g3x.MAX_FIX_CYCLES == planner.MAX_REVIEW_FIX_CYCLES == 3

    cfg = g3x.default_gatekeeper_config()
    assert {"base_url", "model"} <= set(cfg)
    assert cfg["base_url"] and cfg["model"]  # pure env read, no network

    executor = build_live_g3_executor(
        repo=tmp_path,
        test_command=["pytest", "-q"],
        gatekeeper_config=cfg,
    )
    assert isinstance(executor, G3Executor)
    keeper = executor._resolve_gatekeeper()
    assert isinstance(keeper, MiMoProG3Gatekeeper)
    # real default seams wired (capture order = G1's P4-proven pattern)
    assert executor._session_capturer == executor._default_session_capturer
    assert executor._dispatch_worker == executor._default_dispatch_worker
    assert executor._repo_snapshot == g3x._default_repo_snapshot
    assert g3x.AGENT_FOR_ROLE[REVIEWER] == "agy-adversarial-reviewer"
    assert g3x.AGENT_FOR_ROLE[FIXER] == "agy-fixer"
