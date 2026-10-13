import base64
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from gemini_samples import (
    BAD_REQUEST,
    BLOCKED,
    EMPTY_CANDIDATE,
    MODEL,
    PATH,
    RATE_LIMIT,
    STREAM,
    STREAM_ERROR,
    STREAM_PATH,
    TEXT,
    THINKING,
    TOOLS,
    event,
    exchange,
)

from wiretruth.formats import Body, Call, ForcedTool, Message, Reasoning, Tool, load_scenario
from wiretruth.providers import PROVIDERS, gemini
from wiretruth.replay import BASE_PATHS

REPO = Path(__file__).resolve().parents[2]
CALL = Call(False, (Message("user", "Name an icy moon."),), None, None, (), None, None, None, {})


def truth(payload, *, status=200, request=None):
    return gemini.extract_truth([exchange(payload, status=status)], request)


def test_gemini_plain_stop():
    assert truth(TEXT) == {
        "finish": "stop",
        "raw_finish": "STOP",
        "text": "Europa is an icy moon.",
        "tool_calls": [],
        "structured": None,
        "error": None,
        "stream_completed": None,
        "usage": {
            "input": 9,
            "cached_input": None,
            "output": 6,
            "reasoning": None,
            "total": 15,
            "alternatives": {},
            "raw": TEXT["usageMetadata"],
        },
    }


@pytest.mark.parametrize(
    "parts,expected",
    [
        (None, None),
        ([], None),
        ([{"text": ""}], ""),
        ([{"text": "Café"}, {"text": " 東京", "thought": False}], "Café 東京"),
        ([{"text": "Private calculation.", "thought": True}], None),
        ([{"thoughtSignature": "c2lnbmF0dXJl"}], None),
    ],
)
def test_text_distinguishes_empty_absent_and_thoughts(parts, expected):
    assert truth({"candidates": [{"content": {"parts": parts}}]})["text"] == expected


@pytest.mark.parametrize(
    "content",
    [None, {}, {"role": "model"}, {"parts": []}, {"parts": [{"thoughtSignature": "c2lnbmF0dXJl"}]}],
)
def test_max_tokens_empty_candidate_backtest(content):
    payload = deepcopy(EMPTY_CANDIDATE)
    if content is not None:
        payload["candidates"][0]["content"] = content
    result = truth(payload)
    assert (result["finish"], result["raw_finish"], result["text"]) == (
        "length",
        "MAX_TOKENS",
        None,
    )
    assert result["tool_calls"] == []
    assert result["usage"]["output"] == result["usage"]["reasoning"] == 32


@pytest.mark.parametrize(
    "finish,expected",
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        *[
            (value, "content_filter")
            for value in (
                "SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "IMAGE_SAFETY",
                "IMAGE_PROHIBITED_CONTENT",
                "IMAGE_RECITATION",
            )
        ],
        *[
            (value, "other")
            for value in (
                "FINISH_REASON_UNSPECIFIED",
                "OTHER",
                "LANGUAGE",
                "MALFORMED_FUNCTION_CALL",
                "UNEXPECTED_TOOL_CALL",
                "TOO_MANY_TOOL_CALLS",
                "IMAGE_OTHER",
                "NO_IMAGE",
                "MISSING_THOUGHT_SIGNATURE",
                "MALFORMED_RESPONSE",
                "ESCALATION",
                "PUP_LIMITED_DISABLED",
                "WIRETRUTH_UNKNOWN_REASON",
                "",
            )
        ],
        (None, "none"),
    ],
)
def test_finish_table_keeps_wire_value(finish, expected):
    payload = deepcopy(TEXT)
    payload["candidates"][0]["finishReason"] = finish
    payload["candidates"][0]["finishMessage"] = "Additional provider detail."
    result = truth(payload)
    assert (result["finish"], result["raw_finish"]) == (expected, finish)
    assert result["text"] == "Europa is an icy moon."
    assert result["error"] is None


