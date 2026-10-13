"""Read-only OpenCode configuration-location discovery; never read auth/config contents."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .policy import WorkspaceRegistry


def configuration_locations(root: Path, environment: dict | None = None) -> dict:
    env = os.environ if environment is None else environment
    paths, current = [], root.resolve()
    while True:
        paths.extend(
            current / name for name in ("opencode.json", "opencode.jsonc", ".opencode")
        )
        if (current / ".git").exists() or current.parent == current:
            break
        current = current.parent
    config_home = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    paths.extend(
        config_home / "opencode" / name for name in ("opencode.json", "opencode.jsonc")
    )
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR"):
        if env.get(name):
            paths.append(Path(env[name]))
    return {
        "locations": [
            {"path": str(p), "exists": p.exists()} for p in dict.fromkeys(paths)
        ],
        "inline_config_present": bool(env.get("OPENCODE_CONFIG_CONTENT")),
        "resolution": "Locations only; native OpenCode owns parsing, precedence and effective model",
        "credential_files_opened": False,
    }


def discover(registry: Path) -> dict:
    workspaces = WorkspaceRegistry(registry).workspaces if registry.exists() else {}
    return {
        "workspaces": [
            {
                "id": w.id,
                "root": str(w.root),
                "capabilities": sorted(w.capabilities),
                "write_paths": list(w.write_paths),
                "configuration": configuration_locations(w.root),
            }
            for w in workspaces.values()
        ],
        "registered": len(workspaces),
    }


def register(registry: Path, workspace_id: str, root: Path, write_paths: list[str]):
    before = registry.read_bytes() if registry.exists() else None
    document = json.loads(before) if before else {"workspaces": {}}
    if workspace_id in document["workspaces"]:
        raise ValueError("Workspace already registered; review before changing scope")
    document["workspaces"][workspace_id] = {
        "root": str(root.resolve()),
        "capabilities": ["read", "edit"] if write_paths else ["read"],
        "write_paths": write_paths,
    }
    registry.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".workspace-", dir=registry.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, ensure_ascii=False)
        WorkspaceRegistry(temporary)
        if (registry.read_bytes() if registry.exists() else None) != before:
            raise ValueError("Registry changed concurrently")
        os.replace(temporary, registry)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(
            os.environ.get("HYBRID_WORKSPACES_FILE", "config/workspaces.local.json")
        ),
    )
    parser.add_argument("--register-id")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--write-path", action="append", default=[])
    args = parser.parse_args()
    if args.register_id:
        if not args.root:
            parser.error("--register-id requires --root")
        register(args.registry, args.register_id, args.root, args.write_path)
    elif args.root or args.write_path:
        parser.error("Registration must be explicit with --register-id")
    print(json.dumps(discover(args.registry), indent=2, ensure_ascii=False))
