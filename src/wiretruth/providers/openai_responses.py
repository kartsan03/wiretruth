"""openai-responses: OpenAI Responses, ``POST /v1/responses``.

Truth comes from the last exchange, which is the response the caller ends up with.

- ``finish``: ``error`` for an error response, ``response.failed`` or an in-band error;
  ``refusal`` for a refusal content part or delta, also in a stream without a final event.
  Otherwise ``incomplete`` maps its reason: ``max_output_tokens`` to ``length``,
  ``content_filter`` to itself, anything else to ``other``. ``completed`` means ``tool_calls``
  if output has a function call, otherwise ``stop``. Other statuses mean ``other``; no status or
  no final stream event means ``none``.
- ``raw_finish``: ``incomplete_details.reason`` when present, otherwise ``status``.
- ``text``: message ``output_text`` parts joined, or streamed ``response.output_text.delta``
  pieces joined; null when none were sent. Reasoning items contribute no text.
- ``tool_calls``: function calls, using ``call_id`` (the id sent back with a tool result), not
  the item ``id``. Streamed calls use ``output_index``, with ``call_id`` and ``name`` from
  ``response.output_item.added`` and argument deltas joined. Partial calls survive a cut stream.
- ``structured``: parsed text, only when ``text.format.type`` asks for ``json_schema`` or
  ``json_object`` and the finish is ``stop``. Duplicate JSON keys use their last value.
- ``stream_completed``: true when ``response.completed``, ``response.incomplete`` or
  ``response.failed`` arrived and the exchange ended cleanly; false otherwise, null for a
  non-stream response. There is no ``[DONE]`` terminator. Finish, status and usage come from
  that final event's response; text and tool calls come only from deltas.
- ``usage``: input, cached input, output and reasoning tokens from ``usage`` and its details;
  total is ``total_tokens`` or input + output. Alternatives are empty; raw usage is kept verbatim.
- ``error``: the error object's type, code and message; status is the HTTP status for an error
  response and null for an in-band error, including ``response.failed``'s ``response.error``.

A non-stream body cut off before it parses delivered nothing: its finish is ``none``. Any other
non-object body or non-JSON event data raises ValueError, as for Chat Completions.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

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
from wiretruth.providers.openai_chat import _error_truth, _parse, _payload, _ToolCall
from wiretruth.replay import BASE_PATHS

ID = "openai-responses"
API_BASE = "https://api.openai.com" + BASE_PATHS[ID]  # the path libraries use on replay
KEY_ENV = "OPENAI_API_KEY"
DEFAULT_MODEL = "gpt-6-luna"
PATH = "/responses"

FINISH = {"max_output_tokens": "length", "content_filter": "content_filter"}
_FINAL = ("response.completed", "response.incomplete", "response.failed")
_JSON_FORMATS = ("json_schema", "json_object")


# --- Request --------------------------------------------------------------------------------------


def build_request(call: Call, model: str) -> RawRequest:
    if call.stop is not None:
        raise ValueError(f"stop has no meaning for {ID}")
    if call.extra:
        raise ValueError(f"extra field {sorted(call.extra)[0]!r} has no meaning for {ID}")
    body: JsonObject = {
        "model": model,
        "input": [{"role": message.role, "content": message.content} for message in call.messages],
    }
    if call.stream:
        body["stream"] = True
    if call.max_output_tokens is not None:
        body["max_output_tokens"] = call.max_output_tokens
    if call.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in call.tools
        ]
    if isinstance(call.tool_choice, ForcedTool):
        body["tool_choice"] = {"type": "function", "name": call.tool_choice.name}
    elif call.tool_choice is not None:
        body["tool_choice"] = call.tool_choice
    if call.output_schema is not None:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": "output",
                "schema": call.output_schema,
                "strict": True,
            }
        }
    if call.reasoning is not None:
        body["reasoning"] = {"effort": call.reasoning.effort}
    accept = "text/event-stream" if call.stream else "application/json"
    return RawRequest(
        method="POST",
        path=PATH,
        headers={"Content-Type": "application/json", "Accept": accept},
        body=body,
    )


def auth_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


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
            chunk = json_object(parse_json(event.data.encode())) or {}
        except (ValueError, RecursionError):
            continue
        if chunk.get("type") == "response.created":
            model = json_str(json_object(chunk.get("response")), "model")
            if model is not None:
                return model
    return None


# --- Transforms -----------------------------------------------------------------------------------


def finish_index(events: Sequence[str], *, kind: str = "sse") -> int | None:
    """The first completed, incomplete or failed response event."""
    if kind != "sse":
        raise ValueError("Responses finish events need an sse body")
    for index, event in enumerate(events):
        payload = event_object(event)
        if payload is not None and payload.get("type") in _FINAL:
            return index
    return None


def error_event(error: JsonObject, *, kind: str = "sse") -> str:
    """Frame the complete error data with its Responses event type."""
    if kind != "sse":
        raise ValueError("Responses error events need an sse body")
    event_type = error.get("type")
    if not isinstance(event_type, str) or not event_type or any(c in event_type for c in "\r\n"):
        raise ValueError("a Responses error event needs a non-empty, single-line type")
    return (
        f"event: {event_type}\ndata: "
        + json.dumps(error, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def _finish_fields(payload: JsonObject, stream: bool) -> list[tuple[JsonObject, str]]:
    if stream:
        if payload.get("type") not in _FINAL:
            return []
        payload = json_object(payload.get("response")) or {}
    details = json_object(payload.get("incomplete_details"))
    return (
        [(details, "reason")] if details is not None and details.get("reason") is not None else []
    )


def replace_finish(body: Body, value: str) -> JsonObject:
    """Change the final response's incomplete_details.reason, when present."""
    if body.kind == "sse":
        reverse_index = finish_index(list(reversed(body.parts)))
        if reverse_index is None:
            raise ValueError("the stream has no final response event")
        index = len(body.parts) - reverse_index - 1
        changed = replace_finish_fields(
            Body("sse", None, (body.parts[index],)), value, _finish_fields
        )
        parts = list(body.parts)
        parts[index] = cast("list[str]", changed["sse"])[0]
        return {"sse": cast("list[JsonValue]", parts)}
    return replace_finish_fields(body, value, _finish_fields)


