from __future__ import annotations

import base64
import json
from pathlib import Path
from types import MappingProxyType

import pytest

from wiretruth.formats import Body, BodyKind, Call, End, Exchange, JsonObject, load_scenario
from wiretruth.providers import PROVIDERS, openai_responses
from wiretruth.providers.openai_responses import build_request, extract_truth, returned_model
from wiretruth.replay import BASE_PATHS

REPO = Path(__file__).resolve().parents[2]
PATH = "/v1/responses"
MODEL = "gpt-6-luna-2026-09-22"
JSON_REQUEST: JsonObject = {"text": {"format": {"type": "json_schema", "name": "output"}}}
USAGE = {
    "input_tokens": 21,
    "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 0},
    "output_tokens": 16,
    "output_tokens_details": {"reasoning_tokens": 4},
    "total_tokens": 37,
}


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


def _response(output: object = None, status: str | None = "completed", **fields: object) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1791676800,
        "model": MODEL,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "output": [] if output is None else output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": None,
        **fields,
    }


def _text(text: str) -> dict:
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def _message(*parts: object, status: str = "completed") -> dict:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": status,
        "content": list(parts),
    }


def _call_item(index: int = 0, arguments: str = "{}", status: str = "completed") -> dict:
    return {
        "type": "function_call",
        "id": f"fc_{index}",
        "call_id": f"call_{index}",
        "name": "get_weather",
        "arguments": arguments,
        "status": status,
    }


def _event(kind: str, **fields: object) -> dict:
    return {"type": kind, **fields}


def _delta(text: str) -> dict:
    return _event(
        "response.output_text.delta",
        item_id="msg_1",
        output_index=0,
        content_index=0,
        delta=text,
        logprobs=[],
    )


def _arguments(index: int, delta: str) -> dict:
    return _event(
        "response.function_call_arguments.delta",
        item_id=f"fc_{index}",
        output_index=index,
        delta=delta,
    )


def _added(index: int = 0) -> dict:
    return _event(
        "response.output_item.added",
        output_index=index,
        item=_call_item(index, "", "in_progress"),
    )


def _stream(*events: dict, end: End = "clean") -> Exchange:
    return _exchange(
        "sse",
        [
            f"event: {event['type']}\ndata: "
            + json.dumps({**event, "sequence_number": i}, ensure_ascii=False)
            + "\n\n"
            for i, event in enumerate(events)
        ],
        end=end,
    )


def _truth(exchange: Exchange, request: JsonObject | None = None) -> JsonObject:
    return extract_truth([exchange], request)


# --- Non-stream -----------------------------------------------------------------------------------


def test_a_length_finish_with_usage() -> None:
    truth = _truth(
        _exchange(
            "json",
            _response(
                [_message(_text("Octopuses change"), status="incomplete")],
                "incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                usage=USAGE,
            ),
        )
    )
    assert truth == {
        "finish": "length",
        "raw_finish": "max_output_tokens",
        "text": "Octopuses change",
        "tool_calls": [],
        "structured": None,
        "error": None,
        "stream_completed": None,
        "usage": {
            "input": 21,
            "cached_input": 8,
            "output": 16,
            "reasoning": 4,
            "total": 37,
            "alternatives": {},
            "raw": USAGE,
        },
    }


@pytest.mark.parametrize(
    ("status", "reason", "finish"),
    [
        ("completed", None, "stop"),
        ("incomplete", "max_output_tokens", "length"),
        ("incomplete", "content_filter", "content_filter"),
        ("incomplete", "max_messages", "other"),
        ("incomplete", "steered", "other"),
        ("incomplete", "new_reason", "other"),
        ("incomplete", None, "other"),
        ("failed", None, "error"),
        ("in_progress", None, "other"),
        ("cancelled", None, "other"),
        ("queued", None, "other"),
        ("new_status", None, "other"),
        (None, None, "none"),
        (None, "max_output_tokens", "none"),
        ("completed", "new_reason", "stop"),
    ],
)
def test_finish_values(status, reason, finish) -> None:
    truth = _truth(
        _exchange(
            "json",
            _response(
                status=status,
                incomplete_details={"reason": reason} if reason is not None else None,
            ),
        )
    )
    assert (truth["finish"], truth["raw_finish"]) == (finish, reason or status)


