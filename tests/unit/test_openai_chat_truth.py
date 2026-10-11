from __future__ import annotations

import base64
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from wiretruth.formats import Body, BodyKind, End, Exchange, JsonObject, load_fixture, load_scenario
from wiretruth.providers import openai_chat, sse_events
from wiretruth.providers.openai_chat import build_request, extract_truth, returned_model
from wiretruth.replay import BASE_PATHS

REPO = Path(__file__).resolve().parents[2]
PATH = "/v1/chat/completions"
JSON_REQUEST: JsonObject = {"response_format": {"type": "json_schema", "json_schema": {}}}


def _exchange(
    kind: BodyKind,
    value: object,
    *,
    status: int = 200,
    end: End = "clean",
    content_type: str | None = None,
) -> Exchange:
    is_list = kind in ("sse", "ndjson", "chunks_b64")
    if content_type is None:
        content_type = "text/event-stream" if kind == "sse" else "application/json"
    return Exchange(
        method="POST",
        path=PATH,
        status=status,
        headers=MappingProxyType({"content-type": content_type}),
        body=Body(kind, None if is_list else value, tuple(value) if is_list else ()),  # type: ignore[arg-type]
        end=end,
    )


def _message(message: object, finish: str | None = "stop", usage: object = None) -> Exchange:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "gpt-6-luna-2026-09-22",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }
    return _exchange("json", payload)


def _chunk(delta: object = None, finish: str | None = None, **extra: object) -> str:
    chunk: dict[str, object] = {"id": "chatcmpl-1", "model": "gpt-6-luna-2026-09-22", **extra}
    if delta is not None or finish is not None:
        chunk["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


DONE = "data: [DONE]\n\n"
USAGE = {
    "prompt_tokens": 21,
    "completion_tokens": 16,
    "total_tokens": 37,
    "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0},
}


def _stream(*events: str, end: End = "clean") -> Exchange:
    return _exchange("sse", events, end=end)


def _truth(exchange: Exchange, request: JsonObject | None = None) -> JsonObject:
    return extract_truth([exchange], request)


# --- Non-stream -----------------------------------------------------------------------------------


def test_a_length_finish_with_usage() -> None:
    truth = _truth(_message({"role": "assistant", "content": "Kyiv is"}, "length", USAGE))
    assert truth == {
        "finish": "length",
        "raw_finish": "length",
        "text": "Kyiv is",
        "tool_calls": [],
        "structured": None,
        "error": None,
        "stream_completed": None,
        "usage": {
            "input": 21,
            "cached_input": 0,
            "output": 16,
            "reasoning": 0,
            "total": 37,
            "alternatives": {},
            "raw": USAGE,
        },
    }


@pytest.mark.parametrize(
    ("raw", "finish"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_calls"),
        ("function_call", "tool_calls"),
        ("content_filter", "content_filter"),
        ("something_new", "other"),
        (None, "none"),
    ],
)
def test_finish_values(raw: str | None, finish: str) -> None:
    truth = _truth(_message({"role": "assistant", "content": "x"}, raw))
    assert (truth["finish"], truth["raw_finish"]) == (finish, raw)


def test_a_refusal_wins_over_the_finish_reason() -> None:
    message = {"role": "assistant", "content": None, "refusal": "I can't help with that."}
    truth = _truth(_message(message, "stop"))
    assert (truth["finish"], truth["raw_finish"], truth["text"]) == ("refusal", "stop", None)


def test_tool_calls_with_good_and_bad_arguments() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Kyiv"}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "Lv'},
            },
        ],
    }
    truth = _truth(_message(message, "tool_calls"))
    assert truth["text"] is None
    assert truth["tool_calls"] == [
        {
            "id": "call_1",
            "name": "get_weather",
            "arguments_raw": '{"city": "Kyiv"}',
            "arguments_parsed_ok": True,
            "arguments": {"city": "Kyiv"},
        },
        {
            "id": "call_2",
            "name": "get_weather",
            "arguments_raw": '{"city": "Lv',
            "arguments_parsed_ok": False,
            "arguments": None,
        },
    ]


def test_the_legacy_function_call() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "function_call": {"name": "f", "arguments": "{}"},
    }
    truth = _truth(_message(message, "function_call"))
    assert truth["finish"] == "tool_calls"
    assert truth["tool_calls"] == [
        {
            "id": None,
            "name": "f",
            "arguments_raw": "{}",
            "arguments_parsed_ok": True,
            "arguments": {},
        }
    ]


