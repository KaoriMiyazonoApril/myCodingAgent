"""Command-line interface for the independently implemented Coding Agent."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import sys

from agent.host.server import DEFAULT_PORT, ProductionAssetsError, run_web
from agent.host.workspace import WorkspaceBrowseError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("web", help="start the local Agent Web UI")
    web.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=f"loopback port (default: {DEFAULT_PORT})",
    )
    web.add_argument(
        "--workspace-root",
        dest="workspace_roots",
        action="append",
        default=[],
        metavar="PATH",
        help="allowed Host workspace root; repeat for multiple roots",
    )
    web.add_argument(
        "--dev",
        action="store_true",
        help="run the API for the separate Vite development server",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "web":
            return run_web(
                port=args.port,
                workspace_roots=args.workspace_roots,
                dev=args.dev,
            )
    except (ProductionAssetsError, WorkspaceBrowseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 2


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port
