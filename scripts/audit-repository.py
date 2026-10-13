#!/usr/bin/env python3
"""Read-only all-object audit: report classes and references, never matched values."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private_key": rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "github_token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "provider_token": rb"\b(?:sk-[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16})",
    "credential_url": rb"https?://[^\s/@:]+:[^\s/@]+@",
    "credential_literal": rb"""(?i)(?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*["']([A-Za-z0-9_+./=-]{16,})["']""",
    "host_path": rb'(?:/home/[A-Za-z0-9_.-]+|[A-Za-z]:\\(?:Users|Work)\\[^\s"\']+)',
    "email_shape": rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
}


def git(root, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(root), *arguments], stderr=subprocess.DEVNULL
    )


def scan(data):
    return [name for name, pattern in PATTERNS.items() if re.search(pattern, data)]


def reference(value: bytes):
    for name in ("private_key", "github_token", "provider_token", "credential_url"):
        value = re.sub(PATTERNS[name], b"[REDACTED]", value)
    return value.decode("utf-8", "replace")


def audit(root):
    revisions = ["HEAD"]
    try:
        git(root, "rev-parse", "--verify", "FETCH_HEAD")
    except subprocess.CalledProcessError:
        pass
    else:
        revisions.append("FETCH_HEAD")
    objects = git(
        root, "rev-list", "--objects", "--all", "--reflog", *revisions
    ).splitlines()
    paths = {line.partition(b" ")[0]: line.partition(b" ")[2] for line in objects}
    all_objects = git(
        root,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname) %(objecttype)",
    ).splitlines()
    findings, metadata, blobs, unreferenced = [], [], 0, 0
    for line in all_objects:
        key, kind = line.split()
        if kind == b"commit":
            metadata.append(git(root, "cat-file", "-p", key.decode()))
        if kind != b"blob":
            continue
        blobs += 1
        unreferenced += key not in paths
        classes = scan(git(root, "cat-file", "-p", key.decode()))
        if classes:
            findings.append(
                {
                    "scope": "history_blob",
                    "object": key.decode(),
                    "path": reference(paths.get(key, b"(unreferenced blob)")),
                    "classes": classes,
                }
            )
    for raw in filter(
        None,
        git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(
            b"\0"
        ),
    ):
        path = root / raw.decode("utf-8", "replace")
        if path.is_file() and not path.is_symlink():
            classes = scan(path.read_bytes())
            if classes:
                findings.append(
                    {
                        "scope": "working_file",
                        "path": reference(raw),
                        "classes": classes,
                    }
                )
    ignored = git(
        root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
    ).split(b"\0")
    return {
        "scope": "all Git objects including unreachable commits/blobs; fetched refs/reflogs; nonignored working files; ignored artifacts inventoried by name only",
        "commits": len(
            git(root, "rev-list", "--all", "--reflog", *revisions).splitlines()
        ),
        "commit_objects": len(metadata),
        "historical_blobs": blobs,
        "unreferenced_blobs": unreferenced,
        "findings": findings,
        "commit_metadata_classes": scan(b"\n".join(metadata)),
        "ignored_artifact_counts": {
            name: sum(bool(re.search(pattern, p)) for p in ignored if p)
            for name, pattern in {
                "virtualenv_cache": rb"(?:^|/)(?:\.venv[^/]*|__pycache__|\.pytest_cache|\.ruff_cache)/",
                "runtime_state_logs": rb"(?:\.(?:sqlite3|log|dump)$|(?:^|/)(?:state|logs)/)",
                "credential_named": rb"(?:^|/)(?:auth\.json|\.env(?:\..*)?|credentials[^/]*)$",
            }.items()
        },
        "limitations": "Pattern scanning is not proof of absence; semantic review required. Credential stores are not opened and history is not rewritten.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    print(json.dumps(audit(parser.parse_args().root), indent=2))
