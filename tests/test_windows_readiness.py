"""Reconstructed installation/discovery/audit contracts; native Windows still pending."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.hybrid_local import installation as installer
from app.hybrid_local import windows
from app.hybrid_local.discovery import configuration_locations, discover, register
from app.hybrid_local.policy import HybridError, WorkspaceRegistry

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root, config = (
        tmp_path / "Installation équipe 日本語",
        tmp_path / "Client config 日本語.json",
    )
    original = {
        "mcpServers": {
            "existing": {
                "command": "existing.exe",
                "env": {"PRIVATE_SENTINEL": "not-a-real-credential"},
            }
        },
        "preferences": {"color": "blue"},
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(
        installer,
        "preflight",
        lambda *_: {"opencode_binary": "native-opencode", "python": "3.12"},
    )

    def fake_run(command, **_):
        if command[1:3] == ["-m", "venv"]:
            python = installer.python_in(Path(command[3]).parent)
            python.parent.mkdir(parents=True)
            python.write_text("synthetic venv")
        if len(command) > 2 and command[1] == "-c" and "pip._vendor" in command[2]:
            return "[]"
        return "STDIO_SMOKE_OK"

    monkeypatch.setattr(installer, "run", fake_run)
    return root, config, original


def install(setup):
    return installer.install(
        SOURCE, setup[0], setup[1], "opencode", "node", manual_registration=False
    )


def test_dry_run_writes_nothing(setup):
    root, config, _ = setup
    before = config.read_bytes()
    result = installer.install(
        SOURCE,
        root,
        config,
        "opencode",
        "node",
        dry_run=True,
        manual_registration=False,
    )
    assert (
        result["status"] == "DRY_RUN"
        and not root.exists()
        and config.read_bytes() == before
    )
    assert "app/config.py" not in result["files"] and "recovery" not in "\n".join(
        result["files"]
    )


def test_additive_install_doctor_and_rollback(setup):
    root, config, original = setup
    result = install(setup)
    after = json.loads(config.read_text())
    assert (
        after["mcpServers"]["existing"] == original["mcpServers"]["existing"]
        and after["preferences"] == original["preferences"]
    )
    entry = after["mcpServers"]["hybrid-local"]
    assert "model" not in json.dumps(entry).lower()
    assert "PRIVATE_SENTINEL" not in (root / "installation.json").read_text()
    assert WorkspaceRegistry(root / "data/workspaces.json").workspaces == {}
    assert installer.doctor(root)["status"] == "HEALTHY"
    assert (
        installer.uninstall(root, dry_run=True, modify_registration=True)["status"]
        == "DRY_RUN"
        and Path(result["release"]).exists()
    )
    assert (
        installer.uninstall(root, modify_registration=True)["status"] == "UNINSTALLED"
    )
    assert (
        json.loads(config.read_text()) == original
        and not Path(result["release"]).exists()
    )
    assert (root / "data/workspaces.json").exists()
    assert (
        installer.uninstall(root, modify_registration=True)["status"] == "UNINSTALLED"
    )
    assert install(setup)["status"] == "INSTALLED"


def test_existing_server_never_replaced(setup):
    install(setup)
    before = setup[1].read_bytes()
    with pytest.raises(installer.InstallError, match="MCP_NAME_COLLISION"):
        install(setup)
    assert setup[1].read_bytes() == before


def test_native_config_never_registration_target(setup):
    root, config, _ = setup
    config.write_text('{"model":"native/value","provider":{}}')
    before = config.read_bytes()
    with pytest.raises(installer.InstallError, match="NATIVE_CONFIG_PROTECTED"):
        install(setup)
    assert config.read_bytes() == before and not root.exists()


def test_partial_dependency_failure_has_recovery_receipt(setup, monkeypatch):
    root, config, _ = setup
    before = config.read_bytes()

    def fail(*_, **__):
        raise installer.InstallError("DEPENDENCY_FAILURE")

    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(installer.InstallError, match="INSTALL_FAILED"):
        install(setup)
    assert (
        installer.load_receipt(root)["status"] == "FAILED"
        and config.read_bytes() == before
    )
    assert installer.doctor(root)["status"] == "NEEDS_ATTENTION"
    installer.uninstall(root, modify_registration=True)
    assert config.read_bytes() == before


def test_crash_after_registration_is_recoverable(setup):
    root, config, original = setup
    install(setup)
    receipt = installer.load_receipt(root)
    receipt["status"] = "REGISTERING"
    installer.atomic_json(installer.receipt_path(root), receipt)
    assert installer.doctor(root)["status"] == "NEEDS_ATTENTION"
    installer.uninstall(root, modify_registration=True)
    assert json.loads(config.read_text()) == original


def test_user_edited_entry_stops_rollback(setup):
    result = install(setup)
    document = json.loads(setup[1].read_text())
    document["mcpServers"]["hybrid-local"]["command"] = "edited.exe"
    setup[1].write_text(json.dumps(document))
    before = setup[1].read_bytes()
    with pytest.raises(installer.InstallError, match="MCP_ENTRY_CHANGED"):
        installer.uninstall(setup[0], modify_registration=True)
    assert setup[1].read_bytes() == before and Path(result["release"]).exists()


def test_concurrent_config_edit_prevents_merge(setup, monkeypatch):
    def change(*_):
        setup[1].write_text('{"mcpServers":{"new":{"command":"preserved"}}}')

    monkeypatch.setattr(installer, "smoke", change)
    with pytest.raises(installer.InstallError, match="INSTALL_FAILED"):
        install(setup)
    assert "hybrid-local" not in json.loads(setup[1].read_text())["mcpServers"]
    assert installer.load_receipt(setup[0])["status"] == "FAILED"


def test_active_or_uncertain_state_blocks_uninstall(setup):
    install(setup)
    state = setup[0] / "data/state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "hybrid-local.sqlite3") as db:
        db.execute("CREATE TABLE runs(status TEXT)")
        db.execute("INSERT INTO runs VALUES('REMOTE_STOP_UNCONFIRMED')")
    with pytest.raises(installer.InstallError, match="RUNS_ACTIVE_OR_UNCERTAIN"):
        installer.uninstall(setup[0], modify_registration=True)


def test_doctor_detects_source_tampering(setup):
    result = install(setup)
    (Path(result["release"]) / "source/scripts/hybrid-local.py").write_text("tampered")
    assert any(
        "SOURCE_HASH_MISMATCH" in p for p in installer.doctor(setup[0])["problems"]
    )


def test_discovery_is_readonly_and_never_reads_credentials(tmp_path, monkeypatch):
    root, registry = tmp_path / "Projet équipe 日本語", tmp_path / "registry.json"
    root.mkdir()
    (root / "opencode.json").write_text("private sentinel; deliberately invalid JSON")
    (root / "auth.json").write_text("private sentinel")
    assert (
        discover(registry) == {"workspaces": [], "registered": 0}
        and not registry.exists()
    )
    register(registry, "reporting", root, ["src"])
    before, original = registry.read_bytes(), Path.read_text

    def guarded(self, *args, **kwargs):
        if self.name in {"opencode.json", "auth.json"}:
            pytest.fail("Native config/credentials read")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    result = discover(registry)
    assert (
        result["registered"] == 1
        and registry.read_bytes() == before
        and "sentinel" not in json.dumps(result)
    )
    with pytest.raises(ValueError, match="already registered"):
        register(registry, "reporting", root, [])


def test_inline_config_presence_does_not_expose_content(tmp_path):
    result = configuration_locations(
        tmp_path,
        {
            "OPENCODE_CONFIG_CONTENT": "private sentinel",
            "OPENCODE_CONFIG": str(tmp_path / "explicit.json"),
        },
    )
    assert result["inline_config_present"] and "private sentinel" not in json.dumps(
        result
    )


def test_registration_overlap_traversal_fail_atomically(tmp_path):
    root, registry = tmp_path / "root", tmp_path / "registry.json"
    root.mkdir()
    register(registry, "first", root, ["src"])
    before = registry.read_bytes()
    with pytest.raises(HybridError):
        register(registry, "second", root, ["../escape"])
    assert registry.read_bytes() == before


@pytest.mark.parametrize(
    "value",
    [
        "C:relative",
        r"\\server\share",
        r"\\?\C:\root",
        "file.txt:secret",
        "trailing.",
        "trailing ",
        "NUL.txt",
        "COM1",
    ],
)
def test_windows_aliases_denied(value):
    with pytest.raises(ValueError):
        windows.validate_windows_path(value)


@pytest.mark.parametrize(
    "value", ["C:\\Projet équipe\\日本語\\src\\report.txt", "src/reporting file.txt"]
)
def test_windows_local_unicode_spaces_allowed(value):
    windows.validate_windows_path(value)


def test_actual_powershell_dry_run(tmp_path):
    pwsh, binary = (
        os.environ.get("HYBRID_TEST_PWSH"),
        os.environ.get("HYBRID_TEST_OPENCODE"),
    )
    if not pwsh or not binary:
        pytest.skip("Set research PowerShell and OpenCode paths for live dry run")
    root, config = (
        tmp_path / "Installation équipe 日本語",
        tmp_path / "Client configuration 日本語.json",
    )
    config.write_text('{"mcpServers":{"existing":{"command":"preserved"}}}')
    before = config.read_bytes()
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(SOURCE / "scripts/install-windows.ps1"),
            "-PythonPath",
            sys.executable,
            "-OpenCodePath",
            binary,
            "-InstallRoot",
            str(root),
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        json.loads(result.stdout)["status"] == "DRY_RUN"
        and not root.exists()
        and config.read_bytes() == before
    )


def test_windows_npm_shim_is_never_executed(tmp_path, monkeypatch):
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    shim = tmp_path / "opencode.cmd"
    shim.write_text("shell text must never execute")
    exe = (
        tmp_path
        / "node_modules/opencode-ai/node_modules/opencode-windows-x64/bin/opencode.exe"
    )
    exe.parent.mkdir(parents=True)
    exe.write_text("synthetic exe")
    assert windows.native_binary(str(shim)) == str(exe)
    exe.unlink()
    with pytest.raises(ValueError, match="native opencode.exe"):
        windows.native_binary(str(shim))


def test_empty_registry_stdio_without_opencode(tmp_path):
    registry = tmp_path / "Registry 日本語.json"
    registry.write_text('{"workspaces":{}}')
    environment = {
        **os.environ,
        "HYBRID_WORKSPACES_FILE": str(registry),
        "HYBRID_STATE_ROOT": str(tmp_path / "state"),
        "HYBRID_OPENCODE_BIN": "must-not-be-invoked",
    }
    result = subprocess.run(
        [sys.executable, str(SOURCE / "scripts/install-smoke.py")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "STDIO_SMOKE_OK" in result.stdout


class KernelFunction:
    def __init__(self, name, result, calls):
        self.name, self.result, self.calls = name, result, calls

    def __call__(self, *args):
        self.calls.append((self.name, args))
        return self.result


def mock_kernel(monkeypatch, *, assigned=True):
    import ctypes

    calls = []
    kernel = SimpleNamespace(
        **{
            name: KernelFunction(name, result, calls)
            for name, result in {
                "CreateJobObjectW": 101,
                "OpenProcess": 202,
                "SetInformationJobObject": 1,
                "AssignProcessToJobObject": int(assigned),
                "CloseHandle": 1,
            }.items()
        }
    )
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False
    )
    return calls


def test_windows_job_only_owns_started_pid(monkeypatch):
    calls = mock_kernel(monkeypatch)
    job = windows.OwnedWindowsJob(12345)
    assert next(args for name, args in calls if name == "OpenProcess")[-1] == 12345
    assert ("AssignProcessToJobObject", (101, 202)) in calls
    job.close()
    assert ("CloseHandle", (101,)) in calls
    calls.clear()
    job.close()
    assert not calls


def test_windows_job_assignment_failure_fails_closed(monkeypatch):
    calls = mock_kernel(monkeypatch, assigned=False)
    with pytest.raises(OSError, match="contain owned ACP"):
        windows.OwnedWindowsJob(12345)
    assert ("CloseHandle", (101,)) in calls and ("CloseHandle", (202,)) in calls


def test_duplicate_client_json_keys_rejected(setup):
    setup[1].write_text(
        '{"mcpServers":{"same":{"command":"first"},"same":{"command":"second"}}}'
    )
    before = setup[1].read_bytes()
    with pytest.raises(installer.InstallError, match="MCP_CONFIG_FORMAT"):
        install(setup)
    assert setup[1].read_bytes() == before and not setup[0].exists()


def test_locked_dependency_mismatch_detected(setup, monkeypatch):
    install(setup)
    original = installer.run

    def altered(command, **kwargs):
        return (
            '["mcp"]'
            if len(command) > 2 and command[1] == "-c" and "pip._vendor" in command[2]
            else original(command, **kwargs)
        )

    monkeypatch.setattr(installer, "run", altered)
    assert "DEPENDENCY_VERSION_MISMATCH:mcp" in installer.doctor(setup[0])["problems"]


def test_receipt_escape_denied(setup):
    install(setup)
    receipt = installer.load_receipt(setup[0])
    receipt["manifest"]["../../outside/auth.json"] = "a" * 64
    installer.atomic_json(installer.receipt_path(setup[0]), receipt)
    with pytest.raises(installer.InstallError, match="RECEIPT_INVALID"):
        installer.doctor(setup[0])


def test_subprocess_failure_redacts_private_output(monkeypatch):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="private sentinel", stderr="private sentinel"
        ),
    )
    with pytest.raises(installer.InstallError) as failure:
        installer.run(["synthetic"])
    assert "private sentinel" not in str(failure.value)


def test_retained_data_symlink_not_followed(setup, tmp_path):
    if os.name == "nt":
        pytest.skip("Actual Windows reparse behavior requires device validation")
    install(setup)
    installer.uninstall(setup[0], modify_registration=True)
    shutil.rmtree(setup[0] / "data")
    outside = tmp_path / "outside"
    outside.mkdir()
    (setup[0] / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="OWNED_PATH_CHANGED"):
        install(setup)
    assert not list(outside.iterdir())


def test_audit_covers_dangling_objects_without_values(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "audit_repository", SOURCE / "scripts/audit-repository.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "audit repository"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Synthetic audit fixture",
        ],
        capture_output=True,
        check=True,
    )
    synthetic = ("sk-" + "fixtureONLY" * 4).encode()
    subprocess.run(
        ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
        input=synthetic,
        capture_output=True,
        check=True,
    )
    result = module.audit(root)
    assert result["unreferenced_blobs"] == 1 and any(
        "provider_token" in f["classes"] for f in result["findings"]
    )
    assert synthetic.decode() not in json.dumps(result) and not list(
        root.glob("*.json")
    )
