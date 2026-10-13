import base64
from copy import deepcopy
from dataclasses import replace

import pytest
from anthropic_samples import (
    ERROR,
    MODEL,
    OVERLOADED,
    SIGNATURE,
    STREAM,
    TEXT,
    THINKING,
    TOOL,
    event,
    exchange,
)

from wiretruth.formats import Body
from wiretruth.providers import anthropic


def truth(payload, **kwargs):
    return anthropic.extract_truth([exchange(payload, **kwargs)], None)


def test_anthropic_plain_and_thinking_only():
    result = truth(TEXT)
    assert result == {
        "finish": "stop",
        "raw_finish": "end_turn",
        "text": "Hello!",
        "tool_calls": [],
        "structured": None,
        "error": None,
        "stream_completed": None,
        "usage": {
            "input": 25,
            "cached_input": None,
            "output": 15,
            "reasoning": None,
            "total": 40,
            "alternatives": {},
            "raw": TEXT["usage"],
        },
    }
    result = truth(THINKING)
    assert result["text"] is None and result["tool_calls"] == []
    assert result["finish"] == "length" and result["usage"]["reasoning"] is None
    assert result["usage"]["output"] == 16


@pytest.mark.parametrize(
    "raw,finish",
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("tool_use", "tool_calls"),
        ("pause_turn", "other"),
        ("refusal", "refusal"),
        ("WIRETRUTH_UNKNOWN_REASON", "other"),
        ("", "other"),
        (None, "none"),
    ],
)
def test_anthropic_finish_table(raw, finish):
    payload = {**TEXT, "stop_reason": raw}
    if raw == "refusal":
        payload["stop_details"] = {"type": "refusal", "category": None, "explanation": "Declined."}
    assert (truth(payload)["finish"], truth(payload)["raw_finish"]) == (finish, raw)


@pytest.mark.parametrize(
    "content,expected",
    [
        (None, None),
        ([], None),
        ([{}], None),
        ([None], None),
        ([{"type": "text", "text": ""}], ""),
        ([{"type": "text", "text": "Café "}, {"type": "text", "text": "東京 🌸"}], "Café 東京 🌸"),
        ([{"type": "thinking", "thinking": "not answer text", "signature": SIGNATURE}], None),
        (
            [{"type": "redacted_thinking", "data": "opaque"}, {"type": "future", "text": "ignore"}],
            None,
        ),
        ([{"type": "text", "text": None}], None),
    ],
)
def test_anthropic_content_blocks(content, expected):
    assert truth({**TEXT, "content": content})["text"] == expected


@pytest.mark.parametrize(
    "usage,input_tokens,cached,output,total,alternatives",
    [
        (None, None, None, None, None, {}),
        ({}, None, None, None, None, {}),
        ({"input_tokens": 10}, 10, None, None, None, {}),
        ({"output_tokens": 4}, None, None, 4, None, {}),
        ({"input_tokens": True, "output_tokens": "4"}, None, None, None, None, {}),
        ({"input_tokens": 0, "output_tokens": 0}, 0, None, 0, 0, {}),
        (
            {
                "input_tokens": 12,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 20,
                "output_tokens": 4,
            },
            112,
            80,
            4,
            116,
            {"input": {"uncached_input": 12}},
        ),
        (
            {"input_tokens": 12, "cache_creation_input_tokens": 20, "output_tokens": 4},
            32,
            None,
            4,
            36,
            {"input": {"uncached_input": 12}},
        ),
        ({"input_tokens": 12, "cache_read_input_tokens": 0, "output_tokens": 4}, 12, 0, 4, 16, {}),
    ],
)
def test_anthropic_usage_preserves_missing_zero_and_cache_counts(
    usage, input_tokens, cached, output, total, alternatives
):
    result = truth({**TEXT, "usage": usage})["usage"]
    assert result == {
        "input": input_tokens,
        "cached_input": cached,
        "output": output,
        "reasoning": None,
        "total": total,
        "alternatives": alternatives,
        "raw": usage,
    }


def test_anthropic_stream_merges_cumulative_usage_and_requires_message_stop():
    saved = deepcopy(STREAM)
    result = truth(STREAM)
    assert result["text"] == "Hello!" and result["stream_completed"] is True
    assert result["usage"]["input"] == 25 and result["usage"]["output"] == 15
    assert result["usage"]["raw"] == TEXT["usage"]
    assert truth(STREAM[:-1])["stream_completed"] is False
    assert truth(STREAM, end="abort")["stream_completed"] is False
    assert truth(STREAM[:-2])["finish"] == "none"
    assert saved == STREAM
    counts = {
        "input_tokens": 30,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 6,
        "output_tokens": 16,
        "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 0},
    }
    result = truth(
        [*STREAM[:-1], event({"type": "message_delta", "delta": {}, "usage": counts}), STREAM[-1]]
    )
    assert result["usage"]["raw"] == counts and result["usage"]["total"] == 56


