"""
P8.1 compatibility fix — analyst runtime hardening regression tests.

Two live-discovered defects in ``codebase_analysis``:

1. The default analyst dispatch passed ``repeat_limit=0`` to the external
   ``opencode-supervise`` wrapper, whose loop guard then killed a HEALTHY
   session on the first legitimately repeated read-only command
   (REPEATED_TOOL_LOOP). Fixed to ``repeat_limit=3``.
2. ``_extract_json_object`` grabbed the FIRST balanced JSON object in the
   transcript. Real transcripts contain tool arguments/results and
   intermediate JSON BEFORE the final evidence object (observed live: it
   extracted ``{"relative_path": "search.py"}``). Fixed to a schema-aware
   END->BEGINNING scan validated against ``AnalysisEvidence``, failing
   closed with ``AnalystOutputInvalid`` (subclass of
   ``AnalystUnavailable``).

Fakes only: no network, no subprocess, no OpenCode server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations import external_stack
from app.orchestration.checkpoint_planning.codebase_analysis import (
    AnalystOutputInvalid,
    AnalystUnavailable,
    _extract_json_object,
    default_analyst,
    evidence_from_payload,
)

try:  # Worker A's schemas at integration; B's dataclass fallback mid-flight
    from app.orchestration.checkpoint_planning.schemas import AnalysisRequest
except ImportError:  # pragma: no cover
    from app.orchestration.checkpoint_planning.codebase_analysis import (
        AnalysisRequest,
    )


EVIDENCE = {
    "summary": "Refunds route through payments/ledger.py.",
    "relevant_symbols": ["ledger.refund", "checkout.finalize"],
    "dependency_paths": ["app/checkout.py -> app/payments/ledger.py"],
    "existing_tests": ["tests/test_refunds.py"],
    "likely_files": ["app/payments/ledger.py", "app/checkout.py"],
    "constraints": ["ledger writes are idempotent"],
    "open_questions": ["partial refunds out of scope?"],
}

EVIDENCE_JSON = json.dumps(EVIDENCE)


def make_request(**kw) -> AnalysisRequest:
    base = dict(
        objective="add refund window to checkout",
        questions=["where are refunds handled?"],
        focus=["payments"],
    )
    base.update(kw)
    return AnalysisRequest(**base)


def captured_dispatch(stdout: str):
    """run_dispatch fake that records kwargs and returns an ok result."""
    calls: list[dict] = []

    def dispatch(repo_, prompt_, **kwargs):
        calls.append({"repo": repo_, "prompt": prompt_, **kwargs})
        return {"ok": True, "stdout": stdout, "stderr": ""}

    return dispatch, calls


# ---------------------------------------------------------------------------
# T1: repeat-limit default (live-discovered runtime defect)
# ---------------------------------------------------------------------------


def test_default_dispatch_uses_repeat_limit_3(tmp_path: Path, monkeypatch):
    """The closure inside default_analyst must hand the wrapper repeat_limit=3.

    repeat_limit=0 made the external opencode-supervise loop guard kill a
    healthy session on its FIRST legitimate repeated read-only command.
    """
    seen: dict = {}

    def fake_run_supervised(repo_, prompt_, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "stdout": EVIDENCE_JSON, "stderr": ""}

    monkeypatch.setattr(external_stack, "run_supervised", fake_run_supervised)

    evidence = default_analyst(make_request(), tmp_path)

    assert seen.get("repeat_limit") == 3
    assert seen.get("stall_seconds") == 120
    assert seen.get("max_restarts") == 0
    assert evidence.summary == EVIDENCE["summary"]


# ---------------------------------------------------------------------------
# T2: one repeated action allowed / guard exhaustion -> fail closed
# ---------------------------------------------------------------------------


def test_default_limit_allows_healthy_repetition(tmp_path: Path, monkeypatch):
    """Wrapper-side semantics: repeat_limit=3 lets up to three legitimate
    repeats run, so a healthy session that completed (ok=True) is ACCEPTED.

    Encoded at this seam: the default dispatch is invoked with
    repeat_limit=3 and an ok-True stdout yields evidence without raising
    AnalystUnavailable.
    """
    seen: dict = {}

    def fake_run_supervised(repo_, prompt_, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "stdout": EVIDENCE_JSON, "stderr": ""}

    monkeypatch.setattr(external_stack, "run_supervised", fake_run_supervised)

    evidence = default_analyst(make_request(), tmp_path)

    assert seen.get("repeat_limit") == 3
    assert evidence.likely_files == EVIDENCE["likely_files"]


def test_guard_exhaustion_fails_closed(tmp_path: Path):
    """Beyond the limit the wrapper aborts the session nonzero with
    REPEATED_TOOL_LOOP (limit 0 = one repeat kills; limit 3 = three
    repeats allowed). The analyst seam must fail closed.
    """

    def dispatch(_repo, _prompt, **_kw):
        return {
            "ok": False,
            "stdout": "",
            "stderr": "loop guard REPEATED_TOOL_LOOP triggered (repeat_limit=0)",
        }

    with pytest.raises(AnalystUnavailable):
        default_analyst(make_request(), tmp_path, run_dispatch=dispatch)


# ---------------------------------------------------------------------------
# T3: schema-aware END->BEGINNING extraction
# ---------------------------------------------------------------------------


def test_transcript_with_tool_json_before_evidence_extracts_final_object(
    tmp_path: Path,
):
    """Real transcript shape: session header + tool-argument JSON +
    intermediate JSON + FINAL evidence. The last valid object wins — never
    the first fragment."""
    stdout = (
        "Session ID: ses_x\n"
        + json.dumps({"relative_path": "search.py"})
        + "\n"
        + json.dumps({"status": "ok"})
        + "\n"
        + EVIDENCE_JSON
    )

    assert _extract_json_object(stdout) == EVIDENCE

    dispatch, _calls = captured_dispatch(stdout)
    evidence = default_analyst(
        make_request(), tmp_path, run_dispatch=dispatch,
        session_capturer=lambda out, path: ("ses_x", "stdout"),
    )
    for key, value in EVIDENCE.items():
        assert getattr(evidence, key) == value


def test_whole_text_json_fast_path_accepted():
    """If the whole text is itself valid evidence JSON, return it."""
    assert _extract_json_object(EVIDENCE_JSON) == EVIDENCE


def test_tool_fragment_alone_is_not_evidence(tmp_path: Path):
    """A fragment that parses as JSON but fails the schema (no required
    ``summary``) must NOT be accepted — the run fails closed with
    AnalystOutputInvalid, which is still an AnalystUnavailable."""
    fragment = json.dumps({"relative_path": "search.py"})

    assert _extract_json_object(f"Session ID: ses_x\n{fragment}") is None

    dispatch, _calls = captured_dispatch(f"Session ID: ses_x\n{fragment}")
    with pytest.raises(AnalystOutputInvalid) as exc_info:
        default_analyst(make_request(), tmp_path, run_dispatch=dispatch)
    assert isinstance(exc_info.value, AnalystUnavailable)


def test_no_json_at_all_fails_closed(tmp_path: Path):
    """Prose-only transcript -> AnalystOutputInvalid, never fabricated
    empty evidence."""
    dispatch, _calls = captured_dispatch("I found some files, hope that helps!")

    assert _extract_json_object("I found some files, hope that helps!") is None

    with pytest.raises(AnalystOutputInvalid):
        default_analyst(make_request(), tmp_path, run_dispatch=dispatch)


def test_later_malformed_object_does_not_beat_earlier_valid_one():
    """A truncated junk tail after a valid evidence object: only VALID
    candidates count, the earlier valid one wins."""
    truncated_tail = '{"summary": "never closes'
    stdout = EVIDENCE_JSON + "\n" + truncated_tail

    assert _extract_json_object(stdout) == EVIDENCE


# ---------------------------------------------------------------------------
# T4: existing-behavior invariants (guard against drift)
# ---------------------------------------------------------------------------


def test_fenced_json_block_still_parses(tmp_path: Path):
    """Mirrors test_agy_p8_analyst.py's fenced-evidence test."""
    stdout = "Session ID: ses_abc123\n```json\n" + EVIDENCE_JSON + "\n```\n"

    dispatch, _calls = captured_dispatch(stdout)
    evidence = default_analyst(
        make_request(),
        tmp_path,
        run_dispatch=dispatch,
        session_capturer=lambda out, path: ("ses_abc123", "stdout"),
    )
    assert evidence.summary == EVIDENCE["summary"]
    assert evidence.existing_tests == ["tests/test_refunds.py"]


def test_bounded_caps_still_enforced():
    """evidence_from_payload keeps clamping list length and summary size."""
    payload = dict(EVIDENCE)
    payload["likely_files"] = [f"file{i}.py" for i in range(500)]
    payload["summary"] = "x" * 10_000
    evidence = evidence_from_payload(payload)
    assert len(evidence.likely_files) <= 25
    assert len(evidence.summary) <= 2000
