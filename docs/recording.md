# Recording fixtures

This page is for contributors who record provider responses into fixtures. It explains what the recorder sends and
stores, how to record in CI or on your own machine, and the checks that keep credentials out of the repository.
Running the suite needs no keys. Only recording does.

## What the recorder does

`wiretruth record` takes a scenario's provider-neutral call, sends it to the real API and saves the response as a
fixture in `suite/fixtures/<provider>/<scenario>.json`.

- **No SDK.** The provider module builds the native request, and Python's `http.client` sends it. No provider
  library sits between the API and the fixture, so a library bug can't leak into the recorded truth.
- **The key stays in the request.** It is read from the provider's environment variable (`OPENAI_API_KEY` for
  `openai-chat`) and sent only in a header. Request headers are never stored. A key with spaces or other characters
  a header can't carry is rejected without being printed. If the key itself shows up in a response, nothing is
  written.
- **One call, the scenario's call.** The request is built from the scenario's `call` (with its `call_overrides` for
  the provider) and the provider's current model, or `--model`. The request doesn't set a temperature or a seed:
  the call format has no such fields, and the libraries under test don't send them either.

## What a fixture stores

| Part | What is kept |
|---|---|
| Status | The HTTP status |
| Headers | Only `content-type` and `retry-after`. Everything else is dropped: request ids, rate-limit counters, organization and project headers, cookies |
| Body | An event stream split into events, each with its blank line. A JSON body parsed and written back in the same key order. Any other body as text |
| Body, as read | `--keep-bytes` stores the chunks exactly as they were read, base64-encoded. The recorder does the same when the body isn't UTF-8, or when an event stream doesn't split into events that each end with one blank line |
| End | `abort` when the connection closed before the body was complete, otherwise `clean` |
| Provenance | `recorded_at` (UTC), `requested_model`, `returned_model` (from the body), `recorder` (the wiretruth version), `source_commit`, `ci_run` (in CI), `raw_sha256` (of the body bytes as received), and `request` (the body that was sent) |
| Truth | Computed from the response by the provider module, right away |

