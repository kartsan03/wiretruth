from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest
from ollama_samples import NATIVE_REASONING_LINES, PROBES, exchange

from wiretruth.formats import (
    Body,
    Call,
    ForcedTool,
    Message,
    Reasoning,
    Tool,
    load_scenario,
)
from wiretruth.providers import PROVIDERS, body_text, ollama, ollama_openai, openai_chat
from wiretruth.replay import BASE_PATHS, _chunks

REPO = Path(__file__).resolve().parents[2]
CALL = Call(
    False, (Message("user", "Name three moons of Jupiter."),), None, None, (), None, None, None, {}
)


def truth(payload, *, end="clean", status=200, request=None):
    ex = replace(exchange(), status=status, end=end, body=Body("json", payload, ()))
    return ollama.extract_truth([ex], request)


def test_native_probe_length_and_usage():
    result = ollama.extract_truth([exchange()], None)
    assert result == {
        "finish": "length",
        "raw_finish": "length",
        ("text"): (
            "Octopuses possess remarkable color-changing abilities, evolv"
            "ing distinct hues to camouflage against the"
        ),
        "tool_calls": [],
        "structured": None,
        "error": None,
        "stream_completed": None,
        "usage": {
            "input": 32,
            "cached_input": 0,
            "output": 16,
            "reasoning": None,
            "total": 48,
            "alternatives": {},
            "raw": {"prompt_eval_count": 32, "prompt_eval_cached_count": 0, "eval_count": 16},
        },
    }


@pytest.mark.parametrize("kind", [None, "text", "chunks_b64"])
@pytest.mark.parametrize("name", ["n-text", "n-text-stream"])
def test_native_probe_body_forms_and_returned_model(name, kind):
    ex = exchange(name, kind=kind)
    result = ollama.extract_truth([ex], None)
    assert result == ollama.extract_truth([exchange(name)], None)
    assert ollama.returned_model(ex) == ollama.DEFAULT_MODEL
    assert result["stream_completed"] is (True if "stream" in name else None)
    if ex.body.kind != "json":
        assert body_text(ex) == PROBES[name]["body"]
        if ex.body.kind != "text":
            assert b"".join(_chunks(ex.body)) == PROBES[name]["body"].encode()


@pytest.mark.parametrize("name", ["n-tools", "n-tools-stream"])
def test_native_whole_tool_calls_use_wire_ids_and_object_arguments(name):
    result = ollama.extract_truth([exchange(name)], None)
    assert (result["finish"], result["raw_finish"], result["text"]) == ("tool_calls", "stop", "")
    payloads = [json.loads(line) for line in PROBES[name]["body"].splitlines()]
    calls = [call for p in payloads for call in p["message"].get("tool_calls", [])]
    assert result["tool_calls"] == [
        {
            "id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
            "arguments_raw": json.dumps(
                call["function"]["arguments"], ensure_ascii=False, separators=(",", ":")
            ),
            "arguments_parsed_ok": True,
        }
        for call in calls
    ]
    if name.endswith("stream"):
        assert result["usage"]["cached_input"] == 288
        assert result["usage"]["input"] == 292


def test_native_call_without_id_and_empty_arguments():
    result = truth({"done": True, "message": {"tool_calls": [{"function": {"arguments": {}}}]}})
    assert result["tool_calls"] == [
        {
            "id": None,
            "name": "",
            "arguments": {},
            "arguments_raw": "{}",
            "arguments_parsed_ok": True,
        }
    ]


@pytest.mark.parametrize("tool", [None, {}, {"function": {"arguments": "{}"}}])
def test_native_rejects_non_object_tool_arguments(tool):
    with pytest.raises(ValueError, match="arguments"):
        truth({"message": {"tool_calls": [tool]}})


@pytest.mark.parametrize("name", ["n-tools-cut", "n-reasoning"])
def test_native_reasoning_and_cut_tool_calls_contribute_no_text(name):
    result = ollama.extract_truth([exchange(name)], None)
    assert (result["finish"], result["text"], result["tool_calls"]) == ("length", "", [])
    assert result["usage"]["reasoning"] is None


@pytest.mark.parametrize("content,expected", [(None, None), ("", ""), ("Café", "Café")])
def test_native_null_and_empty_content(content, expected):
    assert truth({"message": {"content": content, "thinking": "ignored"}})["text"] == expected


