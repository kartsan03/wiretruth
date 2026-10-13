"""Anthropic Messages, JSON and SSE at ``POST /v1/messages``.

Text excludes thinking and signatures. Tool inputs are objects in JSON and accumulated
input_json_delta strings in SSE, including unparseable prefixes. Only message_stop completes
a stream. Usage merges cumulative message_start/message_delta fields without summing snapshots;
input includes cache reads and writes, with uncached input retained as an alternative. Thinking
is billed inside output_tokens; output_tokens_details.thinking_tokens gives its optional breakdown,
reported only in the final message_delta for streams. Null delta counters preserve earlier values.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from wiretruth.formats import Body, Call, Exchange, ForcedTool, JsonObject, JsonValue, parse_json
from wiretruth.providers import (
    RawRequest,
    body_text,
    event_object,
    is_event_stream,
    json_int,
    json_object,
    json_str,
    replace_finish_fields,
    sse_events,
)
from wiretruth.providers.openai_chat import _parse, _payload, _ToolCall

ID = "anthropic"
API_BASE = "https://api.anthropic.com"
KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-sonnet-5-5"
FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "pause_turn": "other",
    "refusal": "refusal",
}


def build_request(call: Call, model: str) -> RawRequest:
    unknown = sorted(set(call.extra) - {"include_usage", "anthropic"})
    if unknown:
        raise ValueError(f"extra field {unknown[0]!r} has no meaning for {ID}")
    hints = json_object(call.extra.get("anthropic", {}))
    if hints is None or set(hints) - {"cache", "eager_input_streaming"}:
        raise ValueError("extra.anthropic supports only cache and eager_input_streaming")
    if any(type(value) is not bool for value in hints.values()):
        raise ValueError("extra.anthropic hints must be booleans")
    if call.max_output_tokens is None:
        raise ValueError("Anthropic requires max_output_tokens (max_tokens on the wire)")
    body: JsonObject = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in call.messages
            if message.role != "system"
        ],
        "max_tokens": call.max_output_tokens,
    }
    system = [message.content for message in call.messages if message.role == "system"]
    if system:
        body["system"] = "\n\n".join(system)
    if call.stream:
        body["stream"] = True
    if call.stop is not None:
        body["stop_sequences"] = list(call.stop)
    if call.tools:
        body["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
                **({"eager_input_streaming": True} if hints.get("eager_input_streaming") else {}),
            }
            for tool in call.tools
        ]
    if call.tool_choice == "required" or (
        isinstance(call.tool_choice, ForcedTool) and not model.startswith("claude-haiku-4-5")
    ):
        raise ValueError("forced tool_choice is unsupported; use an Anthropic auto override")
    if isinstance(call.tool_choice, ForcedTool):
        body["tool_choice"] = {"type": "tool", "name": call.tool_choice.name}
    elif call.tool_choice is not None:
        body["tool_choice"] = {"type": call.tool_choice}
    config: JsonObject = {}
    # A named forced tool carries the structured schema in its input_schema instead.
    if call.output_schema is not None and not isinstance(call.tool_choice, ForcedTool):
        config["format"] = {"type": "json_schema", "schema": call.output_schema}
    # Effort none sends nothing: Sonnet 5.5 rejects thinking: disabled.
    if call.reasoning is not None and call.reasoning.effort != "none":
        body["thinking"] = {"type": "adaptive"}
        effort = call.reasoning.effort
        config["effort"] = "low" if effort == "minimal" else effort
    if config:
        body["output_config"] = config
    if hints.get("cache"):
        body["cache_control"] = {"type": "ephemeral"}
    return RawRequest(
        method="POST",
        path="/v1/messages",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if call.stream else "application/json",
        },
        body=body,
    )


def auth_headers(key: str) -> dict[str, str]:
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def requested_stream(path: str, body: JsonValue) -> bool:
    return isinstance(body, dict) and body.get("stream") is True


def returned_model(exchange: Exchange) -> str | None:
    if not is_event_stream(exchange):
        return json_str(_payload(exchange)[0], "model")
    try:
        events = sse_events(body_text(exchange))
    except ValueError:
        return None
    for event in events:
        try:
            payload = json_object(parse_json(event.data.encode())) or {}
        except (ValueError, RecursionError):
            continue
        if (model := json_str(json_object(payload.get("message")), "model")) is not None:
            return model
    return None


# --- Transforms -----------------------------------------------------------------------------------


def _finish_fields(payload: JsonObject, stream: bool) -> list[tuple[JsonObject, str]]:
    if stream:
        if payload.get("type") != "message_delta":
            return []
        payload = json_object(payload.get("delta")) or {}
    return [(payload, "stop_reason")] if json_str(payload, "stop_reason") is not None else []


def finish_index(events: Sequence[str], *, kind: str = "sse") -> int | None:
    if kind != "sse":
        raise ValueError("Anthropic finish events need an sse body")
    for index, event in enumerate(events):
        payload = event_object(event)
        if payload is not None and _finish_fields(payload, True):
            return index
    return None


def error_event(error: JsonObject, *, kind: str = "sse") -> str:
    if kind != "sse":
        raise ValueError("Anthropic error events need an sse body")
    if error.get("type") != "error" or json_object(error.get("error")) is None:
        raise ValueError("Anthropic error events need type error and an error object")
    return "event: error\ndata: " + json.dumps(error, ensure_ascii=False) + "\n\n"


def replace_finish(body: Body, value: str) -> JsonObject:
    try:
        return replace_finish_fields(body, value, _finish_fields)
    except ValueError as error:
        if body.kind != "sse" or str(error) != "can't preserve this event's JSON serialization":
            raise
    parts = list(body.parts)
    for index, event in enumerate(parts):
        payload = event_object(event)
        fields = [] if payload is None else _finish_fields(payload, True)
        if not fields or fields[0][0]["stop_reason"] == value:
            continue
        # The basic docs example mixes JSON spacing. Replace only the string token
        # in delta.stop_reason, retaining its whitespace and all other event bytes.
        pattern = r'("delta"\s*:\s*\{\s*"stop_reason"\s*:\s*)("(?:[^"\\]|\\.)*")'
        parts[index], count = re.subn(
            pattern, lambda match: match[1] + json.dumps(value), event, count=1
        )
        if not count:
            raise ValueError("can't preserve this event's stop_reason serialization")
    return {"sse": list(parts)}


# --- Truth ----------------------------------------------------------------------------------------


def _usage_truth(usage: JsonObject | None) -> JsonObject:
    uncached = json_int(usage, "input_tokens")
    cached = json_int(usage, "cache_read_input_tokens")
    created = json_int(usage, "cache_creation_input_tokens")
    input_tokens = None if uncached is None else uncached + (cached or 0) + (created or 0)
    output = json_int(usage, "output_tokens")
    return {
        "input": input_tokens,
        "cached_input": cached,
        "output": output,
        "reasoning": json_int(
            json_object((usage or {}).get("output_tokens_details")), "thinking_tokens"
        ),
        "total": None if input_tokens is None or output is None else input_tokens + output,
        "alternatives": {"input": {"uncached_input": uncached}}
        if uncached is not None and uncached != input_tokens
        else {},
        "raw": usage,
    }


def extract_truth(exchanges: Sequence[Exchange], request: JsonObject | None) -> JsonObject:
    if not exchanges:
        raise ValueError("no exchanges")
    exchange = exchanges[-1]
    status = exchange.status if exchange.status >= 400 else None
    stream = is_event_stream(exchange) and status is None
    if stream:
        payloads = []
        for index, event in enumerate(sse_events(body_text(exchange))):
            payload = json_object(parse_json(event.data.encode()))
            if payload is None:
                raise ValueError(f"event {index}: the data is not a JSON object")
            payloads.append(payload)
    else:
        payload, text = _payload(exchange)
        if payload is None:
            if status is not None:
                payload = {"error": {"message": text or f"HTTP {status}"}}
            elif exchange.end == "abort":
                payload = {}
            else:
                raise ValueError("the body is not a JSON object")
        payloads = [payload]
    raw_finish = None
    pieces: list[str] = []
    calls: dict[int, _ToolCall] = {}
    tool_inputs: dict[int, JsonObject] = {}
    closed_tools: set[int] = set()
    usage: JsonObject | None = None
    error: JsonObject | None = None
    completed = False
    for payload in payloads:
        if (wire_error := payload.get("error")) is not None:
            detail = json_object(wire_error) or {"message": str(wire_error)}
            error = {
                "message": json_str(detail, "message") or "",
                "type": json_str(detail, "type"),
                "code": None,
                "status": status,
            }
        kind = payload.get("type")
        if kind == "message_stop":
            completed = True
        if kind == "message_start":
            payload = json_object(payload.get("message")) or {}
        if (counts := json_object(payload.get("usage"))) is not None:
            if kind == "message_delta":
                counts = {key: value for key, value in counts.items() if value is not None}
                if (details := json_object(counts.get("output_tokens_details"))) is not None:
                    counts["output_tokens_details"] = {
                        **(json_object((usage or {}).get("output_tokens_details")) or {}),
                        **{key: value for key, value in details.items() if value is not None},
                    }
            usage = {**(usage or {}), **counts}
        delta = json_object(payload.get("delta")) or {}
        if (
            reason := json_str(delta if kind == "message_delta" else payload, "stop_reason")
        ) is not None:
            raw_finish = reason
        content = payload.get("content")
        blocks = list(enumerate(content)) if isinstance(content, list) else []
        if kind == "content_block_start":
            blocks = [(json_int(payload, "index") or 0, payload.get("content_block"))]
        for index, value in blocks:
            block = json_object(value) or {}
            if block.get("type") == "text" and (text := json_str(block, "text")) is not None:
                pieces.append(text)
            if block.get("type") == "tool_use":
                arguments = json_object(block.get("input"))
                if arguments is None:
                    raise ValueError("Anthropic tool input is not a JSON object")
                tool_inputs[index] = arguments
                call = calls.setdefault(index, _ToolCall())
                # Stream input: {} is a placeholder, not part of the JSON delta string.
                raw = (
                    None
                    if stream
                    else json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                )
                call.add(json_str(block, "id"), json_str(block, "name"), raw)
        if kind == "content_block_delta":
            if delta.get("type") == "text_delta" and (text := json_str(delta, "text")) is not None:
                pieces.append(text)
            if delta.get("type") == "input_json_delta":
                index = json_int(payload, "index") or 0
                if index in calls:
                    calls[index].add(None, None, json_str(delta, "partial_json"))
        if kind == "content_block_stop":
            index = json_int(payload, "index") or 0
            if index in calls:
                closed_tools.add(index)
    for index in closed_tools:
        call = calls[index]
        if not call.has_arguments and raw_finish not in (
            "max_tokens",
            "model_context_window_exceeded",
        ):
            # With no fragments, a completed tool retains its start input, usually {}.
            call.add(
                None,
                None,
                json.dumps(tool_inputs[index], ensure_ascii=False, separators=(",", ":")),
            )
    if status is not None and error is None:
        error = {"message": f"HTTP {status}", "type": None, "code": None, "status": status}
    finish = "none" if raw_finish is None else FINISH.get(raw_finish, "other")
    if error is not None:
        finish = "error"
    text = "".join(pieces) if pieces else None
    config = json_object((request or {}).get("output_config")) or {}
    structured: JsonValue = None
    if finish == "stop" and json_str(json_object(config.get("format")), "type") == "json_schema":
        structured = _parse(text)[0]
    return {
        "finish": finish,
        "raw_finish": raw_finish,
        "text": text,
        "tool_calls": [calls[index].truth() for index in sorted(calls)],
        "structured": structured,
        "stream_completed": completed and error is None and exchange.end == "clean"
        if stream
        else None,
        "usage": _usage_truth(usage),
        "error": error,
    }
