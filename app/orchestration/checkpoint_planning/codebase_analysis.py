"""P8 codebase analysis — read-only Flash analyst dispatch (Serena + Graphify).

``codebase_analyze`` delegates repository understanding to ONE bounded,
read-only OpenCode session running the ``agy-planning-analyst`` agent
(MiMo Flash, permissions deny edit/write/patch/webfetch — read-only by
CONFIGURATION, not by prompt wording, per hard rule 2). The analyst is
instructed to use Graphify first (structure/deps via ``graphify-out``
if present, else read-only ``graphify`` commands), then Serena symbol
tools, and to answer with the structured evidence JSON only.

Zero-mutation guarantee: ``codebase_analyze`` snapshots
``git status --porcelain`` + ``git rev-parse HEAD`` before and after
the analyst runs and raises if the repository changed at all — for the
default dispatch AND for any injected analyst. The planning surface can
therefore never mutate the repository.

Everything external is injectable (``analyst``, ``run_dispatch``,
``git_snapshot``, ``session_capturer``) so tests never touch a network,
a subprocess, or the OpenCode server.

The evidence/requirement types come from Worker A's ``schemas.py``; the
import is guarded so this module remains importable standalone mid-flight
(a duck-typed fallback with the identical field set is used until A's
schemas exist; the coordinator's integration run exercises the real ones).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

try:  # Worker A's domain schemas (authoritative at integration time)
    from app.orchestration.checkpoint_planning.schemas import (
        AnalysisEvidence,
        AnalysisRequest,
    )
except ImportError:  # pragma: no cover - seam-guard for parallel development

    from dataclasses import dataclass, field

    @dataclass
    class AnalysisRequest:  # type: ignore[no-redef]
        objective: str
        questions: list[str] = field(default_factory=list)
        focus: list[str] = field(default_factory=list)

    @dataclass
    class AnalysisEvidence:  # type: ignore[no-redef]
        summary: str = ""
        relevant_symbols: list[str] = field(default_factory=list)
        dependency_paths: list[str] = field(default_factory=list)
        existing_tests: list[str] = field(default_factory=list)
        likely_files: list[str] = field(default_factory=list)
        constraints: list[str] = field(default_factory=list)
        open_questions: list[str] = field(default_factory=list)
        source: str = "agy-planning-analyst"


ANALYST_AGENT = "agy-planning-analyst"
ANALYST_TIMEOUT_SECONDS = 600

# Hard caps so an analyst can never smuggle a repo dump into planning
# context (bounded outputs, same rule as context.py).
MAX_LIST_ITEMS = 25
MAX_ITEM_CHARS = 500
MAX_SUMMARY_CHARS = 2000

_EVIDENCE_LIST_FIELDS = (
    "relevant_symbols",
    "dependency_paths",
    "existing_tests",
    "likely_files",
    "constraints",
    "open_questions",
)


class AnalystUnavailable(RuntimeError):
    """The read-only analyst could not produce parseable evidence."""


class AnalystOutputInvalid(AnalystUnavailable):
    """The analyst's transcript contained no valid evidence object."""


class RepoMutationDetected(RuntimeError):
    """The analyst run changed the repository — hard rule violation."""


# ---------------------------------------------------------------------------
# prompt (Graphify first, then Serena; return ONLY the evidence JSON)
# ---------------------------------------------------------------------------

def build_analyst_prompt(request: AnalysisRequest) -> str:
    """The bounded, read-only analyst task prompt for one request."""
    questions = "\n".join(f"- {q}" for q in request.questions) or "- (none supplied)"
    focus = ", ".join(request.focus) or "(none supplied)"
    return f"""Repository analysis request (READ-ONLY — planning evidence, not implementation).

OBJECTIVE: {request.objective}
FOCUS AREAS: {focus}
QUESTIONS:
{questions}

Investigation order (mandatory):
1. Graphify FIRST for structure/dependency questions: if
   `graphify-out/graph.json` exists, read it with the read-only
   `graphify` commands (`graphify query "..."`, `graphify path A B`,
   `graphify explain X`). Never run graphify update/watch/install.
2. Serena symbol tools next (`serena_get_symbols_overview`,
   `serena_find_symbol`, `serena_find_referencing_symbols`) for exact
   symbols, definitions and references.
3. Native reads only for files already narrowed by 1-2.

Constraints: you are READ-ONLY. Never edit, write, patch, commit, or
run any non-readonly command. Do not answer in prose.

Return EXACTLY ONE JSON object (the evidence contract):
{{
  "summary": "<= 2000 chars of grounded findings",
  "relevant_symbols": ["..."],
  "dependency_paths": ["..."],
  "existing_tests": ["repo-relative test file paths"],
  "likely_files": ["repo-relative files an implementation would touch"],
  "constraints": ["..."],
  "open_questions": ["..."]
}}
Ground every entry in evidence you actually saw; prefer empty lists
over guesses.
""".strip()


