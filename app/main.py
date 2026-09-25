import argparse
import asyncio
from pathlib import Path

from app.config import Settings
from app.orchestration.loop import run_task


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


async def async_main():
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
    asyncio.run(async_main())
