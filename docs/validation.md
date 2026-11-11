# Validation against fixed bugs

These bugs reproduce in committed results before an upstream fix and improve after it. The fixes provide
independent confirmation of the findings: library maintainers agreed there was a problem and changed the
library. Each grade below comes from the linked results files; the versions are the tested releases, which can
be later than the first release containing the fix.

| Bug and fix | Found by | Scenario and providers | Before → grade | After → grade | Results files |
|---|---|---|---|---|---|
| any-llm [#1471](https://github.com/mozilla-ai/any-llm/issues/1471): unfamiliar finish values rejected; fixed by [#1504](https://github.com/mozilla-ai/any-llm/pull/1504) | wiretruth | `finish.unknown-value`: `openai-chat`, `ollama-openai` | 1.33.0 → WRONG | 1.38.0 → PASS | [before](../results/any-llm/1.33.0.json), [after](../results/any-llm/1.38.0.json) |
| any-llm [#1472](https://github.com/mozilla-ai/any-llm/issues/1472): tool-call ids replaced; fixed by [#1514](https://github.com/mozilla-ai/any-llm/pull/1514) | wiretruth | `tools.parallel-calls`, `tools.parallel-calls-stream`: `gemini` | 1.33.0 → WRONG | 1.38.0 → PASS | [before](../results/any-llm/1.33.0.json), [after](../results/any-llm/1.38.0.json) |
| Vercel AI SDK [#23450](https://github.com/vercel/ai/issues/23450): in-stream error ignored; fixed by [#23459](https://github.com/vercel/ai/pull/23459) in `@ai-sdk/google` 4.0.99 | wiretruth | `stream.error-midstream`: `gemini` | `ai` 7.0.142 (Google 4.0.98) → WRONG | `ai` 7.0.146 (Google 4.0.101) → PASS | [before](../results/vercel-ai/7.0.142.json), [after](../results/vercel-ai/7.0.146.json) |
| Vercel AI SDK [#23449](https://github.com/vercel/ai/issues/23449): incomplete stream reported as finished; fixed by [#23814](https://github.com/vercel/ai/pull/23814) | wiretruth | `stream.missing-finish`: `openai-chat`, `openai-responses`, `anthropic`, `gemini` | `ai` 7.0.146 → WRONG | `ai` 7.0.153 → PASS | [before](../results/vercel-ai/7.0.146.json), [after](../results/vercel-ai/7.0.153.json) |
| TanStack AI [#1853](https://github.com/TanStack/ai/issues/1853): truncated tool call loses the length finish and triggers retries; fixed by [#1874](https://github.com/TanStack/ai/pull/1874) | wiretruth | `truncation.tool-call-args-stream`: `openai-chat` | 0.71.0 → WRONG | 1.0.0 → PASS | [before](../results/tanstack-ai/0.71.0.json), [after](../results/tanstack-ai/1.0.0.json) |
| TanStack AI [#1852](https://github.com/TanStack/ai/issues/1852): native Ollama stream cut by the token limit finishes as `stop`; fixed by [#2077](https://github.com/TanStack/ai/pull/2077) in `@tanstack/ai-ollama` 1.1.1 | wiretruth | `truncation.text-stream`: `ollama` | 1.0.0 → WRONG | 1.4.0 → PASS | [before](../results/tanstack-ai/1.0.0.json), [after](../results/tanstack-ai/1.4.0.json) |
| LiteLLM [#46542](https://github.com/BerriAI/litellm/issues/46542): a stream that ends without a finish reason is reported as `stop`; fixed by [#47665](https://github.com/BerriAI/litellm/pull/47665) | wiretruth | `stream.missing-finish`: `anthropic`, `ollama`, `ollama-openai`, `openai-chat` | 1.105.5 → WRONG | 1.109.0 → PASS | [before](../results/litellm/1.105.5.json), [after](../results/litellm/1.109.0.json) |
| pydantic-ai [#7816](https://github.com/pydantic/pydantic-ai/issues/7816): unfamiliar finish values rejected; fixed by [#11119](https://github.com/pydantic/pydantic-ai/pull/11119) | reported by others, confirmed by wiretruth | `finish.unknown-value`: `openai-chat`, `ollama-openai` | 2.56.0 → WRONG | 2.61.0 → LOSSY | [before](../results/pydantic-ai/2.56.0.json), [after](../results/pydantic-ai/2.61.0.json) |

pydantic-ai keeps the response and the original value in `provider_details`, but exposes no finish reason.
That is LOSSY, not PASS. wiretruth's later report was closed as a duplicate of #7816.

## Reproduction fixtures

Models below are each fixture's `provenance.returned_model`; dates are from `provenance.recorded_at` for
recordings. Constructed fixtures are labelled `derived` or `documented` instead of being given a recording date.
Replaying fixtures recorded on today's models is fine: the wire shape is what matters, not the model name.

- **any-llm #1471 and pydantic-ai #7816:** `openai-chat`
  [finish.unknown-value](../suite/fixtures/openai-chat/finish.unknown-value.json) (`gpt-6-luna`, derived), and
  `ollama-openai` [finish.unknown-value](../suite/fixtures/ollama-openai/finish.unknown-value.json)
  (`qwen3.5:0.8b`, derived).
- **any-llm #1472:** `gemini` [tools.parallel-calls](../suite/fixtures/gemini/tools.parallel-calls.json) and
  [tools.parallel-calls-stream](../suite/fixtures/gemini/tools.parallel-calls-stream.json)
  (both `gemini-3.5-flash-lite`, recorded 2026-10-13).
- **Vercel #23450:** `gemini` [stream.error-midstream](../suite/fixtures/gemini/stream.error-midstream.json)
  (`gemini-3.5-flash-lite`, derived). The error frame is injected into a recording; no live mid-stream Gemini
  error has been observed.
- **Vercel #23449:** [stream.missing-finish](../suite/fixtures/openai-chat/stream.missing-finish.json) over
  `openai-chat` (`gpt-6-luna`, derived), [openai-responses](../suite/fixtures/openai-responses/stream.missing-finish.json)
  (`gpt-6-luna`, derived), [anthropic](../suite/fixtures/anthropic/stream.missing-finish.json)
  (`claude-sonnet-5-5`, derived from documented), and
  [gemini](../suite/fixtures/gemini/stream.missing-finish.json) (`gemini-3.5-flash-lite`, derived).
- **TanStack #1853:** `openai-chat`
  [truncation.tool-call-args-stream](../suite/fixtures/openai-chat/truncation.tool-call-args-stream.json)
  (`gpt-6-luna`, recorded 2026-10-11). This is the only cell whose grade changed between these TanStack results.
- **TanStack #1852:** `ollama` [truncation.text-stream](../suite/fixtures/ollama/truncation.text-stream.json)
  (`qwen3.5:0.8b`, recorded 2026-10-12).
- **LiteLLM #46542:** [stream.missing-finish](../suite/fixtures/openai-chat/stream.missing-finish.json) over
  `openai-chat` (`gpt-6-luna`, derived), [anthropic](../suite/fixtures/anthropic/stream.missing-finish.json)
  (`claude-sonnet-5-5`, derived from documented), and [ollama](../suite/fixtures/ollama/stream.missing-finish.json)
  and [ollama-openai](../suite/fixtures/ollama-openai/stream.missing-finish.json) (both `qwen3.5:0.8b`, derived).
  The `gemini` cell stays WRONG in 1.109.0.

The other consecutive result comparisons add no fixed-bug rows: LiteLLM 1.105.1 → 1.105.5 and any-llm
1.38.0 → 1.40.2 have unchanged grades. The Vercel comparisons change only the cells listed above.
TanStack 1.0.0 → 1.4.0 also moves `stream.missing-finish` over `anthropic` LOSSY → PASS
([#2042](https://github.com/TanStack/ai/pull/2042), `@tanstack/ai-anthropic` 1.1.1); wiretruth graded that gap but
filed no issue for it, so it has no row above.

## Not checked yet

- LiteLLM's older Gemini empty-candidate bug (`truncation.reasoning-only`): no committed results for a release
  before the fix.
- any-llm [#1389](https://github.com/mozilla-ai/any-llm/pull/1389) (`request.no-injected-options`): no committed
  results for a release before the fix.
- TanStack [#1548](https://github.com/TanStack/ai/pull/1548) (`truncation.structured-output` over `ollama`): no
  committed results for the older adapter versions on either side of the fix.
- TanStack [#1426](https://github.com/TanStack/ai/issues/1426)/[#1427](https://github.com/TanStack/ai/pull/1427):
  not runnable with the current shim; `chat({ outputSchema })` always streams, so the non-streamed fixture is
  UNSUPPORTED.
