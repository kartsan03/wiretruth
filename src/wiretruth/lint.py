"""``wiretruth lint``: validate suite/ and results/, file by file and against each other.

Each file's kind comes from where it lives:

    suite/bug-classes.json                       bug-classes
    suite/scenarios/<family>/<name>.json         scenario
    suite/fixtures/<provider>/<id>.json          fixture
    results/upstream.json                        upstream
    results/<library>/<version>.json             results

Every file also goes through the secret scan, and every fixture's truth is recomputed from its
exchanges by its provider module, when that provider has one yet. No problem lint reports repeats
what the scan flags: a parser, schema or extractor message can quote the file, so such a message is
withheld, and the scan's own finding points to the line.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

from wiretruth.checks import CHECKS
from wiretruth.formats import (
    Fixture,
    FormatError,
    JsonObject,
    JsonValue,
    Kind,
    Problem,
    Scenario,
    json_path,
    load_bug_classes,
    load_document,
    load_fixture,
    load_scenario,
    load_shim_manifest,
    parse_json,
    read_json,
)
from wiretruth.providers import PROVIDERS
from wiretruth.secret_scan import scan_headers, scan_text

T = TypeVar("T")


class SetupError(Exception):
    """Lint can't run, for example because suite/ is missing."""


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    files: int = 0
    notes: list[str] = field(default_factory=list)


def _kind(parts: tuple[str, ...]) -> Kind | None:
    match parts:
        case ("suite", "bug-classes.json"):
            return "bug-classes"
        case ("suite", "scenarios", _, _):
            return "scenario"
        case ("suite", "fixtures", _, _):
            return "fixture"
        case ("results", "upstream.json"):
            return "upstream"
        case ("results", _, _):
            return "results"
    return None


def _load(loader: Callable[[Path], T], path: Path, report: Report) -> T | None:
    try:
        return loader(path)
    except FormatError as error:
        report.problems.extend(error.problems)
        return None


_SECRET_ADVICE = "remove it, and revoke the credential if it is real"
_SHOWN = 80


def _scan_secrets(path: Path, report: Report) -> JsonValue:
    """Secret-scan one file: its text and, for JSON, its headers objects and decoded chunks_b64
    bodies. Returns the parsed JSON, or None."""
    try:
        data = path.read_bytes()
    except OSError as error:
        if path.suffix != ".json":  # the loader reports a JSON file
            message = f"cannot read the file: {error.strerror}"
            report.problems.append(Problem(str(path), "$", message))
        return None
    for finding in scan_text(data.decode("utf-8", "replace")):
        report.problems.append(
            Problem(str(path), finding.where, f"{finding.what}; {_SECRET_ADVICE}")
        )
    if path.suffix != ".json":
        return None
    try:
        doc = parse_json(data)
    except (ValueError, RecursionError):
        return None  # the loader reports it
    for finding in scan_headers(doc):
        report.problems.append(
            Problem(str(path), finding.where, f"{finding.what}; {_SECRET_ADVICE}")
        )
    exchanges = doc.get("exchanges") if isinstance(doc, dict) else None
    for i, exchange in enumerate(exchanges if isinstance(exchanges, list) else []):
        response = exchange.get("response") if isinstance(exchange, dict) else None
        body = response.get("body") if isinstance(response, dict) else None
        parts = body.get("chunks_b64") if isinstance(body, dict) else None
        if not isinstance(parts, list):
            continue
        try:
            text = b"".join(base64.b64decode(cast(str, part), validate=True) for part in parts)
        except (TypeError, ValueError):
            continue  # a chunk the schema rejects
        where = json_path(["exchanges", i, "response", "body", "chunks_b64"])
        decoded = text.decode("utf-8", "replace")
        for finding in [*scan_text(decoded), *scan_headers(decoded)]:
            message = f"{finding.what} (decoded, {finding.where}); {_SECRET_ADVICE}"
            report.problems.append(Problem(str(path), where, message))
    return doc


def _shown(value: JsonValue) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if scan_text(text):  # the scan reports it too; lint output never repeats a secret
        return "a value the secret scan flags (not shown)"
    return text if len(text) <= _SHOWN else text[: _SHOWN - 1] + "…"


def _quiet(problem: Problem) -> Problem:
    """``problem``, with its path or message withheld where it repeats what the scan flags."""
    if scan_text(problem.where):
        problem = replace(problem, where="a path the secret scan flags (not shown)")
    if scan_text(problem.message):
        problem = replace(
            problem, message="a message that quotes what the secret scan flags (not shown)"
        )
    return problem


def _canonical(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True)


def _truth_differences(
    stored: JsonValue, computed: JsonValue, where: list[str | int]
) -> list[tuple[str, str]]:
    """(JSON path, message), one per differing field, going into objects with the same keys."""
    if isinstance(stored, dict) and isinstance(computed, dict) and stored.keys() == computed.keys():
        return [
            found
            for key in computed
            for found in _truth_differences(stored[key], computed[key], [*where, key])
        ]
    if _canonical(stored) == _canonical(computed):
        return []
    return [(json_path(where), f"doesn't match the exchanges, which give {_shown(computed)}")]


