"""P19 — Release info (single source of truth for "which release is live").

Computes, for the CURRENT release checkout:
  * ``release_sha`` — the git HEAD of the release root (what is actually
    running), resolved from the checkout on disk (not from __file__, so a
    symlinked/copy release reports its true identity).
  * ``schema_version`` — the MCP tool schema version (bumped when the
    exposed tool surface changes).
  * ``capabilities`` — which production surfaces this release provides,
    each probed by a real import (a missing dep = capability absent, not
    an error).

No secrets are ever read or returned.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Bump when the MCP tool surface (agy-hybrid-mcp) changes shape.
SCHEMA_VERSION = "1.0"

DEFAULT_RELEASE_ROOT = Path("/home/leon/antigravity-hybrid")


def release_root() -> Path:
    """The release checkout root (env-overridable, canonical default)."""
    import os

    return Path(os.environ.get("AGY_RELEASE_ROOT", str(DEFAULT_RELEASE_ROOT)))


def _git_head(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _git_dirty(root: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return True


def _can_import(mod: str) -> bool:
    import importlib

    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class ReleaseInfo:
    release_sha: str
    dirty: bool
    schema_version: str
    root: str
    capabilities: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "release_sha": self.release_sha,
            "dirty": self.dirty,
            "schema_version": self.schema_version,
            "root": self.root,
            "capabilities": list(self.capabilities),
        }


def collect(root: Path | None = None) -> ReleaseInfo:
    """Probe the release and report its identity + capabilities.

    ``capabilities`` is derived from real imports so the report is honest:
    a surface whose dependency is absent is listed as ABSENT, not as a
    crash. Never raises, never reads credentials.
    """
    root = root or release_root()
    caps: list[str] = []

    # The P18 production surfaces (deterministic; no heavy deps).
    if _can_import("app.orchestration.project_supervisor"):
        caps.append("project-supervisor")
    if _can_import("app.human_surface"):
        caps.append("human-surface")
    if _can_import("app.orchestration.checkpoint_planning.checkpoint_viewer"):
        caps.append("checkpoint-authority")
    if _can_import("app.orchestration.cloud_batch.planner"):
        caps.append("cloud-planner")
    # The tool-free transport needs google.genai; the legacy agentic path
    # needs google.antigravity. Report each honestly.
    if _can_import("google.genai"):
        caps.append("cloud-planner:tool-free(genai)")
    else:
        caps.append("cloud-planner:tool-free(genai):UNAVAILABLE")
    if _can_import("google.antigravity"):
        caps.append("cloud-planner:cli(antigravity):restricted-B")
    # Worker backend.
    if _can_import("app.integrations.external_stack"):
        caps.append("workers:opencode")
    else:
        caps.append("workers:opencode:UNAVAILABLE")

    return ReleaseInfo(
        release_sha=_git_head(root),
        dirty=_git_dirty(root),
        schema_version=SCHEMA_VERSION,
        root=str(root),
        capabilities=tuple(caps),
    )


def render(root: Path | None = None) -> str:
    info = collect(root)
    lines = [
        f"release_sha: {info.release_sha}",
        f"dirty:       {info.dirty}",
        f"schema:      {info.schema_version}",
        f"root:        {info.root}",
        "capabilities:",
    ]
    lines += [f"  - {c}" for c in info.capabilities]
    return "\n".join(lines)


__all__ = [
    "SCHEMA_VERSION",
    "ReleaseInfo",
    "collect",
    "release_root",
    "render",
]
