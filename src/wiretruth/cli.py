"""Command-line entry point.

Exit codes: 0 ok, 1 check failure (invalid suite data, grade mismatch), 2 usage or setup error.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from wiretruth import __version__
from wiretruth.lint import SetupError, lint

EXIT_CHECK = 1
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
        subparser = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "lint":
            subparser.add_argument(
                "path",
                nargs="?",
                default=".",
                help="repository root that holds suite/ and results/ (default: current directory)",
            )
    return parser


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def run_lint(root: Path) -> int:
    try:
        report = lint(root)
    except SetupError as error:
        print(f"wiretruth lint: {error}", file=sys.stderr)
        return EXIT_USAGE
    for note in report.notes:
        print(f"wiretruth lint: {note}", file=sys.stderr)
    for problem in report.problems:
        print(problem)
    if report.problems:
        failing = len({problem.file for problem in report.problems})
        print(
            f"wiretruth lint: {_plural(len(report.problems), 'error')} in "
            f"{failing} of {_plural(report.files, 'file')}"
        )
        return EXIT_CHECK
    print(f"wiretruth lint: {_plural(report.files, 'file')}, no errors")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "lint":
        return run_lint(Path(args.path))
    print(f"wiretruth {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_USAGE
