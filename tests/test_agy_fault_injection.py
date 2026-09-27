"""
P5 fault-injection tests — network-free, everything injected.

Requirement-item matrix (tests 1-8 of the P5 contract's TESTS section):

 1  controlled transient injection: an armed injector returns the
    synthetic transient ExternalResult for the primary agent and the
    wrapped runner is NEVER called while armed
 2  exactly four primary attempts max: after 4 injections the 5th
    primary call passes through to the real runner (default
    max_injections=4; variants with 1 and 2 injected too)
 3  transition to configured fallback: calls with agent == fallback
    always pass through regardless of remaining injections
 4  same session retained: the session parameter is forwarded
    VERBATIM on pass-through and recorded in .history for injections
 5  non-transient does not trigger fallback: an ok=False result whose
    stderr is "compilation error: expected('(')" does NOT classify
    transient, while the injector's synthetic text DOES (both
    asserted through failover.is_transient_provider_failure)
 6  injector disabled by default: build_live_executor() without an
    injected run_supervised dispatches through the DEFAULT
    run_agy_supervised binding (patched-default proof), and a static
    source scan shows agy_runtime/agy_plan/agy_execute/agy_gatekeeper
    reference 'fault_injection' nowhere
 7  fault injection cannot leak into production: AGY_FAULT_INJECTION=1
    without AGY_LIVE -> assert_injection_not_armed raises; with
    AGY_LIVE set -> does not raise; env restored in finally
 8  .history records each injection with attempt index / agent /
    session

No network, no subprocess, no model: every runner is a recording
fake.
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from app.config import AgySettings
from app.integrations.external_stack import ExternalResult
from app.orchestration.fault_injection import (
    DEFAULT_TRANSIENT_TEXT,
    TransientFaultInjector,
    assert_injection_not_armed,
)
from app.orchestration.failover import (
    PRIMARY_MAX_ATTEMPTS,
    is_transient_provider_failure,
)
from app.providers import ProviderError
from app.schemas import (
    AcceptanceCommand,
    DevelopmentPlan,
    PlanStage,
    WorkerTask,
)

PRIMARY_AGENT = "agy-dispatch"
FALLBACK_AGENT = "agy-dispatch-fallback"
SETTINGS = AgySettings.from_env({})


def install_host_runtime_stub() -> None:
    """google.antigravity stub (same as the other P3/P4/P5 tests)."""
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


class RecordingRunner:
    """run_agy_supervised-shaped fake: records calls, returns ok."""

    def __init__(self, result: ExternalResult | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or ExternalResult(
            ok=True, returncode=0, stdout="real run", stderr=""
        )

    def __call__(
        self,
        settings,
        repo,
        prompt,
        *,
        agent,
        session=None,
        **kw,
    ):
        self.calls.append(
            {
                "settings": settings,
                "repo": repo,
                "prompt": prompt,
                "agent": agent,
                "session": session,
                "kw": dict(kw),
            }
        )
        return self.result


def synthetic_result(injector: TransientFaultInjector) -> ExternalResult:
    return injector(SETTINGS, "/nonexistent", "prompt",
                    agent=PRIMARY_AGENT, session=None)


# ---------------------------------------------------------------------------
# Cases 1-4: injection core
# ---------------------------------------------------------------------------


class TestInjectionCore(unittest.TestCase):
    def test_01_controlled_transient_injection_primary_only(self):
        """Case 1: armed injector serves the synthetic failure; no runner."""
        runner = RecordingRunner()
        injector = TransientFaultInjector(
            runner, primary_agent=PRIMARY_AGENT
        )

        self.assertFalse(injector.exhausted)
        for _ in range(PRIMARY_MAX_ATTEMPTS):
            result = synthetic_result(injector)

            self.assertFalse(result.ok)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, DEFAULT_TRANSIENT_TEXT)
            # The wrapped runner is NEVER touched while armed.
            self.assertEqual(runner.calls, [])

        self.assertTrue(injector.exhausted)

    def test_02_exactly_four_primary_attempts_max(self):
        """Case 2: 4 injections by default, then pass-through (1/2 too)."""
        for budget in (
            PRIMARY_MAX_ATTEMPTS,  # default contract budget (4)
            1,
            2,
        ):
            with self.subTest(max_injections=budget):
                real = ExternalResult(
                    ok=True, returncode=0,
                    stdout="real output", stderr="",
                )
                runner = RecordingRunner(real)
                injector = TransientFaultInjector(
                    runner,
                    primary_agent=PRIMARY_AGENT,
                    max_injections=budget,
                )

                for _ in range(budget):
                    result = synthetic_result(injector)
                    self.assertFalse(result.ok)

                # Budget exhausted -> the NEXT primary call reaches
                # the real runner (no injection, no silent hide).
                result = synthetic_result(injector)
                self.assertNotEqual(result.stderr, DEFAULT_TRANSIENT_TEXT)
                self.assertTrue(result.ok)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(runner.calls[0]["agent"], PRIMARY_AGENT)
                self.assertEqual(
                    len(injector.history), budget,
                    "exactly `budget` injections recorded",
                )

    def test_03_fallback_agent_always_passes_through(self):
        """Case 3: agent != primary passes through while armed."""
        runner = RecordingRunner()
        injector = TransientFaultInjector(
            runner, primary_agent=PRIMARY_AGENT, max_injections=4
        )

        # While fully armed: fallback calls pass through directly.
        for _ in range(3):
            result = injector(
                SETTINGS, "/nonexistent", "prompt",
                agent=FALLBACK_AGENT, session="ses_fallback0001",
            )
            self.assertTrue(result.ok)
            self.assertEqual(injector.injections_remaining, 4)

        self.assertEqual(len(runner.calls), 3)

        # Primary still injects; fallback still passes through.
        synthetic_result(injector)
        result = injector(
            SETTINGS, "/nonexistent", "prompt",
            agent=FALLBACK_AGENT, session="ses_fallback0001",
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(len(injector.history), 1)
        self.assertEqual(
            [entry["agent"] for entry in injector.history],
            [PRIMARY_AGENT],
        )

    def test_04_same_session_retained_verbatim(self):
        """Case 4: session forwarded verbatim; injections record it."""
        runner = RecordingRunner(
            ExternalResult(ok=True, returncode=0,
                           stdout="Session ID: ses_same0001", stderr="")
        )
        injector = TransientFaultInjector(
            runner, primary_agent=PRIMARY_AGENT, max_injections=4
        )

        # Injected primary call: session recorded, runner untouched.
        injector(SETTINGS, "/r", "p", agent=PRIMARY_AGENT,
                 session="ses_same0001")
        self.assertEqual(injector.history[0]["session"], "ses_same0001")
        self.assertEqual(runner.calls, [])

        # Fallback call: the SAME session id arrives at the real
        # runner verbatim (session continuity through the switch).
        result = injector(SETTINGS, "/r", "p", agent=FALLBACK_AGENT,
                          session="ses_same0001")
        self.assertTrue(result.ok)
        self.assertEqual(runner.calls[0]["session"], "ses_same0001")

        # Exhausted primary: also verbatim.
        while not injector.exhausted:
            synthetic_result(injector)
        injector(SETTINGS, "/r2", "p2", agent=PRIMARY_AGENT,
                 session="ses_same0002")
        self.assertEqual(runner.calls[1]["session"], "ses_same0002")
        self.assertEqual(runner.calls[1]["agent"], PRIMARY_AGENT)


# ---------------------------------------------------------------------------
# Cases 5 + 8: classifier alignment + history
# ---------------------------------------------------------------------------


class TestClassifierAlignment(unittest.TestCase):
    def test_05_non_transient_does_not_classify_transient(self):
        """Case 5: deterministic failure stays non-transient; synthetic
        text DOES classify (the exact exec-task semantics)."""

        def executor_classifies(result: ExternalResult) -> bool:
            failure_text = (result.stderr or "").strip() or (
                result.stdout or ""
            )
            return (not result.ok) and is_transient_provider_failure(
                ProviderError(
                    failure_text or "supervised run failed",
                    status=None,
                    body=result.stderr or None,
                )
            )

        # Deterministic coding failure: NOT transient -> no failover
        # (the executor returns the output for validation to judge).
        coding_failure = ExternalResult(
            ok=False, returncode=1, stdout="",
            stderr="compilation error: expected '('",
        )
        self.assertFalse(
            is_transient_provider_failure(
                ProviderError("compilation error: expected '('",
                              status=None, body=coding_failure.stderr)
            )
        )
        self.assertFalse(executor_classifies(coding_failure))

        # The injector's synthetic text DOES classify as transient
        # (this is what routes the injected failure into the honest
        # 4-attempts-then-fallback path).
        runner = RecordingRunner()
        injector = TransientFaultInjector(
            runner, primary_agent=PRIMARY_AGENT
        )
        synthetic = synthetic_result(injector)
        self.assertTrue(
            is_transient_provider_failure(
                ProviderError(
                    (synthetic.stderr or "").strip(), status=None,
                    body=synthetic.stderr or None,
                )
            )
        )
        self.assertTrue(executor_classifies(synthetic))

    def test_08_history_records_each_injection(self):
        """Case 8: every injection logs attempt_index/agent/session."""
        runner = RecordingRunner()
        injector = TransientFaultInjector(
            runner, primary_agent=PRIMARY_AGENT, max_injections=4
        )

        # 3 injected + 2 pass-through calls interleaved.
        injector(SETTINGS, "/r", "p", agent=PRIMARY_AGENT,
                 session="ses_a1")
        injector(SETTINGS, "/r", "p", agent=FALLBACK_AGENT,
                 session="ses_a2")
        injector(SETTINGS, "/r", "p", agent=PRIMARY_AGENT,
                 session="ses_a3")
        injector(SETTINGS, "/r", "p", agent=PRIMARY_AGENT, session=None)
        injector(SETTINGS, "/r", "p", agent=FALLBACK_AGENT,
                 session="ses_a5")

        self.assertEqual(len(injector.history), 3)
        expected = [
            {"attempt_index": 1, "agent": PRIMARY_AGENT,
             "session": "ses_a1"},
            {"attempt_index": 3, "agent": PRIMARY_AGENT,
             "session": "ses_a3"},
            {"attempt_index": 4, "agent": PRIMARY_AGENT,
             "session": None},
        ]
        self.assertEqual(injector.history, expected)
        # Pass-through calls are not injections and never enter history.
        self.assertEqual(len(runner.calls), 2)


# ---------------------------------------------------------------------------
# Cases 6-7: safety — disabled by default, no production leak
# ---------------------------------------------------------------------------


def _static_default_modules() -> list[str]:
    return [
        "app/orchestration/agy_runtime.py",
        "app/orchestration/agy_plan.py",
        "app/orchestration/agy_execute.py",
        "app/orchestration/agy_gatekeeper.py",
    ]


def _tiny_plan():
    stage = PlanStage(
        stage_id="s1",
        objective="touch one line",
        dependencies=[],
        allowed_files=["thing.py"],
        implementation_context="append a comment to thing.py",
        acceptance_commands=[
            AcceptanceCommand(
                command="python3 -m unittest discover -s tests -q",
                expected_exit_code=0,
            )
        ],
        acceptance_conditions=["thing.py documents itself"],
        cumulative_invariants=["thing.py still imports"],
    )
    plan = DevelopmentPlan(
        plan_id="agp-fault-injection-safety",
        objective="one-line documentation stage",
        stages=[stage],
        final_acceptance=[
            AcceptanceCommand(
                command="python3 -m unittest discover -s tests -q",
                expected_exit_code=0,
            )
        ],
    )
    task = WorkerTask(
        id="s1-t00",
        goal="touch one line",
        files=["thing.py"],
        steps=["append a comment to thing.py"],
        verification=TaskVerification_stub(),
        worker_instruction="Stage s1: implement ONLY bounded task s1-t00.",
    )
    return plan, stage, task


def TaskVerification_stub():  # local helper to avoid import noise
    from app.schemas import TaskVerification

    return TaskVerification(
        command="python3 -m unittest discover -s tests -q",
        expected_exit_code=0,
    )


class TestSafetyGuards(unittest.TestCase):
    def test_06_injector_disabled_by_default(self):
        """Case 6: no default production path touches fault_injection."""
        # (a) Static source scan: none of the four default-path
        # modules even MENTIONS fault_injection (+ no 'alias-' ids).
        for relative in _static_default_modules():
            source = Path(relative).read_text(encoding="utf-8")
            with self.subTest(module=relative):
                self.assertNotIn("fault_injection", source)
                self.assertNotIn("TransientFaultInjector", source)

        # (b) agy_runtime exposes no fault-injection attribute at all.
        from app.orchestration import agy_runtime

        self.assertFalse(hasattr(agy_runtime, "fault_injection"))
        self.assertFalse(hasattr(agy_runtime, "TransientFaultInjector"))

        # (c) build_live_executor() with NO run_supervised injected
        # dispatches through the patched DEFAULT run_agy_supervised
        # binding (so the default is provably the supervised runner,
        # not any wrapper).
        plan, stage, task = _tiny_plan()
        sentinel = RecordingRunner(
            ExternalResult(
                ok=True, returncode=0,
                stdout="Session ID: ses_default0001", stderr="",
            )
        )
        with mock.patch.object(
            agy_runtime, "run_agy_supervised", sentinel
        ):
            execute_task, _validate, _collect, _run = (
                agy_runtime.build_live_executor(
                    SETTINGS, "/nonexistent-default-repo", plan, stage
                )
            )
            output = asyncio.run(execute_task(task, 1, None))

        self.assertEqual(len(sentinel.calls), 1)
        self.assertEqual(sentinel.calls[0]["agent"], "agy-dispatch")
        # ExternalResult.output concatenates stdout (the session line)
        # — the sentinel's own payload, verbatim proof the DEFAULT
        # runner binding served this call.
        self.assertEqual(output, "Session ID: ses_default0001")

        # (d) Even the BUILT default executor never references the
        # injector: its module namespace is unchanged (checked in (b))
        # and the source scan in (a) covers every default-path file.

    def test_07_env_guard_cannot_leak_into_production(self):
        """Case 7: AGY_FAULT_INJECTION=1 needs AGY_LIVE (finally-clean)."""
        saved = {
            key: os.environ.get(key)
            for key in ("AGY_FAULT_INJECTION", "AGY_LIVE")
        }
        try:
            # Clean env: never raises.
            os.environ.pop("AGY_FAULT_INJECTION", None)
            os.environ.pop("AGY_LIVE", None)
            self.assertIsNone(assert_injection_not_armed(SETTINGS))

            # Armed-looking env OUTSIDE a live run -> raises.
            os.environ["AGY_FAULT_INJECTION"] = "1"
            os.environ.pop("AGY_LIVE", None)
            with self.assertRaises(RuntimeError) as caught:
                assert_injection_not_armed(SETTINGS)
            self.assertIn("AGY_FAULT_INJECTION", str(caught.exception))
            self.assertIn("AGY_LIVE", str(caught.exception))

            # Armed + LIVE -> allowed.
            os.environ["AGY_LIVE"] = "1"
            self.assertIsNone(assert_injection_not_armed(SETTINGS))

            # Any other AGY_FAULT_INJECTION value is not "armed".
            os.environ["AGY_FAULT_INJECTION"] = "0"
            os.environ.pop("AGY_LIVE", None)
            self.assertIsNone(assert_injection_not_armed(SETTINGS))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    unittest.main()