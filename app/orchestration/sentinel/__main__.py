"""python -m app.orchestration.sentinel — daemon or CLI depending on args.

    python3 -m app.orchestration.sentinel daemon [flags]     # the daemon
    python3 -m app.orchestration.sentinel spawn|snapshot|... # sentry-cli
"""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "daemon":
        from .daemon import main as daemon_main
        return daemon_main(argv[1:])
    from .cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())