def test_a_tool_call_without_arguments() -> None:
    message = {"role": "assistant", "tool_calls": [{"id": "c", "function": {"name": "f"}}]}
    (call,) = _truth(_message(message, "tool_calls"))["tool_calls"]  # type: ignore[misc]
    assert call == {
        "id": "c",
        "name": "f",
        "arguments_raw": None,
        "arguments_parsed_ok": False,
        "arguments": None,
    }


def test_only_the_index_0_choice_counts() -> None:
    payload = {
        "choices": [
            {"index": 1, "message": {"content": "second"}, "finish_reason": "length"},
            {"index": 0, "message": {"content": "first"}, "finish_reason": "stop"},
        ]
    }
    truth = _truth(_exchange("json", payload))
    assert (truth["text"], truth["finish"]) == ("first", "stop")


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": "x"}, {"choices": [7]}])
def test_no_usable_choice(payload: JsonObject) -> None:
    truth = _truth(_exchange("json", payload))
    assert (truth["finish"], truth["text"], truth["tool_calls"]) == ("none", None, [])


@pytest.mark.parametrize(
    ("request_body", "finish", "text", "structured"),
    [
        (JSON_REQUEST, "stop", '{"city": "Київ"}', {"city": "Київ"}),
        ({"response_format": {"type": "json_object"}}, "stop", "[1]", [1]),
        (JSON_REQUEST, "stop", '{"city": "Ки', None),  # doesn't parse
        (JSON_REQUEST, "length", "{}", None),  # parses, but the output was cut off
        ({"response_format": {"type": "text"}}, "stop", "{}", None),
        (None, "stop", "{}", None),
    ],
)
def test_structured_output(
    request_body: JsonObject | None, finish: str, text: str, structured: object
) -> None:
    truth = _truth(_message({"content": text}, finish), request_body)
    assert truth["structured"] == structured


def test_total_is_computed_when_missing() -> None:
    usage = {"prompt_tokens": 3, "completion_tokens": 4}
    truth = _truth(_message({"content": "x"}, "stop", usage))
    assert truth["usage"] == {
        "input": 3,
        "cached_input": None,
        "output": 4,
        "reasoning": None,
        "total": 7,
        "alternatives": {},
        "raw": usage,
    }


def test_no_usage_object() -> None:
    usage = _truth(_message({"content": "x"}))["usage"]
    assert usage == {
        "input": None,
        "cached_input": None,
        "output": None,
        "reasoning": None,
        "total": None,
        "alternatives": {},
        "raw": None,
    }


# --- Errors ---------------------------------------------------------------------------------------


def test_an_http_error() -> None:
    body = {
        "error": {
            "message": "Unsupported parameter: 'max_tokens' is not supported with this model.",
            "type": "invalid_request_error",
            "param": "max_tokens",
            "code": "unsupported_parameter",
        }
    }
    truth = _truth(_exchange("json", body, status=400))
    assert truth["finish"] == "error"
    assert truth["error"] == {
        "status": 400,
        "type": "invalid_request_error",
        "code": "unsupported_parameter",
        "message": "Unsupported parameter: 'max_tokens' is not supported with this model.",
    }
    assert (truth["text"], truth["raw_finish"], truth["stream_completed"]) == (None, None, None)


def test_an_error_body_that_isnt_json() -> None:
    exchange = _exchange("text", "upstream connect error", status=503, content_type="text/plain")
    error = _truth(exchange)["error"]
    assert error == {"status": 503, "type": None, "code": None, "message": "upstream connect error"}


def test_an_error_status_with_an_empty_json_object() -> None:
    error = _truth(_exchange("json", {}, status=500))["error"]
    assert error == {"status": 500, "type": None, "code": None, "message": "HTTP 500"}


def test_an_error_object_that_is_a_string_and_a_numeric_code() -> None:
    error = _truth(_exchange("json", {"error": "overloaded"}, status=529))["error"]
    assert error == {"status": 529, "type": None, "code": None, "message": "overloaded"}
    body = {"error": {"message": "m", "code": 1234}}
    assert _truth(_exchange("json", body, status=400))["error"] == {
        "status": 400,
        "type": None,
        "code": "1234",
        "message": "m",
    }


