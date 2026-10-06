"""Command-line entry point.

Exit codes: 0 ok, 1 check failure (invalid suite data, grade mismatch), 2 usage or setup error.
"""

import argparse
import contextlib
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from wiretruth import __version__
from wiretruth.formats import FormatError, load_fixture
from wiretruth.lint import SetupError, lint
from wiretruth.replay import HOST, ReplayServer

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
        elif name == "serve":
            subparser.add_argument(
                "fixture",
                help="a fixture file, or a fixture id such as openai-chat/truncation.text "
                "(read from suite/fixtures/ in the current directory)",
            )
            subparser.add_argument(
                "--port", type=int, default=0, help=f"port on {HOST} (default: a free one)"
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


def run_serve(target: str, port: int) -> int:
    path = Path(target)
    if not path.is_file():
        path = Path("suite", "fixtures", f"{target}.json")
    if not path.is_file():
        print(f"wiretruth serve: {target}: no such file, and no fixture {path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        fixture = load_fixture(path)
    except FormatError as error:
        print(error, file=sys.stderr)
        return EXIT_CHECK
    server = ReplayServer(fixture, port=port)
    # Python leaves SIGINT ignored when it starts with it ignored (a background job, some wrappers).
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        # A second Ctrl-C while the server drains still ends in the summary below.
        with contextlib.suppress(KeyboardInterrupt), contextlib.ExitStack() as stack:
            try:
                stack.enter_context(server)
            except (OSError, OverflowError) as error:
                print(f"wiretruth serve: can't listen on {HOST}:{port}: {error}", file=sys.stderr)
                return EXIT_USAGE
            print(f"wiretruth serve: {fixture.id} on {server.url}")
            print(f"{fixture.provider} base URL: {server.base_urls()[fixture.provider]}")
            print("Press Ctrl-C to stop.", flush=True)
            while True:
                time.sleep(60)  # interruptible on every platform, unlike a bare lock wait
    finally:
        signal.signal(signal.SIGINT, signal.SIG_DFL if previous is None else previous)
    log = server.request_log
    print(f"wiretruth serve: stopped after {_plural(len(log), 'request')}")
    count = len(fixture.exchanges)
    for request in log:
        if request.exchange is None:
            answer = "no matching exchange (404)"
        else:
            sent = "sent in full" if request.response_complete else "not sent in full"
            answer = f"exchange {request.exchange + 1} of {count}, {sent}"
        print(f"  {request.method} {request.path}: {answer}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "lint":
        return run_lint(Path(args.path))
    if args.command == "serve":
        return run_serve(args.fixture, args.port)
    print(f"wiretruth {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_USAGE
