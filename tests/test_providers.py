"""
Deterministic tests for the model/provider abstraction with
mocked HTTP (injected transport; no network).
"""

import json
import unittest
import urllib.request
from typing import cast

from app.config import AgySettings
from app.providers import (
    ChatMessage,
    ModelRole,
    OpenAICompatibleClient,
    ProviderEndpoint,
    ProviderError,
    RoleModels,
)


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


def ok_body(model="alias-mimo-v2.6-pro", text="hello"):
    return json.dumps(
        {
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": text}}],
        }
    ).encode("utf-8")


def client(transport, *, api_key=None, models=None):
    return OpenAICompatibleClient(
        ProviderEndpoint(
            base_url="https://provider.example.invalid/v1",
            api_key=api_key,
            timeout=42.0,
        ),
        models
        or RoleModels(
            supervisor_model="alias-mimo-v2.6-pro",
            dispatch_model="alias-mimo-v2.6-flash",
        ),
        transport=transport,
    )


class TestRoleBinding(unittest.TestCase):
    def test_agy_settings_bind_helmholtz_models(self):
        settings = AgySettings.from_env({})
        models = RoleModels.for_agy(settings)

        self.assertEqual(models.model_for(ModelRole.SUPERVISOR), "alias-mimo-v2.6-pro")
        self.assertEqual(models.model_for(ModelRole.DISPATCH), "alias-mimo-v2.6-flash")

    def test_unknown_role_rejected(self):
        models = RoleModels(supervisor_model="s", dispatch_model="d")
        with self.assertRaises(ValueError):
            models.model_for(cast(ModelRole, "nemotron"))


class TestChatCompletion(unittest.TestCase):
    def test_supervisor_role_request_shape(self):
        transport = FakeTransport(body=ok_body())
        result = client(transport).complete(
            ModelRole.SUPERVISOR,
            [
                ChatMessage("system", "plan"),
                {"role": "user", "content": "stage 1"},
            ],
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(
            request.full_url.endswith("/chat/completions"),
            request.full_url,
        )
        self.assertIsNone(request.get_header("Authorization"))

        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "alias-mimo-v2.6-pro")
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "plan"},
                {"role": "user", "content": "stage 1"},
            ],
        )
        self.assertNotIn("temperature", payload)
        self.assertNotIn("max_tokens", payload)

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.model, "alias-mimo-v2.6-pro")
        self.assertEqual(transport.timeouts, [42.0])

    def test_dispatch_role_uses_dispatch_model(self):
        transport = FakeTransport(
            body=ok_body(model="alias-mimo-v2.6-flash", text="done")
        )
        result = client(transport).complete(
            ModelRole.DISPATCH,
            [ChatMessage("user", "implement stage")],
            temperature=0.0,
            max_tokens=256,
        )

        payload = json.loads(transport.requests[0].data.decode("utf-8"))
        self.assertEqual(payload["model"], "alias-mimo-v2.6-flash")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["max_tokens"], 256)
        self.assertEqual(result.text, "done")

    def test_bearer_auth_only_when_key_configured(self):
        transport = FakeTransport(body=ok_body())
        client(transport, api_key="s3cret").complete(
            ModelRole.SUPERVISOR, [ChatMessage("user", "x")]
        )
        self.assertEqual(
            transport.requests[0].get_header("Authorization"),
            "Bearer s3cret",
        )

    def test_http_error_raises_provider_error(self):
        transport = FakeTransport(status=503, body=b"boom")
        with self.assertRaises(ProviderError) as ctx:
            client(transport).complete(
                ModelRole.SUPERVISOR, [ChatMessage("user", "x")]
            )
        self.assertEqual(ctx.exception.status, 503)

    def test_malformed_payload_raises(self):
        transport = FakeTransport(body=b'{"choices": []}')
        with self.assertRaises(ProviderError):
            client(transport).complete(
                ModelRole.DISPATCH, [ChatMessage("user", "x")]
            )

    def test_transport_exception_wrapped(self):
        def broken(request, timeout):
            raise OSError("connection refused")

        with self.assertRaises(ProviderError):
            client(broken).complete(
                ModelRole.SUPERVISOR, [ChatMessage("user", "x")]
            )

    def test_no_network_transport_used_in_tests(self):
        # Guard: every test above injects a transport; urllib is
        # never reached because requests == captured fakes only.
        transport = FakeTransport(body=ok_body())
        client(transport).complete(ModelRole.SUPERVISOR, [])
        self.assertEqual(len(transport.requests), 1)
        self.assertIsInstance(transport.requests[0], urllib.request.Request)


if __name__ == "__main__":
    unittest.main()