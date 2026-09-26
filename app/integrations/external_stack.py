from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GRAPHIFY_ENSURE = Path(
    os.getenv(
        "GRAPHIFY_ENSURE_BIN",
        "/home/leon/.local/bin/graphify-ensure",
    )
)

GRAPHIFY = Path(
    os.getenv(
        "GRAPHIFY_BIN",
        "/home/leon/.local/bin/graphify",
    )
)

OPENCODE_SUPERVISE = Path(
    os.getenv(
        "OPENCODE_SUPERVISE_BIN",
        "/home/leon/.local/bin/opencode-supervise",
    )
)

OPENCODE_SERVER = os.getenv(
    "OPENCODE_SERVER",
    "http://127.0.0.1:4096",
)


@dataclass
class ExternalResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        parts = []

        if self.stdout.strip():
            parts.append(self.stdout.strip())

        if self.stderr.strip():
            parts.append(self.stderr.strip())

        return "\n".join(parts)


def _isolated_env() -> dict[str, str]:
    """
    Preserve the user's normal shell environment while preventing
    antigravity-hybrid model configuration from overriding the
    external stack's own configuration files.
    """
    env = os.environ.copy()

    # opencode-supervise owns these through:
    # ~/.config/opencode-supervise.env
    for key in list(env):
        if key.startswith("SUPERVISOR_"):
            env.pop(key, None)

    # graphify-ensure explicitly sources:
    # ~/.config/graphify.env
    for key in (
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    ):
        env.pop(key, None)

    return env


def ensure_graph(repo: str | Path) -> ExternalResult:
    repo = Path(repo).expanduser().resolve()

    if not repo.is_dir():
        raise ValueError(
            f"Repository does not exist: {repo}"
        )

    result = subprocess.run(
        [
            str(GRAPHIFY_ENSURE),
            str(repo),
        ],
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )

    return ExternalResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def graph_query(
    repo: str | Path,
    query: str,
) -> ExternalResult:
    repo = Path(repo).expanduser().resolve()

    graph = ensure_graph(repo)

    if not graph.ok:
        return graph

    result = subprocess.run(
        [
            str(GRAPHIFY),
            "query",
            query,
        ],
        cwd=repo,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    return ExternalResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def supervisor_self_test() -> ExternalResult:
    result = subprocess.run(
        [
            str(OPENCODE_SUPERVISE),
            "--self-test",
        ],
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )

    return ExternalResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def supervisor_smoke_test() -> ExternalResult:
    result = subprocess.run(
        [
            str(OPENCODE_SUPERVISE),
            "--supervisor-smoke-test",
        ],
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )

    return ExternalResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def supervisor_status(returncode: int) -> str:
    """
    Interpret opencode-supervise exit codes.

    0  = coding session completed normally
    76 = bounded recovery exhausted; working tree preserved
    """
    if returncode == 0:
        return "SUCCESS"

    if returncode == 76:
        return "RECOVERY_EXHAUSTED"
    if returncode == 124:
        return "TIMEOUT"

    return "ERROR"


SESSION_ID_PATTERN = re.compile(r"^ses_[A-Za-z0-9]+$")

def validate_session_id(raw: str) -> str:
    """
    Fail before any side effects on a malformed explicit session.
    Mirrors opencode-supervise's ``ses_`` contract.
    """
    candidate = (raw or "").strip()

    if not candidate:
        raise ValueError("session id must not be empty")

    if not SESSION_ID_PATTERN.match(candidate):
        raise ValueError("malformed session id " + repr(raw))

    return candidate

def run_supervised(
    repo: str | Path,
    task: str,
    *,
    agent: str = "gemma-local",
    repeat_limit: int = 3,
    stall_seconds: int = 120,
    max_restarts: int = 1,
    timeout: int = 300,
    cloud_led: bool = True,
    session: str | None = None,
) -> ExternalResult:
    """
    Execute a coding task through the existing isolated
    opencode-supervise stack.

    OpenCode, Graphify, Serena, Headroom, Gemma, and Nemotron
    stay in their own existing environments.

    For cloud-led Antigravity jobs, Nemotron preflight planning is
    disabled because the cloud model already supplied the initial plan.
    Nemotron remains active for recovery/escalation.
    """
    repo = Path(repo).expanduser().resolve()

    if not repo.is_dir():
        raise ValueError(
            f"Repository does not exist: {repo}"
        )

    if not task.strip():
        raise ValueError(
            "Task must not be empty."
        )

    env = _isolated_env()

    if cloud_led:
        env["SUPERVISOR_PREFLIGHT"] = "0"

    navigation_policy = """
LOCAL REPOSITORY NAVIGATION POLICY:

- The repository Graphify graph is prepared before coding.
- For broad, architectural, multi-file, dependency, caller/callee,
  or test-impact questions, use Graphify before broad file reads.
- For exact symbols, definitions, references, or signatures, use Serena.
- Prefer Graphify/Serena evidence over repository-wide file reading.
- Use native file reads only after the relevant area has been narrowed.
- Use native edit/bash tools for implementation and deterministic tests.
- Do not ask the user for repository information that can be discovered
  locally.
""".strip()

    execution_task = (
        task.rstrip()
        + "\n\n"
        + navigation_policy
    )

    command = [
        str(OPENCODE_SUPERVISE),
        "--repo",
        str(repo),
        "--agent",
        agent,
        "--server",
        OPENCODE_SERVER,
        "--repeat-limit",
        str(repeat_limit),
        "--stall-seconds",
        str(stall_seconds),
        "--max-restarts",
        str(max_restarts),
    ]

    if session is not None:
        # Session binding: inserted before the prompt, matching the
        # supervisor's own --session contract. Validated first so a
        # malformed id fails before any process is launched.
        command += ["--session", validate_session_id(session)]

    command.append(execution_task)

    proc = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        stdout, stderr = proc.communicate(
            timeout=timeout,
        )

    except subprocess.TimeoutExpired:
        # Give opencode-supervise SIGTERM first so its signal handler
        # can terminate the active OpenCode process group.
        proc.terminate()

        try:
            stdout, stderr = proc.communicate(
                timeout=10,
            )

        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        stderr = (
            (stderr or "")
            + "\n[hybrid] opencode-supervise exceeded "
            + f"{timeout}s and was terminated.\n"
        )

        return ExternalResult(
            ok=False,
            returncode=124,
            stdout=stdout or "",
            stderr=stderr,
        )

    return ExternalResult(
        ok=proc.returncode == 0,
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
    )

    return ExternalResult(
        ok=result.returncode == 0,
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def effective_status(
    supervisor_returncode: int,
    *,
    validation_ok: bool,
) -> str:
    """
    Resolve the effective coding outcome.

    Deterministic validation is authoritative.

    The supervisor return code remains useful diagnostic metadata,
    but a completed worker session is not considered successful when
    deterministic validation still fails.
    """
    if validation_ok:
        return "SUCCESS"

    return "NEEDS_CLOUD_INPUT"