def test_refusal_and_error_precedence() -> None:
    payload = _response([_message({"type": "refusal", "refusal": ""})])
    truth = _truth(_exchange("json", payload))
    assert (truth["finish"], truth["text"]) == ("refusal", None)
    payload["error"] = {"code": "server_error", "message": "failed"}
    assert _truth(_exchange("json", payload))["finish"] == "error"


def test_text_parts_join_and_other_items_do_not_contribute() -> None:
    payload = _response(
        [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "hidden"}],
            },
            _message(_text("São "), _text("Paulo")),
            {"type": "future_item", "text": "skip"},
            _message(_text(" / Zürich")),
        ]
    )
    assert _truth(_exchange("json", payload))["text"] == "São Paulo / Zürich"


def test_reasoning_only_output() -> None:
    usage = {**USAGE, "output_tokens_details": {"reasoning_tokens": 16}}
    truth = _truth(
        _exchange(
            "json",
            _response(
                [{"id": "rs_1", "type": "reasoning", "summary": [], "status": "incomplete"}],
                "incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                usage=usage,
            ),
        )
    )
    assert (truth["finish"], truth["text"], truth["tool_calls"]) == ("length", None, [])
    assert truth["usage"]["reasoning"] == 16


@pytest.mark.parametrize("output", [None, "bad", [7], [{"type": "message", "content": [None, {}]}]])
def test_missing_or_unusable_output(output) -> None:
    truth = _truth(_exchange("json", {"output": output}))
    assert (truth["finish"], truth["text"], truth["tool_calls"]) == ("none", None, [])


def test_tool_calls_use_call_id_and_parse_complete_arguments() -> None:
    payload = _response(
        [
            _call_item(0, '{"city":"São Paulo"}'),
            _call_item(1, '{"city":"Zürich"}'),
        ]
    )
    truth = _truth(_exchange("json", payload))
    assert (truth["finish"], truth["raw_finish"], truth["text"]) == (
        "tool_calls",
        "completed",
        None,
    )
    assert truth["tool_calls"] == [
        {
            "id": f"call_{i}",
            "name": "get_weather",
            "arguments_raw": raw,
            "arguments_parsed_ok": True,
            "arguments": {"city": city},
        }
        for i, raw, city in [
            (0, '{"city":"São Paulo"}', "São Paulo"),
            (1, '{"city":"Zürich"}', "Zürich"),
        ]
    ]


@pytest.mark.parametrize(
    ("arguments", "parsed", "ok"),
    [
        ('{"city":"Zü', None, False),
        ('{"city":"old","city":"Zürich"}', {"city": "Zürich"}, True),
        ('{"value":1e400}', None, False),
        ('{"value":"\\ud800"}', None, False),
    ],
)
def test_tool_arguments(arguments, parsed, ok) -> None:
    truth = _truth(
        _exchange(
            "json",
            _response(
                [_call_item(arguments=arguments)],
                "incomplete",
                incomplete_details={"reason": "max_output_tokens"},
            ),
        )
    )
    assert truth["finish"] == "length"
    assert truth["tool_calls"][0] == {
        "id": "call_0",
        "name": "get_weather",
        "arguments_raw": arguments,
        "arguments_parsed_ok": ok,
        "arguments": parsed,
    }


def test_a_tool_call_without_arguments() -> None:
    truth = _truth(_exchange("json", _response([{"type": "function_call"}])))
    assert truth["tool_calls"] == [
        {
            "id": None,
            "name": "",
            "arguments_raw": None,
            "arguments_parsed_ok": False,
            "arguments": None,
        }
    ]


@pytest.mark.parametrize(
    ("request_body", "status", "text", "structured"),
    [
        (JSON_REQUEST, "completed", '{"island":"Κρήτη"}', {"island": "Κρήτη"}),
        ({"text": {"format": {"type": "json_object"}}}, "completed", "[1]", [1]),
        (JSON_REQUEST, "completed", '{"x":0,"x":1}', {"x": 1}),
        (JSON_REQUEST, "completed", '{"island":"Κρή', None),
        (JSON_REQUEST, "incomplete", "{}", None),
        ({"text": {"format": {"type": "text"}}}, "completed", "{}", None),
        (None, "completed", "{}", None),
    ],
)
def test_structured_output(request_body, status, text, structured) -> None:
    truth = _truth(
        _exchange(
            "json",
            _response(
                [_message(_text(text))],
                status,
                incomplete_details={"reason": "max_output_tokens"}
                if status == "incomplete"
                else None,
            ),
        ),
        request_body,
    )
    assert truth["structured"] == structured


