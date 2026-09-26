"""
Deterministic, network-free tests for the AGY gatekeeper decision
step (MiMo Pro supervisor role).

The provider is faked two ways the contract allows: an injected
``client_factory`` returning a duck-typed fake client (with the
``with_models`` construction seam ``complete_with_failover`` uses for
its fallback) and, for the default-client path, a scripted transport
on a real ``OpenAICompatibleClient``. No network anywhere.

Covered: ACCEPT on all-green evidence, fail-closed parsing (invalid
JSON / non-enum decision / non-object payload -> REJECT with
``gatekeeper response invalid``), ``deterministic_failures`` embedded
in the evidence prompt, role=SUPERVISOR binding, model identity
recorded (including on a synthetic transient failover to the
fallback model), rationale/repair_targets normalization.

NOTE: forcing a model ACCEPT down to REPAIR when deterministic checks
failed lives in ``execute_stage`` (contract §5/§9) — see
``tests/test_agy_execute.py``; this file pins that the evidence
``deterministic_failures`` field is populated and serialized here.
"""

import json
import sys
import types
import unittest

import jsonschema

from app.config import AgySettings
from app.orchestration.agy_gatekeeper import (
    SUPERVISOR_GATE_SYSTEM,
    build_gate_evidence,
    review_stage,
)
from app.providers import (
    ChatResult,
    ModelRole,
    OpenAICompatibleClient,
    ProviderError,
    ProviderEndpoint,
    RoleModels,
)

SCHEMA_PATH = (
    "/home/leon/antigravity-hybrid/state/autopilot/"
    "agy-failover-state.schema.json"
)


def install_host_runtime_stub() -> None:
    """
    ``app.agents.planner`` (owner of ``_extract_json``, imported
    lazily by the gatekeeper) needs the host-provided
    ``google.antigravity`` runtime at import time; it is absent
    outside the host. Same minimal stub as
    ``tests/test_agy_development_plan.py``. No execution of anything.
    """
    if "google.antigravity" in sys.modules:
        return

    try:
        import importlib.util

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


