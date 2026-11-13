"""Weekly release checks and an artifact for the separate PR-opening job."""

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote
from urllib.request import urlopen

from wiretruth import envs, formats, results
from wiretruth.formats import JsonObject, ShimManifest
from wiretruth.runner import GLYPHS

# google-genai 3.x sends Gemini requests replay cannot match, so every Gemini cell is HARNESS.
HOLDS = {"google-genai": "<3"}


@dataclass(frozen=True)
class PinCheck:
    status: Literal["matches", "differs", "error"]
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Update:
    before: results.SavedResults
    after: results.SavedResults
    pins: tuple[str, ...]
    changes: tuple[str, ...]
    red: tuple[str, ...]


def replace_pins(text: str, old: Mapping[str, str], new: Mapping[str, str]) -> str:
    for name, version in old.items():
        pin = f'"{name}": "{version}"'
        if text.count(pin) != 1:
            raise ValueError(f"expected exactly one occurrence of {pin}")
        text = text.replace(pin, f'"{name}": "{new[name]}"')
    return text


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def select_extras(requirements: Sequence[str], companions: Sequence[str]) -> tuple[str, ...]:
    selected = []
    for companion in companions:
        candidates: list[str] = []
        for requirement in requirements:
            name = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if name is None or normalize(name[0]) != normalize(companion):
                continue
            extras = re.findall(r"\bextra\s*==\s*['\"]([^'\"]+)['\"]", requirement)
            if not extras:
                candidates = []
                break
            candidates.extend(extras)
        if candidates:
            extra = companion if companion in candidates else candidates[0]
            if extra not in selected:
                selected.append(extra)
    return tuple(selected)


def python_requirements(
    manifest: ShimManifest, version: str, requires_dist: Sequence[str]
) -> tuple[str, ...]:
    primary = manifest.primary_package
    companions = sorted(name for name in manifest.packages if name != primary)
    extras = select_extras(requires_dist, companions)
    requirement = primary + (f"[{','.join(extras)}]" if extras else "") + f"=={version}"
    return (
        requirement + (f",{HOLDS[primary]}" if primary in HOLDS else ""),
        *(name + HOLDS.get(name, "") for name in companions),
    )


def parse_compile_output(output: str, packages: Mapping[str, str]) -> dict[str, str]:
    resolved = envs.parse_installed_packages(output)
    return {name: resolved[normalize(name)] for name in packages}


def matching_upstream(
    upstream: Sequence[JsonObject], library: str, cell: tuple[str, str]
) -> list[JsonObject]:
    scenario, provider = cell
    return [
        entry
        for entry in upstream
        if entry["library"] == library
        and scenario in cast(list[str], entry["scenarios"])
        and provider in cast(list[str], entry["providers"])
    ]


def grade_changes(
    before: results.SavedResults, after: results.SavedResults, upstream: Sequence[JsonObject]
) -> tuple[str, ...]:
    changes = []
    for cell in sorted(before.grades.keys() | after.grades.keys()):
        old, new = before.grades.get(cell), after.grades.get(cell)
        if old is not None and new is not None and old.grade == new.grade:
            continue
        scenario, provider = cell
        line = (
            f"`{scenario}` over `{provider}` "
            f"{old.grade if old else 'absent'} → {new.grade if new else 'absent'}"
        )
        issue = next(
            (
                entry["issue"]
                for entry in matching_upstream(upstream, after.name, cell)
                if entry["issue"]
            ),
            None,
        )
        if issue:
            line += f" ([upstream]({issue}))"
        changes.append(line)
    return tuple(changes)


def new_red_cells(after: results.SavedResults, upstream: Sequence[JsonObject]) -> tuple[str, ...]:
    return tuple(
        f"`{scenario}` over `{provider}`: {grade.grade}"
        for (scenario, provider), grade in sorted(after.grades.items())
        if grade.grade in {"WRONG", "CRASH", "HANG"}
        and not matching_upstream(upstream, after.name, (scenario, provider))
    )