@pytest.mark.parametrize(
    ("usage", "total"),
    [
        ({"input_tokens": 3, "output_tokens": 4}, 7),
        ({"input_tokens": 3}, None),
        ({"output_tokens": 4}, None),
        (None, None),
    ],
)
def test_total_fallback_and_missing_usage(usage, total) -> None:
    truth = _truth(_exchange("json", _response(usage=usage)))
    assert truth["usage"] == {
        "input": (usage or {}).get("input_tokens"),
        "output": (usage or {}).get("output_tokens"),
        "cached_input": None,
        "reasoning": None,
        "total": total,
        "alternatives": {},
        "raw": usage,
    }


# --- Stream ---------------------------------------------------------------------------------------


def test_streamed_text_uses_deltas_and_final_metadata() -> None:
    initial = _response(status="in_progress", usage={"input_tokens": 999})
    final = _response(
        [_message(_text("not the deltas"), status="incomplete")],
        "incomplete",
        incomplete_details={"reason": "max_output_tokens"},
        usage=USAGE,
    )
    truth = _truth(
        _stream(
            _event("response.created", response=initial),
            _event("response.in_progress", response=initial),
            _event(
                "response.output_item.added", output_index=0, item=_message(status="in_progress")
            ),
            _event(
                "response.content_part.added",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                part=_text(""),
            ),
            _delta("Octopuses "),
            _delta("change"),
            _event(
                "response.output_text.done",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                text="not the deltas",
                logprobs=[],
            ),
            _event(
                "response.content_part.done",
                output_index=0,
                item_id="msg_1",
                content_index=0,
                part=_text("not the deltas"),
            ),
            _event("response.output_item.done", output_index=0, item=final["output"][0]),
            _event("response.incomplete", response=final),
        )
    )
    assert (truth["finish"], truth["text"], truth["stream_completed"]) == (
        "length",
        "Octopuses change",
        True,
    )
    assert truth["usage"]["raw"] == USAGE


def test_streamed_parallel_calls_use_output_index() -> None:
    final = _response([_call_item(0, '{"city":"ignored"}'), _call_item(1, "{}")])
    truth = _truth(
        _stream(
            _added(1),
            _added(0),
            _arguments(0, '{"city":"São '),
            _arguments(1, '{"city":"Zü'),
            _arguments(0, 'Paulo"}'),
            _arguments(1, 'rich"}'),
            _event(
                "response.function_call_arguments.done",
                output_index=0,
                item_id="fc_0",
                arguments="{}",
            ),
            _event("response.output_item.done", output_index=0, item=_call_item()),
            _event("response.completed", response=final),
        )
    )
    assert truth["finish"] == "tool_calls"
    assert [(c["id"], c["name"], c["arguments"]) for c in truth["tool_calls"]] == [
        ("call_0", "get_weather", {"city": "São Paulo"}),
        ("call_1", "get_weather", {"city": "Zürich"}),
    ]


@pytest.mark.parametrize(
    ("arguments", "parsed", "status"),
    [
        ('{"city":"Zü', None, "incomplete"),
        ('{"city":"old","city":"Zürich"}', {"city": "Zürich"}, "completed"),
    ],
)
def test_streamed_single_tool_call(arguments, parsed, status) -> None:
    truth = _truth(
        _stream(
            _added(),
            _arguments(0, arguments),
            _event(
                f"response.{status}",
                response=_response(
                    [_call_item(arguments=arguments)],
                    status,
                    incomplete_details={"reason": "max_output_tokens"}
                    if status == "incomplete"
                    else None,
                ),
            ),
        )
    )
    assert truth["finish"] == ("length" if status == "incomplete" else "tool_calls")
    assert truth["tool_calls"][0]["arguments"] == parsed
    assert truth["tool_calls"][0]["arguments_raw"] == arguments
    assert truth["tool_calls"][0]["arguments_parsed_ok"] == (parsed is not None)


def test_added_arguments_and_final_output_do_not_replace_deltas() -> None:
    added = _added()
    added["item"]["arguments"] = '{"city":"ignored"}'
    truth = _truth(_stream(added, _event("response.completed", response=_response([_call_item()]))))
    assert truth["tool_calls"][0]["arguments_raw"] is None
    assert (
        _truth(_stream(_event("response.completed", response=_response([_call_item()]))))[
            "tool_calls"
        ]
        == []
    )