def test_an_error_while_streaming_is_sent_as_json() -> None:
    body = {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit"}}
    truth = _truth(_exchange("json", body, status=429))
    assert truth["error"]["status"] == 429  # type: ignore[index]
    assert truth["stream_completed"] is None


def test_an_error_in_band_in_a_stream() -> None:
    error = {"error": {"message": "The server had an error", "type": "server_error", "code": None}}
    exchange = _stream(
        _chunk({"role": "assistant", "content": "Kyiv"}), f"data: {json.dumps(error)}\n\n", DONE
    )
    truth = _truth(exchange)
    assert truth["finish"] == "error"
    assert truth["text"] == "Kyiv"
    assert truth["error"] == {
        "status": None,
        "type": "server_error",
        "code": None,
        "message": "The server had an error",
    }
    assert truth["stream_completed"] is True


def test_an_error_status_with_an_event_stream_body() -> None:
    exchange = _exchange("sse", ("data: nope\n\n",), status=500)
    error = _truth(exchange)["error"]
    assert error == {"status": 500, "type": None, "code": None, "message": "data: nope\n\n"}


# --- Stream ---------------------------------------------------------------------------------------


def test_a_stream_cut_off_by_the_limit() -> None:
    exchange = _stream(
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "Kyiv, "}),
        _chunk({"content": "Київ"}),
        _chunk({}, "length"),
        DONE,
    )
    truth = _truth(exchange)
    assert (truth["finish"], truth["raw_finish"], truth["text"]) == (
        "length",
        "length",
        "Kyiv, Київ",
    )
    assert truth["stream_completed"] is True
    assert truth["usage"]["raw"] is None  # type: ignore[index]


def test_a_stream_with_a_usage_chunk() -> None:
    exchange = _stream(
        _chunk({"role": "assistant", "content": "Kyiv"}, usage=None),
        _chunk({}, "length", usage=None),
        _chunk(choices=[], usage=USAGE),
        DONE,
    )
    truth = _truth(exchange)
    assert truth["finish"] == "length"
    assert truth["usage"]["output"] == 16  # type: ignore[index]
    assert truth["usage"]["raw"] == USAGE  # type: ignore[index]


def test_streamed_tool_calls_are_put_together_by_index() -> None:
    def call(index: int, **fields: object) -> dict[str, object]:
        return {"index": index, **fields}

    exchange = _stream(
        _chunk({"role": "assistant", "content": None}),
        _chunk(
            {
                "tool_calls": [
                    call(0, id="call_a", type="function", function={"name": "f", "arguments": ""})
                ]
            }
        ),
        _chunk({"tool_calls": [call(0, function={"arguments": '{"city": '})]}),
        _chunk(
            {
                "tool_calls": [
                    call(1, id="call_b", type="function", function={"name": "f", "arguments": ""})
                ]
            }
        ),
        _chunk({"tool_calls": [call(0, function={"arguments": '"Kyiv"}'})]}),
        _chunk({"tool_calls": [call(1, function={"arguments": '{"city": "Lviv"}'})]}),
        _chunk({}, "tool_calls"),
        DONE,
    )
    truth = _truth(exchange)
    assert truth["finish"] == "tool_calls"
    assert truth["text"] is None
    assert truth["tool_calls"] == [
        {
            "id": "call_a",
            "name": "f",
            "arguments_raw": '{"city": "Kyiv"}',
            "arguments_parsed_ok": True,
            "arguments": {"city": "Kyiv"},
        },
        {
            "id": "call_b",
            "name": "f",
            "arguments_raw": '{"city": "Lviv"}',
            "arguments_parsed_ok": True,
            "arguments": {"city": "Lviv"},
        },
    ]


def test_a_streamed_tool_call_without_an_index_and_a_legacy_function_call() -> None:
    exchange = _stream(
        _chunk({"tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{}"}}]}),
        _chunk({"function_call": {"name": "g", "arguments": "{"}}),
        _chunk({"function_call": {"arguments": "}"}}),
        _chunk({}, "tool_calls"),
        DONE,
    )
    calls = _truth(exchange)["tool_calls"]
    assert [(call["id"], call["name"], call["arguments_raw"]) for call in calls] == [  # type: ignore[index, union-attr]
        (None, "g", "{}"),
        ("c", "f", "{}"),
    ]