@pytest.mark.parametrize(
    "reason,finish",
    [
        ("stop", "stop"),
        ("length", "length"),
        ("load", "other"),
        ("unload", "other"),
        ("", "other"),
        (None, "none"),
    ],
)
@pytest.mark.parametrize("done", [True, False])
def test_native_finish_needs_done(reason, finish, done):
    result = truth({"done": done, "done_reason": reason})
    assert result["finish"] == (finish if done else "none")
    assert result["raw_finish"] == reason


@pytest.mark.parametrize("end", ["clean", "abort"])
@pytest.mark.parametrize("with_done", [True, False])
def test_native_stream_completion(end, with_done):
    ex = exchange("n-text-stream")
    if not with_done:
        ex = replace(ex, body=replace(ex.body, parts=ex.body.parts[:-1]))
    result = ollama.extract_truth([replace(ex, end=end)], None)
    assert result["stream_completed"] is (with_done and end == "clean")
    assert result["finish"] == ("length" if with_done else "none")
    assert result["text"]


def test_native_calls_without_done_have_no_finish():
    ex = exchange("n-tools-stream")
    result = ollama.extract_truth(
        [replace(ex, body=replace(ex.body, parts=ex.body.parts[:-1]))], None
    )
    assert len(result["tool_calls"]) == 2
    assert result["finish"] == "none"


@pytest.mark.parametrize("name", ["n-structured", "n-structured-ok", "n-stop"])
def test_native_structured_and_stop_probes(name):
    probe = PROBES[name]
    result = ollama.extract_truth([exchange(name)], probe["request"])
    assert result["finish"] == ("length" if name == "n-structured" else "stop")
    assert result["structured"] == (
        {"title": "The Godfather", "year": 1972} if name == "n-structured-ok" else None
    )


@pytest.mark.parametrize(
    "request_body,content,expected",
    [
        (None, "{}", None),
        ({"format": {}}, '{"x":0,"x":1}', {"x": 1}),
        ({"format": {}}, "bad", None),
    ],
)
def test_native_structured_duplicate_keys_and_invalid_content(request_body, content, expected):
    assert (
        truth(
            {"done": True, "done_reason": "stop", "message": {"content": content}},
            request=request_body,
        )["structured"]
        == expected
    )


@pytest.mark.parametrize(
    "counters,total",
    [
        ({}, None),
        ({"prompt_eval_count": 0}, None),
        ({"eval_count": 3}, None),
        ({"prompt_eval_count": 0, "eval_count": 3}, 3),
        ({"prompt_eval_count": True, "eval_count": 3}, None),
    ],
)
def test_native_usage_omits_duration_and_missing_counters(counters, total):
    usage = truth({**counters, "total_duration": 234})["usage"]
    assert usage["raw"] == counters
    assert usage["total"] == total
    assert usage["alternatives"] == {}


@pytest.mark.parametrize("name", ["n-unknown-model", "n-bad-body"])
def test_native_http_error_probe(name):
    result = ollama.extract_truth([exchange(name)], None)
    assert result["finish"] == "error"
    assert result["error"] == {
        "message": json.loads(PROBES[name]["body"])["error"],
        "status": PROBES[name]["status"],
        "type": None,
        "code": None,
    }


def test_native_in_band_error_overrides_finish_and_preserves_text():
    ex = exchange("n-text-stream")
    ex = replace(
        ex,
        body=replace(
            ex.body,
            parts=(*ex.body.parts, '{"error":"an error was encountered while running the model"}'),
        ),
    )
    result = ollama.extract_truth([ex], None)
    assert result["finish"] == "error" and result["text"]
    assert result["error"] == {
        "message": "an error was encountered while running the model",
        "type": None,
        "code": None,
        "status": None,
    }
    assert truth({"error": "", "done": True})["finish"] == "error"


def test_native_only_last_exchange_counts():
    assert ollama.extract_truth(
        [exchange("n-unknown-model"), exchange()], None
    ) == ollama.extract_truth([exchange()], None)
    with pytest.raises(ValueError, match="no exchanges"):
        ollama.extract_truth([], None)


@pytest.mark.parametrize("value", ["bad", "[]", '{"done":false}\n\n', '{"done":false}\nno-json'])
def test_native_non_json_line_or_non_object_is_rejected(value):
    ex = replace(exchange("n-text-stream"), body=Body("text", value, ()))
    with pytest.raises(ValueError):
        ollama.extract_truth([ex], None)


