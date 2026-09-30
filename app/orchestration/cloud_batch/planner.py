"""P14 — Cloud planner adapter + prompt construction.

The cloud planner is Gemini inside the AGY CLI (same transport the P8
planner uses — one-shot ``agy -p``; OAuth lives in the CLI). P14 adds:

* a hard CORE_RULES prompt (the cloud model's only job is the four cloud
  modes — it is not a general operational agent),
* the compact role manifest (roles.py) instead of tool schemas,
* a structured :data:`CloudAction` parse that FAILS CLOSED on any
  malformed output or detected tool-event (Classification-B interim
  defense; see protocol.scan_for_tool_events),
* prompt byte budgets (schemas.*_MAX_BYTES) enforced before invocation,
* quota-tier transport routing: Gemini is the primary tier; on a 429
  RESOURCE_EXHAUSTED the tier is parked (persisted, until its advertised
  reset) and the Claude Opus fallback tier is tried for the SAME turn; a 503
  capacity error is transient (never persisted). Model switching is a
  transport decision only — prompt, protocol, and CloudAction are unchanged.

``app.agents.planner._extract_json`` is imported LAZILY (that module
pulls in the host google.antigravity runtime at import time) — the same
pattern the P8 planner uses. Nothing here imports the host runtime at
module top level.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import TypeAdapter

from app.orchestration.cloud_batch.protocol import (
    CloudProtocolError,
    CloudProtocolViolation,
    scan_for_tool_events,
)
from app.orchestration.cloud_batch.schemas import (
    BatchOutcome,
    BatchPlan,
    BATCH_PROMPT_MAX_BYTES,
    CloudAction,
    ClarificationRequest,
    CORE_RULES_MAX_BYTES,
    ProjectContract,
)


class CloudPlannerUnavailable(CloudProtocolError):
    """The cloud planner could not produce a usable action. Fail closed:
    a plan is never fabricated. Among transport failures a quota wall
    (429) dominates; :class:`CloudTierCapacityError` is the transient
    (503-only) variant. Both are CloudProtocolError, so the protocol
    handles them identically (state -> FAILED, no partial action)."""


class CloudTierCapacityError(CloudProtocolError):
    """No transport tier could serve the turn and NO quota wall was
    involved (every failure a transient 503) — retryable soon. When a
    quota wall is among the failures, :class:`CloudPlannerUnavailable`
    is raised instead (waiting a minute cannot fix it)."""


CLOUD_ACTION_ADAPTER = TypeAdapter(CloudAction)


# ---------------------------------------------------------------------------
# Quota-tier transport (fluent Gemini / Claude Opus fallback)
# ---------------------------------------------------------------------------

#: Priority-ordered cloud transport tiers. The Gemini flavor is the
#: primary planner transport; Claude Opus is the quota-fallback. Tiers are
#: AGY-CLI models, not new cloud plumbing — the same ``agy -p`` transport,
#: different ``--model`` (slugs from ``agy models``).
TIER_PRIORITY: list[str] = [
    "Gemini 3.6 Flash (Medium)",
    "claude-opus-4-6-thinking",
]


_QUOTA_RESET_RE = re.compile(r"Resets in (\d+)h(\d+)m(\d+)s")


def _stderr_has_quota_exhausted(stderr: str) -> bool:
    """429 RESOURCE_EXHAUSTED — a persistent per-tier quota wall."""
    return "RESOURCE_EXHAUSTED" in stderr or "Individual quota reached" in stderr


def _stderr_has_capacity(stderr: str) -> bool:
    """503 UNAVAILABLE / no capacity — transient, not exhaustion."""
    return (
        "UNAVAILABLE" in stderr
        or "No capacity available" in stderr
        or "high traffic" in stderr
    )


def _quota_reset_seconds(stderr: str) -> int | None:
    m = _QUOTA_RESET_RE.search(stderr)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


class QuotaRouter:
    """Priority-ordered transport-tier selection with quota awareness.

    * 429 RESOURCE_EXHAUSTED on a tier  -> the tier is marked exhausted
      UNTIL the advertised reset (persisted to ``quota.json`` under the
      state root, so a process restart does not re-probe the dead tier).
    * 503 UNAVAILABLE on a tier         -> transient: skip it for the
      CURRENT call only (in-memory; a success on any tier clears the
      set). Never persisted.
    * No healthy tier                   -> CloudPlannerUnavailable
      (fail closed; a plan is never fabricated).
    """

    def __init__(
        self,
        tiers: list[str] | None = None,
        *,
        state_root: Path | None = None,
        project_id: str | None = None,
    ) -> None:
        self.tiers = list(tiers) if tiers else list(TIER_PRIORITY)
        self._state_root = state_root
        self._project_id = project_id
        self._exhausted_until: dict[str, float] = {}
        self._transiently_down: set[str] = set()
        if state_root is not None and project_id is not None:
            self._load()

    # -- persistence -----------------------------------------------------

    def _path(self) -> Path | None:
        if self._state_root is None or self._project_id is None:
            return None
        return self._state_root / "projects" / self._project_id / "quota.json"

    def _load(self) -> None:
        path = self._path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return  # corrupt quota state must not block the planner
        for model, until in (data.get("exhausted_until") or {}).items():
            try:
                until_f = float(until)
            except (TypeError, ValueError):
                continue
            if until_f > time.time():
                self._exhausted_until[model] = until_f
            # Expired entries are dropped (quota reset -> tier healthy).

    def _persist(self) -> None:
        path = self._path()
        if path is None:
            return
        try:
            now = time.time()
            payload = {
                "exhausted_until": {
                    m: u for m, u in self._exhausted_until.items() if u > now
                },
                "tier_priority": self.tiers,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except OSError:
            # Persistence failure degrades to in-memory routing only.
            pass

    # -- routing ----------------------------------------------------------

    def tier_status(self) -> list[dict[str, Any]]:
        now = time.time()
        out: list[dict[str, Any]] = []
        for model in self.tiers:
            until = self._exhausted_until.get(model, 0.0)
            if until > now:
                status = "exhausted"
            elif model in self._transiently_down:
                status = "down"
            else:
                status = "healthy"
            out.append(
                {
                    "model": model,
                    "status": status,
                    "exhausted_until": until if until > now else None,
                }
            )
        return out

    def next_model(self) -> str:
        for model in self.tiers:
            until = self._exhausted_until.get(model, 0.0)
            if until > time.time() or model in self._transiently_down:
                continue
            return model
        raise CloudPlannerUnavailable(
            "cloud planner unavailable: all transport tiers exhausted "
            f"or down: {self.tiers}"
        )

    def record_exhausted(self, model: str, reset_seconds: int | None) -> None:
        # Absent advertised reset: assume 1h, fail safe toward re-probe.
        self._exhausted_until[model] = time.time() + float(
            reset_seconds if reset_seconds is not None else 3600
        )
        self._transiently_down.discard(model)
        self._persist()

    def record_capacity(self, model: str) -> None:
        self._transiently_down.add(model)

    def record_success(self, model: str) -> None:
        # Any success clears transient down state; a 200 from a tier also
        # means its quota is NOT actually exhausted (defensive: the 429
        # message may have been a shared-account race).
        self._transiently_down.discard(model)
        self._exhausted_until.pop(model, None)
        self._persist()

#: Core system rules for the project-level cloud planner. Hard budget:
#: CORE_RULES_MAX_BYTES (tested). The cloud model is a PLANNER ONLY.
CORE_RULES = """\
You are the AGY project-level CLOUD PLANNER. You plan in batches; you never execute.

