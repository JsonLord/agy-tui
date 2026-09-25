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


from app.orchestration.prepare_jules import (
    build_preparation_brief,
    inspect_jules_readiness,
)


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
    )

    parser.add_argument(
        "--brief",
        action="store_true",
    )

    args = parser.parse_args()

    result = inspect_jules_readiness(
        args.repo
    )

    print(
        json.dumps(
            result.to_dict(),
            indent=2,
        )
    )

    if args.brief:
        print()
        print("=== GEMMA PREPARATION BRIEF ===")
        print()
        print(
            build_preparation_brief(
                result
            )
        )

    return (
        0
        if result.ready
        else 10
    )


if __name__ == "__main__":
    raise SystemExit(main())
