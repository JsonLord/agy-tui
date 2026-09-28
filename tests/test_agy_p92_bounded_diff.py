"""P9.2 — pure unit tests for ``build_implementation_evidence``.

Real (throwaway) git repos under tmp_path; no network, no gatekeeper.
Deterministic completion contract: bounded product diff, test files listed
but never diffed, tooling artifacts excluded, untracked product files via
``git diff --no-index``, truncation marker + caps.
"""

from __future__ import annotations

import subprocess

import pytest

from app.orchestration.checkpoint_planning.g1_executor import (
    _DIFF_TRUNCATION_MARKER,
    build_implementation_evidence,
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture()
def repo(tmp_path):
    """A committed base repo: app/refunds.py + tests/test_refund_window.py."""
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "refunds.py").write_text(
        "def refund_window_days():\n"
        "    return 30\n"
        "\n"
        "class RefundPolicy:\n"
        "    def check(self, days):\n"
        "        return days <= refund_window_days()\n"
    )
    (root / "tests" / "test_refund_window.py").write_text(
        "def test_window():\n"
        "    assert True\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "agy-test@example.invalid")
    _git(root, "config", "user.name", "AGY Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_small_diff_bounded_tests_excluded_symbols_found(repo):
    # tracked product modification
    (repo / "app" / "refunds.py").write_text(
        "def refund_window_days():\n"
        "    return 14\n"
        "\n"
        "class RefundPolicy:\n"
        "    def check(self, days):\n"
        "        return days <= refund_window_days()\n"
        "\n"
        "def grace_period():\n"
        "    return 2\n"
    )
    # test-file change: must be LISTED, never diffed
    (repo / "tests" / "test_refund_window.py").write_text(
        "def test_window():\n"
        "    assert refund_window_days() == 14\n"
    )
    ev = build_implementation_evidence(
        repo,
        ["app/refunds.py", "tests/test_refund_window.py"],
        test_path="tests",
    )
    assert ev["truncated"] is False
    assert "app/refunds.py" in ev["git_stat"]
    assert "def refund_window_days" in ev["product_diff"]
    assert "+    return 14" in ev["product_diff"]
    # the test file never appears in the diff, only in the list
    assert "test_refund_window" not in ev["product_diff"]
    assert ev["test_files"] == ["tests/test_refund_window.py"]
    assert ev["changed_symbols"] == [
        "RefundPolicy", "grace_period", "refund_window_days",
    ]


def test_big_diff_line_cap_truncation_marker(repo):
    body = "\n".join(f"    x{i} = {i}" for i in range(500))
    (repo / "app" / "refunds.py").write_text(
        "def big():\n" + body + "\n"
    )
    ev = build_implementation_evidence(
        repo, ["app/refunds.py"], max_lines=50, test_path="tests",
    )
    assert ev["truncated"] is True
    lines = ev["product_diff"].splitlines()
    assert len(lines) <= 50 + 1  # capped lines (+ optional byte marker line)
    assert lines[0].startswith("diff --git a/app/refunds.py")


def test_byte_cap_emits_truncation_marker_with_remaining_count(repo):
    body = "\n".join(
        f"    filler_{i} = '{'y' * 200}'" for i in range(120)
    )
    (repo / "app" / "refunds.py").write_text("def big():\n" + body + "\n")
    ev = build_implementation_evidence(
        repo, ["app/refunds.py"], max_bytes=2000, max_lines=400,
        test_path="tests",
    )
    assert ev["truncated"] is True
    marker = _DIFF_TRUNCATION_MARKER.format(n=1)
    prefix = marker.split("truncated:")[0]
    assert prefix in ev["product_diff"]
    assert "more bytes]" in ev["product_diff"]
    # the diff body itself respects the byte cap before the marker is appended
    capped = ev["product_diff"].split("…[diff truncated")[0]
    assert len(capped.encode("utf-8")) <= 2000


def test_new_untracked_product_file_appears(repo):
    (repo / "app" / "newmodule.py").write_text(
        "def brand_new():\n"
        "    return 42\n"
    )
    ev = build_implementation_evidence(
        repo, ["app/newmodule.py"], test_path="tests",
    )
    assert "diff --git" in ev["product_diff"]
    assert "+def brand_new():" in ev["product_diff"]
    assert ev["changed_symbols"] == ["brand_new"]
    assert ev["truncated"] is False


def test_tooling_artifacts_excluded(repo):
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "refunds.cpython-313.pyc").write_bytes(b"\x00\x01")
    (repo / ".serena").mkdir()
    (repo / ".serena" / "project.yml").write_text("ignored: true\n")
    (repo / "app" / "refunds.py").write_text(
        "def refund_window_days():\n"
        "    return 7\n"
    )
    ev = build_implementation_evidence(
        repo,
        [
            "app/refunds.py",
            "__pycache__/refunds.cpython-313.pyc",
            ".serena/project.yml",
        ],
        test_path="tests",
    )
    assert "pycache" not in ev["product_diff"]
    assert "serena" not in ev["product_diff"]
    assert "pycache" not in ev["git_stat"]
    # the rewritten refunds.py only defines the function now — the class
    # is gone from the file, and the artifacts contribute no symbols
    assert ev["changed_symbols"] == ["refund_window_days"]
    assert ev["test_files"] == []


def test_empty_changed_files_yields_empty_evidence(repo):
    ev = build_implementation_evidence(repo, [], test_path="tests")
    assert ev == {
        "git_stat": "",
        "product_diff": "",
        "test_files": [],
        "changed_symbols": [],
        "truncated": False,
    }


def test_clean_worktree_no_changes_yields_empty_diff(repo):
    """A changed_files list whose contents match HEAD produces no hunks —
    evidence degrades to empty, never to an error."""
    ev = build_implementation_evidence(
        repo, ["app/refunds.py", "tests/test_refund_window.py"],
        test_path="tests",
    )
    assert ev["product_diff"] == ""
    assert ev["truncated"] is False
    # symbols are still reported from the diffed product file (it was named)
    assert ev["changed_symbols"] == ["RefundPolicy", "refund_window_days"]
    assert ev["test_files"] == ["tests/test_refund_window.py"]
