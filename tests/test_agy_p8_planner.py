"""P8 worker A — planner adapter tests (contract items 22, 25-27 + preview).

22  both adapters produce the identical CheckpointPlan schema from the
    same provider JSON (pipeline shared; provenance differs only)
25  any adapter failure (CLI nonzero/missing/timeout/unparseable,
    provider HTTP error) raises PlannerUnavailable — never a fabricated
    plan
26  route_planner retries the fallback EXACTLY once on unavailability
27  gate_templates canonical shape; MAX_REVIEW_FIX_CYCLES == 3

Plus: preview_checkpoint_plan plain-language rendering and the shared
prompt contract. All deterministic: every transport is injected.
"""

import importlib.util
import json
import sys
import types
import unittest
import urllib.error
import urllib.request


def install_host_runtime_stub() -> None:
    """Same host-runtime stub as the P2/P3 tests (lazy-import pattern)."""
    if "google.antigravity" in sys.modules:
        return
    try:
        spec = importlib.util.find_spec("google.antigravity")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is not None:
        return
    import google

    module = types.ModuleType("google.antigravity")
    module.Agent = object
    module.LocalOpenAIAgentConfig = object
    module.types = types.SimpleNamespace()
    sys.modules["google.antigravity"] = module
    google.antigravity = module  # type: ignore[attr-defined]


install_host_runtime_stub()

from app.orchestration.checkpoint_planning.planner import (  # noqa: E402
    MAX_REVIEW_FIX_CYCLES,
    GeminiCloudPlanner,
    MiMoProFallbackPlanner,
    build_planning_prompt,
    classify_complexity,
    gate_templates,
    preview_checkpoint_plan,
    route_planner,
)
from app.orchestration.checkpoint_planning.schemas import (  # noqa: E402
    AnalysisEvidence,
    CheckpointPlan,
    PlannerUnavailable,
)
from app.orchestration.checkpoint_planning.validation import (  # noqa: E402
    validate_checkpoint_plan,
)


def plan_payload(plan_id="plan-1", revision=1):
    def gate(required=True, reason=None, tests=None):
        return {
            "required": required,
            "reason": reason,
            "planned_tests": tests if tests is not None else (["test_a"] if required else []),
            "focus": [],
            "test_instructions": [],
        }

    return {
        "plan_id": plan_id,
        "feature_id": "feat-1",
        "objective": "add checkpoint planning",
        "planner": {
            "role": "cloud",
            "provider": "gemini",
            "model": None,
            "fallback_used": False,
            "gate_template_version": "1",
        },
        "checkpoints": [
            {
                "checkpoint_id": "cp-1",
                "title": "schemas",
                "objective": "domain models",
                "dependencies": [],
                "implementation_context": "",
                "relevant_files": ["app/x.py"],
                "stages": ["s1"],
                "gates": {
                    "G1_behavior": gate(),
                    "G2_ui": gate(False, "no UI"),
                    "G3_adversarial": gate(),
                    "G4_human": gate(),
                },
                # Planner CLAIMS gates already passed — submission discards.
                "gate_records": {},
                "repair_lineage": [],
            }
        ],
        "created_at": "2026-09-28T00:00:00+00:00",
        "revision": revision,
        "design_context_refs": [],
        "learning_refs": [],
        "analyses": [],
    }


CREATION_KW = dict(
    context={"repo": "agy-hybrid"},
    analyses=[AnalysisEvidence(summary="evidence")],
    gate_templates=gate_templates(),
    request="build checkpoint planning",
)


