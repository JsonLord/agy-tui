from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.integrations.jules_api import (
    JulesAPIError,
    JulesClient,
)

from app.integrations.external_stack import (
    run_supervised,
    supervisor_status,
)


READY = "READY_FOR_JULES"

REQUIRED_IGNORE_MARKERS = (
    ".venv/",
    ".env",
    "state/",
    "logs/",
    "__pycache__/",
)


@dataclass
class JulesReadiness:
    state: str
    repo: str

    git_repo: bool = False
    gitignore_safe: bool = False
    has_commit: bool = False
    clean_worktree: bool = False

    branch: str | None = None
    local_sha: str | None = None

    remote_url: str | None = None
    github_repository: str | None = None
    remote_sha: str | None = None

    jules_source: str | None = None

    blockers: list[str] = field(
        default_factory=list
    )

    facts: dict[str, Any] = field(
        default_factory=dict
    )

    @property
    def ready(self) -> bool:
        return self.state == READY

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:

    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _git(
    repo: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:

    return _run(
        ["git", *args],
        cwd=repo,
    )


def _git_output(
    repo: Path,
    *args: str,
) -> str | None:

    result = _git(
        repo,
        *args,
    )

    if result.returncode != 0:
        return None

    value = result.stdout.strip()

    return value or None


def _parse_github_remote(
    remote: str,
) -> str | None:

    patterns = (
        r"^https://github\.com/"
        r"([^/]+)/([^/]+?)(?:\.git)?$",

        r"^git@github\.com:"
        r"([^/]+)/([^/]+?)(?:\.git)?$",

        r"^ssh://git@github\.com/"
        r"([^/]+)/([^/]+?)(?:\.git)?$",
    )

    for pattern in patterns:
        match = re.match(
            pattern,
            remote,
        )

        if match:
            return (
                f"{match.group(1)}/"
                f"{match.group(2)}"
            )

    return None


def _gitignore_safe(
    repo: Path,
) -> tuple[bool, list[str]]:

    path = repo / ".gitignore"

    if not path.exists():
        return (
            False,
            list(REQUIRED_IGNORE_MARKERS),
        )

    text = path.read_text(
        errors="replace"
    )

    missing = [
        marker
        for marker
        in REQUIRED_IGNORE_MARKERS
        if marker not in text
    ]

    return (
        not missing,
        missing,
    )


def _remote_sha(
    repo: Path,
    branch: str,
) -> str | None:

    git_bin = (
        shutil.which("ag-git")
        or shutil.which("git")
    )

    if not git_bin:
        return None

    result = _run(
        [
            git_bin,
            "ls-remote",
            "origin",
            f"refs/heads/{branch}",
        ],
        cwd=repo,
    )

    if result.returncode != 0:
        return None

    line = result.stdout.strip()

    if not line:
        return None

    return line.split()[0]


def inspect_jules_readiness(
    repo: str | Path,
    *,
    client: JulesClient | None = None,
) -> JulesReadiness:

    root = Path(repo).expanduser().resolve()

    result = JulesReadiness(
        state="UNKNOWN",
        repo=str(root),
    )

    if not root.is_dir():
        result.state = "PREPARATION_FAILED"
        result.blockers.append(
            "Repository directory does not exist."
        )
        return result

    git_check = _git(
        root,
        "rev-parse",
        "--is-inside-work-tree",
    )

    if (
        git_check.returncode != 0
        or git_check.stdout.strip() != "true"
    ):
        result.state = "NOT_GIT_REPO"
        result.blockers.append(
            "Initialize Git and create a safe "
            ".gitignore before staging files."
        )
        return result

    result.git_repo = True

    ignore_ok, missing = _gitignore_safe(
        root
    )

    result.gitignore_safe = ignore_ok

    if not ignore_ok:
        result.state = "NEEDS_GITIGNORE"
        result.blockers.append(
            "Missing required ignore markers: "
            + ", ".join(missing)
        )
        result.facts[
            "missing_ignore_markers"
        ] = missing
        return result

    sha = _git_output(
        root,
        "rev-parse",
        "HEAD",
    )

    if not sha:
        result.state = "NEEDS_COMMIT"
        result.blockers.append(
            "Repository has no commit yet."
        )
        return result

    result.has_commit = True
    result.local_sha = sha

    status = _git_output(
        root,
        "status",
        "--porcelain",
    )

    result.clean_worktree = not bool(
        status
    )

    if status:
        result.state = "NEEDS_COMMIT"
        result.blockers.append(
            "Working tree contains uncommitted "
            "source changes."
        )
        result.facts[
            "git_status"
        ] = status.splitlines()
        return result

    branch = _git_output(
        root,
        "branch",
        "--show-current",
    )

    if not branch:
        result.state = "PREPARATION_FAILED"
        result.blockers.append(
            "Repository is not on a named branch."
        )
        return result

    result.branch = branch

    remote = _git_output(
        root,
        "config",
        "--get",
        "remote.origin.url",
    )

    if not remote:
        result.state = "NEEDS_REMOTE"
        result.blockers.append(
            "No origin remote is configured."
        )
        return result

    result.remote_url = remote

    slug = _parse_github_remote(
        remote
    )

    if not slug:
        result.state = "NEEDS_REMOTE"
        result.blockers.append(
            "origin is not a recognized GitHub URL."
        )
        return result

    result.github_repository = slug

    remote_sha = _remote_sha(
        root,
        branch,
    )

    result.remote_sha = remote_sha

    if not remote_sha:
        result.state = "NEEDS_PUSH"
        result.blockers.append(
            "Current branch does not exist on "
            "GitHub or cannot be read."
        )
        return result

    if remote_sha != sha:
        result.state = "REMOTE_SHA_MISMATCH"
        result.blockers.append(
            "Local HEAD differs from the GitHub "
            "branch HEAD."
        )
        return result

    if client is None:
        try:
            client = JulesClient()
        except JulesAPIError:
            result.state = "AWAITING_JULES_AUTH"
            result.blockers.append(
                "Jules API credentials are not "
                "available to the controller."
            )
            return result

    owner, repo_name = slug.split(
        "/",
        1,
    )

    try:
        source = client.find_source(
            owner,
            repo_name,
        )
    except JulesAPIError:
        result.state = "AWAITING_JULES_SOURCE"
        result.blockers.append(
            "GitHub repository is ready, but "
            "Jules does not expose it as a Source."
        )
        return result

    result.jules_source = source.get(
        "name"
    )

    result.state = READY

    return result


def build_preparation_brief(
    readiness: JulesReadiness,
) -> str:

    return f"""
Prepare this repository for a Jules handoff.

Current deterministic state:
{readiness.state}

Repository:
{readiness.repo}

Required final invariant:

1. This directory is a Git repository.
2. A safe .gitignore excludes at minimum:
   - .venv/
   - .env and local secret files
   - state/
   - logs/
   - __pycache__/
3. Intended source files are committed.
4. The GitHub repository is private.
5. origin uses an HTTPS github.com URL.
6. The current named branch is pushed.
7. Local HEAD equals the remote branch HEAD.
8. Never print, copy, stage, commit, or expose
   credentials or local environment values.

GitHub automation commands available:
- ag-gh
- ag-git

When no GitHub repository exists, create a private
repository with ag-gh.

Use ag-git rather than ordinary authenticated git
operations for remote push/fetch.

Do not invoke Jules yourself.

Stop when the repository is safely pushed to GitHub.
The deterministic controller will verify readiness
after you finish.
""".strip()


def _github_automation_check(
    repo: Path,
) -> tuple[bool, str]:
    """
    Verify the dedicated Antigravity GitHub wrappers
    exist and the PAT can authenticate.

    This is deterministic and consumes no model tokens.
    """

    ag_gh = shutil.which("ag-gh")
    ag_git = shutil.which("ag-git")

    if not ag_gh:
        return (
            False,
            "ag-gh is not available on PATH.",
        )

    if not ag_git:
        return (
            False,
            "ag-git is not available on PATH.",
        )

    result = _run(
        [
            ag_gh,
            "api",
            "user",
            "--jq",
            ".login",
        ],
        cwd=repo,
    )

    if result.returncode != 0:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "unknown authentication error"
        )

        return (
            False,
            "Antigravity GitHub authentication "
            f"failed: {detail}",
        )

    login = result.stdout.strip()

    if not login:
        return (
            False,
            "Antigravity GitHub authentication "
            "returned no login.",
        )

    return (
        True,
        login,
    )


