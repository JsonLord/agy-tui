"""Read only explicit AGY project mapping; never inspect Argus/native credentials."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.factory_local.store import FactoryError

from .protocol import ProjectRegistry, unique_object


def load_registry(path):
    if not path or not Path(path).is_absolute():
        raise FactoryError("ARGUS_PROJECT_REGISTRY_REQUIRED")
    target = Path(path)
    forbidden = {
        ".argus-skill",
        ".gemini",
        ".opencode",
        "auth.json",
        "opencode.json",
        "opencode.jsonc",
        ".env",
    }
    if (
        any(part.casefold() in forbidden for part in target.resolve().parts)
        or target.is_symlink()
    ):
        raise FactoryError("ARGUS_PROJECT_REGISTRY_INVALID")
    try:
        with target.open("rb") as handle:
            raw = handle.read(128_001)
        if len(raw) > 128_000:
            raise ValueError()
        return ProjectRegistry.model_validate(
            json.loads(raw, object_pairs_hook=unique_object)
        )
    except (OSError, ValueError, ValidationError):
        raise FactoryError("ARGUS_PROJECT_REGISTRY_INVALID") from None
