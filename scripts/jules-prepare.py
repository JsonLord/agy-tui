#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(
        0,
        str(ROOT),
    )


from app.integrations.jules_api import (
    JulesClient,
)
from app.orchestration.prepare_jules import (
    prepare_for_jules,
)


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
    )

    parser.add_argument(
        "--artifacts",
        default=None,
    )

    args = parser.parse_args()

    client = JulesClient()

    result = prepare_for_jules(
        args.repo,
        client=client,
        output_dir=args.artifacts,
        timeout=args.timeout,
    )

    print(
        json.dumps(
            result,
            indent=2,
        )
    )

    status = result.get(
        "status"
    )

    if status == "READY_FOR_JULES":
        return 0

    if status == "AWAITING_JULES_SOURCE":
        return 11

    if status == "AWAITING_GITHUB_AUTH":
        return 12

    return 10


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