def test_a_streamed_refusal() -> None:
    exchange = _stream(
        _chunk({"role": "assistant", "content": None, "refusal": ""}),
        _chunk({"refusal": "I can't"}),
        _chunk({}, "stop"),
        DONE,
    )
    truth = _truth(exchange)
    assert (truth["finish"], truth["raw_finish"], truth["text"]) == ("refusal", "stop", None)


def test_a_stream_without_done_or_cut_off() -> None:
    no_done = _truth(_stream(_chunk({"content": "a"}), _chunk({}, "stop")))
    assert (no_done["finish"], no_done["stream_completed"]) == ("stop", False)
    aborted = _truth(_stream(_chunk({"content": "a"}), DONE, end="abort"))
    assert aborted["stream_completed"] is False
    no_finish = _truth(_stream(_chunk({"content": "a"}), DONE))
    assert (no_finish["finish"], no_finish["stream_completed"]) == ("none", True)


def test_events_after_done_are_ignored() -> None:
    truth = _truth(_stream(_chunk({"content": "a"}), DONE, _chunk({"content": "b"}, "stop")))
    assert (truth["text"], truth["finish"]) == ("a", "none")


def test_a_later_null_finish_reason_keeps_the_earlier_one() -> None:
    truth = _truth(_stream(_chunk({}, "length"), _chunk(choices=[], usage=USAGE), DONE))
    assert truth["finish"] == "length"


def test_other_choices_and_choiceless_chunks_in_a_stream() -> None:
    other = 'data: {"choices": [{"index": 1, "delta": {"content": "no"}}]}\n\n'
    truth = _truth(_stream(other, _chunk(), _chunk({"content": "yes"}, "stop"), DONE))
    assert truth["text"] == "yes"


