import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path

from openai import LengthFinishReasonError, OpenAI
from pydantic import ConfigDict, create_model

started = time.monotonic()
call = json.load(sys.stdin)
manifest = json.loads(Path("wiretruth-shim.json").read_text())
provider = os.environ["WIRETRUTH_PROVIDER"]
responses = provider == "openai-responses"
structured = call["output_schema"] is not None
resource = "responses" if responses else "chat.completions"
api = f"OpenAI.{resource}.{'parse' if structured else 'create'}"
FIELDS = ("input", "cached_input", "output", "reasoning", "total")
obs = {
    "format": "wiretruth.observation/v1",
    "library": dict(name="openai-python", version=version("openai"), language="python", api=api),
    "outcome": "returned",
    "unsupported_reason": None,
    "error": None,
    "finish": dict(value=None, raw=None, surface="absent", source=None),
    "text": dict(value=None, surface="absent"),
    "structured": dict(attempted=structured, returned_as_success=False, value=None),
    "tool_calls": [],
    "usage": {**dict.fromkeys(FIELDS), "surface": dict.fromkeys(FIELDS, "absent")},
    "stream": dict(used=call["stream"], completed=None, chunks_seen=0 if call["stream"] else None),
    "notes": [],
}


def schema_type(schema, name="Output"):
    """Translate only the schema features used by the structured-output cells."""
    if schema == {"type": "string"}:
        return str
    if schema.get("type") == "array" and set(schema) == {"type", "items"}:
        return list[schema_type(schema["items"], name + "Item")]
    if (
        schema.get("type") == "object"
        and set(schema) == {"type", "properties", "required", "additionalProperties"}
        and schema["additionalProperties"] is False
        and set(schema["required"]) == set(schema["properties"])
    ):
        return create_model(
            name,
            __config__=ConfigDict(extra="forbid"),
            **{
                key: (schema_type(value, name + key.title()), ...)
                for key, value in schema["properties"].items()
            },
        )
    sys.exit("output_schema feature not mapped")


def finish(raw, source):
    if raw is not None:
        obs["finish"].update(
            value=manifest["finish_map"].get(raw, "other"),
            raw=raw,
            surface="primary",
            source=source,
        )


def text(value, append=False):
    if value is not None:
        obs["text"].update(
            value=(obs["text"]["value"] or "") + value if append else value,
            surface="primary",
        )


def usage(value):
    if value is None:
        return
    input_name, output_name = (
        ("input_tokens", "output_tokens") if responses else ("prompt_tokens", "completion_tokens")
    )
    counts = {
        "input": getattr(value, input_name, None),
        "cached_input": getattr(
            getattr(value, input_name + "_details", None), "cached_tokens", None
        ),
        "output": getattr(value, output_name, None),
        "reasoning": getattr(
            getattr(value, output_name + "_details", None), "reasoning_tokens", None
        ),
        "total": value.total_tokens,
    }
    for field, count in counts.items():
        if count is not None:
            obs["usage"][field] = count
            obs["usage"]["surface"][field] = "primary"


def tool(identifier, name, arguments):
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        parsed, ok = None, False
    else:
        ok = True
    return dict(
        id=identifier,
        name=name,
        arguments_raw=arguments,
        arguments_parsed_ok=ok,
        arguments=parsed,
    )


def parsed(value):
    if value is not None:
        obs["structured"].update(returned_as_success=True, value=value.model_dump(mode="json"))


def observe_chat(result, prefix=""):
    usage(result.usage)
    if not result.choices:
        return
    choice = result.choices[0]
    finish(choice.finish_reason, prefix + "choices[0].finish_reason")
    text(choice.message.content)
    obs["tool_calls"] = [
        tool(item.id, item.function.name, item.function.arguments)
        for item in choice.message.tool_calls or []
        if item.type == "function"
    ]
    if structured and obs["outcome"] == "returned":
        parsed(choice.message.parsed)


def error_fields(error):
    types = [f"{cls.__module__}.{cls.__qualname__}" for cls in type(error).__mro__]
    default = "internal" if type(error).__module__ == "builtins" else "other"
    category = next(
        (entry["category"] for entry in manifest["error_map"] if entry["type"] in types),
        default,
    )
    return dict(
        type=types[0],
        message=str(getattr(error, "message", error))[:500],
        category=category,
        retry_after=None,
        stack_top=None,
    )


def raised(error):
    frame = traceback.extract_tb(error.__traceback__)[-1]
    if Path(frame.filename).resolve() == Path(__file__).resolve():
        raise error
    obs["outcome"] = "raised"
    obs["error"] = error_fields(error)
    relative = frame.filename.partition("/site-packages/")[2]
    if relative:
        obs["error"]["stack_top"] = f"{relative}:{frame.lineno}"
    if isinstance(error, LengthFinishReasonError):
        observe_chat(error.completion, "error.completion.")
    if call["stream"]:
        obs["stream"]["completed"] = False


def observe_response(result, streamed=False):
    source = "response." if streamed else ""
    if result.incomplete_details and result.incomplete_details.reason is not None:
        finish(result.incomplete_details.reason, source + "incomplete_details.reason")
    else:
        finish(result.status, source + "status")
    obs["tool_calls"] = [
        tool(item.call_id, item.name, item.arguments)
        for item in result.output
        if item.type == "function_call"
    ]
    if result.status == "completed":
        if obs["tool_calls"]:
            obs["finish"].update(value="tool_calls", source=source + "status, output[].type")
        elif any(
            part.type == "refusal"
            for item in result.output
            if item.type == "message"
            for part in item.content
        ):
            obs["finish"].update(value="refusal", source=source + "status, output[].content[].type")
    if result.error is not None:
        obs["error"] = error_fields(result.error)
    usage(result.usage)
    if not streamed:
        text(result.output_text)
    if structured:
        parsed(result.output_parsed)


