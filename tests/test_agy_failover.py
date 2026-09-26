"""
Deterministic, network-free tests for the P3 failover runtime.

Every provider interaction goes through a real
``OpenAICompatibleClient`` bound to a SCRIPTED transport — no
network, no real sleeps (the backoff sleeper is injected and counted).

Covered per the P3 contract:
* ``is_transient_provider_failure`` for 429/500/502/503/504/524/
  auth-401/422/status-None message patterns; non-transient never
  fails over;
* ``classify_reason`` for all four enum values;
* ``complete_with_failover``: primary success (no event), 4 transient
  failures then fallback success (event field-for-field), fast
  non-transient re-raise (ZERO fallback calls), fallback failure after
  exactly 4+1 calls, ``on_event`` exactly once, injectable sleep
  (nothing really sleeps), role symmetry for SUPERVISOR (contract
  case 19).
"""

import json
import unittest
from datetime import datetime

import jsonschema

from app.config import AgySettings
from app.orchestration.failover import (
    PRIMARY_BACKOFF_SECONDS,
    PRIMARY_MAX_ATTEMPTS,
    classify_reason,
    complete_with_failover,
    is_transient_provider_failure,
)
from app.providers import (
    ChatResult,
    ModelRole,
    OpenAICompatibleClient,
    ProviderEndpoint,
    ProviderError,
    RoleModels,
)

SCHEMA_PATH = (
    "/home/leon/antigravity-hybrid/state/autopilot/"
    "agy-failover-state.schema.json"
)


# ---------------------------------------------------------------------------
# Scripted-transport helpers
# ---------------------------------------------------------------------------


def _ok(payload, _calls, text="{{\"ok\": true}}"):
    return 200, json.dumps(
        {
            "model": payload["model"],
            "choices": [{"message": {"content": text}}],
        }
    )


def _status(code):
    def handler(payload, calls):
        return code, json.dumps(
            {"error": {"message": f"simulated HTTP {code}"}}
        )

    return handler


def scripted_transport(script, calls):
    """
    Transport resolving responses from ``script`` (a list of
    ``(payload, calls) -> (status, body_str)`` handlers, last one
    sticky) while recording every outgoing payload in ``calls``.
    """

    def transport(request, timeout):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(payload)
        handler = script[min(len(calls) - 1, len(script) - 1)]
        status, body = handler(payload, len(calls))
        return status, body.encode("utf-8")

    return transport


def make_client(settings, script, calls):
    return OpenAICompatibleClient(
        ProviderEndpoint.for_agy(settings),
        RoleModels.for_agy(settings),
        transport=scripted_transport(script, calls),
    )


def run(coro):
    import asyncio

    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestTransientClassification(unittest.TestCase):
    def assert_transient(self, err):
        self.assertTrue(is_transient_provider_failure(err), str(err))

    def assert_not_transient(self, err):
        self.assertFalse(is_transient_provider_failure(err), str(err))

    def test_transient_status_codes(self):
        for status in (429, 502, 503, 504, 520, 524, 529):
            self.assert_transient(
                ProviderError(f"HTTP {status}", status=status)
            )

    def test_non_transient_status_codes(self):
        for status in (400, 401, 403, 404, 422, 500, 501, 505):
            self.assert_not_transient(
                ProviderError(f"HTTP {status}", status=status)
            )

    def test_status_none_message_patterns(self):
        cases = [
            "rate limit exceeded, retry later",
            "Too Many Requests: rate limited",
            "quota exceeded for this month",
            "Provider is overloaded",
            "insufficient capacity right now",
            "server busy, try again",
            "please retry: too many requests",
        ]
        for message in cases:
            self.assert_transient(ProviderError(message, status=None))

    def test_status_none_non_matching_messages(self):
        cases = [
            "connection reset by peer",
            "name or service not known",
            "provider request failed: certificate verify failed",
            "",
        ]
        for message in cases:
            self.assert_not_transient(ProviderError(message, status=None))

    def test_malformed_200_payload_is_not_transient(self):
        # A 200-with-bad-body error carries status 200: deterministic
        # payload problem, never a failover trigger — even if the
        # text happens to contain a transient word.
        self.assert_not_transient(
            ProviderError(
                "provider returned an invalid payload: rate limit?",
                status=200,
                body="rate limit?",
            )
        )


