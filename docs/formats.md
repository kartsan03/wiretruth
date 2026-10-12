# File formats

This page is for people who read or write wiretruth's data: maintainers who vendor scenarios and fixtures, shim
authors, and contributors who add scenarios. It lists every field of every format. The JSON Schemas in
[`src/wiretruth/schemas/`](../src/wiretruth/schemas/) are the source of truth; this page describes them.
[Concepts](concepts.md) explains what each format is for.

## Common rules

### Versions

Every document has a `format` field, such as `"wiretruth.fixture/v1"`. The bug-class registry and the upstream
tracking file are plain lists, so they have a schema but no `format` field.

- Adding an optional field keeps `/v1`.
- Removing or renaming a field, making a field required, or changing what a field means needs `/v2`. For one minor
  release, wiretruth then reads both versions, and the changelog explains how to migrate.

### Strictness

Every object these formats define rejects unknown fields (`additionalProperties: false`), so a misspelled field
fails validation instead of being ignored. Free-form JSON is not checked: output schemas, tool parameters, `extra`,
recorded bodies, the recorded request, raw usage and check arguments.

Integer fields take integers only: `200.0` is rejected even though JSON Schema would count it as an integer.

### Reading JSON

wiretruth reads JSON strictly. Each of these is an error:

- a file that isn't valid UTF-8;
- the same key twice in one object;
- `NaN`, `Infinity` or `-Infinity`, and numbers too large to represent, such as `1e400`;
- a `\u` escape that is half of a surrogate pair;
- nesting too deep to parse.

Different JSON parsers handle these in different ways, so a file that contains them could mean different things in
different languages.

### Where files live