def test_malformed_stream_items_and_missing_indexes() -> None:
    added = _added()
    del added["output_index"]
    args = _arguments(0, "{}")
    del args["output_index"]
    truth = _truth(
        _stream(
            _event("response.output_item.added", output_index=1, item=None),
            added,
            args,
            _event("response.completed", response=None),
        )
    )
    assert truth["tool_calls"][0]["arguments"] == {}
    assert truth["finish"] == "none"


@pytest.mark.parametrize("source", ["delta", "part", "item", "final"])
def test_streamed_refusal(source) -> None:
    part = {"type": "refusal", "refusal": "I cannot help."}
    event = {
        "delta": _event(
            "response.refusal.delta",
            output_index=0,
            content_index=0,
            item_id="msg_1",
            delta="I cannot help.",
        ),
        "part": _event(
            "response.content_part.added",
            output_index=0,
            content_index=0,
            item_id="msg_1",
            part=part,
        ),
        "item": _event("response.output_item.added", output_index=0, item=_message(part)),
        "final": _event(
            "response.refusal.done",
            output_index=0,
            content_index=0,
            item_id="msg_1",
            refusal="I cannot help.",
        ),
    }[source]
    final = _response([_message(part)] if source == "final" else [])
    truth = _truth(_stream(event, _event("response.completed", response=final)))
    assert (truth["finish"], truth["raw_finish"], truth["text"]) == ("refusal", "completed", None)


def test_streamed_refusal_without_a_final_event() -> None:
    delta = _event(
        "response.refusal.delta", output_index=0, content_index=0, item_id="msg_1", delta="No."
    )
    truth = _truth(_stream(delta))
    assert (truth["finish"], truth["raw_finish"], truth["stream_completed"]) == (
        "refusal",
        None,
        False,
    )


def test_reasoning_events_are_skipped() -> None:
    item = {"id": "rs_1", "type": "reasoning", "summary": [], "status": "incomplete"}
    part = {"type": "summary_text", "text": "thinking"}
    fields = dict(item_id="rs_1", output_index=0, summary_index=0)
    truth = _truth(
        _stream(
            _event(
                "response.output_item.added", output_index=0, item={**item, "status": "in_progress"}
            ),
            _event("response.reasoning_summary_part.added", **fields, part={**part, "text": ""}),
            _event("response.reasoning_summary_text.delta", **fields, delta="thinking"),
            _event("response.reasoning_summary_text.done", **fields, text="thinking"),
            _event("response.reasoning_summary_part.done", **fields, part=part),
            _event("response.output_item.done", output_index=0, item=item),
            _event(
                "response.incomplete",
                response=_response(
                    [item],
                    "incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                    usage=USAGE,
                ),
            ),
        )
    )
    assert (truth["finish"], truth["text"], truth["tool_calls"]) == ("length", None, [])


def test_a_stream_without_a_final_event_keeps_partial_content() -> None:
    truth = _truth(
        _stream(
            _event("response.created", response=_response(status="in_progress", usage=USAGE)),
            _delta("partial"),
            _added(),
            _arguments(0, '{"city":"Zü'),
        )
    )
    assert (truth["finish"], truth["raw_finish"], truth["text"], truth["stream_completed"]) == (
        "none",
        None,
        "partial",
        False,
    )
    assert truth["usage"]["raw"] is None
    assert truth["tool_calls"][0]["arguments_raw"] == '{"city":"Zü'


def test_stream_aborted_after_final_event_is_not_completed() -> None:
    truth = _truth(
        _stream(_delta("hi"), _event("response.completed", response=_response()), end="abort")
    )
    assert (truth["finish"], truth["stream_completed"]) == ("stop", False)


def test_streamed_structured_output() -> None:
    truth = _truth(
        _stream(_delta('{"island":"Κρήτη"}'), _event("response.completed", response=_response())),
        JSON_REQUEST,
    )
    assert truth["structured"] == {"island": "Κρήτη"}


def test_stream_stored_as_bytes_split_inside_a_character() -> None:
    exchange = _stream(_delta("Zürich"), _event("response.completed", response=_response()))
    raw = "".join(exchange.body.parts).encode()
    cut = raw.index("ü".encode()) + 1
    chunks = [base64.b64encode(piece).decode() for piece in (raw[:cut], raw[cut:])]
    truth = _truth(_exchange("chunks_b64", chunks, content_type="text/event-stream"))
    assert (truth["finish"], truth["text"], truth["stream_completed"]) == ("stop", "Zürich", True)


# --- Errors ---------------------------------------------------------------------------------------