class GeminiCloudPlannerTests(unittest.TestCase):
    # -- 22 ------------------------------------------------------------------
    def test_parses_provider_json_into_checkpoint_plan(self):
        payload = plan_payload()
        planner = GeminiCloudPlanner(
            invoke=lambda **kw: payload, model="gemini-forename"
        )
        plan = planner.create_plan(**CREATION_KW)
        self.assertIsInstance(plan, CheckpointPlan)
        self.assertEqual(plan.planner.provider, "gemini")
        self.assertEqual(plan.planner.role, "cloud")
        self.assertFalse(plan.planner.fallback_used)
        self.assertEqual(plan.planner.model, "gemini-forename")
        self.assertTrue(validate_checkpoint_plan(plan).ok)

    def test_prompt_contains_shared_rules_and_inputs(self):
        seen = {}

        def invoke(*, prompt, **kw):
            seen["prompt"] = prompt
            return plan_payload()

        GeminiCloudPlanner(invoke=invoke).create_plan(**CREATION_KW)
        prompt = seen["prompt"]
        self.assertIn("MUST NOT edit files", prompt)
        self.assertIn("agy_checkpoint_plan_submit", prompt)
        self.assertIn("build checkpoint planning", prompt)
        self.assertIn("GATE TEMPLATES", prompt)

    def test_fenced_json_output_accepted_via_extract_json(self):
        fenced = "```json\n" + json.dumps(plan_payload()) + "\n```"
        planner = GeminiCloudPlanner(
            invoke=lambda **kw: _as_dict_from_text(fenced)
        )
        plan = planner.create_plan(**CREATION_KW)
        self.assertEqual(plan.plan_id, "plan-1")

    # -- 25: fail-closed -------------------------------------------------------
    def test_unparseable_output_raises_planner_unavailable(self):
        planner = GeminiCloudPlanner(invoke=lambda **kw: "not a json object")
        with self.assertRaises(PlannerUnavailable):
            planner.create_plan(**CREATION_KW)

    def test_schema_violation_raises_planner_unavailable(self):
        bad = plan_payload()
        bad["checkpoints"][0]["checkpoint_id"] = "CP-1!!"
        planner = GeminiCloudPlanner(invoke=lambda **kw: bad)
        with self.assertRaises(PlannerUnavailable):
            planner.create_plan(**CREATION_KW)

    def test_invoke_exception_raises_planner_unavailable(self):
        def boom(**kw):
            raise RuntimeError("CSRF token required")

        with self.assertRaises(PlannerUnavailable):
            GeminiCloudPlanner(invoke=boom).create_plan(**CREATION_KW)

    def test_default_invoke_missing_cli_raises_planner_unavailable(self):
        # No subprocess is spawned here: monkeypatch subprocess.run in
        # the planner module to simulate a missing binary.
        import app.orchestration.checkpoint_planning.planner as planner_mod

        original = planner_mod.subprocess.run

        def missing(*args, **kwargs):
            raise FileNotFoundError(2, "agy")

        planner_mod.subprocess.run = missing
        try:
            with self.assertRaises(PlannerUnavailable) as ctx:
                GeminiCloudPlanner().create_plan(**CREATION_KW)
            self.assertIn("unavailable", str(ctx.exception))
        finally:
            planner_mod.subprocess.run = original

    def test_revise_plan_roundtrips(self):
        payload = plan_payload(revision=2)
        planner = GeminiCloudPlanner(invoke=lambda **kw: payload)
        first = planner.create_plan(**CREATION_KW)
        revised = planner.revise_plan(plan=first, evidence={"q": "1"}, reason="more tests")
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.planner.provider, "gemini")


def _as_dict_from_text(text):
    from app.agents.planner import _extract_json

    return _extract_json(text)


class MiMoFallbackPlannerTests(unittest.TestCase):
    # -- 22: identical pipeline, provenance differs only ----------------------
    def _http_ok(self, payload):
        def http(url, headers, body, timeout):
            return (
                200,
                json.dumps(
                    {"choices": [{"message": {"content": json.dumps(payload)}}]}
                ),
            )

        return http

    def test_same_schema_different_provenance(self):
        payload = plan_payload()
        gem = GeminiCloudPlanner(invoke=lambda **kw: dict(payload)).create_plan(**CREATION_KW)
        mimo = MiMoProFallbackPlanner(
            base_url="https://helmholtz.invalid/v1",
            model="mimo-pro",
            http=self._http_ok(dict(payload)),
        ).create_plan(**CREATION_KW)

        g = gem.model_dump(mode="json")
        m = mimo.model_dump(mode="json")
        self.assertEqual(
            {k: v for k, v in g.items() if k != "planner"},
            {k: v for k, v in m.items() if k != "planner"},
        )
        self.assertEqual(mimo.planner.provider, "mimo")
        self.assertEqual(mimo.planner.role, "fallback")
        self.assertTrue(mimo.planner.fallback_used)
        self.assertTrue(validate_checkpoint_plan(mimo).ok)

    def test_request_uses_json_object_response_format(self):
        captured = {}

        def http(url, headers, body, timeout):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            return (
                200,
                json.dumps(
                    {"choices": [{"message": {"content": json.dumps(plan_payload())}}]}
                ),
            )

        MiMoProFallbackPlanner(
            base_url="https://x.invalid/v1/", model="mimo-pro", api_key="k", http=http
        ).create_plan(**CREATION_KW)
        self.assertEqual(captured["url"], "https://x.invalid/v1/chat/completions")
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(captured["headers"]["Authorization"], "Bearer k")

    # -- 25 ---------------------------------------------------------------------
    def test_http_error_raises_planner_unavailable(self):
        planner = MiMoProFallbackPlanner(
            base_url="https://x.invalid/v1",
            model="mimo-pro",
            http=lambda *a: (503, "overloaded"),
        )
        with self.assertRaises(PlannerUnavailable):
            planner.create_plan(**CREATION_KW)

    def test_transport_exception_raises_planner_unavailable(self):
        def boom(*a):
            raise urllib.error.URLError("connection refused")

        planner = MiMoProFallbackPlanner(
            base_url="https://x.invalid/v1", model="mimo-pro", http=boom
        )
        with self.assertRaises(PlannerUnavailable):
            planner.create_plan(**CREATION_KW)

    def test_garbage_payload_raises_planner_unavailable(self):
        planner = MiMoProFallbackPlanner(
            base_url="https://x.invalid/v1",
            model="mimo-pro",
            http=lambda *a: (200, json.dumps({"choices": [{"message": {"content": "nope"}}]})),
        )
        with self.assertRaises(PlannerUnavailable):
            planner.create_plan(**CREATION_KW)


