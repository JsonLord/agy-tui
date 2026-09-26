"""
Deterministic tests for the Needle3 routing seam with mocked
HTTP (injected transport; no network, no MiniCPM/tool-repair).
"""

import json
import unittest

from app.integrations.needle import (
    MAX_CANDIDATE_TOOLS,
    NeedleToolRouter,
    NeedleUnavailable,
    RouteOutcome,
    RoutingValidationError,
    ToolSetRejected,
    route_or_none,
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


SELECTED_BODY = json.dumps(
    {
        "outcome": "selected",
        "function_calls": [
            {
                "name": "read_file",
                "arguments": {"path": "app/main.py"},
            }
        ],
        "suppressed_calls": [{"name": "write_file"}],
        "confidence": 0.91,
        "reasoning": "inspection fits read_file",
        "validation": {"ok": True},
        "model": "needle3",
    }
).encode("utf-8")


def router(transport, *, api_key=None):
    return NeedleToolRouter(
        "https://needle.example.invalid",
        api_key=api_key,
        timeout=7.0,
        transport=transport,
    )


def tools(*names):
    return [{"name": n, "description": n} for n in names]


class TestRouteRequest(unittest.TestCase):
    def test_selected_outcome_and_payload_shape(self):
        transport = FakeTransport(body=SELECTED_BODY)
        decision = router(transport).route(
            "inspect app/main.py",
            tools("read_file", "write_file"),
            system="be precise",
        )

        self.assertTrue(decision.selected)
        self.assertFalse(decision.abstained)
        self.assertEqual(decision.outcome, RouteOutcome.SELECTED)
        self.assertEqual(len(decision.function_calls), 1)
        self.assertEqual(decision.function_calls[0].name, "read_file")
        self.assertEqual(
            decision.function_calls[0].arguments, {"path": "app/main.py"}
        )
        self.assertEqual(decision.suppressed_calls[0]["name"], "write_file")
        self.assertEqual(decision.confidence, 0.91)
        self.assertEqual(decision.validation, {"ok": True})
        self.assertEqual(decision.model, "needle3")

        request = transport.requests[0]
        self.assertTrue(request.full_url.endswith("/v1/route"))
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["input"], "inspect app/main.py")
        self.assertEqual(
            payload["tools"], tools("read_file", "write_file")
        )
        self.assertEqual(payload["system"], "be precise")
        self.assertEqual(transport.timeouts, [7.0])

    def test_system_omitted_when_absent(self):
        transport = FakeTransport(
            body=b'{"outcome": "abstain", "function_calls": [],'
            b' "suppressed_calls": [], "confidence": null,'
            b' "reasoning": "no fit", "validation": null,'
            b' "model": "needle3"}'
        )
        decision = router(transport).route("do nothing", tools("bash"))
        self.assertTrue(decision.abstained)
        self.assertEqual(decision.outcome, RouteOutcome.ABSTAIN)
        self.assertIsNone(decision.confidence)
        self.assertNotIn("system", json.loads(transport.requests[0].data))

    def test_confirm_outcome_is_distinct(self):
        transport = FakeTransport(
            body=b'{"outcome": "confirm", "function_calls": [],'
            b' "suppressed_calls": [], "confidence": 0.5,'
            b' "reasoning": null, "validation": null,'
            b' "model": "needle3"}'
        )
        decision = router(transport).route("write", tools("write_file"))
        self.assertEqual(decision.outcome, RouteOutcome.CONFIRM)
        self.assertFalse(decision.selected)
        self.assertFalse(decision.abstained)

    def test_auth_header_only_when_key_configured(self):
        transport = FakeTransport(body=SELECTED_BODY)
        router(transport, api_key="k").route("x", tools("bash"))
        self.assertEqual(
            transport.requests[0].get_header("Authorization"), "Bearer k"
        )

        transport2 = FakeTransport(body=SELECTED_BODY)
        router(transport2).route("x", tools("bash"))
        self.assertIsNone(transport2.requests[0].get_header("Authorization"))


class TestToolSetValidation(unittest.TestCase):
    def test_candidate_tool_sets_capped_at_five(self):
        self.assertEqual(MAX_CANDIDATE_TOOLS, 5)

        transport = FakeTransport(body=SELECTED_BODY)
        with self.assertRaises(ToolSetRejected):
            router(transport).route("x", tools("a", "b", "c", "d", "e", "f"))
        # Rejected before any HTTP.
        self.assertEqual(transport.requests, [])

    def test_empty_tool_set_rejected_without_http(self):
        transport = FakeTransport(body=SELECTED_BODY)
        with self.assertRaises(RoutingValidationError):
            router(transport).route("x", [])
        self.assertEqual(transport.requests, [])

    def test_five_tools_accepted(self):
        transport = FakeTransport(body=SELECTED_BODY)
        router(transport).route(
            "x", tools("a", "b", "c", "d", "e")
        )
        self.assertEqual(len(transport.requests), 1)

    def test_empty_input_rejected_without_http(self):
        transport = FakeTransport(body=SELECTED_BODY)
        with self.assertRaises(RoutingValidationError):
            router(transport).route("   ", tools("bash"))
        self.assertEqual(transport.requests, [])

    def test_server_422_maps_to_validation_error_with_detail(self):
        detail = [{"loc": ["body", "tools"], "msg": "too many", "type": "value_error"}]
        transport = FakeTransport(
            status=422,
            body=json.dumps({"detail": detail}).encode("utf-8"),
        )
        with self.assertRaises(ToolSetRejected) as ctx:
            router(transport).route("x", tools("bash"))
        self.assertEqual(ctx.exception.details, detail)


class TestFailuresAndOptionalSeam(unittest.TestCase):
    def test_server_error_is_unavailable(self):
        transport = FakeTransport(status=500, body=b"oops")
        with self.assertRaises(NeedleUnavailable):
            router(transport).route("x", tools("bash"))

    def test_invalid_json_is_unavailable(self):
        transport = FakeTransport(body=b"not json")
        with self.assertRaises(NeedleUnavailable):
            router(transport).route("x", tools("bash"))

    def test_unknown_outcome_is_unavailable(self):
        transport = FakeTransport(
            body=b'{"outcome": "exploded", "function_calls": [],'
            b' "suppressed_calls": [], "confidence": null,'
            b' "reasoning": null, "validation": null,'
            b' "model": "needle3"}'
        )
        with self.assertRaises(NeedleUnavailable):
            router(transport).route("x", tools("bash"))

    def test_transport_failure_is_unavailable(self):
        def broken(request, timeout):
            raise OSError("dns failure")

        with self.assertRaises(NeedleUnavailable):
            router(broken).route("x", tools("bash"))

    def test_optional_seam_needs_no_router(self):
        # No router configured: nothing routes, nothing errors,
        # and no transport is touched.
        self.assertIsNone(route_or_none(None, "x", tools("bash")))

        transport = FakeTransport(body=SELECTED_BODY)
        decision = route_or_none(router(transport), "x", tools("bash"))
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertTrue(decision.selected)
        self.assertEqual(len(transport.requests), 1)


if __name__ == "__main__":
    unittest.main()