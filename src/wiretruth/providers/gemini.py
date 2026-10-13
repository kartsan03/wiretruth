"""gemini: GenerateContent, ``POST /v1beta/models/<model>:generateContent`` (JSON or SSE).

Truth comes from ``candidates[0]`` of the last exchange.

- ``finish``: HTTP errors or an ``error`` object mean ``error``; prompt blocks mean
  ``content_filter``. ``STOP`` means ``tool_calls`` when function calls were sent, otherwise
  ``stop``; ``MAX_TOKENS`` means ``length``. Filter reasons map through ``FINISH``; other strings
  mean ``other``, and no finish reason means ``none``. ``raw_finish`` is ``finishReason`` or null.
- ``text``: join text parts except those with ``thought: true``; null if none were sent, empty
  when empty text was sent. Missing content/parts and signature-only parts are normal.
- ``tool_calls``: whole functionCall parts in wire order, with the wire id or null. Object
  arguments always parse; ``arguments_raw`` is their compact JSON, since no raw text is sent.
- ``structured``: parsed text only for ``responseMimeType: application/json`` and finish stop.
- ``stream_completed``: a finishReason arrived and HTTP ended cleanly; null for non-stream.
  No separate terminator is required. An optional [DONE] contributes no completion evidence.
- ``usage``: the last usageMetadata object is a snapshot, even when several chunks carry it.
  Input includes cached input; output adds candidatesTokenCount and thoughtsTokenCount, treating
  an omitted counter as zero only when the other exists. Candidates alone are an alternative
  when present and different. Reasoning is thoughtsTokenCount; total and raw stay as sent.
- ``error``: message, status enum as type, stringified code, and HTTP status only for HTTP errors.

An aborted non-stream body that cannot parse delivered nothing. Other unreadable bodies raise
ValueError. SSE drops an unfinished event, including an aborted trailing UTF-8 sequence.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from urllib.parse import urlsplit

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
from wiretruth.providers.openai_chat import _parse, _payload
from wiretruth.replay import BASE_PATHS

ID = "gemini"
KEY_ENV = "GEMINI_API_KEY"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
API_BASE = "https://generativelanguage.googleapis.com" + BASE_PATHS[ID]
FINISH = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_RECITATION": "content_filter",
}
_EFFORT = {
    "none": "MINIMAL",
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}


def build_request(call: Call, model: str) -> RawRequest:
    unknown = sorted(set(call.extra) - {"include_usage"})
    if unknown:
        raise ValueError(f"extra field {unknown[0]!r} has no meaning for {ID}")
    body: JsonObject = {
        "contents": [
            {
                "role": "model" if message.role == "assistant" else message.role,
                "parts": [{"text": message.content}],
            }
            for message in call.messages
            if message.role != "system"
        ]
    }
    system: list[JsonValue] = [
        {"text": message.content} for message in call.messages if message.role == "system"
    ]
    if system:
        body["systemInstruction"] = {"parts": system}
    config: JsonObject = {}
    if call.max_output_tokens is not None:
        config["maxOutputTokens"] = call.max_output_tokens
    if call.stop is not None:
        config["stopSequences"] = list(call.stop)
    if call.output_schema is not None:
        config.update(responseMimeType="application/json", responseJsonSchema=call.output_schema)
    if call.reasoning is not None:
        config["thinkingConfig"] = {"thinkingLevel": _EFFORT[call.reasoning.effort]}
    if config:
        body["generationConfig"] = config
    if call.tools:
        body["tools"] = [
            {
                "functionDeclarations": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parametersJsonSchema": tool.parameters,
                    }
                    for tool in call.tools
                ]
            }
        ]
    if isinstance(call.tool_choice, ForcedTool):
        body["toolConfig"] = {
            "functionCallingConfig": {
                "mode": "ANY",
                "allowedFunctionNames": [call.tool_choice.name],
            }
        }
    elif call.tool_choice is not None:
        body["toolConfig"] = {
            "functionCallingConfig": {
                "mode": {"auto": "AUTO", "required": "ANY", "none": "NONE"}[call.tool_choice]
            }
        }
    method = "streamGenerateContent?alt=sse" if call.stream else "generateContent"
    return RawRequest(
        method="POST",
        path=f"/v1beta/models/{model}:{method}",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if call.stream else "application/json",
        },
        body=body,
    )


def auth_headers(key: str) -> dict[str, str]:
    return {"x-goog-api-key": key}


def requested_stream(path: str, body: JsonValue) -> bool:
    path = urlsplit(path).path
    return path.startswith("/v1beta/models/") and path.endswith(":streamGenerateContent")


def returned_model(exchange: Exchange) -> str | None:
    if not is_event_stream(exchange):
        return json_str(_payload(exchange)[0], "modelVersion")
    try:
        events = sse_events(body_text(exchange))
    except ValueError:
        return None
    for event in events:
        try:
            model = json_str(json_object(parse_json(event.data.encode())), "modelVersion")
        except (ValueError, RecursionError):
            continue
        if model is not None:
            return model
    return None


# --- Transforms -----------------------------------------------------------------------------------


def _candidate(payload: JsonObject) -> JsonObject:
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        return json_object(candidates[0]) or {}
    return {}


def _finish_fields(payload: JsonObject, stream: bool) -> list[tuple[JsonObject, str]]:
    candidate = _candidate(payload)
    return [(candidate, "finishReason")] if json_str(candidate, "finishReason") is not None else []


def finish_index(events: Sequence[str], *, kind: str = "sse") -> int | None:
    if kind != "sse":
        raise ValueError("Gemini finish events need an sse body")
    for index, event in enumerate(events):
        payload = event_object(event, terminator="[DONE]")
        if payload is not None and _finish_fields(payload, True):
            return index
    return None


def error_event(error: JsonObject, *, kind: str = "sse") -> str:
    if kind != "sse":
        raise ValueError("Gemini error events need an sse body")
    if json_object(error.get("error")) is None:
        raise ValueError("Gemini error events need an error object")
    return "data: " + json.dumps(error, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def replace_finish(body: Body, value: str) -> JsonObject:
    return replace_finish_fields(body, value, _finish_fields, terminator="[DONE]")


# --- Truth ----------------------------------------------------------------------------------------


def _tool_truth(function: JsonObject) -> JsonObject:
    arguments = json_object(function.get("args", {}))
    if arguments is None:
        raise ValueError("Gemini tool arguments are not a JSON object")
    return {
        "id": json_str(function, "id"),
        "name": json_str(function, "name") or "",
        "arguments_raw": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        "arguments_parsed_ok": True,
        "arguments": arguments,
    }


def _usage_truth(usage: JsonObject | None) -> JsonObject:
    candidates = json_int(usage, "candidatesTokenCount")
    thoughts = json_int(usage, "thoughtsTokenCount")
    output = (
        None if candidates is None and thoughts is None else (candidates or 0) + (thoughts or 0)
    )
    return {
        "input": json_int(usage, "promptTokenCount"),
        "cached_input": json_int(usage, "cachedContentTokenCount"),
        "output": output,
        "reasoning": thoughts,
        "total": json_int(usage, "totalTokenCount"),
        "alternatives": {"output": {"candidates_only": candidates}}
        if candidates is not None and output != candidates
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
            if event.data == "[DONE]":
                continue
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
    blocked = False
    pieces: list[str] = []
    calls: list[JsonValue] = []
    usage = None
    error: JsonObject | None = None
    for payload in payloads:
        if (wire_error := payload.get("error")) is not None:
            detail = json_object(wire_error) or {"message": str(wire_error)}
            code = detail.get("code")
            error = {
                "message": json_str(detail, "message") or "",
                "type": json_str(detail, "status"),
                "code": str(code) if code is not None else None,
                "status": status,
            }
        feedback = json_object(payload.get("promptFeedback"))
        blocked = blocked or json_str(feedback, "blockReason") is not None
        if (metadata := json_object(payload.get("usageMetadata"))) is not None:
            usage = metadata
        candidate = _candidate(payload)
        if (reason := json_str(candidate, "finishReason")) is not None:
            raw_finish = reason
        content = json_object(candidate.get("content")) or {}
        parts = content.get("parts")
        for value in parts if isinstance(parts, list) else []:
            part = json_object(value) or {}
            if part.get("thought") is not True and (text := json_str(part, "text")) is not None:
                pieces.append(text)
            if (function := json_object(part.get("functionCall"))) is not None:
                calls.append(_tool_truth(function))
    if status is not None and error is None:
        error = {"message": f"HTTP {status}", "type": None, "code": None, "status": status}
    if error is not None:
        finish = "error"
    elif blocked:
        finish = "content_filter"
    elif raw_finish == "STOP" and calls:
        finish = "tool_calls"
    else:
        finish = "none" if raw_finish is None else FINISH.get(raw_finish, "other")
    text = "".join(pieces) if pieces else None
    config = json_object((request or {}).get("generationConfig"))
    structured: JsonValue = None
    if finish == "stop" and json_str(config, "responseMimeType") == "application/json":
        structured = _parse(text)[0]
    return {
        "finish": finish,
        "raw_finish": raw_finish,
        "text": text,
        "tool_calls": calls,
        "structured": structured,
        "stream_completed": raw_finish is not None and error is None and exchange.end == "clean"
        if stream
        else None,
        "usage": _usage_truth(usage),
        "error": error,
    }