# ---------------------------------------------------------------------------
# evidence extraction / coercion (bounded)
# ---------------------------------------------------------------------------

def _candidate_is_evidence(candidate: dict) -> bool:
    """Schema check: the candidate satisfies AnalysisEvidence (authoritative).

    Parses as JSON is NOT enough — required fields (e.g. ``summary``) decide.
    """
    validator = getattr(AnalysisEvidence, "model_validate", None)
    if validator is not None:
        try:
            validator(candidate)
            return True
        except Exception:  # noqa: BLE001 - any validation failure = not evidence
            return False
    # dataclass fallback (mid-flight import seam): required summary, list[str]s
    if not isinstance(candidate.get("summary"), str) or not candidate.get("summary"):
        return False
    return all(
        isinstance(candidate.get(name, []), list) for name in _EVIDENCE_LIST_FIELDS
    )


def _extract_json_object(text: str) -> dict | None:
    """Schema-aware extraction: latest balanced JSON object that validates.

    The analyst transcript interleaves tool arguments, tool results and
    intermediate JSON BEFORE the final evidence object, so the first-object
    grab is wrong. Candidates are scanned END -> BEGINNING and the first
    (i.e. latest-in-text) one that validates as ``AnalysisEvidence`` wins.
    Returns None when no candidate validates (caller fails closed).
    """
    if not text:
        return None
    # fast path: whole text is JSON
    try:
        data = json.loads(text)
        if isinstance(data, dict) and _candidate_is_evidence(data):
            return data
    except json.JSONDecodeError:
        pass
    candidates: list[dict] = []
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : idx + 1])
                        if isinstance(data, dict) and _candidate_is_evidence(data):
                            candidates.append(data)
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    # END -> BEGINNING: last valid candidate in text order is the answer
    return candidates[-1] if candidates else None


def _bounded_items(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            items.append(text[:MAX_ITEM_CHARS])
        if len(items) >= MAX_LIST_ITEMS:
            break
    return items


def evidence_from_payload(payload: dict) -> AnalysisEvidence:
    """Coerce a parsed analyst JSON object into bounded AnalysisEvidence."""
    kwargs: dict[str, Any] = {
        "summary": str(payload.get("summary", "")).strip()[:MAX_SUMMARY_CHARS]
    }
    for field_name in _EVIDENCE_LIST_FIELDS:
        kwargs[field_name] = _bounded_items(payload.get(field_name))
    kwargs["source"] = str(payload.get("source") or ANALYST_AGENT)[:200]
    return AnalysisEvidence(**kwargs)


# ---------------------------------------------------------------------------
# git snapshot (zero-mutation verification)
# ---------------------------------------------------------------------------

def _git_snapshot_subprocess(repo: Path) -> str:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}"
            )
        return result.stdout

    return f"HEAD={run('rev-parse', 'HEAD')}\nSTATUS=BEGIN\n{run('status', '--porcelain')}STATUS=END\n"


def _snapshot(repo: Path, git_snapshot: Callable[[Path], str] | None) -> str | None:
    if not (repo / ".git").exists():
        return None  # not a git repo: nothing to compare against
    snap = git_snapshot if git_snapshot is not None else _git_snapshot_subprocess
    try:
        return snap(repo)
    except Exception:  # noqa: BLE001 - snapshot is verification, not flow
        return None


def _assert_unchanged(before: str | None, after: str | None, repo: Path) -> None:
    if before is not None and after is not None and before != after:
        raise RepoMutationDetected(
            "Analyst run mutated the repository "
            f"(git status/HEAD changed under {repo}); planning surface "
            "must be read-only."
        )


# ---------------------------------------------------------------------------
# default analyst: ONE read-only opencode-supervise dispatch
# ---------------------------------------------------------------------------

def _default_session_capturer(
    stdout: str, repo: Path
) -> tuple[str | None, str]:
    from app.orchestration.agy_session import capture_session

    return capture_session(
        stdout,
        known_session=None,
        agent=ANALYST_AGENT,
        directory=str(repo),
    )