def test_a_stream_stored_as_chunks_split_inside_a_character() -> None:
    raw = (_chunk({"content": "Привіт"}) + _chunk({}, "stop") + DONE).encode()
    cut = raw.index("и".encode()) + 1
    parts = [base64.b64encode(piece).decode() for piece in (raw[:cut], raw[cut:])]
    exchange = _exchange("chunks_b64", parts, content_type="text/event-stream; charset=utf-8")
    truth = _truth(exchange)
    assert (truth["text"], truth["finish"], truth["stream_completed"]) == ("Привіт", "stop", True)


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (("data: {not json\n\n",), "event 0: the data is not JSON"),
        (("data: [1]\n\n",), "event 0: the data is not a JSON object"),
    ],
)
def test_a_stream_the_extractor_cant_read(events: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _truth(_stream(*events))


def test_bodies_the_extractor_cant_read() -> None:
    with pytest.raises(ValueError, match="no exchanges"):
        extract_truth([], None)
    with pytest.raises(ValueError, match="not a JSON object"):
        _truth(_exchange("json", [1]))
    with pytest.raises(ValueError, match="not a JSON object"):
        _truth(_exchange("text", "plain", content_type="text/plain"))
    not_utf8 = [base64.b64encode(b"\xff").decode()]
    with pytest.raises(ValueError):
        _truth(_exchange("chunks_b64", not_utf8, content_type="text/event-stream"))


def test_the_last_exchange_is_the_one_that_counts() -> None:
    first = _exchange("json", {"error": {"message": "slow down"}}, status=429)
    second = _message({"content": "ok"}, "stop")
    truth = extract_truth([first, second], None)
    assert (truth["finish"], truth["error"]) == ("stop", None)


# --- The event-stream parser ----------------------------------------------------------------------


def test_sse_parsing_follows_the_html_standard() -> None:
    text = (
        "﻿: a comment\n"
        "event: message\n"
        "data: one\n"
        "data:two\n"
        "id: 7\n"
        "\n"
        "event: lonely\n"
        "\r\n"
        "data: three\r"
        "\r"
        "data:\n"
        "\n"
        "data: never dispatched"
    )
    events = [(event.type, event.data) for event in sse_events(text)]
    assert events == [("message", "one\ntwo"), (None, "three"), (None, "")]


# --- Request --------------------------------------------------------------------------------------


def _call(**fields: object) -> object:
    from wiretruth.formats import _build_call  # the loader's own call builder

    base: JsonObject = {"stream": False, "messages": [{"role": "user", "content": "Привіт"}]}
    return _build_call({**base, **fields})  # type: ignore[dict-item]


def test_the_request_for_truncation_text() -> None:
    scenario = load_scenario(REPO / "suite" / "scenarios" / "truncation" / "text.json")
    request = build_request(scenario.call_for("openai-chat"), "gpt-6-luna")
    assert (request.method, request.path) == ("POST", "/chat/completions")
    assert request.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert request.body == {
        "model": "gpt-6-luna",
        "messages": [
            {
                "role": "user",
                "content": "Write a 200-word paragraph about the history of Kyiv, in English.",
            }
        ],
        "max_completion_tokens": 16,
        "reasoning_effort": "none",
    }


def test_every_call_field_maps_to_the_chat_request() -> None:
    tool = {"name": "get_weather", "description": "d", "parameters": {"type": "object"}}
    schema = {"type": "object"}
    call = _call(
        stream=True,
        stop=["\n\n"],
        tools=[tool],
        tool_choice={"name": "get_weather"},
        output_schema=schema,
        reasoning={"effort": "medium"},
        extra={"include_usage": True},
        max_output_tokens=50,
    )
    request = build_request(call, "m")  # type: ignore[arg-type]
    assert request.headers["Accept"] == "text/event-stream"
    assert request.body == {
        "model": "m",
        "messages": [{"role": "user", "content": "Привіт"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 50,
        "stop": ["\n\n"],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "d",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": schema, "strict": True},
        },
        "reasoning_effort": "medium",
    }


@pytest.mark.parametrize("choice", ["auto", "required", "none"])
def test_tool_choice_strings_pass_through(choice: str) -> None:
    request = build_request(_call(tool_choice=choice), "m")  # type: ignore[arg-type]
    assert request.body["tool_choice"] == choice


def test_include_usage_needs_a_stream_and_unknown_extras_fail() -> None:
    request = build_request(_call(extra={"include_usage": True}), "m")  # type: ignore[arg-type]
    assert "stream_options" not in request.body
    with pytest.raises(ValueError, match="extra field 'num_ctx' has no meaning for openai-chat"):
        build_request(_call(extra={"num_ctx": 4096}), "m")  # type: ignore[arg-type]


def test_auth_header() -> None:
    assert openai_chat.auth_headers("k") == {"Authorization": "Bearer k"}


# --- Returned model -------------------------------------------------------------------------------


def test_returned_model_from_a_message_stored_as_chunks_or_text() -> None:
    payload = {"model": "gpt-6-luna", "choices": []}
    chunks = [base64.b64encode(json.dumps(payload).encode()).decode()]
    assert returned_model(_exchange("chunks_b64", chunks)) == "gpt-6-luna"
    assert returned_model(_exchange("text", json.dumps(payload))) == "gpt-6-luna"


def test_the_api_base_ends_where_the_replay_server_serves_the_api() -> None:
    assert "https://api.openai.com" + BASE_PATHS["openai-chat"] == openai_chat.API_BASE


def test_returned_model() -> None:
    assert returned_model(_message({"content": "x"})) == "gpt-6-luna-2026-09-22"
    stream = _stream("data: {bad\n\n", 'data: {"choices": []}\n\n', _chunk({"content": "a"}), DONE)
    assert returned_model(stream) == "gpt-6-luna-2026-09-22"
    assert returned_model(_stream(DONE)) is None
    assert returned_model(_exchange("text", "oops", content_type="text/plain")) is None
    not_utf8 = [base64.b64encode(b"\xff").decode()]
    assert (
        returned_model(_exchange("chunks_b64", not_utf8, content_type="text/event-stream")) is None
    )


def test_an_error_body_that_isnt_text_or_an_object() -> None:
    not_utf8 = [base64.b64encode(b"\xff\xfe").decode()]
    exchange = _exchange("chunks_b64", not_utf8, status=502, content_type="text/plain")
    assert _truth(exchange)["error"] == {
        "status": 502,
        "type": None,
        "code": None,
        "message": "HTTP 502",
    }
    assert _truth(_exchange("json", [1], status=400))["error"]["message"] == "HTTP 400"  # type: ignore[index]
    empty = _exchange("text", "", status=504, content_type="text/plain")
    assert _truth(empty)["error"]["message"] == "HTTP 504"  # type: ignore[index]


def test_a_message_cut_off_before_it_parses_delivered_nothing() -> None:
    parts = [base64.b64encode(b'{"choices": [{"index": 0, "mess').decode()]
    exchange = _exchange("chunks_b64", parts, end="abort")
    truth = _truth(exchange)
    assert (truth["finish"], truth["text"], truth["error"]) == ("none", None, None)
    assert truth["stream_completed"] is None


# --- Recorded responses ---------------------------------------------------------------------------
# Real gpt-6-luna responses, recorded on 2026-10-07 with `wiretruth record` from scenarios kept
# outside the suite. Each stored truth was checked against the wire by hand; the asserts below
# repeat the facts read off the wire, so the test isn't the extractor checking itself.

RECORDED = REPO / "tests" / "samples" / "recorded" / "openai-chat"
KYIV = "Kyiv, the capital of Ukraine, is one of Europe\u2019s"  # a typographic apostrophe


def _recorded(name: str) -> tuple[JsonObject, JsonObject]:
    fixture = load_fixture(RECORDED / f"{name}.json")
    truth = extract_truth(fixture.exchanges, fixture.provenance.request)
    stored = json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))["truth"]
    assert truth == stored
    assert returned_model(fixture.exchanges[0]) == fixture.provenance.returned_model
    return truth, stored["usage"]


