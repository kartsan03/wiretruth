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
from wiretruth.envs import (
    RunError,
    find_library,
    installed_packages,
    list_envs,
    prune_envs,
    resolve_interpreter,
)
from wiretruth.formats import FormatError, load_fixture, load_shim_manifest
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
from wiretruth.results import (
    collect_results,
    load_results_grades,
    package_changes,
    results_path,
    summary_lines,
    write_results,
)
from wiretruth.runner import DEFAULT_JOBS, compare_grades, load_expected_grades, run_suite

EXIT_CHECK = 1
EXIT_USAGE = 2

COMMANDS = {
    "run": "run shims against replayed fixtures and grade the results",
    "serve": "serve one fixture on 127.0.0.1 for manual reproduction",
    "record": "record provider responses into fixtures (needs API keys)",
    "lint": "validate suite data and results",
    "envs": "list, build or prune per-library environments",
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
            shim = subparser.add_mutually_exclusive_group(required=True)
            shim.add_argument("--shim", type=Path, help="a shim directory with wiretruth-shim.json")
            shim.add_argument("--lib", metavar="NAME", help="a library shim under shims/*/NAME/")
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
            results = subparser.add_mutually_exclusive_group()
            results.add_argument(
                "--write-results",
                action="store_true",
                help="write results/NAME/VERSION.json for a library shim (runs every cell)",
            )
            results.add_argument(
                "--check-against",
                type=Path,
                nargs="?",
                const=True,
                metavar="FILE",
                help="exit 1 if a grade or requirement status differs from this results file "
                "(default: results/NAME/VERSION.json; runs every cell)",
            )
            results.add_argument(
                "--check-grades",
                type=Path,
                metavar="FILE",
                help="exit 1 if a grade or requirement status differs from this file "
                "(runs every cell)",
            )
        elif name == "envs":
            action = subparser.add_mutually_exclusive_group()
            action.add_argument("--list", action="store_true", help="list environments (default)")
            action.add_argument("--prune", action="store_true", help="remove stale environments")
            action.add_argument(
                "--build", metavar="NAME", help="build or reuse a library environment"
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
    used = {
        "--write-results": args.write_results,
        "--check-against": args.check_against is not None,
        "--check-grades": args.check_grades is not None,
    }
    flag = next((name for name, on in used.items() if on), None)
    if flag is not None and (args.scenario != "*" or args.provider is not None):
        # The file lists every cell, so a filtered run would report the rest as missing.
        print(
            f"wiretruth run: {flag} runs every cell; drop --scenario and --provider",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.write_results and args.lib is None:
        print(
            "wiretruth run: --write-results needs --lib; results files are for library shims",
            file=sys.stderr,
        )
        return EXIT_USAGE
    saved = None
    try:
        expected = None if args.check_grades is None else load_expected_grades(args.check_grades)
        shim_dir = find_library(Path(), args.lib) if args.lib is not None else args.shim
        manifest = load_shim_manifest(shim_dir / "wiretruth-shim.json")
        version = manifest.packages[manifest.primary_package]
        if args.check_against is not None:
            path = (
                results_path(Path(), manifest.name, version)
                if args.check_against is True
                else args.check_against
            )
            if not path.is_file():
                raise RunError(f"{path}: no such results file; write it with --write-results")
            try:
                saved = load_results_grades(path)
            except FormatError as error:
                print(error, file=sys.stderr)
                return EXIT_CHECK
            if (saved.name, saved.version) != (manifest.name, version):
                raise RunError(
                    f"{path}: results are for {saved.name} {saved.version}; "
                    f"expected {manifest.name} {version}"
                )
            expected = saved.grades
        report = run_suite(
            Path(),
            shim_dir,
            pattern=args.scenario,
            provider=args.provider,
            jobs=args.jobs,
        )
    except (ValueError, RecordError, OSError) as error:
        print(f"wiretruth run: {error}", file=sys.stderr)
        return EXIT_USAGE
    for line in summary_lines(report, manifest):
        print(line)
    if args.write_results:
        harness = sum(cell.grade.grade == "HARNESS" for cell in report.cells)
        if harness:
            print(
                f"wiretruth run: --write-results: {_plural(harness, 'HARNESS cell')}; "
                "fix the shim or the harness first",
                file=sys.stderr,
            )
            return EXIT_CHECK
        try:
            path = write_results(Path(), collect_results(Path(), report, manifest))
        except FormatError as error:
            print(error, file=sys.stderr)
            return EXIT_CHECK
        except (RunError, OSError) as error:
            print(f"wiretruth run: {error}", file=sys.stderr)
            return EXIT_USAGE
        print(f"wrote {path}")
    if saved is not None:
        try:
            interpreter = resolve_interpreter(Path(), manifest)
            if interpreter is not None:
                changes = package_changes(saved.packages, installed_packages(interpreter))
                if changes:
                    print(f"packages differ from {path}: {', '.join(changes)}")
        except (RunError, OSError) as error:
            print(f"wiretruth run: {error}", file=sys.stderr)
            return EXIT_USAGE
    if expected is not None:
        differences = compare_grades(report.cells, expected)
        for difference in differences:
            print(difference)
        if differences:
            return EXIT_CHECK
    return 0


def run_envs(args: argparse.Namespace) -> int:
    try:
        if args.build is not None:
            shim_dir = find_library(Path(), args.build)
            manifest = load_shim_manifest(shim_dir / "wiretruth-shim.json")
            interpreter = resolve_interpreter(Path(), manifest)
            if interpreter is None:
                raise RunError("only Python library environments can be built")
            print(interpreter)
        elif args.prune:
            for path in prune_envs(Path()):
                print(f"removed {path}")
        else:
            for path, current in list_envs(Path()):
                print(f"{'current' if current else 'stale'} {path}")
    except (ValueError, OSError) as error:
        print(f"wiretruth envs: {error}", file=sys.stderr)
        return EXIT_USAGE
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
    if args.command == "envs":
        return run_envs(args)
    print(f"wiretruth {args.command}: not implemented yet", file=sys.stderr)
    return EXIT_USAGE
