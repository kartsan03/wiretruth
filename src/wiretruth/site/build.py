"""Build deterministic, self-contained pages from suite and results data."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import cmp_to_key
from html import escape
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from string import Template
from typing import NamedTuple, cast

from wiretruth.catalog import load_suite
from wiretruth.formats import (
    BugClass,
    Fixture,
    JsonObject,
    JsonValue,
    Scenario,
    ShimManifest,
    load_bug_classes,
    load_document,
    load_shim_manifest,
)
from wiretruth.grading import RULES, Grade
from wiretruth.lint import SetupError
from wiretruth.runner import GLYPHS

REPO_URL = "https://github.com/kartsan03/wiretruth"
UPSTREAM_URL = f"{REPO_URL}/blob/main/results/upstream.json"
MEANINGS = {
    "PASS": "Reports what happened: meets every MUST requirement of the scenario.",
    "LOSSY": (
        "Doesn't contradict what happened, but you only find it in raw metadata, or not at all."
    ),
    "WRONG": "Reports something that contradicts what happened.",
    "CRASH": "Fails with an internal error, such as a KeyError in its own parser.",
    "HANG": "Doesn't return after the whole response was sent.",
    "UNSUPPORTED": (
        "The library doesn't support this provider or feature, according to its documentation."
    ),
    "HARNESS": "Our problem, not the library's.",
}
BAR_GRADES = ("PASS", "LOSSY", "WRONG", "CRASH", "HANG", "HARNESS")


class Library(NamedTuple):
    name: str
    versions: list[JsonObject]  # newest first
    cells: dict[tuple[str, str], JsonObject]
    manifest: ShimManifest | None


def _html(source: str, *, fragments: Mapping[str, str] | None = None, **values: object) -> str:
    """Escape every data substitution; fragments contain only HTML already rendered here."""
    substitutions = {key: escape(str(value), quote=True) for key, value in values.items()}
    substitutions.update(fragments or {})
    return Template(source).substitute(substitutions)


def _template(name: str, *, fragments: Mapping[str, str], **values: object) -> str:
    source = (resources.files("wiretruth.site") / "templates" / name).read_text(encoding="utf-8")
    return _html(source, fragments=fragments, **values)


def _object(value: JsonValue) -> JsonObject:
    return cast(JsonObject, value)


def _objects(doc: JsonObject, key: str) -> list[JsonObject]:
    return cast(list[JsonObject], doc.get(key) or [])


def _text(doc: JsonObject, key: str) -> str:
    return cast(str, doc[key])


def _version(doc: JsonObject) -> str:
    return _text(_object(doc["library"]), "version")


def _compare_versions(left: str, right: str) -> int:
    a, b = left.split("."), right.split(".")
    for x, y in zip(a, b, strict=False):
        if x.isdecimal() and y.isdecimal():
            if int(x) != int(y):
                return 1 if int(x) > int(y) else -1
        elif x != y:
            return 1 if x > y else -1
    return (len(a) > len(b)) - (len(a) < len(b))


def _cell_map(doc: JsonObject) -> dict[tuple[str, str], JsonObject]:
    return {
        (_text(cell, "scenario"), _text(cell, "provider")): cell for cell in _objects(doc, "cells")
    }


def _libraries(root: Path) -> list[Library]:
    libraries = []
    version_key = cmp_to_key(_compare_versions)
    for directory in sorted((root / "results").iterdir()):
        if not directory.is_dir():
            continue
        versions = [
            _object(load_document("results", path)) for path in sorted(directory.glob("*.json"))
        ]
        if not versions:
            continue
        versions.sort(key=lambda doc: version_key(_version(doc)), reverse=True)
        manifest = None
        for path in sorted((root / "shims").glob(f"*/{directory.name}/wiretruth-shim.json")):
            candidate = load_shim_manifest(path)
            if candidate.name == directory.name:
                manifest = candidate
                break
        libraries.append(Library(directory.name, versions, _cell_map(versions[0]), manifest))
    return libraries


def _label(cell: JsonObject | None) -> str:
    if cell is None:
        return "not covered"
    grade = cast(Grade, cell["grade"])
    return f"{GLYPHS[grade]} {grade}"


def _grade_class(grade: str) -> str:
    return grade.lower().replace(" ", "-")


def _grade(cell: JsonObject | None) -> str:
    return _html(
        '<span class="grade $cls">$label</span>',
        cls=_grade_class(_text(cell, "grade") if cell else "not covered"),
        label=_label(cell),
    )


def _cell_path(library: str, scenario: str, provider: str) -> str:
    return f"cells/{library}/{scenario}/{provider}.html"


def _link(href: str, label: str) -> str:
    return _html('<a href="$href">$label</a>', href=href, label=label)


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    headers = list(headers)
    short = {"Level", "Status", "Grade", "Tier", "Provider", "Version", "Check"}
    classes = ["short" if label in short else "" for label in headers]
    classes = [
        f"{cls} num" if label == "Version" else cls
        for label, cls in zip(headers, classes, strict=True)
    ]
    head = "".join(
        _html('<th scope="col" class="$cls">$label</th>', cls=cls, label=label)
        for label, cls in zip(headers, classes, strict=True)
    )
    body = "".join(
        _html(
            "<tr>$cells</tr>",
            fragments={
                "cells": "".join(
                    _html('<td class="$cls">$cell</td>', fragments={"cell": c}, cls=cls)
                    for c, cls in zip(row, classes, strict=True)
                )
            },
        )
        for row in rows
    )
    return _html(
        '<div class="table-wrap"><table><thead><tr>$head</tr></thead>'
        "<tbody>$body</tbody></table></div>",
        fragments={"head": head, "body": body},
    )


def _escaped(value: object) -> str:
    return _html("$value", value=value)


def _legend() -> str:
    return _table(
        ["Grade", "Meaning"],
        (
            [
                _html(
                    '<span class="grade $cls">$label</span>',
                    cls=_grade_class(grade),
                    label=f"{GLYPHS[cast(Grade, grade)]} {grade}",
                ),
                _escaped(meaning),
            ]
            for grade, meaning in MEANINGS.items()
        ),
    )


def _evidence(cell: JsonObject) -> str:
    grade = _text(cell, "grade")
    observation = _object(cell.get("observation") or {})
    if grade == "PASS":
        return "Every MUST requirement is satisfied."
    if grade == "UNSUPPORTED" and observation.get("unsupported_reason"):
        return _text(observation, "unsupported_reason")
    status = {"WRONG": "CONTRADICTED", "LOSSY": "NOT_OBSERVABLE"}.get(grade)
    if status:
        for requirement in _objects(cell, "requirements"):
            if requirement["level"] == "MUST" and requirement["status"] == status:
                return " ".join(_text(requirement, "evidence").splitlines())
    return " ".join(cast(list[str], cell["notes"])[0].splitlines()) if cell["notes"] else grade


def _summary(library: Library, pairs: list[tuple[str, str]]) -> str:
    counts = Counter(
        _text(library.cells[pair], "grade") if pair in library.cells else "not covered"
        for pair in pairs
    )
    segments, pills = [], []
    for grade in BAR_GRADES:
        if not counts[grade]:
            continue
        cls = _grade_class(grade)
        segments.append(
            _html(
                '<span class="segment $cls" data-grade="$cls" style="flex-grow: $count"></span>',
                cls=cls,
                count=counts[grade],
            )
        )
        pills.append(
            _html(
                '<button type="button" class="grade-pill $cls" data-grade="$cls">'
                '<span class="dot" aria-hidden="true"></span>$grade <span>$count</span></button>',
                cls=cls,
                grade=grade,
                count=counts[grade],
            )
        )
    exclusions = []
    if counts["UNSUPPORTED"]:
        exclusions.append(
            f"{counts['UNSUPPORTED']} unsupported "
            "(the library's docs say it can't make these calls)"
        )
    if counts["not covered"]:
        exclusions.append(f"{counts['not covered']} not covered")
    return _html(
        '<article class="meter"><div class="meter-heading"><a href="#library-$name">'
        '$name <span class="muted">$version</span></a><p class="pass-count">'
        "<strong data-ui-number>$passed</strong> <span>of $total pass</span></p></div>"
        '<div class="bar" role="img" aria-label="$description">$segments</div>'
        '$exclusions<div class="grade-pills">$pills</div></article>',
        fragments={
            "segments": "".join(segments),
            "pills": "".join(pills),
            "exclusions": _html(
                '<p class="exclusions muted">$text</p>', text=" · ".join(exclusions)
            )
            if exclusions
            else "",
        },
        name=library.name,
        version=_version(library.versions[0]),
        passed=counts["PASS"],
        total=sum(counts[grade] for grade in BAR_GRADES),
        description=", ".join(f"{grade}: {counts[grade]}" for grade in BAR_GRADES if counts[grade]),
    )


def _filters(groups: Mapping[str, Iterable[str]]) -> str:
    return "".join(
        _html(
            '<fieldset><legend>$label</legend><div class="toggle-track">$options</div></fieldset>',
            label=group.capitalize(),
            fragments={
                "options": "".join(
                    _html(
                        '<label class="toggle"><input type="radio" name="$group" value="$value"'
                        " $checked><span>$label</span></label>",
                        group=group,
                        value=value,
                        label=value or "All",
                        checked="checked" if not value else "",
                    )
                    for value in ["", *values]
                )
            },
        )
        for group, values in groups.items()
    )


def _index(
    families: dict[str, list[Scenario]], fixtures: dict[str, Fixture], libraries: list[Library]
) -> str:
    pairs = [
        (scenario.id, provider)
        for scenarios in families.values()
        for scenario in scenarios
        for provider in sorted(scenario.providers)
    ]
    columns = []
    for library in libraries:
        counts = Counter(_text(c, "grade") for pair, c in library.cells.items() if pair in pairs)
        columns.append(
            _html(
                '<th scope="col" id="library-$name" data-library="$name">$name $version'
                "<small>$counts</small></th>",
                name=library.name,
                version=_version(library.versions[0]),
                counts=" · ".join(
                    f"{counts[grade]} {GLYPHS[cast(Grade, grade)]}"
                    for grade in (*BAR_GRADES, "UNSUPPORTED")
                    if counts[grade]
                ),
            )
        )
    rows = []
    tip = 0
    for family, scenarios in families.items():
        rows.append(
            _html(
                '<tr class="family" data-family="$family"><th colspan="$span" scope="rowgroup">'
                "$family</th></tr>",
                family=family,
                span=len(libraries) + 1,
            )
        )
        for scenario in scenarios:
            rows.append(
                _html(
                    '<tr class="scenario" data-family="$family" data-scenario="$scenario">'
                    '<th colspan="$span" scope="rowgroup"><a href="scenarios/$scenario.html">'
                    '<code>$scenario</code> <span class="muted">$title</span></a></th></tr>',
                    family=family,
                    scenario=scenario.id,
                    title=scenario.title,
                    span=len(libraries) + 1,
                )
            )
            for provider in sorted(scenario.providers):
                fixture = fixtures.get(f"{provider}/{scenario.id}")
                tier = fixture.tier if fixture else "no fixture"
                marker = (
                    _html('<span class="badge">$tier</span>', tier=tier)
                    if tier != "recorded"
                    else ""
                )
                cells = []
                for library in libraries:
                    cell = library.cells.get((scenario.id, provider))
                    anchor = f"{library.name}/{scenario.id}/{provider}"
                    if cell is None:
                        cells.append(
                            _html(
                                '<td id="$anchor" data-library="$library" class="not-covered">'
                                '<span class="sr-only">not covered</span></td>',
                                anchor=anchor,
                                library=library.name,
                            )
                        )
                        continue
                    tip += 1
                    cells.append(
                        _html(
                            '<td id="$anchor" data-library="$library" class="result $cls">'
                            '<a class="cell-link grade $cls" href="$href"'
                            ' aria-describedby="tip-$tip">'
                            "$label</a>"
                            '<span role="tooltip" id="tip-$tip">$evidence</span></td>',
                            anchor=anchor,
                            library=library.name,
                            cls=_grade_class(_text(cell, "grade")),
                            href=_cell_path(library.name, scenario.id, provider),
                            tip=tip,
                            label=_label(cell),
                            evidence=_evidence(cell),
                        )
                    )
                rows.append(
                    _html(
                        '<tr class="matrix-row" data-family="$family" data-provider="$provider"'
                        ' data-tier="$tier" data-scenario="$scenario">'
                        '<th scope="row" class="short">$provider $marker</th>'
                        "$cells</tr>",
                        fragments={"marker": marker, "cells": "".join(cells)},
                        family=family,
                        provider=provider,
                        tier=tier,
                        scenario=scenario.id,
                    )
                )
    current = [library.versions[0] for library in libraries]
    updated = max(
        (datetime.fromisoformat(_text(_object(doc["run"]), "at")) for doc in current),
        default=None,
    )
    suite = max(
        (_text(doc, "suite_version") for doc in current),
        key=cmp_to_key(_compare_versions),
        default="unavailable",
    )
    return _template(
        "index.html",
        fragments={
            "summaries": "".join(_summary(library, pairs) for library in libraries),
            "filters": _filters(
                {
                    "provider": sorted({provider for _, provider in pairs}),
                    "family": families,
                    "tier": ["recorded", "documented", "derived"],
                    "library": [library.name for library in libraries],
                }
            ),
            "columns": "".join(columns),
            "rows": "".join(rows),
            "legend": _legend(),
        },
        updated=updated.date().isoformat() if updated else "unavailable",
        suite=suite,
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _fact_value(label: str, value: JsonValue) -> str:
    if isinstance(value, str):
        return _html('<div class="plain-value">$value</div>', value=value)
    if label in {"text", "error"} and isinstance(value, dict):
        return _facts(value)
    return _html("<pre>$value</pre>", value=_json(value))


def _facts(values: Mapping[str, JsonValue]) -> str:
    return _html(
        '<dl class="facts">$rows</dl>',
        fragments={
            "rows": "".join(
                _html(
                    "<div><dt>$label</dt><dd>$value</dd></div>",
                    fragments={"value": _fact_value(label, value)},
                    label=label.replace("_", " ").capitalize(),
                )
                for label, value in values.items()
            )
        },
    )


def _wire(fixture: Fixture) -> str:
    excerpts = []
    for exchange in fixture.exchanges:
        body = exchange.body
        if body.kind == "json":
            text = _json(body.data)
        elif body.kind == "text":
            text = str(body.data)
        else:
            text = "\n".join(body.parts)
        content_type = next(
            (value for key, value in exchange.headers.items() if key.lower() == "content-type"), ""
        )
        excerpts.append(
            f"status: {exchange.status}\ncontent-type: {content_type}\nbody ({body.kind}):\n{text}"
            + ("\nend: abort" if exchange.end == "abort" else "")
        )
    return "\n\n".join(excerpts)


def _provenance(fixture: Fixture) -> str:
    p = fixture.provenance
    rows: list[tuple[str, str]] = []
    if fixture.tier == "recorded":
        rows.extend(
            (label, _escaped(value or "unavailable"))
            for label, value in [
                ("Recorded at", p.recorded_at),
                ("Requested model", p.requested_model),
                ("Returned model", p.returned_model),
            ]
        )
        if p.ci_run:
            rows.append(("CI run", _link(p.ci_run, p.ci_run)))
    if p.source_url:
        rows.append(("Source", _link(p.source_url, p.source_url)))
    if fixture.derived_from:
        d = fixture.derived_from
        rows.extend(
            (label, _escaped(value))
            for label, value in [
                ("Transform", d.transform),
                ("Source fixture", d.fixture),
                ("Rationale", d.rationale),
            ]
        )
        rows.append(("Evidence", _link(d.evidence_url, d.evidence_url)))
    if p.notes:
        rows.append(("Notes", _escaped(p.notes)))
    return _html(
        '<p><span class="badge">$tier</span> <code>$id</code></p>$table',
        fragments={"table": _table(["Provenance", "Value"], rows)},
        tier=fixture.tier,
        id=fixture.id,
    )


def _upstream(entries: list[JsonObject], library: str, scenario: str, provider: str) -> str:
    reports = []
    for entry in entries:
        if (
            entry["library"] != library
            or scenario not in cast(list[str], entry["scenarios"])
            or provider not in cast(list[str], entry["providers"])
        ):
            continue
        links = " · ".join(
            _link(_text(entry, key), label)
            for key, label in [("issue", "Issue"), ("pr", "PR")]
            if entry[key]
        )
        reports.append(
            _html(
                '<article class="upstream"><p><span class="badge">$status</span> $links</p>'
                "<p>Fixed in: $fixed</p><p>$notes</p></article>",
                fragments={"links": links},
                status=entry["status"],
                fixed=entry["fixed_in"] or "not specified",
                notes=entry["notes"],
            )
        )
    return "".join(reports) or "<p>No matching upstream report.</p>"


def _cell(library: Library, cell: JsonObject, fixture: Fixture, upstream: list[JsonObject]) -> str:
    scenario, provider = _text(cell, "scenario"), _text(cell, "provider")
    observation = _object(cell.get("observation") or {})
    repeated = {
        f"{r['level']} {r['id']}: {r['status']}: {r['evidence']}"
        for r in _objects(cell, "requirements")
    }
    notes = [note for note in cast(list[str], cell["notes"]) if note not in repeated]
    notes.extend(f"shim: {note}" for note in cast(list[str], observation.get("notes") or []))
    truth = fixture.truth
    # The JSON summaries use the original validated documents, retaining all nested fields.
    reported = (
        _facts(
            {
                key: observation.get(key)
                for key in [
                    "outcome",
                    "finish",
                    "text",
                    "tool_calls",
                    "usage",
                    "stream",
                    "error",
                    "unsupported_reason",
                ]
            }
        )
        if observation
        else "<p>No observation was produced.</p>"
    )
    # Load-free conversion of the few typed truth records that contain mapping proxies.
    sent = _facts(
        {
            "finish": truth.finish,
            "raw_finish": truth.raw_finish,
            "text": truth.text,
            "tool_calls": [cast(JsonObject, dict(call.__dict__)) for call in truth.tool_calls],
            "usage": cast(
                JsonObject,
                {
                    **truth.usage.__dict__,
                    "alternatives": {k: dict(v) for k, v in truth.usage.alternatives.items()},
                },
            ),
            "stream_completed": truth.stream_completed,
            "error": cast(JsonObject, dict(truth.error.__dict__)) if truth.error else None,
        }
    )
    snippet = ""
    if library.manifest:
        snippet = _html(
            "<section><h2>Reproduce</h2><pre><code>$command</code></pre>"
            "<p>Plain-library snippet from the shim manifest.</p>"
            "<pre><code>$snippet</code></pre></section>",
            command=f"wiretruth serve {fixture.id}",
            snippet=library.manifest.providers.get(provider, "No snippet for this provider."),
        )
    return _template(
        "cell.html",
        fragments={
            "requirements": _table(
                ["Id", "Level", "Status", "Evidence"],
                (
                    [
                        _escaped(r["id"]),
                        _escaped(r["level"]),
                        _html(
                            '<span class="status grade $cls">$status</span>',
                            cls={
                                "SATISFIED": "pass",
                                "CONTRADICTED": "wrong",
                                "NOT_OBSERVABLE": "lossy",
                            }[_text(r, "status")],
                            status=r["status"],
                        ),
                        _escaped(r["evidence"]),
                    ]
                    for r in _objects(cell, "requirements")
                ),
            ),
            "notes": _html(
                '<h3>Notes</h3><ul class="notes">$items</ul>',
                fragments={"items": "".join(_html("<li>$note</li>", note=note) for note in notes)},
            )
            if notes
            else "",
            "sent": sent,
            "reported": reported,
            "provenance": _provenance(fixture),
            "reproduce": snippet,
            "upstream": _upstream(upstream, library.name, scenario, provider),
            "history": _table(
                ["Version", "Grade"],
                (
                    [
                        _escaped(_version(doc)),
                        _grade(_cell_map(doc).get((scenario, provider))),
                    ]
                    for doc in library.versions
                ),
            ),
        },
        label=_label(cell),
        cls=_grade_class(_text(cell, "grade")),
        library=library.name,
        version=_version(library.versions[0]),
        scenario=scenario,
        provider=provider,
        wire=_wire(fixture),
        observation=_json(cell["observation"]),
    )


def _scenario(
    scenario: Scenario, bug: BugClass, fixtures: dict[str, Fixture], libraries: list[Library]
) -> str:
    rows = []
    for provider in sorted(scenario.providers):
        fixture = fixtures.get(f"{provider}/{scenario.id}")
        rows.append(
            [
                _escaped(provider),
                _escaped(fixture.tier if fixture else "no fixture"),
                *[
                    _html(
                        '<a href="$href">$grade</a>',
                        fragments={"grade": _grade(cell)},
                        href="../" + _cell_path(library.name, scenario.id, provider),
                    )
                    if (cell := library.cells.get((scenario.id, provider)))
                    else _grade(None)
                    for library in libraries
                ],
            ]
        )
    return _template(
        "scenario.html",
        fragments={
            "requirements": _table(
                ["Level", "Id", "Check", "Requirement", "Providers"],
                (
                    [
                        _escaped(value)
                        for value in [
                            r.level,
                            r.id,
                            r.check,
                            r.text,
                            ", ".join(r.providers) if r.providers else "All",
                        ]
                    ]
                    for r in scenario.requirements
                ),
            ),
            "fixtures": _table(
                ["Provider", "Tier", *(library.name for library in libraries)], rows
            ),
        },
        id=scenario.id,
        title=scenario.title,
        summary=scenario.summary,
        bug_title=bug.title,
        description=bug.description,
        effect=bug.effect,
    )


def _page(title: str, content: str, prefix: str) -> str:
    return _template(
        "base.html",
        fragments={"content": content},
        title=title,
        prefix=prefix,
        repo=REPO_URL,
        upstream=UPSTREAM_URL,
    )


def _copy_static(source: Traversable, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.iterdir(), key=lambda entry: entry.name):
        path = destination / entry.name
        if entry.is_dir():
            _copy_static(entry, path)
        else:
            path.write_bytes(entry.read_bytes())


def build(root: Path, out: Path) -> int:
    """Rebuild a marked output directory and return the number of HTML pages."""
    for directory in [root / "results", root / "suite"]:
        if not directory.is_dir():
            raise SetupError(f"{directory}: no such directory")
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise SetupError(f"{out}: output must be a directory, not a file or symlink")
    if out.exists() and any(out.iterdir()) and not (out / ".wiretruth-site").is_file():
        raise SetupError(
            f"{out}: non-empty output directory has no .wiretruth-site marker; "
            "refusing to remove it"
        )
    families, fixtures = load_suite(root)
    libraries = _libraries(root)
    bugs = {bug.id: bug for bug in load_bug_classes(root / "suite" / "bug-classes.json")}
    upstream_path = root / "results" / "upstream.json"
    upstream = (
        cast(list[JsonObject], load_document("upstream", upstream_path))
        if upstream_path.is_file()
        else []
    )
    pages = {"index.html": _page("wiretruth results", _index(families, fixtures, libraries), "")}
    pages["method.html"] = _page(
        "Method · wiretruth",
        _template(
            "method.html",
            fragments={
                "legend": _legend(),
                "rules": "".join(_html("<li>$rule</li>", rule=rule) for rule in RULES),
            },
            docs=f"{REPO_URL}/blob/main/docs",
            upstream=UPSTREAM_URL,
        ),
        "",
    )
    pairs: set[tuple[str, str]] = set()
    for scenarios in families.values():
        for scenario in scenarios:
            pages[f"scenarios/{scenario.id}.html"] = _page(
                f"{scenario.id} · wiretruth",
                _scenario(scenario, bugs[scenario.bug_class], fixtures, libraries),
                "../",
            )
            pairs.update((scenario.id, provider) for provider in scenario.providers)
    for library in libraries:
        for pair, cell in sorted(library.cells.items()):
            if pair not in pairs:
                continue
            pages[_cell_path(library.name, *pair)] = _page(
                f"{_label(cell)} · {library.name} · {pair[0]} · {pair[1]}",
                _cell(library, cell, fixtures[_text(cell, "fixture")], upstream),
                "../../../",
            )
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / ".wiretruth-site").write_text("wiretruth site\n", encoding="utf-8")
    for name, content in sorted(pages.items()):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    _copy_static(resources.files("wiretruth.site") / "static", out / "static")
    return len(pages)