class FakeGateClient:
    """
    Duck-typed OpenAI-compatible client: each ``complete`` consumes
    the next scripted outcome (str = response text, Exception =
    raised) and records the role it was called with. ``with_models``
    is the fallback-construction seam ``complete_with_failover``
    uses; the script position is SHARED across primary/fallback so
    call counting stays exact.
    """

    def __init__(self, models, script, calls=None, position=None):
        self.models = models
        self.script = list(script)
        self.calls = calls if calls is not None else []
        self._position = position if position is not None else {"i": 0}

    def with_models(self, models):
        return FakeGateClient(
            models, self.script, self.calls, self._position
        )

    def complete(self, role, messages, *, temperature=None, max_tokens=None):
        index = self._position["i"]
        self._position["i"] = index + 1
        model = self.models.model_for(role)
        self.calls.append(
            {
                "role": role,
                "model": model,
                "message_count": len(list(messages)),
                "messages": list(messages),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )

        outcome = self.script[min(index, len(self.script) - 1)]

        if isinstance(outcome, Exception):
            raise outcome

        text = json.dumps(outcome) if isinstance(outcome, dict) else outcome
        return ChatResult(model=model, text=text, raw={})


def accept_payload():
    return {
        "decision": "ACCEPT",
        "rationale": "All deterministic checks are green and the "
        "change covers the stage objective.",
        "repair_targets": [],
    }


def green_evidence():
    return {
        "plan_id": "agp-gate-test",
        "stage_id": "multiply",
        "stage_objective": "Implement multiply(a, b)",
        "tasks": [
            {
                "worker_task_id": "multiply-t00",
                "attempt": 1,
                "status": "SUCCESS",
                "validation_ok": True,
            }
        ],
        "verification": {"multiply-t00": {"validation_ok": True}},
        "changed_files": ["calculator.py"],
        "scope_violations": [],
        "acceptance": {"ok": True, "checks": []},
        "cumulative": {"ok": True, "checks": []},
        "commit": {"commit_sha": "a" * 40},
        "invariants": ["add(a, b) still returns a + b"],
        "deterministic_failures": [],
    }


def run(coro):
    import asyncio

    return asyncio.run(coro)


class TestReviewStage(unittest.TestCase):
    def setUp(self):
        self.settings = AgySettings.from_env({})
        self.calls = []

    def review(self, client, evidence, on_failover_event=None):
        return run(
            review_stage(
                evidence=evidence,
                settings=self.settings,
                client_factory=lambda models: client,
                on_failover_event=on_failover_event,
            )
        )

    def test_accept_on_all_green_evidence(self):
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            [accept_payload()],
            calls=self.calls,
        )
        result = self.review(client, green_evidence())

        self.assertEqual(result["decision"], "ACCEPT")
        self.assertIn("deterministic checks are green", result["rationale"])
        self.assertEqual(result["repair_targets"], [])
        self.assertIs(result["deterministic_override"], False)
        self.assertIsNone(result["failover_event"])
        # Model identity recorded: which model actually answered.
        self.assertEqual(result["model"], "alias-mimo-v2.6-pro")

        # Role binding: this is the SUPERVISOR role, never dispatch.
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0]["role"], ModelRole.SUPERVISOR)
        self.assertEqual(
            self.calls[0]["model"],
            self.settings.supervisor_model,
        )

        system_message, user_message = self.calls[0]["messages"]
        self.assertEqual(system_message.content, SUPERVISOR_GATE_SYSTEM)
        self.assertEqual(system_message.role, "system")
        self.assertIn("agp-gate-test", user_message.content)
        self.assertIn("deterministic_failures", user_message.content)
        self.assertTrue(user_message.content.startswith("EVIDENCE:"))

    def test_invalid_json_fails_closed_as_reject(self):
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            ["this is definitely not JSON {oops"],
            calls=self.calls,
        )
        result = self.review(client, green_evidence())

        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["rationale"], "gatekeeper response invalid")
        self.assertEqual(result["repair_targets"], [])
        self.assertIn("parse_detail", result)
        self.assertIs(result["deterministic_override"], False)

    def test_non_enum_decision_fails_closed_as_reject(self):
        for payload in (
            {"decision": "APPROVE", "rationale": "sounds good"},
            {"decision": "accept"},  # wrong case is still non-enum
            {"decision": 42},
            {"rationale": "no decision at all"},
        ):
            with self.subTest(payload=payload):
                client = FakeGateClient(
                    RoleModels.for_agy(self.settings),
                    [json.dumps(payload)],
                    calls=self.calls,
                )
                result = self.review(client, green_evidence())
                self.assertEqual(result["decision"], "REJECT")
                self.assertEqual(
                    result["rationale"], "gatekeeper response invalid"
                )

    def test_non_object_payload_fails_closed(self):
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            ['["ACCEPT", "REJECT"]'],
            calls=self.calls,
        )
        result = self.review(client, green_evidence())
        self.assertEqual(result["decision"], "REJECT")
        self.assertEqual(result["rationale"], "gatekeeper response invalid")

    def test_deterministic_failures_are_embedded_in_the_evidence(self):
        """
        Contract: build_gate_evidence embeds the caller-computed
        'deterministic_failures' list; here we pin that it survives
        to the model prompt, INDEPENDENT of decision forcing (which
        lives in execute_stage).
        """
        evidence = green_evidence()
        failure = (
            "stage acceptance failed: ut exited 1, expected 0"
        )
        evidence["deterministic_failures"] = [failure]

        serialized = build_gate_evidence(evidence)
        parsed = json.loads(serialized)
        self.assertEqual(parsed["deterministic_failures"], [failure])
        self.assertEqual(json.loads(serialized), parsed)  # deterministic

        # Default: key always present, empty when green.
        missing_key = green_evidence()
        del missing_key["deterministic_failures"]
        self.assertEqual(
            json.loads(build_gate_evidence(missing_key))[
                "deterministic_failures"
            ],
            [],
        )

        # Model says ACCEPT despite the failure; this layer records
        # it verbatim with override False — execute_stage forces it.
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            [accept_payload()],
            calls=self.calls,
        )
        result = self.review(client, evidence)
        self.assertEqual(result["decision"], "ACCEPT")
        self.assertIs(result["deterministic_override"], False)
        self.assertIn(
            failure, self.calls[0]["messages"][1].content
        )

    def test_repair_targets_normalized(self):
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            [
                {
                    "decision": "REPAIR",
                    "rationale": "  ",
                    "repair_targets": ["fix unittest exit 1", 7, ""],
                },
                {
                    "decision": "REPAIR",
                    "rationale": "fix it",
                    "repair_targets": "not-a-list",
                },
            ],
            calls=self.calls,
        )

        first = self.review(client, green_evidence())
        self.assertEqual(first["decision"], "REPAIR")
        # Blank rationale -> honest normalization, not fabricated praise.
        self.assertEqual(first["rationale"], "gatekeeper returned no rationale")
        self.assertEqual(first["repair_targets"], ["fix unittest exit 1", "7"])

        second = self.review(client, green_evidence())
        self.assertEqual(second["repair_targets"], [])

    def test_transient_failover_serves_the_gate(self):
        """
        Contract §5: complete_with_failover on role=SUPERVISOR, the
        model identity recorded is the one that actually answered,
        and the event is forwarded EXACTLY once.
        """
        transient = ProviderError(
            "provider returned HTTP 429: rate limit",
            status=429,
            body="rate limit",
        )
        client = FakeGateClient(
            RoleModels.for_agy(self.settings),
            [transient, transient, transient, transient, accept_payload()],
            calls=self.calls,
        )
        events = []

        result = self.review(
            client, green_evidence(), on_failover_event=events.append
        )

        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(len(events), 1)
        self.assertEqual(result["failover_event"], events[0])
        self.assertEqual(len(self.calls), 5)  # 4 primary + 1 fallback
        self.assertEqual(
            self.calls[-1]["model"], "alias-qwen3.8-27b"
        )
        self.assertIs(self.calls[-1]["role"], ModelRole.SUPERVISOR)

        # Model identity: after failover the fallback answered.
        self.assertEqual(result["model"], "alias-qwen3.8-27b")
        self.assertEqual(events[0]["role"], "supervisor")
        self.assertEqual(events[0]["primary_attempts"], 4)

        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            schema = json.load(handle)
        filled = dict(events[0], session_id="ses_providerlayerpending")
        jsonschema.validate([filled], schema)

    def test_default_client_path_with_scripted_transport(self):
        """No client_factory -> real client over the injected transport."""
        seen = []

        def transport(request, timeout):
            payload = json.loads(request.data.decode("utf-8"))
            seen.append(payload)
            body = {
                "model": payload["model"],
                "choices": [
                    {"message": {"content": json.dumps(accept_payload())}}
                ],
            }
            return 200, json.dumps(body).encode("utf-8")

        result = run(
            review_stage(
                evidence=green_evidence(),
                settings=self.settings,
                transport=transport,
            )
        )

        self.assertEqual(result["decision"], "ACCEPT")
        self.assertEqual(result["model"], "alias-mimo-v2.6-pro")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model"], "alias-mimo-v2.6-pro")
        self.assertIn("messages", seen[0])


if __name__ == "__main__":
    unittest.main()