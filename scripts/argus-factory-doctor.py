#!/usr/bin/env python3
"""Read-only external Argus stdio compatibility check; never sends a goal."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.argus_local.client import ArgusMcpClient
from app.factory_local.store import FactoryError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="argus-plugin-server")
    args = parser.parse_args()
    try:
        projects = asyncio.run(
            ArgusMcpClient(args.executable, timeout=15).project_list()
        )
        print(
            json.dumps(
                {
                    "status": "COMPATIBLE_TOOL_SCHEMAS",
                    "projects_count": len(projects),
                    "transport": "stdio",
                    "version_verified": False,
                    "planning_review_live_validation": "NOT_RUN",
                }
            )
        )
        return 0
    except FactoryError as error:
        print(json.dumps({"status": "BLOCKED", "error_code": error.code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
