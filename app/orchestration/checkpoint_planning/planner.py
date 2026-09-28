"""P8 checkpoint planners (worker A).

Architecture (corrected by the milestone owner): the cloud planner is
GEMINI RUNNING INSIDE THE AGY CLI. This adapter shells out to ``agy -p``
with the shared planning prompt; local models are reached through the
``hybrid-local`` MCP server attached to that CLI. The SDK localharness
path is set aside — nothing here imports it.

Both adapters share the EXACT SAME prompt contract, schema, and output
parsing (``app.agents.planner._extract_json``, imported lazily because
that module pulls in the host ``google.antigravity`` runtime at import
time). They differ only in transport and provenance.

Planner contract (enforced by prompt + tests, not trust):
  the planner may call context/analysis/validate/preview/submit
  SERVICES; it may NOT edit files, run repo shell, commit, mark runtime
  gates PASS, bypass gates, or invoke workers directly. Both adapters
  only ever PRODUCE a CheckpointPlan object; submission discards any
  gate status the planner claims.

Any adapter failure (CLI missing / nonzero exit / timeout / provider
error / unparseable output / schema violation) raises
:class:`PlannerUnavailable` — a plan is never fabricated.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Protocol

from app.orchestration.checkpoint_planning.schemas import (
    AnalysisEvidence,
    CheckpointPlan,
    Complexity,
    PlannerProvenance,
    PlannerUnavailable,
)
from app.orchestration.checkpoint_planning.validation import (
    PlanValidationError,
    plan_from_payload,
    validate_checkpoint_plan,
)

# Bounded adversarial fix cycles (G3) — canonical constant.
MAX_REVIEW_FIX_CYCLES = 3

# Default wall-clock budget for one ``agy -p`` planning invocation.
AGY_CLI_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Prompt contract (shared byte-for-byte by both adapters)
# ---------------------------------------------------------------------------

PLANNER_ROLE_RULES = """\
You are the AGY checkpoint PLANNING layer. You plan; you never execute.

HARD RULES:
- You MUST NOT edit files, run repository shell commands, commit, mark
  runtime gates PASS, bypass gates, or invoke workers directly.
- You MAY call the read-only planning services: agy_project_inspect,
  agy_design_context, agy_learnings_read, agy_gate_templates,
  agy_codebase_analyze, agy_checkpoint_plan_validate,
  agy_checkpoint_plan_preview, agy_checkpoint_plan_submit.
- Submit a COMPLETE gate-preseeded CheckpointPlan, then YIELD execution
  to AGY-local. Execution is not yours to drive.
- Every checkpoint must explicitly account for all four gates
  (G1_behavior, G2_ui, G3_adversarial, G4_human): either required=true
  with data, or required=false WITH a written reason. No silent omissions.
- A required G1_behavior must list planned_tests before implementation.

Return EXACTLY ONE JSON object shaped as the CheckpointPlan schema:
{"plan_id", "feature_id", "objective",
 "planner": {"role", "provider", "model", "fallback_used",
             "gate_template_version"},
 "checkpoints": [{"checkpoint_id" (^[a-z0-9][a-z0-9-]*$), "title",
   "objective", "dependencies", "implementation_context",
   "relevant_files", "stages",
   "gates": {"G1_behavior": {"required", "reason", "planned_tests",
     "focus", "test_instructions"}, "G2_ui": {...}, "G3_adversarial": {...},
     "G4_human": {...}}}],
 "created_at", "revision", "design_context_refs", "learning_refs",
 "analyses"}
