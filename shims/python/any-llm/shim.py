import asyncio
import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path

import any_llm
from pydantic import create_model


def schema_type(schema, name="output"):
    kind = schema.get("type") if isinstance(schema, dict) else None
    primitives = {"string": str, "integer": int, "number": float, "boolean": bool}
    allowed = {"object": {"properties", "required", "additionalProperties"}, "array": {"items"}}
    if kind not in (*primitives, *allowed) or schema.keys() - {"type"} - allowed.get(kind, set()):
        sys.exit("any-llm: schema not mapped yet")
    if kind == "array":
        return list[schema_type(schema.get("items"), name + "_item")]
    if kind != "object":
        return primitives[kind]
    if schema.get("additionalProperties") is not False:
        sys.exit("any-llm: additionalProperties not mapped yet")
    fields = {}
    for key, child in schema.get("properties", {}).items():
        annotation = schema_type(child, name + "_" + key)
        required = key in schema.get("required", [])
        fields[key] = (annotation, ...) if required else (annotation | None, None)
    return create_model(name, __config__={"extra": "forbid"}, **fields)


def observe(result, prefix="", streaming=False):
    if result.choices:
        choice = result.choices[0]
        if choice.finish_reason is not None:
            raw = choice.finish_reason
            obs["finish"].update(value=manifest["finish_map"].get(raw, "other"), raw=raw)
            obs["finish"].update(surface="primary", source=prefix + "choices[0].finish_reason")
        message = choice.delta if streaming else choice.message
        if message.content is not None:
            text = obs["text"]
            text.update(value=(text["value"] or "") + message.content, surface="primary")
        for index, tool in enumerate(message.tool_calls or []):
            index = tool.index if streaming else index
            stored = tool_calls.setdefault(index, dict(id=None, name="", arguments_raw=None))
            stored["id"] = stored["id"] or tool.id
            if tool.function is not None:
                stored["name"] = stored["name"] or tool.function.name or ""
                if tool.function.arguments is not None:
                    stored["arguments_raw"] = (
                        stored["arguments_raw"] or ""
                    ) + tool.function.arguments
            try:
                arguments, ok = json.loads(stored["arguments_raw"]), True
                # Values the observation can't carry (1e400, a lone surrogate) count as unparsed.
                json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
            except Exception:
                arguments, ok = None, False
            stored.update(arguments_parsed_ok=ok, arguments=arguments)
        obs["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        parsed = getattr(message, "parsed", None)
        if parsed is not None and obs["outcome"] == "returned":
            obs["structured"].update(returned_as_success=True, value=parsed.model_dump(mode="json"))
    if result.usage is not None:
        for name, path in usage_fields.items():
            value = result.usage
            for attr in path.split("."):
                value = getattr(value, attr, None)
            obs["usage"][name] = value
            obs["usage"]["surface"][name] = "primary" if value is not None else "absent"


def responses_kwargs():
    if call.get("stop") is not None:
        sys.exit("any-llm: stop not mapped yet")
    kwargs = {}
    if call.get("tools"):
        kwargs["tools"] = [{"type": "function", **tool} for tool in call["tools"]]
    if (choice := call.get("tool_choice")) is not None:
        kwargs["tool_choice"] = (
            {"type": "function", **choice} if isinstance(choice, dict) else choice
        )
    if call.get("reasoning") is not None:
        kwargs["reasoning"] = {"effort": call["reasoning"]["effort"]}
    if schema is not None:
        kwargs["response_format"] = schema_type(schema)
    return kwargs


def responses_tool(index, id=None, name=None, arguments=None):
    stored = tool_calls.setdefault(index, dict(id=None, name="", arguments_raw=None))
    stored["id"] = stored["id"] or id
    stored["name"] = stored["name"] or name or ""
    if arguments is not None:
        stored["arguments_raw"] = (stored["arguments_raw"] or "") + arguments
    try:
        parsed, ok = json.loads(stored["arguments_raw"]), True
        json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode()
    except Exception:
        parsed, ok = None, False
    stored.update(arguments_parsed_ok=ok, arguments=parsed)
    obs["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]


def responses_error(error):
    # The library returned or yielded this error instead of raising it: the outcome stays returned.
    obs["structured"].update(returned_as_success=False, value=None)
    obs["error"] = dict(
        type=f"{type(error).__module__}.{type(error).__qualname__}",
        message=getattr(error, "message", "")[:500],
        category="api",
        retry_after=None,
        stack_top=None,
    )
    obs["stream"]["completed"] = False if call["stream"] else None


def observe_responses(result, prefix="", streaming=False):
    global responses_refusal
    has_tool = False
    for index, item in enumerate(result.output or []):
        if item.type == "message":
            for part in item.content:
                if part.type == "refusal":
                    responses_refusal = True
                elif part.type == "output_text" and not streaming:
                    text = obs["text"]
                    text.update(value=(text["value"] or "") + part.text, surface="primary")
        elif item.type == "function_call":
            has_tool = True
            if not streaming:
                responses_tool(index, item.call_id, item.name, item.arguments)
    reason = getattr(result.incomplete_details, "reason", None)
    raw = reason if reason is not None else result.status
    value = None
    if result.status is not None:
        value = "other"
        if result.status == "incomplete" and raw in ("max_output_tokens", "content_filter"):
            value = manifest["finish_map"].get(raw, "other")
        if result.status == "completed":
            value = "tool_calls" if has_tool else manifest["finish_map"]["completed"]
        if responses_refusal:
            value = "refusal"
        if result.status == "failed" or result.error is not None:
            value = "error"
            responses_error(result.error or result)
        obs["finish"].update(
            value=value,
            raw=raw,
            surface="primary",
            source=prefix + ("incomplete_details.reason" if reason is not None else "status"),
        )
    if result.usage is not None:
        for name, path in usage_fields.items():
            value = result.usage
            for attr in path.split("."):
                value = getattr(value, attr, None)
            obs["usage"][name] = value
            obs["usage"]["surface"][name] = "primary" if value is not None else "absent"
    parsed = getattr(result, "output_parsed", None)
    if parsed is not None and obs["outcome"] == "returned":
        obs["structured"].update(returned_as_success=True, value=parsed.model_dump(mode="json"))


def observe_responses_event(event):
    global responses_refusal
    kind = event.type
    if kind in ("response.completed", "response.incomplete", "response.failed"):
        observe_responses(event.response, kind + ".response.", streaming=True)
        if kind == "response.failed" and obs["error"] is None:
            responses_error(event.response)
        obs["stream"]["completed"] = obs["error"] is None
    elif kind == "error":
        responses_error(getattr(event, "error", None) or event)
    elif kind == "response.output_text.delta":
        text = obs["text"]
        text.update(value=(text["value"] or "") + event.delta, surface="primary")
    elif kind == "response.refusal.delta" or (
        kind == "response.content_part.added" and event.part.type == "refusal"
    ):
        responses_refusal = True
        obs["finish"].update(value="refusal", raw=None, surface="primary", source=kind)
    elif kind == "response.output_item.added" and event.item.type == "function_call":
        responses_tool(event.output_index, event.item.call_id, event.item.name)
    elif kind == "response.function_call_arguments.delta":
        responses_tool(event.output_index, arguments=event.delta)


def observe_error(error):
    obs["outcome"] = "raised"
    obs["structured"].update(returned_as_success=False, value=None)
    frame = (traceback.extract_tb(error.__traceback__) or [None])[-1]
    if type(error).__module__ == "builtins" and frame and frame.filename == __file__:
        raise error  # a bug in this shim, not in the library: exit 1 grades HARNESS
    relative = frame.filename.replace("\\", "/").partition("/site-packages/")[2] if frame else ""
    types = [f"{cls.__module__}.{cls.__qualname__}" for cls in type(error).__mro__]
    category = next(
        (item["category"] for item in manifest["error_map"] if item["type"] in types),
        "internal" if type(error).__module__ == "builtins" and relative else "other",
    )
    stack_top = f"{relative}:{frame.lineno}" if relative else None
    obs["error"] = dict(type=types[0], message=str(error)[:500], category=category)
    obs["error"].update(retry_after=None, stack_top=stack_top)
    obs["stream"]["completed"] = False if call["stream"] else None
    if (completion := getattr(error, "completion", None)) is not None:
        observe(completion, "error.completion.")


class ObserverPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        loop = super().new_event_loop()
        loop.set_exception_handler(lambda _, ctx: observe_error(ctx["exception"]))
        return loop


started = time.monotonic()
call = json.load(sys.stdin)
schema = call["output_schema"]
if call["provider"] not in ("openai-chat", "openai-responses") or (
    call["provider"] != os.environ["WIRETRUTH_PROVIDER"]
):
    sys.exit("any-llm: provider not mapped yet")
responses = call["provider"] == "openai-responses"
responses_refusal = False
manifest = json.loads(Path("wiretruth-shim.json").read_text())
usage_fields = dict(
    input="prompt_tokens",
    output="completion_tokens",
    total="total_tokens",
    cached_input="prompt_tokens_details.cached_tokens",
    reasoning="completion_tokens_details.reasoning_tokens",
)
if responses:
    usage_fields = dict(
        input="input_tokens",
        output="output_tokens",
        total="total_tokens",
        cached_input="input_tokens_details.cached_tokens",
        reasoning="output_tokens_details.reasoning_tokens",
    )
obs = {
    "format": "wiretruth.observation/v1",
    "library": dict(
        name="any-llm",
        version=version("any-llm-sdk"),
        language="python",
        api="any_llm.responses" if responses else "any_llm.completion",
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
tool_calls = {}
kwargs = {}
if call.get("tools"):
    kwargs["tools"] = [{"type": "function", "function": tool} for tool in call["tools"]]
if (choice := call.get("tool_choice")) is not None:
    kwargs["tool_choice"] = (
        {"type": "function", "function": choice} if isinstance(choice, dict) else choice
    )
if call.get("stop") is not None:
    kwargs["stop"] = call["stop"]
if schema is not None:
    # https://docs.mozilla.ai/api-reference/completion (Structured output)
    kwargs["response_format"] = schema_type(schema)
if call.get("reasoning") is not None:
    kwargs["reasoning_effort"] = call["reasoning"]["effort"]
if call.get("extra", {}).get("include_usage"):
    kwargs["stream_options"] = {"include_usage": True}
if responses:
    kwargs = responses_kwargs()
threading.excepthook = lambda args: observe_error(args.exc_value)
asyncio.set_event_loop_policy(ObserverPolicy())
try:
    with redirect_stdout(sys.stderr):
        if responses:
            # https://docs.mozilla.ai/api-reference/responses
            result = any_llm.responses(
                model=call["model"],
                provider="openai",
                input_data=call["messages"],
                api_base=os.environ["WIRETRUTH_BASE_URL_OPENAI_RESPONSES"],
                api_key=os.environ["WIRETRUTH_API_KEY"],
                max_output_tokens=call["max_output_tokens"],
                stream=call["stream"],
                **kwargs,
            )
            if call["stream"]:
                obs["stream"]["completed"] = False
                for event in result:
                    obs["stream"]["chunks_seen"] += 1
                    observe_responses_event(event)
            else:
                observe_responses(result)
        else:
            # https://docs.mozilla.ai/api-reference/completion (Basic completion and Streaming)
            result = any_llm.completion(
                model=call["model"],
                provider="openai",
                messages=call["messages"],
                api_base=os.environ["WIRETRUTH_BASE_URL_OPENAI_CHAT"],
                api_key=os.environ["WIRETRUTH_API_KEY"],
                max_tokens=call["max_output_tokens"],
                stream=call["stream"],
                **kwargs,
            )
            if call["stream"]:
                for chunk in result:
                    obs["stream"]["chunks_seen"] += 1
                    observe(chunk, streaming=True)
                # Chat iteration ends the same way with or without the wire's completion marker,
                # so the library gives no completion signal and `completed` stays None.
            else:
                observe(result)
except Exception as error:
    # https://docs.mozilla.ai/api-reference/exceptions (provider exceptions pass through by default)
    observe_error(error)
obs["elapsed_ms"] = (time.monotonic() - started) * 1000
print(json.dumps(obs, ensure_ascii=True, allow_nan=False))
