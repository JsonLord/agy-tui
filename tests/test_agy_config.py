"""
Deterministic tests for the AGY model/provider, supervisor
lifecycle and Needle configuration seam.

No network. ``from_env()`` accepts an explicit mapping so every
case is pure; the OpenCode-config fallback is steered via the
OPENCODE_CONFIG_PATH environment variable (saved/restored around
each case so tests never read the real user config).
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.config import (
    DEFAULT_DISPATCH_MODEL,
    DEFAULT_HELMHOLTZ_BASE_URL,
    DEFAULT_NEEDLE_BASE_URL,
    DEFAULT_SUPERVISOR_MODEL,
    AgySettings,
    NeedleSettings,
    SupervisorLifecycle,
)

_MISSING_OPENCODE_PATH = str(
    Path(tempfile.gettempdir()) / "agy-test-opencode-missing.json"
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
    def setUp(self):
        # Isolate from the real user OpenCode config so the
        # default-path assertions stay deterministic.
        self._saved = os.environ.get("OPENCODE_CONFIG_PATH")
        os.environ["OPENCODE_CONFIG_PATH"] = _MISSING_OPENCODE_PATH

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OPENCODE_CONFIG_PATH", None)
        else:
            os.environ["OPENCODE_CONFIG_PATH"] = self._saved

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


class TestOpencodeProviderFallback(unittest.TestCase):
    """
    The OpenCode provider.helmholtz block as fallback source for
    AgySettings provider base URL / api key (env vars win, then
    OpenCode config, then code defaults).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._saved = os.environ.get("OPENCODE_CONFIG_PATH")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("OPENCODE_CONFIG_PATH", None)
        else:
            os.environ["OPENCODE_CONFIG_PATH"] = self._saved

    def _write_config(self, payload: dict) -> str:
        path = Path(self._tmp.name) / "opencode.jsonc"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_opencode_config_fallback(self):
        path = self._write_config(
            {
                "provider": {
                    "helmholtz": {
                        "apiKey": "test-key-123",
                        "options": {"baseURL": "https://example.invalid/v1"},
                    }
                }
            }
        )
        os.environ["OPENCODE_CONFIG_PATH"] = path
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.provider_base_url, "https://example.invalid/v1"
        )
        self.assertEqual(settings.provider_api_key, "test-key-123")

    def test_env_var_overrides_opencode_config(self):
        path = self._write_config(
            {
                "provider": {
                    "helmholtz": {
                        "apiKey": "test-key-123",
                        "options": {"baseURL": "https://example.invalid/v1"},
                    }
                }
            }
        )
        os.environ["OPENCODE_CONFIG_PATH"] = path
        settings = AgySettings.from_env(
            {
                "AGY_PROVIDER_BASE_URL": "https://env-wins.invalid/v1",
                "AGY_PROVIDER_API_KEY": "env-key",
            }
        )
        self.assertEqual(
            settings.provider_base_url, "https://env-wins.invalid/v1"
        )
        self.assertEqual(settings.provider_api_key, "env-key")

    def test_missing_opencode_config_uses_default(self):
        os.environ["OPENCODE_CONFIG_PATH"] = _MISSING_OPENCODE_PATH
        self.assertFalse(Path(_MISSING_OPENCODE_PATH).exists())
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.provider_base_url,
            "https://api.helmholtz-blablador.fz-juelich.de/v1",
        )
        self.assertEqual(
            settings.provider_base_url, DEFAULT_HELMHOLTZ_BASE_URL
        )
        self.assertIsNone(settings.provider_api_key)

    def test_opencode_config_no_helmholtz_block(self):
        path = self._write_config({"provider": {"other": {}}})
        os.environ["OPENCODE_CONFIG_PATH"] = path
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.provider_base_url, DEFAULT_HELMHOLTZ_BASE_URL
        )
        self.assertIsNone(settings.provider_api_key)

    def test_top_level_base_url_used_without_options(self):
        path = self._write_config(
            {
                "provider": {
                    "helmholtz": {
                        "apiKey": "k",
                        "baseURL": "https://top-level.invalid/v1",
                    }
                }
            }
        )
        os.environ["OPENCODE_CONFIG_PATH"] = path
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.provider_base_url, "https://top-level.invalid/v1"
        )

    def test_unparseable_config_falls_back_to_defaults(self):
        path = Path(self._tmp.name) / "broken.jsonc"
        path.write_text("{not json", encoding="utf-8")
        os.environ["OPENCODE_CONFIG_PATH"] = str(path)
        settings = AgySettings.from_env({})
        self.assertEqual(
            settings.provider_base_url, DEFAULT_HELMHOLTZ_BASE_URL
        )
        self.assertIsNone(settings.provider_api_key)


class TestP10PackageExports(unittest.TestCase):
    """The checkpoint_planning package re-exports the P10 surface."""

    def test_p10_names_are_re_exported(self):
        import app.orchestration.checkpoint_planning as cp

        for name in (
            "G2Executor",
            "G3Executor",
            "MiMoProG2Gatekeeper",
            "MiMoProG3Gatekeeper",
            "build_live_g2_executor",
            "build_live_g3_executor",
            "G2_GATE_ID",
            "G3_GATE_ID",
            "MAX_FIX_CYCLES",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(cp, name))
                self.assertIn(name, cp.__all__)


if __name__ == "__main__":
    unittest.main()