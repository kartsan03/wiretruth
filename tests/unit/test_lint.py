import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from wiretruth.cli import main
from wiretruth.formats import JsonObject
from wiretruth.lint import lint
from wiretruth.providers import PROVIDERS

REPO = Path(__file__).resolve().parents[2]
VALID = REPO / "tests" / "samples" / "valid"


def _sample(name: str) -> JsonObject:
    doc = json.loads((VALID / name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def _write(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A small, consistent repository: registry, one scenario, two fixtures, results."""
    (tmp_path / "suite").mkdir()
    shutil.copy(REPO / "suite" / "bug-classes.json", tmp_path / "suite" / "bug-classes.json")

    scenario = _sample("scenario/structured-output.json")
    scenario["id"] = "truncation.text-stream"
    _write(tmp_path / "suite" / "scenarios" / "truncation" / "text-stream.json", scenario)

    fixtures = tmp_path / "suite" / "fixtures" / "openai-chat"
    _write(fixtures / "truncation.text-stream.json", _sample("fixture/recorded-sse.json"))
    # Only the dated id matters here, not what a dated file would really hold: the derived
    # sample gives the cross-checks a source fixture to look up.
    derived = _sample("fixture/derived-chunks-abort.json")
    derived.update(
        id="openai-chat/truncation.text-stream@2026-10-01", scenario="truncation.text-stream"
    )
    assert isinstance(derived["derived_from"], dict)
    derived["derived_from"]["fixture"] = "openai-chat/truncation.text-stream"
    _write(fixtures / "truncation.text-stream@2026-10-01.json", derived)

    results = tmp_path / "results"
    _write(results / "litellm" / "1.103.1.json", _sample("results/litellm-1.103.1.json"))
    shutil.copy(VALID / "upstream" / "upstream.json", results / "upstream.json")
    return tmp_path


def _lines(root: Path) -> list[str]:
    return [str(problem).replace(f"{root}/", "") for problem in lint(root).problems]


def test_the_repository_passes() -> None:
    report = lint(REPO)
    assert report.problems == []
    assert report.files >= 1


def test_a_consistent_suite_passes(root: Path) -> None:
    report = lint(root)
    assert report.problems == []
    assert report.files == 6
    assert report.notes == []


def test_missing_results_is_only_a_note(root: Path) -> None:
    shutil.rmtree(root / "results")
    report = lint(root)
    assert report.problems == []
    assert report.notes == [f"{root / 'results'}: no such directory, skipped"]


def test_missing_registry(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "suite" / "bug-classes.json").unlink()
    assert _lines(root) == ["suite/bug-classes.json: $: the bug-class registry is missing"]
    shutil.rmtree(root / "suite")
    (root / "suite").mkdir()
    assert main(["lint", str(root)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "wiretruth lint: 1 error in 1 of 3 files"


def test_scenario_cross_checks(root: Path) -> None:
    path = root / "suite" / "scenarios" / "truncation" / "text-stream.json"
    scenario = json.loads(path.read_text())
    scenario.update(id="truncation.text", bug_class="slow-start", providers=["gemini"])
    _write(path, scenario)
    assert _lines(root) == [
        "suite/fixtures/openai-chat/truncation.text-stream.json: $.provider: "
        "'openai-chat' is not in the providers of 'truncation.text-stream'",
        "suite/fixtures/openai-chat/truncation.text-stream@2026-10-01.json: $.provider: "
        "'openai-chat' is not in the providers of 'truncation.text-stream'",
        "suite/scenarios/truncation/text-stream.json: $.id: "
        "'truncation.text' doesn't match the file path: expected 'truncation.text-stream'",
        "suite/scenarios/truncation/text-stream.json: $.bug_class: "
        "'slow-start' is not a bug class in suite/bug-classes.json",
    ]


def test_unknown_requirement_checks_are_reported(root: Path) -> None:
    path = root / "suite" / "scenarios" / "truncation" / "text-stream.json"
    scenario = json.loads(path.read_text())
    scenario["requirements"] = [
        dict(id="first", level="MUST", check="finish_typo", text="First check."),
        dict(id="second", level="SHOULD", check="text_typo", text="Second check."),
        dict(id="known", level="MUST", check="finish_is", args={}, text="Known check."),
    ]
    _write(path, scenario)
    assert _lines(root) == [
        "suite/scenarios/truncation/text-stream.json: $.requirements[0].check: "
        "requirement 'first': unknown check 'finish_typo'",
        "suite/scenarios/truncation/text-stream.json: $.requirements[1].check: "
        "requirement 'second': unknown check 'text_typo'",
    ]


def test_fixture_cross_checks(root: Path) -> None:
    fixtures = root / "suite" / "fixtures" / "openai-chat"
    (fixtures / "truncation.text-stream.json").rename(fixtures / "truncation.text.json")
    assert _lines(root) == [
        "suite/fixtures/openai-chat/truncation.text-stream@2026-10-01.json: "
        "$.derived_from.fixture: no fixture 'openai-chat/truncation.text-stream' in suite/fixtures",
        "suite/fixtures/openai-chat/truncation.text.json: $.id: "
        "'openai-chat/truncation.text-stream' doesn't match the file path: "
        "expected 'openai-chat/truncation.text'",
    ]


def test_fixture_of_a_missing_scenario(root: Path) -> None:
    shutil.rmtree(root / "suite" / "scenarios")
    lines = _lines(root)
    assert len(lines) == 2
    assert lines[0] == (
        "suite/fixtures/openai-chat/truncation.text-stream.json: $.scenario: "
        "no scenario 'truncation.text-stream' in suite/scenarios"
    )


def test_an_invalid_scenario_does_not_cascade(root: Path) -> None:
    path = root / "suite" / "scenarios" / "truncation" / "text-stream.json"
    scenario = json.loads(path.read_text())
    scenario["priority"] = "P9"
    _write(path, scenario)
    assert _lines(root) == [
        "suite/scenarios/truncation/text-stream.json: $.priority: "
        "'P9' is not one of ['P0', 'P1', 'P2']",
    ]


def test_an_invalid_registry_skips_the_bug_class_check(root: Path) -> None:
    (root / "suite" / "bug-classes.json").write_text("{}")
    assert _lines(root) == ["suite/bug-classes.json: $: {} is not of type 'array'"]


def test_results_path_must_match_the_library(root: Path) -> None:
    results = root / "results" / "litellm"
    (results / "1.103.1.json").rename(results / "1.104.0.json")
    results.rename(root / "results" / "LiteLLM")
    assert _lines(root) == [
        "results/LiteLLM/1.104.0.json: $.library.name: "
        "'litellm' doesn't match the file path: expected 'LiteLLM'",
        "results/LiteLLM/1.104.0.json: $.library.version: "
        "'1.103.1' doesn't match the file path: expected '1.104.0'",
    ]


@pytest.mark.parametrize(
    "relative",
    ["suite/notes.json", "suite/scenarios/text.json", "results/litellm/1/2.json", "results/x.json"],
)
def test_unexpected_json_files(root: Path, relative: str) -> None:
    _write(root / relative, {})
    assert _lines(root) == [
        f"{relative}: $: unexpected JSON file: see docs/formats.md for where each format lives"
    ]


def test_cli_exits_0_on_valid_data(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint", str(root)]) == 0
    assert capsys.readouterr().out == "wiretruth lint: 6 files, no errors\n"


def test_cli_exits_1_and_lists_problems(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "suite" / "bug-classes.json").write_text("[")
    (root / "results" / "upstream.json").write_text("{}")
    assert main(["lint", str(root)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{root}/results/upstream.json: $: {{}} is not of type 'array'",
        f"{root}/suite/bug-classes.json: $: invalid JSON: Expecting value (line 1, column 2)",
        "wiretruth lint: 2 errors in 2 of 6 files",
    ]


def test_cli_exits_2_without_a_suite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["lint", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"wiretruth lint: {tmp_path / 'suite'}: no such directory\n"


def test_cli_notes_go_to_stderr(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.rmtree(root / "results")
    assert main(["lint", str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "wiretruth lint: 4 files, no errors\n"
    assert captured.err == f"wiretruth lint: {root / 'results'}: no such directory, skipped\n"


def test_cli_lints_the_current_directory_by_default(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    assert main(["lint"]) == 0
    assert capsys.readouterr().out == "wiretruth lint: 6 files, no errors\n"


def _rewrite(path: Path, change: Callable[[JsonObject], None]) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    change(doc)
    _write(path, doc)


def test_a_truth_that_doesnt_match_the_exchanges(root: Path) -> None:
    def edit(doc: JsonObject) -> None:
        truth = doc["truth"]
        assert isinstance(truth, dict) and isinstance(truth["usage"], dict)
        truth["finish"] = "stop"
        truth["usage"]["output"] = 16
        truth["text"] = "x" * 100

    path = "suite/fixtures/openai-chat/truncation.text-stream.json"
    _rewrite(root / path, edit)
    text = json.dumps("Kyiv, one of the oldest cities in Eastern Europe, was founded")
    assert _lines(root) == [
        f'{path}: $.truth.finish: doesn\'t match the exchanges, which give "length"',
        f"{path}: $.truth.text: doesn't match the exchanges, which give {text}",
        f"{path}: $.truth.usage.output: doesn't match the exchanges, which give null",
    ]


def test_a_truth_compared_whole_when_its_shape_differs(root: Path) -> None:
    def edit(doc: JsonObject) -> None:
        truth = doc["truth"]
        assert isinstance(truth, dict)
        truth["error"] = {"message": "boom"}

    path = "suite/fixtures/openai-chat/truncation.text-stream.json"
    _rewrite(root / path, edit)
    assert _lines(root) == [f"{path}: $.truth.error: doesn't match the exchanges, which give null"]


def test_exchanges_the_extractor_cant_read(root: Path) -> None:
    def edit(doc: JsonObject) -> None:
        doc["exchanges"][0]["response"]["body"] = {"sse": ["data: {oops\n\n"]}  # type: ignore[index]

    path = "suite/fixtures/openai-chat/truncation.text-stream.json"
    _rewrite(root / path, edit)
    (line,) = _lines(root)
    assert line.startswith(
        f"{path}: $.exchanges: the openai-chat truth extractor can't read them: event 0: the data "
        "is not JSON"
    )


def test_providers_without_an_extractor_are_noted(root: Path, monkeypatch) -> None:
    monkeypatch.delitem(PROVIDERS, "gemini")
    scenario_path = root / "suite" / "scenarios" / "truncation" / "text-stream.json"
    _rewrite(scenario_path, lambda doc: doc.update(providers=["openai-chat", "gemini"]))
    fixture = _sample("fixture/recorded-sse.json")
    fixture.update(id="gemini/truncation.text-stream", provider="gemini")
    _write(root / "suite" / "fixtures" / "gemini" / "truncation.text-stream.json", fixture)
    report = lint(root)
    assert report.problems == []
    assert report.notes == ["truth not recomputed for gemini: no truth extractor yet"]


def test_a_truth_difference_under_a_key_with_a_colon(root: Path) -> None:
    def edit(doc: JsonObject) -> None:
        exchanges = doc["exchanges"]
        assert isinstance(exchanges, list)
        events = exchanges[0]["response"]["body"]["sse"]  # type: ignore[index, call-overload]
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "a: b": 1}
        events.insert(-1, f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n")
        truth = doc["truth"]
        assert isinstance(truth, dict)
        truth["usage"] = {
            "input": 1,
            "cached_input": None,
            "output": 2,
            "reasoning": None,
            "total": 3,
            "alternatives": {},
            "raw": {**usage, "a: b": 2},
        }

    path = "suite/fixtures/openai-chat/truncation.text-stream.json"
    _rewrite(root / path, edit)
    assert _lines(root) == [
        f'{path}: $.truth.usage.raw["a: b"]: doesn\'t match the exchanges, which give 1'
    ]
