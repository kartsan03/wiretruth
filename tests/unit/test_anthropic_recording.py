import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from anthropic_samples import MODEL, STREAM, TEXT

from wiretruth import recorder
from wiretruth.cli import main
from wiretruth.formats import (
    Call,
    ForcedTool,
    Message,
    Reasoning,
    Tool,
    load_fixture,
    load_scenario,
)
from wiretruth.providers import PROVIDERS, anthropic
from wiretruth.recorder import Response

REPO = Path(__file__).resolve().parents[2]
CALL = Call(False, (Message("user", "Hello"),), 16, None, (), None, None, None, {})


def test_anthropic_request_required_fields_and_registry():
    assert PROVIDERS["anthropic"] is anthropic
    assert anthropic.API_BASE == "https://api.anthropic.com"
    assert anthropic.DEFAULT_MODEL == MODEL and anthropic.KEY_ENV == "ANTHROPIC_API_KEY"
    request = anthropic.build_request(CALL, MODEL)
    assert request.method == "POST" and request.path == "/v1/messages"
    assert request.body == {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 16,
    }
    assert request.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert anthropic.auth_headers("unit-key") == {
        "x-api-key": "unit-key",
        "anthropic-version": "2023-06-01",
    }
    with pytest.raises(ValueError, match="requires max_output_tokens"):
        anthropic.build_request(replace(CALL, max_output_tokens=None), MODEL)


@pytest.mark.parametrize("choice", [None, "auto", "none", ForcedTool("get_weather")])
def test_anthropic_request_messages_tools_and_native_schema(choice):
    schema = {"type": "object", "properties": {"city": {"type": "string"}}}
    call = replace(
        CALL,
        stream=True,
        stop=("END",),
        messages=(
            Message("system", "Guide."),
            Message("system", "More."),
            Message("user", "Hello"),
            Message("assistant", "Hi"),
            Message("user", "Weather?"),
        ),
        tools=(Tool("get_weather", "Get weather.", schema),),
        tool_choice=choice,
        output_schema=schema,
        reasoning=Reasoning("medium"),
        extra={"include_usage": True, "anthropic": {"eager_input_streaming": True, "cache": True}},
    )
    body = anthropic.build_request(
        call, "claude-haiku-4-5" if isinstance(choice, ForcedTool) else MODEL
    ).body
    assert body["system"] == "Guide.\n\nMore."
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["tools"] == [
        {
            "name": "get_weather",
            "description": "Get weather.",
            "input_schema": schema,
            "eager_input_streaming": True,
        }
    ]
    assert body["stop_sequences"] == ["END"] and body["stream"] is True
    assert body["output_config"] == (
        {"effort": "medium"}
        if isinstance(choice, ForcedTool)
        else {"format": {"type": "json_schema", "schema": schema}, "effort": "medium"}
    )
    assert body["thinking"] == {"type": "adaptive"} and body["cache_control"] == {
        "type": "ephemeral"
    }
    expected = (
        {"type": "tool", "name": "get_weather"}
        if isinstance(choice, ForcedTool)
        else {"type": choice}
        if choice
        else None
    )
    assert body.get("tool_choice") == expected


@pytest.mark.parametrize(
    "effort,expected",
    [
        ("none", None),
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "max"),
    ],
)
def test_anthropic_effort_never_disables_thinking(effort, expected):
    body = anthropic.build_request(replace(CALL, reasoning=Reasoning(effort)), MODEL).body
    if expected is None:
        assert "thinking" not in body and "output_config" not in body
    else:
        assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {
            "effort": expected
        }


@pytest.mark.parametrize(
    "extra",
    [
        {"unknown": True},
        {"anthropic": None},
        {"anthropic": {"temperature": 1}},
        {"anthropic": {"cache": 1}},
    ],
)
def test_anthropic_rejects_unknown_extra(extra):
    with pytest.raises(ValueError, match="extra"):
        anthropic.build_request(replace(CALL, extra=extra), MODEL)


@pytest.mark.parametrize("choice", ["required", ForcedTool("create_event")])
def test_anthropic_rejects_forced_choice_on_current_model(choice):
    with pytest.raises(ValueError, match="forced tool_choice"):
        anthropic.build_request(replace(CALL, tool_choice=choice), MODEL)


def test_anthropic_scenario_overrides_and_cache_hint():
    for stream in [False, True]:
        name = "tool-call-args-stream" if stream else "tool-call-args"
        scenario = load_scenario(REPO / f"suite/scenarios/truncation/{name}.json")
        body = anthropic.build_request(scenario.call_for("anthropic"), MODEL).body
        assert body["tool_choice"] == {"type": "auto"}
        assert body["messages"][0]["content"].startswith("Call create_event.")
        assert body["tools"][0].get("eager_input_streaming", False) is stream
    scenario = load_scenario(REPO / "suite/scenarios/truncation/structured-output-forced-tool.json")
    body = anthropic.build_request(scenario.call_for("anthropic"), "claude-haiku-4-5").body
    assert body["tools"][0]["input_schema"] == scenario.call.output_schema
    assert body["tool_choice"] == {"type": "tool", "name": "structured_output"}
    assert "output_config" not in body
    scenario = load_scenario(REPO / "suite/scenarios/usage/cached-tokens.json")
    assert len(scenario.call.messages[0].content.split()) > 800
    assert anthropic.build_request(scenario.call, MODEL).body["cache_control"] == {
        "type": "ephemeral"
    }


@pytest.mark.parametrize("stream", [False, True])
def test_anthropic_recorder_round_trip_without_dispatch(stream, tmp_path, monkeypatch):
    scenario = load_scenario(
        REPO
        / (
            "suite/scenarios/truncation/text-stream.json"
            if stream
            else "suite/scenarios/truncation/text.json"
        )
    )
    raw = "".join(STREAM).encode() if stream else json.dumps(TEXT).encode()
    response = Response(
        200, {"content-type": "text/event-stream" if stream else "application/json"}, [raw], "clean"
    )
    send = MagicMock(return_value=response)
    monkeypatch.setattr(recorder, "send", send)
    monkeypatch.setattr(recorder, "_source_commit", lambda _: None)
    result = recorder.record_scenario(
        anthropic,
        scenario,
        model=MODEL,
        key="unit-key",
        root=tmp_path,
        base_url=anthropic.API_BASE,
        env={},
    )
    fixture = load_fixture(result.path)
    assert fixture.exchanges[0].path == "/v1/messages"
    assert fixture.provenance.returned_model == MODEL
    assert fixture.truth.text == "Hello!" and fixture.truth.usage.output == 15
    assert "unit-key" not in result.path.read_text()


def test_anthropic_dry_run_masks_api_key_but_keeps_version(monkeypatch, capsys):
    monkeypatch.chdir(REPO)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    send = MagicMock()
    monkeypatch.setattr(recorder, "send", send)
    assert (
        main(["record", "--provider", "anthropic", "--scenario", "truncation.text", "--dry-run"])
        == 0
    )
    output = capsys.readouterr().out
    assert "POST https://api.anthropic.com/v1/messages" in output
    assert "x-api-key: ***" in output and "anthropic-version: 2023-06-01" in output
    request = replace(anthropic.build_request(CALL, MODEL), headers={"X-Api-Key": "header-secret"})
    description = recorder.describe_request(anthropic, request, anthropic.API_BASE)
    assert "header-secret" not in description and "X-Api-Key: ***" in description
    send.assert_not_called()
