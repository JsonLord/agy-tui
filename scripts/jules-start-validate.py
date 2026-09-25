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


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "state" / "jules-runs"


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
    ).strip()


def parse_github_remote(remote: str) -> tuple[str, str]:
    patterns = (
        r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
        r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
    )

    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            return match.group(1), match.group(2)

    raise RuntimeError(
        f"Unsupported GitHub remote: {remote}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--task",
        default="Validate the current repository.",
    )

    args = parser.parse_args()

    mode = get_mode("validate")

    remote = git(
        "config",
        "--get",
        "remote.origin.url",
    )

    owner, repo = parse_github_remote(remote)

    branch = git(
        "branch",
        "--show-current",
    )

    sha = git(
        "rev-parse",
        "HEAD",
    )

    client = JulesClient()

    source = client.find_source(
        owner,
        repo,
    )

    source_name = source["name"]

    prompt = f"""
Act as an independent validation agent.

Task:
{args.task}

Repository:
{owner}/{repo}

Branch:
{branch}

Expected starting commit:
{sha}

VALIDATE ONLY.

Run the repository-defined build, lint,
type-check, unit-test, and integration-test
commands that are appropriate.

Restrictions:
- Do not modify files.
- Do not modify tests.
- Do not commit.
- Do not push.
- Do not create a PR.
- Do not deploy.
- Do not weaken validation.
- Ask a question rather than guessing if
  necessary information is unavailable.

Finish with exactly one verdict:
PASS
FAIL
BLOCKED
""".strip()

    session = client.create_session(
        source=source_name,
        branch=branch,
        prompt=prompt,
        title=f"Antigravity validate {repo}:{branch}",
        require_plan_approval=(
            mode.require_plan_approval
        ),
        auto_create_pr=False,
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
        "starting_sha": sha,
        "task": args.task,
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
        "initial_sha": sha,
        "current_sha": sha,
    }

    (root / "spec.json").write_text(
        json.dumps(spec, indent=2) + "\n"
    )

    (root / "state.json").write_text(
        json.dumps(state, indent=2) + "\n"
    )

    (root / "session-created.json").write_text(
        json.dumps(session, indent=2) + "\n"
    )

    print(
        json.dumps(
            {
                "run_id": run_id,
                "session_id": session_id,
                "jules_state": session.get(
                    "state"
                ),
                "repository": f"{owner}/{repo}",
                "branch": branch,
                "starting_sha": sha,
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