HARD RULES:
- You may respond with EXACTLY ONE JSON object, one of four actions:
  CLARIFY | BATCH | NEEDS_HUMAN | FINAL. Nothing else.
- You MUST NOT edit files, run repository shell commands, commit, invoke
  workers, or perform any operational action. You have no tools.
- You MUST NOT manage todo/progress state. Progress is derived
  deterministically by the AGY runtime from worker states.
- You know worker ROLES, not tools. Roles come from the ROLE MANIFEST.
- CLARIFY is only for a brand-new project and only for USER-INTENT
  questions (desired result, scope, priority, architecture preference,
  tradeoffs, autonomy, deployment, must-preserve, must-not-change,
  acceptance criteria). Never ask for facts workers can investigate.
- After you emit a BATCH, you go DORMANT. The deterministic dispatcher
  runs the workers. You are invoked again at most ONCE at the reentry
  gate with a compact BatchOutcome.
- At reentry you choose: another BATCH, NEEDS_HUMAN (park for the human),
  or FINAL (project done).
- Every BATCH must be bounded: unique delegation ids, known roles,
  acyclic deps, owned_files per delegation (1-3), a reentry gate.

ACTION SHAPES (field rules are strict — a plan that violates them is
rejected and the turn fails; do not approximate):
- owned_files: 1-3 repo-relative file paths the delegation CREATES OR
  MODIFIES — never empty. Read-only work may list the file it reads or
  documents. verification: 1+ deterministic shell commands.