def test_recorded_truncation() -> None:
    truth, usage = _recorded("truncation.text")
    assert (truth["finish"], truth["text"], truth["stream_completed"]) == ("length", KYIV, None)
    assert (usage["input"], usage["output"], usage["total"]) == (21, 16, 37)


def test_recorded_stream_with_and_without_usage() -> None:
    truth, usage = _recorded("truncation.text-stream-usage")
    assert (truth["finish"], truth["text"], truth["stream_completed"]) == ("length", KYIV, True)
    assert (usage["input"], usage["output"], usage["reasoning"]) == (21, 16, 0)
    assert "cache_write_tokens" in usage["raw"]["prompt_tokens_details"]  # kept in raw only
    truth, usage = _recorded("truncation.text-stream")
    assert truth["text"] == "Kyiv, the capital of Ukraine, has a history spanning more"
    assert (truth["finish"], usage["raw"], usage["output"]) == ("length", None, None)


def test_recorded_tool_calls() -> None:
    for name in ("tools.parallel-calls", "tools.parallel-calls-stream"):
        truth, usage = _recorded(name)
        assert (truth["finish"], truth["text"]) == ("tool_calls", None)
        calls = [(call["name"], call["arguments"]) for call in truth["tool_calls"]]
        assert calls == [("get_weather", {"city": "Kyiv"}), ("get_weather", {"city": "Lviv"})]
        assert all(call["id"].startswith("call_") for call in truth["tool_calls"])
        assert (usage["input"], usage["output"]) == (142, 49)


def test_recorded_structured_output() -> None:
    truth, _ = _recorded("truncation.structured-output")
    assert truth["finish"] == "length"
    assert truth["text"] == '{"cities":[{"name":"Київ","oblast":"місто зі спеціальним'
    assert truth["structured"] is None
    truth, _ = _recorded("finish.stop-structured")
    assert truth["finish"] == "stop"
    assert truth["structured"] == {"name": "Київ", "oblast": "Київська область"}


def test_recorded_error() -> None:
    truth, usage = _recorded("request.tools-with-reasoning-chat")
    assert truth["finish"] == "error"
    assert truth["error"] == {
        "status": 400,
        "type": "invalid_request_error",
        "code": None,
        "message": "Function tools with reasoning_effort are not supported for gpt-6-luna in "
        "/v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort "
        "to 'none'.",
    }
    assert usage["raw"] is None