class _StubPlanner:
    def __init__(self, *, raises=None, plan=None):
        self.raises = raises
        self.plan = plan
        self.calls = 0

    def create_plan(self, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.plan

    def revise_plan(self, **kw):  # pragma: no cover
        return self.plan


class RoutePlannerTests(unittest.TestCase):
    # -- 26 ---------------------------------------------------------------------
    def test_preferred_success_no_fallback(self):
        good = _StubPlanner(plan=_make_plan())
        fb = _StubPlanner(plan=_make_plan("fb"))
        plan, used = route_planner(preferred=good, fallback=fb)(**CREATION_KW)
        self.assertFalse(used)
        self.assertEqual(good.calls, 1)
        self.assertEqual(fb.calls, 0)

    def test_unavailable_retries_fallback_exactly_once(self):
        bad = _StubPlanner(raises=PlannerUnavailable("agy offline"))
        fb = _StubPlanner(plan=_make_plan("plan-fb"))
        plan, used = route_planner(preferred=bad, fallback=fb)(**CREATION_KW)
        self.assertTrue(used)
        self.assertEqual(plan.plan_id, "plan-fb")
        self.assertEqual(bad.calls, 1)
        self.assertEqual(fb.calls, 1)

    def test_provider_error_also_routes(self):
        bad = _StubPlanner(raises=RuntimeError("provider exploded"))
        fb = _StubPlanner(plan=_make_plan("plan-fb"))
        plan, used = route_planner(preferred=bad, fallback=fb)(**CREATION_KW)
        self.assertTrue(used)

    def test_fallback_failure_propagates(self):
        bad = _StubPlanner(raises=PlannerUnavailable("no cloud"))
        worse = _StubPlanner(raises=PlannerUnavailable("no fallback"))
        with self.assertRaises(PlannerUnavailable):
            route_planner(preferred=bad, fallback=worse)(**CREATION_KW)


def _make_plan(plan_id="plan-cloud"):
    from app.orchestration.checkpoint_planning.planner import _payload_to_plan

    return _payload_to_plan(plan_payload(plan_id))


class GateTemplateTests(unittest.TestCase):
    # -- 27 ---------------------------------------------------------------------
    def test_canonical_shape(self):
        templates = gate_templates()
        self.assertEqual(templates["version"], "1")
        self.assertEqual(MAX_REVIEW_FIX_CYCLES, 3)
        for kind in ("G1_behavior", "G2_ui", "G3_adversarial", "G4_human"):
            tpl = templates[kind]
            for key in ("required_when", "workflow", "reviewer", "fixer_policy", "defaults"):
                self.assertIn(key, tpl, f"{kind} missing {key}")
        self.assertIn("tests-planned", templates["G1_behavior"]["workflow"])
        self.assertEqual(
            templates["G3_adversarial"]["fixer_policy"]["max_cycles"], 3
        )
        self.assertIn("read-only", templates["G3_adversarial"]["reviewer"])
        self.assertIn("human", templates["G4_human"]["reviewer"].lower())
        self.assertEqual(gate_templates("2")["version"], "2")

    def test_defaults_are_gate_expectations_shaped(self):
        from app.orchestration.checkpoint_planning.schemas import GateExpectations

        templates = gate_templates()
        for kind in ("G1_behavior", "G2_ui", "G3_adversarial", "G4_human"):
            expectations = GateExpectations(**templates[kind]["defaults"])
            self.assertEqual(expectations.required, templates[kind]["defaults"]["required"])


class PreviewTests(unittest.TestCase):
    def test_preview_renders_plain_language(self):
        from app.orchestration.checkpoint_planning.planner import _payload_to_plan

        plan = _payload_to_plan(plan_payload())
        text = preview_checkpoint_plan(plan)
        self.assertIn("CHECKPOINT PLAN plan-1", text)
        self.assertIn("CP-1:", text)
        self.assertIn("G1_behavior: REQUIRED (1 planned tests)", text)
        self.assertIn("G2_ui: N/A — no UI", text)
        self.assertIn("passes all validation checks", text)

    def test_preview_reports_validation_errors(self):
        from app.orchestration.checkpoint_planning.planner import _payload_to_plan

        bad = plan_payload()
        bad["checkpoints"][0]["gates"]["G1_behavior"]["planned_tests"] = []
        plan = _payload_to_plan(bad)
        text = preview_checkpoint_plan(plan)
        self.assertIn("validation error", text)
        self.assertIn("planned_tests", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