- reentry.reason: EXACTLY ONE literal, never prose:
  "ALL_REQUIRED_DELEGATIONS_COMPLETE" (normal completion) — other
  literals are reserved for failure/supervisor paths.

CLARIFY: {"mode":"CLARIFY","questions":[...],"reasoning":"..."}
BATCH: {"mode":"BATCH","batch_id":"...","objective":"...",
  "delegations":[{"id":"...","role":"...","objective":"...",
    "owned_files":["a.py"],"deps":[],"reads":[],"invariants":[...],
    "verification":["python -m pytest tests/ -q"]}],
  "parallelism":N,"reentry":{"reason":"ALL_REQUIRED_DELEGATIONS_COMPLETE",
  "required_ids":[]}}
NEEDS_HUMAN: {"mode":"NEEDS_HUMAN","question":"...","context":"...",
  "options":[...]}
FINAL: {"mode":"FINAL","success":true,"summary":"...","artifacts":[...]}

Return EXACTLY ONE JSON object and nothing else.
"""

_MODE_TO_ACTION = {
    "CLARIFY": ClarificationRequest,
    "BATCH": BatchPlan,
}


def core_rules_within_budget() -> bool:
    return len(CORE_RULES.encode("utf-8")) <= CORE_RULES_MAX_BYTES


def _check_prompt_budget(name: str, prompt: str) -> str:
    size = len(prompt.encode("utf-8"))
    if size > BATCH_PROMPT_MAX_BYTES:
        raise CloudProtocolError(
            f"{name} prompt is {size} bytes, exceeds budget "
            f"{BATCH_PROMPT_MAX_BYTES} — trim context, do not send"
        )
    return prompt


def build_clarify_prompt(contract: ProjectContract) -> str:
    """CLARIFY turn: initial prompt + intent-only question contract."""
    prompt = "\n\n".join(
        [
            CORE_RULES,
            "STATE: NEW (first turn for this project). You must emit "
            "CLARIFY now — user-intent questions only.",
            "ROLE MANIFEST:\n" + _role_manifest_section(),
            "INITIAL USER PROMPT:\n" + contract.initial_prompt.strip(),
        ]
    )
    return _check_prompt_budget("clarify", prompt)


def build_batch_prompt(
    contract: ProjectContract,
    *,
    role_manifest: str,
    durable_state: dict | None = None,
) -> str:
    """BATCH turn: full ProjectContract (original prompt + clarification
    answer + durable state + compact role manifest)."""
    parts = [
        CORE_RULES,
        "STATE: READY_TO_PLAN. You must emit exactly one BATCH now.",
        "ROLE MANIFEST:\n" + role_manifest,
        "PROJECT:\n"
        + json.dumps(
            {
                "project_id": contract.project_id,
                "initial_prompt": contract.initial_prompt,
                "clarification_answer": contract.clarification_answer,
                "objective": contract.objective,
                "base_sha": contract.base_sha,
            },
            sort_keys=True,
            indent=2,
            default=str,
        ),
    ]
    if durable_state:
        parts.append(
            "DURABLE PROJECT STATE (compact):\n"
            + json.dumps(durable_state, sort_keys=True, indent=2, default=str)
        )
    return _check_prompt_budget("batch", "\n\n".join(parts))


def build_reentry_prompt(
    contract: ProjectContract,
    outcome: BatchOutcome,
    role_manifest: str,
) -> str:
    """Reentry turn: the ONE compact BatchOutcome is the whole context
    the cloud sees (no transcripts, no logs, no raw evidence)."""
    prompt = "\n\n".join(
        [
            CORE_RULES,
            "STATE: REENTRY_READY. Choose exactly one action: BATCH | "
            "NEEDS_HUMAN | FINAL.",
            "ROLE MANIFEST:\n" + role_manifest,
            "PROJECT OBJECTIVE:\n" + contract.objective.strip(),
            "BATCH OUTCOME (compact, bounded):\n"
            + json.dumps(outcome.model_dump(mode="json"), sort_keys=True, indent=2),
        ]
    )
    return _check_prompt_budget("reentry", prompt)


def _role_manifest_section() -> str:
    # Lazy import: avoids a circular import at module load.
    from app.orchestration.cloud_batch.roles import render_role_manifest

    return render_role_manifest()


# ---------------------------------------------------------------------------
# Structured output parsing (fail closed)
# ---------------------------------------------------------------------------


def _extract_json_lazy(text: str) -> Any:
    """``app.agents.planner._extract_json`` — imported lazily at call
    time (that module imports the host google.antigravity runtime)."""
    from app.agents.planner import _extract_json as planner_extract

    return planner_extract(text)


def parse_cloud_action(raw_text: str) -> CloudAction:
    """Parse ONE raw cloud response into a CloudAction. Fail closed.

    Order of defense: (1) tool-event scan on the raw text, (2) JSON
    extraction, (3) strict discriminated-union validation. Any failure
    raises CloudProtocolError (never returns a partial action).
    """
    if raw_text is None or not raw_text.strip():
        raise CloudProtocolViolation("cloud response is empty")
    events = scan_for_tool_events(raw_text)
    if events:
        raise CloudProtocolViolation(
            "protocol violation: tool-use event detected in cloud "
            f"response: {events[:3]}"
        )
    try:
        payload = _extract_json_lazy(raw_text)
    except Exception as exc:  # noqa: BLE001 - boundary, fail closed
        raise CloudProtocolViolation(
            f"unparseable cloud output: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise CloudProtocolViolation("cloud output is not a JSON object")
    try:
        return CLOUD_ACTION_ADAPTER.validate_python(payload)
    except Exception as exc:  # noqa: BLE001 - boundary, fail closed
        raise CloudProtocolViolation(f"invalid cloud action: {exc}") from exc


# ---------------------------------------------------------------------------
# Production adapter: Gemini inside the AGY CLI (one-shot `agy -p`),
# routed across quota tiers
# ---------------------------------------------------------------------------

AGY_CLI_TIMEOUT_SECONDS = 600


def agy_cli_invoke(prompt: str, *, model: str | None = None) -> str:
    """One one-shot ``agy -p`` call. The CLI is the cloud connection
    (OAuth lives in the CLI profile); no credential touches this
    process. Raises CloudPlannerUnavailable with the raw stderr on a
    non-zero exit (the tier router classifies it).

    P17: this is the RESTRICTED transport (classification B — tool
    schemas remain visible to the model; the CLI soft-denies calls at
    execution). It is no longer the default planner transport; use it
    only via ``CloudPlanner(transport="agy_cli")`` when the tool-free
    transport has no credentials configured.
    """
    cmd = ["agy", f"-p={prompt}"]
    if model:
        cmd += ["--model", model, "--print-timeout", "300s"]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=AGY_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CloudPlannerUnavailable(
            "cloud planner unavailable: agy CLI not found"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: agy CLI timed out after "
            f"{AGY_CLI_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise CloudPlannerUnavailable(f"cloud planner unavailable: {exc}") from exc
    if completed.returncode != 0:
        raise CloudPlannerUnavailable(
            f"agy CLI exited {completed.returncode} for model "
            f"{model or '<default>'}: {completed.stderr.strip()[:400]}"
        )
    return completed.stdout


def default_agy_invoke(prompt: str) -> str:
    """Legacy single-tier default (the CLI's own default model)."""
    return agy_cli_invoke(prompt)


# ---------------------------------------------------------------------------
# P17 — tool-free planner transport (classification A)
# ---------------------------------------------------------------------------
#
# The google-genai SDK's ``models.generate_content`` is a PLAIN, non-agentic
# completion call: its signature is (model, contents, config) and there is
# no tools parameter, no MCP surface, no filesystem, and no shell — the
# operational execution surface does not exist, it is not merely disabled
# by config (unlike the AGY CLI, where tool schemas stay visible and calls
# are soft-denied at execution — classification B). Tool isolation here is
# a STRUCTURAL property of the API surface, proven by inspect in the P17
# tests.
#
# The tool-free chain is Gemini/Vertex-ONLY: every tier in
# :data:`TOOL_FREE_TIER_PRIORITY` is served by the same tool-free call, so
# the quota fallback retains the same restriction (a tool-bearing tier
# cannot enter the list without changing its transport, which this module
# refuses by construction — ``routed_tool_free_invoke`` never references
# the CLI transport). The P14 CLI chain (Gemini + Opus, classification B)
# remains available ONLY as the explicit restricted/experimental
# ``transport="agy_cli"`` opt-in, never as the default.
#
# Credentials: the Developer API key comes from ``GEMINI_API_KEY`` (or an
# explicit ``api_key`` argument); Vertex ADC is picked up automatically by
# the SDK. There is NO interactive OAuth on this path. When no credential
# is configured every tier fails closed (CloudPlannerUnavailable) — the
# planner never falls through to a tool-bearing transport silently.

#: Tool-free (classification A) tier slugs: google-genai model IDs, NOT
#: AGY-CLI slugs. All tiers share the same tool-free transport; ordering
#: is priority (index 0 = primary). The fallback is a LARGER/FASTER Gemini
#: flavor — never a tool-bearing model, per the P17 fallback rule.
TOOL_FREE_TIER_PRIORITY: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

#: The single sanctioned tool-free call surface. P17 tests assert
#: structurally (via inspect) that this call has no tools/MCP parameters.
_GENAI_CALL_PARAMS: tuple[str, ...] = ("model", "contents", "config")


def _genai_generate_content(
    prompt: str, *, model: str, api_key: str | None
) -> str:
    """ONE plain google-genai completion call — no tools, no MCP, no
    filesystem, no shell. Returns the raw text (the CloudAction JSON the
    prompt builder demanded). Raises :class:`CloudPlannerUnavailable`
    with the provider error text embedded so the tier router's existing
    string classifiers (``RESOURCE_EXHAUSTED`` / ``UNAVAILABLE`` /
    ``Resets in ...``) classify it unchanged.

    Imported lazily (like the P14 ``_extract_json`` pattern): importing
    ``google.genai`` at module top level would couple the whole
    orchestration package to the SDK import cost.
    """
    try:
        from google import genai  # lazy, per the module-import pattern
        from google.genai import errors as genai_errors
    except ImportError as exc:
        raise CloudPlannerUnavailable(
            "cloud planner unavailable: google-genai SDK not installed "
            f"({exc})"
        ) from exc
    try:
        client = genai.Client(api_key=api_key)  # None -> GEMINI_API_KEY / ADC
        resp = client.models.generate_content(model=model, contents=prompt)
    except genai_errors.APIError as exc:
        # str(APIError) embeds "<code> <STATUS>. <response json>" — the
        # quota/capacity classifiers match it verbatim.
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: genai transport error for model "
            f"{model}: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - adapter boundary, fail closed
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: genai transport failure for model "
            f"{model}: {exc}"
        ) from exc
    text = getattr(resp, "text", None)
    if text is None or not str(text).strip():
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: genai returned no text for model "
            f"{model} (candidates={len(getattr(resp, 'candidates', []) or [])})"
        )
    return str(text)


