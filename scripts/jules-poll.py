#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.jules_api import (
    JulesClient,
)
from app.orchestration.jules_poll import (
    poll_once,
)


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "run_id",
        help=(
            "Local Jules run ID or an "
            "explicit run directory."
        ),
    )

    args = parser.parse_args()

    supplied = Path(
        args.run_id
    ).expanduser()

    if supplied.is_dir():
        run_dir = supplied
    else:
        run_dir = (
            ROOT
            / "state"
            / "jules-runs"
            / args.run_id
        )

    result = poll_once(
        run_dir,
        client=JulesClient(),
    )

    print(
        json.dumps(
            result.to_dict(),
            indent=2,
        )
    )

    # Polling itself succeeded regardless of the
    # remote Jules workflow state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
