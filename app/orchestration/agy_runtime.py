"""P3 live runtime helpers: the real callables ``execute_stage`` and
the live E2E consume.

Kept thin — every effect is one explicit seam:

* :func:`build_live_executor` — dispatch execution through
  ``opencode-supervise`` (agent ``agy-dispatch``) with OpenCode
  session capture, OpenCode-layer failover (4 primary attempts on the
  SAME session, then ONE same-session re-run under
  ``agy-dispatch-fallback`` emitting a schema record with the real
  session id), plus git-based changed-file collection and shell
  command execution. Worker prose never decides anything: task
  verification is the task's own verification command through
  ``run_command``.
* :func:`make_live_committer` — the ONE meaningful stage commit
  (``git add -A -- .`` respecting ``.gitignore``, never staging
  ``state/``, ``.serena/`` or scratch), message
  ``AGY stage <stage_id>: <objective>`` (a repair commit's message is
  supplied by the caller and wins).
* :func:`build_cumulative_suite` — stage acceptance commands plus the
  full repo test suite. P3 uses this conservative full-suite selection
  by design; later milestones may pick a smaller relevant suite.

P4 (session capture): the bare stdout extraction inside
``execute_task`` is replaced by :func:`capture_session` — stdout
first, then the already-known session, then a SERVER READ-BACK keyed
by ``(agent, repo directory, since=stage-run start)``. The read-back
is injectable (``session_read_back=``, default ``read_back_session``)
so tests never touch the network, the capture SOURCE (``stdout`` /
``known`` / ``readback`` / ``none``) is recorded per task in the
executor's ``TaskSessionLog`` (``execute_task.session_log``) and
forwarded to ``on_session`` binders that understand it. The
same-session failover path (4 primary attempts, then one same-session
run under the fallback agent) is UNCHANGED — only how an id is
captured when the supervisor prints nothing differs.

The ``opencode-supervise`` invocation itself is FACTORED behind an
injectable callable (``run_supervised=`` in
:func:`build_live_executor`, default ``run_agy_supervised``) so tests
exercise the retry/failover/session logic without launching any
process or contacting any network.

LIVENESS: the provider layer (the AGY gatekeeper decision call) uses
``complete_with_failover`` from ``failover``; the OpenCode layer
above is where same-session continuity lives, because provider calls
are stateless.
"""

from __future__ import annotations

import inspect
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from app.config import AgySettings
from app.integrations.external_stack import ExternalResult
from app.orchestration.agy_session import (
    SESSION_ANY_RE,
    SESSION_LINE_RE,
    TaskSessionLog,
    capture_session,
    extract_session_from_stdout,
    read_back_session,
)
from app.orchestration.agy_supervisor import run_agy_supervised
from app.orchestration.failover import (
    PRIMARY_MAX_ATTEMPTS,
    build_failover_event,
    classify_reason,
    is_transient_provider_failure,
)
from app.orchestration.plan_decompose import build_stage_dispatch_prompt
from app.providers import ModelRole, ProviderError
from app.schemas import AcceptanceCommand, DevelopmentPlan, PlanStage

PRIMARY_DISPATCH_AGENT = "agy-dispatch"
FALLBACK_DISPATCH_AGENT = "agy-dispatch-fallback"

COMMAND_TIMEOUT_SECONDS = 600

# Session capture from opencode-supervise stdout: the supervisor
# prints 'Session ID: ses_...'; the loose pattern keeps the same
# semantics as external_stack's validate_session_id regex. The patterns
# and the extractor are owned by agy_session (they must agree with the
# read-back capture); the runtime name is kept for P3 callers.
_SESSION_LINE_RE = SESSION_LINE_RE
_SESSION_ANY_RE = SESSION_ANY_RE

_COMMIT_EXCLUDED_PREFIXES = ("state/", ".serena/", "scratch/", ".git/")


def extract_session_id(text: str | None) -> str | None:
    """First OpenCode session id in supervisor output, or None."""
    return extract_session_from_stdout(text)


