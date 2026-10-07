"""Provider modules: how to call each wire API, and how to read the truth from its responses.

Each module builds the native request for a provider-neutral call and extracts the canonical
truth from recorded exchanges. The truth extractors are the reference parsers: the recorder writes
their result into each fixture, and ``wiretruth lint`` recomputes it.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from wiretruth.formats import Call, Exchange, JsonObject, JsonValue


@dataclass(frozen=True)
class RawRequest:
    """A native request, without credentials. ``path`` is relative to the API base URL."""

    method: str
    path: str
    headers: Mapping[str, str]
    body: JsonObject


class Provider(Protocol):
    """What a provider module exports."""

    ID: str
    API_BASE: str  # where the recorder sends requests, such as https://api.openai.com/v1
    KEY_ENV: str  # the environment variable that holds the API key
    DEFAULT_MODEL: str

    def build_request(self, call: Call, model: str) -> RawRequest: ...

    def auth_headers(self, key: str) -> dict[str, str]: ...

    def returned_model(self, exchange: Exchange) -> str | None: ...

    def extract_truth(
        self, exchanges: Sequence[Exchange], request: JsonObject | None
    ) -> JsonObject: ...


# --- Helpers for extractors -----------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """One server-sent event: its ``event`` field (None when absent) and its data."""

    type: str | None
    data: str


_LINE = re.compile(r"\r\n|\r|\n")


def is_event_stream(exchange: Exchange) -> bool:
    """Whether the response is a server-sent event stream."""
    if exchange.body.kind in ("sse", "json"):
        return exchange.body.kind == "sse"
    content_type = exchange.headers.get("content-type", "")
    return content_type.split(";")[0].strip().lower() == "text/event-stream"


def body_text(exchange: Exchange) -> str:
    """The body as text, for every form but ``json``. Raises ValueError if it isn't UTF-8."""
    body = exchange.body
    if body.kind == "json":
        raise ValueError("a json body has no text form")
    if body.kind == "text":
        return cast(str, body.data)
    if body.kind == "chunks_b64":
        return b"".join(base64.b64decode(part) for part in body.parts).decode("utf-8")
    newline = "\n" if body.kind == "ndjson" else ""
    return "".join(part + newline for part in body.parts)


def sse_events(text: str) -> list[Event]:
    """Parse an event stream the way the HTML standard does (§9.2.6).

    Lines starting with ``:`` are comments; a ``data`` field's single leading space is dropped;
    several ``data`` lines join with a newline; an event without data isn't dispatched. An event
    that the stream ends before its blank line is dropped too, as a browser would drop it.
    """
    events: list[Event] = []
    data: list[str] = []
    event_type: str | None = None
    lines = _LINE.split(text.removeprefix("﻿"))
    for line in lines[:-1]:  # the last piece has no line ending: it never completes
        if not line:
            if data:
                events.append(Event(event_type, "\n".join(data)))
            data, event_type = [], None
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            data.append(value)
        elif name == "event":
            event_type = value
    return events


def json_object(value: JsonValue) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def json_int(obj: JsonObject | None, key: str) -> int | None:
    value = None if obj is None else obj.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def json_str(obj: JsonObject | None, key: str) -> str | None:
    value = None if obj is None else obj.get(key)
    return value if isinstance(value, str) else None


def _registry() -> dict[str, Provider]:
    from wiretruth.providers import openai_chat

    return {openai_chat.ID: openai_chat}


PROVIDERS: Mapping[str, Provider] = _registry()
