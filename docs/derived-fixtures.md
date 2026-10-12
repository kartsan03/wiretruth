# Derived fixtures

This page is for contributors who construct response shapes that are hard to record on demand. A derived fixture
starts with a recorded or documented fixture and changes it with a named transform. It stores the source id,
transform, arguments, rationale and evidence URL. A derived fixture cannot be the source of another derivation.

## Create a fixture

Run from the repository root:

```text
wiretruth derive --from <fixture id> --transform <name> --out <fixture id> [--args '<JSON object>']
                 [--rationale TEXT] [--evidence-url URL] [--replace]
```

The command reads `suite/fixtures/<source id>.json` and writes `suite/fixtures/<out id>.json`. The output id is
`<provider>/<scenario>`; its provider must match the source. `--args` defaults to `{}`. Each transform supplies a
rationale sentence and the evidence URL shown below. The two optional flags override them.

The output has tier `derived`. Its provenance copies only `requested_model`, `returned_model` and `request`,
when present. Recording time, recorder version, raw hash, source commit, CI run, source URL and notes describe the
source and are not copied. `derived_from.fixture` points to that source.

Truth is recomputed from the transformed bytes by the provider's existing extractor, using the copied request.
With several exchanges, truth comes from the last exchange: the response the caller ends up with. In particular,
`prepend_http_error` leaves truth describing the original final response, not the initial error.

A derived fixture never replaces a recorded or documented one, even with `--replace`. An existing derived fixture
can be overwritten only with `--replace`. The output is schema-validated before writing. Identical source bytes,
arguments and options produce identical output. Retained stream events keep their bytes; a rewritten event keeps
its key order, JSON spacing and Unicode escaping. An event layout the transform cannot preserve is rejected.

Each output scenario must exist and list its provider before `wiretruth lint` will pass. The command does not
create scenarios. The examples below match the committed fixtures except where noted.

| Exit code | Meaning |
|---|---|
| 0 | The fixture was written; stdout names its path |
| 1 | A fixture document is invalid |
| 2 | A source or transform is unknown, arguments are invalid, the shape is unsupported, or the file cannot be written |

Today the provider helpers support `openai-chat` and `openai-responses`. Both use SSE for stream finish signals
and error events. A transform fails when the provider cannot read the resulting body; it does not infer truth.

## `cut_stream_after`

Keep the first `n` stored events of the last exchange and discard the rest. Set its ending to `end`. The body must
be `sse` or `ndjson`; `n` must be an integer from 1 to one less than the event count. Both `n` and `end` are required.
`end` is `clean` or `abort`.

