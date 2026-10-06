import dataclasses
import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import get_args

import pytest
from jsonschema import Draft202012Validator

from wiretruth import formats
from wiretruth.formats import (
    KINDS,
    ForcedTool,
    FormatError,
    Problem,
    json_path,
    load_bug_classes,
    load_call_spec,
    load_document,
    load_fixture,
    load_observation,
    load_scenario,
    load_schema,
    load_shim_manifest,
    read_json,
    schema_names,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "tests" / "samples"

LOADERS: dict[str, Callable[[Path], object]] = {
    "fixture": load_fixture,
    "scenario": load_scenario,
    "call": load_call_spec,
    "observation": load_observation,
    "shim-manifest": load_shim_manifest,
    "bug-classes": load_bug_classes,
    "results": partial(load_document, "results"),
    "upstream": partial(load_document, "upstream"),
}


def _samples(group: str) -> list[Path]:
    return sorted((SAMPLES / group).glob("*/*.json"))


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


@pytest.mark.parametrize("path", _samples("valid"), ids=_id)
def test_valid_sample_loads(path: Path) -> None:
    LOADERS[path.parent.name](path)


@pytest.mark.parametrize("path", _samples("invalid"), ids=_id)
def test_invalid_sample_reports_every_problem(path: Path) -> None:
    with pytest.raises(FormatError) as exc:
        LOADERS[path.parent.name](path)
    expected = path.with_suffix(".expected").read_text(encoding="utf-8").splitlines()
    assert [str(problem) for problem in exc.value.problems] == [
        f"{path}: {line}" for line in expected
    ]


def test_every_kind_has_valid_and_invalid_samples() -> None:
    assert set(LOADERS) == set(KINDS)
    for kind in KINDS:
        assert list((SAMPLES / "valid" / kind).glob("*.json")), kind
        assert list((SAMPLES / "invalid" / kind).glob("*.json")), kind


def test_several_problems_in_one_file_are_all_reported() -> None:
    with pytest.raises(FormatError) as exc:
        load_fixture(SAMPLES / "invalid" / "fixture" / "several-errors.json")
    assert len(exc.value.problems) == 6
    assert str(exc.value) == "\n".join(str(problem) for problem in exc.value.problems)


@pytest.mark.parametrize("name", schema_names())
def test_bundled_schemas_are_valid_draft_2020_12(name: str) -> None:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == f"https://kartsan03.github.io/wiretruth/schemas/{name}"


def test_every_kind_has_a_schema() -> None:
    assert set(schema_names()) == {f"{kind}.schema.json" for kind in KINDS} | {"common.schema.json"}


@pytest.mark.parametrize(
    ("literal", "definition"),
    [
        (formats.Tier, "tier"),
        (formats.Finish, "finish"),
        (formats.ErrorCategory, "error_category"),
        (formats.Surface, "surface"),
        (formats.Language, "language"),
    ],
)
def test_python_vocabulary_matches_the_schema(literal: object, definition: str) -> None:
    defs = load_schema("common.schema.json")["$defs"]
    assert isinstance(defs, dict)
    enum = defs[definition]
    assert isinstance(enum, dict)
    assert list(get_args(literal)) == enum["enum"]


def test_fixture_loads_into_immutable_records() -> None:
    fixture = load_fixture(SAMPLES / "valid" / "fixture" / "recorded-sse.json")
    assert fixture.id == "openai-chat/truncation.text-stream"
    assert fixture.tier == "recorded"
    assert fixture.provenance.ci_run == "https://github.com/kartsan03/wiretruth/actions/runs/1"
    (exchange,) = fixture.exchanges
    assert (exchange.method, exchange.path, exchange.status, exchange.end) == (
        "POST",
        "/v1/chat/completions",
        200,
        "clean",
    )
    assert exchange.body.kind == "sse"
    assert exchange.body.data is None
    assert exchange.body.parts[-1] == "data: [DONE]\n\n"
    assert fixture.truth.finish == "length"
    with pytest.raises(dataclasses.FrozenInstanceError):
        fixture.id = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        exchange.headers["x-request-id"] = "1"  # type: ignore[index]


def test_fixture_body_forms() -> None:
    documented = load_fixture(SAMPLES / "valid" / "fixture" / "documented-json.json")
    body = documented.exchanges[0].body
    assert body.kind == "json"
    assert isinstance(body.data, dict)
    assert body.parts == ()
    assert documented.truth.usage.alternatives == {"input": {"uncached_input": 24}}

    retry = load_fixture(SAMPLES / "valid" / "fixture" / "derived-retry.json")
    first, second = retry.exchanges
    assert (first.status, first.body.kind, first.body.data) == (
        429,
        "text",
        "Resource has been exhausted",
    )
    assert first.headers["retry-after"] == "1"
    assert second.end == "clean"
    assert retry.derived_from is not None
    assert retry.derived_from.transform == "prepend_http_error"
    assert retry.truth.error is not None
    assert retry.truth.error.status == 429

    chunks = load_fixture(SAMPLES / "valid" / "fixture" / "derived-chunks-abort.json")
    assert chunks.id == "openai-chat/stream.utf8-split@abort"
    assert chunks.exchanges[0].body.parts == ("ZGF0YTog", "e30KCg==", "0Jw=")
    assert chunks.exchanges[0].end == "abort"


def test_scenario_applies_call_overrides_per_provider() -> None:
    scenario = load_scenario(SAMPLES / "valid" / "scenario" / "with-overrides.json")
    assert scenario.call.tool_choice == "required"
    assert scenario.call_for("openai-chat") is scenario.call
    anthropic = scenario.call_for("anthropic")
    assert anthropic.tool_choice == "auto"
    assert anthropic.messages[0].content == "Call create_event for the Lviv coffee festival."
    assert anthropic.tools == scenario.call.tools
    assert scenario.requirements[2].providers == ("openai-chat",)
    assert scenario.requirements[0].providers is None
    assert scenario.references == ()


def test_scenario_call_defaults() -> None:
    scenario = load_scenario(SAMPLES / "valid" / "scenario" / "structured-output.json")
    call = scenario.call
    assert (call.stop, call.tools, call.tool_choice, call.extra) == (None, (), None, {})
    assert call.reasoning is not None
    assert call.reasoning.effort == "none"
    assert scenario.requirements[2].args == {"fields": ["input", "output"]}


def test_call_spec_with_forced_tool() -> None:
    spec = load_call_spec(SAMPLES / "valid" / "call" / "forced-tool.json")
    assert (spec.scenario, spec.provider, spec.model) == (
        "finish.stop-sequence",
        "gemini",
        "gemini-3.5-flash-lite",
    )
    assert spec.call.tool_choice == ForcedTool("noop")
    assert spec.call.stop == ("END",)
    assert spec.call.reasoning is None


def test_observation_records() -> None:
    raised = load_observation(SAMPLES / "valid" / "observation" / "raised.json")
    assert raised.outcome == "raised"
    assert raised.error is not None
    assert (raised.error.category, raised.error.retry_after) == ("rate_limit", 1.5)
    assert raised.finish is not None
    assert raised.finish.surface == "absent"
    assert raised.usage is None

    returned = load_observation(SAMPLES / "valid" / "observation" / "returned.json")
    assert returned.elapsed_ms == 42.0
    assert returned.usage is not None
    assert returned.usage.surface["cached_input"] == "absent"
    assert returned.tool_calls[0].arguments == {"city": "Kyiv"}


def test_shim_manifest_records() -> None:
    manifest = load_shim_manifest(SAMPLES / "valid" / "shim-manifest" / "vercel-ai.json")
    assert manifest.entry == ("node", "shim.mjs")
    assert manifest.finish_map["tool-calls"] == "tool_calls"
    assert manifest.surfaces["finish"]["raw"] == "result.rawFinishReason"
    # A JavaScript named group: not valid Python, and only Python patterns are checked.
    assert manifest.error_map[2].message_regex == r"^output truncated \((?<limit>\d+)\)"
    assert manifest.unsupported[0].providers == ("openai-chat",)
    minimal = load_shim_manifest(SAMPLES / "valid" / "shim-manifest" / "minimal.json")
    assert minimal.env == {}


def test_suite_registry_has_the_seven_bug_classes() -> None:
    classes = load_bug_classes(REPO / "suite" / "bug-classes.json")
    assert [bug_class.id for bug_class in classes] == [
        "silent-truncation",
        "finish-reason-mapping",
        "usage-accounting",
        "stream-integrity",
        "tool-call-fidelity",
        "error-classification",
        "request-fidelity",
    ]
    assert all(bug_class.references for bug_class in classes)


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ([], "$"),
        (["requirements", 0, "level"], "$.requirements[0].level"),
        (["headers", "content-type"], '$.headers["content-type"]'),
        (["call_overrides", "openai-chat", "stream"], '$.call_overrides["openai-chat"].stream'),
        ([3, "_x1"], "$[3]._x1"),
    ],
)
def test_json_path(parts: list[str | int], expected: str) -> None:
    assert json_path(parts) == expected


