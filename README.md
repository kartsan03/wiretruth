# wiretruth

wiretruth replays real LLM provider responses through LLM SDKs and checks whether each SDK reports what actually
happened.

**Does your LLM SDK tell you the truth?**

[![CI](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml/badge.svg)](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wiretruth?label=PyPI)](https://pypi.org/project/wiretruth/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/kartsan03/wiretruth/blob/main/LICENSE)

[![Pass counts per library on the wiretruth results site](https://raw.githubusercontent.com/kartsan03/wiretruth/main/docs/img/matrix.png)](https://kartsan03.github.io/wiretruth/)

wiretruth tests any-llm, LiteLLM, pydantic-ai, the Vercel AI SDK, TanStack AI and the OpenAI Python SDK across six
APIs: OpenAI Chat Completions and Responses, Ollama's native and OpenAI-compatible endpoints, Gemini GenerateContent
and Anthropic Messages. That is 24 scenarios and 100 cells per library, shown in the
[results matrix](https://kartsan03.github.io/wiretruth/) (rebuilt from
[`results/`](https://github.com/kartsan03/wiretruth/tree/main/results) on every push to `main`). Some libraries don't
speak every API, so those cells are UNSUPPORTED;
[shims/README.md](https://github.com/kartsan03/wiretruth/blob/main/shims/README.md) lists each shim's coverage.
Anthropic responses are transcribed from Anthropic's documentation, not recorded (see Fixture provenance below).

## Why

A model hits its output token limit halfway through a JSON object. The provider says so plainly: the OpenAI
Responses API returns `"status": "incomplete"` with `"incomplete_details": {"reason": "max_output_tokens"}`. What
your code sees depends on the SDK in between. If the SDK raises a generic JSON parse error, nothing tells you that
a higher limit would fix it. If it returns the partial object as a success, your pipeline stores an incomplete
record.

wiretruth has this response recorded as `truncation.structured-output` on `openai-responses`. Every library gets
the same bytes:

| Library | What the caller gets | Grade |
|---|---|---|
| [LiteLLM](https://github.com/kartsan03/wiretruth/blob/main/results/litellm/1.110.2.json) | Returns the response with `incomplete_details.reason: "max_output_tokens"`, the cut-off JSON as text and the token usage, and parses none of it. | ✓ PASS |
| [pydantic-ai](https://github.com/kartsan03/wiretruth/blob/main/results/pydantic-ai/2.71.0.json) | Raises `OutputTokenLimitExceeded`, which names the token limit. The cut-off text and the usage are only in the messages that `capture_run_messages()` keeps. | ✓ PASS |
| [Vercel AI SDK](https://github.com/kartsan03/wiretruth/blob/main/results/vercel-ai/7.0.179.json) | Raises `NoObjectGeneratedError` with `finishReason: "length"` and the token usage. | ✓ PASS |
| [any-llm](https://github.com/kartsan03/wiretruth/blob/main/results/any-llm/1.53.1.json) | Raises pydantic's `ValidationError` ("Invalid JSON: EOF while parsing a string"), with no finish reason and no usage. | ~ LOSSY |
| [OpenAI Python SDK](https://github.com/kartsan03/wiretruth/blob/main/results/openai-python/3.57.0.json) | `responses.parse()` raises pydantic's `ValidationError` ("Invalid JSON: EOF while parsing a string"), with no finish reason and no usage. | ~ LOSSY |

The last two rows are one known bug: any-llm calls the OpenAI SDK's `responses.parse()`, which parses the text
without checking the response status ([openai/openai-python#2486](https://github.com/openai/openai-python/issues/2486),
open upstream).

The table is a snapshot: each library name links to the results file its row comes from, and the
[scenario page](https://kartsan03.github.io/wiretruth/scenarios/truncation.structured-output.html) shows the current
grades. TanStack AI isn't listed: its structured output streams on this API, so this non-streamed fixture is
UNSUPPORTED there.

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
partial text in the truncation scenarios, add a note when they aren't met. See
[how grades are computed](https://github.com/kartsan03/wiretruth/blob/main/docs/grading.md), the
[scenario catalog](https://github.com/kartsan03/wiretruth/blob/main/docs/scenarios.md),
[fixed-bug validation](https://github.com/kartsan03/wiretruth/blob/main/docs/validation.md) and the
[FAQ](https://github.com/kartsan03/wiretruth/blob/main/docs/faq.md).

## Quickstart

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). Node.js and npm are needed only for the Vercel AI SDK
and TanStack AI shims; the runner builds their pinned dependencies itself. Everything runs against stored responses
on `127.0.0.1` with no API keys. The first run downloads the selected library into `.wiretruth/envs/` for Python or
`node_modules` next to the shim for JavaScript, and later runs work offline.

```bash
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv run wiretruth run --lib any-llm --provider openai-responses
```

This prints any-llm's `openai-responses` cells (full results in
[`results/any-llm/1.53.1.json`](https://github.com/kartsan03/wiretruth/blob/main/results/any-llm/1.53.1.json)):

```text
scenario                          openai-responses
finish.unknown-value              ✓ PASS
stream.error-midstream            ✓ PASS
stream.missing-finish             ✓ PASS
stream.missing-finish-abort       ✓ PASS
stream.utf8                       ✓ PASS
stream.utf8-split                 ✓ PASS
tools.parallel-calls              ✓ PASS
tools.parallel-calls-stream       ✓ PASS
truncation.reasoning-only         ✓ PASS
truncation.structured-output      ~ LOSSY
truncation.text                   ✓ PASS
truncation.text-stream            ✓ PASS
truncation.tool-call-args         ✓ PASS
truncation.tool-call-args-stream  ✓ PASS
usage.cached-tokens               ✓ PASS
usage.reasoning-tokens            ✓ PASS

truncation.structured-output openai-responses
  MUST cause-is-truncation: NOT_OBSERVABLE: finish.value=null (absent); truth.finish="length"; error.category="validation"; structured.returned_as_success=false
  SHOULD text-reported: NOT_OBSERVABLE: text.value=null (absent); truth.text="{\"islands\":[{\"name\":\"Κρήτη\",\"sea\":\"Κρητικό Πέ"
  SHOULD usage-reported: NOT_OBSERVABLE: usage.input=null (absent); truth.usage.input=68; usage.output=null (absent); truth.usage.output=24

any-llm 1.53.1: 16 cells, 15 PASS, 1 LOSSY
stderr: .wiretruth/runs/20261124T042419.885741Z
```

That LOSSY row is the same cell shown in the Why table above.

- Other `--lib` values: `litellm`, `pydantic-ai`, `vercel-ai`, `tanstack-ai` and `openai-python`.
- Leave out `--provider` to run all 100 cells and print evidence for every cell that isn't PASS.
- Pass `--scenario` with ids or globs, such as `--scenario 'tools.*'`, to run a subset.
- `uv run wiretruth run --shim shims/reference` runs the built-in reference client, which must pass every cell.
- `uv run wiretruth site` builds the matrix into `_site/`.

A weekly workflow runs each library's latest releases and proposes the new results as a pull request.

Releases are also on [PyPI](https://pypi.org/project/wiretruth/), where `pip install wiretruth` installs only the
command. The scenarios, fixtures and shims live in the repository, so run it inside a checkout of the matching
release tag (for example `v0.5.0`); on `main`, use `uv run`.

## Reproduce a cell without wiretruth

Every cell can be reproduced with plain library code. `wiretruth serve` serves one fixture on `127.0.0.1`:

```bash
uv run wiretruth serve openai-responses/truncation.structured-output --port 8765
```

```text
wiretruth serve: openai-responses/truncation.structured-output on http://127.0.0.1:8765
openai-responses base URL: http://127.0.0.1:8765/v1
Press Ctrl-C to stop.
```

In another terminal, call it the way you normally would. With the OpenAI Python SDK (`pip install openai`):

```python
from openai import OpenAI
from pydantic import BaseModel


class Island(BaseModel):
    name: str
    sea: str


class Islands(BaseModel):
    islands: list[Island]


client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
client.responses.parse(model="gpt-6-luna", input="List ten Greek islands.", text_format=Islands)
```

```text
Traceback (most recent call last):
  ...
pydantic_core._pydantic_core.ValidationError: 1 validation error for Islands
  Invalid JSON: EOF while parsing a string at line 1 column 59 [type=json_invalid, input_value='{"islands":[{"name":"Κ...a":"Κρητικό Πέ', input_type=str]
```

The error doesn't say that the response stopped at `max_output_tokens`. The server answers every request to that
path with the recorded response, so the prompt and the model don't change what comes back.

## Add your library

A shim is a small program. It reads a call spec as JSON on stdin, calls your library the way its docs show, and
prints what the library reported as JSON on stdout. The
[shims in this repository](https://github.com/kartsan03/wiretruth/tree/main/shims) are examples. The steps are in
[Writing a shim](https://github.com/kartsan03/wiretruth/blob/main/docs/writing-a-shim.md); to propose a library,
[open a new-library issue](https://github.com/kartsan03/wiretruth/issues/new?template=new-library.yml).

## How it works

```mermaid
flowchart LR
    R[recorded provider bytes] --> S[replay server on 127.0.0.1]
    S <--> L[the library, via its base URL option]
    L --> O[what the library reported]
    O --> G[grade against the wire truth]
    G --> M[matrix]
```

- **Fixtures** are provider HTTP responses stored with their provenance: for a recorded one, when, which model and
  which CI run. Stream events are kept as sent; JSON bodies are parsed and written back in the same key order.
- **Truth** is computed from the wire by a small parser for each provider API, with no SDK involved.
- **Shims** call each library the way its documentation shows, with default settings, and report only what its
  public API exposes. A setting a fixture needs is declared in the shim's manifest.
- **Checks** compare what the library reported with the truth. **Grades** fold the checks into one verdict per cell.
- **Self-tests:** a reference client must pass every cell, and a deliberately wrong client must fail in known ways.

## Fixture provenance

- **Recorded:** real provider responses, recorded by a
  [manual CI workflow](https://github.com/kartsan03/wiretruth/blob/main/.github/workflows/record.yml) whose runs
  are public. Each fixture links its run.
- **Documented:** transcribed from official API docs when there is no key for that provider (Anthropic today).
  Each fixture links its source page.
- **Derived:** a recorded or documented response with one deterministic change, such as a stream that stops before
  its finish event, for cases a provider can't be made to produce on demand. Each fixture gives the reason and
  evidence that the shape happens.

Every cell shows its tier. By default, re-recording keeps the old fixture in the suite as a dated file. Details:
[docs/recording.md](https://github.com/kartsan03/wiretruth/blob/main/docs/recording.md) and
[docs/derived-fixtures.md](https://github.com/kartsan03/wiretruth/blob/main/docs/derived-fixtures.md).

## Disclosure and upstream fixes

A cell turns WRONG, CRASH or HANG on `main` only after the finding is on record upstream: an issue this project
opened, an existing issue it links to, or the library's own documentation of the behavior. When a fix ships, the
weekly run picks up the release, and the changelog's Results section names the fix. Every report and its status is
in [`results/upstream.json`](https://github.com/kartsan03/wiretruth/blob/main/results/upstream.json), and each cell
page on the site shows the reports for that cell. The policy:
[docs/disclosure.md](https://github.com/kartsan03/wiretruth/blob/main/docs/disclosure.md).

## Related work

- [Open Responses](https://github.com/openresponses/openresponses): a spec and compliance tests for servers that
  implement the Responses API. wiretruth tests clients across several native APIs.
- [LLMConform](https://github.com/aitk-org/LLMConform): conformance tests for LLM gateways and APIs, run live.
- [llmprobe](https://github.com/ivanfioravanti/llmprobe) and [conform](https://github.com/NiLabs-Org/conform):
  conformance for inference engines (vLLM, llama.cpp, Ollama, …).
- [aimock](https://github.com/CopilotKit/aimock): a mock server for testing AI apps, with record and replay. Use it
  for your app's tests; wiretruth audits the SDK layer itself.
- [spantruth](https://github.com/Plexito-de/spantruth): a separate project (called wiretruth until 2026-10-12) that
  replays provider responses through LLM instrumentation libraries and checks what they write on spans. Use it for
  your traces; wiretruth checks what the SDK returns to the caller. A
  [joint page on dropped streams](https://github.com/Plexito-de/spantruth/blob/main/results/2026-11-24-dropped-streams/README.md)
  shows both layers' results for the same responses, with the wiretruth grades taken from `v0.4.0`.
- The design borrows from [JSONTestSuite](https://github.com/nst/JSONTestSuite),
  [Wycheproof](https://github.com/C2SP/wycheproof), [toml-test](https://github.com/toml-lang/toml-test) and
  [compat-table](https://github.com/compat-table/compat-table).

## Author note

I've contributed fixes to libraries that wiretruth tests or will test:
[mozilla-ai/any-llm#1389](https://github.com/mozilla-ai/any-llm/pull/1389),
[mozilla-ai/any-llm#1433](https://github.com/mozilla-ai/any-llm/pull/1433) and
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
