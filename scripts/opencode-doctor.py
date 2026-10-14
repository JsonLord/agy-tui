#!/usr/bin/env python3
"""Read-only resolver/version/help/config-hash diagnostic; never opens an ACP session."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.hybrid_local.installation import InstallError, preflight
from app.hybrid_local.windows import OpenCodeResolutionError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode", default="opencode")
    parser.add_argument("--node", default="node")
    parser.add_argument("--workspace", action="append", type=Path, default=[])
    parser.add_argument("--hash-config", action="append", type=Path, default=[])
    args = parser.parse_args()
    hashes = []
    for path in args.hash_config:
        if (
            path.resolve().name.casefold() not in {"opencode.json", "opencode.jsonc"}
            or not path.is_file()
        ):
            parser.error(
                "--hash-config accepts existing opencode.json/jsonc only; never supply auth/token files"
            )
        hashes.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    roots = []
    for path in args.workspace:
        if not path.is_absolute() or not path.is_dir():
            parser.error("--workspace must be an existing absolute local path")
        roots.append(str(path.resolve()))
    # Preserve all native environment; Argus selectors are irrelevant to CLI probes and removed.
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.upper().startswith("ARGUS_")
    }
    try:
        for key in saved:
            del os.environ[key]
        result = preflight(args.opencode, args.node)
        for entry in hashes:
            entry["sha256_before"] = entry.pop("sha256")
            entry["sha256_after"] = hashlib.sha256(
                Path(entry["path"]).read_bytes()
            ).hexdigest()
            entry["unchanged"] = entry["sha256_before"] == entry["sha256_after"]
        print(
            json.dumps(
                {
                    "versions": result,
                    "workspace_paths_only": roots,
                    "config_hashes": hashes,
                    "sessions_started": False,
                    "configuration_values_parsed_or_displayed": False,
                    "note": "Hashes consume file bytes without parsing/displaying values; workspace/model/provider runtime validation remains blocked/pending.",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if all(entry["unchanged"] for entry in hashes) else 2
    except (InstallError, OpenCodeResolutionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        os.environ.update(saved)


if __name__ == "__main__":
    raise SystemExit(main())
