"""
Deterministic tests for the AGY DISPATCH-role seam (cases 16-19):
role binding through AgySettings/RoleModels, DISPATCH-only dispatch,
and no-live-network guarantees (fake Transport injected, urllib
blocked for the whole module).
"""

from __future__ import annotations

import asyncio
import json
import unittest
import urllib.request
from typing import cast

from app.config import (
    DEFAULT_DISPATCH_MODEL,
    DEFAULT_SUPERVISOR_MODEL,
    AgySettings,
)
from app.orchestration.agy_dispatch import (
    DISPATCH_SYSTEM,
    dispatch_stage,
)
from app.providers import (
    ModelRole,
    OpenAICompatibleClient,
    ProviderEndpoint,
    RoleModels,
)


BOUND_SUPERVISOR = "bound-supervisor-model"
BOUND_DISPATCH = "bound-dispatch-model"


# ---------------------------------------------------------------------------
# urllib guard: any attempt to reach the real network trips loudly
# ---------------------------------------------------------------------------

_urllib_visits: list[object] = []

_original_urlopen = urllib.request.urlopen


def _blocked_urlopen(*args, **kwargs):
    _urllib_visits.append(args)
    raise AssertionError(
        "live network use is forbidden in AGY P2 tests"
    )


def setUpModule() -> None:
    urllib.request.urlopen = _blocked_urlopen


def tearDownModule() -> None:
    urllib.request.urlopen = _original_urlopen


class FakeTransport:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self.body = body
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.status, self.body


def ok_body(model=BOUND_DISPATCH, text="stage implemented"):
    return json.dumps(
        {
            "model": model,
            "choices": [
                {"message": {"role": "assistant", "content": text}}
            ],
        }
    ).encode("utf-8")


def bound_settings(**env: str) -> AgySettings:
    payload = {
        "SUPERVISOR_MODEL": BOUND_SUPERVISOR,
        "DISPATCH_MODEL": BOUND_DISPATCH,
    }
    payload.update(env)
    return AgySettings.from_env(payload)


def build_client(
    transport: FakeTransport | None = None,
    *,
    models: RoleModels | None = None,
) -> OpenAICompatibleClient:
    endpoint = ProviderEndpoint(
        base_url="https://provider.example.invalid/v1",
        timeout=7.0,
    )
    return OpenAICompatibleClient(
        endpoint,
        models or RoleModels.for_agy(bound_settings()),
        transport=transport,
    )


class TestRoleBinding(unittest.TestCase):
    """Case 16: supervisor and dispatch roles are distinct."""

    def test_supervisor_and_dispatch_roles_bound_to_distinct_models(
        self,
    ):
        models = RoleModels.for_agy(
            AgySettings.from_env(
                {
                    "SUPERVISOR_MODEL": BOUND_SUPERVISOR,
                    "DISPATCH_MODEL": BOUND_DISPATCH,
                }
            )
        )

        supervisor = models.model_for(ModelRole.SUPERVISOR)
        dispatch = models.model_for(ModelRole.DISPATCH)

        self.assertNotEqual(supervisor, dispatch)
        self.assertEqual(supervisor, BOUND_SUPERVISOR)
        self.assertEqual(dispatch, BOUND_DISPATCH)

        # settings defaults (already bound in app/config) are distinct too
        defaults = RoleModels.for_agy(AgySettings.from_env({}))
        self.assertNotEqual(
            defaults.model_for(ModelRole.SUPERVISOR),
            defaults.model_for(ModelRole.DISPATCH),
        )
        self.assertEqual(
            defaults.model_for(ModelRole.SUPERVISOR),
            DEFAULT_SUPERVISOR_MODEL,
        )
        self.assertEqual(
            defaults.model_for(ModelRole.DISPATCH),
            DEFAULT_DISPATCH_MODEL,
        )


class TestDispatchStage(unittest.TestCase):
    """Cases 17-18: role gate and mocked-transport dispatch."""

    def test_dispatch_stage_rejects_non_dispatch_role(self):
        transport = FakeTransport(body=ok_body(text="must not run"))
        client = build_client(transport)

        with self.assertRaises(ValueError):
            asyncio.run(
                dispatch_stage(
                    client,
                    role=ModelRole.SUPERVISOR,
                    prompt="implement one bounded task",
                )
            )

        # rejected before any request was built
        self.assertEqual(transport.requests, [])
        self.assertEqual(_urllib_visits, [])

    def test_dispatch_stage_uses_dispatch_model_with_mocked_transport(
        self,
    ):
        transport = FakeTransport(
            body=ok_body(model=BOUND_DISPATCH, text="bounded done")
        )
        client = build_client(transport)

        prompt = "Implement bounded task alpha-t00 only."
        result = asyncio.run(
            dispatch_stage(
                client,
                role=ModelRole.DISPATCH,
                prompt=prompt,
            )
        )

        # exactly one request, to the chat endpoint, no network
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(_urllib_visits, [])

        request = transport.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(
            request.full_url.endswith("/chat/completions"),
            request.full_url,
        )

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], BOUND_DISPATCH)
        self.assertNotEqual(payload["model"], BOUND_SUPERVISOR)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": DISPATCH_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)

        self.assertEqual(result.text, "bounded done")
        self.assertEqual(result.model, BOUND_DISPATCH)
        self.assertEqual(transport.timeouts, [7.0])


class TestNoLiveNetwork(unittest.TestCase):
    """Case 19: no test in P2 ever reaches urllib / a live model."""

    def test_no_live_network_reaches_urllib(self):
        # A default client (no fake transport injected) would default
        # to the stdlib urllib transport...
        from app.providers import urllib_transport

        default_client = build_client()
        self.assertIs(default_client._transport, urllib_transport)

        # ...so the only way P2 dispatches is with an injected
        # transport (here: the explicit override), and even then the
        # urllib guard is never touched.
        transport = FakeTransport(
            body=ok_body(model=BOUND_DISPATCH, text="injected only")
        )
        result = asyncio.run(
            dispatch_stage(
                default_client,
                role=ModelRole.DISPATCH,
                prompt="Implement bounded task beta-t00 only.",
                transport=transport,
            )
        )

        self.assertEqual(result.text, "injected only")
        self.assertEqual(len(transport.requests), 1)
        self.assertIs(urllib.request.urlopen, _blocked_urlopen)
        self.assertEqual(_urllib_visits, [])

    def test_role_argument_rejects_string_denormalization(self):
        # dispatch_stage takes a ModelRole (identity-checked), not a
        # free-form role string.
        transport = FakeTransport(body=ok_body())
        client = build_client(transport)

        with self.assertRaises(ValueError):
            asyncio.run(
                dispatch_stage(
                    client,
                    role=cast(ModelRole, "not-a-role"),
                    prompt="must not dispatch",
                )
            )

        with self.assertRaises(ValueError):
            asyncio.run(
                dispatch_stage(
                    client,
                    role=cast(ModelRole, "dispatch"),
                    prompt="must not dispatch",
                )
            )

        self.assertEqual(transport.requests, [])
        self.assertEqual(_urllib_visits, [])


if __name__ == "__main__":
    unittest.main()