def test_an_http_error() -> None:
    body = {
        "error": {
            "message": "Unknown parameter: 'stop'. Did you mean 'store'?",
            "type": "invalid_request_error",
            "param": "stop",
            "code": "unknown_parameter",
        }
    }
    truth = _truth(_exchange("json", body, status=400))
    assert truth["finish"] == "error"
    assert truth["error"] == {
        "status": 400,
        "type": "invalid_request_error",
        "code": "unknown_parameter",
        "message": "Unknown parameter: 'stop'. Did you mean 'store'?",
    }
    assert (truth["text"], truth["raw_finish"], truth["stream_completed"]) == (None, None, None)


@pytest.mark.parametrize("stream", [False, True])
def test_failed_response(stream) -> None:
    payload = _response(status="failed", error={"code": "server_error", "message": "failed"})
    exchange = (
        _stream(_delta("partial"), _event("response.failed", response=payload))
        if stream
        else _exchange("json", payload)
    )
    truth = _truth(exchange)
    assert (truth["finish"], truth["raw_finish"]) == ("error", "failed")
    assert truth["error"] == {
        "status": None,
        "type": None,
        "code": "server_error",
        "message": "failed",
    }
    assert truth["stream_completed"] == (True if stream else None)


def test_failed_event_without_error_still_means_error() -> None:
    truth = _truth(_stream(_event("response.failed", response=_response(status=None))))
    assert truth["finish"] == "error"


@pytest.mark.parametrize("nested", [False, True])
def test_error_event(nested) -> None:
    error = {"code": "server_error", "message": "failed", "param": None}
    event = _event("error", **({"error": error} if nested else error))
    truth = _truth(_stream(_delta("partial"), event))
    assert (truth["finish"], truth["text"], truth["stream_completed"]) == (
        "error",
        "partial",
        False,
    )
    assert truth["error"] == {
        "status": None,
        "type": None,
        "code": "server_error",
        "message": "failed",
    }


def test_in_band_error_is_not_lost_at_a_final_event() -> None:
    truth = _truth(
        _stream(
            _event("error", code="server_error", message="failed", param=None),
            _event("response.completed", response=_response()),
        )
    )
    assert (truth["finish"], truth["stream_completed"]) == ("error", True)


@pytest.mark.parametrize(
    ("kind", "body", "message"),
    [
        ("text", "upstream error", "upstream error"),
        ("text", "", "HTTP 500"),
        ("json", {}, "HTTP 500"),
        ("json", [1], "HTTP 500"),
        ("json", {"error": "overloaded"}, "overloaded"),
        ("chunks_b64", [base64.b64encode(b"\xff").decode()], "HTTP 500"),
        ("sse", ["data: nope\n\n"], "data: nope\n\n"),
    ],
)
def test_other_error_bodies(kind, body, message) -> None:
    truth = _truth(_exchange(kind, body, status=500))
    assert truth["error"] == {"status": 500, "type": None, "code": None, "message": message}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("{not json", "the data is not JSON"),
        ("[1]", "the data is not a JSON object"),
        ("[DONE]", "the data is not JSON"),
    ],
)
def test_unreadable_event_data(data, message) -> None:
    with pytest.raises(ValueError, match=message):
        _truth(_exchange("sse", [f"data: {data}\n\n"]))


def test_unreadable_bodies() -> None:
    with pytest.raises(ValueError, match="no exchanges"):
        extract_truth([], None)
    for kind, body in [("json", [1]), ("text", "bad")]:
        with pytest.raises(ValueError, match="not a JSON object"):
            _truth(_exchange(kind, body))
    with pytest.raises(ValueError):
        _truth(
            _exchange(
                "chunks_b64", [base64.b64encode(b"\xff").decode()], content_type="text/event-stream"
            )
        )


def test_aborted_non_stream_body_delivered_nothing() -> None:
    chunks = [base64.b64encode(b'{"output":[').decode()]
    truth = _truth(_exchange("chunks_b64", chunks, end="abort"))
    assert (truth["finish"], truth["text"], truth["error"], truth["stream_completed"]) == (
        "none",
        None,
        None,
        None,
    )


def test_last_exchange_only() -> None:
    first = _exchange("json", {"error": {"message": "retry"}}, status=429)
    final = _exchange("json", _response([_message(_text("ok"))]))
    truth = extract_truth([first, final], None)
    assert (truth["finish"], truth["text"], truth["error"]) == ("stop", "ok", None)


# --- Request and returned model -------------------------------------------------------------------


