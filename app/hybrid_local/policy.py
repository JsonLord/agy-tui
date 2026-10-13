from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .windows import validate_windows_path


class HybridError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    capabilities: frozenset[str]
    write_paths: tuple[str, ...] = ()

    def validate(self):
        if not self.root.is_dir() or self.root.resolve() != self.root:
            raise HybridError("WORKSPACE_ROOT_CHANGED")

    def file(self, value: str, *, write: bool = False) -> Path:
        self.validate()
        if os.name == "nt":
            try:
                validate_windows_path(value)
            except ValueError:
                raise HybridError("UNSUPPORTED_WINDOWS_PATH") from None
        candidate = Path(value)
        resolved = (
            candidate if candidate.is_absolute() else self.root / candidate
        ).resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError:
            raise HybridError("PATH_OUTSIDE_WORKSPACE") from None
        if any(
            part.casefold()
            in {
                ".git",
                ".env",
                "auth.json",
                ".opencode",
                ".gemini",
                "opencode.json",
                "opencode.jsonc",
            }
            or part.casefold().startswith(".env.")
            for part in relative.parts
        ):
            raise HybridError("PROTECTED_PATH")
        if write:
            if "edit" not in self.capabilities:
                raise HybridError("CAPABILITY_DENIED")
            if not any(
                relative == Path(p) or Path(p) in relative.parents
                for p in self.write_paths
            ):
                raise HybridError("WRITE_SCOPE_DENIED")
        return resolved


class WorkspaceRegistry:
    def __init__(self, path: Path):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))["workspaces"]
            if not isinstance(entries, dict):
                raise TypeError()
            self.workspaces = {}
            for key, item in entries.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key):
                    raise ValueError()
                root = Path(item["root"])
                if not root.is_absolute() or not root.is_dir():
                    raise ValueError()
                root = root.resolve()
                caps = frozenset(item.get("capabilities", ["read"]))
                paths = item.get("write_paths", [])
                if (
                    not caps <= {"read", "edit"}
                    or "read" not in caps
                    or not isinstance(paths, list)
                ):
                    raise ValueError()
                for p in paths:
                    if (
                        not isinstance(p, str)
                        or not p
                        or Path(p).is_absolute()
                        or ".." in Path(p).parts
                        or p == "."
                    ):
                        raise ValueError()
                    if os.name == "nt":
                        validate_windows_path(p)
                if any(
                    root == w.root or root in w.root.parents or w.root in root.parents
                    for w in self.workspaces.values()
                ):
                    raise ValueError()
                self.workspaces[key] = Workspace(key, root, caps, tuple(paths))
        except (OSError, ValueError, KeyError, TypeError):
            raise HybridError("INVALID_WORKSPACE_REGISTRY") from None

    def get(self, workspace_id: str) -> Workspace:
        if workspace_id not in self.workspaces:
            raise HybridError("UNKNOWN_WORKSPACE")
        workspace = self.workspaces[workspace_id]
        workspace.validate()
        return workspace


ROLES = frozenset({"engineer", "reviewer", "supervisor"})


def native_environment(
    workspace: Workspace, role: str, inherited: dict | None = None
) -> dict:
    if role not in ROLES:
        raise HybridError("INVALID_ROLE")
    if role == "engineer" and "edit" not in workspace.capabilities:
        raise HybridError("CAPABILITY_DENIED")
    env = dict(os.environ if inherited is None else inherited)
    for key in list(env):
        if key.upper().startswith("ARGUS_"):
            del env[key]
    try:
        config = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
        if not isinstance(config, dict) or not isinstance(
            config.get("agent", {}), dict
        ):
            raise TypeError()
    except (ValueError, TypeError):
        raise HybridError("INVALID_NATIVE_CONFIG_CONTENT") from None
    permission = {
        "*": "deny",
        "read": "ask",
        "glob": "ask",
        "grep": "ask",
        "list": "ask",
        "external_directory": "deny",
        "bash": "deny",
        "edit": "ask" if role == "engineer" else "deny",
    }
    config.setdefault("agent", {})[f"hybrid-local-{role}"] = {
        "description": f"Bounded hybrid-local {role}",
        "mode": "primary",
        "permission": permission,
    }
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    env["OPENCODE_PERMISSION"] = json.dumps(permission)
    return env


def permission_response(
    workspace: Workspace, role: str, params: dict, session_id: str
) -> tuple[dict, bool]:
    tool = params.get("toolCall", {})
    locations = tool.get("locations", [])
    allowed = False
    if (
        params.get("sessionId") == session_id
        and locations
        and tool.get("kind") in {"read", "search", "edit"}
    ):
        try:
            if tool["kind"] == "edit" and role != "engineer":
                raise HybridError("CAPABILITY_DENIED")
            for location in locations:
                workspace.file(location["path"], write=tool["kind"] == "edit")
            allowed = True
        except (HybridError, KeyError, TypeError, ValueError):
            allowed = False
    option = (
        next(
            (
                o["optionId"]
                for o in params.get("options", [])
                if o.get("kind") == "allow_once"
            ),
            None,
        )
        if allowed
        else None
    )
    return (
        {"outcome": {"outcome": "selected", "optionId": option}}
        if option
        else {"outcome": {"outcome": "cancelled"}}
    ), bool(option)
