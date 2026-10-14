#!/usr/bin/env python3
"""Read-only live doctor for the local Automaker API; never mutates state.

Uses only the READ endpoints behind automaker-local. Exit code 0 only when
every read check succeeds. Prints bounded redacted results, never the key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automaker_local.client import AutomakerClient
from app.automaker_local.policy import AutomakerError, environment
from app.automaker_local.schemas import (
    autonomy_summary,
    backlog_summary,
    health_summary,
    job_summaries,
    project_summaries,
    running_agent_summaries,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-path",
        default="",
        help="Optional project path for project-scoped read checks.",
    )
    args = parser.parse_args()

    try:
        base_url, api_key = environment()
    except AutomakerError as error:
        print(json.dumps({"status": "BLOCKED", "error_code": error.code}))
        return 2

    client = AutomakerClient(base_url, api_key)
    checks = {
        "automaker_health": lambda: health_summary(client.health()),
        "automaker_projects": lambda: project_summaries(client.projects_overview()),
        "automaker_running_agents": lambda: running_agent_summaries(
            client.running_agents()
        ),
        "automaker_autonomy_status": lambda: autonomy_summary(
            client.auto_mode_status(args.project_path)
        ),
        "automaker_backlog_status": lambda: backlog_summary(
            client.backlog_status(args.project_path)
        ),
    }
    if args.project_path:
        checks["automaker_jobs"] = lambda: job_summaries(
            client.features_list(args.project_path)
        )

    report: dict = {"base_url": base_url, "checks": {}, "status": "OK"}
    failed = False
    for name, call in checks.items():
        try:
            report["checks"][name] = {"status": "OK", "result": call()}
        except AutomakerError as error:
            failed = True
            report["checks"][name] = {"status": "BLOCKED", "error_code": error.code}
        except Exception:  # noqa: BLE001 -- redact at the operator boundary
            failed = True
            report["checks"][name] = {
                "status": "BLOCKED",
                "error_code": "INTERNAL_ERROR",
            }
    report["status"] = "BLOCKED" if failed else "OK"
    print(json.dumps(report, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
