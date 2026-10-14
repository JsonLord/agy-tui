from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FEATURE_STATUSES = ("pending", "running", "completed", "failed", "verified")
LIST_LIMIT = 200
TITLE_LIMIT = 300
DESCRIPTION_LIMIT = 4000
TEXT_LIMIT = 200
DEPS_LIMIT = 100


class JobCreateInput(BaseModel):
    """Restricted create contract for POST /api/features/create.

    Deliberately excludes model/provider/thinking/reasoning fields: worker
    execution identity belongs to workspace-native OpenCode configuration.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_LIMIT)
    description: str = Field(default="", max_length=20000)
    category: str = Field(default="Uncategorized", min_length=1, max_length=TEXT_LIMIT)
    priority: int | None = Field(default=None, ge=0, le=10)
    dependencies: list[str] = Field(default_factory=list, max_length=DEPS_LIMIT)
    branch_name: str | None = Field(default=None, min_length=1, max_length=TEXT_LIMIT)

    def to_feature(self) -> dict:
        feature: dict[str, Any] = {
            "title": self.title,
            "description": self.description,
            "category": self.category,
        }
        if self.priority is not None:
            feature["priority"] = self.priority
        if self.dependencies:
            feature["dependencies"] = list(self.dependencies)
        if self.branch_name:
            feature["branchName"] = self.branch_name
        return feature


class JobUpdateInput(BaseModel):
    """Restricted update contract for POST /api/features/update."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "running", "completed", "failed", "verified"] | None = (
        None
    )
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_LIMIT)
    description: str | None = Field(default=None, max_length=20000)
    category: str | None = Field(default=None, min_length=1, max_length=TEXT_LIMIT)
    priority: int | None = Field(default=None, ge=0, le=10)
    dependencies: list[str] | None = Field(default=None, max_length=DEPS_LIMIT)
    branch_name: str | None = Field(default=None, min_length=1, max_length=TEXT_LIMIT)

    @model_validator(mode="after")
    def _at_least_one(self) -> JobUpdateInput:
        if not self.model_dump(exclude_none=True):
            raise ValueError("at least one field is required")
        return self

    def to_updates(self) -> dict:
        updates: dict[str, Any] = {}
        if self.status is not None:
            updates["status"] = self.status
        if self.title is not None:
            updates["title"] = self.title
        if self.description is not None:
            updates["description"] = self.description
        if self.category is not None:
            updates["category"] = self.category
        if self.priority is not None:
            updates["priority"] = self.priority
        if self.dependencies is not None:
            updates["dependencies"] = list(self.dependencies)
        if self.branch_name is not None:
            updates["branchName"] = self.branch_name
        return updates