@pytest.mark.parametrize("first", ["km", "mi"])
def test_streamed_duplicate_tool_arguments_extract_and_grade(first) -> None:
    from dataclasses import replace

    from wiretruth.formats import Requirement, _build_truth, load_observation
    from wiretruth.grading import ShimRun, grade_cell
    from wiretruth.replay import LoggedRequest

    pieces = ['{"unit":"' + first + '",', '"unit":"km"}']
    events = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-1",
                        "function": {"name": "convert_units", "arguments": pieces[0]},
                    }
                ]
            }
        ),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": pieces[1]}}]}),
        _chunk(finish="tool_calls"),
        DONE,
    ]
    truth = _build_truth(_truth(_stream(*events)))
    (tool,) = truth.tool_calls
    assert tool.arguments_raw == "".join(pieces)
    assert tool.arguments_parsed_ok is True
    assert tool.arguments == {"unit": "km"}
    obs = load_observation(REPO / "tests/samples/valid/observation/returned.json")
    obs = replace(obs, tool_calls=(replace(tool, arguments=json.loads(tool.arguments_raw)),))
    checks = ("no_complete_tool_call_from_partial_args", "tool_calls_match_truth")
    scenario = replace(
        load_scenario(REPO / "suite/scenarios/truncation/text.json"),
        requirements=tuple(
            Requirement(f"check-{i}", "MUST", check, "Calls match the wire.", {}, None)
            for i, check in enumerate(checks)
        ),
    )
    log = [LoggedRequest("POST", PATH, {}, {}, {}, None, "", 0, True)]
    grade = grade_cell(
        ShimRun(False, None, obs, None, None, log),
        scenario=scenario,
        provider="openai-chat",
        truth=truth,
        unsupported=(),
    )
    assert grade.grade == "PASS"
    assert [item.status for item in grade.requirements] == ["SATISFIED", "SATISFIED"]


@pytest.mark.parametrize("stream", [False, True])
def test_structured_text_duplicate_keys_use_last_value(stream) -> None:
    raw = '{"unit":"mi","unit":"km"}'
    exchange = (
        _stream(_chunk({"content": raw}), _chunk(finish="stop"), DONE)
        if stream
        else _message({"content": raw})
    )
    assert _truth(exchange, JSON_REQUEST)["structured"] == {"unit": "km"}


ALSO_RESPONSES = {
    "truncation.tool-call-args",
    "truncation.tool-call-args-stream",
    "truncation.reasoning-only",
    "tools.parallel-calls",
    "tools.parallel-calls-stream",
}


@pytest.mark.parametrize(
    "id,stream,limit,effort,usage,tool,choice",
    [
        ("truncation.tool-call-args", False, 20, "none", False, "create_event", "required"),
        ("truncation.tool-call-args-stream", True, 20, "none", False, "create_event", "required"),
        ("truncation.text-stream-usage", True, 16, "none", True, None, None),
        ("truncation.reasoning-only", False, 64, "medium", False, None, None),
        ("usage.stream-final-chunk", True, 64, "none", True, None, None),
        ("tools.parallel-calls", False, None, "none", False, "get_weather", "auto"),
        ("tools.parallel-calls-stream", True, None, "none", False, "get_weather", "auto"),
        ("request.max-tokens-field", False, 16, "none", False, None, None),
        ("request.tools-with-reasoning-chat", False, None, None, False, "convert_units", "auto"),
    ],
)
def test_new_chat_scenario_requests(id, stream, limit, effort, usage, tool, choice) -> None:
    family, name = id.split(".")
    scenario = load_scenario(REPO / "suite/scenarios" / family / f"{name}.json")
    also = ("openai-responses",) if id in ALSO_RESPONSES else ()
    assert scenario.providers == ("openai-chat", *also)
    body = build_request(scenario.call, "gpt-6-luna").body
    assert body["model"] == "gpt-6-luna"
    assert body.get("stream", False) is stream
    assert body.get("max_completion_tokens") == limit
    assert ("max_completion_tokens" in body) is (limit is not None)
    assert "max_tokens" not in body and "temperature" not in body
    assert body.get("reasoning_effort") == effort
    assert ("reasoning_effort" in body) is (effort is not None)
    assert body.get("stream_options") == ({"include_usage": True} if usage else None)
    assert body.get("tool_choice") == choice
    if tool is None:
        assert "tools" not in body
    else:
        (function,) = body["tools"]
        assert function["type"] == "function"
        assert function["function"] == {
            "name": tool,
            "description": scenario.call.tools[0].description,
            "parameters": scenario.call.tools[0].parameters,
        }
        parameters = function["function"]["parameters"]
        assert parameters["required"] == list(parameters["properties"])
        assert parameters["additionalProperties"] is False
