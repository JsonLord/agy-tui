"""P14 — BatchOutcome compression.

Cloud token reduction is the entire purpose of P14. This module turns
raw worker execution results (which may carry transcripts, logs, diffs,
Serena/Graphify output) into bounded, cloud-facing
:class:`WorkerOutcome` / :class:`BatchOutcome` objects:

* raw evidence is NEVER copied into cloud-facing fields — it is written
  to the local store (``workers/<id>.raw.json``) and referenced by a
  relative ``raw_evidence_ref``;
* every list is deterministically truncated to its bound, with
  ``truncated=True`` set when anything was cut;
* the assembled :class:`BatchOutcome` is size-checked (its own hard
  validator rejects > 16 KB).
"""

from __future__ import annotations

from typing import Any

from app.orchestration.cloud_batch.schemas import (
    BatchOutcome,
    REENTRY_REASONS,
    WORKER_CONCLUSION_MAX_CHARS,
    WORKER_EVIDENCE_REF_MAX,
    WORKER_RISK_MAX,
    WORKER_UNKNOWN_MAX,
    WorkerOutcome,
)


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= limit:
        return text, False
    return text[: limit - 1].rstrip() + "\u2026", True


def _truncate_list(items: list[str], limit: int) -> tuple[list[str], bool]:
    cleaned = [str(item).strip() for item in (items or []) if str(item).strip()]
    if len(cleaned) <= limit:
        return cleaned, False
    return cleaned[:limit], True


def compress_worker_result(
    delegation_id: str,
    role: str,
    status: str,
    raw: dict[str, Any],
    *,
    raw_evidence_ref: str = "",
) -> WorkerOutcome:
    """Deterministically compress one raw worker result.

    ``raw`` keys (all optional): conclusion, evidence_refs, risks,
    unknowns. Any other raw keys (transcripts, logs, stdout, diffs) are
    ignored here by design — they belong to the local .raw.json artifact
    only.
    """
    truncated = False

    conclusion, t1 = _truncate_text(str(raw.get("conclusion", "")), WORKER_CONCLUSION_MAX_CHARS)
    truncated = truncated or t1

    evidence, t2 = _truncate_list(
        [str(item) for item in raw.get("evidence_refs", [])], WORKER_EVIDENCE_REF_MAX
    )
    truncated = truncated or t2

    risks, t3 = _truncate_list(
        [str(item) for item in raw.get("risks", [])], WORKER_RISK_MAX
    )
    truncated = truncated or t3

    unknowns, t4 = _truncate_list(
        [str(item) for item in raw.get("unknowns", [])], WORKER_UNKNOWN_MAX
    )
    truncated = truncated or t4

    return WorkerOutcome(
        delegation_id=delegation_id,
        role=role,
        status=status,
        conclusion=conclusion,
        evidence_refs=evidence,
        risks=risks,
        unknowns=unknowns,
        truncated=truncated,
        raw_evidence_ref=raw_evidence_ref or "",
    )


def build_batch_outcome(
    batch_id: str,
    objective: str,
    workers: list[WorkerOutcome],
    *,
    reentry_reason: str = REENTRY_REASONS[0],
) -> BatchOutcome:
    """Assemble the compact batch outcome. The BatchOutcome schema's hard
    size validator is the final guard; this function keeps the summary
    deterministic and bounded."""
    if reentry_reason not in REENTRY_REASONS:
        raise ValueError(f"unknown reentry reason {reentry_reason!r}")
    summary, _ = _truncate_text(
        "; ".join(
            f"{w.delegation_id}:{w.status}" for w in workers
        ),
        WORKER_CONCLUSION_MAX_CHARS,
    )
    return BatchOutcome(
        batch_id=batch_id,
        objective=objective,
        workers=workers,
        reentry_reason=reentry_reason,  # type: ignore[arg-type]
        summary=summary,
    )


__all__ = [
    "compress_worker_result",
    "build_batch_outcome",
    "_truncate_text",
    "_truncate_list",
]
