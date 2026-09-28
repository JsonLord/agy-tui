"""P11 — curated durable learnings (deliberate promotion only).

One concept per file, three distinct stores in AGY — do not mix them:

* ``activity.log``  — events (what happened, append-everything);
* the plan         — current work (what we are doing now);
* ``design.md``    — standing constraints (what must stay true);
* ``learnings.md`` — DURABLE CURATED LESSONS (what we learned).

Because lessons are curated, promotion is DELIBERATE: nothing lands in
the store without an explicit :func:`promote_learning` call carrying a
non-empty rationale. Human feedback is NEVER dumped here
automatically — the planner path may DRAFT a lesson
(:func:`propose_learning_from_feedback`) but a draft is inert until a
human/curator explicitly promotes it.

Canonical store format follows the existing learnings.md contract
(context.py): ``##``-header sections are topics. One promoted lesson is
one section, stable-shaped::

    ## learning <UTC date> — <source>

    <learning text>

    - rationale: <rationale>
    - provenance: <provenance>

The header prefix ``## learning`` + the sorted-keys footer make entries
machine-recognizable while staying valid for the keyword-topic reader.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from app.orchestration.checkpoint_planning.schemas import (
    HumanPlannerInputRequest,
)

#: Every promoted entry opens with this header prefix (contract marker).
ENTRY_HEADER_PREFIX = "## learning "

_ENTRY_RE = re.compile(r"^## learning (?P<date>\S+) — (?P<source>.*)$")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def promote_learning(
    store_path: Path | str,
    learning: str,
    *,
    source: str,
    provenance: str,
    rationale: str,
) -> Path:
    """Append ONE deliberately-promoted lesson; return the store path.

    Fail-closed curation rules — a lesson that cannot say WHY it is
    durable does not enter the store:

    * ``learning`` / ``source`` / ``rationale`` must be non-empty
      (``ValueError`` otherwise); ``rationale`` is mandatory precisely
      because promotion is a judgement, not an event dump.
    * ``provenance`` (where the lesson came from, e.g. a human decision
      ref or plan id) is kept on the entry line for audit.

    No automatic ingestion exists: callers invoke this only after an
    explicit curation decision.
    """
    for name, value in (
        ("learning", learning),
        ("source", source),
        ("rationale", rationale),
    ):
        if not str(value or "").strip():
            raise ValueError(
                f"promote_learning: {name} is mandatory (deliberate "
                "promotion only — nothing lands without an explicit, "
                "reasoned curation decision)"
            )

    path = Path(store_path)
    entry = (
        f"{ENTRY_HEADER_PREFIX}{_utc_date()} — {source.strip()}\n\n"
        f"{learning.strip()}\n\n"
        f"- rationale: {rationale.strip()}\n"
        f"- provenance: {str(provenance or '').strip()}\n"
    )

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        sep = "" if existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n"
        )
        path.write_text(existing + sep + entry, encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry, encoding="utf-8")
    return path


def read_learnings(store_path: Path | str) -> list[dict]:
    """Parse promoted entries (date, source, learning, rationale,
    provenance). Read-only; missing store → empty list."""
    path = Path(store_path)
    if not path.exists():
        return []
    entries: list[dict] = []
    current: dict | None = None
    body: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENTRY_RE.match(line)
        if match:
            if current is not None:
                entries.append(_finalize(current, body))
            current = match.groupdict()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        entries.append(_finalize(current, body))
    return entries


def _finalize(head: dict, lines: list[str]) -> dict:
    rationale = ""
    provenance = ""
    body: list[str] = []
    for line in lines:
        if line.startswith("- rationale: "):
            rationale = line[len("- rationale: "):]
        elif line.startswith("- provenance: "):
            provenance = line[len("- provenance: "):]
        else:
            body.append(line)
    return {
        "date": head["date"],
        "source": head["source"],
        "learning": "\n".join(body).strip(),
        "rationale": rationale,
        "provenance": provenance,
    }


def propose_learning_from_feedback(
    request: HumanPlannerInputRequest,
) -> str:
    """DRAFT a lesson from a human NEEDS_CHANGES feedback request.

    This is the planner-proposes path ONLY: it returns draft text and
    touches no store. A draft becomes a durable learning solely when a
    curator explicitly calls :func:`promote_learning` with it (plus a
    rationale) — feedback never flows into learnings automatically.
    """
    return (
        f"Human feedback on {request.checkpoint_id} ({request.gate_id}): "
        f"{request.feedback.strip()} — candidate lesson awaiting explicit "
        "promotion (draft is inert until promote_learning)"
    )