def _write_preparation_artifacts(
    output_dir: Path | None,
    *,
    before: JulesReadiness,
    after: JulesReadiness | None = None,
    brief: str | None = None,
    agent_output: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:

    if output_dir is None:
        return

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    (
        output_dir
        / "jules-readiness-before.json"
    ).write_text(
        json.dumps(
            before.to_dict(),
            indent=2,
        )
        + "\n"
    )

    if brief is not None:
        (
            output_dir
            / "jules-preparation-brief.txt"
        ).write_text(
            brief.rstrip()
            + "\n"
        )

    if agent_output is not None:
        (
            output_dir
            / "jules-preparation-agent.txt"
        ).write_text(
            agent_output.rstrip()
            + "\n"
        )

    if after is not None:
        (
            output_dir
            / "jules-readiness-after.json"
        ).write_text(
            json.dumps(
                after.to_dict(),
                indent=2,
            )
            + "\n"
        )

    if result is not None:
        (
            output_dir
            / "jules-preparation-result.json"
        ).write_text(
            json.dumps(
                result,
                indent=2,
            )
            + "\n"
        )


def prepare_for_jules(
    repo: str | Path,
    *,
    client: JulesClient | None = None,
    output_dir: str | Path | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    """
    Prepare one local project for a Jules handoff.

    The coding agent may mutate Git/GitHub state.
    The deterministic readiness inspector remains
    authoritative.

    This function does NOT create a Jules session.
    """

    root = Path(
        repo
    ).expanduser().resolve()

    artifact_dir = (
        Path(output_dir)
        .expanduser()
        .resolve()
        if output_dir is not None
        else None
    )

    before = inspect_jules_readiness(
        root,
        client=client,
    )

    _write_preparation_artifacts(
        artifact_dir,
        before=before,
    )

    if before.ready:
        result = {
            "status": "READY_FOR_JULES",
            "agent_invoked": False,
            "before": before.to_dict(),
            "after": before.to_dict(),
        }

        _write_preparation_artifacts(
            artifact_dir,
            before=before,
            after=before,
            result=result,
        )

        return result

    # These are not repository preparation tasks.
    # Running Gemma cannot repair Jules account state.
    if before.state in {
        "AWAITING_JULES_SOURCE",
        "AWAITING_JULES_AUTH",
    }:
        result = {
            "status": before.state,
            "agent_invoked": False,
            "before": before.to_dict(),
            "after": before.to_dict(),
        }

        _write_preparation_artifacts(
            artifact_dir,
            before=before,
            after=before,
            result=result,
        )

        return result

    github_ok, github_detail = (
        _github_automation_check(
            root
        )
    )

    if not github_ok:
        result = {
            "status": "AWAITING_GITHUB_AUTH",
            "agent_invoked": False,
            "detail": github_detail,
            "before": before.to_dict(),
        }

        _write_preparation_artifacts(
            artifact_dir,
            before=before,
            result=result,
        )

        return result

    brief = build_preparation_brief(
        before
    )

    brief += f"""

ANTIGRAVITY GITHUB IDENTITY:

The dedicated GitHub automation identity has already
been authenticated successfully as:

{github_detail}

Use ONLY these wrappers for GitHub remote operations:

  /home/leon/.local/bin/ag-gh
  /home/leon/.local/bin/ag-git

Do not use or modify Leon's normal GitHub credentials.

If this directory is not yet a Git repository:
- initialize it on branch main;
- inspect the project before staging;
- create or harden .gitignore first.

Before every commit:
- inspect git status;
- verify that .env, .env.*, .venv, state, logs,
  caches, credentials, keys, tokens, and generated
  runtime artifacts are not staged.

If no GitHub repository exists:
- create it as PRIVATE using ag-gh;
- use the current directory name as the default
  repository name unless repository metadata clearly
  indicates another canonical name;
- configure origin as an HTTPS github.com URL.

Commit intended source/configuration only.

Push the current named branch using ag-git.

Do NOT:
- invoke Jules;
- expose secret values;
- print the contents of credential files;
- commit local runtime state;
- make the repository public.

When Git/GitHub preparation is complete, stop.
""".strip()

    _write_preparation_artifacts(
        artifact_dir,
        before=before,
        brief=brief,
    )

    try:
        supervised = run_supervised(
            root,
            brief,
            # The preparation brief is already complete.
            # Disable the separate Nemotron preflight
            # planner; keep Nemotron recovery active.
            cloud_led=True,
            repeat_limit=3,
            stall_seconds=120,
            max_restarts=1,
            timeout=timeout,
        )

        agent_output = supervised.output

    except Exception as exc:
        result = {
            "status": "PREPARATION_ERROR",
            "agent_invoked": True,
            "error": str(exc),
            "before": before.to_dict(),
        }

        _write_preparation_artifacts(
            artifact_dir,
            before=before,
            result=result,
        )

        return result

    # Always re-inspect, even if the agent/supervisor
    # returned TIMEOUT or another nonzero status.
    #
    # The deterministic state is authoritative.
    after = inspect_jules_readiness(
        root,
        client=client,
    )

    result = {
        "status": after.state,
        "agent_invoked": True,
        "supervisor": {
            "returncode": (
                supervised.returncode
            ),
            "status": supervisor_status(
                supervised.returncode
            ),
        },
        "before": before.to_dict(),
        "after": after.to_dict(),
    }

    _write_preparation_artifacts(
        artifact_dir,
        before=before,
        after=after,
        brief=brief,
        agent_output=agent_output,
        result=result,
    )

    return result

