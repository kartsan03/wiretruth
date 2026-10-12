"""An ideal raw-HTTP client, using the same wire reader as the recorder."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from typing import cast

from wiretruth import __version__
from wiretruth.formats import (
    CallSpec,
    JsonObject,
    build_exchange,
    call_spec_from_document,
    parse_json,
)
from wiretruth.providers import PROVIDERS
from wiretruth.recorder import exchange_document, response_body, send

USAGE_FIELDS = ("input", "cached_input", "output", "reasoning", "total")


def empty_observation(spec: CallSpec | None) -> JsonObject:
    return {
        "format": "wiretruth.observation/v1",
        "library": {
            "name": "wiretruth",
            "version": __version__,
            "language": "python",
            "api": "providers.extract_truth",
        },
        "outcome": "returned",
        "unsupported_reason": None,
        "error": None,
        "finish": {"value": None, "raw": None, "surface": "absent", "source": None},
        "text": {"value": None, "surface": "absent"},
        "structured": {
            "attempted": spec is not None and spec.call.output_schema is not None,
            "returned_as_success": False,
            "value": None,
        },
        "tool_calls": [],
        "usage": {
            **dict.fromkeys(USAGE_FIELDS),
            "surface": dict.fromkeys(USAGE_FIELDS, "absent"),
        },
        "stream": {
            "used": spec is not None and spec.call.stream,
            "completed": None,
            "chunks_seen": None,
        },
        "notes": [],
        "elapsed_ms": 0,
    }


def observation_from_truth(spec: CallSpec, truth: JsonObject) -> JsonObject:
    obs = empty_observation(spec)
    error = truth["error"]
    if isinstance(error, dict):
        obs["outcome"] = "raised"
        obs["error"] = {
            "type": error["type"] or "ProviderError",
            "message": str(error["message"])[:500],
            "category": "api",
            "retry_after": None,
            "stack_top": None,
        }
    obs["finish"] = {
        "value": truth["finish"],
        "raw": truth["raw_finish"],
        "surface": "primary" if truth["finish"] is not None else "absent",
        "source": "truth.finish",
    }
    obs["text"] = {
        "value": truth["text"],
        "surface": "primary" if truth["text"] is not None else "absent",
    }
    success = (
        spec.call.output_schema is not None
        and truth["finish"] == "stop"
        and truth["structured"] is not None
        and error is None
    )
    obs["structured"] = {
        "attempted": spec.call.output_schema is not None,
        "returned_as_success": success,
        "value": truth["structured"] if success else None,
    }
    obs["tool_calls"] = truth["tool_calls"]
    usage = cast(JsonObject, truth["usage"])
    obs["usage"] = {
        **{name: usage[name] for name in USAGE_FIELDS},
        "surface": {
            name: "primary" if usage[name] is not None else "absent" for name in USAGE_FIELDS
        },
    }
    obs["stream"] = {
        "used": spec.call.stream,
        "completed": truth["stream_completed"],
        "chunks_seen": None,
    }
    return obs


def observe(spec: CallSpec) -> JsonObject:
    provider_id = os.environ["WIRETRUTH_PROVIDER"]
    provider = PROVIDERS[provider_id]
    request = provider.build_request(spec.call, spec.model)
    base_url = os.environ[f"WIRETRUTH_BASE_URL_{provider_id.upper().replace('-', '_')}"]
    response = send(provider, request, base_url, os.environ["WIRETRUTH_API_KEY"])
    body, _ = response_body(response, False)
    exchange = build_exchange(exchange_document(request, base_url, response, body))
    obs = observation_from_truth(spec, provider.extract_truth([exchange], request.body))
    events = body.get("sse", body.get("ndjson"))
    cast(JsonObject, obs["stream"])["chunks_seen"] = (
        len(events) if spec.call.stream and isinstance(events, list) else None
    )
    return obs


def main(observe_call: Callable[[CallSpec], JsonObject] = observe) -> None:
    started = time.monotonic()
    spec = None
    try:
        spec = call_spec_from_document(parse_json(sys.stdin.buffer.read()))
        obs = observe_call(spec)
    except Exception as error:
        obs = empty_observation(spec)
        obs["outcome"] = "raised"
        obs["error"] = {
            "type": f"{type(error).__module__}.{type(error).__qualname__}",
            "message": str(error)[:500],
            "category": "internal",
            "retry_after": None,
            "stack_top": None,
        }
    obs["elapsed_ms"] = (time.monotonic() - started) * 1000
    print(json.dumps(obs, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
