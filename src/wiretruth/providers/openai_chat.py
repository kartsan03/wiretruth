"""openai-chat: OpenAI Chat Completions, ``POST /v1/chat/completions``.

Truth comes from ``choices[0]`` (the index-0 choice in a stream), and from the last exchange,
which is the response the caller ends up with.

- ``finish``: ``error`` for an error response or an in-band error event; ``refusal`` when the
  message carries a ``refusal``; otherwise ``finish_reason`` mapped through ``FINISH``, ``other``
  for a value not in it, and ``none`` when no ``finish_reason`` was sent.
- ``text``: ``message.content``, or the streamed ``delta.content`` pieces joined; null when no
  content was sent.
- ``tool_calls``: ``message.tool_calls`` (and the legacy ``function_call``); streamed calls are
  put together by ``index``, with their ``arguments`` pieces joined.
- ``structured``: the parsed text, only when the request asked for JSON (``response_format``) and
  the finish is ``stop``.
- ``stream_completed``: true when ``data: [DONE]`` arrived and the response ended cleanly; null
  for a response that wasn't streamed.
- ``usage``: the ``usage`` object (in a stream, the chunk that carries it, which only comes with
  ``stream_options.include_usage``). ``total`` is ``total_tokens``, or input + output.
- ``error``: the ``error`` object; ``status`` is the HTTP status of an error response, and null
  for an error event inside a stream. An error response without an ``error`` object gets its body
  text, or ``HTTP <status>``, as the message.

A response that isn't streamed and is cut off before its body parses delivered nothing: its finish
is ``none``. Any other body that isn't a JSON object, or a stream event whose data isn't one, is
an error: the extractor can't say what happened.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

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
from wiretruth.replay import BASE_PATHS

ID = "openai-chat"
API_BASE = "https://api.openai.com" + BASE_PATHS[ID]  # the path libraries use on replay
KEY_ENV = "OPENAI_API_KEY"
DEFAULT_MODEL = "gpt-6-luna"
PATH = "/chat/completions"

FINISH: dict[str, str] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}
_JSON_FORMATS = ("json_schema", "json_object")
_EXTRA = frozenset({"include_usage"})


# --- Request --------------------------------------------------------------------------------------


def build_request(call: Call, model: str) -> RawRequest:
    unknown = sorted(set(call.extra) - _EXTRA)
    if unknown:
        raise ValueError(f"extra field {unknown[0]!r} has no meaning for {ID}")
    body: JsonObject = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content} for message in call.messages
        ],
    }
    if call.stream:
        body["stream"] = True
        if call.extra.get("include_usage") is True:
            body["stream_options"] = {"include_usage": True}
    if call.max_output_tokens is not None:
        body["max_completion_tokens"] = call.max_output_tokens
    if call.stop is not None:
        body["stop"] = list(call.stop)
    if call.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in call.tools
        ]
    if isinstance(call.tool_choice, ForcedTool):
        body["tool_choice"] = {"type": "function", "function": {"name": call.tool_choice.name}}
    elif call.tool_choice is not None:
        body["tool_choice"] = call.tool_choice
    if call.output_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": call.output_schema, "strict": True},
        }
    if call.reasoning is not None:
        body["reasoning_effort"] = call.reasoning.effort
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
            model = json_str(json_object(parse_json(event.data.encode())), "model")
        except (ValueError, RecursionError):
            continue
        if model is not None:
            return model
    return None


# --- Transforms -----------------------------------------------------------------------------------


def _finish_fields(payload: JsonObject, stream: bool) -> list[tuple[JsonObject, str]]:
    choices = payload.get("choices")
    return (
        [
            (choice, "finish_reason")
            for choice in choices
            if isinstance(choice, dict) and choice.get("finish_reason") is not None
        ]
        if isinstance(choices, list)
        else []
    )


def finish_index(events: Sequence[str], *, kind: str = "sse") -> int | None:
    """The first chunk with a non-null finish_reason on any choice."""
    if kind != "sse":
        raise ValueError("Chat Completions finish events need an sse body")
    for index, event in enumerate(events):
        payload = event_object(event, terminator="[DONE]")
        if payload is not None and _finish_fields(payload, True):
            return index
    return None


def error_event(error: JsonObject, *, kind: str = "sse") -> str:
    """Frame the complete error data as a Chat Completions event."""
    if kind != "sse":
        raise ValueError("Chat Completions error events need an sse body")
    return "data: " + json.dumps(error, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def replace_finish(body: Body, value: str) -> JsonObject:
    """Change every choice's non-null finish_reason."""
    return replace_finish_fields(body, value, _finish_fields, terminator="[DONE]")


# --- Truth ----------------------------------------------------------------------------------------


@dataclass
class _ToolCall:
    id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)
    has_arguments: bool = False

    def add(self, id: str | None, name: str | None, arguments: str | None) -> None:
        self.id = self.id or id
        self.name = self.name or name
        if arguments is not None:
            self.arguments.append(arguments)
            self.has_arguments = True

    def truth(self) -> JsonObject:
        raw = "".join(self.arguments) if self.has_arguments else None
        parsed, ok = _parse(raw)
        return {
            "id": self.id,
            "name": self.name or "",
            "arguments_raw": raw,
            "arguments_parsed_ok": ok,
            "arguments": parsed,
        }


