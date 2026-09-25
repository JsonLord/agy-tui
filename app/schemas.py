from typing import Literal

from pydantic import BaseModel, Field


class WorkPlan(BaseModel):
    goal: str

    files: list[str] = Field(default_factory=list)

    steps: list[str]

    constraints: list[str] = Field(default_factory=list)

    verification: list[str] = Field(default_factory=list)

    risk: Literal["low", "medium", "high"] = "medium"

    worker_instruction: str