def test_native_bad_utf8_and_non_stream_body():
    ex = replace(
        exchange("n-text-stream"),
        body=Body("chunks_b64", None, (base64.b64encode(b"\xff").decode(),)),
    )
    with pytest.raises(ValueError):
        ollama.extract_truth([ex], None)
    with pytest.raises(ValueError, match="not a JSON object"):
        truth([])
    ex = replace(
        exchange(),
        body=Body("chunks_b64", None, (base64.b64encode(b'{"done":').decode(),)),
        end="abort",
    )
    assert ollama.extract_truth([ex], None)["finish"] == "none"
    ex = replace(ex, status=500, end="clean")
    assert ollama.extract_truth([ex], None)["error"]["message"] == '{"done":'
    assert truth({}, status=503)["error"]["message"] == "HTTP 503"
    ex = replace(ex, body=Body("text", "", ()))
    assert ollama.extract_truth([ex], None)["error"]["message"] == "HTTP 500"


@pytest.mark.parametrize(
    "body",
    [
        Body("ndjson", None, ()),
        Body("ndjson", None, ('{"model":"first"}', "{}")),
        Body("ndjson", None, ("bad",)),
        Body("json", {}, ()),
    ],
)
def test_native_returned_model_missing_or_bad(body):
    assert ollama.returned_model(replace(exchange(), body=body)) is None


def test_native_request_has_no_injected_options_or_auth():
    req = ollama.build_request(CALL, ollama.DEFAULT_MODEL)
    assert req.body == {
        "model": "qwen3.5:0.8b",
        "messages": [{"role": "user", "content": "Name three moons of Jupiter."}],
        "stream": False,
    }
    assert req.path == "/api/chat" and req.method == "POST"
    assert req.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert ollama.auth_headers("unused") == {}


@pytest.mark.parametrize(
    "effort,think",
    [
        ("none", False),
        ("minimal", True),
        ("low", True),
        ("medium", True),
        ("high", True),
        ("xhigh", True),
        ("max", True),
    ],
)
def test_native_request_options_and_reasoning(effort, think):
    call = replace(
        CALL,
        stream=True,
        max_output_tokens=16,
        stop=("END",),
        tools=(Tool("get_weather", "Weather", {}),),
        tool_choice="auto",
        output_schema={"type": "object"},
        reasoning=Reasoning(effort),
        extra={"include_usage": True},
    )
    req = ollama.build_request(call, "model")
    assert req.body == {
        "model": "model",
        "messages": [{"role": "user", "content": "Name three moons of Jupiter."}],
        "stream": True,
        "options": {"num_predict": 16, "stop": ["END"]},
        "think": think,
        "tools": [
            {
                "type": "function",
                "function": {"name": "get_weather", "description": "Weather", "parameters": {}},
            }
        ],
        "format": {"type": "object"},
    }
    assert req.headers["Accept"] == "application/x-ndjson"


@pytest.mark.parametrize("choice", ["required", "none", ForcedTool("get_weather")])
def test_native_rejects_tool_choice(choice):
    with pytest.raises(ValueError, match="has no meaning for ollama"):
        ollama.build_request(replace(CALL, tool_choice=choice), "model")


def test_native_rejects_unknown_extra():
    with pytest.raises(ValueError, match="extra field 'surprise' has no meaning for ollama"):
        ollama.build_request(replace(CALL, extra={"surprise": True}), "model")


@pytest.mark.parametrize(
    "path,body,expected",
    [
        ("/api/chat", {}, True),
        ("/api/chat", {"stream": True}, True),
        ("/api/chat", {"stream": False}, False),
        ("/api/chat", None, False),
        ("/other", {}, False),
    ],
)
def test_native_requested_stream_defaults_true(path, body, expected):
    assert ollama.requested_stream(path, body) is expected


@pytest.mark.parametrize("provider", [ollama, ollama_openai])
def test_ollama_registration_defaults_and_scenarios(provider):
    assert PROVIDERS[provider.ID] is provider
    assert provider.DEFAULT_MODEL == "qwen3.5:0.8b"
    assert provider.KEY_ENV == ""
    assert "http://127.0.0.1:11434" + BASE_PATHS[provider.ID] == provider.API_BASE
    assert provider.auth_headers("ignored") == {}
    for path in (REPO / "suite/scenarios").glob("*/*.json"):
        scenario = load_scenario(path)
        if provider.ID in scenario.providers:
            provider.build_request(scenario.call_for(provider.ID), provider.DEFAULT_MODEL)


