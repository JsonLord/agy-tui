"""
Deterministic tests for the AGY supervisor dispatch seam.

The subprocess boundary is faked: no real opencode-supervise is
launched and argv inspection is exact.
"""

import subprocess
import unittest

from app.config import AgySettings
from app.orchestration.agy_supervisor import (
    default_timeout,
    run_agy_supervised,
)


class FakeProc:
    def __init__(self, args, kwargs):
        self.args = args
        self.kwargs = kwargs
        self.returncode = 0
        self.communicated = []
        self.terminated = False

    def communicate(self, timeout=None):
        self.communicated.append(timeout)
        return ("supervisor out", "supervisor err")

    def terminate(self):
        self.terminated = True


class FakeSubprocess:
    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self):
        self.launches = []

    def Popen(self, args, **kwargs):
        proc = FakeProc(args, kwargs)
        self.launches.append(proc)
        return proc


class TestRunAgySupervised(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSubprocess()
        import app.integrations.external_stack as external_stack

        self.external_stack = external_stack
        self._original = external_stack.subprocess
        external_stack.subprocess = self.fake   # type: ignore[assignment]

    def tearDown(self):
        self.external_stack.subprocess = self._original

    def test_lifecycle_values_forwarded_explicitly(self):
        settings = AgySettings.from_env({})
        run_agy_supervised(
            settings, "/tmp", "do stage 1", agent="build"
        )

        self.assertEqual(len(self.fake.launches), 1)
        argv = self.fake.launches[0].args

        # AGY values, never the 120s factory default.
        self.assertEqual(argv[argv.index("--stall-seconds") + 1], "900")
        self.assertEqual(argv[argv.index("--repeat-limit") + 1], "3")
        self.assertEqual(argv[argv.index("--max-restarts") + 1], "1")
        self.assertEqual(argv[argv.index("--agent") + 1], "build")

        # Prompt is exactly one trailing positional.
        self.assertIn("do stage 1", argv[-1])
        self.assertNotIn("gemma-local", argv)

    def test_lifecycle_values_configurable(self):
        settings = AgySettings.from_env(
            {
                "SUPERVISOR_STALL_SECONDS": "450",
                "SUPERVISOR_REPEAT_LIMIT": "2",
                "SUPERVISOR_MAX_RESTARTS": "0",
            }
        )
        run_agy_supervised(settings, "/tmp", "work", agent="build")

        argv = self.fake.launches[0].args
        self.assertEqual(argv[argv.index("--stall-seconds") + 1], "450")
        self.assertEqual(argv[argv.index("--repeat-limit") + 1], "2")
        self.assertEqual(argv[argv.index("--max-restarts") + 1], "0")

    def test_agent_must_be_explicit(self):
        settings = AgySettings.from_env({})

        for bad in (None, "", "   "):
            with self.assertRaises(ValueError):
                run_agy_supervised(
                    settings,
                    "/tmp",
                    "work",
                    agent=bad,   # type: ignore[arg-type]
                )

        self.assertEqual(self.fake.launches, [])

    def test_session_binding_before_prompt(self):
        settings = AgySettings.from_env({})
        run_agy_supervised(
            settings,
            "/tmp",
            "work",
            agent="build",
            session="ses_abc123",
        )

        argv = self.fake.launches[0].args
        self.assertEqual(
            argv[argv.index("--session") + 1], "ses_abc123"
        )
        self.assertLess(argv.index("--session"), len(argv) - 1)

        argv_no_session = None
        run_agy_supervised(settings, "/tmp", "work", agent="build")
        argv_no_session = self.fake.launches[1].args
        self.assertNotIn("--session", argv_no_session)

    def test_malformed_session_fails_before_launch(self):
        settings = AgySettings.from_env({})

        for bad in ("", "  ", "nope", "ses_has space"):
            with self.assertRaises(ValueError):
                run_agy_supervised(
                    settings,
                    "/tmp",
                    "work",
                    agent="build",
                    session=bad,
                )

        self.assertEqual(self.fake.launches, [])

    def test_default_timeout_sized_for_stall_policy(self):
        settings = AgySettings.from_env({})
        self.assertEqual(default_timeout(settings.lifecycle), 4500)

        run_agy_supervised(settings, "/tmp", "work", agent="build")
        self.assertEqual(
            self.fake.launches[0].communicated, [4500]
        )

        run_agy_supervised(
            settings, "/tmp", "work", agent="build", timeout=120
        )
        self.assertEqual(self.fake.launches[1].communicated, [120])

    def test_result_mapping(self):
        settings = AgySettings.from_env({})
        result = run_agy_supervised(settings, "/tmp", "work", agent="build")
        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertIn("supervisor out", result.output)


if __name__ == "__main__":
    unittest.main()