def _text(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _bounded_list(items: list) -> tuple[list, bool]:
    return items[:LIST_LIMIT], len(items) > LIST_LIMIT


def health_summary(payload: dict) -> dict:
    return {
        "status": _text(payload.get("status"), 40),
        "version": _text(payload.get("version"), 60),
        "timestamp": _text(payload.get("timestamp"), 40),
    }


def project_summaries(payload: dict) -> dict:
    projects = payload.get("projects")
    items, truncated = _bounded_list(projects if isinstance(projects, list) else [])
    return {
        "projects": [
            {
                "project_id": _text(item.get("projectId")),
                "project_name": _text(item.get("projectName")),
                "project_path": _text(item.get("projectPath"), 1000),
                "health_status": _text(item.get("healthStatus"), 40),
                "total_features": item.get("totalFeatures")
                if isinstance(item.get("totalFeatures"), int)
                else None,
                "feature_counts": _counts(item.get("featureCounts")),
                "is_auto_mode_running": item.get("isAutoModeRunning") is True,
                "active_branch": _text(item.get("activeBranch")),
                "last_activity_at": _text(item.get("lastActivityAt"), 40),
            }
            for item in items
            if isinstance(item, dict)
        ],
        "truncated": truncated,
        "generated_at": _text(payload.get("generatedAt"), 40),
    }


def _counts(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value[key]
        for key in ("total", "pending", "running", "completed", "failed", "verified")
        if isinstance(value.get(key), int)
    }


def job_summary(feature: dict, *, detailed: bool = False) -> dict:
    job = {
        "id": _text(feature.get("id"), 100),
        "title": _text(feature.get("title"), TITLE_LIMIT),
        "status": _text(feature.get("status"), 40),
        "priority": feature.get("priority")
        if isinstance(feature.get("priority"), int)
        else None,
        "category": _text(feature.get("category")),
        "dependencies": [
            _text(item, TITLE_LIMIT)
            for item in feature.get("dependencies", [])[:DEPS_LIMIT]
        ]
        if isinstance(feature.get("dependencies"), list)
        else [],
        "branch_name": _text(feature.get("branchName")),
        "passes": feature.get("passes")
        if isinstance(feature.get("passes"), bool)
        else None,
        "created_at": _text(feature.get("created_at") or feature.get("createdAt"), 40),
    }
    if detailed:
        job["description"] = _text(feature.get("description"), DESCRIPTION_LIMIT)
        job["started_at"] = _text(feature.get("startedAt"), 40)
        job["error"] = _text(feature.get("error"), TITLE_LIMIT)
        job["summary"] = _text(feature.get("summary"), DESCRIPTION_LIMIT)
    return job


def job_summaries(payload: dict) -> dict:
    features = payload.get("features")
    items, truncated = _bounded_list(features if isinstance(features, list) else [])
    return {
        "jobs": [job_summary(item) for item in items if isinstance(item, dict)],
        "truncated": truncated,
    }


def running_agent_summaries(payload: dict) -> dict:
    agents = payload.get("runningAgents")
    items, truncated = _bounded_list(agents if isinstance(agents, list) else [])
    return {
        "running_agents": [
            {
                "feature_id": _text(item.get("featureId"), 200),
                "project_name": _text(item.get("projectName")),
                "project_path": _text(item.get("projectPath"), 1000),
                "is_auto_mode": item.get("isAutoMode") is True,
                "title": _text(item.get("title"), TITLE_LIMIT),
            }
            for item in items
            if isinstance(item, dict)
        ],
        "total_count": payload.get("totalCount")
        if isinstance(payload.get("totalCount"), int)
        else None,
        "truncated": truncated,
    }


def autonomy_summary(payload: dict) -> dict:
    summary = {
        "is_running": payload.get("isRunning") is True,
        "running_features": [
            _text(item, 200) for item in payload.get("runningFeatures", [])[:LIST_LIMIT]
        ]
        if isinstance(payload.get("runningFeatures"), list)
        else [],
        "running_count": payload.get("runningCount")
        if isinstance(payload.get("runningCount"), int)
        else None,
    }
    if "isAutoLoopRunning" in payload:
        summary["is_auto_loop_running"] = payload.get("isAutoLoopRunning") is True
    if isinstance(payload.get("maxConcurrency"), int):
        summary["max_concurrency"] = payload["maxConcurrency"]
    if isinstance(payload.get("activeAutoLoopProjects"), list):
        summary["active_auto_loop_projects"] = [
            _text(item, 1000) for item in payload["activeAutoLoopProjects"][:LIST_LIMIT]
        ]
    if isinstance(payload.get("activeAutoLoopWorktrees"), list):
        summary["active_auto_loop_worktrees"] = [
            _text(item, 1000)
            for item in payload["activeAutoLoopWorktrees"][:LIST_LIMIT]
        ]
    return summary


def backlog_summary(payload: dict) -> dict:
    saved_plan = payload.get("savedPlan")
    summary = {
        "is_running": payload.get("isRunning") is True,
        "has_saved_plan": isinstance(saved_plan, dict),
    }
    if isinstance(saved_plan, dict):
        result = saved_plan.get("result")
        summary["saved_plan"] = {
            "saved_at": _text(saved_plan.get("savedAt"), 40),
            "feature_count": len(result.get("modifications", []))
            if isinstance(result, dict)
            and isinstance(result.get("modifications"), list)
            else None,
        }
    return summary


def control_result(payload: dict, *, keys: tuple[str, ...]) -> dict:
    return {
        key: payload[key]
        for key in keys
        if key in payload and isinstance(payload[key], (bool, int, str))
    }