This simulates a server that stops early or a connection that drops before all events arrive. The
[reported incomplete chunked read](https://github.com/openai/openai-python/issues/2065) shows a Chat stream
failing partway through the response. `abort` closes before the final HTTP chunk; `clean` ends HTTP normally.

```bash
wiretruth derive --from openai-chat/truncation.text-stream-usage --transform cut_stream_after \
  --out openai-chat/stream.missing-finish-abort --args '{"n":8,"end":"abort"}'
```

## `strip_finish`

Remove the first finish event and everything after it in the last exchange. End the response cleanly. Arguments
are `{}`. The transform fails if there is no finish event or if removing it would leave no events.

For Chat Completions, the finish event is the first chunk with a non-null `finish_reason` on any choice. Removing
its tail also removes the usage-only chunk and `[DONE]`. For Responses, it is the first `response.completed`,
`response.incomplete` or `response.failed` event.

This simulates a clean HTTP ending without the provider's completion signal. A
[missing-finish report](https://github.com/pydantic/pydantic-ai/issues/6610) describes a library accepting a partial
Chat response as complete when both `finish_reason` and `[DONE]` are absent.

```bash
wiretruth derive --from openai-chat/truncation.text-stream-usage --transform strip_finish \
  --out openai-chat/stream.missing-finish
```

## `inject_error_event`

Keep the first `after` events of the last exchange, append one error event, and drop the remaining events. End the
response cleanly. Both arguments are required: `after` is an integer from 1 to one less than the event count;
`error` is an object containing the event's complete JSON data in the provider's format.

Chat Completions uses `data: <json>` followed by a blank line. Responses also puts `event: <type>` before the data
line, taking the type from `error.type`. Both helpers require SSE for injection. The output is rejected unless
the provider's extractor reads an error from it.

This simulates an error inside an HTTP 200 stream. The
[Responses error handling change](https://github.com/pydantic/pydantic-ai/pull/9321) covers `error` and
`response.failed` events that can otherwise be lost after the stream starts.

```bash
wiretruth derive --from openai-responses/truncation.text-stream --transform inject_error_event \
  --out openai-responses/stream.error-midstream \
  --args '{"after":8,"error":{"type":"error","code":"server_error","message":"The server had an error while processing your request. Sorry about that!","param":null,"sequence_number":8}}'
```

The Chat fixture uses the same event count with a nested `error` object and an explicit evidence URL to
[the Chat stream error handler](https://github.com/openai/openai-python/blob/v3.29.0/src/openai/_streaming.py#L94-L106).

## `rechunk_inside_utf8`

Turn the last exchange's `sse`, `ndjson` or `text` body into `chunks_b64`, preserving its bytes, headers and ending.
NDJSON lines keep the newline that replay appends. Supply exactly one argument:

- `every`: an integer of at least 1, the chunk size in bytes; the last chunk can be shorter.
- `points`: a list of strictly increasing byte offsets, each greater than zero and smaller than the body length.

At least one boundary must fall inside a multi-byte UTF-8 sequence. A boundary between characters is not enough.
Output is limited to **100 chunks**. Replay pauses 5 ms between chunks by default, so this adds less than half a
second of pauses and leaves room within the runner's 20 s cell timeout. Smaller chunks on large bodies could
spend seconds in replay pauses alone. Use fewer explicit points or a larger `every` when the limit is reached.

This simulates network chunks that split a character. A
[multi-byte text report](https://github.com/langchain-ai/langchainjs/issues/5285) describes replacement characters
appearing in streamed text when chunk boundaries split characters. In the committed source below, offset 1596
falls inside a multi-byte sequence. This illustrates the transform with an existing tool-call recording; it is
not the fixture for `stream.utf8-split`, which awaits a recording of the `stream.utf8` text call.

```bash
wiretruth derive --from openai-chat/tools.parallel-calls-stream --transform rechunk_inside_utf8 \
  --out openai-chat/stream.utf8-split --args '{"points":[1596]}'
```

## `replace_finish_value`

Replace the raw finish value in the last exchange with the required non-empty string `value`. For Chat
Completions, replace every non-null `finish_reason` on a choice, in the finish chunks or the JSON body. For
Responses, replace `incomplete_details.reason` in the final stream response or top-level JSON response.
A completed Responses response without a reason cannot be changed this way.

The body must be `sse` or `json` for these providers. The transform fails if no finish value changes, or if the
recomputed `truth.raw_finish` does not equal `value`.

This simulates a finish value a library has not mapped. The
[provider mapping report](https://github.com/BerriAI/litellm/issues/21347) describes gaps between published provider
finish values and library mappings. The constructed value tests whether a library retains an unfamiliar value.

```bash
wiretruth derive --from openai-responses/truncation.text --transform replace_finish_value \
  --out openai-responses/finish.unknown-value --args '{"value":"wiretruth_unknown_reason"}'
```

## `prepend_http_error`

Insert an exchange before the source's exchanges, using the first exchange's method and path. All three arguments
are required: `status` is an integer from 400 to 599; `headers` is an object allowing only lowercase `content-type`
and `retry-after` with string values; `body` is any JSON value. The new response uses a `json` body and ends
cleanly. Every original exchange follows unchanged.

This simulates a retryable failure before the original response. The
[rate-limit guide](https://developers.openai.com/api/docs/guides/rate-limits) describes HTTP 429 and retrying with
backoff. Replay serves exchanges in order, so the first retry receives the source's first response. Truth still
comes from the final exchange; it does not grade the initial error as the final outcome. The following example
is illustrative; no `errors.rate-limit` scenario or fixture is committed.

```bash
wiretruth derive --from openai-chat/request.max-tokens-field --transform prepend_http_error \
  --out openai-chat/errors.rate-limit \
  --args '{"status":429,"headers":{"content-type":"application/json","retry-after":"1"},"body":{"error":{"message":"Try again."}}}'
```