def test_problem_format() -> None:
    assert str(Problem("a.json", "$.x", "bad")) == "a.json: $.x: bad"


def test_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.json"
    with pytest.raises(FormatError) as exc:
        read_json(path)
    assert str(exc.value) == f"{path}: $: cannot read the file: No such file or directory"


def test_deeply_nested_json(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(FormatError) as exc:
        read_json(path)
    assert str(exc.value) == f"{path}: $: invalid JSON: nested too deeply"


def test_long_messages_are_shortened(tmp_path: Path) -> None:
    path = tmp_path / "bug-classes.json"
    path.write_text(json.dumps({"id": "x" * 400}))
    with pytest.raises(FormatError) as exc:
        load_bug_classes(path)
    (problem,) = exc.value.problems
    assert len(problem.message) == 300
    assert problem.message.endswith("…")


def test_checks_run_only_after_the_schema_passes(tmp_path: Path) -> None:
    doc = json.loads((SAMPLES / "valid" / "shim-manifest" / "vercel-ai.json").read_text())
    doc["primary_package"] = "missing"
    doc["docs"] = ["not a url"]
    path = tmp_path / "wiretruth-shim.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(FormatError) as exc:
        load_shim_manifest(path)
    assert [problem.where for problem in exc.value.problems] == ["$.docs[0]"]


def test_valid_strings_with_a_trailing_newline_are_rejected(tmp_path: Path) -> None:
    doc = json.loads((SAMPLES / "valid" / "bug-classes" / "one-class.json").read_text())
    doc[0]["id"] += "\n"
    path = tmp_path / "bug-classes.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(FormatError) as exc:
        load_bug_classes(path)
    assert [problem.where for problem in exc.value.problems] == ["$[0].id"]


def test_integers_too_long_to_convert(tmp_path: Path) -> None:
    path = tmp_path / "long.json"
    path.write_text('{"n": ' + "1" * 5000 + "}")
    with pytest.raises(FormatError) as exc:
        read_json(path)
    (problem,) = exc.value.problems
    assert problem.message.startswith("invalid JSON: ")
