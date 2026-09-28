"""
P8 Worker B — analyst dispatch tests (contract items 16-21) and the
planner-permission surface tests (contract items 23-24).

 16 codebase_analyze delegates to the injected analyst callable
 17 default dispatch prompt orders Graphify first, then Serena
 18 evidence parsed from the analyst JSON; bounded (caps enforced)
 19 unparseable / nonzero analyst -> AnalystUnavailable, never a
    fabricated empty success
 20 zero-mutation guarantee: git status+HEAD pre/post compared;
    a mutating analyst -> RepoMutationDetected
 21 codebase_analyze_many runs requests independently, order-preserving,
    and enforces max_analyses BEFORE dispatching anything
 23 the planning surface exposes no mutation tool: every mcp_tools
    entry point is read-only or state_root-scoped (static + runtime)
 24 analyst agent definition denies edit/write/patch by configuration

Fakes only: no network, no subprocess in the default-dispatch path,
no OpenCode server. The git snapshot probe is injected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning import mcp_tools
from app.orchestration.checkpoint_planning.codebase_analysis import (
    ANALYST_AGENT,
    AnalystUnavailable,
    RepoMutationDetected,
    build_analyst_prompt,
    codebase_analyze,
    codebase_analyze_many,
    evidence_from_payload,
)
try:  # Worker A's schemas at integration; B's dataclass fallback mid-flight
    from app.orchestration.checkpoint_planning.schemas import (
        AnalysisEvidence,  # noqa: F401
        AnalysisRequest,
    )
except ImportError:  # pragma: no cover
    from app.orchestration.checkpoint_planning.codebase_analysis import (
        AnalysisEvidence,
        AnalysisRequest,
    )


def _snapshot_via_mtime(repo: Path):
    """Injected git_snapshot: content-addressed listing of repo files."""
    import hashlib

    def snap(path: Path) -> str:
        entries = []
        for f in sorted(path.rglob("*")):
            if f.is_file() and ".git" not in f.parts:
                digest = hashlib.sha256(f.read_bytes()).hexdigest()
                entries.append(f"{f.relative_to(path)}:{digest}")
        return "\n".join(entries)

    return snap


def make_request(**kw) -> AnalysisRequest:
    base = dict(
        objective="add refund window to checkout",
        questions=["where are refunds handled?"],
        focus=["payments"],
    )
    base.update(kw)
    return AnalysisRequest(**base)


EVIDENCE_JSON = {
    "summary": "Refunds route through payments/ledger.py.",
    "relevant_symbols": ["ledger.refund", "checkout.finalize"],
    "dependency_paths": ["app/checkout.py -> app/payments/ledger.py"],
    "existing_tests": ["tests/test_refunds.py"],
    "likely_files": ["app/payments/ledger.py", "app/checkout.py"],
    "constraints": ["ledger writes are idempotent"],
    "open_questions": ["partial refunds out of scope?"],
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "ticketing"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "ledger.py").write_text("def refund(): ...\n")
    git = root / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def fake_evidence(**overrides):
    payload = dict(EVIDENCE_JSON)
    payload.update(overrides)
    return evidence_from_payload(payload)


# ---------------------------------------------------------------------------
# 16: injectable analyst
# ---------------------------------------------------------------------------

def test_analyze_uses_injected_analyst(repo):
    seen = {}

    def analyst(request, path):
        seen["objective"] = request.objective
        seen["path"] = path
        return fake_evidence()

    evidence = codebase_analyze(repo, make_request(), analyst=analyst)

    assert seen["objective"] == "add refund window to checkout"
    assert Path(seen["path"]) == repo.resolve()
    assert evidence.summary.startswith("Refunds route")
    assert evidence.likely_files == EVIDENCE_JSON["likely_files"]
    assert evidence.source in ("agy-planning-analyst",)


def test_analyze_rejects_missing_repo(tmp_path):
    with pytest.raises(ValueError):
        codebase_analyze(
            tmp_path / "nope", make_request(), analyst=lambda r, p: fake_evidence()
        )


# ---------------------------------------------------------------------------
# 17: prompt contract — Graphify first, then Serena, JSON-only answer
# ---------------------------------------------------------------------------

def test_prompt_orders_graphify_before_serena():
    prompt = build_analyst_prompt(make_request())
    assert prompt.lower().index("graphify") < prompt.lower().index("serena")
    assert ANALYST_AGENT == "agy-planning-analyst"


def test_prompt_carries_request_and_evidence_contract():
    prompt = build_analyst_prompt(make_request())
    assert "add refund window to checkout" in prompt
    assert "where are refunds handled?" in prompt
    assert "payments" in prompt
    for key in (
        "summary",
        "relevant_symbols",
        "dependency_paths",
        "existing_tests",
        "likely_files",
        "constraints",
        "open_questions",
    ):
        assert key in prompt


def test_prompt_forbids_mutation_commands():
    prompt = build_analyst_prompt(make_request())
    lowered = prompt.lower()
    assert "read-only" in lowered
    assert "never edit" in lowered or "never run" in lowered
    # graphify write subcommands explicitly banned
    assert "never run graphify update" in lowered


# ---------------------------------------------------------------------------
# 18: evidence coercion is bounded
# ---------------------------------------------------------------------------

def test_evidence_from_payload_caps_lists():
    payload = dict(EVIDENCE_JSON)
    payload["likely_files"] = [f"file{i}.py" for i in range(500)]
    payload["summary"] = "x" * 10_000
    evidence = evidence_from_payload(payload)
    assert len(evidence.likely_files) <= 25
    assert len(evidence.summary) <= 2000


def test_evidence_from_payload_caps_item_length_and_junk():
    payload = dict(EVIDENCE_JSON)
    payload["relevant_symbols"] = ["s" * 9999, "", None, 42]
    evidence = evidence_from_payload(payload)
    assert all(len(s) <= 500 for s in evidence.relevant_symbols)
    assert evidence.relevant_symbols  # junk entries dropped, valid kept


def test_evidence_source_defaults_to_analyst():
    payload = {k: v for k, v in EVIDENCE_JSON.items()}
    evidence = evidence_from_payload(payload)
    assert evidence.source == "agy-planning-analyst"


# ---------------------------------------------------------------------------
# 19: failure modes — never a fabricated success
# ---------------------------------------------------------------------------

def test_default_analyst_nonzero_result_raises(repo):
    def dispatch(_repo, _prompt, **_kw):
        return {"ok": False, "stdout": "", "stderr": "supervisor crashed"}

    with pytest.raises(AnalystUnavailable):
        codebase_analyze(
            repo,
            make_request(),
            git_snapshot=_snapshot_via_mtime(repo),
            run_dispatch=dispatch,
        )


def test_default_analyst_unparseable_stdout_raises(repo):
    def dispatch(_repo, _prompt, **_kw):
        return {"ok": True, "stdout": "I found some files, hope that helps!", "stderr": ""}

    with pytest.raises(AnalystUnavailable):
        codebase_analyze(
            repo,
            make_request(),
            git_snapshot=_snapshot_via_mtime(repo),
            run_dispatch=dispatch,
        )


def test_default_analyst_parses_fenced_json_evidence(repo):
    stdout = (
        "Session ID: ses_abc123\n"
        "```json\n" + json.dumps(EVIDENCE_JSON) + "\n```\n"
    )

    def dispatch(_repo, _prompt, **_kw):
        return {"ok": True, "stdout": stdout, "stderr": ""}

    evidence = codebase_analyze(
        repo,
        make_request(),
        git_snapshot=_snapshot_via_mtime(repo),
        run_dispatch=dispatch,
        session_capturer=lambda out, path: ("ses_abc123", "stdout"),
    )
    assert evidence.summary == EVIDENCE_JSON["summary"]
    assert evidence.existing_tests == ["tests/test_refunds.py"]


def test_injected_analyst_none_evidence_raises(repo):
    with pytest.raises(AnalystUnavailable):
        codebase_analyze(repo, make_request(), analyst=lambda r, p: None)


# ---------------------------------------------------------------------------
# 20: zero-mutation guarantee via git pre/post snapshot
# ---------------------------------------------------------------------------

def test_mutation_during_analysis_is_detected(repo):
    def mutating_analyst(request, path):
        (path / "app" / "ledger.py").write_text("def refund(): raise SystemExit\n")
        return fake_evidence()

    with pytest.raises(RepoMutationDetected):
        codebase_analyze(
            repo,
            make_request(),
            analyst=mutating_analyst,
            git_snapshot=_snapshot_via_mtime(repo),
        )


def test_head_move_during_analysis_is_detected(repo):
    """A git_snapshot that reports HEAD moved -> mutation, regardless of files."""
    calls = {"n": 0}

    def head_moving_snapshot(path):
        calls["n"] += 1
        return f"HEAD=sha{calls['n']}"

    with pytest.raises(RepoMutationDetected):
        codebase_analyze(
            repo,
            make_request(),
            analyst=lambda r, p: fake_evidence(),
            git_snapshot=head_moving_snapshot,
        )


def test_clean_analysis_passes_snapshot_check(repo):
    evidence = codebase_analyze(
        repo,
        make_request(),
        analyst=lambda r, p: fake_evidence(),
        git_snapshot=_snapshot_via_mtime(repo),
    )
    assert evidence.likely_files


# ---------------------------------------------------------------------------
# 21: analyze_many — independence + max_analyses enforced up front
# ---------------------------------------------------------------------------

def test_analyze_many_independent_and_ordered(repo):
    calls = []

    def analyst(request, path):
        calls.append(request.objective)
        return fake_evidence(summary=f"evidence for {request.objective}")

    requests = [
        make_request(objective="first"),
        make_request(objective="second"),
        make_request(objective="third"),
    ]
    results = codebase_analyze_many(
        repo,
        requests,
        analyst=analyst,
        git_snapshot=_snapshot_via_mtime(repo),
    )

    assert calls == ["first", "second", "third"]  # one dispatch each, in order
    assert [r.summary for r in results] == [
        "evidence for first",
        "evidence for second",
        "evidence for third",
    ]


def test_analyze_many_enforces_max_before_dispatching(repo):
    dispatched = []

    def analyst(request, path):
        dispatched.append(request.objective)
        return fake_evidence()

    requests = [make_request(objective=f"r{i}") for i in range(6)]
    with pytest.raises(ValueError):
        codebase_analyze_many(
            repo,
            requests,
            analyst=analyst,
            max_analyses=5,
            git_snapshot=_snapshot_via_mtime(repo),
        )
    assert dispatched == []  # nothing ran — the cap is checked up front


def test_analyze_many_default_cap_is_five(repo):
    requests = [make_request(objective=f"r{i}") for i in range(6)]
    with pytest.raises(ValueError):
        codebase_analyze_many(
            repo,
            requests,
            analyst=lambda r, p: fake_evidence(),
            git_snapshot=_snapshot_via_mtime(repo),
        )


# ---------------------------------------------------------------------------
# 23: the planning surface exposes no mutation tool
# ---------------------------------------------------------------------------

def test_mcp_tools_expose_exactly_the_eight_planning_tools():
    public = {
        name
        for name in dir(mcp_tools)
        if name.endswith("_tool") and callable(getattr(mcp_tools, name))
    }
    assert public == {
        "agy_project_inspect_tool",
        "agy_design_context_tool",
        "agy_learnings_read_tool",
        "agy_gate_templates_tool",
        "agy_codebase_analyze_tool",
        "agy_checkpoint_plan_validate_tool",
        "agy_checkpoint_plan_preview_tool",
        "agy_checkpoint_plan_submit_tool",
    }


def test_mcp_tools_module_imports_no_write_primitives():
    """Static proof: no os.remove/shutil/open('w')/subprocess writes in B's
    read-only tools module (submit writes only via Worker A's submission)."""
    source = Path(mcp_tools.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\bshutil\b", source)
    assert not re.search(r"open\([^)]*[\"']w", source)
    # only AnalystUnavailable/RepoMutationDetected plumbing may catch broad
    # exceptions; the module never shells out itself
    assert "subprocess.run" not in source


def test_inspect_tool_validates_repo_argument(tmp_path):
    with pytest.raises(ValueError):
        mcp_tools.agy_project_inspect_tool(str(tmp_path / "ghost"))
    with pytest.raises(ValueError):
        mcp_tools.agy_codebase_analyze_tool(str(tmp_path), "   ")


def test_analyze_tool_builds_request_from_plain_args(tmp_path):
    """The MCP tool accepts plain JSON args and returns a dict evidence."""
    repo = tmp_path / "r"
    repo.mkdir()

    def analyst(request, path):
        assert request.objective == "objective text"
        assert request.questions == ["q1"]
        assert request.focus == ["f1"]
        return fake_evidence()

    # route the tool through a fake analyst by patching the module seam
    from app.orchestration.checkpoint_planning import codebase_analysis

    original = codebase_analysis.codebase_analyze
    try:
        codebase_analysis.codebase_analyze = lambda root, req, **kw: analyst(req, root)
        mcp_tools.codebase_analyze = codebase_analysis.codebase_analyze
        out = mcp_tools.agy_codebase_analyze_tool(
            str(repo), "objective text", ["q1"], ["f1"]
        )
    finally:
        codebase_analysis.codebase_analyze = original
        mcp_tools.codebase_analyze = original
    assert out["summary"].startswith("Refunds route")
    assert "likely_files" in out


# ---------------------------------------------------------------------------
# 24: the analyst agent definition denies mutation by configuration
# ---------------------------------------------------------------------------

AGENT_DOC = Path(__file__).resolve().parents[1] / "docs" / "agy-planning-analyst.agent.md"


def _agent_block() -> dict:
    text = AGENT_DOC.read_text(encoding="utf-8")
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert match, "agent doc must contain the JSON agent block"
    # the block starts at the key name; wrap for json.loads
    return json.loads("{" + match.group(1) + "}")[ANALYST_AGENT]


def test_agent_doc_block_is_read_only_by_configuration():
    block = _agent_block()
    assert block["model"] == "helmholtz/alias-mimo-v2.6-flash"
    for permission in ("edit", "write", "patch", "webfetch", "websearch"):
        assert block["permission"][permission] == "deny"
    for tool in ("edit", "write", "patch"):
        assert block["tools"][tool] is False
    assert block["permission"]["*"] == "deny"
    assert block["tools"]["*"] is False


def test_agent_doc_grants_the_serena_read_tools():
    block = _agent_block()
    for tool in (
        "serena_get_symbols_overview",
        "serena_find_symbol",
        "serena_find_referencing_symbols",
    ):
        assert block["tools"][tool] is True
        assert block["permission"][tool] == "allow"
