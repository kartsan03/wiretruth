# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Fixture, scenario and result formats are versioned
separately (`wiretruth.fixture/v1`, …).

The **Results** section lists grade changes for tested libraries (for example, a library release that fixes a
finding).

## [Unreleased]

### Added
- Anthropic Messages provider for JSON and SSE, with truth extraction, transforms and shim mapping.
- Thirteen documented Anthropic fixtures and five derived stream/finish fixtures, with source links and assumptions.
- Cached-token usage and legacy forced-tool structured-output truncation scenarios.
- `gemini` GenerateContent support for JSON and SSE: recording, truth extraction, transforms and shim
  mapping, using `gemini-3.5-flash-lite` by default. Thinking usage includes thoughts and preserves the
  candidates-only alternative. Empty candidates and optional function-call ids are handled.
- Gemini scenario coverage (the tool-argument truncation scenarios stay off: a cut sends no call part), including `usage.reasoning-tokens`, with separate recording dispatches for
  plain and thinking calls. The any-llm shim uses its native Gemini provider and pins `google-genai`.
- Google `AQ.` credential detection, masked Gemini header and query credentials, and query-free fixture
  match paths for streamed GenerateContent requests.
- Gemini fixtures: nine scenarios recorded in CI against `gemini-3.5-flash-lite` (run
  [38381941285](https://github.com/kartsan03/wiretruth/actions/runs/38381941285)), `usage.reasoning-tokens` against
  `gemini-3.8-flash` (run [38381937993](https://github.com/kartsan03/wiretruth/actions/runs/38381937993)),
  `truncation.reasoning-only` on `gemini-3.5-flash-lite` at thinking level HIGH (run
  [38382529221](https://github.com/kartsan03/wiretruth/actions/runs/38382529221); `gemini-3.8-flash` answered 503
  three times), and five derived from them (missing finish, aborted stream, a 503 error frame mid-stream,
  unfamiliar finish value, UTF-8 split inside characters).
- `ollama` and `ollama-openai` support recording, truth extraction, lint, transforms and shim mapping,
  using `qwen3.5:0.8b` by default. Native streams record as NDJSON lines, with byte chunks for irregular
  framing or aborted responses. Both providers record without an API key.
- The record workflow installs a checksum-verified Ollama release, pulls the model and records against
  the local server, with a 40-minute timeout for Ollama jobs and the server log in the summary.
- `finish.stop-sequence` checks stop and text fidelity on both Ollama APIs. `request.no-injected-options`
  checks that a minimal native call leaves `options.num_ctx` unset. `request.max-tokens-ollama-openai`
  checks that the output limit is sent as `max_tokens`, the only field Ollama's OpenAI-compatible
  endpoint applies.
- Ollama fixtures: ten recorded per provider in CI against `qwen3.5:0.8b` (runs
  [38294709109](https://github.com/kartsan03/wiretruth/actions/runs/38294709109) and
  [38294739276](https://github.com/kartsan03/wiretruth/actions/runs/38294739276)), and nine derived from them
  (missing finish, aborted stream, native error line, unfamiliar finish value, UTF-8 split inside characters).
- Six scenarios for missing stream finishes (clean and aborted), errors during streams, unfamiliar finish
  values and UTF-8 text (unsplit and split inside characters). Eight derived fixtures cover the first four
  scenarios on `openai-chat` and `openai-responses`. `stream.utf8` is recorded in CI against `gpt-6-luna` on
  both providers, and `stream.utf8-split` re-chunks those recordings so that boundaries fall inside 2-, 3- and
  4-byte characters.
- `raw_finish_matches_truth` checks that the provider's raw finish value is preserved on any surface.
- `outcome_is_returned` checks that a library hands back a response that carried no error, instead of raising
  or returning an error for it.
- `wiretruth derive` creates derived fixtures with recomputed truth using six transforms: `cut_stream_after`,
  `strip_finish`, `inject_error_event`, `rechunk_inside_utf8`, `replace_finish_value` and `prepend_http_error`.
- `openai-responses` supports recording, truth extraction, lint and replay. The any-llm shim maps it through
  `any_llm.responses`. The truncated tool-argument scenarios (plain and streamed), reasoning-only truncation
  and parallel tool-call scenarios (plain and streamed) now list the provider.
- `openai-responses` fixtures for eight scenarios, recorded in CI against `gpt-6-luna`: text, streamed text and
  structured output cut by the token limit, cut tool-call arguments (plain and streamed), reasoning-only
  truncation, and parallel tool calls (plain and streamed).
- Nine `openai-chat` scenarios for truncated tool arguments, streamed usage, reasoning-only truncation,
  parallel tool calls, token-limit request fields and tools with default reasoning, with fixtures recorded in CI
  against `gpt-6-luna`.
- `wiretruth record --scenario` and `wiretruth run --scenario` accept comma-separated ids or globs, combine
  matches without duplicates, and reject any part that matches nothing.
- `wiretruth lint` reports unknown requirement check names, with the scenario path and requirement id.
- The any-llm shim maps tools, tool choice and stop sequences, and reads tool calls from results, streams and
  error completions.

### Changed
- `outcome_is_error` counts returned or yielded error objects, with the same category rule as raised errors.
- The any-llm shim reports no completion signal for Chat Completions streams, since iteration alone does not
  distinguish a complete stream from one that ends early.
- Truth extraction from an aborted byte body accepts an incomplete trailing UTF-8 sequence, while still
  rejecting invalid bytes elsewhere.
- The liar shim claims every stream completed, maps unfamiliar finishes to stop, and replaces the first
  non-ASCII text character with U+FFFD.
- The plain and streamed text truncation scenarios use an octopus prompt, and structured-output truncation
  uses Greek islands. An openai-chat override keeps the earlier calls its fixtures were recorded with; every
  other provider uses the current prompts.
- `no_complete_tool_call_from_partial_args` accepts visibly partial, unparsed calls; reporting parsed arguments
  where the wire carried partial arguments still contradicts. Truncation signalling is checked separately.
- `request_field_equals` contradicts a JSON request missing the required field, so omitted settings no longer
  grade as unobservable.
- `text_matches_truth` accepts reporting no text when the wire carried null or empty text, and rejects invented
  answer text in that case.

### Fixed
- Complete tool-call arguments with duplicate JSON keys no longer read as partial. Tool arguments and structured
  text use the last value for each duplicate key, as clients do, while retaining strict number and Unicode checks.

### Results
- any-llm 1.33.0: PASS on 16 of 18 `openai-chat` scenarios, 13 of 14 `openai-responses`, 11 of 15 `ollama`, 11
  of 14 `ollama-openai` and 12 of 16 `gemini` ([results/any-llm/1.33.0.json](results/any-llm/1.33.0.json)). LOSSY on `truncation.structured-output` over
  `openai-responses`: structured output raises a pydantic `ValidationError` on the cut-off JSON, with no sign that
  the token limit was hit, and without the partial text or usage.
- any-llm 1.33.0: WRONG on `finish.unknown-value` over `openai-chat`: a finish value outside OpenAI's five raises
  a pydantic `ValidationError`, so the caller loses a valid response. LOSSY on `stream.missing-finish` over
  `openai-chat`: Chat Completions streams give no completion signal, so nothing tells the caller that the
  stream ended early. The same two cells on `ollama-openai`, and LOSSY `stream.missing-finish` on `ollama`.
- any-llm 1.33.0 on Ollama, recorded with `qwen3.5:0.8b` on Ollama v0.40.3. WRONG on `tools.parallel-calls` and
  `tools.parallel-calls-stream` over `ollama`: the native provider generates UUIDs for tool calls because the
  `ollama` Python client has no field for the `call_*` ids Ollama sends
  ([ollama/ollama-python#667](https://github.com/ollama/ollama-python/issues/667)). WRONG on `finish.unknown-value`
  over `ollama`: an unfamiliar `done_reason` is reported as `stop`, a fallback any-llm documents in its code. WRONG on
  `request.max-tokens-ollama-openai`: the `openai` provider sends `max_completion_tokens`, which Ollama's
  OpenAI-compatible endpoint ignores ([ollama/ollama#7125](https://github.com/ollama/ollama/issues/7125)). That
  cell is tied to Ollama v0.40.3: it checks the request, so it stays WRONG until the scenario is retired.
- any-llm 1.33.0 on Gemini, recorded with `gemini-3.5-flash-lite` and `gemini-3.8-flash`. PASS on every
  token-usage scenario, including `usage.reasoning-tokens` (output includes thoughts) and the empty candidate in
  `truncation.reasoning-only`. WRONG on `tools.parallel-calls` and `tools.parallel-calls-stream`: the Gemini
  provider builds its own `call_{hash(name)}_{n}` ids and ignores the id Gemini sends
  ([mozilla-ai/any-llm#1472](https://github.com/mozilla-ai/any-llm/issues/1472)). WRONG on
  `finish.unknown-value`: a finish reason without an OpenAI counterpart is reported as `stop`, a fallback any-llm
  documents in its code. LOSSY on `stream.missing-finish`: no completion signal for streams.

## [0.1.0] - 2026-10-11

### Added
- `wiretruth run --lib NAME --write-results` writes `results/<library>/<version>.json`: each cell's grade,
  requirement evidence and observation, and every package installed in the library's environment. It requires
  a checkout clean outside `results/` and refuses HARNESS cells.
  `--check-against [FILE]` exits 1 if a grade or requirement status differs from a results file, by default the one
  for the library's version; package drift is a note. The conformance workflow checks each library against its
  committed results.
- `wiretruth run` prints a table of grades, scenarios by providers, then each cell's notes and a count per grade.
  Every requirement that isn't satisfied adds a note with its evidence, so a WRONG or LOSSY cell says why.
- `results/upstream.json` tracks findings reported to library maintainers. It's empty so far.
- A publish workflow builds the package on a version tag and publishes it to PyPI with trusted publishing.
- `wiretruth run --lib NAME` runs the library shim in `shims/*/NAME/` in its own virtual environment, built with
  uv from the manifest's exact pins and cached under `.wiretruth/envs/py/`. `wiretruth envs` lists the cached
  environments, `--build NAME` builds one ahead of an offline run, and `--prune` removes every environment that
  doesn't match a manifest's current pins, half-built ones included.
- The first library shim: any-llm 1.33.0 on `openai-chat`, plain and streamed, with structured output through a
  Pydantic model, the form any-llm's docs show.
- A conformance workflow runs each library shim against the suite with no network access except loopback,
  checks its grades, and keeps each cell's stderr as an artifact.
- `wiretruth run --shim PATH` grades a shim on every cell that has a fixture: one replay server and one shim
  process per cell, in parallel, with a 20 s timeout. It prints each cell's grade and notes, and keeps each cell's
  stderr under `.wiretruth/runs/`. The shim gets a fixed environment, so local API keys, base URLs and proxy
  settings never reach it. `--check-grades FILE` exits 1 if any grade or requirement status differs from the file.
  A library that returns or raises without its request reaching the replay server, through a wrong base URL for
  example, grades HARNESS, not as a finding about the library; a `validation` or `internal` error raised before
  the request is still graded.
- Two built-in shims test the suite in CI: `shims/reference`, a plain HTTP client that must pass every
  requirement, and `shims/liar`, which misreports on purpose and must match its expected grades.
- Requirement checks and pure cell grading, with evidence for each requirement and the detailed rules in
  `docs/grading.md`.
- Project scaffolding: package skeleton, CLI entry point, CI for lint, types and tests.
- Concepts guide (`docs/concepts.md`) and the first design decision records (`docs/adr/`).
- JSON Schemas (Draft 2020-12) for every format: scenarios, fixtures, call specs, observations, shim manifests,
  results, upstream tracking and bug classes, shipped in the package. Field reference: `docs/formats.md`.
- `wiretruth lint` validates everything under `suite/` and `results/` and checks the files against each other.
- Fixture variants are dated (`<scenario>@<date>.json`): an older recording kept for reference, which no cell
  uses by default. A streamed, aborted or otherwise different form of a call is its own scenario.
- The bug-class registry, `suite/bug-classes.json`, with seven classes.
- `wiretruth serve <fixture>` replays one fixture on 127.0.0.1 for manual reproduction, until Ctrl-C. The replay
  server sends stream events, NDJSON lines and raw chunks one HTTP chunk each, can drop the connection before the
  final chunk, and logs every request without credentials, noting whether each response was sent in full.
- Lint rejects an `abort` ending on a `json` or `text` body, and an empty `chunks_b64` chunk.
- `wiretruth record` records `openai-chat` responses into fixtures over plain HTTP, with no SDK in between. It
  keeps only the `content-type` and `retry-after` headers, stores the request, the models and a hash of the raw
  body, and computes the truth right away. A new recording becomes the fixture the cell uses; the one it replaces
  is kept as a dated variant. `--dry-run` prints the exact request with the key masked. A manually started
  `record` workflow records in CI and pushes a `record/*` branch.
- `wiretruth lint` recomputes each `openai-chat` fixture's truth from its exchanges, and scans every file under
  `suite/` and `results/` for keys, tokens, organization and project ids, and credential headers, including those
  behind JSON escapes or in the JSON of a stream event, also one split over several `data:` lines. The recorder runs
  the same scan on the raw body before it parses it, and on the fixture before it writes. Neither prints a flagged
  value in any other message.
- The first scenario, `truncation.text`: plain text cut off by the output token limit.
- Two more truncation scenarios: `truncation.text-stream`, the same text streamed, and
  `truncation.structured-output`, a JSON object for a strict schema cut off halfway.
- Each truncation scenario has a SHOULD requirement that the partial text is reported exactly as the provider sent
  it.
- The first recorded fixtures: the three truncation scenarios on `openai-chat`, recorded in CI against
  `gpt-6-luna`.

### Results
- any-llm 1.33.0, the first library results: PASS on all three truncation scenarios on `openai-chat`
  ([results/any-llm/1.33.0.json](results/any-llm/1.33.0.json)).

[Unreleased]: https://github.com/kartsan03/wiretruth/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kartsan03/wiretruth/releases/tag/v0.1.0
