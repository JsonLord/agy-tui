from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)


class WorkPlan(BaseModel):
    goal: str

    files: list[str] = Field(default_factory=list)

    steps: list[str]

    constraints: list[str] = Field(default_factory=list)

    verification: list[str] = Field(default_factory=list)

    risk: Literal["low", "medium", "high"] = "medium"

    worker_instruction: str


# ---------------------------------------------------------------------------
# Bounded worker execution contract
# ---------------------------------------------------------------------------


class TaskVerification(BaseModel):
    """One deterministic, mechanically checkable verification command."""

    command: str = Field(min_length=1)

    expected_exit_code: int = 0

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("verification.command must not be blank")
        return value


def _repo_relative(value: str, field_name: str) -> str:
    value = value.strip()

    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if value.startswith("/") or value.startswith("~/"):
        raise ValueError(
            f"{field_name} must be repo-relative, got absolute path: "
            f"{value!r}"
        )
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise ValueError(
            f"{field_name} must not escape the repository: {value!r}"
        )
    return value


def _ensure_unique_case_insensitive(
    values: list[str],
    field_name: str,
) -> None:
    lowered = [value.lower() for value in values]
    duplicates = sorted(
        {
            original
            for original, folded in zip(values, lowered)
            if lowered.count(folded) > 1
        }
    )
    if duplicates:
        raise ValueError(
            f"{field_name} must be unique, duplicates: "
            + ", ".join(duplicates)
        )


