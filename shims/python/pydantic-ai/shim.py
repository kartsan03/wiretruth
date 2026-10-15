import asyncio
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path

from pydantic import create_model
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    DeferredToolRequests,
    NativeOutput,
    ToolOutput,
    capture_run_messages,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    SystemPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ExternalToolset


def schema_type(schema, name="output"):
    kind = schema.get("type")
    primitives = {"string": str, "integer": int, "number": float, "boolean": bool}
    allowed = {"object": {"properties", "required", "additionalProperties"}, "array": {"items"}}
    if kind not in (*primitives, *allowed) or schema.keys() - {"type"} - allowed.get(kind, set()):
        sys.exit("pydantic-ai: schema not mapped yet")
    if kind == "array":
        return list[schema_type(schema["items"], name + "_item")]
    if kind != "object":
        return primitives[kind]
    if schema.get("additionalProperties") is not False:
        sys.exit("pydantic-ai: additionalProperties not mapped yet")
    fields = {}
    for key, child in schema.get("properties", {}).items():
        annotation = schema_type(child, name + "_" + key)
        fields[key] = (
            (annotation, ...) if key in schema.get("required", []) else (annotation | None, None)
        )
    return create_model(name, __config__={"extra": "forbid"}, **fields)


def text(value, surface="primary"):
    obs["text"].update(value=value, surface=surface)


def observe(response, surface="primary"):
    reason = response.finish_reason
    raw = (response.provider_details or {}).get("finish_reason")
    obs["finish"].update(
        value=manifest["finish_map"].get(reason) if reason is not None else None,
        raw=raw,
        surface=surface if reason is not None else "raw" if raw is not None else "absent",
        source="ModelResponse.finish_reason"
        if reason is not None
        else "provider_details.finish_reason"
        if raw is not None
        else None,
    )
    values = [part.content for part in response.parts if isinstance(part, TextPart)]
    if values and (surface == "primary" or obs["text"]["surface"] != "primary"):
        text("".join(values), surface)
    obs["tool_calls"] = []
    for part in response.parts:
        if not isinstance(part, ToolCallPart):
            continue
        raw_args = part.args_as_json_str()
        try:
            arguments, ok = json.loads(raw_args), True
            json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, UnicodeError):
            arguments, ok = None, False
        obs["tool_calls"].append(
            dict(
                id=part.tool_call_id,
                name=part.tool_name,
                arguments_raw=raw_args,
                arguments=arguments,
                arguments_parsed_ok=ok,
            )
        )
    for name, attr in usage_fields.items():
        value = getattr(response.usage, attr, None)
        obs["usage"][name] = value
        obs["usage"]["surface"][name] = surface if value is not None else "absent"


def observe_error(error):
    frame = (traceback.extract_tb(error.__traceback__) or [None])[-1]
    if type(error).__module__ == "builtins" and frame and frame.filename == __file__:
        raise error
    relative = frame.filename.replace("\\", "/").partition("/site-packages/")[2] if frame else ""
    types = [f"{cls.__module__}.{cls.__qualname__}" for cls in type(error).__mro__]
    category = next(
        (item["category"] for item in manifest["error_map"] if item["type"] in types),
        "internal" if type(error).__module__ == "builtins" and relative else "other",
    )
    # https://ai.pydantic.dev/api/exceptions/#pydantic_ai.exceptions.UnexpectedModelBehavior
    if "Model token limit" in str(error):
        category = "truncation"
    if error.__cause__ is not None and type(error.__cause__).__name__ == "ValidationError":
        category = "validation"
    obs.update(
        outcome="raised",
        error=dict(
            type=types[0],
            message=str(error)[:500],
            category=category,
            retry_after=None,
            stack_top=f"{relative}:{frame.lineno}" if relative else None,
        ),
    )
    obs["stream"]["completed"] = False if call["stream"] else None


