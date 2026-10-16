"""Results files and a compact terminal summary of a graded run."""

import json
import os
import subprocess
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple, cast

from wiretruth import __version__
from wiretruth.checks import Status
from wiretruth.envs import (
    RunError,
    find_library,
    installed_javascript_packages,
    installed_packages,
    resolve_interpreter,
)
from wiretruth.formats import (
    FormatError,
    JsonObject,
    Problem,
    ShimManifest,
    load_document,
    observation_document,
    validate,
)
from wiretruth.grading import Grade
from wiretruth.runner import GLYPHS, ExpectedGrade, RunReport


def results_document(
    report: RunReport,
    manifest: ShimManifest,
    *,
    at: datetime,
    git_sha: str,
    ci_run: str | None,
    packages: Mapping[str, str],
) -> JsonObject:
    """Build a results/v1 document from the run and its captured metadata."""
    return {
        "format": "wiretruth.results/v1",
        "suite_version": __version__,
        "library": {
            "name": manifest.name,
            "version": manifest.packages[manifest.primary_package],
            "language": manifest.language,
        },
        "run": {
            "at": at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_sha": git_sha,
            "ci_run": ci_run,
            "runner": f"wiretruth {__version__}",
            "packages": dict(packages),
        },
        "cells": [
            {
                "scenario": cell.scenario,
                "provider": cell.provider,
                "fixture": cell.fixture,
                "fixture_tier": cell.fixture_tier,
                "grade": cell.grade.grade,
                "requirements": [
                    {
                        "id": result.id,
                        "level": result.level,
                        "status": result.status,
                        "evidence": result.evidence,
                    }
                    for result in cell.grade.requirements
                ],
                "notes": list(cell.notes),
                "observation": None
                if cell.observation is None
                else observation_document(cell.observation),
                "request_count": cell.request_count,
            }
            for cell in report.cells
        ],
    }


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise RunError("--write-results needs a git checkout") from error
    if result.returncode:
        raise RunError("--write-results needs a git checkout")
    return result.stdout


def collect_results(root: Path, report: RunReport, manifest: ShimManifest) -> JsonObject:
    """Gather the checkout, CI and environment metadata for a results file."""
    git_sha = _git(root, "rev-parse", "HEAD").strip()
    status = _git(root, "status", "--porcelain", "--", ".", ":(exclude)results")
    if status:
        raise RunError(
            "--write-results needs a clean checkout outside results/; commit or stash: "
            f"{status.splitlines()[0][3:]}"
        )
    server, repository, run_id = (
        os.environ.get(name) for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    ci_run = (
        f"{server}/{repository}/actions/runs/{run_id}"
        if all((server, repository, run_id))
        else None
    )
    if manifest.language == "javascript":
        packages = installed_javascript_packages(find_library(root, manifest.name))
    else:
        interpreter = resolve_interpreter(root, manifest)
        if interpreter is None:
            raise RunError("--write-results needs a Python shim environment")
        packages = installed_packages(interpreter)
    return results_document(
        report,
        manifest,
        at=datetime.now(UTC),
        git_sha=git_sha,
        ci_run=ci_run,
        packages=packages,
    )


def results_path(root: Path, name: str, version: str) -> Path:
    return root / "results" / name / f"{version}.json"


def write_results(root: Path, document: JsonObject) -> Path:
    problems = validate("results", document, "<results>")
    if problems:
        raise FormatError(problems)
    library = cast(JsonObject, document["library"])
    path = results_path(root, cast(str, library["name"]), cast(str, library["version"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


class SavedResults(NamedTuple):
    name: str
    version: str
    packages: Mapping[str, str]
    grades: dict[tuple[str, str], ExpectedGrade]


def load_results_grades(path: Path) -> SavedResults:
    """Read library identity and expected grades from a validated results file."""
    document = cast(JsonObject, load_document("results", path))
    library = cast(JsonObject, document["library"])
    run = cast(JsonObject, document["run"])
    expected = {}
    for index, cell in enumerate(cast(list[JsonObject], document["cells"])):
        scenario, provider = cast(str, cell["scenario"]), cast(str, cell["provider"])
        key = (scenario, provider)
        if key in expected:
            raise FormatError(
                [Problem(str(path), f"$.cells[{index}]", f"duplicate cell {scenario} {provider}")]
            )
        requirements = cast(list[JsonObject], cell["requirements"])
        expected[key] = ExpectedGrade(
            cast(Grade, cell["grade"]),
            {cast(str, item["id"]): cast(Status, item["status"]) for item in requirements},
        )
    return SavedResults(
        cast(str, library["name"]),
        cast(str, library["version"]),
        cast(dict[str, str], run["packages"]),
        expected,
    )


def package_changes(saved: Mapping[str, str], current: Mapping[str, str]) -> tuple[str, ...]:
    changes = []
    for name in sorted(saved.keys() | current.keys()):
        if name not in saved:
            changes.append(f"+{name} {current[name]}")
        elif name not in current:
            changes.append(f"-{name} {saved[name]}")
        elif saved[name] != current[name]:
            changes.append(f"{name} {saved[name]} → {current[name]}")
    return tuple(changes)


def summary_lines(report: RunReport, manifest: ShimManifest) -> tuple[str, ...]:
    providers = sorted({cell.provider for cell in report.cells})
    scenarios = sorted({cell.scenario for cell in report.cells})
    grades = {
        (cell.scenario, cell.provider): f"{GLYPHS[cell.grade.grade]} {cell.grade.grade}"
        for cell in report.cells
    }
    rows = [["scenario", *providers]] + [
        [scenario, *(grades.get((scenario, provider), "") for provider in providers)]
        for scenario in scenarios
    ]
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    lines = [
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
    for cell in report.cells:
        notes = list(cell.notes)
        if cell.observation is not None:
            notes.extend(f"shim: {note}" for note in cell.observation.notes)
        if notes:
            lines.extend(["", f"{cell.scenario} {cell.provider}", *(f"  {note}" for note in notes)])
    count = len(report.cells)
    counts = Counter(cell.grade.grade for cell in report.cells)
    totals = ", ".join(f"{counts[grade]} {grade}" for grade in GLYPHS if counts[grade])
    version = manifest.packages[manifest.primary_package]
    lines.extend(
        [
            "",
            f"{manifest.name} {version}: {count} cell{'' if count == 1 else 's'}"
            + (f", {totals}" if totals else ""),
            f"stderr: {report.stderr_dir}",
        ]
    )
    return tuple(lines)