# --- Truth ----------------------------------------------------------------------------------------


@dataclass
class _Reading:
    """What the response said, collected from output items or from the stream's deltas."""

    status: str | None = None
    raw_finish: str | None = None
    text: list[str] | None = None
    refusal: bool = False
    has_function_call: bool = False
    failed: bool = False
    tool_calls: dict[int, _ToolCall] = field(default_factory=dict)
    usage: JsonObject | None = None
    error: JsonObject | None = None
    stream_completed: bool | None = None

    def add_text(self, text: JsonValue) -> None:
        if isinstance(text, str):
            if self.text is None:
                self.text = []
            self.text.append(text)


def _objects(value: JsonValue) -> list[JsonObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _read_message(payload: JsonObject) -> _Reading:
    status = json_str(payload, "status")
    reason = json_str(json_object(payload.get("incomplete_details")), "reason")
    reading = _Reading(
        status=status,
        raw_finish=reason if reason is not None else status,
        usage=json_object(payload.get("usage")),
        failed=status == "failed",
    )
    if payload.get("error") is not None:
        reading.error = json_object(payload["error"]) or {"message": str(payload["error"])}
    for index, item in enumerate(_objects(payload.get("output"))):
        if item.get("type") == "message":
            for part in _objects(item.get("content")):
                if part.get("type") == "output_text":
                    reading.add_text(part.get("text"))
                elif part.get("type") == "refusal":
                    reading.refusal = True
        elif item.get("type") == "function_call":
            reading.has_function_call = True
            call = reading.tool_calls.setdefault(index, _ToolCall())
            call.add(json_str(item, "call_id"), json_str(item, "name"), json_str(item, "arguments"))
    return reading


def _read_stream(exchange: Exchange) -> _Reading:
    reading = _Reading(stream_completed=False)
    for i, event in enumerate(sse_events(body_text(exchange))):
        try:
            chunk = json_object(parse_json(event.data.encode()))
        except (ValueError, RecursionError) as error:
            raise ValueError(f"event {i}: the data is not JSON: {error}") from None
        if chunk is None:
            raise ValueError(f"event {i}: the data is not a JSON object")
        kind = chunk.get("type")
        if kind in _FINAL:
            final = _read_message(json_object(chunk.get("response")) or {})
            reading.status, reading.raw_finish = final.status, final.raw_finish
            reading.usage = final.usage
            reading.error = final.error or reading.error
            reading.refusal = final.refusal or reading.refusal
            reading.has_function_call = final.has_function_call
            reading.failed = kind == "response.failed" or final.failed
            reading.stream_completed = exchange.end == "clean"
        elif kind == "error":
            # The event's own ``type`` names the event, not the error.
            reading.error = json_object(chunk.get("error")) or {
                key: value for key, value in chunk.items() if key != "type"
            }
        elif kind == "response.output_text.delta":
            reading.add_text(chunk.get("delta"))
        elif kind == "response.refusal.delta":
            reading.refusal = True
        elif kind == "response.content_part.added":
            reading.refusal = (
                reading.refusal or (json_object(chunk.get("part")) or {}).get("type") == "refusal"
            )
        elif kind == "response.output_item.added":
            item = json_object(chunk.get("item")) or {}
            reading.refusal = reading.refusal or _read_message({"output": [item]}).refusal
            if item.get("type") == "function_call":
                index = json_int(chunk, "output_index")
                call = reading.tool_calls.setdefault(0 if index is None else index, _ToolCall())
                call.add(json_str(item, "call_id"), json_str(item, "name"), None)
        elif kind == "response.function_call_arguments.delta":
            index = json_int(chunk, "output_index")
            call = reading.tool_calls.setdefault(0 if index is None else index, _ToolCall())
            call.add(None, None, json_str(chunk, "delta"))
    return reading


def _usage_truth(usage: JsonObject | None) -> JsonObject:
    input_tokens = json_int(usage, "input_tokens")
    output_tokens = json_int(usage, "output_tokens")
    total = json_int(usage, "total_tokens")
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    input_details = json_object((usage or {}).get("input_tokens_details"))
    output_details = json_object((usage or {}).get("output_tokens_details"))
    return {
        "input": input_tokens,
        "cached_input": json_int(input_details, "cached_tokens"),
        "output": output_tokens,
        "reasoning": json_int(output_details, "reasoning_tokens"),
        "total": total,
        "alternatives": {},
        "raw": usage,
    }


def _asked_for_json(request: JsonObject | None) -> bool:
    text = json_object((request or {}).get("text")) or {}
    return json_str(json_object(text.get("format")), "type") in _JSON_FORMATS


def extract_truth(exchanges: Sequence[Exchange], request: JsonObject | None) -> JsonObject:
    """The canonical truth of the last exchange. Raises ValueError for a body it can't read."""
    if not exchanges:
        raise ValueError("no exchanges")
    exchange = exchanges[-1]
    error_status = exchange.status if exchange.status >= 400 else None
    if is_event_stream(exchange) and error_status is None:
        reading = _read_stream(exchange)
    else:
        payload, text = _payload(exchange)
        if payload is not None:
            reading = _read_message(payload)
        elif error_status is not None:
            reading = _Reading(error={"message": text or f"HTTP {error_status}"})
        elif exchange.end == "abort":
            reading = _Reading()  # cut off before it was a response: nothing was delivered
        else:
            raise ValueError("the body is not a JSON object")
        if error_status is not None and reading.error is None:
            reading.error = {"message": f"HTTP {error_status}"}
    error = None if reading.error is None else _error_truth(reading.error, error_status)

    if error is not None or reading.failed:
        finish = "error"
    elif reading.refusal:
        finish = "refusal"
    elif reading.status is None:
        finish = "none"
    elif reading.status == "incomplete":
        finish = FINISH.get(reading.raw_finish or "", "other")
    elif reading.status == "completed":
        finish = "tool_calls" if reading.has_function_call else "stop"
    else:
        finish = "other"
    text = None if reading.text is None else "".join(reading.text)
    structured: JsonValue = None
    if finish == "stop" and text is not None and _asked_for_json(request):
        structured = _parse(text)[0]
    return {
        "finish": finish,
        "raw_finish": reading.raw_finish,
        "text": text,
        "tool_calls": [reading.tool_calls[i].truth() for i in sorted(reading.tool_calls)],
        "structured": structured,
        "error": error,
        "stream_completed": reading.stream_completed,
        "usage": _usage_truth(reading.usage),
    }
