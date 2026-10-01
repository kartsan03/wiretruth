"""Command-line entry point.

Exit codes: 0 ok, 1 check or grade mismatch, 2 usage or setup error.
"""

import argparse
import sys
from collections.abc import Sequence

from wiretruth import __version__

EXIT_USAGE = 2

COMMANDS = {
    "run": "run shims against replayed fixtures and grade the results",
    "serve": "serve one fixture on 127.0.0.1 for manual reproduction",
    "record": "record provider responses into fixtures (needs API keys)",
    "derive": "derive a fixture from another one with a fault-injection transform",
    "lint": "validate suite data and results",
    "catalog": "generate the scenario catalog",
    "site": "build the static results site",
    "envs": "list or prune per-library environments",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiretruth",
        description="Replay real provider responses through LLM SDKs and grade what they report.",
    )
    parser.add_argument("--version", action="version", version=f"wiretruth {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, help_text in COMMANDS.items():
        subparsers.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    print(f"wiretruth {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_USAGE
