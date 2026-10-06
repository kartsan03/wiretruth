"""``wiretruth lint``: validate suite/ and results/, file by file and against each other.

Each file's kind comes from where it lives:

    suite/bug-classes.json                       bug-classes
    suite/scenarios/<family>/<name>.json         scenario
    suite/fixtures/<provider>/<id>.json          fixture
    results/upstream.json                        upstream
    results/<library>/<version>.json             results
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TypeVar, cast

from wiretruth.formats import (
    Fixture,
    FormatError,
    JsonObject,
    Kind,
    Problem,
    Scenario,
    load_bug_classes,
    load_document,
    load_fixture,
    load_scenario,
)

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


def lint(root: Path) -> Report:
    suite = root / "suite"
    if not suite.is_dir():
        raise SetupError(f"{suite}: no such directory")
    report = Report()
    paths = sorted(path for path in suite.rglob("*.json") if path.is_file())
    results = root / "results"
    if results.is_dir():
        paths += sorted(path for path in results.rglob("*.json") if path.is_file())
    else:
        report.notes.append(f"{results}: no such directory, skipped")

    registry = suite / "bug-classes.json"
    bug_classes: set[str] | None = None
    if not registry.is_file():
        report.files += 1  # checked, and found missing
        report.problems.append(Problem(str(registry), "$", "the bug-class registry is missing"))
    # Keyed by the id the file path implies; None when the file itself is invalid.
    scenarios: dict[str, tuple[Path, Scenario | None]] = {}
    fixtures: dict[str, tuple[Path, Fixture | None]] = {}
    results_files: list[tuple[Path, JsonObject]] = []

    for path in paths:
        report.files += 1
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
            fixtures[f"{parts[2]}/{path.stem}"] = (path, _load(load_fixture, path, report))
        else:
            doc = _load(partial(load_document, kind), path, report)
            if kind == "results" and doc is not None:
                results_files.append((path, cast(JsonObject, doc)))

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

    for expected, (path, fixture) in fixtures.items():
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

    for path, doc in results_files:
        library = cast(JsonObject, doc["library"])
        for key, expected in (("name", path.parent.name), ("version", path.stem)):
            if library[key] != expected:
                message = f"{library[key]!r} doesn't match the file path: expected {expected!r}"
                problem(path, f"$.library.{key}", message)

    report.problems.sort(key=lambda found: found.file)
    return report