| Path | Format |
|---|---|
| `suite/bug-classes.json` | [bug classes](#bug-classes) |
| `suite/scenarios/<family>/<name>.json` | [scenario](#scenario--wiretruthscenariov1) with id `<family>.<name>` |
| `suite/fixtures/<provider>/<scenario>.json` | [fixture](#fixture--wiretruthfixturev1) with id `<provider>/<scenario>` |
| `suite/fixtures/<provider>/<scenario>@<date>.json` | an older recording of the same fixture, such as `@2026-11-02`, kept for reference, which no cell uses by default ([ADR 0005](adr/0005-provenance-tiers.md)). A streamed, aborted or otherwise different form of a call is its own scenario, such as `tools.parallel-calls-stream` |
| `results/<library>/<version>.json` | [results](#results--wiretruthresultsv1) |
| `results/upstream.json` | [upstream tracking](#upstream-tracking) |
| `shims/…/wiretruth-shim.json` | [shim manifest](#shim-manifest--wiretruthshimv1) (not checked by `wiretruth lint` yet) |

Call specs and observations are messages between the runner and a shim, not files in the repository.

### `wiretruth lint`

```text
wiretruth lint [PATH]
```

Lint validates every `.json` file under `PATH/suite/` and `PATH/results/` (`PATH` defaults to the current
directory). Then it checks the files against each other:

- each file's path matches its id;
- each scenario's `bug_class` is in the registry;
- each fixture's scenario exists and lists the fixture's provider;
- each derived fixture's `derived_from.fixture` exists;
- each results file's path matches its `library.name` and `library.version`;
- no other JSON file sits under `suite/` or `results/`;
- each fixture's `truth` matches the truth its provider module computes from its exchanges (today for
  `openai-chat`, `openai-responses`, `ollama` and `ollama-openai`; lint notes the providers it can't check yet);
- no file under `suite/` or `results/`, JSON or not, contains anything that looks like a key, a token, an
  organization or project id, or a credential header ([the secret scan](recording.md#the-secret-scan)).

It prints one line per problem, with every problem in a file, as `<file>: <JSON path>: <message>`:

```text
suite/scenarios/truncation/text.json: $.requirements[0].level: 'MUSTT' is not one of ['MUST', 'SHOULD']
```

| Exit code | Meaning |
|---|---|
| 0 | Everything is valid. A missing `results/` directory is fine; lint notes it on stderr. |
| 1 | At least one file has a problem. |
| 2 | Lint can't run, for example because `suite/` is missing. |

A truth difference is reported per field, such as `$.truth.usage.output`, with the value the exchanges give.
[Recording](recording.md#how-the-truth-is-read) explains how each provider's truth is read.

### Shared vocabulary

| Name | Values |
|---|---|
| Provider id | `openai-chat`, `openai-responses`, `anthropic`, `gemini`, `gemini-interactions`, `ollama`, `ollama-openai` |
| Scenario family | `truncation`, `finish`, `usage`, `stream`, `tools`, `request`, `errors` |
| Finish value | `stop`, `length`, `tool_calls`, `content_filter`, `refusal`, `error`, `other`, `none` |
| Error category | `rate_limit`, `context_length`, `truncation`, `content_filter`, `parse`, `validation`, `api`, `network`, `internal`, `other` |
| Surface | `primary`, `raw`, `absent` |
| Fixture tier | `recorded`, `documented`, `derived` |
| Language | `python`, `javascript` |

Timestamps are UTC in RFC 3339 form, such as `2026-10-10T10:12:03Z`. Dates are `YYYY-MM-DD`. Links are `https://`
URLs. [Concepts](concepts.md#terms) explains truth, surfaces and the canonical values.

## Scenario — `wiretruth.scenario/v1`

One edge case: the call to make, and the requirements a library must meet.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `format` | `"wiretruth.scenario/v1"` | yes | |
| `id` | string | yes | `<family>.<name>`: lowercase, with `-` inside the name |
| `title` | string | yes | One line, shown in the catalog and on the site |
| `bug_class` | string | yes | An id from `suite/bug-classes.json` |
| `priority` | `P0`, `P1` or `P2` | yes | |
| `summary` | string | yes | What happens on the wire, in a sentence or two |
| `call` | object | yes | The provider-neutral call: the [call fields](#call-fields). `stream` and `messages` are required |
| `providers` | list of provider ids | yes | Where a fixture is expected. A provider without a fixture shows as "not covered" |
| `requirements` | list of [requirements](#requirement) | yes | At least one |
| `call_overrides` | object | no | Provider id → call fields that replace the neutral ones for that provider. Each provider must be in `providers` |
| `grading_notes` | string | when `call_overrides` is set | How edge cases grade, and why each override exists |
| `references` | list of URLs | no | Issues and docs that show the bug class in the wild |

### Requirement

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Lowercase with `-`; unique within the scenario |
| `level` | `MUST` or `SHOULD` | yes | A MUST decides the grade. An unmet SHOULD adds a note |
| `check` | string | yes | The name of a check, such as `truncation_is_signalled` |
| `args` | object | no | Arguments for the check, which validates them |
| `text` | string | yes | The requirement in plain words |
| `providers` | list of provider ids | no | Limits the requirement to these providers, each of which must be in the scenario's `providers` |

## Call spec — `wiretruth.call/v1`

What the runner writes to a shim's stdin: the scenario's call, with that provider's overrides applied, plus the
cell it belongs to. Every field is present; fields the scenario leaves out are `null` or empty.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"wiretruth.call/v1"` | |
| `scenario` | scenario id | |
| `provider` | provider id | The same as `WIRETRUTH_PROVIDER` |
| `model` | string | The model from the fixture's provenance |
| … | | The [call fields](#call-fields) |

### Call fields

| Field | Type | Default in a scenario | Meaning |
|---|---|---|---|
| `stream` | boolean | (required) | Whether to stream |
| `messages` | list of `{role, content}` | (required) | `role` is `system`, `user` or `assistant`; `content` is a string |
| `max_output_tokens` | integer ≥ 1 or `null` | `null` | The output token limit |
| `stop` | list of strings or `null` | `null` | Stop sequences |
| `tools` | list of `{name, description, parameters}` | `[]` | `parameters` is a JSON Schema |
| `tool_choice` | `"auto"`, `"required"`, `"none"`, `{"name": …}` or `null` | `null` | `{"name": …}` forces one tool |
| `output_schema` | object or `null` | `null` | A JSON Schema for structured output |
| `reasoning` | `{"effort": …}` or `null` | `null` | `effort` is `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. `null` sends nothing, so the provider's default applies |
| `extra` | object | `{}` | Hints for one scenario, such as `{"include_usage": true}` |

## Fixture — `wiretruth.fixture/v1`

The HTTP exchanges for one scenario and provider, where they came from, and the truth computed from them.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"wiretruth.fixture/v1"` | |
| `id` | string | `<provider>/<scenario>`, optionally followed by `@<date>` |
| `scenario` | scenario id | |
| `provider` | provider id | |
| `tier` | fixture tier | Where the bytes came from ([ADR 0005](adr/0005-provenance-tiers.md)) |
| `provenance` | [provenance](#provenance) | |
| `derived_from` | [derived from](#derived-from) or `null` | Required for `derived`; `null` for the other tiers |
| `exchanges` | list of [exchanges](#exchange) | The responses, in the order the library is expected to request them |
| `truth` | [truth](#truth) | What happened on the wire |

All fields are required.

### Provenance

`requested_model` is required for every tier, because the call spec takes its model from here. A derived fixture
copies `requested_model`, `returned_model` and `request` from its source, when present. It copies no other
provenance fields. The other fields are optional, except where the tier requires them.

| Field | Type | Required for | Meaning |
|---|---|---|---|
| `recorded_at` | timestamp | `recorded` | When the response was recorded |
| `requested_model` | string | every tier | The model in the request |
| `returned_model` | string or `null` | `recorded` | The model the provider reported, or `null` if it reported none |
| `recorder` | string | `recorded` | The recorder version, such as `wiretruth 0.1.0` |
| `source_commit` | git SHA or `null` | | The commit the recorder ran from |
| `ci_run` | URL or `null` | | The workflow run, when it was recorded in CI |
| `raw_sha256` | 64 hex characters | `recorded` | SHA-256 of the raw response body |
| `request` | object | | The native request body that was sent, without credentials |
| `source_url` | URL | `documented` | The official docs page the response was transcribed from |
| `notes` | string | `documented` (non-empty) | What was transcribed and what was filled in |

### Derived from

| Field | Type | Meaning |
|---|---|---|
| `fixture` | fixture id | The fixture this one was derived from, never its own id |
| `transform` | string | The transform's name, such as `strip_finish` |
| `args` | object | The transform's arguments |
| `rationale` | string | What the derived shape simulates |
| `evidence_url` | URL | A provider docs page or an issue that shows the shape occurs in practice |

All fields are required.

### Exchange

| Field | Type | Meaning |
|---|---|---|
| `match.method` | string | An upper-case HTTP method |
| `match.path` | string | The request path. `*` matches any run of characters, as in `/v1beta/models/*:generateContent` |
| `response.status` | integer 100–599 | |
| `response.headers` | object | Only `content-type` and `retry-after`, in lowercase |
| `response.body` | object | Exactly one of the body forms below |
| `response.end` | `clean` or `abort` | Optional, default `clean`. `abort` means the connection closes without the final chunk |

| Body form | Value |
|---|---|
| `{"json": …}` | any JSON value |
| `{"sse": […]}` | one string per event, each ending with its blank line (`\n\n` or `\r\n\r\n`) |
| `{"ndjson": […]}` | one string per line, without the newline |
| `{"chunks_b64": […]}` | one base64 string per chunk, for bodies whose exact chunk boundaries matter |
| `{"text": …}` | a string |

The replay server sends `json` and `text` bodies whole, with `Content-Length`. It sends the other forms with
chunked transfer encoding: one chunk per event, per line (with its newline) or per stored chunk, each flushed on its
own, with a short gap between chunks. So `abort` needs `sse`, `ndjson` or `chunks_b64`, and a `chunks_b64` chunk
can't be empty, because an empty chunk ends a chunked body.

Exchanges are answered in order. A request that matches the next exchange's method and path gets it. Once every
exchange has been used, the last one that matches repeats, so retries stay visible. Any other request gets a 404
that names the method and path.

### Truth

| Field | Type | Meaning |
|---|---|---|
| `finish` | finish value | The canonical finish |
| `raw_finish` | string or `null` | The provider's own value, such as `MAX_TOKENS` |
| `text` | string or `null` | The full generated text |
| `tool_calls` | list of [tool calls](#tool-call) | |
| `structured` | any JSON value | The parsed structured output, or `null` |
| `error` | `{message, status?, type?, code?}` or `null` | An error the provider signalled |
| `stream_completed` | boolean or `null` | Whether the stream ended normally; `null` for a response that wasn't streamed |
| `usage` | [usage](#usage) | |

All fields are required.

### Usage

| Field | Type | Meaning |
|---|---|---|
| `input` | integer or `null` | All input tokens, including cached ones |
| `cached_input` | integer or `null` | Input tokens served from a cache |
| `output` | integer or `null` | All billed output tokens, including reasoning |
| `reasoning` | integer or `null` | Reasoning tokens, when the provider reports them |
| `total` | integer or `null` | The provider's total, or input + output |
| `alternatives` | object | Other documented readings of a field, by name: `{"input": {"uncached_input": 12}}` |
| `raw` | object or `null` | The provider's usage object as sent, or `null` when it sent none |

`null` means the provider didn't report the number. All fields are required.

### Tool call

The same shape appears in the truth and in observations.

| Field | Type | Meaning |
|---|---|---|
| `id` | string or `null` | |
| `name` | string | |
| `arguments_raw` | string or `null` | The arguments as text, as sent or as the library exposes them |
| `arguments_parsed_ok` | boolean | Whether `arguments_raw` parses as JSON |
| `arguments` | any JSON value | The parsed arguments, or `null` |

## Observation — `wiretruth.observation/v1`

What a library reported. A shim prints exactly one observation to stdout. Every field is present.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"wiretruth.observation/v1"` | |
| `library` | `{name, version, language, api}` | `api` is the entry point the shim called, such as `litellm.completion` |
| `outcome` | `returned`, `raised` or `unsupported` | |
| `unsupported_reason` | string or `null` | Required when `outcome` is `unsupported`, with a docs link; `null` otherwise |
| `error` | [error](#observed-error) or `null` | Required when `outcome` is `raised` |
| `finish` | `{value, raw, surface, source}` or `null` | `value` is a finish value, or `null` when `surface` is `absent`. `source` is the attribute path that was read |
| `text` | `{value, surface}` or `null` | |
| `structured` | `{attempted, returned_as_success, value}` | `returned_as_success`: the library handed back a parsed object with no error |
| `tool_calls` | list of [tool calls](#tool-call) | |
| `usage` | `{input, cached_input, output, reasoning, total, surface}` or `null` | `surface` gives a surface for each of the five counts |
| `stream` | `{used, completed, chunks_seen}` | `completed` is the library's own signal that the stream ended normally, or `null` when it gives none |
| `notes` | list of strings | Anything the grader should know, such as a setting the library couldn't send |
| `elapsed_ms` | number ≥ 0 | |

### Observed error

| Field | Type | Meaning |
|---|---|---|
| `type` | string | The fully qualified error type |
| `message` | string, at most 500 characters | |
| `category` | error category | From the manifest's `error_map` |
| `retry_after` | number or `null` | Seconds, when the library exposes it |
| `stack_top` | string or `null` | `file:line` of the raising frame, relative to the library |

## Shim manifest — `wiretruth.shim/v1`

`wiretruth-shim.json`, next to the shim. It says how to run the shim and how to read its library.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `format` | `"wiretruth.shim/v1"` | yes | |
| `name` | string | yes | Lowercase with `-`, such as `vercel-ai` |
| `language` | language | yes | |
| `packages` | object | yes | Package → exact version. Ranges such as `^7.0.0` are rejected |
| `primary_package` | string | yes | The package under test. It must be in `packages` |
| `entry` | list of strings | yes | The command to run, in the shim's directory |
| `env` | object | no | Extra environment variables. Names that start with `WIRETRUTH_` are reserved for the runner |
| `providers` | object | yes | Provider id → how the shim builds a client for it |
| `surfaces` | object | yes | `finish`, `text`, `usage`, `tool_calls`, `structured` or `stream` → `{primary?, raw?}`: where the library exposes it |
| `finish_map` | object | yes | Library finish value → finish value |
| `error_map` | list | yes | `{type, category, note?}`, plus `message_regex` and `source` for a builtin error the library raises on purpose. `source` links to the raising line. The regex syntax is checked for Python shims only |
| `unsupported` | list | yes | `{scenario, providers?, reason, docs}`: what the library can't express, with the docs that say so |
| `config_notes` | list of strings | yes | Every change from the library's default configuration, and why ([ADR 0006](adr/0006-default-library-config.md)) |
| `docs` | list of URLs | yes | |

## Results — `wiretruth.results/v1`

Every graded cell for one library version.

| Field | Type | Meaning |
|---|---|---|
| `format` | `"wiretruth.results/v1"` | |
| `suite_version` | string | The wiretruth version that ran the suite |
| `library` | `{name, version, language}` | |
| `run` | `{at, git_sha, ci_run, runner, packages}` | When and where the run happened; `ci_run` is a URL or `null`; `packages` maps every package installed in the shim's environment to its version |
| `cells` | list of [cells](#cell) | |

### Cell

| Field | Type | Meaning |
|---|---|---|
| `scenario` | scenario id | |
| `provider` | provider id | |
| `fixture` | fixture id | |
| `fixture_tier` | fixture tier | |
| `grade` | `PASS`, `LOSSY`, `WRONG`, `CRASH`, `HANG`, `UNSUPPORTED` or `HARNESS` | |
| `requirements` | list of `{id, level, status, evidence}` | `status` is `SATISFIED`, `CONTRADICTED` or `NOT_OBSERVABLE` |
| `notes` | list of strings | Why a rule from 1 to 5 applied, and one note per requirement that isn't `SATISFIED` ([Grading](grading.md#evidence-and-result-cells)) |
| `observation` | [observation](#observation--wiretruthobservationv1) or `null` | `null` when the shim produced none |
| `request_count` | integer | Requests the replay server received |

Lint doesn't check cells against the current suite, so results stay valid after a scenario changes.

## Upstream tracking

`results/upstream.json`: a list of findings and the upstream issues that track them. When the root cause is in a
dependency of the library or in the provider, `issue` links that project's existing issue and `notes` says so.

| Field | Type | Meaning |
|---|---|---|
| `library` | string | The shim name |
| `scenarios` | list of scenario ids | |
| `providers` | list of provider ids | |
| `issue` | URL or `null` | |
| `pr` | URL or `null` | |
| `status` | `reported`, `confirmed`, `fix-merged`, `released`, `wontfix`, `disputed` or `documented-behavior` | |
| `fixed_in` | string or `null` | The first library version with the fix |
| `opened` | date | |
| `notes` | string | |

All fields are required.

## Bug classes

`suite/bug-classes.json`: a list of the bug classes that scenarios refer to. Ids are unique.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Lowercase with `-`, such as `silent-truncation` |
| `title` | string | |
| `description` | string | What goes wrong in the library |
| `effect` | string | What goes wrong for the people who use it |
| `references` | list of URLs | Public issues and fixes that show the class |
