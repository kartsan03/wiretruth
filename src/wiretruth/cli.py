"""Command-line entry point.

Exit codes: 0 ok, 1 check failure (invalid suite data, grade mismatch), 2 usage or setup error.
"""

import argparse
import contextlib
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from wiretruth import __version__
from wiretruth.formats import FormatError, load_fixture
from wiretruth.lint import SetupError, lint
from wiretruth.providers import PROVIDERS
from wiretruth.recorder import (
    RecordError,
    api_key,
    describe_request,
    destination,
    find_scenarios,
    record_scenario,
    summary,
)
from wiretruth.replay import HOST, ReplayServer
from wiretruth.runner import DEFAULT_JOBS, GLYPHS, compare_grades, load_expected_grades, run_suite

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
        elif name == "run":
            subparser.add_argument(
                "--shim", required=True, type=Path, help="a shim directory with wiretruth-shim.json"
            )
            subparser.add_argument(
                "--scenario",
                default="*",
                help="a scenario id, or a glob such as 'truncation.*' (default: every scenario; "
                "read from suite/ in the current directory)",
            )
            subparser.add_argument(
                "--provider", choices=sorted(PROVIDERS), help="only this wire API (default: all)"
            )
            subparser.add_argument(
                "--jobs",
                type=int,
                default=DEFAULT_JOBS,
                help=f"cells to run at once (default: {DEFAULT_JOBS})",
            )
            subparser.add_argument(
                "--check-grades",
                type=Path,
                metavar="FILE",
                help="exit 1 if a grade or requirement status differs from this file",
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
        elif name == "record":
            subparser.add_argument(
                "--provider", required=True, choices=sorted(PROVIDERS), help="the wire API"
            )
            subparser.add_argument(
                "--scenario",
                required=True,
                help="a scenario id, or a glob such as 'truncation.*' "
                "(read from suite/scenarios/ in the current directory)",
            )
            subparser.add_argument(
                "--model", help="the model to request (default: the provider's current one)"
            )
            subparser.add_argument(
                "--dry-run",
                action="store_true",
                help="print each request, with the key masked, and send nothing",
            )
            subparser.add_argument(
                "--replace",
                action="store_true",
                help="overwrite an existing fixture instead of keeping it as a dated variant",
            )
            subparser.add_argument(
                "--keep-bytes",
                action="store_true",
                help="store the body as the byte chunks read, instead of events or JSON",
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


def run_record(args: argparse.Namespace) -> int:
    provider = PROVIDERS[args.provider]
    model = args.model or provider.DEFAULT_MODEL
    root = Path()
    # Everything that can fail before a request is checked first, for every scenario, so a
    # mistake in the last one doesn't come after paying for the others.
    try:
        scenarios, skipped = find_scenarios(root, args.scenario, provider.ID)
        for scenario_id in skipped:
            print(f"wiretruth record: skipped {scenario_id}: it doesn't list {provider.ID}")
        requests = [provider.build_request(s.call_for(provider.ID), model) for s in scenarios]
        if args.dry_run:
            for scenario, request in zip(scenarios, requests, strict=True):
                print(f"# {scenario.id}")
                print(describe_request(provider, request, provider.API_BASE))
            return 0
        key = api_key(provider, os.environ)
        for scenario in scenarios:
            destination(root, provider.ID, scenario.id, replace=args.replace)
    except FormatError as error:
        print(error, file=sys.stderr)
        return EXIT_CHECK
    except (RecordError, ValueError) as error:
        print(f"wiretruth record: {error}", file=sys.stderr)
        return EXIT_USAGE
    # A scenario that fails doesn't stop the others: each recording that works is kept.
    code = 0
    for scenario in scenarios:
        try:
            recording = record_scenario(
                provider,
                scenario,
                model=model,
                key=key,
                root=root,
                base_url=provider.API_BASE,
                env=os.environ,
                replace=args.replace,
                keep_bytes=args.keep_bytes,
            )
        except FormatError as error:
            print(error, file=sys.stderr, flush=True)
            code = EXIT_CHECK
            continue
        except (RecordError, ValueError, OSError) as error:
            print(f"wiretruth record: {scenario.id}: {error}", file=sys.stderr, flush=True)
            code = code or EXIT_USAGE
            continue
        print(f"wrote {summary(recording)}", flush=True)
        for warning in recording.warnings:
            print(f"  warning: {warning}", flush=True)
    return code


def run_shim(args: argparse.Namespace) -> int:
    try:
        expected = None if args.check_grades is None else load_expected_grades(args.check_grades)
        report = run_suite(
            Path(), args.shim, pattern=args.scenario, provider=args.provider, jobs=args.jobs
        )
    except (ValueError, RecordError, OSError) as error:
        print(f"wiretruth run: {error}", file=sys.stderr)
        return EXIT_USAGE
    for cell in report.cells:
        grade = cell.grade.grade
        print(f"{GLYPHS[grade]} {grade} {cell.scenario} {cell.provider}")
        for note in cell.notes:
            print(f"  {note}")
        if cell.observation is not None:  # e.g. a setting the library couldn't send (ADR 0006)
            for note in cell.observation.notes:
                print(f"  shim: {note}")
    print(f"stderr: {report.stderr_dir}")
    if expected is not None:
        differences = compare_grades(report.cells, expected)
        for difference in differences:
            print(difference)
        if differences:
            return EXIT_CHECK
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
    if args.command == "record":
        return run_record(args)
    if args.command == "run":
        return run_shim(args)
    print(f"wiretruth {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_USAGE