def _call(**fields: object) -> Call:
    from wiretruth.formats import _build_call

    base = {"stream": False, "messages": [{"role": "user", "content": "Hello"}]}
    return _build_call({**base, **fields})


def test_minimal_request_and_registration() -> None:
    assert PROVIDERS["openai-responses"] is openai_responses
    assert "https://api.openai.com" + BASE_PATHS["openai-responses"] == openai_responses.API_BASE
    assert openai_responses.KEY_ENV == "OPENAI_API_KEY"
    assert openai_responses.DEFAULT_MODEL == "gpt-6-luna"
    request = build_request(_call(), "m")
    assert (request.method, request.path) == ("POST", "/responses")
    assert request.headers == {"Content-Type": "application/json", "Accept": "application/json"}
    assert request.body == {"model": "m", "input": [{"role": "user", "content": "Hello"}]}
    assert openai_responses.auth_headers("k") == {"Authorization": "Bearer k"}


def test_every_call_field_maps_to_the_responses_request() -> None:
    tool = {"name": "get_weather", "description": "d", "parameters": {"type": "object"}}
    messages = [{"role": "system", "content": "Be brief"}, {"role": "user", "content": "Hello"}]
    schema = {"type": "object"}
    request = build_request(
        _call(
            stream=True,
            max_output_tokens=50,
            messages=messages,
            tools=[tool],
            tool_choice={"name": "get_weather"},
            output_schema=schema,
            reasoning={"effort": "medium"},
        ),
        "m",
    )
    assert request.headers["Accept"] == "text/event-stream"
    assert request.body == {
        "model": "m",
        "input": messages,
        "stream": True,
        "max_output_tokens": 50,
        "tools": [{"type": "function", **tool}],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "text": {
            "format": {"type": "json_schema", "name": "output", "schema": schema, "strict": True}
        },
        "reasoning": {"effort": "medium"},
    }


@pytest.mark.parametrize("choice", ["auto", "required", "none"])
def test_tool_choice_strings(choice) -> None:
    assert build_request(_call(tool_choice=choice), "m").body["tool_choice"] == choice


@pytest.mark.parametrize(
    "fields", [{"stop": ["END"]}, {"extra": {"include_usage": True}}, {"extra": {"future": 1}}]
)
def test_unsupported_call_fields_fail(fields) -> None:
    with pytest.raises(ValueError, match="has no meaning for openai-responses"):
        build_request(_call(**fields), "m")


@pytest.mark.parametrize(
    ("body", "stream"),
    [
        (None, False),
        ([], False),
        ({}, False),
        ({"stream": False}, False),
        ({"stream": 1}, False),
        ({"stream": True}, True),
    ],
)
def test_requested_stream(body, stream) -> None:
    assert openai_responses.requested_stream(PATH, body) is stream


def test_returned_model_from_json_text_and_chunks() -> None:
    payload = _response()
    for kind, body in [
        ("json", payload),
        ("text", json.dumps(payload)),
        ("chunks_b64", [base64.b64encode(json.dumps(payload).encode()).decode()]),
    ]:
        assert returned_model(_exchange(kind, body)) == MODEL
    assert returned_model(_exchange("text", "bad")) is None


def test_returned_model_only_from_response_created() -> None:
    assert returned_model(_stream(_event("response.created", response=_response()))) == MODEL
    events = ["data: {bad\n\n", "data: []\n\n"]
    events.extend(
        _stream(
            _event("response.in_progress", response=_response()),
            _event("response.created", response={}),
            _event("response.created", response=_response()),
        ).body.parts
    )
    assert returned_model(_exchange("sse", events)) == MODEL
    assert returned_model(_stream(_event("response.completed", response=_response()))) is None
    assert (
        returned_model(
            _exchange(
                "chunks_b64", [base64.b64encode(b"\xff").decode()], content_type="text/event-stream"
            )
        )
        is None
    )


def test_current_prompt_is_used_for_responses() -> None:
    scenario = load_scenario(REPO / "suite/scenarios/truncation/text.json")
    body = build_request(scenario.call_for("openai-responses"), "gpt-6-luna").body
    assert body["input"] == [
        {
            "role": "user",
            "content": "Write a 200-word paragraph about how octopuses change colour "
            "to hide from predators.",
        }
    ]
    assert body["max_output_tokens"] == 16


def test_non_text_values_do_not_create_text() -> None:
    truth = _truth(_exchange("json", _response([_message({"type": "output_text", "text": None})])))
    assert truth["text"] is None
