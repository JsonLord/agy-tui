from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

# Repository root (this file lives at app/investigation/graph_manager.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

GRAPHIFY_BIN = Path(
    os.getenv(
        "GRAPHIFY_BIN",
        "/home/leon/.local/bin/graphify",
    )
)

# Canonical env file carrying Graphify's backend configuration
GRAPHIFY_ENV_FILE = (
    Path.home() / ".config" / "graphify.env"
)
STATE_ROOT = (
    PROJECT_ROOT / "state" / "repositories"
)


def graphify_env() -> dict[str, str]:
    """
    Build the environment used for every Graphify subprocess.

    ~/.config/graphify.env is the canonical source for
    Graphify's OpenAI-compatible backend configuration.
    """
    env = os.environ.copy()

    if not GRAPHIFY_ENV_FILE.exists():
        return env

    for raw in GRAPHIFY_ENV_FILE.read_text().splitlines():
        line = raw.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line[len("export "):]

        if "=" not in line:
            continue

        key, value = line.split("=", 1)

        key = key.strip()
        value = value.strip()

        # strip matching surrounding quotes
        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] in {'"', "'"}:
                value = value[1:-1]

        if not key:
            continue
        env[key] = value

    return env



def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=graphify_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def repo_id(repo: Path) -> str:
    repo = repo.expanduser().resolve()

    return hashlib.sha256(
        str(repo).encode()
    ).hexdigest()[None:16]


def repository_state_dir(repo: Path) -> Path:
    path = STATE_ROOT / repo_id(repo)
    path.mkdir(
        parents=True,
        exist_ok=True,
    )
    return path


def _is_git_repo(repo: Path) -> bool:
    result = _run(
        [
            "git",
            "rev-parse",
            "--is-inside-work-tree",
        ],

        cwd=repo,
        timeout=10,
    )
    return (
        result.returncode == 0
        and result.stdout.strip() == "true"
    )


def _git_fingerprint(repo: Path) -> str:
    h = hashlib.sha256()

    head = _run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        timeout=10,
    )

    h.update(
        (
            head.stdout.strip()
            if head.returncode == 0
            else "NO_HEAD"
        ).encode()
    )

    diff = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            "HEAD",
            "--",
        ],
        cwd=repo,
        env=graphify_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )

    h.update(diff.stdout)

    untracked = _run(

        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ],

        cwd=repo,
        timeout=30,
    )

    for rel in untracked.stdout.split("\x00"):
        if not rel:
            continue

        # Graphify's own output must not feed back into
        # the fingerprint of the repository it indexes
        if rel == "graphify-out" or rel.startswith(
            "graphify-out/"
        ):
            continue

        path = repo / rel

        h.update(rel.encode())

        try:
            stat = path.stat()

            h.update(
                str(stat.st_size).encode(),
            )
            h.update(
                str(stat.st_mtime_ns).encode(),
            )

            # content of small files is part of identity;
            # large files only contribute metadata
            if path.is_file():
                if stat.st_size <= 2000000:
                    h.update(path.read_bytes())

        except OSError:
            continue

    return h.hexdigest()

def _filesystem_fingerprint(repo: Path) -> str:
    h = hashlib.sha256()

    for path in sorted(repo.rglob("*")):
        try:
            rel = path.relative_to(repo)
        except ValueError:
            continue

        if ".git" in rel.parts:
            continue
        if "graphify-out" in rel.parts:
            continue

        if not path.is_file():
            continue

        try:
            stat = path.stat()
        except OSError:
            continue

        h.update(str(rel).encode())
        h.update(str(stat.st_size).encode())
        h.update(str(stat.st_mtime_ns).encode())

    return h.hexdigest()



def repository_fingerprint(repo: Path) -> str:
    repo = repo.expanduser().resolve()
    if _is_git_repo(repo):
        return _git_fingerprint(repo)

    return _filesystem_fingerprint(repo)