# This synchronous call path starts no event loop. Also capture escaped thread errors.
threading.excepthook = lambda args: raised(args.exc_value)
unsupported = next(
    (
        entry
        for entry in manifest["unsupported"]
        if entry["scenario"] == call["scenario"] and provider in entry.get("providers", [provider])
    ),
    None,
)
if unsupported:
    obs["outcome"] = "unsupported"
    obs["unsupported_reason"] = unsupported["reason"] + " " + unsupported["docs"]
else:
    params = {"model": call["model"]}
    params["input" if responses else "messages"] = call["messages"]
    if call["max_output_tokens"] is not None:
        limit = (
            "max_output_tokens"
            if responses
            else "max_tokens"
            if provider == "ollama-openai"
            else "max_completion_tokens"
        )
        params[limit] = call["max_output_tokens"]
        if call["scenario"].startswith("request."):
            # The SDK sends the field it is given, so these cells grade the shim's choice.
            obs["notes"].append(
                f"The SDK sends the token-limit field the caller passes; this shim passes {limit}."
            )
    if call["reasoning"]:
        params["reasoning" if responses else "reasoning_effort"] = (
            call["reasoning"] if responses else call["reasoning"]["effort"]
        )
    if call["stop"]:
        if responses:
            sys.exit("Responses stop sequences not mapped")
        params["stop"] = call["stop"]
    if call["tools"]:
        params["tools"] = [
            {"type": "function", **item} if responses else {"type": "function", "function": item}
            for item in call["tools"]
        ]
    if call["tool_choice"] is not None:
        choice = call["tool_choice"]
        params["tool_choice"] = (
            (
                {"type": "function", **choice}
                if responses
                else {"type": "function", "function": choice}
            )
            if isinstance(choice, dict)
            else choice
        )
    if structured:
        if call["stream"]:
            sys.exit("streamed structured output not mapped")
        params["text_format" if responses else "response_format"] = schema_type(
            call["output_schema"]
        )
    else:
        params["stream"] = call["stream"]
    for hint, value in call["extra"].items():
        if hint == "include_usage" and not responses and call["stream"]:
            params["stream_options"] = {"include_usage": value}
        else:
            obs["notes"].append(f"The chosen SDK call has no mapped switch for extra.{hint}.")
    try:
        # https://github.com/openai/openai-python/blob/v3.52.0/README.md#configuring-the-http-client
        with (
            redirect_stdout(sys.stderr),
            OpenAI(
                api_key=os.environ["WIRETRUTH_API_KEY"],
                base_url=os.environ["WIRETRUTH_BASE_URL_" + provider.upper().replace("-", "_")],
            ) as client,
        ):
            if structured:
                if responses:
                    # https://github.com/openai/openai-python/blob/v3.52.0/helpers.md#parsing-responses-api-output
                    result = client.responses.parse(**params)
                else:
                    # https://github.com/openai/openai-python/blob/v3.52.0/helpers.md#auto-parsing-response-content-with-pydantic-models
                    result = client.chat.completions.parse(**params)
            elif responses:
                # https://github.com/openai/openai-python/blob/v3.52.0/README.md#streaming-responses
                # https://github.com/openai/openai-python/blob/v3.52.0/README.md#usage
                result = client.responses.create(**params)
            else:
                # https://github.com/openai/openai-python/blob/v3.52.0/api.md#completions-1
                # https://docs.ollama.com/api/openai-compatibility
                result = client.chat.completions.create(**params)
            if not call["stream"]:
                (observe_response if responses else observe_chat)(result)
            else:
                tool_deltas = {}
                if responses:
                    # Responses streams end with a terminal event; without one the stream is cut.
                    obs["stream"]["completed"] = False
                with result:
                    for chunk in result:
                        obs["stream"]["chunks_seen"] += 1
                        if responses:
                            if chunk.type == "response.output_text.delta":
                                text(chunk.delta, append=True)
                            elif chunk.type == "response.output_item.done":
                                if chunk.item.type == "function_call":
                                    item = chunk.item
                                    obs["tool_calls"].append(
                                        tool(item.call_id, item.name, item.arguments)
                                    )
                            elif chunk.type in {
                                "response.completed",
                                "response.incomplete",
                                "response.failed",
                            }:
                                observe_response(chunk.response, streamed=True)
                                obs["stream"]["completed"] = chunk.type != "response.failed"
                            elif chunk.type == "error":
                                obs["error"] = error_fields(chunk)
                                obs["finish"].update(
                                    value="error", raw=None, surface="primary", source="event.type"
                                )
                                obs["stream"]["completed"] = False
                        else:
                            usage(chunk.usage)
                            for choice in chunk.choices:
                                if choice.index != 0:
                                    continue
                                finish(choice.finish_reason, "choices[0].finish_reason")
                                text(choice.delta.content, append=True)
                                for delta in choice.delta.tool_calls or []:
                                    item = tool_deltas.setdefault(
                                        delta.index, dict(id=None, name="", arguments="")
                                    )
                                    if delta.id is not None:
                                        item["id"] = delta.id
                                    if delta.function is not None:
                                        item["name"] += delta.function.name or ""
                                        item["arguments"] += delta.function.arguments or ""
                                obs["tool_calls"] = [
                                    tool(item["id"], item["name"], item["arguments"])
                                    for _, item in sorted(tool_deltas.items())
                                ]
    except Exception as error:
        raised(error)
obs["elapsed_ms"] = (time.monotonic() - started) * 1000
print(json.dumps(obs))
