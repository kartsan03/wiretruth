import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path

import litellm


def text(value):
    if value is not None:
        obs["text"].update(value=(obs["text"]["value"] or "") + value, surface="primary")


def tool(index, id=None, name=None, arguments=None):
    stored = tool_calls.setdefault(index, dict(id=None, name="", arguments_raw=None))
    stored["id"] = stored["id"] or id
    stored["name"] = stored["name"] or name or ""
    if arguments is not None:
        stored["arguments_raw"] = (stored["arguments_raw"] or "") + arguments
    try:
        parsed, ok = json.loads(stored["arguments_raw"]), True
        json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError):
        parsed, ok = None, False
    stored.update(arguments_parsed_ok=ok, arguments=parsed)
    obs["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]


def usage(result):
    reported = result.get("usage") if isinstance(result, dict) else getattr(result, "usage", None)
    if reported is not None:
        for name, path in usage_fields.items():
            value = reported
            for attr in path.split("."):
                value = value.get(attr) if isinstance(value, dict) else getattr(value, attr, None)
            obs["usage"][name] = value
            obs["usage"]["surface"][name] = "primary" if value is not None else "absent"


def observe(result, streaming=False):
    if result.choices:
        choice = result.choices[0]
        if choice.finish_reason is not None:
            raw = choice.finish_reason
            native = (getattr(choice, "provider_specific_fields", None) or {}).get(
                "native_finish_reason"
            )
            obs["finish"].update(
                value=manifest["finish_map"].get(raw, "other"),
                raw=native if native is not None else raw,
                surface="primary",
                source="choices[0].finish_reason",
            )
        message = choice.delta if streaming else choice.message
        text(message.content)
        for index, item in enumerate(message.tool_calls or []):
            function = item.function
            tool(
                item.index if streaming else index,
                item.id,
                function.name if function else None,
                function.arguments if function else None,
            )
    usage(result)


def returned_error(error):
    # Error data is still a returned outcome, not a raised exception.
    obs["error"] = dict(
        type=f"{type(error).__module__}.{type(error).__qualname__}",
        message=(
            error.get("message", "") if isinstance(error, dict) else getattr(error, "message", "")
        )[:500],
        category="api",
        retry_after=None,
        stack_top=None,
    )
    obs["stream"]["completed"] = False if call["stream"] else None


def observe_responses(result, streaming=False):
    global refusal
    # LiteLLM's forward-compatible Responses fallback retains nested dictionaries.
    error = result.error
    result = result.model_dump(mode="json")
    has_tool = False
    for index, item in enumerate(result.get("output") or []):
        if item["type"] == "message":
            for part in item["content"]:
                if part["type"] == "refusal":
                    refusal = True
                elif part["type"] == "output_text" and not streaming:
                    text(part["text"])
        elif item["type"] == "function_call":
            has_tool = True
            if not streaming:
                tool(index, item["call_id"], item["name"], item["arguments"])
    reason = (result.get("incomplete_details") or {}).get("reason")
    raw = reason if reason is not None else result.get("status")
    if result.get("status") is not None:
        value = manifest["finish_map"].get(raw, "other")
        if result["status"] == "completed" and has_tool:
            value = "tool_calls"
        if refusal:
            value = "refusal"
        if result["status"] == "failed" or error is not None:
            value = "error"
            if error is not None:
                returned_error(error)
        obs["finish"].update(
            value=value,
            raw=raw,
            surface="primary",
            source="incomplete_details.reason" if reason is not None else "status",
        )
    usage(result)


def observe_event(event):
    global refusal
    kind = event.type
    if kind in ("response.completed", "response.incomplete", "response.failed"):
        observe_responses(event.response, streaming=True)
        obs["stream"]["completed"] = kind != "response.failed" and obs["error"] is None
    elif kind in ("response.created", "response.in_progress"):
        obs["stream"]["completed"] = False
    elif kind == "error":
        returned_error(getattr(event, "error", None) or event)
    elif kind == "response.output_text.delta":
        text(event.delta)
    elif kind == "response.refusal.delta" or (
        kind == "response.content_part.added" and event.part.type == "refusal"
    ):
        refusal = True
        obs["finish"].update(value="refusal", raw=None, surface="primary", source=kind)
    elif kind == "response.output_item.added" and event.item.type == "function_call":
        tool(event.output_index, event.item.call_id, event.item.name)
    elif kind == "response.function_call_arguments.delta":
        tool(event.output_index, arguments=event.delta)


def observe_error(error):
    frame = (traceback.extract_tb(error.__traceback__) or [None])[-1]
    if type(error).__module__ == "builtins" and frame and frame.filename == __file__:
        raise error  # shim bugs exit nonzero and grade HARNESS
    relative = frame.filename.replace("\\", "/").partition("/site-packages/")[2] if frame else ""
    types = [f"{cls.__module__}.{cls.__qualname__}" for cls in type(error).__mro__]
    category = next(
        (item["category"] for item in manifest["error_map"] if item["type"] in types),
        "internal" if type(error).__module__ == "builtins" and relative else "other",
    )
    obs["outcome"] = "raised"
    obs["error"] = dict(
        type=types[0],
        message=str(error)[:500],
        category=category,
        retry_after=None,
        stack_top=f"{relative}:{frame.lineno}" if relative else None,
    )
    obs["stream"]["completed"] = False if call["stream"] else None


started = time.monotonic()
call = json.load(sys.stdin)
manifest = json.loads(Path("wiretruth-shim.json").read_text())
provider = call["provider"]
if provider not in manifest["providers"] or provider != os.environ["WIRETRUTH_PROVIDER"]:
    sys.exit("litellm: provider not mapped yet")
responses = provider == "openai-responses"
refusal = False
tool_calls = {}
schema = call["output_schema"]
usage_fields = dict(
    input="input_tokens" if responses else "prompt_tokens",
    output="output_tokens" if responses else "completion_tokens",
    total="total_tokens",
    cached_input=("input_tokens_details" if responses else "prompt_tokens_details")
    + ".cached_tokens",
    reasoning=("output_tokens_details" if responses else "completion_tokens_details")
    + ".reasoning_tokens",
)
obs = {
    "format": "wiretruth.observation/v1",
    "library": dict(
        name="litellm",
        version=version("litellm"),
        language="python",
        api="litellm.responses" if responses else "litellm.completion",
    ),
    "outcome": "returned",
    **dict.fromkeys(("unsupported_reason", "error")),
    "finish": dict(value=None, raw=None, surface="absent", source=None),
    "text": dict(value=None, surface="absent"),
    "structured": dict(attempted=schema is not None, returned_as_success=False, value=None),
    "tool_calls": [],
    "usage": {**dict.fromkeys(usage_fields), "surface": dict.fromkeys(usage_fields, "absent")},
    "stream": dict(used=call["stream"], completed=None, chunks_seen=0 if call["stream"] else None),
    "notes": [],
}
unsupported = next(
    (
        entry
        for entry in manifest["unsupported"]
        if entry["scenario"] == call["scenario"] and provider in entry["providers"]
    ),
    None,
)
if unsupported:
    obs.update(
        outcome="unsupported",
        unsupported_reason=unsupported["reason"] + " " + unsupported["docs"],
    )
    obs["structured"]["attempted"] = False
else:
    kwargs = {}
    if call.get("tools"):
        kwargs["tools"] = [
            {"type": "function", **tool} if responses else {"type": "function", "function": tool}
            for tool in call["tools"]
        ]
    if (choice := call.get("tool_choice")) is not None:
        kwargs["tool_choice"] = choice
        if isinstance(choice, dict):
            kwargs["tool_choice"] = (
                {"type": "function", **choice}
                if responses
                else {"type": "function", "function": choice}
            )
    if call.get("reasoning") is not None:
        kwargs["reasoning" if responses else "reasoning_effort"] = (
            call["reasoning"] if responses else call["reasoning"]["effort"]
        )
    if call.get("stop") is not None:
        kwargs["stop"] = call["stop"]
    if schema is not None:
        # https://docs.litellm.ai/docs/completion/json_mode
        if responses:
            kwargs["text"] = {
                "format": dict(type="json_schema", name="output", schema=schema, strict=True)
            }
        else:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": dict(name="output", schema=schema, strict=True),
            }
    if call.get("extra", {}).get("include_usage") and not responses:
        kwargs["stream_options"] = {"include_usage": True}
    if provider == "anthropic":
        # https://docs.litellm.ai/docs/providers/anthropic
        hints = call.get("extra", {}).get("anthropic", {})
        if hints.get("cache"):
            kwargs["cache_control"] = {"type": "ephemeral"}
        # https://docs.litellm.ai/docs/providers/bedrock#eager-input-streaming-for-tool-calls
        if hints.get("eager_input_streaming"):
            for item in kwargs["tools"]:
                item["eager_input_streaming"] = True
    base = os.environ["WIRETRUTH_BASE_URL_" + provider.upper().replace("-", "_")]
    if provider == "gemini":
        base = base.rstrip("/") + "/v1beta"
    prefix = {"ollama": "ollama_chat", "gemini": "gemini", "anthropic": "anthropic"}.get(
        provider, "openai"
    )
    litellm.suppress_debug_info = True
    try:
        with redirect_stdout(sys.stderr):
            if responses:
                # https://docs.litellm.ai/docs/response_api (Python SDK, including streaming)
                result = litellm.responses(
                    model=prefix + "/" + call["model"],
                    input=call["messages"],
                    api_base=base,
                    api_key=os.environ["WIRETRUTH_API_KEY"],
                    max_output_tokens=call["max_output_tokens"],
                    stream=call["stream"],
                    **kwargs,
                )
                if call["stream"]:
                    for event in result:
                        obs["stream"]["chunks_seen"] += 1
                        observe_event(event)
                else:
                    observe_responses(result)
            else:
                # https://docs.litellm.ai/docs/completion (same call for all provider prefixes)
                result = litellm.completion(
                    model=prefix + "/" + call["model"],
                    messages=call["messages"],
                    api_base=base,
                    api_key=os.environ["WIRETRUTH_API_KEY"],
                    max_tokens=call["max_output_tokens"],
                    stream=call["stream"],
                    **kwargs,
                )
                if call["stream"]:
                    for chunk in result:
                        obs["stream"]["chunks_seen"] += 1
                        observe(chunk, streaming=True)
                    # The normalized chunk iterator has no independent completion signal.
                else:
                    observe(result)
    except Exception as error:
        # https://docs.litellm.ai/docs/exception_mapping
        observe_error(error)
obs["elapsed_ms"] = (time.monotonic() - started) * 1000
print(json.dumps(obs, ensure_ascii=True, allow_nan=False))
