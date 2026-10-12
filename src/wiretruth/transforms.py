"""Deterministic changes to recorded or documented fixtures, with recomputed truth."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from itertools import pairwise
from typing import cast

from wiretruth.formats import (
    FormatError,
    JsonObject,
    JsonValue,
    build_exchange,
    validate,
)
from wiretruth.providers import PROVIDERS, body_text, json_object

# At the default 5 ms replay gap, 100 chunks add less than half a second, well below
# the runner's 20 s cell timeout. Small byte sizes on large bodies would take seconds.
MAX_CHUNKS = 100


@dataclass(frozen=True)
class Transform:
    args: frozenset[str]
    rationale: str
    evidence_url: str


TRANSFORMS = {
    "cut_stream_after": Transform(
        frozenset({"n", "end"}),
        "The connection closes before the stream has delivered every event.",
        "https://github.com/openai/openai-python/issues/2065",
    ),
    "strip_finish": Transform(
        frozenset(),
        "The stream ends cleanly without its finish signal.",
        "https://github.com/pydantic/pydantic-ai/issues/6610",
    ),
    "inject_error_event": Transform(
        frozenset({"after", "error"}),
        "The provider reports an error after starting a successful HTTP stream.",
        "https://github.com/pydantic/pydantic-ai/pull/9321",
    ),
    "rechunk_inside_utf8": Transform(
        frozenset({"every", "points"}),
        "A network chunk boundary splits a multi-byte UTF-8 character.",
        "https://github.com/langchain-ai/langchainjs/issues/5285",
    ),
    "replace_finish_value": Transform(
        frozenset({"value"}),
        "The provider sends a finish value that a library may not recognize.",
        "https://github.com/BerriAI/litellm/issues/21347",
    ),
    "prepend_http_error": Transform(
        frozenset({"status", "headers", "body"}),
        "An HTTP error precedes the original response when the client retries.",
        "https://developers.openai.com/api/docs/guides/rate-limits",
    ),
}


def _integer(value: JsonValue, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _stream(body: JsonObject) -> tuple[str, list[JsonValue]]:
    kind = next(iter(body))
    if kind not in ("sse", "ndjson"):
        raise ValueError("this transform needs an sse or ndjson body")
    return kind, cast("list[JsonValue]", body[kind])


def _rechunk(exchange: JsonObject, args: JsonObject) -> JsonObject:
    parsed = build_exchange(exchange)
    if parsed.body.kind not in ("sse", "ndjson", "text"):
        raise ValueError("rechunk_inside_utf8 needs an sse, ndjson or text body")
    raw = body_text(parsed).encode("utf-8")
    points: Sequence[int]
    if "every" in args:
        every = args["every"]
        if type(every) is not int or every < 1:
            raise ValueError("every must be an integer of at least 1")
        points = range(every, len(raw), every)
    else:
        given = args["points"]
        if not isinstance(given, list):
            raise ValueError("points must be a list of strictly increasing byte offsets")
        offsets: list[int] = []
        previous = 0
        for point in given:
            previous = _integer(point, "each point", previous + 1, len(raw) - 1)
            offsets.append(previous)
        points = offsets
    if len(points) >= MAX_CHUNKS:
        raise ValueError(f"rechunk_inside_utf8 allows at most {MAX_CHUNKS} chunks")
    if not any(raw[point] & 0xC0 == 0x80 for point in points):
        raise ValueError("no chunk boundary falls inside a multi-byte UTF-8 sequence")
    edges = [0, *points, len(raw)]
    return {
        "chunks_b64": [
            base64.b64encode(raw[start:stop]).decode("ascii") for start, stop in pairwise(edges)
        ]
    }


def derive(
    source: JsonObject,
    *,
    transform: str,
    out: str,
    args: JsonObject,
    rationale: str | None = None,
    evidence_url: str | None = None,
) -> JsonObject:
    """Return a validated derived document without changing the source or arguments.

    ``source`` is a fixture document read with ``load_document``. Unsupported bodies,
    providers and arguments raise ValueError; invalid documents raise FormatError.
    """
    if problems := validate("fixture", source, "source"):
        raise FormatError(problems)
    if source["tier"] not in ("recorded", "documented"):
        raise ValueError("the source must be recorded or documented, not derived")
    provider_id, _, scenario = out.partition("/")
    if provider_id != source["provider"]:
        raise ValueError("the output provider must equal the source provider")
    if provider_id not in PROVIDERS:
        raise ValueError(f"provider {provider_id!r} does not support transforms")
    if transform not in TRANSFORMS:
        raise ValueError(f"unknown transform {transform!r}")
    spec = TRANSFORMS[transform]
    if not isinstance(args, dict):
        raise ValueError("args must be a JSON object")
    expected = [{"every"}, {"points"}] if transform == "rechunk_inside_utf8" else [spec.args]
    if set(args) not in expected:
        raise ValueError(
            f"{transform} requires exactly "
            + " or ".join(", ".join(sorted(keys)) or "{}" for keys in expected)
        )
    args = deepcopy(args)
    exchanges = deepcopy(cast("list[JsonObject]", source["exchanges"]))
    response = cast(JsonObject, exchanges[-1]["response"])
    body = cast(JsonObject, response["body"])
    provider = PROVIDERS[provider_id]

    if transform == "cut_stream_after":
        kind, events = _stream(body)
        n = _integer(args["n"], "n", 1, len(events) - 1)
        if args["end"] not in ("clean", "abort"):
            raise ValueError("end must be clean or abort")
        response["body"] = {kind: events[:n]}
        response["end"] = args["end"]
    elif transform == "strip_finish":
        kind, events = _stream(body)
        index = provider.finish_index(cast("list[str]", events), kind=kind)
        if index is None:
            raise ValueError("the stream has no finish event")
        if index == 0:
            raise ValueError("removing the finish would leave no events")
        response["body"] = {kind: events[:index]}
        response["end"] = "clean"
    elif transform == "inject_error_event":
        kind, events = _stream(body)
        after = _integer(args["after"], "after", 1, len(events) - 1)
        error = args["error"]
        if not isinstance(error, dict):
            raise ValueError("error must be a JSON object with the event's complete data")
        response["body"] = {kind: [*events[:after], provider.error_event(error, kind=kind)]}
        response["end"] = "clean"
    elif transform == "rechunk_inside_utf8":
        response["body"] = _rechunk(exchanges[-1], args)
    elif transform == "replace_finish_value":
        value = args["value"]
        if not isinstance(value, str) or not value:
            raise ValueError("value must be a non-empty string")
        response["body"] = provider.replace_finish(build_exchange(exchanges[-1]).body, value)
    else:  # prepend_http_error
        status = _integer(args["status"], "status", 400, 599)
        headers = args["headers"]
        if not isinstance(headers, dict) or any(
            key not in ("content-type", "retry-after") or not isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError("headers must contain only content-type and retry-after string values")
        exchanges.insert(
            0,
            {
                "match": deepcopy(exchanges[0]["match"]),
                "response": {
                    "status": status,
                    "headers": deepcopy(headers),
                    "body": {"json": deepcopy(args["body"])},
                    "end": "clean",
                },
            },
        )

    if exchanges == source["exchanges"]:
        raise ValueError("the transform would leave the exchanges unchanged")
    original = cast(JsonObject, source["provenance"])
    provenance: JsonObject = {
        key: deepcopy(original[key])
        for key in ("requested_model", "returned_model", "request")
        if key in original
    }
    truth = provider.extract_truth(
        [build_exchange(item) for item in exchanges], json_object(provenance.get("request"))
    )
    if transform == "inject_error_event" and truth["error"] is None:
        raise ValueError("the provider does not read the injected payload as an error")
    if transform == "replace_finish_value" and truth["raw_finish"] != args["value"]:
        raise ValueError("the provider does not read the replacement as raw_finish")
    document: JsonObject = {
        "format": "wiretruth.fixture/v1",
        "id": out,
        "scenario": scenario.split("@", 1)[0],
        "provider": provider_id,
        "tier": "derived",
        "provenance": provenance,
        "derived_from": {
            "fixture": source["id"],
            "transform": transform,
            "args": args,
            "rationale": spec.rationale if rationale is None else rationale,
            "evidence_url": spec.evidence_url if evidence_url is None else evidence_url,
        },
        "exchanges": cast("list[JsonValue]", exchanges),
        "truth": truth,
    }
    if problems := validate("fixture", document, out):
        raise FormatError(problems)
    return document