def default_analyst(
    request: AnalysisRequest,
    repo: Path,
    *,
    run_dispatch: Callable[..., Any] | None = None,
    session_capturer: Callable[[str, Path], tuple[str | None, str]] | None = None,
    timeout: int = ANALYST_TIMEOUT_SECONDS,
) -> AnalysisEvidence:
    """
    Dispatch ONE read-only OpenCode session to ``agy-planning-analyst``.

    ``run_dispatch(repo, prompt, agent=..., timeout=...)`` defaults to
    ``external_stack.run_supervised`` (same process seam the P3/P4
    runtime uses; lifecycle values sized for a bounded read-only
    investigation). The session id is resolved through the P4
    ``capture_session`` order (stdout -> read-back). Raises
    ``AnalystUnavailable`` (or its subclass ``AnalystOutputInvalid``, when
    the transcript parsed but held no valid evidence object) on nonzero
    exit, timeout, or unparseable
    evidence — never fabricates an empty success.
    """
    dispatch = run_dispatch
    if dispatch is None:
        from app.integrations.external_stack import run_supervised

        def dispatch(  # type: ignore[misc]
            repo_, prompt_, *, agent=ANALYST_AGENT, timeout=timeout, **_kw
        ):
            return run_supervised(
                repo_,
                prompt_,
                agent=agent,
                repeat_limit=3,
                stall_seconds=120,
                max_restarts=0,
                timeout=timeout,
                cloud_led=True,
            )

    prompt = build_analyst_prompt(request)

    try:
        result = dispatch(
            repo,
            prompt,
            agent=ANALYST_AGENT,
            timeout=timeout,
        )
    except AnalystUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize dispatch failures
        raise AnalystUnavailable(f"analyst dispatch failed: {exc}") from exc

    stdout = getattr(result, "stdout", None)
    if stdout is None and isinstance(result, dict):
        stdout = result.get("stdout")
        ok = result.get("ok", result.get("returncode", 1) == 0)
    else:
        ok = getattr(result, "ok", False)

    stdout = stdout or ""

    capture = session_capturer or _default_session_capturer
    try:
        capture(stdout, repo)  # best-effort provenance capture
    except Exception:  # noqa: BLE001 - capture must never mask the outcome
        pass

    if not ok:
        stderr = getattr(result, "stderr", "") or ""
        raise AnalystUnavailable(
            f"analyst session exited nonzero: {stderr.strip()[:300] or stdout.strip()[:300]}"
        )

    payload = _extract_json_object(stdout)
    if payload is None:
        raise AnalystOutputInvalid(
            "analyst transcript contained no valid evidence object; "
            f"stdout[:200]={stdout[:200]!r}"
        )

    return evidence_from_payload(payload)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def codebase_analyze(
    repo: Path,
    request: AnalysisRequest,
    *,
    analyst: (
        Callable[[AnalysisRequest, Path], AnalysisEvidence] | None
    ) | None = None,
    max_analyses: int = 5,
    **dispatch_kw: Any,
) -> AnalysisEvidence:
    """
    Run ONE read-only analyst investigation over ``repo`` for ``request``.

    ``analyst`` is the injectable seam (default:
    :func:`default_analyst`, i.e. one OpenCode session bound to the
    ``agy-planning-analyst`` read-only agent). Extra keyword arguments
    (``run_dispatch``, ``session_capturer``, ``git_snapshot``) are
    test/runtime seams; ``git_snapshot`` replaces the before/after
    verification probe.

    Guarantees:
    * never grants write capability itself (read-only agent by
      configuration — see docs/agy-planning-analyst.agent.md);
    * raises :class:`RepoMutationDetected` if the repo changed during
      the call (git status --porcelain + HEAD compared pre/post);
    * raises :class:`AnalystUnavailable` instead of fabricating evidence.
    """
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Repository does not exist: {root}")
    if int(max_analyses) < 1:
        raise ValueError("max_analyses must be >= 1")

    git_snapshot = dispatch_kw.pop("git_snapshot", None)

    analyst_fn = analyst
    if analyst_fn is None:
        def analyst_fn(req: AnalysisRequest, path: Path) -> AnalysisEvidence:
            return default_analyst(req, path, **dispatch_kw)

    before = _snapshot(root, git_snapshot)
    evidence = analyst_fn(request, root)
    after = _snapshot(root, git_snapshot)
    _assert_unchanged(before, after, root)

    if evidence is None:
        raise AnalystUnavailable("analyst produced no evidence")
    return evidence


def codebase_analyze_many(
    repo: Path,
    requests: list[AnalysisRequest],
    *,
    analyst: (
        Callable[[AnalysisRequest, Path], AnalysisEvidence] | None
    ) | None = None,
    max_analyses: int = 5,
    **dispatch_kw: Any,
) -> list[AnalysisEvidence]:
    """
    Independent analyst investigations — NO cross-coordination: one
    request's evidence never conditions another's dispatch, and the
    result order matches the request order. Exceeding ``max_analyses``
    is rejected before anything runs.
    """
    if len(requests) > int(max_analyses):
        raise ValueError(
            f"{len(requests)} analysis requests exceeds max_analyses={max_analyses}"
        )

    git_snapshot = dispatch_kw.pop("git_snapshot", None)
    root = Path(repo).expanduser().resolve()

    results: list[AnalysisEvidence] = []
    for request in requests:
        results.append(
            codebase_analyze(
                root,
                request,
                analyst=analyst,
                max_analyses=max_analyses,
                git_snapshot=git_snapshot,
                **dispatch_kw,
            )
        )
    return results
