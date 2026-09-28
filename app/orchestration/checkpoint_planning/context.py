"""P8 planning context services — BOUNDED, read-only repo introspection.

These functions are the only repository view the cloud planner (Gemini
inside the AGY CLI, or the MiMo Pro fallback) gets before it plans. Hard
rules encoded here:

* NO file dumps. Every return value is size-bounded (character caps,
  capped list lengths). The planner gets identity, presence flags and
  short excerpts — never repository content at large.
* NO mutation. Plain reads only; no subprocess, no git commands, no
  writes. (git metadata is read from ``.git/HEAD`` + packed refs files,
  never by shelling out.)

Three surfaces, per the P8 contract:

``agy_project_inspect(repo)``
    bounded project identity: name, branch, head sha, primary language,
    manifests, test-command hints, design/learnings/graphify/serena
    presence, AGY run count (``state/runs`` directory only).
``agy_design_context(repo, query, max_chars)``
    targeted keyword excerpts from ``design.md`` / ``docs/**/*.md`` /
    prototype paths; ``{found, excerpts, truncated}``.
``agy_learnings_read(repo, query, max_chars)``
    learnings.md contract: ``##``-header sections are topics; keyword
    matched, bounded; ``{topics, present}``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# bounds (all outputs are hard-capped; contract: bounded outputs, no dumps)
# ---------------------------------------------------------------------------

MAX_EXCERPT_CHARS = 8000
MAX_LEARNINGS_CHARS = 6000
MAX_TOPIC_CHARS = 1200
MAX_FILES_LISTED = 25
MAX_EXCERPTS = 12
MAX_LANGUAGE_SCAN_FILES = 400

_MANIFEST_NAMES = (
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "Makefile",
)

# extension -> primary language label (first hit wins, ordered scan)
_EXT_LANGUAGES = (
    (".py", "python"),
    (".ts", "typescript"),
    (".tsx", "typescript"),
    (".js", "javascript"),
    (".jsx", "javascript"),
    (".go", "go"),
    (".rs", "rust"),
    (".rb", "ruby"),
    (".java", "java"),
    (".php", "php"),
    (".c", "c"),
    (".cpp", "cpp"),
    (".cs", "csharp"),
)

_SKIP_SCAN_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".serena",
    "graphify-out",
    "state",
    ".next",
    "target",
}

_DESIGN_CANDIDATES = ("design.md", "DESIGN.md", "docs/design.md")
_PROTOTYPE_DIR_NAMES = ("prototype", "prototypes", "mockups", "ui")
_LEARNINGS_CANDIDATES = ("learnings.md", "LEARNINGS.md", "docs/learnings.md")


def _resolve_repo(repo: Path | str) -> Path:
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Repository does not exist: {path}")
    return path


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _read_bounded(path: Path, max_chars: int) -> tuple[str, bool]:
    """Read at most ``max_chars`` from a file; return (text, truncated)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    if len(raw) > max_chars:
        return raw[:max_chars], True
    return raw, False


# ---------------------------------------------------------------------------
# git identity WITHOUT shelling out (read .git/HEAD; tolerate worktrees)
# ---------------------------------------------------------------------------

def _git_dir(root: Path) -> Path | None:
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        # worktree: ".git" file with "gitdir: <path>"
        try:
            text = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            pointer = Path(text.split(":", 1)[1].strip())
            resolved = (root / pointer).resolve() if not pointer.is_absolute() else pointer
            if resolved.is_dir():
                return resolved
    return None


def _git_identity(root: Path) -> dict[str, str | None]:
    """``{branch, head_sha}`` read from git files only — never git run."""
    branch: str | None = None
    sha: str | None = None

    git_dir = _git_dir(root)
    if git_dir is None:
        return {"branch": None, "head_sha": None}

    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {"branch": None, "head_sha": None}

    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref.rsplit("/", 1)[-1] if ref else None
        # loose object first, then packed-refs
        loose = git_dir / ref
        try:
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8").strip() or None
        except OSError:
            sha = None
        if sha is None:
            try:
                packed = (git_dir / "packed-refs").read_text(encoding="utf-8")
                for line in packed.splitlines():
                    line = line.strip()
                    if line.endswith(ref) and " " in line:
                        sha = line.split(" ", 1)[0]
                        break
            except OSError:
                pass
    elif re.fullmatch(r"[0-9a-f]{7,40}", head):
        sha = head  # detached HEAD

    return {"branch": branch, "head_sha": sha}


