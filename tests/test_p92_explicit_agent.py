"""P9.2 FIX B: run_supervised requires an explicit agent (no gemma-local default)."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from app.integrations import external_stack
from app.integrations.external_stack import run_supervised

REPO = Path(__file__).resolve().parent


def test_no_gemma_local_default_in_signature():
    """``agent`` is a required keyword-only parameter with no default."""
    params = inspect.signature(run_supervised).parameters
    agent = params["agent"]
    assert agent.kind is inspect.Parameter.KEYWORD_ONLY
    assert agent.default is inspect.Parameter.empty


def test_missing_agent_raises_typeerror_before_popen(monkeypatch):
    """Omitting agent= raises TypeError without launching any subprocess."""
    calls = []

    def fake_popen(*args, **kwargs):  # pragma: no cover - must not run
        calls.append((args, kwargs))
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(external_stack.subprocess, "Popen", fake_popen)

    with pytest.raises(TypeError):
        run_supervised(str(REPO), "do the thing")

    assert calls == []


def test_empty_agent_raises_valueerror_before_side_effects(monkeypatch):
    """A whitespace/empty agent fails closed before env/subprocess work."""
    calls = []

    def fake_popen(*args, **kwargs):  # pragma: no cover - must not run
        calls.append((args, kwargs))
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(external_stack.subprocess, "Popen", fake_popen)

    for bad in ("", "   "):
        with pytest.raises(ValueError, match="non-empty OpenCode agent name"):
            run_supervised(str(REPO), "task", agent=bad)

    assert calls == []


def test_external_stack_has_no_gemma_local_default_source():
    """Grep-level guard: the old default literal is gone from the module."""
    src = Path(external_stack.__file__).read_text(encoding="utf-8")
    assert 'agent: str = "gemma-local"' not in src
    assert "gemma-local" not in src
