"""P17 — Tool-free planner transport (classification A).

Zero live model calls, zero network, zero credentials. Proves:

 1. the sanctioned tool-free call surface (google-genai
    ``Models.generate_content``) has NO tools/MCP parameter —
    classification A is a STRUCTURAL property of the API, not a config
    assertion (proven by inspecting the installed SDK, not a mock);
 2. the tool-free tier list is Gemini-only (the quota fallback cannot
    silently be a tool-bearing model);
 3. ``routed_tool_free_invoke`` NEVER reaches the CLI transport —
    quota fallback on tier 1 still falls back to the tool-free tier 2
    and the CLI spy is never called (fallback retains the restriction,
    cannot silently gain tools);
 4. quota parity mirrors of P14 test_27–32 for the tool-free router:
    429 park (persisted, no re-probe across rebuild), 503 transient
    (never persisted), pure-503 -> CloudTierCapacityError vs quota-wall
    -> CloudPlannerUnavailable, non-quota fail-closed (one call),
    expiry restores the primary;
 5. a planner attempt to emit a tool event fails closed even on the
    tool-free path (scan_for_tool_events is not bypassed);
 6. missing credentials fail closed across ALL tool-free tiers and
    never degrade into the tool-bearing CLI transport;
 7. the CloudAction contract is unchanged on the tool-free path
    (mode narrowing, fail-closed mode mismatch);
 8. transport selection: the production default is tool_free, the CLI
    is an explicit restricted opt-in, unknown transports raise.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import app.orchestration.cloud_batch.planner as planner_mod
from app.orchestration.cloud_batch import (
    CloudPlanner,
    CloudPlannerUnavailable,
    CloudProtocol,
    CloudProtocolViolation,
    CloudTierCapacityError,
    QuotaRouter,
    TIER_PRIORITY,
    TOOL_FREE_TIER_PRIORITY,
    schemas,
)

PROJECT = "p17-proj"
BASE = "a" * 40
T1, T2 = TOOL_FREE_TIER_PRIORITY[0], TOOL_FREE_TIER_PRIORITY[1]

CLARIFY_JSON = json.dumps(
    {"mode": "CLARIFY", "questions": ["What scope for the change?"]}
)
BATCH_JSON = json.dumps(
    {
        "mode": "BATCH",
        "batch_id": "batch-001",
        "objective": "investigate",
        "delegations": [
            {
                "id": "r1",
                "role": "codebase-investigator",
                "objective": "find the module",
                "owned_files": ["m.py"],
                "verification": ["pytest -q"],
            }
        ],
    }
)
FINAL_JSON = json.dumps({"mode": "FINAL", "success": True, "summary": "done"})


# ---------------------------------------------------------------------------
# 1. Structural classification A — the installed SDK has no tool surface
# ---------------------------------------------------------------------------


def test_tool_free_call_surface_has_no_tools_or_mcp():
    """The sanctioned call (google.genai Models.generate_content) has NO
    tools parameter and no MCP surface — proven on the INSTALLED SDK via
    inspect, not a mock. Classification A is structural: there is no
    execution surface to enable, deny, or misconfigure."""
    from google.genai.models import Models

    params = {
        p
        for p in inspect.signature(Models.generate_content).parameters
        if p != "self"
    }
    # exactly the plain-completion surface (no self)
    assert params == set(planner_mod._GENAI_CALL_PARAMS), (
        f"tool-free surface changed: {sorted(params)} — re-audit the P17 "
        "isolation claim before relying on it"
    )
    assert "tools" not in params
    assert not any("mcp" in p for p in params)
    assert not any("function" in p for p in params)


def test_tool_free_tiers_are_gemini_only():
    """The tool-free tier list can only hold plain-completion (Gemini/
    Vertex) model IDs — a tool-bearing model (e.g. the CLI's Opus slug)
    cannot enter without this test failing. The fallback therefore
    retains the same restriction by construction."""
    assert T1 in TOOL_FREE_TIER_PRIORITY
    for slug in TOOL_FREE_TIER_PRIORITY:
        assert slug.startswith("gemini-"), (
            f"tool-free tier {slug!r} is not a gemini plain-completion "
            "model — the fallback must not silently gain a tool surface"
        )
    # and it is a DIFFERENT list from the CLI chain (which carries Opus)
    assert "claude-opus-4-6-thinking" not in TOOL_FREE_TIER_PRIORITY
    assert any("claude" in t for t in TIER_PRIORITY)  # the B-chain is intact


# ---------------------------------------------------------------------------
# 3. Isolation invariants: the tool-free router never reaches the CLI
# ---------------------------------------------------------------------------


def _patch_tool_free(
    monkeypatch, behavior: dict[str, object]
) -> dict[str, list[str]]:
    """Patch the module-global genai transport with per-model behavior.
    Returns a call log keyed by model."""
    calls: dict[str, list[str]] = {m: [] for m in TOOL_FREE_TIER_PRIORITY}

    def fake(prompt: str, *, model: str | None = None, api_key: str | None = None):
        calls[str(model)].append(prompt)
        outcome = behavior[str(model)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(planner_mod, "genai_tool_free_invoke", fake)
    return calls


def _cli_spy(monkeypatch) -> list:
    """A spy that FAILS the test if the CLI transport is ever reached."""
    hits: list[str] = []

    def spy(prompt: str, *, model: str | None = None):
        hits.append(model or "<default>")
        raise AssertionError(
            "tool-free router must never call the CLI transport"
        )

    monkeypatch.setattr(planner_mod, "agy_cli_invoke", spy)
    return hits


def test_quota_fallback_retains_tool_free_restriction(monkeypatch):
    """429 on tier 1 -> tier 2 serves the SAME turn, both via the
    tool-free transport; the CLI transport is never reached (the
    fallback cannot silently gain tools)."""
    behavior = {
        T1: CloudPlannerUnavailable(
            "genai transport error for model "
            f"{T1}: 429 RESOURCE_EXHAUSTED. {{'error': {{'message': "
            "'Quota exceeded. Resets in 1h2m3s.', 'status': "
            "'RESOURCE_EXHAUSTED'}}}"
        ),
        T2: FINAL_JSON,
    }
    calls = _patch_tool_free(monkeypatch, behavior)
    cli_hits = _cli_spy(monkeypatch)

    invoke = planner_mod.routed_tool_free_invoke(QuotaRouter())
    out = invoke("prompt")
    assert out == FINAL_JSON
    assert calls[T1] == ["prompt"]  # probed, hit the quota wall
    assert calls[T2] == ["prompt"]  # fallback served the SAME turn
    assert cli_hits == []  # the tool-bearing transport was never touched


def test_missing_credentials_fail_closed_and_never_fall_back_to_cli(monkeypatch):
    """No credential on a tier -> CloudPlannerUnavailable; with every
    tool-free tier uncredentialed the turn fails closed — it does NOT
    degrade into the tool-bearing CLI transport."""
    behavior = {
        m: CloudPlannerUnavailable(
            f"genai transport failure for model {m}: A Gemini API key is "
            "required (GEMINI_API_KEY or Vertex ADC not configured)"
        )
        for m in TOOL_FREE_TIER_PRIORITY
    }
    calls = _patch_tool_free(monkeypatch, behavior)
    cli_hits = _cli_spy(monkeypatch)

    # A missing credential is a non-quota failure -> fail closed on the
    # current tier (no tier hop), the original transport error propagates.
    with pytest.raises(CloudPlannerUnavailable, match="Gemini API key"):
        planner_mod.routed_tool_free_invoke(QuotaRouter())("prompt")
    assert calls[T1] == ["prompt"] and calls[T2] == []
    assert cli_hits == []


# ---------------------------------------------------------------------------
# 4. Quota parity mirrors (P14 test_27-32 semantics on the tool-free router)
# ---------------------------------------------------------------------------


def test_tool_free_429_park_persists_across_rebuild(monkeypatch, tmp_path):
    """Mirror of test_28: the park persists to quota.json and a fresh
    router skips the exhausted tier (no re-probe)."""
    qpath = tmp_path / "projects" / PROJECT
    state_root = tmp_path

    behavior = {
        T1: CloudPlannerUnavailable(
            "genai transport error: 429 RESOURCE_EXHAUSTED. Resets in 1h2m3s."
        ),
        T2: FINAL_JSON,
    }
    calls = _patch_tool_free(monkeypatch, behavior)
    invoke = planner_mod.routed_tool_free_invoke(
        QuotaRouter(state_root=state_root, project_id=PROJECT)
    )
    assert invoke("p") == FINAL_JSON
    assert (state_root / "projects" / PROJECT / "quota.json").exists()

    # Restart: fresh router + fresh invoke must SKIP tier 1 entirely.
    calls2 = _patch_tool_free(monkeypatch, behavior)
    invoke2 = planner_mod.routed_tool_free_invoke(
        QuotaRouter(state_root=state_root, project_id=PROJECT)
    )
    assert invoke2("p") == FINAL_JSON
    assert calls2[T1] == []  # parked: not re-probed
    assert calls2[T2] == ["p"]


def test_tool_free_503_transient_and_capacity_semantics(monkeypatch):
    """Mirror of test_29/29b: 503 is transient (never persisted); a pure
    503 wall -> CloudTierCapacityError; a quota wall dominating ->
    CloudPlannerUnavailable."""
    cli_hits = _cli_spy(monkeypatch)
    behavior = {
        T1: CloudPlannerUnavailable("genai error: 503 UNAVAILABLE. high traffic"),
        T2: CloudPlannerUnavailable("genai error: 503 UNAVAILABLE. No capacity"),
    }
    _patch_tool_free(monkeypatch, behavior)
    with pytest.raises(CloudTierCapacityError):
        planner_mod.routed_tool_free_invoke(QuotaRouter())("p")
    assert cli_hits == []

    # quota wall on tier1 + capacity on tier2 -> Unavailable (quota dominates)
    behavior2 = {
        T1: CloudPlannerUnavailable("genai error: 429 RESOURCE_EXHAUSTED."),
        T2: CloudPlannerUnavailable("genai error: 503 UNAVAILABLE."),
    }
    _patch_tool_free(monkeypatch, behavior2)
    with pytest.raises(CloudPlannerUnavailable):
        planner_mod.routed_tool_free_invoke(QuotaRouter())("p")


def test_tool_free_expiry_restores_primary(monkeypatch, tmp_path):
    """Mirror of test_32: once the advertised reset passes, tier 1 is
    served first again."""
    state_root = tmp_path
    behavior = {
        T1: CloudPlannerUnavailable("genai error: 429 RESOURCE_EXHAUSTED."),
        T2: FINAL_JSON,
    }
    _patch_tool_free(monkeypatch, behavior)
    invoke = planner_mod.routed_tool_free_invoke(
        QuotaRouter(state_root=state_root, project_id=PROJECT)
    )
    invoke("p")  # parks T1

    # fast-forward past the advertised reset
    import json as _json

    q = state_root / "projects" / PROJECT / "quota.json"
    data = _json.loads(q.read_text())
    for m in data["exhausted_until"]:
        data["exhausted_until"][m] = __import__("time").time() - 10
    q.write_text(_json.dumps(data))

    # After the advertised reset, the primary tier is healthy AND the
    # quota is genuinely restored (it now succeeds) -> it serves first.
    behavior[T1] = FINAL_JSON
    calls = _patch_tool_free(monkeypatch, behavior)
    invoke2 = planner_mod.routed_tool_free_invoke(
        QuotaRouter(state_root=state_root, project_id=PROJECT)
    )
    assert invoke2("p") == FINAL_JSON
    assert calls[T1] == ["p"] and calls[T2] == []  # primary restored
    # Both TOOL-FREE tiers are healthy again (the CLI chain is a different
    # tier list — assert over the tool-free set, not a bare default router).
    router = QuotaRouter(
        tiers=list(TOOL_FREE_TIER_PRIORITY),
        state_root=state_root,
        project_id=PROJECT,
    )
    statuses = {s["model"]: s["status"] for s in router.tier_status()}
    assert statuses == {T1: "healthy", T2: "healthy"}


def test_tool_free_all_tiers_down_quota_message(monkeypatch):
    """Mirror of test_31: every tool-free tier exhausted -> the
    unavailability message names the tool-free transport tiers."""
    behavior = {
        m: CloudPlannerUnavailable("genai error: 429 RESOURCE_EXHAUSTED.")
        for m in TOOL_FREE_TIER_PRIORITY
    }
    _patch_tool_free(monkeypatch, behavior)
    with pytest.raises(CloudPlannerUnavailable, match="tool-free transport tiers unavailable"):
        planner_mod.routed_tool_free_invoke(QuotaRouter())("p")


# ---------------------------------------------------------------------------
# 5. Tool-event emission still fails closed on the tool-free path
# ---------------------------------------------------------------------------


def test_tool_event_emission_fails_closed_on_tool_free_path():
    """Even a tool-free model that hallucinates a tool-call event is
    caught by the fail-closed scan (the Classification-B interim
    defense is NOT bypassed by switching transports)."""
    from app.orchestration.cloud_batch.protocol import scan_for_tool_events

    raw = '{"mode": "BATCH", "batch_id": "b1"}\n{"tool_use": "run_command"}'
    assert scan_for_tool_events(raw)  # the scan fires on the raw text

    planner = CloudPlanner(invoke=lambda prompt: raw)
    with pytest.raises(CloudProtocolViolation, match="tool"):
        planner.batch("prompt")


# ---------------------------------------------------------------------------
# 6. CloudAction contract unchanged on the tool-free path
# ---------------------------------------------------------------------------


def test_cloudaction_contract_unchanged_via_tool_free(monkeypatch):
    """Drive the production tool-free wiring (routed builder + real
    QuotaRouter + CloudPlanner) end to end with a fake genai transport:
    the parse funnel, mode narrowing, and one-call-per-turn behavior are
    identical to the P14 contract."""
    cli_hits = _cli_spy(monkeypatch)
    behavior = {T1: BATCH_JSON, T2: BATCH_JSON}
    calls = _patch_tool_free(monkeypatch, behavior)

    planner = CloudPlanner(quota_router=QuotaRouter())  # default: tool_free
    plan = planner.batch("any prompt")
    assert isinstance(plan, schemas.BatchPlan)
    assert plan.batch_id == "batch-001"
    assert calls[T1] == ["any prompt"]  # ONE call, primary tier
    assert cli_hits == []

    # mode mismatch still fails closed, never coerced
    monkeypatch.setitem(behavior, T1, CLARIFY_JSON)
    with pytest.raises(CloudProtocolViolation, match="expected BATCH"):
        planner.batch("any prompt")


def test_reentry_turn_via_tool_free_contract(monkeypatch):
    """A reentry turn (protocol seam) over the tool-free transport yields
    the union-narrowed CloudAction with the same one-call budget."""
    cli_hits = _cli_spy(monkeypatch)
    behavior = {T1: FINAL_JSON, T2: FINAL_JSON}
    calls = _patch_tool_free(monkeypatch, behavior)
    planner = CloudPlanner(quota_router=QuotaRouter())
    action = planner.reentry("reentry prompt")
    assert isinstance(action, schemas.FinalResult)
    assert action.success is True
    assert calls[T1] == ["reentry prompt"]
    assert cli_hits == []


# ---------------------------------------------------------------------------
# 7. Transport selection: default tool-free, CLI explicit, unknown raises
# ---------------------------------------------------------------------------


def test_default_transport_is_tool_free_and_cli_is_explicit(monkeypatch):
    """CloudPlanner() defaults to the tool-free transport; the
    classification-B CLI chain is only reachable via the explicit
    restricted opt-in; an unknown transport raises (fail closed)."""
    built: list[str] = []

    def tf_marker(router=None, *, api_key=None):
        built.append("tool_free")
        return lambda prompt: BATCH_JSON

    def cli_marker(router=None):
        built.append("agy_cli")
        return lambda prompt: BATCH_JSON

    monkeypatch.setattr(planner_mod, "routed_tool_free_invoke", tf_marker)
    monkeypatch.setattr(planner_mod, "routed_agy_invoke", cli_marker)

    CloudPlanner(quota_router=QuotaRouter()).batch("p")
    assert built == ["tool_free"]  # production default

    built.clear()
    CloudPlanner(transport="agy_cli", quota_router=QuotaRouter()).batch("p")
    assert built == ["agy_cli"]  # explicit restricted opt-in

    built.clear()
    with pytest.raises(ValueError, match="unknown planner transport"):
        CloudPlanner(transport="telepathy")
    assert built == []  # nothing wired before the fail-closed raise


def test_p14_cli_chain_unaffected(monkeypatch):
    """The P14 classification-B chain still works exactly as before
    (429 -> Opus same turn) — P17 is additive to that path."""
    GEM, OPUS = TIER_PRIORITY[0], TIER_PRIORITY[1]
    calls: list[str] = []

    def fake_cli(prompt: str, *, model: str | None = None):
        calls.append(str(model))
        if model == GEM:
            raise CloudPlannerUnavailable(
                "agy CLI exited 1 for model "
                f"{GEM}: 429 RESOURCE_EXHAUSTED. Resets in 1h2m3s."
            )
        return FINAL_JSON

    monkeypatch.setattr(planner_mod, "agy_cli_invoke", fake_cli)
    invoke = planner_mod.routed_agy_invoke(QuotaRouter())
    assert invoke("p") == FINAL_JSON
    assert calls == [GEM, OPUS]