# ---------------------------------------------------------------------------
# language + test hints
# ---------------------------------------------------------------------------

def _primary_language(root: Path) -> str | None:
    counts: dict[str, int] = {}
    scanned = 0

    for path in root.rglob("*"):
        if scanned >= MAX_LANGUAGE_SCAN_FILES:
            break
        if not path.is_file():
            continue
        if any(part in _SKIP_SCAN_DIRS for part in path.parts):
            continue
        try:
            if path.stat().st_size > 512 * 1024:
                continue
        except OSError:
            continue
        scanned += 1
        suffix = path.suffix.lower()
        for ext, lang in _EXT_LANGUAGES:
            if suffix == ext:
                counts[lang] = counts.get(lang, 0) + 1
                break

    if not counts:
        return None
    # deterministic: highest count, tie broken by label sort
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _test_hints(root: Path) -> list[str]:
    hints: list[str] = []
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        hints.append("pytest")
    if (root / "tests").is_dir():
        hints.append("tests/ directory present")
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text, _ = _read_bounded(pyproject, 4000)
        if "[tool.pytest" in text:
            hints.append("pytest (pyproject [tool.pytest])")
        if "[tool.poetry.scripts]" in text or "[project.scripts]" in text:
            hints.append("project scripts declared")
    package_json = root / "package.json"
    if package_json.is_file():
        text, _ = _read_bounded(package_json, 4000)
        try:
            data = json.loads(text)
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            for name in ("test", "lint", "build"):
                if name in scripts:
                    hints.append(f"npm {name}")
        except json.JSONDecodeError:
            pass
    if (root / "Makefile").is_file():
        text, _ = _read_bounded(root / "Makefile", 2000)
        if re.search(r"^test:", text, re.MULTILINE):
            hints.append("make test")
    return hints[:MAX_FILES_LISTED]


def _serena_configured(root: Path) -> bool:
    serena = root / ".serena"
    if not serena.is_dir():
        return False
    try:
        return any(
            p.name == "project.yml" or p.suffix == ".yml"
            for p in serena.iterdir()
            if p.is_file()
        )
    except OSError:
        return False


def _agy_run_count(root: Path) -> int:
    """Count entries under state/runs if the directory exists (read-only)."""
    runs = root / "state" / "runs"
    if not runs.is_dir():
        return 0
    try:
        return sum(1 for p in runs.iterdir() if p.is_dir())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# public API 1: agy_project_inspect
# ---------------------------------------------------------------------------

def agy_project_inspect(repo: Path) -> dict:
    """
    Bounded project identity for the planner. NO file contents — names,
    flags, and counts only. Raises ``ValueError`` for a missing dir.
    """
    root = _resolve_repo(repo)

    manifests = [name for name in _MANIFEST_NAMES if (root / name).exists()]

    return {
        "name": root.name,
        "path": str(root),
        **_git_identity(root),
        "primary_language": _primary_language(root),
        "manifests": manifests,
        "test_hints": _test_hints(root),
        "design_md_present": _first_existing(root, _DESIGN_CANDIDATES) is not None,
        "learnings_md_present": (
            _first_existing(root, _LEARNINGS_CANDIDATES) is not None
        ),
        "graphify_out_present": (root / "graphify-out").is_dir(),
        "serena_project_configured": _serena_configured(root),
        "agy_run_count": _agy_run_count(root),
    }


# ---------------------------------------------------------------------------
# public API 2: agy_design_context
# ---------------------------------------------------------------------------

def _keywords(query: str | None) -> list[str]:
    if not query:
        return []
    return [token for token in re.split(r"[^A-Za-z0-9_-]+", query.lower()) if token]

def _score_excerpt(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(word) for word in keywords)


def _collect_md_files(root: Path) -> list[Path]:
    files: list[Path] = []

    design = _first_existing(root, _DESIGN_CANDIDATES)
    if design is not None:
        files.append(design)

    docs = root / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*.md")):
            if path.is_file() and path not in files:
                files.append(path)
            if len(files) >= MAX_EXCERPTS * 2:
                break

    for name in _PROTOTYPE_DIR_NAMES:
        proto = root / name
        if proto.is_dir():
            files.extend(p for p in sorted(proto.rglob("*")) if p.is_file())
            break

    return files[: MAX_EXCERPTS * 2]


