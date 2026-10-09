#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.jules_api import JulesClient
from app.orchestration.jules_modes import get_mode


RUNS = ROOT / "state" / "jules-runs"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
    ).strip()


def ag_git(*args: str) -> str:
    return subprocess.check_output(
        ["ag-git", *args],
        cwd=ROOT,
        text=True,
    ).strip()


def parse_github_remote(
    remote: str,
) -> tuple[str, str]:

    patterns = (
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
    )

    for pattern in patterns:
        match = re.match(pattern, remote)

        if match:
            return (
                match.group(1),
                match.group(2),
            )

    raise RuntimeError(
        f"Unsupported GitHub remote: {remote}"
    )


def remote_branch_sha(
    branch: str,
) -> str:

    result = ag_git(
        "ls-remote",
        "--heads",
        "origin",
        branch,
    )

    if not result:
        raise RuntimeError(
            f"Remote branch does not exist: {branch}"
        )

    return result.split()[0]


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--branch",
        required=True,
    )

    parser.add_argument(
        "--parent-run",
        default=None,
    )

    args = parser.parse_args()

    mode = get_mode(
        "validate_fix"
    )

    remote = git(
        "config",
        "--get",
        "remote.origin.url",
    )

    owner, repo = parse_github_remote(
        remote
    )

    branch = args.branch

    local_sha = git(
        "rev-parse",
        branch,
    )

    remote_sha = remote_branch_sha(
        branch
    )

    if local_sha != remote_sha:
        raise RuntimeError(
            "Local and remote repair branch "
            "do not reference the same SHA."
        )

    client = JulesClient()

    source = client.find_source(
        owner,
        repo,
    )

    source_name = source["name"]

    prompt = f"""
Act as a narrowly scoped repair and validation agent.

Repository:
{owner}/{repo}

Repair branch:
{branch}

Exact starting SHA:
{local_sha}

A previous independent validation run found two
concrete problems.

PROBLEM 1 — PORTABILITY

`test_extractor.py` hardcodes:

    <host_path>/ag-harness-test

This fails in isolated environments.

Repair this so the test is portable and self-contained.
Prefer a temporary directory or another deterministic
sandbox-safe approach. Preserve the intended extractor
coverage.

PROBLEM 2 — GOOGLE ANTIGRAVITY DEPENDENCY

Core modules import `google.antigravity`, but the
validation environment cannot import it.

Investigate the repository's dependency metadata and
architecture before changing anything.

Rules:

- Do NOT invent a PyPI package name.
- Do NOT guess how `google.antigravity` is installed.
- If repository evidence identifies a real dependency
  declaration/install mechanism, repair the relevant
  dependency metadata.
- If the dependency is intentionally host-provided,
  isolate that dependency boundary so modules that do
  not actually execute Antigravity agents can still be
  imported/tested where reasonable.
- Any unavailable host dependency must fail with a clear,
  intentional diagnostic rather than an accidental
  ModuleNotFoundError.
- Preserve the existing local Antigravity architecture.

TEST COVERAGE

Add narrowly scoped automated tests for the repairs where
useful.

At minimum verify:

- portable extractor behavior;
- Python compilation;
- relevant imports that should work without contacting
  external services;
- existing deterministic validation behavior.

Do not require live external APIs in unit tests.

CHANGE BOUNDARY

You MAY:
- edit source;
- edit dependency metadata;
- edit/add tests;
- commit the repair on this branch.

You MUST NOT:
- modify unrelated functionality;
- refactor unrelated modules;
- weaken validation;
- expose credentials;
- modify GitHub secrets;
- deploy;
- merge into main.

Run appropriate validation after the repair.

The session is configured to create a PR. Do not merge it.

Finish the final report with exactly one verdict line:

PASS
FAIL
BLOCKED

PASS means the scoped defects were repaired and the
available validation passed.
""".strip()

    session = client.create_session(
        source=source_name,
        branch=branch,
        prompt=prompt,
        title=(
            "Antigravity validate-fix "
            f"{repo}:{branch}"
        ),
        require_plan_approval=(
            mode.require_plan_approval
        ),
        auto_create_pr=True,
    )

    session_name = str(
        session.get("name", "")
    )

    session_id = (
        session.get("id")
        or session_name.rsplit("/", 1)[-1]
    )

    if not session_id:
        raise RuntimeError(
            "Jules returned no session ID"
        )

    run_id = secrets.token_hex(6)

    root = RUNS / run_id

    root.mkdir(
        parents=True,
        exist_ok=False,
    )

    spec = {
        "run_id": run_id,
        "mode": mode.name,
        "repository": f"{owner}/{repo}",
        "source": source_name,
        "branch": branch,
        "starting_sha": local_sha,
        "target_branch": "main",
        "parent_validation_run": (
            args.parent_run
        ),
    }

    state = {
        "run_id": run_id,
        "phase": "JULES_CREATED",
        "jules_session_id": session_id,
        "jules_state": session.get("state"),
        "last_session_update": session.get(
            "updateTime"
        ),
        "last_activity_time": None,
        "seen_activity_ids": [],
        "pending_question": None,
        "initial_sha": local_sha,
        "current_sha": local_sha,
        "verdict": None,
        "verdict_finalized": False,
        "report_activity_id": None,
        "idle_seconds": None,
        "idle_state": "UNKNOWN",
    }

    (root / "spec.json").write_text(
        json.dumps(
            spec,
            indent=2,
        )
        + "\n"
    )

    (root / "state.json").write_text(
        json.dumps(
            state,
            indent=2,
        )
        + "\n"
    )

    (root / "session-created.json").write_text(
        json.dumps(
            session,
            indent=2,
        )
        + "\n"
    )

    print(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": session_id,
                "mode": mode.name,
                "branch": branch,
                "starting_sha": local_sha,
                "auto_create_pr": True,
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