def genai_tool_free_invoke(
    prompt: str,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> str:
    """One tool-free completion (the P17 transport). Matches the router
    contract of :func:`agy_cli_invoke`: single prompt in, raw text out,
    keyword-only ``model``, :class:`CloudPlannerUnavailable` with the
    provider error text on any failure (the tier router classifies it).
    """
    return _genai_generate_content(
        prompt, model=model or TOOL_FREE_TIER_PRIORITY[0], api_key=api_key
    )


def routed_tool_free_invoke(
    router: QuotaRouter | None = None, *, api_key: str | None = None
) -> Callable[[str], str]:
    """Build the PRODUCTION invoke (P17 default): one tool-free genai
    completion per attempt, trying tiers in :data:`TOOL_FREE_TIER_PRIORITY`
    order.

    * quota exhaustion (429) on a tier  -> parked (persisted) until its
      advertised reset, next (also tool-free) tier tried for the SAME turn;
    * no capacity (503) on a tier       -> transient, next tier tried;
    * every tier fails                  -> CloudPlannerUnavailable
      (fail closed — a plan is never fabricated, and a missing credential
      never degrades into a tool-bearing transport).

    ISOLATION INVARIANT (P17): this builder's ONLY transport is
    :func:`genai_tool_free_invoke` (looked up as a module global at call
    time, mirroring the monkey-patch surface tests 27–30 rely on on the
    CLI path). It never references ``agy_cli_invoke`` — the fallback
    transport cannot silently gain tools.
    """
    r = router if router is not None else QuotaRouter()
    # ISOLATION INVARIANT (P17): the tool-free router may only ever carry
    # tool-free (Gemini/Vertex plain-completion) tiers. If a caller hands
    # in a router pre-configured with the CLI chain (e.g. a bare
    # QuotaRouter(), which defaults to TIER_PRIORITY and carries the
    # tool-bearing Opus slug), override it to the tool-free list so the
    # fallback can NEVER silently reach a tool surface. An explicit
    # custom tiers list is trusted (tests / a future Gemini-only set).
    if r.tiers != list(TOOL_FREE_TIER_PRIORITY):
        r.tiers = list(TOOL_FREE_TIER_PRIORITY)

    def invoke(prompt: str) -> str:
        last_error: str | None = None
        tried: list[str] = []
        while True:
            try:
                model = r.next_model()
            except CloudPlannerUnavailable:
                status = {s["model"]: s["status"] for s in r.tier_status()}
                only_down = all(v == "down" for v in status.values())
                detail = (
                    f"tool-free transport tiers unavailable: {status} "
                    f"(tried {tried}; last error: {last_error})"
                )
                if only_down:
                    raise CloudTierCapacityError(
                        "cloud planner unavailable (transient): " + detail
                    ) from None
                raise CloudPlannerUnavailable(
                    "cloud planner unavailable: " + detail
                ) from None
            tried.append(model)
            try:
                raw = genai_tool_free_invoke(
                    prompt, model=model, api_key=api_key
                )
            except CloudPlannerUnavailable as exc:
                msg = str(exc)
                last_error = msg
                if _stderr_has_quota_exhausted(msg):
                    r.record_exhausted(model, _quota_reset_seconds(msg))
                    continue
                if _stderr_has_capacity(msg):
                    r.record_capacity(model)
                    continue
                raise
            r.record_success(model)
            return raw
        # Unreachable: the loop either returns or raises.
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: no tool-free tier succeeded: "
            f"{last_error} (tried {tried})"
        )

    return invoke


