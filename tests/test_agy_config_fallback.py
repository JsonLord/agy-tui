"""
Deterministic tests for the two P3 fallback-model config fields.

Network-free and environment-mapping based: ``AgySettings.from_env``
accepts an explicit mapping, so every case is pure. These tests also
pin that NOTHING else in ``AgySettings`` changed (primary model
bindings, provider URL default, lifecycle defaults stay as P2 left
them).
"""

import unittest

from app.config import (
    DEFAULT_DISPATCH_FALLBACK_MODEL,
    DEFAULT_DISPATCH_MODEL,
    DEFAULT_SUPERVISOR_FALLBACK_MODEL,
    DEFAULT_SUPERVISOR_MODEL,
    AgySettings,
)
from app.providers import ModelRole, RoleModels


class TestFallbackModelDefaults(unittest.TestCase):
    def test_defaults_are_the_contracted_models(self):
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.supervisor_fallback_model,
            "alias-qwen3.8-27b",
        )
        self.assertEqual(
            settings.dispatch_fallback_model,
            "alias-qwen3.8-flash-next",
        )
        self.assertEqual(
            settings.supervisor_fallback_model,
            DEFAULT_SUPERVISOR_FALLBACK_MODEL,
        )
        self.assertEqual(
            settings.dispatch_fallback_model,
            DEFAULT_DISPATCH_FALLBACK_MODEL,
        )

    def test_primaries_and_fallbacks_are_distinct(self):
        settings = AgySettings.from_env({})
        self.assertNotEqual(
            settings.supervisor_model,
            settings.supervisor_fallback_model,
        )
        self.assertNotEqual(
            settings.dispatch_model,
            settings.dispatch_fallback_model,
        )
        # Verbatim primary bindings unchanged by the P3 additions.
        self.assertEqual(settings.supervisor_model, DEFAULT_SUPERVISOR_MODEL)
        self.assertEqual(settings.dispatch_model, DEFAULT_DISPATCH_MODEL)

    def test_constructor_defaults_match_from_env_defaults(self):
        direct = AgySettings(
            provider_base_url="https://example.invalid/v1",
            supervisor_model=DEFAULT_SUPERVISOR_MODEL,
            dispatch_model=DEFAULT_DISPATCH_MODEL,
            provider_api_key=None,
            lifecycle=AgySettings.from_env({}).lifecycle,
            needle=AgySettings.from_env({}).needle,
        )
        self.assertEqual(
            direct.supervisor_fallback_model,
            DEFAULT_SUPERVISOR_FALLBACK_MODEL,
        )
        self.assertEqual(
            direct.dispatch_fallback_model,
            DEFAULT_DISPATCH_FALLBACK_MODEL,
        )


class TestFallbackModelEnvOverride(unittest.TestCase):
    def test_both_env_overrides(self):
        settings = AgySettings.from_env(
            {
                "AGY_SUPERVISOR_FALLBACK_MODEL": "alias-custom-sup-fb",
                "AGY_DISPATCH_FALLBACK_MODEL": "alias-custom-dis-fb",
            }
        )
        self.assertEqual(
            settings.supervisor_fallback_model, "alias-custom-sup-fb"
        )
        self.assertEqual(
            settings.dispatch_fallback_model, "alias-custom-dis-fb"
        )
        # Primaries untouched by the fallback overrides.
        self.assertEqual(settings.supervisor_model, DEFAULT_SUPERVISOR_MODEL)
        self.assertEqual(settings.dispatch_model, DEFAULT_DISPATCH_MODEL)

    def test_single_env_override_leaves_the_other_default(self):
        settings = AgySettings.from_env(
            {"AGY_SUPERVISOR_FALLBACK_MODEL": "only-sup"}
        )
        self.assertEqual(settings.supervisor_fallback_model, "only-sup")
        self.assertEqual(
            settings.dispatch_fallback_model,
            DEFAULT_DISPATCH_FALLBACK_MODEL,
        )

    def test_blank_override_falls_back_to_the_default(self):
        settings = AgySettings.from_env(
            {
                "AGY_SUPERVISOR_FALLBACK_MODEL": "   ",
                "AGY_DISPATCH_FALLBACK_MODEL": "",
            }
        )
        self.assertEqual(
            settings.supervisor_fallback_model,
            DEFAULT_SUPERVISOR_FALLBACK_MODEL,
        )
        self.assertEqual(
            settings.dispatch_fallback_model,
            DEFAULT_DISPATCH_FALLBACK_MODEL,
        )

    def test_primary_role_models_do_not_pick_up_fallbacks(self):
        settings = AgySettings.from_env(
            {"AGY_DISPATCH_FALLBACK_MODEL": "alias-custom-dis-fb"}
        )
        models = RoleModels.for_agy(settings)
        self.assertEqual(
            models.model_for(ModelRole.DISPATCH), DEFAULT_DISPATCH_MODEL
        )
        self.assertEqual(
            models.model_for(ModelRole.SUPERVISOR), DEFAULT_SUPERVISOR_MODEL
        )


if __name__ == "__main__":
    unittest.main()