class TestClassifyReason(unittest.TestCase):
    def test_all_four_enum_values(self):
        self.assertEqual(
            classify_reason(429, None), "rate_limit_exhausted"
        )
        self.assertEqual(
            classify_reason(None, "Rate limit reached"), "rate_limit_exhausted"
        )
        self.assertEqual(classify_reason(None, "quota 429?"), "quota_exhausted")
        self.assertEqual(classify_reason(503, None), "provider_overload")
        self.assertEqual(classify_reason(521, None), "provider_overload")
        self.assertEqual(
            classify_reason(None, "server busy"), "transient_busy"
        )

    def test_quota_word_wins_over_rate_status(self):
        self.assertEqual(
            classify_reason(429, "quota exhausted"), "quota_exhausted"
        )

    def test_output_always_in_enum(self):
        from app.orchestration.failover import FAILOVER_REASONS

        for status, body in [
            (429, None),
            (None, "rate"),
            (None, "quota"),
            (503, None),
            (None, "overload"),
            (None, "capacity"),
            (None, "server busy"),
        ]:
            self.assertIn(classify_reason(status, body), FAILOVER_REASONS)


# ---------------------------------------------------------------------------
# complete_with_failover
# ---------------------------------------------------------------------------


class TestCompleteWithFailover(unittest.TestCase):
    def setUp(self):
        self.settings = AgySettings.from_env({})
        self.calls = []
        self.events = []

    def test_primary_success_has_no_event(self):
        client = make_client(self.settings, [lambda p, n: _ok(p, n)], self.calls)

        result, event = run(
            complete_with_failover(
                client,
                role=ModelRole.DISPATCH,
                messages=[{"role": "user", "content": "hi"}],
                settings=self.settings,
                on_event=self.events.append,
            )
        )

        self.assertIsNone(event)
        self.assertEqual(self.events, [])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            self.calls[0]["model"], "alias-mimo-v2.6-flash"
        )
        self.assertIsInstance(result, ChatResult)

    def test_four_transient_failures_then_fallback_succeeds(self):
        def handler(payload, calls):
            if calls <= PRIMARY_MAX_ATTEMPTS:
                return _status(429)(payload, calls)
            return _ok(payload, calls)

        client = make_client(self.settings, [handler], self.calls)
        sleeps = []

        result, event = run(
            complete_with_failover(
                client,
                role=ModelRole.DISPATCH,
                messages=[{"role": "user", "content": "hi"}],
                settings=self.settings,
                on_event=self.events.append,
            )
        )

        # Exactly 4 primary + 1 fallback calls.
        self.assertEqual(len(self.calls), PRIMARY_MAX_ATTEMPTS + 1)
        self.assertEqual(len(self.events), 1)
        self.assertIs(self.events[0], event)

        self.assertEqual(event["role"], "dispatch")
        self.assertEqual(
            event["primary_model"], "alias-mimo-v2.6-flash"
        )
        self.assertEqual(
            event["fallback_model"], "alias-qwen3.8-flash-next"
        )
        self.assertEqual(
            event["active_model"], "alias-qwen3.8-flash-next"
        )
        self.assertEqual(event["primary_attempts"], 4)
        self.assertEqual(event["fallback_reason"], "rate_limit_exhausted")
        self.assertIsNone(event["session_id"])  # OpenCode layer fills it
        self.assertEqual(event["fallback_count"], 1)
        self.assertIsNone(event["parent_session_id"])

        ts = datetime.fromisoformat(event["ts"])
        self.assertIsNotNone(ts.tzinfo)

        # Session-less layer check: schema (string ^ses_) is only
        # satisfied once the OpenCode layer sets session_id; everything
        # else must already validate.
        filled = dict(event, session_id="ses_filledbyopencodelayer")
        with open(SCHEMA_PATH, encoding="utf-8") as handle:
            schema = json.load(handle)
        jsonschema.validate([filled], schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate([event], schema)

        # Fallback actually served the call.
        self.assertEqual(
            self.calls[-1]["model"], "alias-qwen3.8-flash-next"
        )
        self.assertIsInstance(result, ChatResult)

        # Retry sleep injectable: nothing slept for real by default,
        # and an injected sleeper is called between retries only.
        # (Fresh client/transport so the script restarts from 429s.)
        sleep_calls = []
        sleep_client = make_client(
            self.settings, [handler], sleep_calls
        )
        run(
            complete_with_failover(
                sleep_client,
                role=ModelRole.DISPATCH,
                messages=[{"role": "user", "content": "again"}],
                settings=self.settings,
                sleep=sleeps.append,
            )
        )
        self.assertEqual(
            sleeps,
            [
                PRIMARY_BACKOFF_SECONDS * 1,
                PRIMARY_BACKOFF_SECONDS * 2,
                PRIMARY_BACKOFF_SECONDS * 3,
            ],
        )

    def test_non_transient_failure_raises_with_zero_failover(self):
        client = make_client(self.settings, [_status(401)], self.calls)

        with self.assertRaises(ProviderError):
            run(
                complete_with_failover(
                    client,
                    role=ModelRole.DISPATCH,
                    messages=[{"role": "user", "content": "hi"}],
                    settings=self.settings,
                    on_event=self.events.append,
                )
            )

        self.assertEqual(len(self.calls), 1)  # no retry, no fallback
        self.assertEqual(self.events, [])

    def test_fallback_failure_raises_after_exactly_4_plus_1(self):
        client = make_client(self.settings, [_status(429)], self.calls)

        with self.assertRaises(ProviderError) as ctx:
            run(
                complete_with_failover(
                    client,
                    role=ModelRole.DISPATCH,
                    messages=[{"role": "user", "content": "hi"}],
                    settings=self.settings,
                    on_event=self.events.append,
                )
            )

        self.assertEqual(len(self.calls), PRIMARY_MAX_ATTEMPTS + 1)
        self.assertEqual(self.calls[-1]["model"], "alias-qwen3.8-flash-next")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(self.events, [])  # failed fallback -> no event

    def test_generic_500_does_not_fail_over_either(self):
        # 500 is deliberately NOT in the transient set: it is not a
        # classified failover trigger.
        client = make_client(self.settings, [_status(500)], self.calls)

        with self.assertRaises(ProviderError):
            run(
                complete_with_failover(
                    client,
                    role=ModelRole.DISPATCH,
                    messages=[{"role": "user", "content": "hi"}],
                    settings=self.settings,
                )
            )
        self.assertEqual(len(self.calls), 1)

    def test_role_symmetry_supervisor_failover(self):
        """Contract case 19: same behavior for ModelRole.SUPERVISOR."""
        supervisor_calls = []

        def handler(payload, calls):
            if calls <= PRIMARY_MAX_ATTEMPTS:
                return _status(503)(payload, calls)
            return _ok(payload, calls)

        client = make_client(self.settings, [handler], supervisor_calls)
        events = []

        result, event = run(
            complete_with_failover(
                client,
                role=ModelRole.SUPERVISOR,
                messages=[{"role": "system", "content": "gate"}],
                settings=self.settings,
                on_event=events.append,
            )
        )

        self.assertEqual(len(supervisor_calls), PRIMARY_MAX_ATTEMPTS + 1)
        for payload in supervisor_calls[:PRIMARY_MAX_ATTEMPTS]:
            self.assertEqual(payload["model"], "alias-mimo-v2.6-pro")
        self.assertEqual(
            supervisor_calls[-1]["model"], "alias-qwen3.8-27b"
        )

        self.assertEqual(event["role"], "supervisor")
        self.assertEqual(event["primary_model"], "alias-mimo-v2.6-pro")
        self.assertEqual(event["fallback_model"], "alias-qwen3.8-27b")
        self.assertEqual(event["fallback_reason"], "provider_overload")
        self.assertEqual(event["primary_attempts"], 4)
        self.assertIsNone(event["session_id"])
        self.assertEqual(len(events), 1)
        self.assertIsInstance(result, ChatResult)

        # Distinctness: the supervisor's bindings never leak into the
        # dispatch role's bindings (and vice versa).
        self.assertNotEqual(event["primary_model"], "alias-mimo-v2.6-flash")
        self.assertNotEqual(
            event["fallback_model"], "alias-qwen3.8-flash-next"
        )

    def test_no_real_sleeping_by_default(self):
        """The default sleeper is a no-op (tests never block)."""
        from app.orchestration.failover import _noop_sleep

        self.assertIsNone(_noop_sleep(99.0))


if __name__ == "__main__":
    unittest.main()