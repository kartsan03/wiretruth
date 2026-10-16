# wiretruth

wiretruth replays real LLM provider responses through LLM SDKs and checks whether each SDK reports what actually
happened.

**Does your LLM SDK tell you the truth?**

[![CI](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml/badge.svg)](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wiretruth?label=PyPI)](https://pypi.org/project/wiretruth/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/kartsan03/wiretruth/blob/main/LICENSE)

wiretruth tests any-llm, LiteLLM and pydantic-ai across six APIs: OpenAI Chat Completions and Responses, Ollama's native
and OpenAI-compatible endpoints, Gemini GenerateContent and Anthropic Messages. That is 24 scenarios and 95 cells
per library. pydantic-ai supports the OpenAI-compatible Ollama endpoint; its native Ollama cells are UNSUPPORTED.
The Anthropic responses are transcribed from Anthropic's documentation rather than recorded: each of those fixtures
is marked `documented` and links the page it comes from. More providers and libraries follow, and a public
results matrix arrives in v0.4. Until then, the grades are in
[`results/`](https://github.com/kartsan03/wiretruth/tree/main/results).

## Why

A model hits its output token limit halfway through a JSON object. The provider says so plainly:
`"finish_reason": "length"`. What your code sees depends on the SDK in between. If the SDK raises a generic JSON
parse error, nothing tells you that a higher limit would fix it. If it returns the partial object as a success,
your pipeline stores an incomplete record.

wiretruth has this case recorded as `truncation.structured-output` on `openai-chat`. Replayed through each library:

| Library | What the caller gets | Grade |
|---|---|---|
| any-llm 1.33.0 | Raises `openai.LengthFinishReasonError`. Its `completion` has `finish_reason: "length"` and the token usage. | ✓ PASS |
| LiteLLM 1.105.1 | Returns the cut-off JSON as text in `message.content` with `finish_reason: "length"` and parses none of it. | ✓ PASS |
| pydantic-ai 2.56.0 | Retries once, then raises `UnexpectedModelBehavior("Exceeded maximum output retries (1)")`. The `length` finish is only in the messages that `capture_run_messages()` keeps. | ~ LOSSY |

pydantic-ai's grades for structured output use `NativeOutput`, the provider's own JSON mode, because that is how
the fixtures were recorded. It is opt-in: by default pydantic-ai asks for a forced tool call instead on every API
here except Anthropic, and no scenario covers that path yet. On `gpt-6-luna` that default request is rejected with the same 400
that `request.tools-with-reasoning-chat` replays (function tools without `reasoning_effort: "none"`).

wiretruth runs the same recorded edge cases through every library it supports and grades each one against what the
provider sent.

## What it checks

| Bug class | What goes wrong for users | Scenarios |
|---|---|---|
| Silent truncation | Agents accept partial output or retry without raising the limit | 6 |
| Finish reason mapping | Control flow branches on the wrong reason | 2 |
| Usage accounting | Cost and quota tracking is off (cached and reasoning tokens) | 3 |
| Stream integrity | Corrupted or unfinished text is treated as final | 4 |
| Tool call fidelity | Tools run with broken or merged arguments | 4 |
| Error classification | Retries and backoff misbehave | 1 |
| Request fidelity | The library sends fields the caller didn't set, or ones the model rejects | 4 |

The scenarios are in
[`suite/scenarios/`](https://github.com/kartsan03/wiretruth/tree/main/suite/scenarios), and the bug classes in
[`suite/bug-classes.json`](https://github.com/kartsan03/wiretruth/blob/main/suite/bug-classes.json).

## Grades

| | Grade | Meaning |
|---|---|---|
| ✓ | PASS | Reports what happened: meets every MUST requirement of the scenario. |
| ~ | LOSSY | Doesn't contradict what happened, but the fact is only in raw metadata, or missing. |
| ✗ | WRONG | Reports something that contradicts what happened. |
| ! | CRASH | Fails with an internal error, such as a `KeyError` in its own parser. |
| ⧗ | HANG | Doesn't return within 20 s on a response that ended within 1 s. |
| – | UNSUPPORTED | The library doesn't support this provider or feature (documented). |
| ? | HARNESS | A problem on our side, not the library's. |

Each scenario lists its requirements. MUST requirements set the grade. SHOULD requirements, such as the exact
partial text in the truncation scenarios, add a note when they aren't met. How grades are computed:
[docs/grading.md](https://github.com/kartsan03/wiretruth/blob/main/docs/grading.md).
Scenario catalog: [docs/scenarios.md](https://github.com/kartsan03/wiretruth/blob/main/docs/scenarios.md).

## Quickstart

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/), which builds each library's own environment.

```bash
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv run wiretruth run --lib any-llm
uv run wiretruth run --lib litellm
uv run wiretruth run --lib pydantic-ai
uv run wiretruth run --lib vercel-ai
```

The Vercel AI SDK shim also needs Node.js and npm. The runner builds its pinned dependencies automatically;
see [Shims](shims/README.md) for supported providers and structured-output modes.

The any-llm run reports:

```text
scenario                                  anthropic      gemini   ollama   ollama-openai  openai-chat  openai-responses
finish.stop-sequence                      ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS
finish.unknown-value                      ✗ WRONG        ✗ WRONG  ✗ WRONG  ✗ WRONG        ✗ WRONG      ✓ PASS
request.max-tokens-field                                                                  ✓ PASS
request.max-tokens-ollama-openai                                           ✗ WRONG
request.no-injected-options                                       ✓ PASS
request.tools-with-reasoning-chat                                                         ✓ PASS
stream.error-midstream                    ✓ PASS         ✓ PASS   ✓ PASS                  ✓ PASS       ✓ PASS
stream.missing-finish                     ~ LOSSY        ~ LOSSY  ~ LOSSY  ~ LOSSY        ~ LOSSY      ✓ PASS
stream.missing-finish-abort               ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
stream.utf8                               ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
stream.utf8-split                         ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
tools.parallel-calls                      ✓ PASS         ✗ WRONG  ✗ WRONG  ✓ PASS         ✓ PASS       ✓ PASS
tools.parallel-calls-stream               ✓ PASS         ✗ WRONG  ✗ WRONG  ✓ PASS         ✓ PASS       ✓ PASS
truncation.reasoning-only                 ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
truncation.structured-output              ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ~ LOSSY
truncation.structured-output-forced-tool  – UNSUPPORTED
truncation.text                           ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
truncation.text-stream                    ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS       ✓ PASS
truncation.text-stream-usage                             ✓ PASS                           ✓ PASS
truncation.tool-call-args                 ✓ PASS                                          ✓ PASS       ✓ PASS
truncation.tool-call-args-stream          ✓ PASS                                          ✓ PASS       ✓ PASS
usage.cached-tokens                       ✓ PASS
usage.reasoning-tokens                                   ✓ PASS
usage.stream-final-chunk                  ✓ PASS         ✓ PASS   ✓ PASS   ✓ PASS         ✓ PASS

finish.stop-sequence gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

finish.unknown-value anthropic
  MUST finish-is-other: CONTRADICTED: finish.value="stop" (primary: choices[0].finish_reason); truth.finish="other"; required finish="other"
  SHOULD raw-finish-reported: CONTRADICTED: finish.raw="stop"; truth.raw_finish="WIRETRUTH_UNKNOWN_REASON"

finish.unknown-value gemini
  MUST finish-is-other: CONTRADICTED: finish.value="stop" (primary: choices[0].finish_reason); truth.finish="other"; required finish="other"
  SHOULD raw-finish-reported: CONTRADICTED: finish.raw="stop"; truth.raw_finish="WIRETRUTH_UNKNOWN_REASON"
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

finish.unknown-value ollama
  MUST finish-is-other: CONTRADICTED: finish.value="stop" (primary: choices[0].finish_reason); truth.finish="other"; required finish="other"
  SHOULD raw-finish-reported: CONTRADICTED: finish.raw="stop"; truth.raw_finish="wiretruth_unknown_reason"

finish.unknown-value ollama-openai
  MUST response-returned: CONTRADICTED: outcome=raised; error.category="validation"; truth.error=null
  MUST finish-is-other: NOT_OBSERVABLE: finish.value=null (absent); truth.finish="other"; required finish="other"
  MUST text-reported: NOT_OBSERVABLE: text.value=null (absent); truth.text="Octopuses, like many marine invertebrates, employ sophisticated methods to camouflage"
  SHOULD raw-finish-reported: NOT_OBSERVABLE: finish.raw=null; truth.raw_finish="wiretruth_unknown_reason"

finish.unknown-value openai-chat
  MUST response-returned: CONTRADICTED: outcome=raised; error.category="validation"; truth.error=null
  MUST finish-is-other: NOT_OBSERVABLE: finish.value=null (absent); truth.finish="other"; required finish="other"
  MUST text-reported: NOT_OBSERVABLE: text.value=null (absent); truth.text="Io, Europa, and Ganymede."
  SHOULD raw-finish-reported: NOT_OBSERVABLE: finish.raw=null; truth.raw_finish="wiretruth_unknown_reason"

request.max-tokens-ollama-openai ollama-openai
  MUST limit-field: CONTRADICTED: request[0].json_body.max_tokens=absent; required=16

stream.error-midstream gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

stream.missing-finish anthropic
  MUST stream-incomplete: NOT_OBSERVABLE: stream.completed=null; outcome=returned; truth.stream_completed=false

stream.missing-finish gemini
  MUST stream-incomplete: NOT_OBSERVABLE: stream.completed=null; outcome=returned; truth.stream_completed=false
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

stream.missing-finish ollama
  MUST stream-incomplete: NOT_OBSERVABLE: stream.completed=null; outcome=returned; truth.stream_completed=false

stream.missing-finish ollama-openai
  MUST stream-incomplete: NOT_OBSERVABLE: stream.completed=null; outcome=returned; truth.stream_completed=false

stream.missing-finish openai-chat
  MUST stream-incomplete: NOT_OBSERVABLE: stream.completed=null; outcome=returned; truth.stream_completed=false

stream.missing-finish-abort gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

stream.utf8 gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

stream.utf8-split gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

tools.parallel-calls gemini
  MUST calls-reported: CONTRADICTED: tool_calls[0] id="call_-5201471136765337120_0", name="get_weather", args={"city": "São Paulo"}, parsed=true; truth id="call_120847", name="get_weather", args={"city": "São Paulo"}, parsed=true; tool_calls[1] id="call_-5201471136765337120_1", name="get_weather", args={"city": "Zürich"}, parsed=true; truth id="call_120848", name="get_weather", args={"city": "Zürich"}, parsed=true
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

tools.parallel-calls ollama
  MUST calls-reported: CONTRADICTED: tool_calls[0] id="3972c760-858b-4728-a05a-279fc6e6a649", name="get_weather", args={"city": "São Paulo"}, parsed=true; truth id="call_9epwsqu6", name="get_weather", args={"city": "São Paulo"}, parsed=true; tool_calls[1] id="4d3747a6-5e75-4637-b82e-f269adbba01e", name="get_weather", args={"city": "Zürich"}, parsed=true; truth id="call_bvzy1ra9", name="get_weather", args={"city": "Zürich"}, parsed=true

tools.parallel-calls-stream gemini
  MUST calls-reported: CONTRADICTED: tool_calls[0] id="call_-1615951408272134819_0", name="get_weather", args={"city": "São Paulo"}, parsed=true; truth id="call_89893", name="get_weather", args={"city": "São Paulo"}, parsed=true; tool_calls[1] id="call_-1615951408272134819_1", name="get_weather", args={"city": "Zürich"}, parsed=true; truth id="call_89896", name="get_weather", args={"city": "Zürich"}, parsed=true
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

tools.parallel-calls-stream ollama
  MUST calls-reported: CONTRADICTED: tool_calls[0] id="8b75c529-6eb6-4a63-a375-4551bfe56f39", name="get_weather", args={"city": "São Paulo"}, parsed=true; truth id="call_8uaqob2r", name="get_weather", args={"city": "São Paulo"}, parsed=true; tool_calls[1] id="8e864d64-6145-4ca4-81ba-cbdbad0523e1", name="get_weather", args={"city": "Zürich"}, parsed=true; truth id="call_nyjtbrz4", name="get_weather", args={"city": "Zürich"}, parsed=true

truncation.structured-output gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

truncation.structured-output openai-responses
  MUST cause-is-truncation: NOT_OBSERVABLE: finish.value=null (absent); truth.finish="length"; error.category="validation"; structured.returned_as_success=false
  SHOULD text-reported: NOT_OBSERVABLE: text.value=null (absent); truth.text="{\"islands\":[{\"name\":\"Κρήτη\",\"sea\":\"Κρητικό Πέ"
  SHOULD usage-reported: NOT_OBSERVABLE: usage.input=null (absent); truth.usage.input=68; usage.output=null (absent); truth.usage.output=24

truncation.structured-output-forced-tool anthropic
  any-llm completion maps typed response_format to native output_config.format and has no structured parser for an explicitly forced tool. (https://github.com/mozilla-ai/any-llm/blob/1.33.0/src/any_llm/providers/anthropic/utils.py#L628-L655)

truncation.text gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

truncation.text-stream gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

truncation.text-stream-usage gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

usage.stream-final-chunk gemini
  shim: Gemini reasoning_effort none is unsupported by any-llm; omitted to use the model default.

any-llm 1.33.0: 95 cells, 78 PASS, 6 LOSSY, 10 WRONG, 1 UNSUPPORTED
stderr: .wiretruth/runs/20261013T134918.993664Z
```

The LiteLLM run grades its 95 cells 81 PASS, 13 WRONG and 1 UNSUPPORTED. Every cell, with its evidence, is in
[`results/litellm/1.105.1.json`](https://github.com/kartsan03/wiretruth/blob/main/results/litellm/1.105.1.json).
The pydantic-ai run grades its 95 cells 58 PASS, 14 LOSSY, 8 WRONG and 15 UNSUPPORTED (native Ollama), in
[`results/pydantic-ai/2.56.0.json`](https://github.com/kartsan03/wiretruth/blob/main/results/pydantic-ai/2.56.0.json).

No API keys are needed. Everything runs against stored responses on `127.0.0.1`. The first run downloads the
selected library into `.wiretruth/envs/`; later runs work offline. `uv run wiretruth run --shim shims/reference`
runs the built-in reference client, which must pass every cell. The scenarios, fixtures and shims live in this
repository, so wiretruth runs from a clone.

Releases are also on [PyPI](https://pypi.org/project/wiretruth/) (`pip install wiretruth`). The package is the
command only, so run it inside a checkout of the matching release tag (for example `v0.2.0`); on `main`, use
`uv run` as above.

## Add your library

A shim is a small program. It reads a call spec as JSON on stdin, calls your library the way its docs show, and
prints what the library reported as JSON on stdout. The
[any-llm shim](https://github.com/kartsan03/wiretruth/blob/main/shims/python/any-llm/shim.py),
[LiteLLM shim](https://github.com/kartsan03/wiretruth/blob/main/shims/python/litellm/shim.py) and
[pydantic-ai shim](https://github.com/kartsan03/wiretruth/blob/main/shims/python/pydantic-ai/shim.py) are examples. The
protocol is in [shims/README.md](https://github.com/kartsan03/wiretruth/blob/main/shims/README.md); to propose a
library, [open a new-library issue](https://github.com/kartsan03/wiretruth/issues/new?template=new-library.yml).

## Related work

- [Open Responses](https://github.com/openresponses/openresponses): a spec and compliance tests for servers that
  implement the Responses API. wiretruth tests clients across several native APIs.
- [LLMConform](https://github.com/aitk-org/LLMConform): conformance tests for LLM gateways and APIs, run live.
- [llmprobe](https://github.com/ivanfioravanti/llmprobe) and [conform](https://github.com/NiLabs-Org/conform):
  conformance for inference engines (vLLM, llama.cpp, Ollama, …).
- [aimock](https://github.com/CopilotKit/aimock): a mock server for testing AI apps, with record and replay. Use it
  for your app's tests; wiretruth audits the SDK layer itself.
- [spantruth](https://github.com/Plexito-de/spantruth): a separate project, called wiretruth until 2026-10-12.
  It replays provider responses through LLM instrumentation libraries (OpenLLMetry, OpenInference, the OpenTelemetry
  GenAI instrumentation) and checks what they write on spans. Use it for your traces; wiretruth checks what the SDK
  returns to the caller.
- The design borrows from [JSONTestSuite](https://github.com/nst/JSONTestSuite),
  [Wycheproof](https://github.com/C2SP/wycheproof), [toml-test](https://github.com/toml-lang/toml-test) and
  [compat-table](https://github.com/compat-table/compat-table).

## Author note

I've contributed fixes to libraries that wiretruth tests or will test:
[mozilla-ai/any-llm#1389](https://github.com/mozilla-ai/any-llm/pull/1389) and
[TanStack/ai#1548](https://github.com/TanStack/ai/pull/1548). Grades are computed by the code in this repository.
If you think a grade is wrong, please
[open a grade dispute](https://github.com/kartsan03/wiretruth/issues/new?template=grade-dispute.yml).

## Contributing

Scenario ideas, new libraries and grade disputes are all welcome. See
[CONTRIBUTING.md](https://github.com/kartsan03/wiretruth/blob/main/CONTRIBUTING.md).

## Citing

See [CITATION.cff](https://github.com/kartsan03/wiretruth/blob/main/CITATION.cff).

## License

MIT. Fixtures contain model outputs recorded from provider APIs;
[docs/recording.md](https://github.com/kartsan03/wiretruth/blob/main/docs/recording.md) explains how they're recorded.