A JSON body is replayed with the same content but not the same bytes, so `raw_sha256` keeps a record of the
original. [File formats](formats.md#fixture--wiretruthfixturev1) lists every field.

A new recording always becomes `<scenario>.json`, the fixture the cell uses. If one already exists, it moves to
`<scenario>@<YYYY-MM-DD>.json`, dated by its own `recorded_at`, with its `id` changed to match. It stays as a dated
variant, which no cell uses ([ADR 0005](adr/0005-provenance-tiers.md)), and the pull request shows both side by
side. `--replace` overwrites the existing fixture instead.

The recorder checks this before it sends anything. It stops if the existing fixture has no `recorded_at` (a
documented or derived fixture), or if its dated name is already taken. Pass `--replace`, or move the file by hand.
When fixtures were derived from the one that moves, the recorder names them: derive them again, or point their
`derived_from` at the dated id.

## Record in CI

Recording in CI is the default for anything that lands on `main`: the fixture links to the run, and the run's log
is public.

1. A maintainer stores the key once: `gh secret set OPENAI_API_KEY` (it prompts for the value).
2. Run the **record** workflow from the Actions tab, or from the command line:

   ```bash
   gh workflow run record.yml -f provider=openai-chat -f scenarios='truncation.*'
   ```

   `model` is optional. Leave it empty to use the provider's current model. `replace` (default `false`) passes
   `--replace`.
3. The workflow records, runs `wiretruth lint`, and pushes a branch named `record/<provider>-<run id>` with one
   commit by `github-actions[bot]`. Its summary lists each fixture with its status, finish and token counts. If
   some scenarios fail, the run fails, but the fixtures that were written still pass through lint and get pushed.
4. Read each fixture's truth against its exchanges. Then open the pull request from the branch:

   ```bash
   gh pr create --head record/openai-chat-<run id> --fill
   ```

   The workflow doesn't open it for you, because a pull request opened with the workflow's own token wouldn't run
   CI.

Only people with write access can start the workflow. It runs two jobs, so no step holds both credentials. The
`record` job gets the key, in its recording step only, and a read-only repository token. The `push` job gets a token
that can push, and only the fixtures the first job handed over, after lint passed.

## Record locally

Export the key in your own shell, then run the recorder from the repository root:

```bash
export OPENAI_API_KEY=...   # in your shell only; never in a file or a commit
uv run wiretruth record --provider openai-chat --scenario truncation.text
```

A local recording has no `ci_run`. Prefer CI for fixtures you plan to merge.

To see what would be sent without sending it, add `--dry-run`. It needs no key and prints the request exactly as
`http.client` sends it, with the key masked:

```text
$ uv run wiretruth record --provider openai-chat --scenario truncation.text --dry-run
# truncation.text
POST https://api.openai.com/v1/chat/completions
Host: api.openai.com
Authorization: Bearer ***
Content-Type: application/json
Accept: application/json
User-Agent: wiretruth/0.1.0.dev0
Accept-Encoding: identity
Content-Length: 229

{
  "model": "gpt-6-luna",
  "messages": [
    {
      "role": "user",
      "content": "Write a 200-word paragraph about the history of Kyiv, in English."
    }
  ],
  "max_completion_tokens": 16,
  "reasoning_effort": "none"
}
```

```text
wiretruth record --provider PROVIDER --scenario GLOB [--model MODEL] [--dry-run] [--replace] [--keep-bytes]
```

| Option | Meaning |
|---|---|
| `--provider` | The wire API. Today: `openai-chat` |
| `--scenario` | A scenario id, or a glob such as `'truncation.*'`. Scenarios that don't list the provider are skipped, with a note |
| `--model` | The model to request. Default: the provider's current model (`gpt-6-luna` for `openai-chat`) |
| `--dry-run` | Print each request and send nothing |
| `--replace` | Overwrite `<scenario>.json` instead of keeping it as a dated variant |
| `--keep-bytes` | Store the body as the chunks read |

| Exit code | Meaning |
|---|---|
| 0 | Every fixture was written (or, with `--dry-run`, printed) |
| 1 | A scenario file is invalid, or a fixture the recorder built is (a recorder bug) |
| 2 | No key, no matching scenario, a fixture that can't move to its dated name, a network failure, a secret found in a response, or a response the truth extractor can't read |

Everything that can fail before a request is checked first, for every matching scenario: the scenario files, the
requests, the key and where each fixture goes. After that, a scenario that fails doesn't stop the others. The
recorder prints why on stderr, keeps every fixture that was written, and exits with the code above at the end.

## The secret scan

The recorder scans each fixture, and the raw response body, before it writes anything. `wiretruth lint` runs the
same scan on every file under `suite/` and `results/`, JSON or not, including the decoded bytes of `chunks_b64`
bodies, and CI runs lint on every pull request.

It reports:

- `sk-` followed by 16 or more letters, digits, `_` or `-`; the prefixes `sk-proj-` and `sk-ant-`;
- `AIza` followed by 35 key characters;
- `Bearer ` followed by a token of 16 or more characters;
- organization and project ids: `org-` or `proj_` followed by 16 or more letters or digits;
- a header named `authorization`, `proxy-authorization`, `x-api-key`, `x-goog-api-key`, `api-key`,
  `x-auth-token`, `cookie`, `set-cookie`, `openai-organization`, `openai-project` or `x-goog-user-project`, in any
  `headers` object. The replay server leaves the same headers out of its request log.

The prefixes count only at the start of a word, so text such as `desk-based-research-notes` passes. A JSON escape
such as `\n` or `\u00a0` also starts a word, so `"Key:\nsk-…"` is caught. A finding
names the pattern and the line, never the matched text, so the scan's own output can't leak a key. If it reports
a real key, remove the file and revoke the key.

## How the truth is read

The provider module computes the truth from the last exchange, which is the response the caller ends up with.
`wiretruth lint` computes it again for every fixture and reports each field that differs from the stored truth.
For `openai-chat`:

| Truth | Read from |
|---|---|
| `finish` | `error` for an error response or an error event in the stream. `refusal` when the message has a `refusal`. Otherwise `finish_reason`: `stop` → `stop`, `length` → `length`, `tool_calls` and `function_call` → `tool_calls`, `content_filter` → `content_filter`, any other value → `other`, none sent → `none` |
| `raw_finish` | `finish_reason` as sent |
| `text` | `message.content`, or the streamed `delta.content` pieces joined. `null` when no content was sent |
| `tool_calls` | `message.tool_calls`, and the legacy `function_call`. Streamed calls are put together by `index`, with their `arguments` pieces joined |
| `structured` | The text parsed as JSON, only when the request asked for JSON output (`response_format`) and the finish is `stop` |
| `stream_completed` | `true` when `data: [DONE]` arrived and the response ended cleanly. `null` for a response that wasn't streamed |
| `usage` | `prompt_tokens` → `input`, `prompt_tokens_details.cached_tokens` → `cached_input`, `completion_tokens` → `output`, `completion_tokens_details.reasoning_tokens` → `reasoning`, `total_tokens` (or input + output) → `total`. In a stream, usage comes only with `stream_options.include_usage` |
| `error` | The `error` object's `message`, `type` and `code`. `status` is the HTTP status of an error response, and `null` for an error event inside a stream |

Only the choice with `index` 0 counts. Stream events follow the HTML standard's event-stream rules. A response
that isn't streamed and is cut off before its body parses delivered nothing, so its finish is `none`.
