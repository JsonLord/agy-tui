import argparse
import asyncio
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Antigravity hybrid coding harness"
    )

    parser.add_argument(
        "repo",
        help="Repository/workspace to modify",
    )

    parser.add_argument(
        "task",
        help="Coding task",
    )

    parser.add_argument(
        "--test-command",
        default=None,
        help="Independent validation command",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# P11 — checkpoint viewer subcommand (read-only canonical state, plain text)
# ---------------------------------------------------------------------------


def build_checkpoint_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m app.main checkpoint",
        description="Inspect canonical AGY checkpoint-planning state "
                    "(read-only; never touches the OpenCode Todo "
                    "projection)",
    )
    sub = parser.add_subparsers(dest="checkpoint_command", required=True)

    view = sub.add_parser(
        "view",
        help="render plan/gate status for one plan from canonical state",
    )
    view.add_argument("--plan-id", required=True, help="plan id to view")
    view.add_argument(
        "--state-root",
        default="state",
        help="canonical state root (default: ./state — the directory "
             "holding plans/<plan_id>/ and runs/<plan_id>/)",
    )
    return parser


def checkpoint_main(argv: list[str]) -> int:
    """Entry for `... checkpoint ...`; returns the process exit code.

    Fail closed: missing/malformed canonical state prints a clear error
    and exits non-zero (CheckpointViewerError is the viewer's contract).
    """
    from app.orchestration.checkpoint_planning.checkpoint_viewer import (
        CheckpointViewerError,
        view_checkpoint,
    )

    args = build_checkpoint_parser().parse_args(argv)
    if args.checkpoint_command == "view":
        try:
            rendered = view_checkpoint(args.state_root, args.plan_id)
        except CheckpointViewerError as exc:
            print(f"checkpoint view failed: {exc}", file=sys.stderr)
            return 1
        print(rendered, end="")
        return 0
    return 2  # pragma: no cover - argparse required=True guards this


async def async_main():
    # P11: lazy so `python3 -m app.main checkpoint …` runs in environments
    # without the google.antigravity host runtime (the viewer is pure
    # stdlib + domain schemas). The run-task path is unchanged.
    from app.config import Settings
    from app.orchestration.loop import run_task

    args = parse_args()

    repo = Path(args.repo).expanduser().resolve()

    if not repo.is_dir():
        raise SystemExit(
            f"Repository does not exist: {repo}"
        )

    settings = Settings.from_env()

    success = await run_task(
        settings=settings,
        repo=repo,
        task=args.task,
        test_command=args.test_command,
    )

    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "checkpoint":
        # P11 viewer path — synchronous, read-only, no run loop.
        raise SystemExit(checkpoint_main(sys.argv[2:]))
    asyncio.run(async_main())