def test_thoughts_are_excluded_but_signature_does_not_hide_answer():
    result = truth(THINKING)
    assert result["text"] == "Eleven pots."
    assert result["usage"] == {
        "input": 24,
        "cached_input": 12,
        "output": 41,
        "reasoning": 37,
        "total": 65,
        "alternatives": {"output": {"candidates_only": 4}},
        "raw": THINKING["usageMetadata"],
    }


@pytest.mark.parametrize(
    "usage,output,reasoning,alternatives",
    [
        (None, None, None, {}),
        ({}, None, None, {}),
        ({"candidatesTokenCount": 0}, 0, None, {}),
        ({"thoughtsTokenCount": 0}, 0, 0, {}),
        ({"candidatesTokenCount": 5}, 5, None, {}),
        ({"thoughtsTokenCount": 5}, 5, 5, {}),
        ({"candidatesTokenCount": 5, "thoughtsTokenCount": 0}, 5, 0, {}),
        (
            {"candidatesTokenCount": 0, "thoughtsTokenCount": 5},
            5,
            5,
            {"output": {"candidates_only": 0}},
        ),
        ({"candidatesTokenCount": True, "thoughtsTokenCount": "5"}, None, None, {}),
    ],
)
def test_optional_usage_does_not_invent_totals(usage, output, reasoning, alternatives):
    result = truth({"usageMetadata": usage})["usage"]
    assert result == {
        "input": None,
        "cached_input": None,
        "output": output,
        "reasoning": reasoning,
        "total": None,
        "alternatives": alternatives,
        "raw": usage,
    }


@pytest.mark.parametrize("stream", [False, True])
def test_whole_parallel_function_calls_keep_wire_order_and_optional_ids(stream):
    if stream:
        parts = TOOLS["candidates"][0]["content"]["parts"]
        ex = exchange(
            events=[event({"candidates": [{"content": {"parts": [part]}}]}) for part in parts]
            + [event({"candidates": [{"finishReason": "STOP"}]})]
        )
    else:
        ex = exchange(TOOLS)
    result = gemini.extract_truth([ex], None)
    assert (result["finish"], result["raw_finish"], result["text"]) == ("tool_calls", "STOP", None)
    assert result["tool_calls"] == [
        {
            "id": None,
            "name": "get_weather",
            "arguments_raw": '{"city":"São Paulo"}',
            "arguments_parsed_ok": True,
            "arguments": {"city": "São Paulo"},
        },
        {
            "id": "call-weather",
            "name": "get_weather",
            "arguments_raw": '{"city":"Zürich"}',
            "arguments_parsed_ok": True,
            "arguments": {"city": "Zürich"},
        },
    ]