def test_anthropic_stream_ignores_thinking_signatures_ping_and_future_events():
    events = [
        event({"type": "message_start", "message": {}}),
        event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            }
        ),
        event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "private"},
            }
        ),
        event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": SIGNATURE},
            }
        ),
        event({"type": "content_block_stop", "index": 0}),
        event({"type": "future"}),
    ]
    assert truth(events)["text"] is None
    assert truth([*events, *STREAM[1:]])["text"] == "Hello!"


def test_anthropic_json_tools_have_object_inputs_and_stable_order():
    second = {**TOOL, "id": "toolu_second", "input": {"location": "Zürich"}}
    result = truth({**TEXT, "content": [TOOL, second], "stop_reason": "tool_use"})
    assert result["finish"] == "tool_calls" and len(result["tool_calls"]) == 2
    first = result["tool_calls"][0]
    assert first == {
        "id": TOOL["id"],
        "name": "get_weather",
        "arguments_raw": '{"location":"San Francisco, CA","unit":"celsius"}',
        "arguments_parsed_ok": True,
        "arguments": TOOL["input"],
    }
    assert result["tool_calls"][1]["arguments"] == {"location": "Zürich"}
    with pytest.raises(ValueError, match="input is not a JSON object"):
        truth({**TEXT, "content": [{**TOOL, "input": '{"location":'}]})


@pytest.mark.parametrize(
    "raw,ok,args",
    [
        ('{"city":"Málaga"}', True, {"city": "Málaga"}),
        ('{"city":"Málaga', False, None),
        ("", False, None),
    ],
)
def test_anthropic_stream_tool_fragments_preserve_partial_json(raw, ok, args):
    events = [
        event({"type": "content_block_start", "index": 1, "content_block": {**TOOL, "input": {}}})
    ]
    events += [
        event(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": piece},
            }
        )
        for piece in (raw[:5], raw[5:])
    ]
    result = truth(events)["tool_calls"][0]
    assert result["id"] == TOOL["id"] and result["name"] == TOOL["name"]
    assert (result["arguments_raw"], result["arguments_parsed_ok"], result["arguments"]) == (
        raw,
        ok,
        args,
    )
    # A server tool or future block's JSON delta is not a client tool call.
    assert (
        truth(
            [
                event(
                    {
                        "type": "content_block_delta",
                        "index": 9,
                        "delta": {"type": "input_json_delta", "partial_json": "{}"},
                    }
                )
            ]
        )["tool_calls"]
        == []
    )


@pytest.mark.parametrize(
    "finish,text,format,expected",
    [
        ("end_turn", '{"n":2}', {"type": "json_schema"}, {"n": 2}),
        ("end_turn", '{"n":', {"type": "json_schema"}, None),
        ("max_tokens", '{"n":2}', {"type": "json_schema"}, None),
        ("end_turn", '{"n":2}', None, None),
    ],
)
def test_anthropic_structured_output_requires_schema_and_success(finish, text, format, expected):
    ex = exchange({**TEXT, "stop_reason": finish, "content": [{"type": "text", "text": text}]})
    assert (
        anthropic.extract_truth([ex], {"output_config": {"format": format}})["structured"]
        == expected
    )


@pytest.mark.parametrize(
    "payload,status,message,type",
    [
        (ERROR, 404, ERROR["error"]["message"], "not_found_error"),
        (ERROR, 200, ERROR["error"]["message"], "not_found_error"),
        ({}, 529, "HTTP 529", None),
        ({"error": "busy"}, 500, "busy", None),
    ],
)
def test_anthropic_error_envelopes(payload, status, message, type):
    result = truth(payload, status=status)
    assert result["finish"] == "error"
    assert result["error"] == {
        "message": message,
        "type": type,
        "code": None,
        "status": status if status >= 400 else None,
    }


def test_anthropic_error_event_overrides_finish_and_completion():
    result = truth([*STREAM, event(OVERLOADED)])
    assert result["finish"] == "error" and result["raw_finish"] == "end_turn"
    assert result["text"] == "Hello!" and result["stream_completed"] is False
    assert result["error"] == {
        "message": "Overloaded",
        "type": "overloaded_error",
        "status": None,
        "code": None,
    }


def test_anthropic_unreadable_bodies_and_partial_utf8():
    ex = exchange(TEXT)
    with pytest.raises(ValueError, match="no exchanges"):
        anthropic.extract_truth([], None)
    for body in [Body("text", "not json", ()), Body("json", [], ())]:
        with pytest.raises(ValueError, match="not a JSON object"):
            anthropic.extract_truth([replace(ex, body=body)], None)
        assert (
            anthropic.extract_truth([replace(ex, body=body, end="abort")], None)["finish"] == "none"
        )
    failed = replace(ex, status=502, body=Body("text", "gateway failed", ()))
    assert anthropic.extract_truth([failed], None)["error"]["message"] == "gateway failed"
    with pytest.raises(ValueError, match="event 0"):
        truth(["data: []\n\n"])
    with pytest.raises(ValueError):
        truth(["data: bad json\n\n"])
    raw = "".join(STREAM[:4]).encode() + 'data: {"text":"é'.encode()[:-1]
    byte_ex = replace(
        exchange(STREAM),
        body=Body("chunks_b64", None, (base64.b64encode(raw).decode(),)),
        headers={"content-type": "text/event-stream"},
        end="abort",
    )
    assert anthropic.extract_truth([byte_ex], None)["text"] == "Hello"
    assert anthropic.extract_truth([failed, ex], None)["finish"] == "stop"


