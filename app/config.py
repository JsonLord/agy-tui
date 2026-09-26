import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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


# --- AGY (standalone development harness) configuration --------------------
#
# AGY runs on the Helmholtz OpenAI-compatible provider:
#   supervisor role -> SUPERVISOR_MODEL (plan/stage, gates, acceptance)
#   dispatch role   -> DISPATCH_MODEL   (bounded one-stage implementation)
# Tool routing is optional and served by Needle3; all values stay
# model-neutral and provider-neutral elsewhere in the code.

DEFAULT_HELMHOLTZ_BASE_URL = "http://100.98.105.55:5173/v1"
DEFAULT_NEEDLE_BASE_URL = "https://leon4gr45-needle.hf.space"
DEFAULT_SUPERVISOR_MODEL = "alias-mimo-v2.6-pro"
DEFAULT_DISPATCH_MODEL = "alias-mimo-v2.6-flash"

# P3 failover binding: each role has ONE fallback model (verbatim
# from the failover architecture contract). Env-configurable, the
# defaults are fixed and match the agy-*-fallback OpenCode agents.
DEFAULT_SUPERVISOR_FALLBACK_MODEL = "alias-qwen3.8-27b"
DEFAULT_DISPATCH_FALLBACK_MODEL = "alias-qwen3.8-flash-next"

ENV_SUPERVISOR_MODEL = "SUPERVISOR_MODEL"
ENV_DISPATCH_MODEL = "DISPATCH_MODEL"
ENV_SUPERVISOR_FALLBACK_MODEL = "AGY_SUPERVISOR_FALLBACK_MODEL"
ENV_DISPATCH_FALLBACK_MODEL = "AGY_DISPATCH_FALLBACK_MODEL"
ENV_PROVIDER_BASE_URL = "AGY_PROVIDER_BASE_URL"
ENV_PROVIDER_API_KEY = "AGY_PROVIDER_API_KEY"
ENV_NEEDLE_BASE_URL = "NEEDLE_BASE_URL"
ENV_NEEDLE_API_KEY = "NEEDLE_API_KEY"
ENV_NEEDLE_ROUTING_ENABLED = "NEEDLE_ROUTING_ENABLED"
ENV_SUPERVISOR_STALL_SECONDS = "SUPERVISOR_STALL_SECONDS"
ENV_SUPERVISOR_REPEAT_LIMIT = "SUPERVISOR_REPEAT_LIMIT"
ENV_SUPERVISOR_MAX_RESTARTS = "SUPERVISOR_MAX_RESTARTS"


def _resolve_env(
    env: Mapping[str, str] | None,
) -> Mapping[str, str]:
    if env is None:
        load_env()
        return os.environ

    return env


def _env_int(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = env.get(key)

    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}")

    if value < minimum:
        raise ValueError(
            f"{key} must be >= {minimum}, got {value}"
        )

    return value


def _env_bool(
    env: Mapping[str, str],
    key: str,
    default: bool,
) -> bool:
    raw = env.get(key)

    if raw is None or raw.strip() == "":
        return default

    normalized = raw.strip().lower()

    if normalized in ("1", "true", "yes", "on"):
        return True

    if normalized in ("0", "false", "no", "off"):
        return False

    raise ValueError(f"{key} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class SupervisorLifecycle:
    """
    opencode-supervise lifecycle values for AGY dispatch.

    AGY defaults favor slow, legitimate generations: the 120s
    factory default false-stalled long thinking passes, so the AGY
    stall window starts at 900s. Configurable, never hard-coded at
    call sites.
    """

    stall_seconds: int = 900
    repeat_limit: int = 3
    max_restarts: int = 1

    def __post_init__(self) -> None:
        if self.stall_seconds < 1:
            raise ValueError("stall_seconds must be >= 1")

        if self.repeat_limit < 0:
            raise ValueError("repeat_limit must be >= 0")

        if self.max_restarts < 0:
            raise ValueError("max_restarts must be >= 0")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "SupervisorLifecycle":
        env = _resolve_env(env)

        return cls(
            stall_seconds=_env_int(
                env,
                ENV_SUPERVISOR_STALL_SECONDS,
                900,
                minimum=1,
            ),
            repeat_limit=_env_int(
                env,
                ENV_SUPERVISOR_REPEAT_LIMIT,
                3,
                minimum=0,
            ),
            max_restarts=_env_int(
                env,
                ENV_SUPERVISOR_MAX_RESTARTS,
                1,
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class NeedleSettings:
    """Needle3 tool-routing endpoint (optional seam)."""

    base_url: str = DEFAULT_NEEDLE_BASE_URL
    api_key: str | None = None
    enabled: bool = True

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "NeedleSettings":
        env = _resolve_env(env)

        return cls(
            base_url=(
                env.get(ENV_NEEDLE_BASE_URL, "").strip()
                or DEFAULT_NEEDLE_BASE_URL
            ),
            api_key=env.get(ENV_NEEDLE_API_KEY, "").strip() or None,
            enabled=_env_bool(
                env,
                ENV_NEEDLE_ROUTING_ENABLED,
                True,
            ),
        )


@dataclass(frozen=True)
class AgySettings:
    """
    Model/provider configuration for the standalone AGY
    large-development harness.

    Binds the Helmholtz supervisor/dispatch models and the
    supervisor lifecycle values. Carries no Hermes, Gemma, or
    MiniCPM assumptions.
    """

    provider_base_url: str
    supervisor_model: str
    dispatch_model: str
    provider_api_key: str | None
    lifecycle: SupervisorLifecycle
    needle: NeedleSettings
    supervisor_fallback_model: str = DEFAULT_SUPERVISOR_FALLBACK_MODEL
    dispatch_fallback_model: str = DEFAULT_DISPATCH_FALLBACK_MODEL

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "AgySettings":
        env = _resolve_env(env)

        return cls(
            provider_base_url=(
                env.get(ENV_PROVIDER_BASE_URL, "").strip()
                or DEFAULT_HELMHOLTZ_BASE_URL
            ),
            supervisor_model=(
                env.get(ENV_SUPERVISOR_MODEL, "").strip()
                or DEFAULT_SUPERVISOR_MODEL
            ),
            dispatch_model=(
                env.get(ENV_DISPATCH_MODEL, "").strip()
                or DEFAULT_DISPATCH_MODEL
            ),
            provider_api_key=(
                env.get(ENV_PROVIDER_API_KEY, "").strip() or None
            ),
            lifecycle=SupervisorLifecycle.from_env(env),
            needle=NeedleSettings.from_env(env),
            supervisor_fallback_model=(
                env.get(ENV_SUPERVISOR_FALLBACK_MODEL, "").strip()
                or DEFAULT_SUPERVISOR_FALLBACK_MODEL
            ),
            dispatch_fallback_model=(
                env.get(ENV_DISPATCH_FALLBACK_MODEL, "").strip()
                or DEFAULT_DISPATCH_FALLBACK_MODEL
            ),
        )