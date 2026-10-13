"""Literal Messages documentation shapes with local values where noted."""

import json

from wiretruth.formats import build_exchange
from wiretruth.providers import anthropic

MODEL = "claude-sonnet-5-5"
SIGNATURE = "EqQBCgIYAhIM1gbcDa9GJwZA2b3hGgxBdjrkzLoky3dl1pkiMOYds..."

# https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
# Model changed from the example; counts/text are the basic streaming example's values.
TEXT = {
    "id": "msg_01Aq9w938a90dw8q",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello!"}],
    "model": MODEL,
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "stop_details": None,
    "usage": {"input_tokens": 25, "output_tokens": 15},
}

# https://platform.claude.com/docs/en/build-with-claude/thinking#controlling-thinking-display
# The one-block max_tokens combination is assumed; the empty block shape is documented.
THINKING = {
    **TEXT,
    "content": [{"type": "thinking", "thinking": "", "signature": SIGNATURE}],
    "stop_reason": "max_tokens",
    "usage": {"input_tokens": 25, "output_tokens": 16},
}

# https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls
TOOL = {
    "type": "tool_use",
    "id": "toolu_01A09q90qw90lq917835lq9",
    "name": "get_weather",
    "input": {"location": "San Francisco, CA", "unit": "celsius"},
}

# https://platform.claude.com/docs/en/api/errors#error-shapes
ERROR = {
    "type": "error",
    "error": {"type": "not_found_error", "message": "The requested resource could not be found."},
    "request_id": "req_011CSHoEeqs5C35K2UUqR7Fy",
}
OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


def event(payload):
    return (
        "event: " + payload["type"] + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    )


# https://platform.claude.com/docs/en/build-with-claude/streaming#basic-streaming-request
# Literal basic example, only the model is substituted. Keep the mixed spacing in message_delta.
STREAM = [
    (
        'event: message_start\ndata: {"type": "message_start", "message": {'
        '"id": "msg_1nZdL29xx5MUA1yADyHTEsnR8uuvGzszyY", "type": "message"'
        ', "role": "assistant", "content": [], "model": "claude-sonnet-5-5'
        '", "stop_reason": null, "stop_sequence": null, "usage": {"input_t'
        'okens": 25, "output_tokens": 1}}}\n\n'
    ),
    (
        'event: content_block_start\ndata: {"type": "content_block_start", '
        '"index": 0, "content_block": {"type": "text", "text": ""}}\n\n'
    ),
    'event: ping\ndata: {"type": "ping"}\n\n',
    (
        'event: content_block_delta\ndata: {"type": "content_block_delta", '
        '"index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n\n'
    ),
    (
        'event: content_block_delta\ndata: {"type": "content_block_delta", '
        '"index": 0, "delta": {"type": "text_delta", "text": "!"}}\n\n'
    ),
    'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n',
    (
        'event: message_delta\ndata: {"type": "message_delta", "delta": {"s'
        'top_reason": "end_turn", "stop_sequence":null}, "usage": {"output'
        '_tokens": 15}}\n\n'
    ),
    'event: message_stop\ndata: {"type": "message_stop"}\n\n',
]


def exchange(payload, status=200, end="clean", headers=None):
    body = {"sse": payload} if isinstance(payload, list) else {"json": payload}
    return build_exchange(
        {
            "match": {"method": "POST", "path": "/v1/messages"},
            "response": {
                "status": status,
                "headers": headers or {},
                "body": body,
                "end": end,
            },
        }
    )


def source():
    ex = {
        "match": {"method": "POST", "path": "/v1/messages"},
        "response": {
            "status": 200,
            "headers": {"content-type": "text/event-stream"},
            "body": {"sse": STREAM},
            "end": "clean",
        },
    }
    return {
        "format": "wiretruth.fixture/v1",
        "id": "anthropic/stream.test-source",
        "scenario": "stream.test-source",
        "provider": "anthropic",
        "tier": "documented",
        "provenance": {
            "requested_model": MODEL,
            "source_url": "https://platform.claude.com/docs/en/build-with-claude/streaming",
            "notes": "Basic streaming example, with model changed to Sonnet 5.5.",
        },
        "derived_from": None,
        "exchanges": [ex],
        "truth": anthropic.extract_truth([build_exchange(ex)], None),
    }