def test_anthropic_returned_model_and_requested_stream():
    assert anthropic.returned_model(exchange(TEXT)) == MODEL
    assert anthropic.returned_model(exchange(STREAM)) == MODEL
    assert anthropic.returned_model(exchange({})) is None
    assert anthropic.returned_model(exchange(["data: bad\n\n", "data: []\n\n", *STREAM])) == MODEL
    assert (
        anthropic.returned_model(exchange([event({"type": "message_start", "message": {}})]))
        is None
    )
    ex = replace(
        exchange(STREAM),
        body=Body("chunks_b64", None, ("/w==",)),
        headers={"content-type": "text/event-stream"},
    )
    assert anthropic.returned_model(ex) is None
    for body, expected in [
        ({"stream": True}, True),
        ({"stream": False}, False),
        ({"stream": "true"}, False),
        ([], False),
    ]:
        assert anthropic.requested_stream("/v1/messages", body) is expected


@pytest.mark.parametrize(
    "details,expected",
    [
        ({"thinking_tokens": 312}, 312),
        ({"thinking_tokens": 0}, 0),
        ({"thinking_tokens": None}, None),
        ({"thinking_tokens": True}, None),
        ({"thinking_tokens": "312"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_anthropic_reasoning_usage_is_an_output_breakdown(details, expected):
    # https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#pricing
    counts = {"input_tokens": 25, "output_tokens": 348, "output_tokens_details": details}
    result = truth({**TEXT, "usage": counts})["usage"]
    assert result["reasoning"] == expected
    assert result["output"] == 348 and result["total"] == 373
    assert result["raw"] == counts


def test_anthropic_stream_reasoning_usage_arrives_only_in_final_delta():
    final = event(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 348, "output_tokens_details": {"thinking_tokens": 312}},
        }
    )
    assert truth(STREAM[:-2])["usage"]["reasoning"] is None
    result = truth([*STREAM[:-2], final, STREAM[-1]])["usage"]
    assert result["reasoning"] == 312
    assert result["output"] == 348 and result["total"] == 373
    assert result["raw"]["output_tokens_details"] == {"thinking_tokens": 312}


@pytest.mark.parametrize("details", [None, {"thinking_tokens": None}])
def test_anthropic_null_usage_deltas_preserve_earlier_counts(details):
    counts = {
        "input_tokens": 25,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 10,
        "output_tokens": 15,
        "output_tokens_details": {"thinking_tokens": 6},
    }
    events = [
        event({"type": "message_start", "message": {**TEXT, "usage": counts}}),
        event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {
                    "input_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                    "output_tokens": 20,
                    "output_tokens_details": details,
                },
            }
        ),
        STREAM[-1],
    ]
    saved = deepcopy(events)
    result = truth(events)["usage"]
    assert result["raw"] == {**counts, "output_tokens": 20}
    assert (result["input"], result["cached_input"], result["output"]) == (40, 10, 20)
    assert result["reasoning"] == 6 and result["total"] == 60
    assert events == saved
    zeros = {key: 0 for key in counts if key != "output_tokens_details"}
    zeros["output_tokens_details"] = {"thinking_tokens": 0}
    result = truth(
        [*events[:-1], event({"type": "message_delta", "delta": {}, "usage": zeros}), STREAM[-1]]
    )["usage"]
    assert result["raw"] == zeros
    assert result["input"] == result["reasoning"] == result["output"] == result["total"] == 0


@pytest.mark.parametrize(
    "stop,closed,fragment,expected,ok",
    [
        ("tool_use", True, None, {}, True),
        ("max_tokens", True, None, None, False),
        ("model_context_window_exceeded", True, None, None, False),
        (None, False, None, None, False),
        ("tool_use", True, "", None, False),
        ("max_tokens", True, '{"city":', None, False),
        ("tool_use", True, '{"city":"Zürich"}', {"city": "Zürich"}, True),
    ],
)
def test_anthropic_stream_empty_tool_input_requires_a_closed_untruncated_block(
    stop, closed, fragment, expected, ok
):
    events = [
        STREAM[0],
        event({"type": "content_block_start", "index": 0, "content_block": {**TOOL, "input": {}}}),
    ]
    if fragment is not None:
        events.append(
            event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": fragment},
                }
            )
        )
    if closed:
        events.append(event({"type": "content_block_stop", "index": 0}))
    if stop is not None:
        events.extend(
            [event({"type": "message_delta", "delta": {"stop_reason": stop}}), STREAM[-1]]
        )
    call = truth(events)["tool_calls"][0]
    assert call["id"] == TOOL["id"] and call["name"] == TOOL["name"]
    assert call["arguments"] == expected and call["arguments_parsed_ok"] is ok
    assert call["arguments_raw"] == ("{}" if ok and fragment is None else fragment)
