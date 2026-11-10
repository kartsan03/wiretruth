import json
import re
import shutil
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from wiretruth.cli import main
from wiretruth.formats import load_fixture
from wiretruth.lint import SetupError
from wiretruth.site.build import TEXT_WITHHELD, _evidence, _version_key, build

REPO = Path(__file__).resolve().parents[2]
RESULT = REPO / "results" / "any-llm" / "1.40.2.json"


class Page(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.descriptions: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if value is None:
                continue
            if key == "id":
                assert value not in self.ids, f"duplicate id: {value}"
                self.ids.add(value)
            if key in {"href", "src"}:
                self.links.append(value)
            if key == "aria-describedby":
                self.descriptions.extend(value.split())


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site")
    assert main(["site", "--root", str(REPO), "--out", str(out)]) == 0
    return out


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    _write(tmp_path / "results" / "any-llm" / "1.40.2.json", json.loads(RESULT.read_text()))
    return tmp_path


def _snapshot(out: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(out)): path.read_bytes() for path in out.rglob("*") if path.is_file()
    }


def test_real_build_pages_and_determinism(built: Path) -> None:
    expected = {"index.html", "method.html"}
    for path in (REPO / "suite" / "scenarios").glob("*/*.json"):
        scenario = json.loads(path.read_text())
        expected.add(f"scenarios/{scenario['id']}.html")
    for directory in (REPO / "results").iterdir():
        if not directory.is_dir():
            continue
        latest = max(
            directory.glob("*.json"), key=lambda path: tuple(map(int, path.stem.split(".")))
        )
        for cell in json.loads(latest.read_text())["cells"]:
            expected.add(f"cells/{directory.name}/{cell['scenario']}/{cell['provider']}.html")
    assert {str(path.relative_to(built)) for path in built.rglob("*.html")} == expected
    before = _snapshot(built)
    assert build(REPO, built) == len(expected)
    assert _snapshot(built) == before
    assert all(
        str(REPO).encode() not in data for name, data in before.items() if name.endswith(".html")
    )
    for name in [
        "OFL.txt",
        "plus-jakarta-sans-latin-wght-normal.woff2",
        "plus-jakarta-sans-latin-ext-wght-normal.woff2",
    ]:
        assert (built / "static" / "fonts" / name).read_bytes() == (
            REPO / "src" / "wiretruth" / "site" / "static" / "fonts" / name
        ).read_bytes()
    schemas = {path.name for path in (REPO / "src" / "wiretruth" / "schemas").iterdir()}
    for name in schemas:
        assert (built / "schemas" / name).read_bytes() == (
            REPO / "src" / "wiretruth" / "schemas" / name
        ).read_bytes()
    assert {path.name for path in (built / "schemas").iterdir()} == schemas