def routed_agy_invoke(router: QuotaRouter | None = None) -> Callable[[str], str]:
    """Build the production invoke: one ``agy -p`` per attempt, trying
    tiers in :data:`TIER_PRIORITY` order.

    * quota exhaustion (429) on a tier  -> tier parked until its
      advertised reset (persisted), next tier tried for the SAME turn;
    * no capacity (503) on a tier       -> transient, next tier tried;
      never persisted;
    * first success wins                -> router state cleared for it;
    * every tier fails                  -> CloudPlannerUnavailable
      (fail closed; the turn is NOT retried silently by the planner).

    Model switching is a TRANSPORT decision only — it never changes the
    prompt, the expected CloudAction, or the protocol.
    """
    r = router or QuotaRouter()

    def invoke(prompt: str) -> str:
        last_error: str | None = None
        tried: list[str] = []
        while True:
            try:
                model = r.next_model()
            except CloudPlannerUnavailable:
                # No tier can serve this turn. Distinguish WHY: if every
                # failure was a transient capacity one (no quota wall),
                # report a capacity error (retryable soon); otherwise a
                # quota wall dominates (not retryable before reset).
                status = {s["model"]: s["status"] for s in r.tier_status()}
                only_down = all(v == "down" for v in status.values())
                detail = (
                    f"transport tiers unavailable: {status} (tried {tried}; "
                    f"last error: {last_error})"
                )
                if only_down:
                    raise CloudTierCapacityError(
                        "cloud planner unavailable (transient): " + detail
                    ) from None
                raise CloudPlannerUnavailable(
                    "cloud planner unavailable: " + detail
                ) from None
            tried.append(model)
            try:
                raw = agy_cli_invoke(prompt, model=model)
            except CloudPlannerUnavailable as exc:
                msg = str(exc)
                last_error = msg
                if _stderr_has_quota_exhausted(msg):
                    r.record_exhausted(model, _quota_reset_seconds(msg))
                    continue
                if _stderr_has_capacity(msg):
                    r.record_capacity(model)
                    continue
                # A non-quota failure on the current model is a genuine
                # planner error — fail closed without touching other
                # tiers or retrying the turn.
                raise
            r.record_success(model)
            return raw
        # Unreachable: the loop either returns or raises.
        raise CloudPlannerUnavailable(
            f"cloud planner unavailable: no tier succeeded: {last_error} "
            f"(tried {tried})"
        )

    return invoke