def build_live_executor(
    settings: AgySettings,
    repo: str | Path,
    plan: DevelopmentPlan,
    stage: PlanStage,
    *,
    on_session: Callable[[str], Any] | None = None,
    on_failover_event: Callable[[dict], None] | None = None,
    run_supervised: Callable[..., ExternalResult] | None = None,
    primary_agent: str = PRIMARY_DISPATCH_AGENT,
    fallback_agent: str = FALLBACK_DISPATCH_AGENT,
    session_read_back: Callable[..., str | None] | None = read_back_session,
) -> tuple[
    Callable,
    Callable,
    Callable,
    Callable,
]:
    """
    Build the LIVE ``(execute_task, validate_task,
    collect_changed_files, run_command)`` quadruple for ONE stage.

    ``plan``/``stage`` are required here (the contract's §6.1
    signature omitted them but ``build_stage_dispatch_prompt(plan,
    stage, task)`` needs both — documented deviation).

    ``run_supervised`` defaults to ``run_agy_supervised`` and is the
    ONLY process-launching seam, so tests inject a fake there.

    SESSION CAPTURE (P4): per supervised run the session id is resolved
    through ``capture_session`` — supervisor stdout first, then the
    session already known for this task, then the server read-back
    (``agent``, this repo's directory, ``since_ms`` = the moment this
    stage executor was built). ``session_read_back`` defaults to
    ``read_back_session`` (the live read-back); tests inject a stub so
    no test ever hits the network (pass an explicit no-op to disable).
    Both the id and the source go to the executor's ``TaskSessionLog``
    (``execute_task.session_log``) and to ``on_session`` — binders
    that accept ``(task_id, session_id, source)`` receive the source,
    plain two-argument binders (P3 ``SessionRecorder``) keep working
    unchanged. The returned quadruple still has exactly four elements.
    """
    runner = run_supervised if run_supervised is not None else run_agy_supervised
    sessions: dict[str, str] = {}
    session_log = TaskSessionLog()
    directory = str(repo)
    # Stage-run start: read-back only accepts sessions updated at/after
    # this epoch-ms, so this run can only ever capture ITS OWN session.
    stage_started_ms = int(time.time() * 1000)

    def _binder_takes_source() -> bool:
        """Does on_session.bind accept the P4 (task, session, source)?"""
        binder = getattr(on_session, "bind", None)
        if not callable(binder):
            return False
        try:
            parameters = inspect.signature(binder).parameters
        except (TypeError, ValueError):  # builtins / uninspectable
            return False
        if any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in parameters.values()
        ):
            return True
        return len(parameters) >= 3

    bind_takes_source = _binder_takes_source()

    def _note_session(
        task_id: str,
        session_id: str | None,
        source: str = "known",
    ) -> None:
        if not session_id:
            return
        sessions[task_id] = session_id
        session_log.record(task_id, session_id, source)
        binder = getattr(on_session, "bind", None)
        if callable(binder):
            if bind_takes_source:
                binder(task_id, session_id, source)
            else:
                binder(task_id, session_id)
        elif on_session is not None:
            on_session(session_id)

    def _capture(
        task_id: str,
        *,
        stdout: str | None,
        known_session: str | None,
        agent: str,
    ) -> str | None:
        session_id, _source = capture_session(
            stdout,
            known_session=known_session,
            agent=agent,
            directory=directory,
            since_ms=stage_started_ms,
            read_back=session_read_back,
        )
        if session_id:
            _note_session(task_id, session_id, _source)
        return session_id

    def _report_failover(event: dict) -> None:
        if on_failover_event is not None:
            on_failover_event(event)

    async def execute_task(task, attempt: int, repair_context: str | None) -> str:
        prompt = build_stage_dispatch_prompt(plan, stage, task)

        if repair_context:
            prompt += (
                "\n\nREPAIR CONTEXT (validation failed on the previous "
                "attempt):\n"
                + str(repair_context)
            )

        session = sessions.get(task.id)
        agent = primary_agent
        attempts = 0
        last_transient_text = ""

        while True:
            attempts += 1

            result = runner(
                settings,
                repo,
                prompt,
                agent=agent,
                session=session,
            )

            captured = _capture(
                task.id,
                stdout=result.stdout,
                known_session=session,
                agent=agent,
            )
            session = captured

            failure_text = (result.stderr or "").strip() or (
                result.stdout or ""
            )
            transient = (not result.ok) and is_transient_provider_failure(
                ProviderError(
                    failure_text or "supervised run failed",
                    status=None,
                    body=result.stderr or None,
                )
            )

            if not transient:
                # Either it worked, or it failed for a NON-provider
                # reason (validation decides; no failover for
                # deterministic failures).
                return result.output

            last_transient_text = failure_text

            if attempts < PRIMARY_MAX_ATTEMPTS:
                # Same session, same agent, bounded retry.
                continue

            # Primary budget exhausted -> same-session failover under
            # the fallback agent (one shot, never an infinite loop).
            fallback_result = runner(
                settings,
                repo,
                prompt,
                agent=fallback_agent,
                session=session,
            )
            fallback_captured = _capture(
                task.id,
                stdout=fallback_result.stdout,
                known_session=session,
                agent=fallback_agent,
            )
            session = fallback_captured

            _report_failover(
                build_failover_event(
                    role=ModelRole.DISPATCH,
                    primary_model=settings.dispatch_model,
                    fallback_model=settings.dispatch_fallback_model,
                    fallback_reason=classify_reason(
                        None,
                        last_transient_text,
                    ),
                    session_id=sessions.get(task.id),
                    parent_session_id=None,
                    fallback_count=1,
                )
            )

            return fallback_result.output

    async def run_command(command: AcceptanceCommand) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                command.command,
                shell=True,
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return (
                124,
                f"command timed out after {COMMAND_TIMEOUT_SECONDS}s: "
                + command.command,
            )

        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    async def validate_task(task) -> tuple[bool, str]:
        # Worker prose is ignored: the verification command decides.
        command = AcceptanceCommand(
            command=task.verification.command,
            expected_exit_code=task.verification.expected_exit_code,
        )
        exit_code, output = await run_command(command)
        return exit_code == command.expected_exit_code, output

    def collect_changed_files(task=None) -> list[str]:
        _ = task  # repo-relative truth, identical for every task

        repo_path = Path(repo)
        paths: list[str] = []

        porcelain = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        for line in (porcelain.stdout or "").splitlines():
            if len(line) > 3:
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ")[-1].strip()
                path = path.strip('"')
                if path:
                    paths.append(path)

        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if diff.returncode != 0:
            diff = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--name-only"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        for line in (diff.stdout or "").splitlines():
            line = line.strip()
            if line:
                paths.append(line)

        seen: set[str] = set()
        ordered: list[str] = []
        for path in paths:
            if path not in seen:
                seen.add(path)
                ordered.append(path)

        return ordered

    # P4 read-back seam: the plan driver recovers each task's
    # (session_id, source) evidence from here (default capturer).
    setattr(execute_task, "session_log", session_log)

    return execute_task, validate_task, collect_changed_files, run_command


