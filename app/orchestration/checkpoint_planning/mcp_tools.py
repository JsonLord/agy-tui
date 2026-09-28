"""P8 MCP tool bodies — the planning surface Gemini-in-AGY-CLI calls.

Plain functions (NO mcp import here; ``mcp_gateway.py`` appends the
``@mcp.tool()`` wrappers). Every tool is read-only against the repo or
writes ONLY under a supplied ``state_root``. No shell execution beyond
the read-only analyst dispatch. Worker A's domain modules
(schemas/validation/planner/submission) are imported LAZILY inside the
tools that need them, so this module always imports cleanly (context
and analysis tools work standalone mid-flight); a planning-domain tool
called before integration raises a clear ``ToolUnavailable`` rather
than an ImportError traceback.

Tool set (8, per contract):
  agy_project_inspect_tool / agy_design_context_tool /
  agy_learnings_read_tool / agy_gate_templates_tool /
  agy_codebase_analyze_tool / agy_checkpoint_plan_validate_tool /
  agy_checkpoint_plan_preview_tool / agy_checkpoint_plan_submit_tool
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from app.orchestration.checkpoint_planning.context import (
    agy_design_context,
    agy_learnings_read,
    agy_project_inspect,
)
from app.orchestration.checkpoint_planning.codebase_analysis import (
    AnalystUnavailable,
    RepoMutationDetected,
    codebase_analyze,
)


class ToolUnavailable(RuntimeError):
    """The planning-domain seam (Worker A) is not importable yet."""


def _require(name: str, module_name: str) -> Any:
    """Lazily import a Worker A public name with a clear failure mode."""
    try:
        module = __import__(
            f"app.orchestration.checkpoint_planning.{module_name}",
            fromlist=[name],
        )
        return getattr(module, name)
    except ImportError as exc:  # pragma: no cover - integration-time path
        raise ToolUnavailable(
            f"planning-domain seam '{name}' "
            f"(app.orchestration.checkpoint_planning.{module_name}) "
            f"not available yet: {exc}"
        ) from exc


def _resolve_repo(repo: str) -> Path:
    path = Path(str(repo)).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Repository does not exist: {path}")
    return path


# ---------------------------------------------------------------------------
# context tools (1-3)
# ---------------------------------------------------------------------------

def agy_project_inspect_tool(repo: str) -> dict:
    """Bounded project identity for planning (no file dumps)."""
    return agy_project_inspect(_resolve_repo(repo))


def agy_design_context_tool(repo: str, query: str = "") -> dict:
    """Bounded design excerpts, optionally keyword-targeted."""
    return agy_design_context(_resolve_repo(repo), query or None)


def agy_learnings_read_tool(repo: str, query: str = "") -> dict:
    """learnings.md topics (## sections), keyword-matched when queried."""
    return agy_learnings_read(_resolve_repo(repo), query or None)


# ---------------------------------------------------------------------------
# gate templates tool (4)
# ---------------------------------------------------------------------------

def agy_gate_templates_tool() -> dict:
    """Canonical gate templates (G1..G4 preseed workflow, version '1')."""
    gate_templates = _require("gate_templates", "planner")
    return gate_templates(version="1")


# ---------------------------------------------------------------------------
# analyst tool (5)
# ---------------------------------------------------------------------------

def agy_codebase_analyze_tool(
    repo: str,
    objective: str,
    questions: list[str] | None = None,
    focus: list[str] | None = None,
) -> dict:
    """
    One read-only analyst investigation (agy-planning-analyst:
    Graphify -> Serena; never mutates — verified git pre/post).
    Returns the evidence object as a plain dict.
    """
    root = _resolve_repo(repo)
    if not str(objective).strip():
        raise ValueError("objective must not be empty")

    AnalysisRequest = _require("AnalysisRequest", "schemas")

    request = AnalysisRequest(
        objective=str(objective),
        questions=[str(q) for q in (questions or [])],
        focus=[str(f) for f in (focus or [])],
    )

    evidence = codebase_analyze(root, request)
    if hasattr(evidence, "model_dump"):
        return evidence.model_dump(mode="json")
    return {
        key: getattr(evidence, key)
        for key in (
            "summary",
            "relevant_symbols",
            "dependency_paths",
            "existing_tests",
            "likely_files",
            "constraints",
            "open_questions",
            "source",
        )
    }


# ---------------------------------------------------------------------------
# plan validate / preview / submit tools (6-8)
# ---------------------------------------------------------------------------

def _parse_plan(plan_json: str) -> Any:
    """Parse + schema-validate a planner-authored plan document."""
    CheckpointPlan = _require("CheckpointPlan", "schemas")
    try:
        raw = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan_json is not valid JSON: {exc}") from exc
    return CheckpointPlan.model_validate(raw)


def agy_checkpoint_plan_validate_tool(plan_json: str) -> dict:
    """Run the deterministic plan validation rules; never mutates state."""
    validate_checkpoint_plan = _require(
        "validate_checkpoint_plan", "validation"
    )
    plan = _parse_plan(plan_json)
    result = validate_checkpoint_plan(plan)
    errors = list(getattr(result, "errors", []) or [])
    return {"ok": bool(getattr(result, "ok", False)), "errors": errors}


def agy_checkpoint_plan_preview_tool(plan_json: str) -> dict:
    """Plain-language preview of the plan + its validation outcome."""
    plan = _parse_plan(plan_json)

    preview_checkpoint_plan = _require("preview_checkpoint_plan", "planner")
    validate_checkpoint_plan = _require(
        "validate_checkpoint_plan", "validation"
    )

    text = preview_checkpoint_plan(plan)
    result = validate_checkpoint_plan(plan)
    return {
        "preview": text,
        "ok": bool(getattr(result, "ok", False)),
        "errors": list(getattr(result, "errors", []) or []),
    }


def agy_checkpoint_plan_submit_tool(plan_json: str, state_root: str) -> dict:
    """
    Submit a complete, gate-preseeded plan to durable state and YIELD
    execution to AGY-local. Persists only under ``state_root``.
    """
    submit_checkpoint_plan = _require(
        "submit_checkpoint_plan", "submission"
    )
    plan = _parse_plan(plan_json)

    root = Path(str(state_root)).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    result = submit_checkpoint_plan(plan, state_root=root)
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return {
        key: getattr(result, key)
        for key in (
            "accepted",
            "plan_id",
            "runtime_owner",
            "next_gate",
            "planner_session_complete",
            "errors",
        )
    }