async def run():
    base = os.environ["WIRETRUTH_BASE_URL_" + provider.upper().replace("-", "_")]
    credentials = dict(base_url=base, api_key=os.environ["WIRETRUTH_API_KEY"])
    # https://ai.pydantic.dev/models/openai/ and https://ai.pydantic.dev/models/anthropic/
    # https://ai.pydantic.dev/models/google/ and https://ai.pydantic.dev/models/ollama/
    model_class, provider_class = {
        "openai-chat": (OpenAIChatModel, OpenAIProvider),
        "openai-responses": (OpenAIResponsesModel, OpenAIProvider),
        "anthropic": (AnthropicModel, AnthropicProvider),
        "gemini": (GoogleModel, GoogleProvider),
        "ollama-openai": (OllamaModel, OllamaProvider),
    }[provider]
    model = model_class(call["model"], provider=provider_class(**credentials))
    settings = {}
    if call["max_output_tokens"] is not None:
        settings["max_tokens"] = call["max_output_tokens"]
    if call.get("stop") is not None:
        settings["stop_sequences"] = call["stop"]
    # https://ai.pydantic.dev/api/settings/#pydantic_ai.settings.ModelSettings
    if call.get("reasoning") is not None:
        effort = call["reasoning"]["effort"]
        settings["thinking"] = False if effort == "none" else effort
    if (choice := call.get("tool_choice")) is not None:
        settings["tool_choice"] = [choice["name"]] if isinstance(choice, dict) else choice
    if provider == "anthropic":
        # https://ai.pydantic.dev/models/anthropic/ (cache and fine-grained tool streaming)
        hints = call.get("extra", {}).get("anthropic", {})
        if hints.get("cache"):
            settings["anthropic_cache"] = True
        if hints.get("eager_input_streaming"):
            settings["anthropic_eager_input_streaming"] = True
    output_type, toolsets = str, []
    if schema is not None:
        # https://ai.pydantic.dev/output/#native-output and #tool-output
        output_type = (
            ToolOutput(schema_type(schema), name="structured_output")
            if call["scenario"] == "truncation.structured-output-forced-tool"
            else NativeOutput(schema_type(schema))
        )
    elif call.get("tools"):
        # https://ai.pydantic.dev/toolsets/#external-toolset
        toolsets = [
            ExternalToolset(
                [
                    ToolDefinition(
                        name=t["name"],
                        description=t["description"],
                        parameters_json_schema=t["parameters"],
                    )
                    for t in call["tools"]
                ]
            )
        ]
        output_type = [str, DeferredToolRequests]
    # https://ai.pydantic.dev/api/agent/#pydantic_ai.agent.AgentModelSettings
    # A forced function call applies to the first step; later steps may produce output.
    model_settings = settings
    if settings.get("tool_choice") == "required":

        def model_settings(ctx):
            return {**settings, "tool_choice": "required" if ctx.run_step == 1 else "auto"}

    # https://ai.pydantic.dev/agents/ (running agents)
    agent = Agent(model, output_type=output_type, model_settings=model_settings, toolsets=toolsets)
    # https://ai.pydantic.dev/message-history/ (reuse messages; inspect failed runs)
    history = []
    for message in call["messages"]:
        part_class = SystemPromptPart if message["role"] == "system" else UserPromptPart
        history.append(ModelRequest(parts=[part_class(message["content"])]))
    result = None
    with capture_run_messages() as messages:
        try:
            if call["stream"]:
                # https://ai.pydantic.dev/agents/#streaming-all-events
                async with agent.run_stream_events(message_history=history) as events:
                    async for event in events:
                        obs["stream"]["chunks_seen"] += 1
                        if isinstance(event, AgentRunResultEvent):
                            result = event.result
                            obs["stream"]["completed"] = result.response.state == "complete"
                        elif isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                            text((obs["text"]["value"] or "") + event.part.content)
                        elif isinstance(event, PartDeltaEvent) and isinstance(
                            event.delta, TextPartDelta
                        ):
                            text((obs["text"]["value"] or "") + event.delta.content_delta)
            else:
                result = await agent.run(message_history=history)
        except Exception as error:
            observe_error(error)
        responses = [message for message in messages if isinstance(message, ModelResponse)]
        if result is not None:
            observe(result.response)
        elif responses:
            # After an error, captured messages are a diagnostic, neither the result nor the error.
            observe(responses[-1], "raw")
        requests = result.usage.requests if result is not None else len(responses)
        if requests > 1:
            obs["notes"].append(f"retries: {requests - 1} ({requests} model requests)")
        if result is not None and schema is not None:
            obs["structured"].update(
                returned_as_success=True, value=result.output.model_dump(mode="json")
            )


started = time.monotonic()
call = json.load(sys.stdin)
manifest = json.loads(Path("wiretruth-shim.json").read_text())
provider, schema = call["provider"], call["output_schema"]
if provider not in manifest["providers"] or provider != os.environ["WIRETRUTH_PROVIDER"]:
    sys.exit("pydantic-ai: provider not mapped yet")
usage_fields = dict(
    input="input_tokens",
    output="output_tokens",
    total="total_tokens",
    cached_input="cache_read_tokens",
    reasoning="output_reasoning_tokens",
)
obs = {
    "format": "wiretruth.observation/v1",
    "library": dict(
        name="pydantic-ai",
        version=version("pydantic-ai-slim"),
        language="python",
        api="Agent.run_stream_events" if call["stream"] else "Agent.run",
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
if provider == "ollama":
    entry = manifest["unsupported"][0]
    obs.update(outcome="unsupported", unsupported_reason=entry["reason"] + " " + entry["docs"])
    obs["structured"]["attempted"] = False
else:
    try:
        with redirect_stdout(sys.stderr):
            asyncio.run(run())
    except Exception as error:
        observe_error(error)
obs["elapsed_ms"] = (time.monotonic() - started) * 1000
print(json.dumps(obs, ensure_ascii=True, allow_nan=False))