def make_live_committer(repo: str | Path) -> Callable[[dict], dict]:
    """
    ``make_commit`` for the live repository: stages everything the
    ``.gitignore`` allows (NEVER ``state/``, ``.serena/`` or scratch)
    and creates the ONE stage commit.

    Message: ``AGY stage <stage_id>: <objective>`` — unless the caller
    supplies ``evidence['message']`` (the bounded repair commit does).
    Returns ``{plan_id, stage_id, parent_sha, commit_sha,
    changed_files, message}``; ``execute_stage`` enriches the record
    with task results and verification evidence for ``commit.json``.
    """
    repo_path = Path(repo)

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def make_commit(evidence: dict) -> dict:
        plan_id = evidence.get("plan_id", "")
        stage_id = evidence.get("stage_id", "")
        objective = str(evidence.get("stage_objective") or "")
        message = evidence.get("message") or (
            f"AGY stage {stage_id}: {objective}"
        )

        parent = _git("rev-parse", "HEAD")
        parent_sha = parent.stdout.strip() if parent.returncode == 0 else ""

        staged_result = _git("add", "-A", "--", ".")
        if staged_result.returncode != 0:
            raise RuntimeError(
                "git add failed: " + staged_result.stderr.strip()
            )

        # Belt-and-braces: never stage run state, Serena artifacts or
        # scratch even if a .gitignore is missing.
        _git("reset", "-q", "HEAD", "--", "state", ".serena", "scratch")

        staged = _git("diff", "--cached", "--name-only")
        changed = [
            line.strip()
            for line in (staged.stdout or "").splitlines()
            if line.strip()
            and not line.strip().startswith(_COMMIT_EXCLUDED_PREFIXES)
        ]

        if not changed:
            raise RuntimeError(
                "stage commit refused: nothing in scope to commit "
                f"(stage {stage_id!r})"
            )

        commit_result = _git("commit", "-m", message)
        if commit_result.returncode != 0:
            raise RuntimeError(
                "git commit failed: "
                + (commit_result.stderr or commit_result.stdout).strip()
            )

        head = _git("rev-parse", "HEAD")
        commit_sha = head.stdout.strip()

        return {
            "plan_id": plan_id,
            "stage_id": stage_id,
            "parent_sha": parent_sha,
            "commit_sha": commit_sha,
            "changed_files": changed,
            "message": message,
        }

    return make_commit


def build_cumulative_suite(
    repo: str | Path,
    stage: PlanStage,
    *,
    full_suite_command: str | None = None,
) -> list[AcceptanceCommand]:
    """
    P3's conservative cumulative suite: the stage's own acceptance
    commands PLUS the full repository test suite command.

    The full-suite command is explicit or auto-detected for the
    disposable E2E layout (``tests/`` -> unittest discover, else
    ``pytest -q``, else ``git diff --check``). P3 deliberately runs
    the conservative full suite; later milestones may select a
    smaller RELEVANT suite instead — nothing repository-specific is
    hardcoded in ``agy_execute``.
    """
    if full_suite_command is None:
        repo_path = Path(repo)

        if (repo_path / "tests").is_dir():
            full_suite_command = "python3 -m unittest discover -s tests -v"
        elif (repo_path / "test").is_dir():
            full_suite_command = "python3 -m unittest discover -s test -v"
        else:
            full_suite_command = "python3 -m pytest -q"

    return [
        *stage.acceptance_commands,
        AcceptanceCommand(
            command=full_suite_command,
            expected_exit_code=0,
            description="P3 conservative full repository test suite",
        ),
    ]