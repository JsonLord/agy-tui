from __future__ import annotations

import os
from enum import Enum
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "http://127.0.0.1:3008"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_API_KEY_LENGTH = 256


class AutomakerError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ToolClass(str, Enum):
    READ = "READ"
    CONTROL = "CONTROL"


READ = ToolClass.READ
CONTROL = ToolClass.CONTROL

# Single authoritative classification. Mutability is decided here, never
# inferred from the HTTP verb (POST /api/auto-mode/status is semantically READ).
TOOL_CLASSES: dict[str, ToolClass] = {
    "automaker_health": READ,
    "automaker_projects": READ,
    "automaker_jobs": READ,
    "automaker_job_status": READ,
    "automaker_running_agents": READ,
    "automaker_autonomy_status": READ,
    "automaker_backlog_status": READ,
    "automaker_job_create": CONTROL,
    "automaker_job_update": CONTROL,
    "automaker_worker_start": CONTROL,
    "automaker_worker_stop": CONTROL,
}


def tool_class(name: str) -> ToolClass:
    return TOOL_CLASSES[name]


def is_mutating(name: str) -> bool:
    return TOOL_CLASSES[name] is CONTROL


def resolve_base_url(raw: str | None) -> str:
    """Normalize and fail closed: http scheme, loopback host, no credentials,
    no path/query/fragment. Remote/LAN/0.0.0.0 modes are not implemented."""
    value = (raw or "").strip() or DEFAULT_BASE_URL
    if "://" not in value:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED") from None
    if parts.scheme.casefold() != "http":
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    host = (parts.hostname or "").casefold()
    if host not in LOOPBACK_HOSTS:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    if "@" in (parts.netloc or ""):
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    if port is not None and not 1 <= port <= 65535:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    return value.rstrip("/")


def resolve_api_key(raw: str | None) -> str:
    key = (raw or "").strip()
    if not key or len(key) > MAX_API_KEY_LENGTH:
        raise AutomakerError("LOCAL_CONFIGURATION_REQUIRED")
    return key


def environment() -> tuple[str, str]:
    return (
        resolve_base_url(os.environ.get("AUTOMAKER_BASE_URL")),
        resolve_api_key(os.environ.get("AUTOMAKER_API_KEY")),
    )