def ensure_graph(repo: Path) -> dict[str, Any]:
    """
    Ensure Graphify's graph exists and matches the current
    repository state.

    Models never decide whether rebuilding is necessary.
    """
    repo = repo.expanduser().resolve()

    if not repo.is_dir():
        raise ValueError(
            f"Repository does not exist: {repo}"
        )

    if not GRAPHIFY_BIN.exists():
        raise RuntimeError(
            f"Graphify executable not found: {GRAPHIFY_BIN}"
        )

    graph_path = (
        repo / "graphify-out" / "graph.json"
    )

    state_dir = repository_state_dir(repo)
    state_file = state_dir / "graph-state.json"

    fingerprint = repository_fingerprint(repo)

    previous: dict[str, Any] = {}

    if state_file.exists():
        try:
            previous = json.loads(
                state_file.read_text()
            )

        except Exception:
            previous = {}

    # reuse only when the current fingerprint still matches
    if (
        graph_path.exists()
        and previous.get("fingerprint") == fingerprint
    ):
        return {
            "status": "reused",
            "repo": str(repo),
            "graph": str(graph_path),
            "fingerprint": fingerprint,
        }

    started = time.time()

    result = _run(
        [str(GRAPHIFY_BIN), "."],
        cwd=repo,
        timeout=300,
    )

    log = (
        (result.stdout or "")
        + (
            "\n"
            if result.stdout and result.stderr
            else ""
        )
        + (result.stderr or "")
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Graphify extraction failed:\n"
            + log[-5000:]
        )

    if not graph_path.exists():
        raise RuntimeError(
            "Graphify returned successfully but graphify-out/graph.json is missing."
        )

    # recompute after extraction; graphify-out is excluded
    # from the fingerprint, so this stays stable for reuse
    # checks on unchanged repositories
    fingerprint = repository_fingerprint(repo)

    state = {
        "repo": str(repo),
        "graph": str(graph_path),
        "fingerprint": fingerprint,
        "generated_at": time.time(),
        "generation_seconds": round(
            time.time() - started,
            3,
        ),
    }

    state_file.write_text(
        json.dumps(state, indent=2) + "\n"
    )

    # keep the raw extractor output for local debugging
    (state_dir / "last-extract.log").write_text(
        log
    )

    return {
        "status": "rebuilt",
    } | state


def query_graph(repo: Path, query: str) -> dict[str, Any]:
    """
    Query the local Graphify graph.

    Full raw output remains local. A parsed structural form is
    returned for consumption by local models.
    """
    # keep every entry point on a resolved absolute path
    # so state lookups and subprocess cwd always agree
    repo = repo.expanduser().resolve()

    graph_state = ensure_graph(repo)

    result = _run(

        [
            str(GRAPHIFY_BIN),
            "query",
            query,
        ],

        cwd=repo,
        timeout=90,
    )

    raw = (
        (result.stdout or "")
        + (
            "\n"
            if result.stdout and result.stderr
            else ""
        )
        + (result.stderr or "")
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Graphify query failed: {query}\n"
            + raw[-4000:]
        )

    files: list[str] = []
    nodes: list[str] = []
    relationships: list[str] = []
    def add_unique(target: list[str], value: str) -> None:
        # Graphify repeats entries across traversal
        # branches of the query result; keep only the
        # first occurrence of every item.
        if value and value not in target:
            target.append(value)

    import re

    for raw_line in raw.splitlines():
        line = raw_line.strip()

        if line.startswith("NODE "):
            item = line[5:]

            add_unique(nodes, item)

            match = re.search(
                r"\[src=([^\s\]]+)",
                item,
            )

            if match:
                add_unique(
                    files,
                    match.group(1),
                )

        elif line.startswith("EDGE "):
            add_unique(
                relationships,
                line[5:],
            )

    return {
        "query": query,
        "graph_status": graph_state["status"],
        "files": files,
        "nodes": nodes,
        "relationships": relationships,
        "raw": raw,
    }