@pytest.mark.parametrize(
    "reason,expected",
    [(None, "none"), ("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("FUTURE", "other")],
)
def test_function_call_does_not_override_non_stop_finish(reason, expected):
    payload = deepcopy(TOOLS)
    payload["candidates"][0]["finishReason"] = reason
    assert truth(payload)["finish"] == expected


def test_function_call_without_arguments_means_empty_object():
    result = truth({"candidates": [{"content": {"parts": [{"functionCall": {"name": "ping"}}]}}]})
    assert result["tool_calls"][0]["arguments"] == {}
    assert result["tool_calls"][0]["arguments_raw"] == "{}"


@pytest.mark.parametrize("arguments", [None, "{}", [], 3])
def test_function_call_rejects_non_object_arguments(arguments):
    with pytest.raises(ValueError, match="arguments are not a JSON object"):
        truth({"candidates": [{"content": {"parts": [{"functionCall": {"args": arguments}}]}}]})


@pytest.mark.parametrize("kind", [None, "text", "chunks_b64"])
@pytest.mark.parametrize("stream", [False, True])
def test_body_forms_models_and_split_multibyte_text(kind, stream):
    ex = exchange(events=STREAM if stream else None, kind=kind)
    expected = gemini.extract_truth([exchange(events=STREAM if stream else None)], None)
    assert gemini.extract_truth([ex], None) == expected
    assert gemini.returned_model(ex) == MODEL
    assert expected["stream_completed"] is (True if stream else None)
    assert expected["text"] == ("Café καλημέρα 東京 🌸" if stream else "Europa is an icy moon.")


def test_stream_uses_last_usage_snapshot_without_adding_or_merging():
    result = gemini.extract_truth([exchange(events=STREAM)], None)
    assert result["usage"]["output"] == 11
    assert result["usage"]["total"] == 20
    assert result["usage"]["alternatives"] == {"output": {"candidates_only": 8}}
    final = event({"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 9}})
    result = gemini.extract_truth([exchange(events=[*STREAM, final])], None)
    assert result["finish"] == "length" and result["stream_completed"] is True
    assert result["usage"]["output"] == 9
    assert result["usage"]["reasoning"] is result["usage"]["total"] is None


@pytest.mark.parametrize(
    "events,end,finish,completed",
    [
        (STREAM, "clean", "length", True),
        (STREAM, "abort", "length", False),
        (STREAM[:-1], "clean", "none", False),
        (STREAM[:-1], "abort", "none", False),
        ([*STREAM[:-1], STREAM[-1][:-1]], "abort", "none", False),
        ([*STREAM[:-1], "data: [DONE]\n\n"], "clean", "none", False),
        ([*STREAM, "data: [DONE]\n\n"], "clean", "length", True),
    ],
)
def test_stream_completion_needs_finish_and_clean_http(events, end, finish, completed):
    result = gemini.extract_truth([exchange(events=events, end=end)], None)
    assert (result["finish"], result["stream_completed"]) == (finish, completed)


def test_abort_mid_event_and_mid_utf8_drops_only_incomplete_event():
    raw = (STREAM[0] + 'data: {"candidates":[{"content":{"parts":[{"text":"東').encode()[:-1]
    ex = replace(
        exchange(events=[]),
        end="abort",
        body=Body("chunks_b64", None, (base64.b64encode(raw).decode(),)),
    )
    result = gemini.extract_truth([ex], None)
    assert (result["text"], result["finish"], result["stream_completed"]) == (
        "Café ",
        "none",
        False,
    )
    with pytest.raises(ValueError):
        gemini.extract_truth([replace(ex, end="clean")], None)


def test_error_frame_after_the_finish_chunk_is_not_a_completed_stream():
    result = gemini.extract_truth([exchange(events=[*STREAM, STREAM_ERROR])], None)
    assert result["finish"] == "error" and result["stream_completed"] is False


@pytest.mark.parametrize("prefix", ["", "event: error\n"])
def test_in_stream_error_preserves_prior_text_and_usage(prefix):
    result = gemini.extract_truth([exchange(events=[STREAM[0], prefix + STREAM_ERROR])], None)
    assert result["finish"] == "error" and result["stream_completed"] is False
    assert result["text"] == "Café " and result["usage"]["output"] == 2
    assert result["error"] == {
        "message": "Generation interrupted.",
        "type": "UNAVAILABLE",
        "code": "503",
        "status": None,
    }


@pytest.mark.parametrize(
    "payload,status", [(BAD_REQUEST, 400), (RATE_LIMIT, 429), (BAD_REQUEST, 200)]
)
def test_error_envelope_code_is_a_string(payload, status):
    result = truth(payload, status=status)
    error = payload["error"]
    assert result["finish"] == "error" and result["raw_finish"] is None
    assert result["error"] == {
        "message": error["message"],
        "type": error["status"],
        "code": str(error["code"]),
        "status": status if status >= 400 else None,
    }
    assert result["stream_completed"] is None


def test_http_error_without_envelope_and_error_precedence():
    assert truth({}, status=429)["error"] == {
        "message": "HTTP 429",
        "type": None,
        "code": None,
        "status": 429,
    }
    ex = replace(exchange(status=400), body=Body("text", "Invalid request.", ()))
    assert gemini.extract_truth([ex], None)["error"]["message"] == "Invalid request."
    result = truth({**TEXT, **BLOCKED, "error": {"message": "Unavailable."}})
    assert result["finish"] == "error" and result["raw_finish"] == "STOP"


@pytest.mark.parametrize("stream", [False, True])
def test_prompt_block_has_no_candidate_finish_or_text(stream):
    result = gemini.extract_truth(
        [exchange(events=[event(BLOCKED)]) if stream else exchange(BLOCKED)], None
    )
    assert result["finish"] == "content_filter"
    assert result["raw_finish"] is result["text"] is result["error"] is None
    assert result["stream_completed"] is (False if stream else None)


@pytest.mark.parametrize(
    "json_request",
    [
        None,
        {},
        {"generationConfig": {"responseMimeType": "text/plain"}},
        {"generationConfig": {"responseMimeType": "application/json"}},
    ],
)
@pytest.mark.parametrize("reason", ["STOP", "MAX_TOKENS"])
def test_structured_text_requires_requested_json_and_stop(json_request, reason):
    result = truth(
        {
            "candidates": [
                {"content": {"parts": [{"text": '{"moon":"Europa"}'}]}, "finishReason": reason}
            ]
        },
        request=json_request,
    )
    expected = (
        {"moon": "Europa"}
        if reason == "STOP"
        and json_request
        and json_request.get("generationConfig", {}).get("responseMimeType") == "application/json"
        else None
    )
    assert result["structured"] == expected


def test_structured_stream_parses_joined_text_and_invalid_json_stays_null():
    request = {"generationConfig": {"responseMimeType": "application/json"}}
    events = [
        event({"candidates": [{"content": {"parts": [{"text": piece}]}}]})
        for piece in ['{"a":1,', '"a":2}']
    ]
    events.append(event({"candidates": [{"finishReason": "STOP"}]}))
    assert gemini.extract_truth([exchange(events=events)], request)["structured"] == {"a": 2}
    assert (
        gemini.extract_truth([exchange(events=[events[0], events[-1]])], request)["structured"]
        is None
    )


def test_only_first_candidate_and_last_exchange_count():
    payload = deepcopy(TEXT)
    payload["candidates"].extend(TOOLS["candidates"])
    assert truth(payload) == truth(TEXT)
    assert gemini.extract_truth(
        [exchange(BAD_REQUEST, status=400), exchange(payload)], None
    ) == truth(TEXT)


@pytest.mark.parametrize(
    "body",
    [
        Body("json", [], ()),
        Body("text", "no-json", ()),
        Body("sse", None, ("data: []\n\n",)),
        Body("sse", None, ("data: not-json\n\n",)),
    ],
)
def test_unreadable_body_raises_value_error(body):
    with pytest.raises(ValueError):
        gemini.extract_truth([replace(exchange(), body=body)], None)


def test_no_exchanges_and_aborted_non_stream_body():
    with pytest.raises(ValueError, match="no exchanges"):
        gemini.extract_truth([], None)
    ex = replace(exchange(), end="abort", body=Body("chunks_b64", None, ("eyJj",)))
    assert gemini.extract_truth([ex], None)["finish"] == "none"
    assert gemini.returned_model(ex) is None
    assert gemini.returned_model(exchange(events=["data: not-json\n\n"])) is None


def test_request_defaults_registry_auth_and_paths():
    assert PROVIDERS["gemini"] is gemini
    assert BASE_PATHS["gemini"] == ""
    assert gemini.API_BASE == "https://generativelanguage.googleapis.com"
    assert gemini.KEY_ENV == "GEMINI_API_KEY" and gemini.DEFAULT_MODEL == MODEL
    assert gemini.auth_headers("unit-key") == {"x-goog-api-key": "unit-key"}
    request = gemini.build_request(CALL, MODEL)
    assert request.method == "POST" and request.path == PATH
    assert request.body == {
        "contents": [{"role": "user", "parts": [{"text": "Name an icy moon."}]}]
    }
    assert request.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    stream = gemini.build_request(replace(CALL, stream=True, extra={"include_usage": True}), MODEL)
    assert stream.path == STREAM_PATH + "?alt=sse" and stream.body == request.body
    assert stream.headers["Accept"] == "text/event-stream"
    assert gemini.requested_stream(stream.path, stream.body)
    assert gemini.requested_stream(STREAM_PATH, {})
    assert not gemini.requested_stream(PATH, {"stream": True})
    assert not gemini.requested_stream("/v1beta/interactions", {})


@pytest.mark.parametrize(
    "effort,level",
    [
        ("none", "MINIMAL"),
        ("minimal", "MINIMAL"),
        ("low", "LOW"),
        ("medium", "MEDIUM"),
        ("high", "HIGH"),
        ("xhigh", "HIGH"),
        ("max", "HIGH"),
    ],
)
def test_request_thinking_efforts(effort, level):
    result = gemini.build_request(replace(CALL, reasoning=Reasoning(effort)), MODEL)
    assert result.body["generationConfig"] == {"thinkingConfig": {"thinkingLevel": level}}


@pytest.mark.parametrize(
    "choice,expected",
    [
        (None, None),
        ("auto", {"mode": "AUTO"}),
        ("required", {"mode": "ANY"}),
        ("none", {"mode": "NONE"}),
        (ForcedTool("get_weather"), {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}),
    ],
)
def test_request_function_choice(choice, expected):
    result = gemini.build_request(replace(CALL, tool_choice=choice), MODEL).body
    assert result.get("toolConfig") == ({"functionCallingConfig": expected} if expected else None)


def test_request_system_turns_stops_and_plain_json_schemas_are_preserved():
    schema = {
        "type": "object",
        "properties": {"moon": {"type": "string"}},
        "required": ["moon"],
        "additionalProperties": False,
    }
    messages = (
        Message("system", "Use astronomy examples."),
        Message("user", "Name a moon."),
        Message("assistant", "Europa."),
        Message("system", "Keep it brief."),
    )
    call = replace(
        CALL,
        messages=messages,
        max_output_tokens=50,
        stop=("END",),
        output_schema=schema,
        tools=(Tool("catalog", "Catalog moons.", schema),),
    )
    saved = deepcopy(call)
    result = gemini.build_request(call, MODEL).body
    assert result["systemInstruction"] == {
        "parts": [{"text": "Use astronomy examples."}, {"text": "Keep it brief."}]
    }
    assert result["contents"] == [
        {"role": "user", "parts": [{"text": "Name a moon."}]},
        {"role": "model", "parts": [{"text": "Europa."}]},
    ]
    assert result["generationConfig"] == {
        "maxOutputTokens": 50,
        "stopSequences": ["END"],
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }
    assert result["tools"] == [
        {
            "functionDeclarations": [
                {"name": "catalog", "description": "Catalog moons.", "parametersJsonSchema": schema}
            ]
        }
    ]
    assert call == saved


def test_unknown_extra_raises_clear_error():
    with pytest.raises(ValueError, match="extra field 'temperature' has no meaning for gemini"):
        gemini.build_request(replace(CALL, extra={"temperature": 1}), MODEL)


def test_all_gemini_scenarios_build_and_thinking_override_is_effective():
    scenarios = [load_scenario(path) for path in (REPO / "suite/scenarios").glob("*/*.json")]
    included = {s.id for s in scenarios if "gemini" in s.providers}
    assert included == {
        "truncation.text",
        "truncation.text-stream",
        "truncation.text-stream-usage",
        "truncation.structured-output",
        "truncation.tool-call-args",
        "truncation.tool-call-args-stream",
        "truncation.reasoning-only",
        "tools.parallel-calls",
        "tools.parallel-calls-stream",
        "usage.stream-final-chunk",
        "usage.reasoning-tokens",
        "finish.stop-sequence",
        "finish.unknown-value",
        "stream.missing-finish",
        "stream.missing-finish-abort",
        "stream.error-midstream",
        "stream.utf8",
        "stream.utf8-split",
    }
    for scenario in scenarios:
        if scenario.id in included:
            gemini.build_request(scenario.call_for("gemini"), MODEL)
    reasoning = next(s for s in scenarios if s.id == "truncation.reasoning-only")
    assert gemini.build_request(reasoning.call_for("gemini"), "gemini-3.8-flash").body[
        "generationConfig"
    ] == {"maxOutputTokens": 32, "thinkingConfig": {"thinkingLevel": "HIGH"}}