def _check_truth(path: Path, fixture: Fixture, doc: JsonValue, report: Report) -> None:
    provider = PROVIDERS[fixture.provider]
    try:
        computed = provider.extract_truth(fixture.exchanges, fixture.provenance.request)
    except ValueError as error:
        message = f"the {fixture.provider} truth extractor can't read them: {error}"
        report.problems.append(Problem(str(path), "$.exchanges", message))
        return
    stored = cast(JsonObject, doc)["truth"]  # the fixture loaded, so the document is valid
    for where, message in _truth_differences(stored, computed, ["truth"]):
        report.problems.append(Problem(str(path), where, message))


def lint(root: Path) -> Report:
    suite = root / "suite"
    if not suite.is_dir():
        raise SetupError(f"{suite}: no such directory")
    report = Report()
    # Every file gets the secret scan; the JSON files are also checked as suite data.
    paths = sorted(path for path in suite.rglob("*") if path.is_file())
    results = root / "results"
    if results.is_dir():
        paths += sorted(path for path in results.rglob("*") if path.is_file())
    else:
        report.notes.append(f"{results}: no such directory, skipped")

    registry = suite / "bug-classes.json"
    bug_classes: set[str] | None = None
    if not registry.is_file():
        report.files += 1  # checked, and found missing
        report.problems.append(Problem(str(registry), "$", "the bug-class registry is missing"))
    # Keyed by the id the file path implies; None when the file itself is invalid.
    scenarios: dict[str, tuple[Path, Scenario | None]] = {}
    fixtures: dict[str, tuple[Path, Fixture | None, JsonValue]] = {}
    results_files: list[tuple[Path, JsonObject]] = []

    for path in paths:
        report.files += 1
        doc = _scan_secrets(path, report)
        if path.suffix != ".json":
            continue
        parts = path.relative_to(root).parts
        kind = _kind(parts)
        if kind is None:
            message = "unexpected JSON file: see docs/formats.md for where each format lives"
            report.problems.append(Problem(str(path), "$", message))
            continue
        if kind == "bug-classes":
            loaded = _load(load_bug_classes, path, report)
            bug_classes = None if loaded is None else {bug_class.id for bug_class in loaded}
        elif kind == "scenario":
            scenarios[f"{parts[2]}.{path.stem}"] = (path, _load(load_scenario, path, report))
        elif kind == "fixture":
            fixture = _load(load_fixture, path, report)
            fixtures[f"{parts[2]}/{path.stem}"] = (path, fixture, doc)
        else:
            loaded_doc = _load(partial(load_document, kind), path, report)
            if kind == "results" and loaded_doc is not None:
                results_files.append((path, cast(JsonObject, loaded_doc)))

    def problem(path: Path, where: str, message: str) -> None:
        report.problems.append(Problem(str(path), where, message))

    for expected, (path, scenario) in scenarios.items():
        if scenario is None:
            continue
        if scenario.id != expected:
            problem(
                path, "$.id", f"{scenario.id!r} doesn't match the file path: expected {expected!r}"
            )
        if bug_classes is not None and scenario.bug_class not in bug_classes:
            problem(path, "$.bug_class", f"{scenario.bug_class!r} is not a bug class in {registry}")
        for index, requirement in enumerate(scenario.requirements):
            if requirement.check not in CHECKS:
                problem(
                    path,
                    f"$.requirements[{index}].check",
                    f"requirement {requirement.id!r}: unknown check {requirement.check!r}",
                )

    unchecked: set[str] = set()
    for expected, (path, fixture, fixture_doc) in fixtures.items():
        if fixture is None:
            continue
        if fixture.id != expected:
            problem(
                path, "$.id", f"{fixture.id!r} doesn't match the file path: expected {expected!r}"
            )
        if fixture.scenario not in scenarios:
            problem(
                path, "$.scenario", f"no scenario {fixture.scenario!r} in {suite / 'scenarios'}"
            )
        else:
            scenario = scenarios[fixture.scenario][1]
            if scenario is not None and fixture.provider not in scenario.providers:
                message = f"{fixture.provider!r} is not in the providers of {fixture.scenario!r}"
                problem(path, "$.provider", message)
        if fixture.derived_from is not None and fixture.derived_from.fixture not in fixtures:
            message = f"no fixture {fixture.derived_from.fixture!r} in {suite / 'fixtures'}"
            problem(path, "$.derived_from.fixture", message)
        if fixture.provider in PROVIDERS:
            _check_truth(path, fixture, fixture_doc, report)
        else:
            unchecked.add(fixture.provider)
    if unchecked:
        names = ", ".join(sorted(unchecked))
        report.notes.append(f"truth not recomputed for {names}: no truth extractor yet")

    for path, doc in results_files:
        library = cast(JsonObject, doc["library"])
        for key, expected in (("name", path.parent.name), ("version", path.stem)):
            if library[key] != expected:
                message = f"{library[key]!r} doesn't match the file path: expected {expected!r}"
                problem(path, f"$.library.{key}", message)

    for path in sorted((root / "shims").glob("*/*/wiretruth-shim.json")):
        report.files += 1
        _scan_secrets(path, report)
        manifest = _load(load_shim_manifest, path, report)
        if manifest is not None and manifest.language == "javascript":
            package_path = path.with_name("package.json")
            report.files += 1
            _scan_secrets(package_path, report)
            package = _load(read_json, package_path, report)
            if not isinstance(package, dict) or package.get("dependencies") != manifest.packages:
                problem(path, "$.packages", "must equal package.json dependencies exactly")

    report.problems = sorted(map(_quiet, report.problems), key=lambda found: found.file)
    return report
