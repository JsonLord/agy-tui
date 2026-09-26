"""
Deterministic tests for the AGY model/provider, supervisor
lifecycle and Needle configuration seam.

No network, no environment mutation: from_env() accepts an
explicit mapping so every case is pure.
"""

import unittest

from app.config import (
    DEFAULT_DISPATCH_MODEL,
    DEFAULT_HELMHOLTZ_BASE_URL,
    DEFAULT_NEEDLE_BASE_URL,
    DEFAULT_SUPERVISOR_MODEL,
    AgySettings,
    NeedleSettings,
    SupervisorLifecycle,
)


class TestSupervisorLifecycle(unittest.TestCase):
    def test_agy_defaults_avoid_the_120s_false_stall(self):
        lifecycle = SupervisorLifecycle()
        self.assertEqual(lifecycle.stall_seconds, 900)
        self.assertEqual(lifecycle.repeat_limit, 3)
        self.assertEqual(lifecycle.max_restarts, 1)

    def test_from_env_empty_mapping_returns_defaults(self):
        lifecycle = SupervisorLifecycle.from_env({})
        self.assertEqual(
            lifecycle,
            SupervisorLifecycle(
                stall_seconds=900,
                repeat_limit=3,
                max_restarts=1,
            ),
        )

    def test_from_env_overrides(self):
        lifecycle = SupervisorLifecycle.from_env(
            {
                "SUPERVISOR_STALL_SECONDS": "450",
                "SUPERVISOR_REPEAT_LIMIT": "5",
                "SUPERVISOR_MAX_RESTARTS": "0",
            }
        )
        self.assertEqual(lifecycle.stall_seconds, 450)
        self.assertEqual(lifecycle.repeat_limit, 5)
        self.assertEqual(lifecycle.max_restarts, 0)

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            SupervisorLifecycle.from_env({"SUPERVISOR_STALL_SECONDS": "abc"})

        with self.assertRaises(ValueError):
            SupervisorLifecycle.from_env({"SUPERVISOR_REPEAT_LIMIT": "-1"})

        with self.assertRaises(ValueError):
            SupervisorLifecycle(stall_seconds=0)

        with self.assertRaises(ValueError):
            SupervisorLifecycle(max_restarts=-2)


class TestNeedleSettings(unittest.TestCase):
    def test_defaults(self):
        needle = NeedleSettings.from_env({})
        self.assertEqual(needle.base_url, DEFAULT_NEEDLE_BASE_URL)
        self.assertIsNone(needle.api_key)
        self.assertTrue(needle.enabled)

    def test_overrides_and_toggle_parsing(self):
        needle = NeedleSettings.from_env(
            {
                "NEEDLE_BASE_URL": "https://example.invalid",
                "NEEDLE_API_KEY": "k",
                "NEEDLE_ROUTING_ENABLED": "off",
            }
        )
        self.assertEqual(needle.base_url, "https://example.invalid")
        self.assertEqual(needle.api_key, "k")
        self.assertFalse(needle.enabled)

        self.assertTrue(
            NeedleSettings.from_env(
                {"NEEDLE_ROUTING_ENABLED": "TRUE"}
            ).enabled
        )

        with self.assertRaises(ValueError):
            NeedleSettings.from_env({"NEEDLE_ROUTING_ENABLED": "maybe"})


class TestAgySettings(unittest.TestCase):
    def test_helmholtz_model_binding(self):
        settings = AgySettings.from_env({})

        # Literal preservation of the configured model names.
        self.assertEqual(settings.supervisor_model, "alias-mimo-v2.6-pro")
        self.assertEqual(settings.dispatch_model, "alias-mimo-v2.6-flash")
        self.assertEqual(settings.supervisor_model, DEFAULT_SUPERVISOR_MODEL)
        self.assertEqual(settings.dispatch_model, DEFAULT_DISPATCH_MODEL)
        self.assertEqual(settings.provider_base_url, DEFAULT_HELMHOLTZ_BASE_URL)
        self.assertIsNone(settings.provider_api_key)

    def test_env_overrides_verbatim(self):
        settings = AgySettings.from_env(
            {
                "SUPERVISOR_MODEL": "alias-mimo-v2.6-pro",
                "DISPATCH_MODEL": "alias-mimo-v2.6-flash",
                "AGY_PROVIDER_BASE_URL": "https://example.invalid/v1",
                "AGY_PROVIDER_API_KEY": "secret",
            }
        )
        self.assertEqual(settings.supervisor_model, "alias-mimo-v2.6-pro")
        self.assertEqual(settings.dispatch_model, "alias-mimo-v2.6-flash")
        self.assertEqual(
            settings.provider_base_url, "https://example.invalid/v1"
        )
        self.assertEqual(settings.provider_api_key, "secret")

    def test_nested_seams_present(self):
        settings = AgySettings.from_env(
            {"SUPERVISOR_STALL_SECONDS": "900"}
        )
        self.assertEqual(settings.lifecycle.stall_seconds, 900)
        self.assertTrue(settings.needle.enabled)
        self.assertEqual(settings.needle.base_url, DEFAULT_NEEDLE_BASE_URL)


if __name__ == "__main__":
    unittest.main()