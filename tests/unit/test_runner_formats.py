import json
from dataclasses import replace
from pathlib import Path

import pytest

from wiretruth.formats import (
    CallSpec,
    ForcedTool,
    FormatError,
    Message,
    Reasoning,
    Tool,
    ToolCall,
    call_spec_document,
    call_spec_from_document,
    load_call_spec,
    load_observation,
    load_scenario,
    observation_document,
    observation_from_document,
    parse_json,
    validate,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", sorted((REPO / "suite" / "scenarios").rglob("*.json")))
def test_every_suite_call_serializes_validates_and_round_trips(path, tmp_path):
    scenario = load_scenario(path)
    for provider in scenario.providers:
        spec = CallSpec(scenario.id, provider, "sample-model", scenario.call_for(provider))
        doc = call_spec_document(spec)
        assert validate("call", doc, "call") == []
        target = tmp_path / "call.json"
        target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        assert load_call_spec(target) == spec
        assert call_spec_from_document(parse_json(target.read_bytes())) == spec


@pytest.mark.parametrize("choice", [None, "auto", "required", "none", ForcedTool("temperature")])
def test_call_serializer_keeps_optional_fields_and_multibyte_text(choice):
    scenario = load_scenario(REPO / "suite" / "scenarios" / "truncation" / "text.json")
    call = replace(
        scenario.call,
        messages=(Message("user", "温度 🌡️"),),
        stop=("τέλος",),
        tools=(Tool("temperature", "Read a sensor", {"type": "object"}),),
        tool_choice=choice,
        output_schema={"type": "integer"},
        reasoning=Reasoning("low"),
        extra={"include_usage": True},
    )
    spec = CallSpec(scenario.id, "openai-chat", "sample-model", call)
    assert call_spec_from_document(call_spec_document(spec)) == spec
    call = replace(
        call, reasoning=None, max_output_tokens=None, stop=None, tools=(), output_schema=None
    )
    spec = replace(spec, call=call)
    assert call_spec_from_document(call_spec_document(spec)) == spec


@pytest.mark.parametrize("name", ["returned", "raised", "unsupported"])
def test_observation_document_loader_matches_file_loader(name):
    path = REPO / "tests" / "samples" / "valid" / "observation" / f"{name}.json"
    assert observation_from_document(parse_json(path.read_bytes())) == load_observation(path)


@pytest.mark.parametrize("path", sorted((REPO / "tests/samples/valid/observation").glob("*.json")))
def test_every_observation_serializes_validates_and_round_trips(path):
    observation = load_observation(path)
    document = observation_document(observation)
    assert validate("observation", document, "observation") == []
    assert observation_from_document(parse_json(json.dumps(document).encode())) == observation


def test_observation_serializer_keeps_required_nulls_and_unparsed_arguments():
    observation = load_observation(REPO / "tests/samples/valid/observation/returned.json")
    observation = replace(
        observation,
        finish=None,
        text=None,
        usage=None,
        tool_calls=(ToolCall(None, "temperature", '{"c":', False, None),),
        notes=("Temperature in °C",),
    )
    document = observation_document(observation)
    assert document["finish"] is document["text"] is document["usage"] is None
    assert document["tool_calls"] == [
        {
            "id": None,
            "name": "temperature",
            "arguments_raw": '{"c":',
            "arguments_parsed_ok": False,
            "arguments": None,
        }
    ]
    assert observation_from_document(document) == observation


@pytest.mark.parametrize("loader", [call_spec_from_document, observation_from_document])
def test_document_loaders_validate_with_the_given_source(loader):
    with pytest.raises(FormatError, match=r"source\.json"):
        loader({}, "source.json")
