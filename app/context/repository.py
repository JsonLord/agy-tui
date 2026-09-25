import re
import subprocess
from pathlib import Path


IMPORTANT_FILES = {
    "README.md",
    "README",
    "AGENTS.md",
    "CLAUDE.md",
    "spec.md",
    "SPEC.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
}

TEXT_EXTENSIONS = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx",
    ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp",
    ".sh", ".bash",
    ".json", ".toml", ".yaml", ".yml",
    ".md", ".txt",
    ".html", ".css",
}

STOPWORDS = {
    "the", "and", "for", "with", "that", "this",
    "from", "into", "existing", "implement",
    "change", "add", "make", "should", "using",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        return ""

    return result.stdout.strip()


def list_repo_files(repo: Path) -> list[str]:
    output = _git(
        repo,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    )

    if output:
        return [
            line.strip()
            for line in output.splitlines()
            if line.strip()
        ]

    files = []

    for path in repo.rglob("*"):
        if not path.is_file():
            continue

        if ".git" in path.parts:
            continue

        files.append(str(path.relative_to(repo)))

    return sorted(files)


def _task_terms(task: str) -> set[str]:
    return {
        word.lower()
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_-]+", task)
        if len(word) >= 3 and word.lower() not in STOPWORDS
    }


def _read_text(path: Path, limit: int = 20_000) -> str | None:
    try:
        if path.stat().st_size > 250_000:
            return None

        data = path.read_text(
            encoding="utf-8",
            errors="replace",
        )

        return data[:limit]

    except OSError:
        return None


def _score_file(
    relative: str,
    content: str,
    terms: set[str],
    changed_files: set[str],
) -> int:
    score = 0

    relative_lower = relative.lower()
    content_lower = content.lower()

    if relative in changed_files:
        score += 100

    if Path(relative).name in IMPORTANT_FILES:
        score += 30

    if "test" in relative_lower:
        score += 8

    for term in terms:
        if term in relative_lower:
            score += 20

        occurrences = content_lower.count(term)

        if occurrences:
            score += min(occurrences, 5) * 3

    return score


def build_snapshot(
    repo: Path,
    task: str,
    max_paths: int = 300,
    max_source_files: int = 12,
    max_chars: int = 70_000,
) -> str:
    repo = repo.resolve()

    paths = list_repo_files(repo)
    terms = _task_terms(task)

    status = _git(repo, "status", "--short")
    diff = _git(repo, "diff", "--no-ext-diff")

    changed_files = set(
        _git(repo, "diff", "--name-only").splitlines()
    )

    sections = [
        f"# Repository\n{repo}",
        "# Git status\n" + (status or "(clean)"),
        "# Repository file map\n" + "\n".join(paths[:max_paths]),
    ]

    candidates = []

    for relative in paths:
        path = repo / relative

        if (
            path.suffix.lower() not in TEXT_EXTENSIONS
            and path.name not in IMPORTANT_FILES
        ):
            continue

        content = _read_text(path)

        if content is None:
            continue

        score = _score_file(
            relative,
            content,
            terms,
            changed_files,
        )

        candidates.append(
            (score, relative, content)
        )

    # On small repositories include essentially everything.
    if len(candidates) <= max_source_files:
        selected = candidates
    else:
        selected = sorted(
            candidates,
            key=lambda item: (-item[0], item[1]),
        )[:max_source_files]

    if selected:
        source_sections = []

        for score, relative, content in selected:
            source_sections.append(
                f"## {relative}\n"
                f"Relevance score: {score}\n\n"
                f"```text\n{content}\n```"
            )

        sections.append(
            "# Selected source context\n\n"
            + "\n\n".join(source_sections)
        )

    if diff:
        sections.append(
            "# Current working-tree diff\n"
            + diff
        )

    result = "\n\n".join(sections)

    return result[:max_chars]
