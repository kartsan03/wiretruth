# wiretruth

wiretruth replays real LLM provider responses through LLM SDKs and checks whether each SDK reports what actually
happened.

**Does your LLM SDK tell you the truth?**

[![CI](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml/badge.svg)](https://github.com/kartsan03/wiretruth/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wiretruth?label=PyPI)](https://pypi.org/project/wiretruth/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/kartsan03/wiretruth/blob/main/LICENSE)

So far wiretruth tests one library, any-llm, on the OpenAI Chat Completions API with 12 scenarios. More providers
and libraries follow, and a public results matrix arrives in v0.4. Until then, the grades are in
[`results/`](https://github.com/kartsan03/wiretruth/tree/main/results).

## Why

A model hits its output token limit halfway through a JSON object. The provider says so plainly:
`"finish_reason": "length"`. What your code sees depends on the SDK in between. If the SDK raises a generic JSON
parse error, nothing tells you that a higher limit would fix it. If it returns the partial object as a success,
your pipeline stores an incomplete record.

wiretruth has this case recorded as `truncation.structured-output` on `openai-chat`. Replayed through any-llm:

| Library | What the caller gets | Grade |
|---|---|---|
| any-llm 1.33.0 | Raises `openai.LengthFinishReasonError`. Its `completion` has `finish_reason: "length"` and the token usage. | ✓ PASS |

wiretruth runs the same recorded edge cases through every library it supports and grades each one against what the
provider sent.

## What it checks

| Bug class | What goes wrong for users | Scenarios |
|---|---|---|
| Silent truncation | Agents accept partial output or retry without raising the limit | 5 |
| Finish reason mapping | Control flow branches on the wrong reason | planned |
| Usage accounting | Cost and quota tracking is off (cached and reasoning tokens) | 1 |
| Stream integrity | Corrupted or unfinished text is treated as final | planned |
| Tool call fidelity | Tools run with broken or merged arguments | 4 |
| Error classification | Retries and backoff misbehave | planned |
| Request fidelity | The library sends fields the caller didn't set, or ones the model rejects | 2 |

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

## Quickstart

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/), which builds each library's own environment.

```bash
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv run wiretruth run --lib any-llm
```

```text
scenario                           openai-chat
request.max-tokens-field           ✓ PASS
request.tools-with-reasoning-chat  ✓ PASS
tools.parallel-calls               ✓ PASS
tools.parallel-calls-stream        ✓ PASS
truncation.reasoning-only          ✓ PASS
truncation.structured-output       ✓ PASS
truncation.text                    ✓ PASS
truncation.text-stream             ✓ PASS
truncation.text-stream-usage       ✓ PASS
truncation.tool-call-args          ✓ PASS
truncation.tool-call-args-stream   ✓ PASS
usage.stream-final-chunk           ✓ PASS

any-llm 1.33.0: 12 cells, 12 PASS
stderr: .wiretruth/runs/20261011T120546.713943Z
```

No API keys are needed. Everything runs against recorded responses on `127.0.0.1`. The first run downloads any-llm
into `.wiretruth/envs/`; later runs work offline. `uv run wiretruth run --shim shims/reference` runs the
built-in reference client, which must pass every cell. The scenarios, fixtures and shims live in this repository,
so wiretruth runs from a clone.

Releases are also on [PyPI](https://pypi.org/project/wiretruth/) (`pip install wiretruth`). The package is the
command only, so run it inside a checkout of the matching release tag (for example `v0.1.0`); on `main`, use
`uv run` as above.

## Add your library

A shim is a small program. It reads a call spec as JSON on stdin, calls your library the way its docs show, and
prints what the library reported as JSON on stdout. The
[any-llm shim](https://github.com/kartsan03/wiretruth/blob/main/shims/python/any-llm/shim.py) is 150 lines. The
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
