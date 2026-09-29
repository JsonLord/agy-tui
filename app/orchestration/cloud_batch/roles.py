"""P14 — Compact role manifest.

The cloud model (Gemini) knows ROLES, not tools: it never receives
Serena/Graphify tool schemas, repository file contents, or raw
transcripts. Workers know their actual tools — the mapping below is the
single source of truth that lowers a role onto the existing, already
registered OpenCode agents. No duplicate agents are created: every role
maps onto an agent that already exists in
~/.config/opencode/opencode.jsonc (verified 2026-09-29).
"""

from __future__ import annotations

import json

from app.orchestration.cloud_batch.schemas import ROLE_MANIFEST_MAX_BYTES

#: Role -> existing OpenCode agent (do not create new agents without a
#: proven gap; verified present in opencode.jsonc 2026-09-29).
ROLE_TO_AGENT = {
    "codebase-investigator": "agy-planning-analyst",
    "test-author": "agy-test-author",
    "implementer": "agy-dispatch",
    "ui-reviewer": "agy-ui-reviewer",
    "adversarial-reviewer": "agy-adversarial-reviewer",
    "fixer": "agy-fixer",
}

#: The manifest the cloud planner sees. Compact on purpose: purpose +
#: capabilities only, no tool schemas, no file listings, no repo state.
ROLE_MANIFEST = {
    "max_parallel": 4,
    "roles": {
        "codebase-investigator": {
            "purpose": "read-only repository investigation",
            "capabilities": ["Serena", "Graphify", "repository reads"],
        },
        "test-author": {
            "purpose": "write tests and RED evidence; never production code",
            "capabilities": ["tests", "RED evidence"],
        },
        "implementer": {
            "purpose": "bounded implementation inside owned files",
            "capabilities": ["edit", "tests"],
        },
        "ui-reviewer": {
            "purpose": "read-only structured UI review vs design reference",
            "capabilities": ["read-only review", "JSON verdict"],
        },
        "adversarial-reviewer": {
            "purpose": "read-only adversarial review; structured findings only",
            "capabilities": ["read-only review", "JSON findings"],
        },
        "fixer": {
            "purpose": "bounded accepted repair; never touches tests",
            "capabilities": ["bounded repair"],
        },
    },
}


def known_roles() -> frozenset[str]:
    return frozenset(ROLE_TO_AGENT)


def agent_for_role(role: str) -> str:
    """Lower a role to its OpenCode agent. Raises on unknown roles —
    fail closed; a batch naming an unknown role is invalid."""
    try:
        return ROLE_TO_AGENT[role]
    except KeyError as exc:
        raise ValueError(f"unknown role {role!r}") from exc


def render_role_manifest() -> str:
    """JSON manifest for the planner prompt. Deterministic key order."""
    return json.dumps(ROLE_MANIFEST, sort_keys=True, indent=2)


def role_manifest_within_budget() -> bool:
    return len(render_role_manifest().encode("utf-8")) <= ROLE_MANIFEST_MAX_BYTES


__all__ = [
    "ROLE_TO_AGENT",
    "ROLE_MANIFEST",
    "known_roles",
    "agent_for_role",
    "render_role_manifest",
    "role_manifest_within_budget",
]