class CloudPlanner:
    """Production project-level cloud planner (Gemini-in-AGY-CLI).

    ``invoke`` is injectable for tests; the default is the quota-routed
    AGY-CLI transport (:func:`routed_agy_invoke` over :data:`TIER_PRIORITY`).
    ``quota_router`` (optional) carries the per-project persisted quota
    state so a process restart does not re-probe an exhausted tier.
    Every method parses through :func:`parse_cloud_action` and narrows
    to the expected leaf type — a mode mismatch is a CloudProtocolViolation,
    never coerced.
    """

    def __init__(
        self,
        invoke: Callable[[str], str] | None = None,
        *,
        quota_router: QuotaRouter | None = None,
        transport: str | None = None,
    ) -> None:
        # P17 transport selection (production default is TOOL-FREE):
        #   "tool_free" (default)  -> routed_tool_free_invoke over
        #      TOOL_FREE_TIER_PRIORITY (google-genai plain completion;
        #      classification A; Gemini/Vertex only; Gemini-preferred
        #      quota parking retained).
        #   "agy_cli" (restricted) -> routed_agy_invoke over TIER_PRIORITY
        #      (the P14 classification-B CLI transport, including the
        #      Opus fallback). Explicitly experimental — it keeps tool
        #      schemas visible to the model; choose it only when no
        #      tool-free credential is configured.
        if invoke is not None:
            self._invoke = invoke
            self.quota_router = None
        else:
            self.quota_router = quota_router
            mode = transport or "tool_free"
            if mode == "tool_free":
                self._invoke = routed_tool_free_invoke(quota_router)
            elif mode == "agy_cli":
                self._invoke = routed_agy_invoke(quota_router)
            else:
                raise ValueError(
                    f"unknown planner transport {mode!r} "
                    "(expected 'tool_free' or 'agy_cli')"
                )

    def _call(self, prompt: str) -> CloudAction:
        try:
            raw = self._invoke(prompt)
        except CloudProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            raise CloudPlannerUnavailable(f"cloud planner unavailable: {exc}") from exc
        return parse_cloud_action(raw)

    def clarify(self, prompt: str) -> ClarificationRequest:
        action = self._call(prompt)
        if not isinstance(action, ClarificationRequest):
            raise CloudProtocolViolation(
                f"clarify expected CLARIFY, got {type(action).__name__}"
            )
        return action

    def batch(self, prompt: str) -> BatchPlan:
        action = self._call(prompt)
        if not isinstance(action, BatchPlan):
            raise CloudProtocolViolation(
                f"batch expected BATCH, got {type(action).__name__}"
            )
        return action

    def reentry(self, prompt: str) -> CloudAction:
        return self._call(prompt)


__all__ = [
    "CORE_RULES",
    "CLOUD_ACTION_ADAPTER",
    "CloudPlannerUnavailable",
    "CloudPlanner",
    "default_agy_invoke",
    "build_clarify_prompt",
    "build_batch_prompt",
    "build_reentry_prompt",
    "parse_cloud_action",
    "core_rules_within_budget",
    "AGY_CLI_TIMEOUT_SECONDS",
]
