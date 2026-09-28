"""
P8 Worker B — context service tests (contract items 11-15).

Covers the bounded, read-only planning context APIs:

 1 agy_project_inspect returns exactly the bounded identity keys
 2 agy_project_inspect never dumps file CONTENT (huge files stay out)
 3 git identity read from files (branch + sha), incl. worktree pointer
 4 primary language, manifests, test hints are bounded + deterministic
 5 design context: query targets the right file; excerpts bounded by
   max_chars with truncated=True
 6 learnings: ## topics, keyword match, missing file = present False
 7 graphify/serena/run-count presence flags; missing repo raises

All fixtures are tiny tmp_path repos (mini ticketing service); no
network, no subprocess, no OpenCode server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.orchestration.checkpoint_planning.context import (
    agy_design_context,
    agy_learnings_read,
    agy_project_inspect,
)


# ---------------------------------------------------------------------------
# fixtures — mini ticketing service repo
# ---------------------------------------------------------------------------

@pytest.fixture()
def ticketing_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ticketing"
    repo.mkdir()

    (repo / "app").mkdir()
    (repo / "app" / "orders.py").write_text("def create_order(): ...\n")
    (repo / "app" / "seats.py").write_text("def hold_seat(): ...\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_orders.py").write_text("def test_create(): pass\n")
    (repo / "pyproject.toml").write_text("[project]\nname='ticketing'\n")
    (repo / "pytest.ini").write_text("[pytest]\n")

    (repo / "design.md").write_text(
        "# Ticketing design\n\n"
        "Checkout flow: hold seat -> reserve -> pay.\n"
        "Refunds route through the payments ledger.\n"
    )
    docs = repo / "docs"
    docs.mkdir()
    (docs / "seat-map.md").write_text("Seat map rendering uses SVG overlay.\n")

    (repo / "learnings.md").write_text(
        "# Learnings\n\n"
        "## payments\n\nLedger writes must be idempotent.\n\n"
        "## testing\n\nFreeze time in refund windows.\n\n"
        "## deploy\n\nRoll back with the previous image tag.\n"
    )

    git = repo / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    refs = git / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "main").write_text("0" * 40 + "\n")

    return repo


# ---------------------------------------------------------------------------
# 1: bounded identity surface
# ---------------------------------------------------------------------------

EXPECTED_KEYS = {
    "name",
    "path",
    "branch",
    "head_sha",
    "primary_language",
    "manifests",
    "test_hints",
    "design_md_present",
    "learnings_md_present",
    "graphify_out_present",
    "serena_project_configured",
    "agy_run_count",
}


def test_inspect_returns_bounded_identity_keys(ticketing_repo):
    info = agy_project_inspect(ticketing_repo)
    assert EXPECTED_KEYS <= set(info)


def test_inspect_identity_values(ticketing_repo):
    info = agy_project_inspect(ticketing_repo)
    assert info["name"] == "ticketing"
    assert info["branch"] == "main"
    assert info["head_sha"] == "0" * 40
    assert info["primary_language"] == "python"
    assert "pyproject.toml" in info["manifests"]
    assert "pytest" in info["test_hints"]
    assert info["design_md_present"] is True
    assert info["learnings_md_present"] is True
    assert info["graphify_out_present"] is False
    assert info["serena_project_configured"] is False


# ---------------------------------------------------------------------------
# 2: NO file dumps
# ---------------------------------------------------------------------------

def test_inspect_never_dumps_file_content(ticketing_repo):
    # A giant file whose unique marker must NOT appear anywhere.
    (ticketing_repo / "app" / "big.py").write_text("NEVER-DUMP-MARKER\n" + "x" * 200_000)

    info = agy_project_inspect(ticketing_repo)
    assert "NEVER-DUMP-MARKER" not in json.dumps(info)
    assert len(json.dumps(info)) < 4000  # identity stays tiny


# ---------------------------------------------------------------------------
# 3: git identity from files, worktree pointer tolerated
# ---------------------------------------------------------------------------

def test_inspect_detached_head_reads_sha(ticketing_repo):
    (ticketing_repo / ".git" / "HEAD").write_text("a" * 40 + "\n")
    info = agy_project_inspect(ticketing_repo)
    assert info["head_sha"] == "a" * 40


def test_inspect_worktree_pointer(tmp_path):
    repo = tmp_path / "wt"
    repo.mkdir()
    common = tmp_path / "repo.git"
    worktree_dir = common / "worktrees" / "wt"
    heads = worktree_dir / "refs" / "heads"
    heads.mkdir(parents=True)
    (heads / "feature").write_text("b" * 40 + "\n")
    (worktree_dir / "HEAD").write_text("ref: refs/heads/feature\n")
    (repo / ".git").write_text(f"gitdir: {worktree_dir}\n")

    info = agy_project_inspect(repo)
    assert info["branch"] == "feature"
    assert info["head_sha"] == "b" * 40


def test_inspect_without_git_is_not_an_error(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    info = agy_project_inspect(repo)
    assert info["branch"] is None
    assert info["head_sha"] is None


# ---------------------------------------------------------------------------
# 4: language detection bounded + deterministic; hints capped
# ---------------------------------------------------------------------------

def test_primary_language_counts_by_file(tmp_path):
    repo = tmp_path / "mixed"
    repo.mkdir()
    for i in range(3):
        (repo / f"a{i}.js").write_text("//x\n")
    (repo / "only.py").write_text("# x\n")

    assert agy_project_inspect(repo)["primary_language"] == "javascript"


def test_language_scan_skips_vendor_dirs(tmp_path):
    repo = tmp_path / "vendored"
    repo.mkdir()
    vendor = repo / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    for i in range(10):
        (vendor / f"junk{i}.js").write_text("//x\n")
    (repo / "main.py").write_text("# x\n")

    assert agy_project_inspect(repo)["primary_language"] == "python"


def test_test_hints_from_package_json(tmp_path):
    repo = tmp_path / "node"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    hints = agy_project_inspect(repo)["test_hints"]
    assert "npm test" in hints


def test_run_count_and_graphify_flags(ticketing_repo):
    runs = ticketing_repo / "state" / "runs"
    (runs / "plan-a").mkdir(parents=True)
    (runs / "plan-b").mkdir()
    (ticketing_repo / "graphify-out").mkdir()
    serena = ticketing_repo / ".serena"
    serena.mkdir()
    (serena / "project.yml").write_text("excluded: {}\n")

    info = agy_project_inspect(ticketing_repo)
    assert info["agy_run_count"] == 2
    assert info["graphify_out_present"] is True
    assert info["serena_project_configured"] is True


def test_inspect_missing_repo_raises(tmp_path):
    with pytest.raises(ValueError):
        agy_project_inspect(tmp_path / "does-not-exist")


# ---------------------------------------------------------------------------
# 5: design context — bounded, query-targeted
# ---------------------------------------------------------------------------

def test_design_context_finds_sources(ticketing_repo):
    ctx = agy_design_context(ticketing_repo)
    assert set(ctx) == {"found", "excerpts", "truncated"}
    sources = {e["source"] for e in ctx["excerpts"]}
    assert "design.md" in sources
    assert any(s.startswith("docs/") for s in sources)


def test_design_context_query_targets_excerpt(ticketing_repo):
    ctx = agy_design_context(ticketing_repo, query="refund ledger")
    assert ctx["excerpts"], "expected keyword-matched excerpts"
    top = ctx["excerpts"][0]
    assert top["source"] == "design.md"
    assert "ledger" in top["text"].lower()


def test_design_context_truncation_is_bounded(ticketing_repo):
    (ticketing_repo / "design.md").write_text("checkout " + "flow " * 5000)
    ctx = agy_design_context(ticketing_repo, max_chars=300)
    total = sum(len(e["text"]) for e in ctx["excerpts"])
    assert total <= 300
    assert ctx["truncated"] is True


def test_design_context_empty_repo_is_empty_not_error(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    ctx = agy_design_context(repo)
    assert ctx == {"found": [], "excerpts": [], "truncated": False}


def test_design_context_prototype_paths_listed_not_dumped(tmp_path):
    repo = tmp_path / "proto"
    repo.mkdir()
    (repo / "design.md").write_text("layout notes\n")
    proto = repo / "prototype"
    proto.mkdir()
    (proto / "checkout.html").write_text("<html>PROTOTYPE-MARKER</html>")
    ctx = agy_design_context(repo)
    assert "prototype/checkout.html" in ctx["found"]
    assert "PROTOTYPE-MARKER" not in json.dumps(ctx)


# ---------------------------------------------------------------------------
# 6: learnings — topics, query, bounded, missing tolerant
# ---------------------------------------------------------------------------

def test_learnings_topics_parsed(ticketing_repo):
    out = agy_learnings_read(ticketing_repo)
    assert out["present"] is True
    assert set(out["topics"]) == {"payments", "testing", "deploy"}
    assert "idempotent" in out["topics"]["payments"]


def test_learnings_query_filters_topics(ticketing_repo):
    out = agy_learnings_read(ticketing_repo, query="refund")
    assert set(out["topics"]) == {"testing"}


def test_learnings_bounded_by_max_chars(ticketing_repo):
    (ticketing_repo / "learnings.md").write_text(
        "## big\n\n" + "lesson " * 4000 + "\n## small\n\nshort\n"
    )
    out = agy_learnings_read(ticketing_repo, max_chars=500)
    assert sum(len(v) for v in out["topics"].values()) <= 500
    assert out["truncated"] is True


def test_learnings_missing_file(tmp_path):
    repo = tmp_path / "nolearn"
    repo.mkdir()
    out = agy_learnings_read(repo)
    assert out == {"present": False, "topics": {}, "truncated": False}
