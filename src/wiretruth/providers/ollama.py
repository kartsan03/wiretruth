"""ollama: native Ollama Chat, ``POST /api/chat`` (JSON or NDJSON).

Truth comes from the last exchange, which is the response the caller ends up with.

- ``finish``: ``error`` for an HTTP error or an ``error`` key. Without ``done: true``,
  finish is ``none``, including an aborted response. Otherwise tool calls mean ``tool_calls``;
  ``done_reason`` maps ``stop`` and ``length`` to themselves, other strings to ``other``,
  and a missing reason to ``none``.
- ``raw_finish``: ``done_reason``, or null.
- ``text``: ``message.content`` pieces joined, or the non-stream content; null when no content
  was sent, and an empty string when empty content was sent. Thinking contributes no text.
- ``tool_calls``: whole ``message.tool_calls`` entries in wire order, using the wire id or null,
  function name and argument object. Arguments always parse; their compact JSON is stored as
  ``arguments_raw`` because the provider sends no raw argument text. There are no partial calls.
- ``structured``: parsed content only when the request has ``format`` and finish is ``stop``.
  Duplicate keys use their last value.
- ``stream_completed``: true when a ``done: true`` line arrived and the exchange ended cleanly;
  false for a stream without it or an aborted stream; null for a non-stream body.
- ``usage``: ``prompt_eval_count`` is input (including cached input), ``prompt_eval_cached_count``
  is cached input, ``eval_count`` is output; total is input + output when both exist. Reasoning
  is null, alternatives are empty, and raw usage keeps only the three counters that are present.
- ``error``: the native string as message, with null type and code; status is the HTTP status
  for an error response and null for an in-band error line.

A non-stream body cut off before it parses delivered nothing: its finish is ``none``. Other
non-object bodies, non-JSON lines and invalid UTF-8 raise ValueError.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from wiretruth.formats import Body, Call, Exchange, JsonObject, JsonValue, parse_json
from wiretruth.providers import (
    RawRequest,
    body_text,
    json_int,
    json_object,
    json_str,
    replace_finish_fields,
)
from wiretruth.providers.openai_chat import _parse, _payload
from wiretruth.replay import BASE_PATHS

ID = "ollama"
API_BASE = "http://127.0.0.1:11434" + BASE_PATHS[ID]
KEY_ENV = ""
DEFAULT_MODEL = "qwen3.5:0.8b"
PATH = "/api/chat"
_COUNTERS = ("prompt_eval_count", "prompt_eval_cached_count", "eval_count")


def build_request(call: Call, model: str) -> RawRequest:
    unknown = sorted(set(call.extra) - {"include_usage"})
    if unknown:
        raise ValueError(f"extra field {unknown[0]!r} has no meaning for {ID}")
    if call.tool_choice not in (None, "auto"):
        raise ValueError(f"tool_choice has no meaning for {ID}")
    body: JsonObject = {
        "model": model,
        "messages": [
            {"role": message.role, "content": message.content} for message in call.messages
        ],
        "stream": call.stream,
    }
    options: JsonObject = {}
    if call.max_output_tokens is not None:
        options["num_predict"] = call.max_output_tokens
    if call.stop is not None:
        options["stop"] = list(call.stop)
    if options:
        body["options"] = options
    if call.output_schema is not None:
        body["format"] = call.output_schema
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
    if call.reasoning is not None:
        body["think"] = call.reasoning.effort != "none"
    accept = "application/x-ndjson" if call.stream else "application/json"
    return RawRequest(
        method="POST",
        path=PATH,
        headers={"Content-Type": "application/json", "Accept": accept},
        body=body,
    )


def auth_headers(key: str) -> dict[str, str]:
    return {}


def requested_stream(path: str, body: JsonValue) -> bool:
    return path == PATH and isinstance(body, dict) and body.get("stream", True) is True


def _is_stream(exchange: Exchange) -> bool:
    if exchange.body.kind in ("ndjson", "json"):
        return exchange.body.kind == "ndjson"
    return (
        exchange.headers.get("content-type", "").split(";")[0].strip().lower()
        == "application/x-ndjson"
    )


def _line_object(line: str) -> JsonObject:
    payload = json_object(parse_json(line.encode()))
    if payload is None:
        raise ValueError("the NDJSON line is not a JSON object")
    return payload


def _lines(exchange: Exchange) -> list[str]:
    lines = body_text(exchange).split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def returned_model(exchange: Exchange) -> str | None:
    if not _is_stream(exchange):
        return json_str(_payload(exchange)[0], "model")
    try:
        lines = _lines(exchange)
        return json_str(_line_object(lines[-1]), "model") if lines else None
    except (ValueError, RecursionError):
        return None


# --- Transforms -----------------------------------------------------------------------------------


def finish_index(events: Sequence[str], *, kind: str = "ndjson") -> int | None:
    if kind != "ndjson":
        raise ValueError("Ollama finish events need an ndjson body")
    for index, event in enumerate(events):
        if _line_object(event).get("done") is True:
            return index
    return None


def error_event(error: JsonObject, *, kind: str = "ndjson") -> str:
    if kind != "ndjson":
        raise ValueError("Ollama error events need an ndjson body")
    if not isinstance(error.get("error"), str):
        raise ValueError("Ollama error events need an error string")
    # Stored NDJSON lines have no newline; replay adds it.
    return json.dumps(error, ensure_ascii=False, separators=(",", ":"))


def _finish_fields(payload: JsonObject, stream: bool) -> list[tuple[JsonObject, str]]:
    if (not stream or payload.get("done") is True) and isinstance(payload.get("done_reason"), str):
        return [(payload, "done_reason")]
    return []


def replace_finish(body: Body, value: str) -> JsonObject:
    return replace_finish_fields(body, value, _finish_fields, kind="ndjson")


# --- Truth ----------------------------------------------------------------------------------------


def _tool_truth(value: JsonValue) -> JsonObject:
    call = json_object(value)
    function = json_object((call or {}).get("function")) or {}
    arguments = json_object(function.get("arguments"))
    if arguments is None:
        raise ValueError("Ollama tool arguments are not a JSON object")
    return {
        "id": json_str(call, "id"),
        "name": json_str(function, "name") or "",
        # The provider sends an object, not raw argument text.
        "arguments_raw": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        "arguments_parsed_ok": True,
        "arguments": arguments,
    }


def extract_truth(exchanges: Sequence[Exchange], request: JsonObject | None) -> JsonObject:
    if not exchanges:
        raise ValueError("no exchanges")
    exchange = exchanges[-1]
    status = exchange.status if exchange.status >= 400 else None
    stream = _is_stream(exchange)
    if stream:
        payloads = [_line_object(line) for line in _lines(exchange)]
    else:
        payload, text = _payload(exchange)
        if payload is None:
            if status is not None:
                payload = {"error": text or f"HTTP {status}"}
            elif exchange.end == "abort":
                payload = {}
            else:
                raise ValueError("the body is not a JSON object")
        payloads = [payload]
    done = False
    raw_finish = None
    pieces: list[str] = []
    has_content = False
    calls: list[JsonValue] = []
    counters: JsonObject = {}
    error: JsonObject | None = None
    for payload in payloads:
        done = done or payload.get("done") is True
        if (reason := json_str(payload, "done_reason")) is not None:
            raw_finish = reason
        if "error" in payload:
            error = {
                "message": json_str(payload, "error") or "",
                "type": None,
                "code": None,
                "status": status,
            }
        message = json_object(payload.get("message")) or {}
        content = json_str(message, "content")
        if content is not None:
            pieces.append(content)
            has_content = True
        tools = message.get("tool_calls")
        if isinstance(tools, list):
            calls.extend(_tool_truth(tool) for tool in tools)
        counters.update({key: payload[key] for key in _COUNTERS if key in payload})
    if status is not None and error is None:
        error = {"message": f"HTTP {status}", "type": None, "code": None, "status": status}
    if error is not None:
        finish = "error"
    elif not done:
        finish = "none"
    elif calls:
        finish = "tool_calls"
    elif raw_finish is None:
        finish = "none"
    else:
        finish = raw_finish if raw_finish in ("stop", "length") else "other"
    text = "".join(pieces) if has_content else None
    structured: JsonValue = None
    if finish == "stop" and text is not None and request is not None and "format" in request:
        structured = _parse(text)[0]
    input_tokens = json_int(counters, "prompt_eval_count")
    output_tokens = json_int(counters, "eval_count")
    return {
        "finish": finish,
        "raw_finish": raw_finish,
        "text": text,
        "tool_calls": calls,
        "structured": structured,
        "error": error,
        "stream_completed": done and exchange.end == "clean" if stream else None,
        "usage": {
            "input": input_tokens,
            "cached_input": json_int(counters, "prompt_eval_cached_count"),
            "output": output_tokens,
            "reasoning": None,
            "total": input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None,
            "alternatives": {},
            "raw": counters,
        },
    }
