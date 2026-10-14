"""Source-derived capability contracts; no real 2.x process installed or started."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from app.hybrid_local import installation as installer
from app.hybrid_local.acp import OpenCodeACP
from app.hybrid_local.capabilities import detect_capabilities
from app.hybrid_local.policy import HybridError, Workspace, native_environment

LEGACY_HELP = "acp --pure --cwd --hostname --port"
MODERN_HELP = "Start an Agent Client Protocol server --login"


def test_legacy_pinned_flags_and_absolute_cwd(tmp_path):
    cap = detect_capabilities("1.18.35", LEGACY_HELP)
    args = cap.acp_arguments(tmp_path)
    cap.require_worker()
    assert args == (
        "acp",
        "--pure",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        "--cwd",
        str(tmp_path),
    )


def test_2x_source_contract_has_private_loopback_child_and_blockers(tmp_path):
    cap = detect_capabilities("2.0.28", MODERN_HELP)
    assert cap.acp_arguments(tmp_path) == ("acp",)
    assert cap.cwd_mode == "os_cwd_and_absolute_acp_session"
    assert cap.transport_mode == "stdio_with_private_http_child"
    assert cap.bind_control == "source_default_127.0.0.1_port_0"
    assert cap.worker_blockers == (
        "OPENCODE_NATIVE_STATE_MUTATION",
        "OPENCODE_PLUGIN_ISOLATION_UNVERIFIED",
    )
    with pytest.raises(HybridError, match="OPENCODE_NATIVE_STATE_MUTATION"):
        cap.require_worker()
    assert not cap.describe()["worker_ready"]


@pytest.mark.parametrize(
    "version", ["2.0.29", "2.1.0", "3.0.0", "1.0.0", "1.18.36", "garbage"]
)
def test_unknown_version_fails_safely(version):
    with pytest.raises(HybridError):
        detect_capabilities(version, MODERN_HELP)


@pytest.mark.parametrize(
    "version,help_text",
    [("1.18.35", "--cwd"), ("2.0.28", LEGACY_HELP), ("2.0.28", "--unknown")],
)
def test_version_help_disagreement_is_not_bypass(version, help_text):
    with pytest.raises(HybridError):
        detect_capabilities(version, help_text)


def test_candidate_requires_absolute_path():
    with pytest.raises(HybridError, match="WORKSPACE_CWD_NOT_ABSOLUTE"):
        detect_capabilities("2.0.28", MODERN_HELP).acp_arguments(Path("relative"))


@pytest.mark.parametrize("role", ["engineer", "reviewer", "supervisor"])
def test_native_environment_keeps_auth_and_config_only_argus_removed(tmp_path, role):
    workspace = Workspace("fixture", tmp_path, frozenset({"read", "edit"}), ("src",))
    original = {
        "OPENCODE_CONFIG": "native config",
        "NATIVE_AUTH_SENTINEL": "test-only",
        "OPENCODE_CONFIG_CONTENT": '{"model":"native/model","provider":{"native":{}}}',
        "ARGUS_SKILL_MODEL": "bad/model",
        "ARGUS_SKILL_OPENCODE_PROVIDER": "bad",
    }
    result = native_environment(workspace, role, original)
    assert (
        result["NATIVE_AUTH_SENTINEL"] == "test-only"
        and result["OPENCODE_CONFIG"] == "native config"
    )
    assert not any(k.startswith("ARGUS_") for k in result)
    config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
    assert config["model"] == "native/model" and config["provider"] == {"native": {}}
    agent = config["agent"]["hybrid-local-" + role]
    assert "model" not in agent and "provider" not in agent
    assert agent["permission"]["edit"] == ("ask" if role == "engineer" else "deny")


def test_blocked_native_executable_never_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "native_binary", lambda _: "/blocked/native")

    def fail(*_, **__):
        raise installer.InstallError("PROCESS_FAILED")

    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(installer.InstallError, match="PROCESS_FAILED"):
        installer.preflight("opencode", "node")


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX executable fixture; Windows Job Object tests remain separate",
)
def test_modern_probe_blocks_before_acp_and_native_files_untouched(tmp_path):
    binary = tmp_path / "opencode synthetic"
    marker = tmp_path / "unsafe-acp-start"
    config = tmp_path / "opencode.json"
    config.write_text("native sentinel")
    before = config.read_bytes()
    binary.write_text(
        "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
        'if "--version" in sys.argv: print("2.0.28")\n'
        'elif "--help" in sys.argv: print("acp --login")\n'
        f'else: Path({str(marker)!r}).write_text("unexpected start")\n'
    )
    binary.chmod(0o700)

    async def check():
        workspace = Workspace("fixture", tmp_path, frozenset({"read"}))
        client = OpenCodeACP(str(binary), workspace, "reviewer")
        with pytest.raises(HybridError, match="OPENCODE_NATIVE_STATE_MUTATION"):
            async with client:
                pytest.fail("unsafe worker activated")
        assert client.process is None

    asyncio.run(check())
    assert not marker.exists() and config.read_bytes() == before


def test_manual_install_fragment_doctor_explicit_only(tmp_path, monkeypatch):
    root = tmp_path / "Install équipe 日本語"
    config = tmp_path / "actual-client.json"
    config.write_text('{"mcpServers":{"existing":{"command":"preserve"}}}')
    before = config.read_bytes()
    monkeypatch.setattr(
        installer,
        "preflight",
        lambda *_: {
            "opencode_binary": "native",
            "opencode_capabilities": detect_capabilities(
                "2.0.28", MODERN_HELP
            ).describe(),
        },
    )

    def fake(command, **_):
        if command[1:3] == ["-m", "venv"]:
            python = installer.python_in(Path(command[3]).parent)
            python.parent.mkdir(parents=True)
            python.write_text("synthetic")
        if "-c" in command and "pip._vendor" in command[2]:
            return "[]"
        return "STDIO_SMOKE_OK"

    monkeypatch.setattr(installer, "run", fake)
    source = Path(__file__).resolve().parents[1]
    result = installer.install(source, root, None, "native", "node")
    assert result["registration_status"] == "USER_ACTION_REQUIRED"
    assert (
        json.loads((root / "mcp-entry.json").read_text(encoding="utf-8"))
        == result["mcp_entry"]
    )
    assert config.read_bytes() == before
    original = installer.config_document
    monkeypatch.setattr(
        installer,
        "config_document",
        lambda _: pytest.fail("implicitly read client config"),
    )
    doctor = installer.doctor(root)
    assert doctor["registration_status"] == "UNVERIFIED_MANUAL"
    assert (
        doctor["status"] == "NEEDS_ATTENTION"
        and "OPENCODE_NATIVE_STATE_MUTATION" in doctor["problems"]
    )
    monkeypatch.setattr(installer, "config_document", original)
    result_check = installer.doctor(root, config)
    assert result_check["registration_status"] == "MISSING_OR_DIFFERENT"
    assert config.read_bytes() == before
    # The user action, not installer, explicitly inserts the entry in this synthetic test.
    document = json.loads(config.read_text())
    document["mcpServers"].update(result["mcp_entry"]["mcpServers"])
    config.write_text(json.dumps(document))
    registered = config.read_bytes()
    assert (
        installer.doctor(root, config)["registration_status"]
        == "VERIFIED_EXPLICIT_READ_ONLY"
    )
    installer.uninstall(root)
    assert config.read_bytes() == registered  # User removes their own entry.


def test_manual_install_never_parses_supplied_native_config(tmp_path, monkeypatch):
    config = tmp_path / "opencode.json"
    config.write_text("credential sentinel; intentionally invalid JSON")
    before = config.read_bytes()
    monkeypatch.setattr(
        installer, "preflight", lambda *_: {"opencode_binary": "native"}
    )
    source = Path(__file__).resolve().parents[1]
    result = installer.install(
        source, tmp_path / "install", config, "native", "node", dry_run=True
    )
    assert result["registration_mode"] == "manual" and config.read_bytes() == before


def test_doctor_legacy_client_still_explicit(tmp_path, monkeypatch):
    # Direct API legacy opt-in remains tested; no user-facing CLI opts into mutation.
    # Only test the receipt/registration read before venv checks; version/helpers are fixture controlled.
    root = tmp_path / "install"
    config = tmp_path / "client.json"
    config.write_text("{}")
    monkeypatch.setattr(
        installer, "preflight", lambda *_: {"opencode_binary": "native"}
    )

    def fake(command, **_):
        if command[1:3] == ["-m", "venv"]:
            python = installer.python_in(Path(command[3]).parent)
            python.parent.mkdir(parents=True)
            python.write_text("fixture")
        return (
            "[]"
            if "-c" in command and "pip._vendor" in command[2]
            else "STDIO_SMOKE_OK"
        )

    monkeypatch.setattr(installer, "run", fake)
    installer.install(
        Path(__file__).resolve().parents[1],
        root,
        config,
        "native",
        "node",
        manual_registration=False,
    )
    with pytest.raises(installer.InstallError, match="MANUAL_REMOVAL_REQUIRED"):
        installer.uninstall(root)
    monkeypatch.setattr(
        installer, "config_document", lambda _: pytest.fail("implicit client read")
    )
    assert installer.doctor(root)["registration_status"] == "UNVERIFIED_LEGACY"


def test_windows_candidate_cwd_is_absolute_without_deprecated_arguments():
    from pathlib import PureWindowsPath

    root = PureWindowsPath("C:/Projects équipe/日本語 Alpha")
    cap = detect_capabilities("2.0.28", MODERN_HELP)
    assert root.is_absolute() and cap.acp_arguments(root) == ("acp",)
    assert detect_capabilities("1.18.35", LEGACY_HELP).acp_arguments(root)[-1] == str(
        root
    )


def test_unknown_preflight_stops_before_acp_help(monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "native_binary", lambda _: "native")

    def version_only(command, **_):
        calls.append(command)
        assert command == ["native", "--version"]
        return "2.0.29"

    monkeypatch.setattr(installer, "run", version_only)
    with pytest.raises(installer.InstallError, match="CAPABILITIES_UNVERIFIED"):
        installer.preflight("native", "node")
    assert calls == [["native", "--version"]]


def test_readonly_diagnostic_refuses_credential_hashes(tmp_path):
    import subprocess
    import sys

    auth = tmp_path / "auth.json"
    auth.write_text("private sentinel never printed")
    source = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(source / "scripts/opencode-doctor.py"),
            "--hash-config",
            str(auth),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "private sentinel" not in result.stdout + result.stderr
    assert "never supply auth/token files" in result.stderr


def test_readonly_diagnostic_compares_config_hashes_not_values(
    tmp_path, monkeypatch, capsys
):
    import importlib.util
    import sys

    config = tmp_path / "opencode.json"
    config.write_text("opaque private project value")
    source = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "readonly_doctor", source / "scripts/opencode-doctor.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "preflight",
        lambda *_: {
            "opencode_capabilities": detect_capabilities(
                "2.0.28", MODERN_HELP
            ).describe()
        },
    )
    monkeypatch.setattr(sys, "argv", ["doctor", "--hash-config", str(config)])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "opaque private project value" not in output
    evidence = json.loads(output)["config_hashes"][0]
    assert (
        evidence["unchanged"] and evidence["sha256_before"] == evidence["sha256_after"]
    )
