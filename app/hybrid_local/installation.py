"""Transactional isolated installation; additive stdio config, no OpenCode credential copies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from .capabilities import detect_capabilities, validate_version
from .policy import HybridError
from .windows import OpenCodeResolutionError, native_binary

SERVER = "hybrid-local"
OWNER = "agy-argus-hybrid-phase12-v1"
CHECK_DEPENDENCIES = r"""
import importlib.metadata as metadata
import json, re, sys
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
mismatches = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not re.match(r"^[A-Za-z0-9_-]+==", line):
        continue
    requirement = Requirement(line.rstrip().removesuffix("\\").strip())
    if requirement.marker and not requirement.marker.evaluate():
        continue
    try:
        valid = metadata.version(requirement.name) in requirement.specifier
    except metadata.PackageNotFoundError:
        valid = False
    if not valid:
        mismatches.append(requirement.name)
print(json.dumps(mismatches))
"""


class InstallError(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def atomic_json(path, document, expected=None, *, compare=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".agy-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if compare and (path.read_bytes() if path.exists() else None) != expected:
            raise InstallError(
                "CONFIG_CHANGED: close the client, review changes and retry after rollback"
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def config_document(path):
    before = path.read_bytes() if path.exists() else None
    try:
        document = (
            json.loads(before.decode("utf-8-sig"), object_pairs_hook=unique_keys)
            if before
            else {}
        )
        if not isinstance(document, dict) or not isinstance(
            document.get("mcpServers", {}), dict
        ):
            raise TypeError()
    except (ValueError, UnicodeError, TypeError):
        raise InstallError(
            "MCP_CONFIG_FORMAT: expected strict JSON with unique mcpServers keys"
        ) from None
    return document, before


def run(command, *, environment=None, timeout=120, include_stderr=False):
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(
            "PROCESS_FAILED: verify executable, permissions, network, proxy and CA trust"
        ) from None
    if result.returncode:
        raise InstallError(
            "PROCESS_FAILED: dependency/prerequisite command failed; check versions, proxy/CA trust and wheel availability"
        )
    return result.stdout + (result.stderr if include_stderr else "")


def preflight(opencode, node):
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise InstallError("PYTHON_VERSION: use Python 3.11–3.13")
    try:
        binary = native_binary(opencode)
        version = run([binary, "--version"]).strip()
        validate_version(version)
        help_text = run([binary, "acp", "--help"], include_stderr=True)
        capabilities = detect_capabilities(version, help_text)
    except (HybridError, OpenCodeResolutionError) as error:
        raise InstallError(
            error.code
            + ": existing OpenCode capability/resolution check failed; do not replace the installation"
        ) from None
    node_path = shutil.which(node) or node
    node_version = run([node_path, "--version"]).strip()
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+", node_version)
    if not match or int(match[1]) not in {22, 24}:
        raise InstallError("NODE_VERSION: use Node.js 22 or 24 LTS")
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "node": node_version,
        "node_binary": str(Path(node_path).resolve()),
        "opencode": version,
        "opencode_binary": binary,
        "opencode_capabilities": capabilities.describe(),
    }


def portable_files(source):
    names = [
        "app/__init__.py",
        "app/schemas.py",
        "requirements-windows.lock",
        "scripts/hybrid-local.py",
        "scripts/install-smoke.py",
        "scripts/workspace-discovery.py",
        "scripts/windows-readiness.py",
        "scripts/install-windows.ps1",
        "scripts/doctor-windows.ps1",
        "scripts/uninstall-windows.ps1",
        "scripts/opencode-doctor.py",
    ]
    names.extend(
        str(p.relative_to(source))
        for p in sorted((source / "app/hybrid_local").glob("*.py"))
    )
    paths = [source / name for name in names]
    if not all(p.is_file() and not p.is_symlink() for p in paths):
        raise InstallError("SOURCE_INCOMPLETE: use a complete reviewed checkout")
    return paths


def python_in(release):
    return (
        release / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


def receipt_path(root):
    return root / "installation.json"


def validate_owned_paths(root):
    for relative in (
        "installation.json",
        "releases",
        "data",
        "data/state",
        "data/workspaces.json",
    ):
        path = root / relative
        if path.is_symlink() or (path.exists() and path.resolve() != path):
            raise InstallError("OWNED_PATH_CHANGED: review unexpected link/junction")


def load_receipt(root):
    validate_owned_paths(root)
    try:
        receipt = json.loads(receipt_path(root).read_text(encoding="utf-8"))
        release = Path(receipt["release"])
        if (
            receipt.get("owner") != OWNER
            or receipt.get("root") != str(root)
            or release.parent != root / "releases"
            or not re.fullmatch(r"[0-9a-f]{32}", release.name)
            or release.is_symlink()
            or (release.exists() and release.resolve() != release)
        ):
            raise ValueError()
        if not isinstance(receipt["manifest"], dict):
            raise TypeError()
        for name, value in receipt["manifest"].items():
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or not re.fullmatch(r"[0-9a-f]{64}", value)
            ):
                raise ValueError()
        if receipt["entry"]["command"] != str(python_in(release)) or receipt["entry"][
            "args"
        ] != [str(release / "source/scripts/hybrid-local.py")]:
            raise ValueError()
        return receipt
    except (OSError, ValueError, KeyError, TypeError):
        raise InstallError(
            "RECEIPT_INVALID: inspect ownership; never delete an unowned directory"
        ) from None


def smoke(release, entry):
    environment = dict(os.environ)
    environment.update(entry["env"])
    output = run(
        [str(python_in(release)), str(release / "source/scripts/install-smoke.py")],
        environment=environment,
    )
    if "STDIO_SMOKE_OK" not in output:
        raise InstallError("STDIO_SMOKE_FAILED")


def install(
    source, root, config, opencode, node, *, dry_run=False, manual_registration=True
):
    source, root = source.resolve(), root.resolve()
    config = config.resolve() if config is not None else None
    if not manual_registration and config is None:
        raise InstallError("MCP_CONFIG_REQUIRED_FOR_EXPLICIT_LEGACY_MODE")
    if (
        root == source
        or root in source.parents
        or source in root.parents
        or (
            config is not None
            and (
                root == config
                or root in config.parents
                or source == config
                or source in config.parents
            )
        )
    ):
        raise InstallError(
            "PATH_LAYOUT: source, installation and client config must be separate"
        )
    versions = preflight(opencode, node)
    validate_owned_paths(root)
    manifest = {str(p.relative_to(source)): digest(p) for p in portable_files(source)}
    document, before = {}, None
    if not manual_registration:
        document, before = config_document(config)
        if config.name.lower() in {
            "opencode.json",
            "opencode.jsonc",
            "auth.json",
        } or any(key in document for key in ("provider", "model", "agent", "mcp")):
            raise InstallError(
                "NATIVE_CONFIG_PROTECTED: select client mcpServers config, never OpenCode settings"
            )
        if SERVER in document.get("mcpServers", {}):
            raise InstallError(
                "MCP_NAME_COLLISION: doctor or uninstall existing owned installation"
            )
    if (
        root.exists()
        and any(root.iterdir())
        and load_receipt(root)["status"] != "UNINSTALLED"
    ):
        raise InstallError(
            "INSTALLATION_EXISTS: doctor then uninstall before reinstalling"
        )
    plan = {
        "status": "DRY_RUN" if dry_run else "INSTALLING",
        "root": str(root),
        "mcp_config": str(config) if not manual_registration else None,
        "registration_mode": "manual"
        if manual_registration
        else "automatic_explicit_opt_in",
        "versions": versions,
        "files": sorted(manifest),
        "network_listeners": False,
        "workspaces_registered": False,
    }
    if dry_run:
        return plan
    release = root / "releases" / uuid.uuid4().hex
    entry = {
        "command": str(python_in(release)),
        "args": [str(release / "source/scripts/hybrid-local.py")],
        "env": {
            "HYBRID_WORKSPACES_FILE": str(root / "data/workspaces.json"),
            "HYBRID_STATE_ROOT": str(root / "data/state"),
            "HYBRID_OPENCODE_BIN": versions["opencode_binary"],
        },
    }
    receipt = {
        "owner": OWNER,
        "root": str(root),
        "release": str(release),
        "status": "PREPARING",
        "mcp_config": str(config) if not manual_registration else None,
        "registration_mode": "manual"
        if manual_registration
        else "automatic_explicit_opt_in",
        "entry": entry,
        "manifest": manifest,
        "versions": versions,
        "config_sha_before": hashlib.sha256(before).hexdigest() if before else None,
    }
    atomic_json(receipt_path(root), receipt)
    try:
        for name, expected_hash in manifest.items():
            target = release / "source" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
            if digest(target) != expected_hash:
                raise InstallError("SOURCE_CHANGED")
        receipt["status"] = "DEPENDENCIES"
        atomic_json(receipt_path(root), receipt)
        run([sys.executable, "-m", "venv", str(release / "venv")])
        run(
            [
                str(python_in(release)),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "--only-binary=:all:",
                "-r",
                str(release / "source/requirements-windows.lock"),
            ],
            timeout=600,
        )
        registry = root / "data/workspaces.json"
        if not registry.exists():
            atomic_json(registry, {"workspaces": {}})
        smoke(release, entry)
        if manual_registration:
            atomic_json(root / "mcp-entry.json", {"mcpServers": {SERVER: entry}})
        receipt["status"] = (
            "REGISTRATION_MANUAL" if manual_registration else "REGISTERING"
        )
        atomic_json(receipt_path(root), receipt)
        if not manual_registration:
            document.setdefault("mcpServers", {})[SERVER] = entry
            atomic_json(config, document, before, compare=True)
        receipt["status"] = "INSTALLED"
        if not manual_registration:
            receipt["config_sha_after"] = digest(config)
        atomic_json(receipt_path(root), receipt)
    except Exception as error:
        receipt["failed_phase"], receipt["status"] = receipt["status"], "FAILED"
        atomic_json(receipt_path(root), receipt)
        raise InstallError(
            "INSTALL_FAILED: doctor reports last phase; uninstall safely rolls back owned partial installation"
        ) from error
    return {
        **plan,
        "status": "INSTALLED",
        "release": str(release),
        "mcp_entry": {"mcpServers": {SERVER: entry}},
        "registration_status": "USER_ACTION_REQUIRED"
        if manual_registration
        else "REGISTERED",
    }


def doctor(root, config=None):
    root = root.resolve()
    receipt = load_receipt(root)
    release, problems = Path(receipt["release"]), []
    if receipt["status"] != "INSTALLED":
        problems.append("INSTALLATION_" + receipt["status"])
    for name, value in receipt["manifest"].items():
        path = release / "source" / name
        if not path.is_file() or digest(path) != value:
            problems.append("SOURCE_HASH_MISMATCH:" + name)
    manual = receipt.get("registration_mode") == "manual"
    registration_status = "UNVERIFIED_MANUAL" if manual else "UNVERIFIED_LEGACY"
    target = config  # Client config is read only when explicitly requested.
    if target is not None:
        document, _ = config_document(target)
        if document.get("mcpServers", {}).get(SERVER) != receipt["entry"]:
            registration_status = "MISSING_OR_DIFFERENT"
            problems.append("MCP_REGISTRATION_MISMATCH")
        else:
            registration_status = "VERIFIED_EXPLICIT_READ_ONLY"
    if not python_in(release).is_file():
        problems.append("VENV_MISSING")
    else:
        try:
            run([str(python_in(release)), "-m", "pip", "check"])
            run([str(python_in(release)), "-c", "import mcp,pydantic,sqlite3"])
            mismatches = json.loads(
                run(
                    [
                        str(python_in(release)),
                        "-c",
                        CHECK_DEPENDENCIES,
                        str(release / "source/requirements-windows.lock"),
                    ]
                )
            )
            problems.extend(
                "DEPENDENCY_VERSION_MISMATCH:" + name for name in mismatches
            )
            versions = preflight(
                receipt["entry"]["env"]["HYBRID_OPENCODE_BIN"],
                receipt["versions"].get("node_binary", "node"),
            )
            problems.extend(
                versions.get("opencode_capabilities", {}).get("worker_blockers", [])
            )
        except (InstallError, ValueError):
            problems.append("PREREQUISITES_OR_DEPENDENCIES_FAILED")
    return {
        "status": "NEEDS_ATTENTION" if problems else "HEALTHY",
        "problems": problems,
        "installation_phase": receipt["status"],
        "registration_status": registration_status,
        "root": str(root),
        "native_windows_validation": "PENDING",
        "action": "Review phase, rollback and reinstall"
        if problems
        else "Ready for Phase 1.5 device validation",
    }


def uninstall(root, *, dry_run=False, modify_registration=False):
    root = root.resolve()
    receipt = load_receipt(root)
    if receipt["status"] == "UNINSTALLED":
        return {"status": "UNINSTALLED", "data_retained": True}
    manual = receipt.get("registration_mode") == "manual"
    config, document, before, existing = None, {}, None, None
    if not manual:
        if not modify_registration:
            raise InstallError(
                "MANUAL_REMOVAL_REQUIRED: legacy automatic receipt; review/remove client entry yourself, do not use automatic rollback"
            )
        config = Path(receipt["mcp_config"])
        document, before = config_document(config)
        existing = document.get("mcpServers", {}).get(SERVER)
        if existing is not None and existing != receipt["entry"]:
            raise InstallError(
                "MCP_ENTRY_CHANGED: review edited entry before rollback; no files removed"
            )
    for database in (root / "data/state").glob("*.sqlite3"):
        try:
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                busy = db.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('STARTING','RUNNING','REMOTE_STOP_UNCONFIRMED')"
                ).fetchone()[0]
            finally:
                db.close()
            if busy:
                raise InstallError(
                    "RUNS_ACTIVE_OR_UNCERTAIN: verify owned sessions stopped before rollback"
                )
        except sqlite3.Error:
            raise InstallError(
                "STATE_UNREADABLE: review evidence before rollback"
            ) from None
    release = Path(receipt["release"])
    if release.exists() and os.name == "nt":
        for directory, directories, files in os.walk(release, followlinks=False):
            for path in [
                Path(directory),
                *(Path(directory) / name for name in directories + files),
            ]:
                if getattr(path.lstat(), "st_file_attributes", 0) & 0x0400:
                    raise InstallError("RELEASE_LINK: review unexpected reparse point")
    if dry_run:
        return {
            "status": "DRY_RUN",
            "remove_server": existing is not None,
            "data_retained": True,
        }
    if existing is not None:
        del document["mcpServers"][SERVER]
        atomic_json(config, document, before, compare=True)
    if release.exists():
        shutil.rmtree(release)
    receipt["status"] = "UNINSTALLED"
    atomic_json(receipt_path(root), receipt)
    return {
        "status": "UNINSTALLED",
        "data_retained": True,
        "mcp_config_retained": True,
        "registration_action": "Remove your manually inserted client entry yourself"
        if manual
        else "owned entry removed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "doctor", "uninstall"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--mcp-config", type=Path)
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--node", default="node")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "install":
            if args.mcp_config is not None:
                parser.error(
                    "Manual registration only: installation never accepts a client config path; use doctor --mcp-config for read-only validation"
                )
            result = install(
                args.source,
                args.root,
                args.mcp_config,
                args.opencode,
                args.node,
                dry_run=args.dry_run,
            )
        else:
            result = (
                doctor(args.root, args.mcp_config)
                if args.action == "doctor"
                else uninstall(args.root, dry_run=args.dry_run)
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2 if result["status"] == "NEEDS_ATTENTION" else 0
    except (InstallError, OSError, ValueError, TypeError):
        error = sys.exc_info()[1]
        print(
            str(error)
            if isinstance(error, InstallError)
            else "FILESYSTEM_OR_CONFIG_ERROR: verify JSON, paths and permissions",
            file=sys.stderr,
        )
        return 2