"""


def build_planning_prompt(
    *,
    context: dict,
    analyses: list[AnalysisEvidence],
    gate_templates: dict,
    request: str,
) -> str:
    """Deterministic shared prompt: rules + serialized planning inputs."""
    parts = [
        PLANNER_ROLE_RULES,
        "REQUEST:\n" + request.strip(),
        "CONTEXT:\n" + json.dumps(context, sort_keys=True, indent=2, default=str),
        "ANALYSES:\n"
        + json.dumps(
            [a.model_dump(mode="json") for a in analyses],
            sort_keys=True,
            indent=2,
            default=str,
        ),
        "GATE TEMPLATES:\n"
        + json.dumps(gate_templates, sort_keys=True, indent=2, default=str),
    ]
    return "\n\n".join(parts)


def build_revision_prompt(
    *, plan: CheckpointPlan, evidence: dict, reason: str
) -> str:
    """Shared revise prompt: current plan + new evidence + revision reason."""
    return "\n\n".join(
        [
            PLANNER_ROLE_RULES,
            "REVISION REQUEST: " + reason.strip(),
            "NEW EVIDENCE:\n"
            + json.dumps(evidence, sort_keys=True, indent=2, default=str),
            "CURRENT PLAN:\n"
            + json.dumps(plan.model_dump(mode="json"), sort_keys=True, indent=2),
            "Return the FULL revised plan as one JSON object with "
            "'revision' incremented by 1.",
        ]
    )


def _extract_json_lazy(text: str) -> dict:
    """``app.agents.planner._extract_json`` — imported lazily at call
    time (that module imports the host ``google.antigravity`` runtime),
    exactly like ``agy_gatekeeper``. Imported, never duplicated."""
    from app.agents.planner import _extract_json as planner_extract

    return planner_extract(text)


def _payload_to_plan(payload: Any) -> CheckpointPlan:
    try:
        plan = plan_from_payload(payload)
    except PlanValidationError as exc:
        raise PlannerUnavailable(f"planner output invalid: {exc}") from exc
    return plan


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class CheckpointPlanner(Protocol):
    def create_plan(
        self,
        *,
        context: dict,
        analyses: list[AnalysisEvidence],
        gate_templates: dict,
        request: str,
    ) -> CheckpointPlan: ...

    def revise_plan(
        self, *, plan: CheckpointPlan, evidence: dict, reason: str
    ) -> CheckpointPlan: ...


# ---------------------------------------------------------------------------
# Gemini-in-AGY-CLI adapter
# ---------------------------------------------------------------------------


def _default_agy_invoke(prompt: str, **_kw) -> dict:
    """Shell out to the AGY CLI (``agy -p <prompt>``).

    The CLI IS the Gemini connection (OAuth lives in the CLI profile);
    no credential ever touches this process. Any failure raises
    :class:`PlannerUnavailable` — including a missing CLI or a
    handshake that needs a CSRF token (known live-run open item).
    """
    try:
        completed = subprocess.run(
            ["agy", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=AGY_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PlannerUnavailable("cloud planner unavailable: agy CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PlannerUnavailable(
            f"cloud planner unavailable: agy CLI timed out after "
            f"{AGY_CLI_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise PlannerUnavailable(f"cloud planner unavailable: {exc}") from exc

    if completed.returncode != 0:
        raise PlannerUnavailable(
            "cloud planner unavailable: agy CLI exited "
            f"{completed.returncode}: {(completed.stderr or '').strip()[:400]}"
        )

    try:
        payload = _extract_json_lazy(completed.stdout)
    except Exception as exc:  # noqa: BLE001 - fail closed, never fabricate
        raise PlannerUnavailable(f"cloud planner unavailable: unparseable output: {exc}") from exc

    if not isinstance(payload, dict):
        raise PlannerUnavailable("cloud planner unavailable: output is not a JSON object")
    return payload


class GeminiCloudPlanner:
    """Cloud planner = Gemini inside the AGY CLI (provider 'gemini')."""

    def __init__(
        self,
        *,
        invoke: Callable[..., dict] | None = None,
        model: str | None = None,
    ) -> None:
        self._invoke = invoke or _default_agy_invoke
        self._model = model

    # -- CheckpointPlanner surface -----------------------------------------

    def create_plan(
        self,
        *,
        context: dict,
        analyses: list[AnalysisEvidence],
        gate_templates: dict,
        request: str,
    ) -> CheckpointPlan:
        prompt = build_planning_prompt(
            context=context,
            analyses=analyses,
            gate_templates=gate_templates,
            request=request,
        )
        payload = self._call(prompt)
        plan = _payload_to_plan(payload)
        return self._stamp(plan)

    def revise_plan(
        self, *, plan: CheckpointPlan, evidence: dict, reason: str
    ) -> CheckpointPlan:
        prompt = build_revision_prompt(plan=plan, evidence=evidence, reason=reason)
        payload = self._call(prompt)
        revised = _payload_to_plan(payload)
        return self._stamp(revised)

    # -- internals ------------------------------------------------------------

    def _call(self, prompt: str) -> dict:
        try:
            payload = self._invoke(prompt=prompt)
        except PlannerUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
            raise PlannerUnavailable(f"cloud planner unavailable: {exc}") from exc
        if not isinstance(payload, dict):
            raise PlannerUnavailable(
                "cloud planner unavailable: invoke returned no JSON object"
            )
        return payload

    def _stamp(self, plan: CheckpointPlan) -> CheckpointPlan:
        plan = plan.model_copy(deep=True)
        plan.planner = PlannerProvenance(
            role="cloud",
            provider="gemini",
            model=self._model,
            fallback_used=False,
            gate_template_version=plan.planner.gate_template_version or "1",
        )
        return plan


# ---------------------------------------------------------------------------
# MiMo Pro fallback adapter (same pipeline, provenance only differs)
# ---------------------------------------------------------------------------


class MiMoProFallbackPlanner:
    """Fallback planner over the Helmholtz OpenAI-compatible endpoint.

    Same prompt contract, same schema, same submission path as the cloud
    planner; only the transport and the provenance strings differ. The
    endpoint is injected — tests never hit the network.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        http: Callable[..., tuple[int, str]] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._http = http or self._urllib_http
        self._timeout = timeout

    def create_plan(
        self,
        *,
        context: dict,
        analyses: list[AnalysisEvidence],
        gate_templates: dict,
        request: str,
    ) -> CheckpointPlan:
        prompt = build_planning_prompt(
            context=context,
            analyses=analyses,
            gate_templates=gate_templates,
            request=request,
        )
        return self._stamp(_payload_to_plan(self._chat(prompt)))

    def revise_plan(
        self, *, plan: CheckpointPlan, evidence: dict, reason: str
    ) -> CheckpointPlan:
        prompt = build_revision_prompt(plan=plan, evidence=evidence, reason=reason)
        return self._stamp(_payload_to_plan(self._chat(prompt)))

    # -- transport --------------------------------------------------------------

    def _chat(self, prompt: str) -> dict:
        headers = {"Content-type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        payload = {
            "model": self._model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PLANNER_ROLE_RULES},
                {"role": "user", "content": prompt},
            ],
        }
        url = self._base_url + "/chat/completions"
        try:
            status, body = self._http(url, headers, payload, self._timeout)
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            raise PlannerUnavailable(f"fallback planner unavailable: {exc}") from exc

        if status != 200:
            raise PlannerUnavailable(
                f"fallback planner unavailable: HTTP {status}: {str(body)[:300]}"
            )

        try:
            envelope = json.loads(body)
            text = envelope["choices"][0]["message"]["content"]
            parsed = _extract_json_lazy(text)
        except Exception as exc:  # noqa: BLE001 - fail closed, never fabricate
            raise PlannerUnavailable(
                f"fallback planner unavailable: unparseable response: {exc}"
            ) from exc

        if not isinstance(parsed, dict):
            raise PlannerUnavailable("fallback planner unavailable: not a JSON object")
        return parsed

    @staticmethod
    def _urllib_http(
        url: str, headers: dict, payload: dict, timeout: float
    ) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def _stamp(self, plan: CheckpointPlan) -> CheckpointPlan:
        plan = plan.model_copy(deep=True)
        plan.planner = PlannerProvenance(
            role="fallback",
            provider="mimo",
            model=self._model,
            fallback_used=True,
            gate_template_version=plan.planner.gate_template_version or "1",
        )
        return plan


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_planner(
    *, preferred: CheckpointPlanner, fallback: CheckpointPlanner
) -> Callable[..., tuple[CheckpointPlan, bool]]:
    """Prefer the cloud planner; on unavailability retry EXACTLY once.

    Returns ``fn(*args, **kwargs) -> (plan, fallback_used)``. Any
    exception from the preferred planner (PlannerUnavailable or a
    provider/runtime error) triggers the single fallback attempt; errors
    from the fallback propagate unchanged.
    """

    def route(*args, **kwargs) -> tuple[CheckpointPlan, bool]:
        try:
            return preferred.create_plan(*args, **kwargs), False
        except PlannerUnavailable:
            return fallback.create_plan(*args, **kwargs), True
        except Exception:  # noqa: BLE001 - provider/runtime errors also route
            return fallback.create_plan(*args, **kwargs), True

    return route