class WorkerTask(BaseModel):
    """
    One bounded implementation objective derived from a WorkPlan.

    A WorkPlan may legitimately span many files and stages; a
    WorkerTask may not. It is the unit actually handed to the local
    coding worker: one goal, at most 3 files, 1-5 atomic steps, one
    deterministic verification command, explicit dependency IDs.
    """

    id: str = Field(min_length=1)

    goal: str = Field(min_length=1, max_length=400)

    files: list[str] = Field(default_factory=list, max_length=3)

    steps: list[str] = Field(min_length=1, max_length=5)

    constraints: list[str] = Field(default_factory=list)

    verification: TaskVerification

    worker_instruction: str = Field(min_length=1)

    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("id must not be blank")
        return value

    @field_validator("goal", "worker_instruction")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("files")
    @classmethod
    def _files_repo_relative(cls, value: list[str]) -> list[str]:
        cleaned = [_repo_relative(item, "files[]") for item in value]
        _ensure_unique_case_insensitive(cleaned, "files")
        return cleaned

    @field_validator("steps")
    @classmethod
    def _steps_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("steps must not contain blanks")
            cleaned.append(item)
        return cleaned

    @field_validator("depends_on")
    @classmethod
    def _depends_unique(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("depends_on must not contain blanks")
            cleaned.append(item)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("depends_on must be unique")
        return cleaned


# ---------------------------------------------------------------------------
# Staged development plan (AGY P2)
# ---------------------------------------------------------------------------


class PlanDependencyError(RuntimeError):
    """
    A DevelopmentPlan stage graph could not be ordered.

    The model validator rejects cycles at construction time; this
    error is the safety net for programmatically constructed plans
    (``DevelopmentPlan.model_construct``) whose graph is unorderable.
    """


class AcceptanceCommand(BaseModel):
    """
    One deterministic, mechanically runnable check at stage or plan
    level.

    Companion to ``TaskVerification`` (the per-task check): this is
    the unit the stage/plan acceptance gate runs verbatim — command
    string and expected exit code are carried through decomposition
    unchanged.
    """

    command: str = Field(min_length=1)

    expected_exit_code: int = 0

    description: str = ""

    @field_validator("command")
    @classmethod
    def _command_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("acceptance.command must not be blank")
        return value


class PlanStage(BaseModel):
    """
    One stage of a development plan: a single bounded unit of change
    with its own scope, context and deterministic acceptance checks.

    The stage DAG (``dependencies``) is validated by
    ``DevelopmentPlan``; ``allowed_files`` is stage scope and may
    exceed the 3-file bounded-task scope — decomposition performs that
    split (see ``app.orchestration.plan_decompose``).
    """

    stage_id: str = Field(min_length=1)

    objective: str = Field(min_length=1, max_length=400)

    dependencies: list[str]

    allowed_files: list[str]

    implementation_context: str = Field(min_length=1)

    acceptance_commands: list[AcceptanceCommand]

    acceptance_conditions: list[str]

    cumulative_invariants: list[str] = Field(default_factory=list)

    @field_validator("stage_id")
    @classmethod
    def _stage_id_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("stage_id must not be blank")
        return value

    @field_validator("objective", "implementation_context")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("dependencies")
    @classmethod
    def _dependencies_unique(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("dependencies must not contain blanks")
            cleaned.append(item)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("dependencies must be unique")
        return cleaned

    @field_validator("allowed_files")
    @classmethod
    def _allowed_files_repo_relative(
        cls,
        value: list[str],
    ) -> list[str]:
        cleaned = [
            _repo_relative(item, "allowed_files[]") for item in value
        ]
        _ensure_unique_case_insensitive(cleaned, "allowed_files")
        return cleaned

    @field_validator("acceptance_conditions", "cumulative_invariants")
    @classmethod
    def _items_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            item = item.strip()
            if not item:
                raise ValueError("must not contain blanks")
            cleaned.append(item)
        return cleaned


class DevelopmentPlan(BaseModel):
    """
    A staged development plan: at least one ``PlanStage`` plus the
    deterministic checks that accept the plan as a whole.

    The stage dependency graph must be a DAG: stage IDs are unique,
    every dependency names an existing stage, no stage depends on
    itself, and no cycle exists. Violations raise ``ValueError`` via
    the model validator (pydantic reports them as ``ValidationError``).
    """

    plan_id: str = Field(min_length=1)

    objective: str = Field(min_length=1)

    stages: list[PlanStage] = Field(min_length=1)

    final_acceptance: list[AcceptanceCommand] = Field(min_length=1)

    @field_validator("plan_id", "objective")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_stage_graph(self) -> "DevelopmentPlan":
        """
        Deterministically reject a structurally invalid stage DAG:
        duplicate stage IDs, unknown dependencies, self-dependencies
        and dependency cycles (in that order).
        """
        stage_ids = [stage.stage_id for stage in self.stages]

        duplicates = sorted(
            {sid for sid in stage_ids if stage_ids.count(sid) > 1}
        )
        if duplicates:
            raise ValueError(
                "duplicate stage ids: " + ", ".join(duplicates)
            )

        known = set(stage_ids)
        for stage in self.stages:
            for dependency in stage.dependencies:
                if dependency == stage.stage_id:
                    raise ValueError(
                        f"stage {stage.stage_id!r} depends on itself"
                    )
                if dependency not in known:
                    raise ValueError(
                        f"stage {stage.stage_id!r} depends on unknown "
                        f"stage {dependency!r}"
                    )

        try:
            self.stage_execution_order()

        except PlanDependencyError as exc:
            raise ValueError(str(exc)) from exc

        return self

    def stage_execution_order(self) -> list[PlanStage]:
        """
        Deterministic topological order of the stages.

        Kahn's algorithm with a frontier seeded in input order: among
        ready stages the input order always wins, so equivalent
        inputs yield the same order. Pure — the plan is never
        mutated.

        ``ValueError`` above already rejects cycles for validated
        plans; raising ``PlanDependencyError`` here is the safety net
        for programmatically constructed plans.
        """
        ordered: list[PlanStage] = []
        resolved: set[str] = set()
        pending = list(self.stages)

        while pending:
            ready = [
                stage
                for stage in pending
                if all(dep in resolved for dep in stage.dependencies)
            ]

            if not ready:
                unfinished = ", ".join(
                    stage.stage_id for stage in pending
                )
                raise PlanDependencyError(
                    f"dependency cycle among stages: {unfinished}"
                )

            for stage in ready:
                ordered.append(stage)
                resolved.add(stage.stage_id)

            pending = [
                stage
                for stage in pending
                if stage.stage_id not in resolved
            ]

        return ordered