def changelog_bullet(update: Update) -> str:
    saved = update.after
    counts = Counter(cell.grade for cell in saved.grades.values())
    parts = [f"{counts[grade]} {grade}" for grade in GLYPHS if counts[grade]]
    totals = ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)
    path = f"results/{saved.name}/{saved.version}.json"
    changes = (
        f"Grade changes from {update.before.version}: {'; '.join(update.changes)}."
        if update.changes
        else f"No grade changes from {update.before.version}."
    )
    return textwrap.fill(
        f"- {saved.name} {saved.version}: {totals} of {len(saved.grades)} cells "
        f"([{path}]({path})). {changes}",
        width=116,
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def insert_changelog(text: str, bullets: Sequence[str]) -> str:
    heading = re.search(r"^## \[Unreleased\][^\n]*\n", text, re.MULTILINE)
    if heading is None:
        raise ValueError("CHANGELOG.md has no Unreleased section")
    following = re.search(r"^## ", text[heading.end() :], re.MULTILINE)
    end = heading.end() + following.start() if following else len(text)
    section = text[heading.end() : end]
    results_heading = re.search(r"^### Results[ \t]*\n", section, re.MULTILINE)
    added = "\n".join(bullets) + "\n"
    if results_heading:
        index = results_heading.end()
        section = section[:index] + added + section[index:]
    else:
        section = section.rstrip("\n") + "\n\n### Results\n" + added + "\n"
    return text[: heading.end()] + section + text[end:]


def pr_body(
    updates: Sequence[Update],
    skipped: Sequence[str],
    checks: Mapping[str, PinCheck],
    up_to_date: Sequence[str],
    run_url: str | None,
) -> str:
    summary = "Weekly library release results"
    if run_url:
        summary += f" ([Actions run]({run_url}))"
    lines = [summary + ".", "", "## Versions", ""]
    for update in updates:
        lines.append(
            f"- {update.after.name}: {update.before.version} → {update.after.version}; "
            + "; ".join(update.pins)
        )
    lines.extend(f"- {name}: up to date" for name in up_to_date)
    if not updates and not up_to_date:
        lines.append("none")
    lines.extend(["", "## Grade changes", ""])
    for update in updates:
        lines.append(f"- {update.after.name}: " + ("; ".join(update.changes) or "none"))
    if not updates:
        lines.append("none")
    lines.extend(["", "## New WRONG / CRASH / HANG cells without an upstream report", ""])
    red = [f"- {update.after.name}: {cell}" for update in updates for cell in update.red]
    lines.extend(red or ["none"])
    if red:
        lines.extend(
            [
                "",
                "The PR is labelled `blocked` until each cell has an entry in "
                "`results/upstream.json`.",
            ]
        )
    lines.extend(["", "## Skipped releases", "", *(f"- {item}" for item in skipped)])
    if not skipped:
        lines.append("none")
    lines.extend(["", "## Committed pins", ""])
    for name, check in checks.items():
        lines.append(f"- {name}: {check.status}")
        lines.extend(f"  - {line}" for line in check.details)
    if not checks:
        lines.append("none")
    lines.extend(
        [
            "",
            "## Not updated by this job",
            "",
            "Version numbers in manifest notes and version-anchored documentation links, "
            "`results/upstream.json` statuses, `docs/validation.md` and `docs/img/matrix.png`.",
            "",
        ]
    )
    return "\n".join(lines)


def command(root: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, input=input, capture_output=True, text=True, check=False)


def failure(result: subprocess.CompletedProcess[str]) -> str:
    return " / ".join((result.stderr.strip() or result.stdout.strip()).splitlines()[-8:])


def checked(root: Path, *args: str) -> str:
    result = command(root, *args)
    if result.returncode:
        raise envs.RunError(f"{' '.join(args)} exited {result.returncode}: {failure(result)}")
    return result.stdout.strip()


def git(root: Path, *args: str) -> str:
    return checked(root, "git", *args)


def commit(root: Path, message: str, *paths: str) -> None:
    git(root, "add", "--", *paths)
    git(
        root,
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898273+github-actions[bot]@users.noreply.github.com",
        "commit",
        "--quiet",
        "--message",
        message,
    )


def build_env(root: Path, name: str) -> None:
    checked(root, sys.executable, "-m", "wiretruth", "envs", "--build", name)


def offline_run(root: Path, name: str, flag: str) -> subprocess.CompletedProcess[str]:
    return command(
        root,
        "unshare",
        "-rn",
        "sh",
        "-c",
        'ip link set lo up && exec "$@"',
        "sh",
        sys.executable,
        "-m",
        "wiretruth",
        "run",
        "--lib",
        name,
        flag,
    )


def check_pins(root: Path, name: str) -> PinCheck:
    try:
        build_env(root, name)
        run = offline_run(root, name, "--check-against")
    except (OSError, ValueError) as error:
        return PinCheck("error", tuple(str(error).splitlines()[-8:]))
    if run.returncode not in (0, 1):
        return PinCheck("error", tuple((run.stderr or run.stdout).splitlines()[-8:]))
    details = tuple(
        line
        for line in run.stdout.splitlines()
        if line.startswith("packages differ from ")
        or re.match(r"^\S+ \S+: (grade |requirement |missing from |not listed )", line)
    )
    if run.returncode and not details:
        details = tuple((run.stderr or run.stdout).splitlines()[-8:])
    return PinCheck("matches" if run.returncode == 0 else "differs", details)


def release_metadata(name: str, language: str) -> JsonObject:
    url = (
        f"https://pypi.org/pypi/{quote(name, safe='')}/json"
        if language == "python"
        else f"https://registry.npmjs.org/{quote(name, safe='')}/latest"
    )
    with urlopen(url, timeout=30) as response:
        document = cast(JsonObject, json.load(response))
    return cast(JsonObject, document["info"]) if language == "python" else document


def resolve_pins(root: Path, manifest: ShimManifest, metadata: JsonObject) -> dict[str, str]:
    version = cast(str, metadata["version"])
    if manifest.language == "javascript":
        return {
            name: version
            if name == manifest.primary_package
            else cast(str, release_metadata(name, manifest.language)["version"])
            for name in manifest.packages
        }
    requirements = python_requirements(
        manifest, version, cast(list[str], metadata.get("requires_dist") or [])
    )
    resolved = command(
        root,
        "uv",
        "pip",
        "compile",
        "-",
        "--python-version",
        envs.PYTHON,
        "--no-header",
        "--no-annotate",
        "--quiet",
        input="\n".join(requirements) + "\n",
    )
    if resolved.returncode:
        lines = (resolved.stderr.strip() or resolved.stdout.strip()).splitlines()
        raise envs.RunError(f"cannot resolve: {lines[0] if lines else 'uv pip compile failed'}")
    return parse_compile_output(resolved.stdout, manifest.packages)


def bump(root: Path, name: str, upstream: Sequence[JsonObject]) -> Update | str | None:
    before = git(root, "rev-parse", "HEAD")
    version = "unknown"
    path = None
    try:
        shim_dir = envs.find_library(root, name)
        manifest_path = shim_dir / "wiretruth-shim.json"
        manifest = formats.load_shim_manifest(manifest_path)
        metadata = release_metadata(manifest.primary_package, manifest.language)
        version = cast(str, metadata["version"])
        pins = resolve_pins(root, manifest, metadata)
        if pins == manifest.packages:
            return None
        old = results.load_results_grades(
            results.results_path(root, name, manifest.packages[manifest.primary_package])
        )
        path = results.results_path(root, name, pins[manifest.primary_package])
        manifest_path.write_text(replace_pins(manifest_path.read_text(), manifest.packages, pins))
        if formats.load_shim_manifest(manifest_path).packages != pins:
            raise ValueError("rewritten manifest does not match resolved pins")
        paths = [str(manifest_path)]
        if manifest.language == "javascript":
            package = shim_dir / "package.json"
            package.write_text(replace_pins(package.read_text(), manifest.packages, pins))
            checked(
                root,
                "npm",
                "install",
                "--package-lock-only",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(shim_dir),
            )
            paths.extend([str(package), str(shim_dir / "package-lock.json")])
        commit(root, f"chore(shims): bump {name} to {version}", *paths)
        build_env(root, name)
        run = offline_run(root, name, "--write-results")
        if run.returncode:
            harness = [
                line for line in run.stdout.splitlines() if f"{GLYPHS['HARNESS']} HARNESS" in line
            ][:3]
            raise envs.RunError(
                " / ".join([f"run exited {run.returncode}: {failure(run)}", *harness])
            )
        new = results.load_results_grades(path)
        commit(root, f"chore(results): {name} {version}", str(path))
        return Update(
            old,
            new,
            results.package_changes(manifest.packages, pins),
            grade_changes(old, new, upstream),
            new_red_cells(new, upstream),
        )
    except (OSError, ValueError, KeyError) as error:
        git(root, "reset", "--hard", before)
        if path is not None and path.exists():
            tracked = command(root, "git", "ls-files", "--error-unmatch", "--", str(path))
            if tracked.returncode:
                path.unlink()
        return f"skipped {name} {version}: {error}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lib", action="append", default=[])
    parser.add_argument("--out", type=Path, default=Path(".wiretruth/weekly"))
    args = parser.parse_args(argv)
    root, out = Path.cwd(), args.out.resolve()
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    updates: list[Update] = []
    skipped: list[str] = []
    checks: dict[str, PinCheck] = {}
    up_to_date: list[str] = []
    changed = False
    try:
        base = git(root, "rev-parse", "HEAD")
        if git(root, "status", "--porcelain"):
            raise envs.RunError("weekly needs a clean checkout")
        git(root, "checkout", "-b", "weekly")
        names = sorted(
            set(args.lib)
            if args.lib
            else {
                path.parent.name
                for path in (root / "shims").glob("*/*/wiretruth-shim.json")
                if formats.load_shim_manifest(path).primary_package != "wiretruth"
            }
        )
        upstream = cast(
            list[JsonObject], formats.load_document("upstream", root / "results/upstream.json")
        )
        for name in names:
            print(f"Checking committed pins: {name}", flush=True)
            checks[name] = check_pins(root, name)
        for name in names:
            print(f"Checking latest release: {name}", flush=True)
            outcome = bump(root, name, upstream)
            if isinstance(outcome, Update):
                updates.append(outcome)
            elif outcome is None:
                up_to_date.append(name)
            else:
                skipped.append(outcome)
        if updates:
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                insert_changelog(
                    changelog.read_text(), [changelog_bullet(item) for item in updates]
                )
            )
            commit(root, f"docs(changelog): weekly run {date}", str(changelog))
            out.mkdir(parents=True, exist_ok=True)
            (out / "weekly.bundle").unlink(missing_ok=True)
            git(root, "bundle", "create", str(out / "weekly.bundle"), f"{base}..weekly")
            changed = True
    except (OSError, ValueError, KeyError) as error:
        skipped.append(f"weekly: {error}")
    server, repository, run_id = (
        os.environ.get(key) for key in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    )
    run_url = (
        f"{server}/{repository}/actions/runs/{run_id}"
        if all((server, repository, run_id))
        else None
    )
    body = pr_body(updates, skipped, checks, up_to_date, run_url)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pr-body.md").write_text(body)
    (out / "blocked").unlink(missing_ok=True)
    if changed and any(update.red for update in updates):
        (out / "blocked").touch()
    print(body.replace("## ", "").replace("`", ""), end="")
    for variable, value in (
        ("GITHUB_STEP_SUMMARY", body),
        ("GITHUB_OUTPUT", f"changed={'true' if changed else 'false'}\ndate={date}\n"),
    ):
        if os.environ.get(variable):
            with Path(os.environ[variable]).open("a") as stream:
                stream.write(value)
    return int(bool(skipped) or any(check.status != "matches" for check in checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