# ---------------------------------------------------------------------------
# Deterministic complexity classification (no LLM, no Laya)
# ---------------------------------------------------------------------------


def classify_complexity(
    *,
    objective: str,
    files_touched: int,
    new_modules: int = 0,
    ui_surface: bool = False,
    public_api: bool = False,
) -> Complexity:
    """Closed, deterministic heuristics (order matters — first match wins).

    TRIVIAL:        files<=1, no ui, no public api, len(objective)<80
    SMALL:          files<=3 and not public_api
    FEATURE:        files<=8 or ui_surface or public_api
    LARGE_FEATURE:  anything else
    """
    objective = objective or ""
    if files_touched <= 1 and not ui_surface and not public_api \
            and len(objective) < 80:
        return Complexity.TRIVIAL
    if files_touched <= 3 and not public_api:
        return Complexity.SMALL
    if files_touched <= 8 or ui_surface or public_api:
        return Complexity.FEATURE
    return Complexity.LARGE_FEATURE


# ---------------------------------------------------------------------------
# Gate templates (canonical, deterministic)
# ---------------------------------------------------------------------------


def gate_templates(version: str = "1") -> dict:
    """Canonical gate-preseed templates keyed by gate kind.

    Each entry: ``required_when`` (deterministic predicate description),
    ``workflow``, ``reviewer``, ``fixer_policy``, ``defaults`` (a
    GateExpectations-shaped dict). G3's fixer is bounded at
    ``MAX_REVIEW_FIX_CYCLES`` cycles with an escalation state after
    exhaustion; G4 closes ONLY via human approval.
    """
    return {
        "version": version,
        "G1_behavior": {
            "required_when": "any behavioral change in scope",
            "workflow": (
                "tests-planned -> tests-written -> RED (recorded rationale "
                "required when a test is already green pre-implementation "
                "via red_exception) -> implementation -> GREEN -> "
                "deterministic acceptance"
            ),
            "reviewer": "deterministic test runner (exit-code evidence)",
            "fixer_policy": {
                "type": "bounded_repair",
                "max_cycles": MAX_REVIEW_FIX_CYCLES,
            },
            "defaults": {
                "required": True,
                "reason": None,
                "planned_tests": [],
                "focus": ["behavioral correctness", "regression safety"],
                "test_instructions": [
                    "write failing tests before implementation",
                    "run the exact acceptance commands recorded in the plan",
                ],
            },
        },
        "G2_ui": {
            "required_when": "ui_surface touched (design.md/prototype exists)",
            "workflow": "read-only visual review vs design.md / prototype",
            "reviewer": "read-only UI reviewer (no mutation tools)",
            "fixer_policy": {
                "type": "bounded_repair",
                "max_cycles": MAX_REVIEW_FIX_CYCLES,
            },
            "defaults": {
                "required": False,
                "reason": "no user-visible surface in this checkpoint",
                "planned_tests": [],
                "focus": ["visual fidelity vs design reference"],
                "test_instructions": [],
            },
        },
        "G3_adversarial": {
            "required_when": "always (adversarial review is mandatory)",
            "workflow": (
                "adversarial read-only reviewer vs bounded fixer; cycles "
                f"bounded at {MAX_REVIEW_FIX_CYCLES}, escalation state after "
                "exhaustion"
            ),
            "reviewer": "adversarial reviewer (read-only, structured findings)",
            "fixer_policy": {
                "type": "bounded_fixer",
                "max_cycles": MAX_REVIEW_FIX_CYCLES,
                "scope": "fixer writes only within checkpoint relevant_files",
                "escalation": "BLOCKED after cycle budget exhausted",
            },
            "defaults": {
                "required": True,
                "reason": None,
                "planned_tests": [],
                "focus": ["failure modes", "edge cases", "security"],
                "test_instructions": [],
            },
        },
        "G4_human": {
            "required_when": "always (human closes the checkpoint)",
            "workflow": "closes ONLY via G4Approval decision (human)",
            "reviewer": "human approver",
            "fixer_policy": {
                "type": "none",
                "note": (
                    "NEEDS_CHANGES routes to a structured follow-up (new "
                    "checkpoint or bounded repair), never raw forward"
                ),
            },
            "defaults": {
                "required": True,
                "reason": None,
                "planned_tests": [],
                "focus": ["acceptance of outcome by a human"],
                "test_instructions": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# Plain-language preview (MCP preview surface; rendered from a plan only)
# ---------------------------------------------------------------------------

_STATUS_MARKERS = {
    "PENDING": "[ ]",
    "ACTIVE": "[>]",
    "PASSED": "[x]",
    "REPAIR": "[!]",
    "REJECTED": "[r]",
}


def preview_checkpoint_plan(plan: CheckpointPlan) -> str:
    """Deterministic plain-language preview of a checkpoint plan.

    Header (complexity + objective), then per checkpoint (in execution
    order when the DAG allows it) one row per gate: REQUIRED or N/A with
    the given reason, plus the current status marker. Read-only render:
    no validation side effects beyond the embedded verdict.
    """
    from app.orchestration.checkpoint_planning.validation import (
        PlanValidationError,
        checkpoint_execution_order,
    )

    by_id = {cp.checkpoint_id: cp for cp in plan.checkpoints}
    try:
        order = checkpoint_execution_order(plan)
    except PlanValidationError:
        order = [cp.checkpoint_id for cp in plan.checkpoints]

    files = sum(len(cp.relevant_files) for cp in plan.checkpoints)
    ui = any(cp.gates.G2_ui.required for cp in plan.checkpoints)
    api = any(
        cp.gates.G1_behavior.required and cp.gates.G1_behavior.test_instructions
        for cp in plan.checkpoints
    )
    complexity = classify_complexity(
        objective=plan.objective, files_touched=files, ui_surface=ui, public_api=api
    )

    lines = [
        f"CHECKPOINT PLAN {plan.plan_id} — {complexity.value} (revision {plan.revision})",
        f"Objective: {plan.objective}",
        f"Planner: {plan.planner.provider} ({plan.planner.role}"
        f"{', FALLBACK' if plan.planner.fallback_used else ''})",
        f"Checkpoints: {len(plan.checkpoints)} | execution order: "
        + " → ".join(order),
        "",
    ]

    for cid in order:
        cp = by_id.get(cid)
        if cp is None:  # pragma: no cover - defensive
            continue
        lines.append(f"{cp.checkpoint_id.upper()}: {cp.title} [{cp.status.value}]")
        if cp.objective:
            lines.append(f"  objective: {cp.objective}")
        for kind, gate in cp.gates.iter_gates():
            record = cp.gate_records.get(kind)
            marker = _STATUS_MARKERS.get(
                record.status.value if record else "PENDING", "[ ]"
            )
            if gate.required:
                detail = "REQUIRED"
                if kind == "G1_behavior" and gate.planned_tests:
                    detail += f" ({len(gate.planned_tests)} planned tests)"
            else:
                detail = f"N/A — {gate.reason}"
            lines.append(f"  {marker} {kind}: {detail}")
        lines.append("")

    verdict = validate_checkpoint_plan(plan)
    if verdict.ok:
        lines.append("PREVIEW VERDICT: plan passes all validation checks.")
    else:
        lines.append(f"PREVIEW VERDICT: {len(verdict.errors)} validation error(s):")
        lines.extend(f"  - {err}" for err in verdict.errors)

    return "\n".join(lines).rstrip() + "\n"