def test_all_links_fragments_tooltips_and_css_urls_resolve(built: Path) -> None:
    pages = {path: Page(path.read_text()) for path in built.rglob("*.html")}

    def resolve(source: Path, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"}:
            return
        assert not parsed.scheme and not parsed.netloc and not parsed.path.startswith("/"), url
        target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
        assert target.is_relative_to(built.resolve()), (source, url)
        assert target.is_file(), (source, url)
        if parsed.fragment:
            assert unquote(parsed.fragment) in pages[target].ids, (source, url)

    for path, page in pages.items():
        for url in page.links:
            resolve(path, url)
        assert set(page.descriptions) <= page.ids
    for path in built.rglob("*.css"):
        for url in re.findall(r"url\([\"']?([^\"')]+)[\"']?\)", path.read_text()):
            resolve(path, url)


def test_data_is_escaped(root: Path) -> None:
    payloads = ["<script>alert(1)</script>", '"><img src=x onerror=alert(1)>']
    path = root / "results" / "any-llm" / RESULT.name
    doc = json.loads(path.read_text())
    cell = next(
        cell for cell in doc["cells"] if cell["requirements"] and cell["observation"]["text"]
    )
    cell["observation"]["text"]["value"] = " ".join(payloads)
    cell["notes"] = payloads
    cell["observation"]["notes"] = payloads
    cell["requirements"][0]["evidence"] = " ".join(payloads)
    _write(path, doc)
    fixture = root / "suite" / "fixtures" / f"{cell['fixture']}.json"
    doc = json.loads(fixture.read_text())
    doc["truth"]["text"] = " ".join(payloads)
    doc["provenance"]["notes"] = " ".join(payloads)
    _write(fixture, doc)
    scenario = next((root / "suite" / "scenarios").glob("*/*.json"))
    doc = json.loads(scenario.read_text())
    doc["title"] = " ".join(payloads)
    _write(scenario, doc)
    manifest = json.loads(
        (REPO / "shims" / "python" / "any-llm" / "wiretruth-shim.json").read_text()
    )
    manifest["providers"][cell["provider"]] = " ".join(payloads)
    _write(root / "shims" / "python" / "any-llm" / "wiretruth-shim.json", manifest)
    entries = json.loads((REPO / "results" / "upstream.json").read_text())
    entry = entries[0]
    entry.update(
        library="any-llm",
        scenarios=[cell["scenario"]],
        providers=[cell["provider"]],
        notes=" ".join(payloads),
    )
    _write(root / "results" / "upstream.json", [entry])
    out = root / "_site"
    assert main(["site", "--root", str(root)]) == 0
    rendered = "\n".join(path.read_text() for path in out.rglob("*.html"))
    assert "<script>alert" not in rendered and "<img src=x" not in rendered
    for payload in payloads:
        assert escape(payload, quote=True) in rendered
    assert "shim: &lt;script&gt;" in rendered


def test_output_safety_and_marked_rebuild(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    assert main(["site", "--out", "."]) == 2
    assert "no .wiretruth-site marker" in capsys.readouterr().err
    assert (root / "suite").is_dir() and (root / "results").is_dir()
    out = root / "_site"
    out.mkdir()
    keep = out / "keep.txt"
    keep.write_text("keep")
    assert main(["site"]) == 2
    assert keep.read_text() == "keep"
    (out / ".wiretruth-site").touch()
    assert main(["site"]) == 0
    assert not keep.exists()
    assert (out / ".wiretruth-site").is_file()
    capsys.readouterr()
    count = len(list(out.rglob("*.html")))
    assert main(["site"]) == 0
    assert capsys.readouterr().out == f"wiretruth site: {count} pages in _site\n"


@pytest.mark.parametrize("missing", ["results", "suite"])
def test_missing_input_exits_2(
    tmp_path: Path, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ({"results", "suite"} - {missing}).pop()).mkdir()
    assert main(["site", "--root", str(tmp_path)]) == 2
    assert f"{missing}: no such directory" in capsys.readouterr().err
    assert not (tmp_path / "_site").exists()


@pytest.mark.parametrize(
    "newer,older",
    [
        ("1.10.0", "1.9.0"),
        ("2.0", "1.99"),
        ("1.10", "1.9a"),
        ("1.0.0", "1.0.0rc1"),
        ("7.0.153", "7.0.153-rc.1"),
        ("1.0.0rc2", "1.0.0rc1"),
    ],
)
def test_version_order(newer: str, older: str) -> None:
    assert _version_key(newer) > _version_key(older)


def test_current_history_missing_and_retired_cells(root: Path) -> None:
    directory = root / "results" / "any-llm"
    (directory / RESULT.name).unlink()
    old = json.loads(RESULT.read_text())
    old["library"]["version"] = "1.9.0"
    old["suite_version"] = "0.9.0"
    old["run"]["at"] = "2020-01-01T00:00:00Z"
    old["cells"].pop(1)
    _write(directory / "1.9.0.json", old)
    new = json.loads(RESULT.read_text())
    new["library"]["version"] = "1.10.0"
    new["suite_version"] = "0.10.0"
    new["run"]["at"] = "2021-02-03T00:00:00Z"
    missing = new["cells"].pop(0)
    new["cells"].append({**missing, "scenario": "errors.retired-example"})
    _write(directory / "1.10.0.json", new)
    out = root / "_site"
    build(root, out)
    index = (out / "index.html").read_text()
    assert "any-llm 1.10.0" in index and "any-llm 1.9.0" not in index
    assert "Suite" not in index and "Updated 2021-02-03" in index
    assert "errors.retired-example" not in index
    assert not (out / "cells" / "any-llm" / "errors.retired-example").exists()
    assert f'id="any-llm/{missing["scenario"]}/{missing["provider"]}"' in index
    assert '<span class="sr-only">not covered</span>' in index
    cell = new["cells"][0]
    page = (out / "cells" / "any-llm" / cell["scenario"] / f"{cell['provider']}.html").read_text()
    history = page.split("<h2>History</h2>")[1]
    assert history.index("1.10.0") < history.index("1.9.0")
    assert "not covered" in history
    assert "<h2>Reproduce</h2>" not in page


def test_tooltip_uses_first_applicable_must_evidence() -> None:
    from wiretruth.formats import JsonObject

    cell: JsonObject = {
        "grade": "WRONG",
        "notes": ["other note"],
        "observation": None,
        "requirements": [
            {"level": "SHOULD", "status": "CONTRADICTED", "evidence": "should"},
            {"level": "MUST", "status": "NOT_OBSERVABLE", "evidence": "lossy"},
            {"level": "MUST", "status": "CONTRADICTED", "evidence": "wrong"},
        ],
    }
    assert _evidence(cell) == "wrong"
    cell["grade"] = "LOSSY"
    assert _evidence(cell) == "lossy"
    cell["grade"] = "CRASH"
    assert _evidence(cell) == "other note"
    cell["grade"] = "UNSUPPORTED"
    cell["observation"] = {"unsupported_reason": "reason"}
    assert _evidence(cell) == "reason"
    cell["grade"] = "PASS"
    assert _evidence(cell) == "Every MUST requirement is satisfied."


def test_summary_uses_applicable_cells_and_complete_header_counts(root: Path) -> None:
    path = root / "results" / "any-llm" / RESULT.name
    doc = json.loads(path.read_text())
    doc["cells"] = doc["cells"][:4]
    for cell, grade in zip(doc["cells"], ["PASS", "PASS", "WRONG", "UNSUPPORTED"], strict=True):
        cell["grade"] = grade
    _write(path, doc)
    build(root, root / "_site")
    index = (root / "_site" / "index.html").read_text()
    summary = re.search(r'<article class="meter">(.*?)</article>', index, re.S)
    assert summary is not None
    text = unescape(summary[1])
    assert "<strong data-ui-number>2</strong> <span>of 3 pass</span>" in text
    assert "1 unsupported" in text
    pairs = sum(
        len(json.loads(path.read_text())["providers"])
        for path in (root / "suite" / "scenarios").glob("*/*.json")
    )
    assert f"{pairs - 4} not covered" in text
    assert re.findall(r'class="segment (\S+)"', text) == ["pass", "wrong"]
    assert re.findall(r'class="grade-pill (\S+)"', text) == ["pass", "wrong"]
    header = re.search(r'id="library-any-llm".*?<small>(.*?)</small>', index, re.S)
    assert header is not None
    assert header[1] == "2 ✓ · 1 ✗ · 1 \u2013"


def test_matrix_groups_providers_by_scenario(built: Path) -> None:
    index = (built / "index.html").read_text()
    matrix = index.split('<table class="matrix">')[1].split("</table>")[0]
    groups = re.split(r'<tr class="scenario"', matrix)[1:]
    scenarios = {
        json.loads(path.read_text())["id"]: json.loads(path.read_text())
        for path in (REPO / "suite" / "scenarios").glob("*/*.json")
    }
    assert len(groups) == len(scenarios)
    libraries = [path for path in (REPO / "results").iterdir() if path.is_dir()]
    for group in groups:
        scenario_id = re.search(r'data-scenario="([^"]+)"', group)
        assert scenario_id is not None
        scenario = scenarios[scenario_id[1]]
        heading, *providers = re.findall(r"(.*?)</tr>", group, re.S)
        assert f"<code>{scenario['id']}</code>" in heading
        assert escape(scenario["title"], quote=True) in heading
        assert f'colspan="{len(libraries) + 1}"' in heading
        providers = [row for row in providers if 'class="matrix-row"' in row]
        assert len(providers) == len(scenario["providers"])
        for row, provider in zip(providers, sorted(scenario["providers"]), strict=True):
            assert f'data-provider="{provider}"' in row
            first_cell = re.search(r'<th scope="row" class="short">(.*?)</th>', row, re.S)
            assert first_cell is not None
            assert provider in first_cell[1] and scenario["id"] not in first_cell[1]


def test_requirement_notes_are_filtered_exactly(root: Path) -> None:
    path = root / "results" / "any-llm" / RESULT.name
    doc = json.loads(path.read_text())
    keep, omit = doc["cells"][:2]
    for cell in [keep, omit]:
        requirement = cell["requirements"][0]
        duplicate = (
            f"{requirement['level']} {requirement['id']}: "
            f"{requirement['status']}: {requirement['evidence']}"
        )
        cell["notes"] = [duplicate]
        cell["observation"]["notes"] = []
    evidence = keep["requirements"][0]["evidence"]
    keep["notes"].extend([evidence, keep["notes"][0] + " "])
    keep["observation"]["notes"] = [evidence]
    _write(path, doc)
    build(root, root / "_site")
    pages = [
        (
            root / "_site" / "cells" / "any-llm" / cell["scenario"] / f"{cell['provider']}.html"
        ).read_text()
        for cell in [keep, omit]
    ]
    notes = re.search(r'<h3>Notes</h3><ul class="notes">(.*?)</ul>', pages[0], re.S)
    assert notes is not None
    assert re.findall(r"<li>(.*?)</li>", unescape(notes[1]), re.S) == [
        evidence,
        keep["notes"][0] + " ",
        f"shim: {evidence}",
    ]
    assert "<h3>Notes</h3>" not in pages[1]


def test_grade_status_and_fact_rendering(built: Path) -> None:
    cell = (built / "cells" / "any-llm" / "truncation.text" / "openai-responses.html").read_text()
    assert '<dt>Text</dt><dd><div class="plain-value">' in cell
    assert '<dt>Text</dt><dd><dl class="facts">' in cell
    assert "<dt>Usage</dt><dd><pre>" in cell
    assert '<td class="short num">1.40.2</td>' in cell
    assert '<td class="short"><span class="grade pass">✓ PASS</span></td>' in cell
    assert "Back to results</a>" in cell
    assert ">GitHub</a>" in cell
    rendered = "\n".join(path.read_text() for path in (built / "cells").rglob("*.html"))
    assert '<dt>Message</dt><dd><div class="plain-value">' in rendered
    assert '<dt>Unsupported reason</dt><dd><div class="plain-value">' in rendered
    for status, grade in [
        ("SATISFIED", "pass"),
        ("CONTRADICTED", "wrong"),
        ("NOT_OBSERVABLE", "lossy"),
    ]:
        assert f'<span class="status grade {grade}">{status}</span>' in rendered


def test_a_cell_whose_fixture_is_gone_is_a_setup_error(root: Path) -> None:
    (root / "suite" / "fixtures" / "openai-chat" / "truncation.text.json").unlink()
    with pytest.raises(
        SetupError, match=re.escape("openai-chat/truncation.text, which is not in suite/fixtures")
    ):
        build(root, root / "_site")
    assert not (root / "_site").exists()


def test_older_recordings_keep_their_text_off_the_site(built: Path) -> None:
    pages = {
        path: path.read_text()
        for path in built.rglob("*")
        if path.is_file() and path.suffix != ".woff2"
    }
    for fixture_id in TEXT_WITHHELD:
        fixture = load_fixture(REPO / "suite" / "fixtures" / f"{fixture_id}.json")
        assert fixture.truth.text
        quoted = escape(fixture.truth.text[:24], quote=True)
        assert not [path for path, text in pages.items() if quoted in text], fixture_id
        provider, scenario = fixture_id.split("/")
        page = pages[built / "cells" / "any-llm" / scenario / f"{provider}.html"]
        assert "older prompts" in page
        assert "Wire excerpt" not in page and "Full observation JSON" not in page


def test_wire_excerpt_keeps_the_provider_key_order(built: Path) -> None:
    fixture = json.loads(
        (REPO / "suite" / "fixtures" / "openai-responses" / "truncation.text.json").read_text()
    )
    body = fixture["exchanges"][-1]["response"]["body"]["json"]
    assert list(body) != sorted(body)
    page = (built / "cells" / "any-llm" / "truncation.text" / "openai-responses.html").read_text()
    assert escape(json.dumps(body, ensure_ascii=False, indent=2), quote=True) in page