def agy_design_context(
    repo: Path,
    query: str | None = None,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> dict:
    """
    Bounded, query-targeted design excerpts: ``design.md``,
    ``docs/**/*.md`` (first matches), prototype paths. Never more than
    ``max_chars`` total excerpt text; ``truncated`` says when caps bit.
    """
    root = _resolve_repo(repo)
    cap = max(0, int(max_chars))
    keywords = _keywords(query)

    found: list[str] = []
    excerpts: list[dict[str, Any]] = []
    used = 0
    truncated = False

    candidates = _collect_md_files(root)

    # When a query is given, rank candidate files by keyword hits so the
    # most relevant excerpts survive the character budget.
    loaded: list[tuple[int, Path, str]] = []
    for path in candidates:
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = path.name
        if path.suffix.lower() == ".md":
            text, cut = _read_bounded(path, min(cap or 1, 4000))
        else:
            # prototype files: path presence only, never content dumps
            text, cut = "", False
            found.append(rel)
            continue
        if not text.strip():
            continue
        found.append(rel)
        loaded.append((_score_excerpt(text, keywords) if keywords else 0, path, text))
        if cut:
            truncated = True

    loaded.sort(key=lambda item: (-item[0], str(item[1])))

    for score, path, text in loaded:
        if cap == 0:
            truncated = truncated or bool(text.strip())
            break
        remaining = cap - used
        if remaining <= 0:
            truncated = True
            break
        snippet = text if (keywords and score > 0) or not keywords else ""
        if keywords and score == 0:
            truncated = truncated or len(loaded) > MAX_EXCERPTS
            continue  # keyword mismatch: skip excerpt, keep it out of budget
        if len(snippet) > remaining:
            snippet = snippet[:remaining]
            truncated = True
        used += len(snippet)
        excerpts.append(
            {
                "source": str(path.relative_to(root)),
                "text": snippet,
                "score": score,
            }
        )
        if used >= cap:
            truncated = True

    if len(excerpts) >= MAX_EXCERPTS:
        truncated = True

    return {"found": found[: 2 * MAX_EXCERPTS], "excerpts": excerpts, "truncated": truncated}


# ---------------------------------------------------------------------------
# public API 3: agy_learnings_read
# ---------------------------------------------------------------------------

def _split_topics(text: str) -> dict[str, str]:
    """learnings.md contract: ``##`` headers delimit topic sections."""
    topics: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current is not None:
                topics[current] = "\n".join(buf).strip()
            current = match.group(1)
            buf = []
        elif current is not None:
            buf.append(line)

    if current is not None:
        topics[current] = "\n".join(buf).strip()

    return topics


def agy_learnings_read(
    repo: Path,
    query: str | None = None,
    max_chars: int = MAX_LEARNINGS_CHARS,
) -> dict:
    """
    Read learnings.md as bounded topics. With a ``query`` only
    keyword-matching topics (by header and body) are returned. Missing
    file -> ``{"present": False, "topics": {}}`` — never an error.
    """
    root = _resolve_repo(repo)
    cap = max(0, int(max_chars))
    path = _first_existing(root, _LEARNINGS_CANDIDATES)

    if path is None:
        return {"present": False, "topics": {}, "truncated": False}

    text, file_cut = _read_bounded(path, cap + 4000)
    all_topics = _split_topics(text)
    keywords = _keywords(query)

    topics: dict[str, str] = {}
    used = 0
    truncated = file_cut

    ordered = sorted(all_topics.items())
    if keywords:
        ordered = sorted(
            ordered,
            key=lambda item: (
                -(
                    _score_excerpt(item[0], keywords) * 2
                    + _score_excerpt(item[1], keywords)
                ),
                item[0],
            ),
        )

    for name, body in ordered:
        if keywords and not (
            _score_excerpt(name, keywords) or _score_excerpt(body, keywords)
        ):
            truncated = True
            continue
        if len(body) > MAX_TOPIC_CHARS:
            body = body[:MAX_TOPIC_CHARS]
            truncated = True
        if used + len(body) > cap:
            body = body[: max(0, cap - used)]
            truncated = True
        used += len(body)
        topics[name] = body
        if used >= cap:
            truncated = True
            break

    return {"present": True, "topics": topics, "truncated": truncated}
