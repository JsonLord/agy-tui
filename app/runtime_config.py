"""P20 — Portability configuration (single source of truth for runtime
paths).

Every runtime path that was previously host-specific now resolves through
this module: environment variable with a sensible local default. The
defaults preserve THIS host's behavior exactly (the accepted release keeps
working unchanged); a container overrides them.

    AGY_STATE_ROOT          durable state (default: <repo>/state)
    AGY_RELEASE_ROOT        release checkout (default: <repo>)
    AGY_CONFIG_ROOT         AGY CLI profile/config (default: ~/.gemini/antigravity-cli)
    OPENCODE_SERVER_URL     worker backend (default: http://127.0.0.1:4096)
    OPENCODE_SUPERVISE_PATH opencode-supervise binary (default: <repo>/scripts)
    AGY_RELEASE_LINK        release symlink (default: /home/leon/antigravity-current)

Secrets are NEVER read here — credentials live in the environment/secret
store (Helmholtz, GitHub, cloud planner, providers), injected per P20-E.
"""
from __future__ import annotations

import os
from pathlib import Path

# <repo> = the directory containing the top-level ``app`` package — this
# file lives at <repo>/app/runtime_config.py, so repo root is the parent
# of the app package. Never an absolute /home/leon path.
_APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = _APP_DIR.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


# --------------------------------------------------------------------------
# state / release / config roots
# --------------------------------------------------------------------------

def state_root() -> Path:
    """Canonical durable state root (projects, plans, runs, human
    decisions, planner requests, continuations, batch state, events)."""
    return Path(_env("AGY_STATE_ROOT", str(REPO_ROOT / "state")))


def release_root() -> Path:
    """The accepted release checkout this process runs from."""
    return Path(_env("AGY_RELEASE_ROOT", str(REPO_ROOT)))


def release_link() -> Path:
    """The atomic release symlink (P19)."""
    return Path(_env("AGY_RELEASE_LINK", "/home/leon/antigravity-current"))


def config_root() -> Path:
    """AGY CLI profile/config root (settings.json, brain, OAuth)."""
    return Path(_env("AGY_CONFIG_ROOT",
                     str(Path.home() / ".gemini" / "antigravity-cli")))


# --------------------------------------------------------------------------
# worker backend (OpenCode)
# --------------------------------------------------------------------------

def opencode_server_url() -> str:
    """The coding-worker backend HTTP endpoint."""
    return _env("OPENCODE_SERVER_URL", "http://127.0.0.1:4096")


def opencode_supervise_path() -> Path:
    """Directory holding the opencode-supervise helper."""
    return Path(_env("OPENCODE_SUPERVISE_PATH", str(REPO_ROOT / "scripts")))


def _which_or(name: str, default: str) -> Path:
    """Resolve a helper binary: explicit env override wins, else PATH,
    else the given host default (preserves local behavior)."""
    import shutil

    explicit = os.environ.get(name)
    if explicit:
        return Path(explicit)
    found = shutil.which(name if "/" not in name else name.rsplit("/", 1)[-1])
    if found:
        return Path(found)
    return Path(default)


def graphify_bin() -> Path:
    return _which_or("GRAPHIFY_BIN", "/home/leon/.local/bin/graphify")


def graphify_ensure_bin() -> Path:
    return _which_or("GRAPHIFY_ENSURE_BIN", "/home/leon/.local/bin/graphify-ensure")


def opencode_supervise_bin() -> Path:
    return _which_or(
        "OPENCODE_SUPERVISE_BIN",
        str(opencode_supervise_path() / "opencode-supervise"),
    )


def gx10_bin() -> Path:
    return _which_or("GX10_BIN", "/home/leon/bin/mcp-gx10-filesystem")


def ag_gh_bin() -> str:
    """GitHub wrapper for the Jules dedicated identity (Jules automation)."""
    return str(_which_or("AG_GH_BIN", "/home/leon/.local/bin/ag-gh"))


def ag_git_bin() -> str:
    return str(_which_or("AG_GIT_BIN", "/home/leon/.local/bin/ag-git"))


# --------------------------------------------------------------------------
# capabilities (honest probes, no secrets)
# --------------------------------------------------------------------------

def opencode_reachable(timeout: float = 5.0) -> bool:
    """Is the worker backend reachable? (deterministic probe, no secrets)"""
    import urllib.request

    try:
        req = urllib.request.Request(opencode_server_url() + "/session")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


def cloud_planner_configured() -> bool:
    """Is the tool-free cloud planner usable on this host?

    The P17 transport is google-genai; it needs GEMINI_API_KEY or a Vertex
    (GOOGLE_CLOUD_PROJECT + GOOGLE_GENAI_USE_VERTEXAI) or ADC. Report
    ABSENT honestly rather than assume.

    The ADC fallback probe hits the GCE metadata server, which HANGS in a
    non-GCE container (no local timeout — OS-level). Set
    AGY_SKIP_ADC_PROBE=1 (the P20 entrypoint always does) in containerized
    environments; the env-var checks above remain authoritative there."""
    import os

    if os.environ.get("GEMINI_API_KEY"):
        return True
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI") == "true" and os.environ.get(
        "GOOGLE_CLOUD_PROJECT"
    ):
        return True
    if os.environ.get("AGY_SKIP_ADC_PROBE") == "1":
        # Containerized / non-GCE: the metadata-server probe would hang.
        # No local credentials file is a definitive "absent" there.
        return False
    try:
        from google.auth import default

        creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return creds is not None
    except Exception:
        return False
