import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    path = path or ROOT / ".env"

    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    planner_base_url: str
    planner_model: str

    worker_base_url: str
    worker_model: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()

        return cls(
            planner_base_url=os.environ["PLANNER_BASE_URL"],
            planner_model=os.environ["PLANNER_MODEL"],
            worker_base_url=os.environ["WORKER_BASE_URL"],
            worker_model=os.environ["WORKER_MODEL"],
        )
