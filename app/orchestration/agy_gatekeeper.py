"""P3 MiMo Pro gatekeeper: the supervisor decision step.

``review_stage`` sends the structured stage-evidence bundle to the
SUPERVISOR role through :func:`app.orchestration.failover.
complete_with_failover` (role-preserving, 4-attempt primary budget +
one fallback-model call) and parses EXACTLY one JSON decision object.

Fail-closed rules (never fail open):

* unparseable response -> ``REJECT`` with rationale
  ``gatekeeper response invalid``;
* ``decision`` outside ``{ACCEPT, REPAIR, REJECT}`` -> same;
* the model CANNOT override failed deterministic checks — they are
  embedded in the evidence as ``deterministic_failures`` and the
  forcing itself lives in ``agy_execute`` (§5 of the P3 contract);
* the model identity that actually answered (primary or fallback) is
  recorded in the result.

JSON extraction reuses ``app.agents.planner._extract_json`` —
imported lazily at call time exactly like ``decompose._extract_json``
because ``app.agents.planner`` pulls in the host-provided
``google.antigravity`` runtime at module import time.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from app.config import AgySettings
from app.orchestration.failover import complete_with_failover
from app.providers import (
    ChatMessage,
    ModelRole,
    OpenAICompatibleClient,
    ProviderEndpoint,
    RoleModels,
    Transport,
)

SUPERVISOR_GATE_SYSTEM = """\
You are the AGY commit gatekeeper: a read-only supervisor that decides
whether ONE development stage may be recorded as accepted.

You receive ONLY structured, deterministic evidence (task results,
verification output, changed files, scope violations, stage
acceptance, cumulative gate, commit record, invariants) as JSON. The
evidence carries a 'deterministic_failures' list; when it is
non-empty you CANNOT return ACCEPT — your options are REPAIR or
REJECT.

Respond with EXACTLY one JSON object and no prose outside it:

{"decision": "ACCEPT" | "REPAIR" | "REJECT", "rationale": "<one paragraph>", "repair_targets": ["<concrete failure>"]}

Rules:
- "repair_targets" must be non-empty only for REPAIR, each entry a
  concrete failing check to fix (command or scope violation);
- ACCEPT only when deterministic_failures is empty AND the evidence
  genuinely satisfies the stage objective and invariants;
- REJECT when the work is unsound or outside scope and a bounded
  repair cannot fix it; always give a rationale;
- you are read-only: you never modify the repository here.
""".strip()


def build_gate_evidence(payload: dict[str, Any]) -> str:
    """
    JSON-serialize the evidence bundle for the gatekeeper prompt.

    ``deterministic_failures`` is computed by the caller
    (``agy_execute.execute_stage``) and embedded here; the key is
    always present (empty list when every deterministic check is
    green) so the model is never missing the marker.
    """
    data = dict(payload)
    data.setdefault("deterministic_failures", [])

    return json.dumps(data, sort_keys=True, indent=2, default=str)


def _extract_json(text: str) -> dict[str, Any]:
    """
    ``_extract_json`` from :mod:`app.agents.planner`, imported
    lazily for the same reason ``decompose`` does it: the host
    runtime is not importable in a plain environment, while the
    extractor itself is pure. Imported, never duplicated.
    """
    from app.agents.planner import _extract_json as planner

    return planner(text)


def _invalid_gate(model: str | None, detail: str) -> dict[str, Any]:
    """Fail-closed result for unparseable/non-enum gatekeeper output."""
    return {
        "decision": "REJECT",
        "rationale": "gatekeeper response invalid",
        "repair_targets": [],
        "model": model,
        "deterministic_override": False,
        "failover_event": None,
        "parse_detail": detail,
    }


async def review_stage(
    *,
    evidence: dict[str, Any],
    settings: AgySettings,
    client_factory: Callable[[RoleModels], Any] | None = None,
    transport: Transport | None = None,
    on_failover_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """
    Review ONE stage's evidence bundle on the SUPERVISOR role.

    ``client_factory`` lets tests inject a deterministic client
    (default builds the real ``OpenAICompatibleClient`` over
    ``ProviderEndpoint.for_agy(settings)``). ``on_failover_event``
    receives the schema record exactly once if — and only if — a
    transient provider failure forced the fallback model.

    Returns ``{decision, rationale, repair_targets, model,
    deterministic_override, failover_event}``. Parse failure or a
    non-enum decision becomes REJECT with rationale
    ``gatekeeper response invalid`` (fail closed).
    """
    models = RoleModels.for_agy(settings)

    if client_factory is not None:
        client = client_factory(models)
    else:
        client = OpenAICompatibleClient(
            ProviderEndpoint.for_agy(settings),
            models,
            transport=transport,
        )

    result, event = await complete_with_failover(
        client,
        role=ModelRole.SUPERVISOR,
        messages=[
            ChatMessage("system", SUPERVISOR_GATE_SYSTEM),
            ChatMessage("user", "EVIDENCE:\n" + build_gate_evidence(evidence)),
        ],
        settings=settings,
        on_event=on_failover_event,
    )

    # Which model actually answered: after a failover that is the
    # fallback binding, otherwise whatever the provider reported.
    model: str = event["active_model"] if event else result.model

    try:
        payload = _extract_json(result.text)
    except Exception as exc:  # noqa: BLE001 - fail closed on anything
        return _invalid_gate(model, f"unparseable JSON: {exc}")

    if not isinstance(payload, dict):
        return _invalid_gate(model, "gate response is not a JSON object")

    decision = payload.get("decision")
    if decision not in ("ACCEPT", "REPAIR", "REJECT"):
        return _invalid_gate(
            model,
            f"decision not in enum: {decision!r}",
        )

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        rationale = "gatekeeper returned no rationale"

    repair_targets_raw = payload.get("repair_targets")
    repair_targets: list[str] = []
    if decision == "REPAIR" and isinstance(repair_targets_raw, list):
        repair_targets = [
            str(target) for target in repair_targets_raw if str(target).strip()
        ]

    return {
        "decision": decision,
        "rationale": rationale,
        "repair_targets": repair_targets,
        "model": model,
        "deterministic_override": False,
        "failover_event": event,
    }