"""Package metadata and locator discovery are read-only, never shell execution."""

import json
from types import SimpleNamespace

import pytest

from app.hybrid_local import windows


@pytest.mark.parametrize("locator", ["opencode.cmd", "opencode.ps1", "opencode"])
def test_modern_metadata_locator_unicode_spaces(tmp_path, monkeypatch, locator):
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    prefix = tmp_path / "User équipe 日本語/npm"
    package = prefix / "node_modules/@opencode/cli"
    (package / "bin").mkdir(parents=True)
    (prefix / locator).write_text("must never execute this shim")
    (package / "package.json").write_text(
        json.dumps({"name": "@opencode/cli", "bin": {"opencode": "bin/opencode.exe"}})
    )
    executable = package / "bin/opencode.exe"
    executable.write_text("synthetic native image")
    before = (package / "package.json").read_bytes()
    assert windows.native_binary(str(prefix / locator)) == str(executable)
    assert (package / "package.json").read_bytes() == before


@pytest.mark.parametrize(
    "entry", ["../outside.exe", "C:/outside.exe", "//server/exe", "bin/script.cmd"]
)
def test_metadata_escape_rejected(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    package = tmp_path / "node_modules/@opencode/cli"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@opencode/cli", "bin": {"opencode": entry}})
    )
    with pytest.raises(windows.OpenCodeResolutionError, match="METADATA_INVALID"):
        windows.native_binary(str(tmp_path / "opencode.cmd"))


def test_explicit_missing_image_precise_error(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    with pytest.raises(windows.OpenCodeResolutionError, match="EXECUTABLE_MISSING"):
        windows.native_binary(str(tmp_path / "missing.exe"))


def test_missing_package_image_not_silently_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    package = tmp_path / "node_modules/@opencode/cli"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name":"@opencode/cli","bin":{"opencode":"bin/opencode.exe"}}'
    )
    with pytest.raises(
        windows.OpenCodeResolutionError, match="PACKAGE_EXECUTABLE_MISSING"
    ):
        windows.native_binary(str(tmp_path / "opencode.cmd"))