@dataclass
class _Reading:
    """What the response said, collected from one message or from the stream's deltas."""

    raw_finish: str | None = None
    text: list[str] | None = None
    refusal: bool = False
    tool_calls: dict[int, _ToolCall] = field(default_factory=dict)
    usage: JsonObject | None = None
    error: JsonObject | None = None
    stream_completed: bool | None = None

    def add_text(self, content: JsonValue) -> None:
        if isinstance(content, str):
            if self.text is None:
                self.text = []
            self.text.append(content)

    def add_tool_call(self, index: int, call: JsonObject | None) -> None:
        function = json_object((call or {}).get("function"))
        self.tool_calls.setdefault(index, _ToolCall()).add(
            json_str(call, "id"), json_str(function, "name"), json_str(function, "arguments")
        )

    def add_function_call(self, call: JsonObject | None) -> None:
        # The legacy single function call has no id, and no index: it gets its own slot.
        if call is not None:
            self.tool_calls.setdefault(-1, _ToolCall()).add(
                None, json_str(call, "name"), json_str(call, "arguments")
            )


def _parse(raw: str | None) -> tuple[JsonValue, bool]:
    if raw is None:
        return None, False
    try:
        return parse_json(raw.encode(), duplicates="last"), True
    except (ValueError, RecursionError):
        return None, False


def _first_choice(payload: JsonObject) -> JsonObject | None:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        choice = json_object(choice)
        if choice is not None and choice.get("index", 0) == 0:
            return choice
    return None


def _read_message(payload: JsonObject) -> _Reading:
    reading = _Reading(usage=json_object(payload.get("usage")))
    choice = _first_choice(payload)
    if choice is None:
        return reading
    reading.raw_finish = json_str(choice, "finish_reason")
    message = json_object(choice.get("message")) or {}
    reading.add_text(message.get("content"))
    reading.refusal = bool(json_str(message, "refusal"))
    tool_calls = message.get("tool_calls")
    for i, call in enumerate(tool_calls if isinstance(tool_calls, list) else []):
        reading.add_tool_call(i, json_object(call))
    reading.add_function_call(json_object(message.get("function_call")))
    return reading


def _read_stream(exchange: Exchange) -> _Reading:
    reading = _Reading(stream_completed=False)
    for i, event in enumerate(sse_events(body_text(exchange))):
        if event.data == "[DONE]":
            reading.stream_completed = exchange.end == "clean"
            break
        try:
            chunk = json_object(parse_json(event.data.encode()))
        except (ValueError, RecursionError) as error:
            raise ValueError(f"event {i}: the data is not JSON: {error}") from None
        if chunk is None:
            raise ValueError(f"event {i}: the data is not a JSON object")
        if chunk.get("error") is not None:
            reading.error = json_object(chunk["error"]) or {"message": str(chunk["error"])}
            continue
        if json_object(chunk.get("usage")) is not None:
            reading.usage = json_object(chunk["usage"])
        choice = _first_choice(chunk)
        if choice is None:
            continue
        reading.raw_finish = json_str(choice, "finish_reason") or reading.raw_finish
        delta = json_object(choice.get("delta")) or {}
        reading.add_text(delta.get("content"))
        reading.refusal = reading.refusal or bool(json_str(delta, "refusal"))
        tool_calls = delta.get("tool_calls")
        for call in tool_calls if isinstance(tool_calls, list) else []:
            call = json_object(call)
            index = json_int(call, "index")
            reading.add_tool_call(0 if index is None else index, call)
        reading.add_function_call(json_object(delta.get("function_call")))
    return reading


def _error_truth(error: JsonObject, status: int | None) -> JsonObject:
    code = error.get("code")
    return {
        "status": status,
        "type": json_str(error, "type"),
        "code": None if code is None else str(code),
        "message": json_str(error, "message") or "",
    }


def _usage_truth(usage: JsonObject | None) -> JsonObject:
    input_tokens = json_int(usage, "prompt_tokens")
    output_tokens = json_int(usage, "completion_tokens")
    total = json_int(usage, "total_tokens")
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    prompt_details = json_object((usage or {}).get("prompt_tokens_details"))
    completion_details = json_object((usage or {}).get("completion_tokens_details"))
    return {
        "input": input_tokens,
        "cached_input": json_int(prompt_details, "cached_tokens"),
        "output": output_tokens,
        "reasoning": json_int(completion_details, "reasoning_tokens"),
        "total": total,
        "alternatives": {},
        "raw": usage,
    }


def _asked_for_json(request: JsonObject | None) -> bool:
    response_format = json_object((request or {}).get("response_format"))
    return json_str(response_format, "type") in _JSON_FORMATS


def _payload(exchange: Exchange) -> tuple[JsonObject | None, str | None]:
    """The body as a JSON object, if it is one, and as text, if it is UTF-8 text."""
    if exchange.body.kind == "json":
        return json_object(exchange.body.data), None
    try:
        text = body_text(exchange)
    except ValueError:
        return None, None
    try:
        return json_object(parse_json(text.encode())), text
    except (ValueError, RecursionError):
        return None, text


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
            reading = _Reading()  # cut off before it was a message: nothing was delivered
        else:
            raise ValueError("the body is not a JSON object")
        if payload is not None and payload.get("error") is not None:
            reading.error = json_object(payload["error"]) or {"message": str(payload["error"])}
        elif error_status is not None and reading.error is None:
            reading.error = {"message": f"HTTP {error_status}"}
    error = None if reading.error is None else _error_truth(reading.error, error_status)

    if error is not None:
        finish = "error"
    elif reading.refusal:
        finish = "refusal"
    elif reading.raw_finish is None:
        finish = "none"
    else:
        finish = FINISH.get(reading.raw_finish, "other")
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