@pytest.mark.parametrize("limit", [None, 16])
def test_compatible_request_only_renames_token_limit(limit):
    call = replace(
        CALL,
        stream=True,
        max_output_tokens=limit,
        stop=("END",),
        tools=(Tool("get_weather", "Weather", {}),),
        output_schema={},
        reasoning=Reasoning("none"),
        extra={"include_usage": True},
    )
    expected = openai_chat.build_request(call, "model")
    if limit is not None:
        expected.body["max_tokens"] = expected.body.pop("max_completion_tokens")
    assert ollama_openai.build_request(call, "model") == expected
    for helper in (
        "extract_truth",
        "returned_model",
        "requested_stream",
        "finish_index",
        "error_event",
        "replace_finish",
    ):
        assert getattr(ollama_openai, helper) is getattr(openai_chat, helper)


@pytest.mark.parametrize(
    "name",
    [
        "o-text",
        "o-no-effort",
        "o-text-stream-usage",
        "o-text-stream",
        "o-tools",
        "o-tools-stream",
        "o-unknown-model",
    ],
)
def test_compatible_probe_bytes(name):
    result = ollama_openai.extract_truth([exchange(name)], PROBES[name]["request"])
    if name == "o-unknown-model":
        assert result["error"] == {
            "status": 404,
            "type": "not_found_error",
            "code": None,
            "message": "model 'no-such-model:1b' not found",
        }
        assert result["finish"] == "error"
        return
    assert ollama_openai.returned_model(exchange(name)) == "qwen3.5:0.8b"
    assert result["finish"] == ("tool_calls" if "tools" in name else "length")
    assert result["stream_completed"] is (True if "stream" in name else None)
    if name == "o-no-effort":
        assert result["text"] == ""  # message.reasoning is not answer text
    if "tools" in name:
        assert len(result["tool_calls"]) == 2
        assert [c["arguments"] for c in result["tool_calls"]] == [
            {"city": "São Paulo"},
            {"city": "Zürich"},
        ]
        assert all(c["id"] and c["arguments_parsed_ok"] for c in result["tool_calls"])
    if name in ("o-text", "o-no-effort", "o-text-stream-usage"):
        assert result["usage"]["output"] == 16
        assert result["usage"]["total"] == result["usage"]["input"] + 16
        assert result["usage"]["reasoning"] is None
        assert "timings" not in result["usage"]["raw"]
    if name == "o-text-stream":
        assert result["usage"]["raw"] is None


def test_new_ollama_scenario_calls():
    scenario = load_scenario(REPO / "suite/scenarios/request/no-injected-options.json")
    assert scenario.providers == ("ollama",)
    assert scenario.call.max_output_tokens is scenario.call.stop is None
    assert scenario.call.reasoning.effort == "none"  # think: false keeps the call short
    body = ollama.build_request(scenario.call, ollama.DEFAULT_MODEL).body
    assert set(body) == {"model", "messages", "stream", "think"}  # no "options" object at all
    scenario = load_scenario(REPO / "suite/scenarios/request/max-tokens-ollama-openai.json")
    assert scenario.providers == ("ollama-openai",)
    body = ollama_openai.build_request(scenario.call, ollama_openai.DEFAULT_MODEL).body
    assert body["max_tokens"] == 16
    assert "max_completion_tokens" not in body
    scenario = load_scenario(REPO / "suite/scenarios/finish/stop-sequence.json")
    assert scenario.providers == ("ollama", "ollama-openai")
    assert scenario.call.stop == ("END",)
    assert scenario.call.max_output_tokens == 120
    assert scenario.call.reasoning.effort == "none"


def test_native_unicode_line_separators_are_content():
    line = json.dumps(
        {"done": True, "done_reason": "stop", "message": {"content": "Café\u2028東京\u2029🌸"}},
        ensure_ascii=False,
    )
    ex = replace(exchange("n-text-stream"), body=Body("ndjson", None, (line,)))
    assert ollama.extract_truth([ex], None)["text"] == "Café\u2028東京\u2029🌸"


def test_native_streamed_thinking_is_not_answer_text():
    ex = replace(exchange("n-text-stream"), body=Body("ndjson", None, NATIVE_REASONING_LINES))
    result = ollama.extract_truth([ex], None)
    assert result["finish"] == "length"
    assert result["text"] == ""
    assert result["usage"]["reasoning"] is None
    assert result["stream_completed"] is True
