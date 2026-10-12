# Grading

This page is for maintainers checking a grade or adding a requirement. It explains the checks, their evidence,
and the order that turns requirement statuses into a cell grade. Start with [Concepts](concepts.md) for the terms
and grade meanings; [decision 0004](adr/0004-grades-and-surfaces.md) explains the choices behind them.

## Checks and surfaces

A check is a pure function of an observation, fixture truth, the logged requests and scenario arguments.
It returns a status and a short evidence string. It does not run a shim or read a file.

| Status | Meaning |
|---|---|
| SATISFIED | The observation meets the requirement. |
| CONTRADICTED | The observation contradicts the requirement. |
| NOT_OBSERVABLE | The observation neither meets nor contradicts it. |

Only **primary** values count when comparing finish, text or usage. A value on the **raw** or **absent** surface
cannot satisfy or contradict a comparison, except that absent text or usage can satisfy the reports-none
rule when the wire carried no value, described below. `raw_finish_matches_truth` compares the raw finish
value on any surface.
A separate signal, such as a raised error, can satisfy a check without a primary value.

Within a check, SATISFIED conditions take precedence over CONTRADICTED conditions. If neither applies, the status
is NOT_OBSERVABLE. Applicability comes first: for example, structured output that was not attempted cannot be
judged as a successful or unsuccessful structured result.

A documented finish field on an error belongs in `obs.finish`, with `surface: "primary"` and `source` naming
that error field. Both `finish_is` and `truncation_is_signalled` read it even when `outcome` is `raised`.
The manifest-declared error path has already been resolved by the shim; the check needs no manifest lookup.

## Check reference

"Reported" below means primary for finish, text and usage. "Absent" includes a null value unless stated otherwise.
A dash means the check cannot return that status. Each check rejects missing, unknown or wrongly typed arguments
with `ValueError`. That is a scenario bug, not a library grade.

| Check | SATISFIED | CONTRADICTED | NOT_OBSERVABLE |
|---|---|---|---|
| `finish_is(value)` | Reported finish equals `value`, including on an error. | A different finish is reported. | Finish is raw-only, absent or null. |
| `finish_is_not(value)` | The outcome is raised, or the reported finish is anything but `value`, including raw-only, absent or null. | The forbidden finish is reported without a raised outcome. | — |
| `raw_finish_matches_truth` | Raw finish equals truth on any surface, or null truth is matched by reporting none. | A non-null raw value differs from truth. | Finish is absent or raw is null when truth is non-null. |
| `truncation_is_signalled` | Reported finish is `length`, or error category is `truncation`. | Without either satisfying signal, reported finish is `stop` or `tool_calls`, or structured output came back as success. | Neither condition applies, including raw-only truncation or a generic parse/validation error without a primary finish. |
| `text_matches_truth` | Reported text exactly equals non-empty truth, or null/empty truth is matched by reporting no text. | Reported text differs from non-empty truth, or non-empty text is reported for null/empty truth. | Text is raw-only, or absent/null when truth is non-empty. |
| `no_replacement_chars` | Reported non-null text contains no U+FFFD. | It contains U+FFFD. | Text is raw-only, absent or null. |
| `no_success_structured_value` | Structured output was attempted and did not return as success. | It was attempted and returned as success. | Structured output was not attempted. |
| `tool_calls_match_truth` | Same count, ordered ids, names and parsed arguments; two empty lists also match. A truth id of null accepts any id. | A known count, id, name or parsed-argument mismatch, including no calls on a returned outcome without an error. | No calls on a raised outcome or next to an error object, or arguments that cannot be compared after matching count, ids and names. |
| `no_complete_tool_call_from_partial_args` | A raised outcome, an error object or a primary `error` finish; otherwise no call reports parsed arguments against partial truth. Unparsed partial calls satisfy. | A call reports `arguments_parsed_ok: true` while its matched truth call has `arguments_parsed_ok: false`, without an error signal. | — |
| `usage_matches_truth(fields)` | Every field matches truth or an alternative, including the null-truth rule. | Any reported field differs from every accepted value. | No contradiction, but at least one field is unavailable on primary; see null truth below. |
| `usage_total_reconciles` | Reported total matches an accepted total, or primary components reconstruct one when no primary total is reported. Null truth uses the reports-none rule. | A reported total differs, or available primary components cannot reconstruct any accepted total. | Total cannot be compared and primary input/output are unavailable; raw-only null-truth total also stays unobservable. |
| `outcome_is_error(categories?)` | Outcome is raised or returned with an error object, with a listed category if a list was supplied. | Outcome is returned with no error signal. | Error category is unlisted, returned with only a primary error finish, or unsupported. |
| `outcome_is_returned` | Outcome is returned with no error object. | Outcome is raised, or returned with an error object, while the wire carried no error. | Unsupported, or the wire carried an error. |
| `stream_not_reported_complete` | Completion is false, or outcome is raised. | Completion is true without a raised outcome. | No completion signal and no raised outcome. |
| `request_field_equals(path, value)` | Every request has the path with the expected JSON value. | Any JSON request lacks the path or has a different value there. | No requests, or a non-JSON request with no contradiction elsewhere. |
| `request_field_absent(path)` | Path absent from every request, including no requests. | Path present in any request, even with a null value. | — |
| `request_count_at_most(n)` | Number of requests is at most `n`. | Number of requests exceeds `n`. | — |

`finish_is` and `finish_is_not` require a canonical finish: `stop`, `length`, `tool_calls`, `content_filter`,
`refusal`, `error`, `other` or `none`. The string `none` is a canonical value, distinct from null.
`usage_matches_truth` requires a non-empty list drawn from `input`, `cached_input`, `output`, `reasoning`, `total`.
`outcome_is_error` accepts an optional list drawn from `rate_limit`, `context_length`, `truncation`,
`content_filter`, `parse`, `validation`, `api`, `network`, `internal`, `other`. Both lists must be non-empty.
`n` is an integer at least zero; a boolean is not an integer argument. All other checks take no
arguments.

For `outcome_is_error`, an error signal is an error object or a primary finish of `error`. Either signal on a
returned outcome prevents silent success. A returned or yielded error object satisfies when no categories were
requested or its category is listed; an unlisted category is NOT_OBSERVABLE. A primary finish of `error` without
an error object on a returned outcome remains NOT_OBSERVABLE.

Request paths are non-empty dotted paths without empty segments. For example, `stream_options.include_usage`
looks up an object field, and `messages.0.content` indexes the first list item. ASCII digit-only segments index
lists, including at the root; they do not look up numeric object keys. A non-JSON body has no paths and is
NOT_OBSERVABLE for equality. A missing path in a JSON body contradicts equality, with `=absent` in the evidence.
Request checks inspect every logged request, including retries and unmatched requests. Equality compares JSON
values recursively: object key order is irrelevant, array order matters, and booleans differ from numbers.
The `value` argument can be any JSON value, including null.

Text comparison is exact, without Unicode normalization. When truth text is null or empty, an absent surface
or primary null/empty text satisfies: the library reported none. Primary non-empty text contradicts. Raw text
stays NOT_OBSERVABLE. For non-empty truth, absent/null text is NOT_OBSERVABLE and a different primary value,
including empty text, contradicts.

Tool-call comparison ignores raw argument formatting and uses parsed JSON values. Some providers send no
tool-call ids, so a null truth id accepts whatever id the library generated. A provider that does send ids
(OpenAI, and Ollama since v0.40) holds the library to them: a generated id contradicts. For partial-argument detection,
truth calls are matched by id first, then by position when no id matches. Returning unparsed partial arguments
is honest; only presenting partial truth arguments as parsed contradicts. Whether the cut is signalled belongs
to `truncation_is_signalled`, a separate MUST requirement in scenarios using this check.

A primary `length` finish or a truncation error satisfies `truncation_is_signalled` even if a structured value
also came back as success. `no_success_structured_value` judges that success separately. Similarly, a raised
outcome satisfies `finish_is_not` and `stream_not_reported_complete` before an equal finish or a true completion
signal can contradict them. `finish_is_not` only asks that the library not report the forbidden value, so a raw-only
or absent finish satisfies it.

## Usage and missing wire values

Accepted values for a usage field are its truth value and every value in `truth.usage.alternatives[field]`.
Alternative names describe the readings; checks compare their numeric values.

When a truth usage field is null, the wire carried no value for that field. A primary null or absent field is
SATISFIED: the library reported none. A primary number, including zero, is CONTRADICTED unless it is an explicitly
accepted alternative. A raw-only field remains NOT_OBSERVABLE. For example, the recorded
[`truncation.text-stream` fixture](../suite/fixtures/openai-chat/truncation.text-stream.json) has no wire usage.
Reporting no output count satisfies the check; inventing a primary output count contradicts it.

`usage_matches_truth` evaluates every requested field. Any CONTRADICTED field makes the check CONTRADICTED.
Otherwise any NOT_OBSERVABLE field makes it NOT_OBSERVABLE. Otherwise it is SATISFIED. Evidence describes each
field, its surface, its truth value and any alternatives.

`usage_total_reconciles` compares a present primary total directly; correct components do not rescue a wrong
total. Without a primary total, it tries primary `input + output`, with either, both or neither of primary
`cached_input` and `reasoning` added. This accommodates counts that expose cached or reasoning tokens separately.
Missing primary input or output is NOT_OBSERVABLE; a complete set that cannot reconstruct an accepted total is
CONTRADICTED. Raw fields never enter a sum, but primary components can satisfy the check despite a raw-only total.
When truth total is null, compare only whether total was reported using the reports-none rule; do not reconstruct
it from other fields.

## Grade rules

The first applicable rule wins. These are the rules from [Concepts](concepts.md), in the same order; the internal
error definition and the reasons for this order are in [decision 0004](adr/0004-grades-and-surfaces.md).

1. The shim's time limit ran out: HANG if the response had been served, otherwise HARNESS.
2. The shim's process ended without an observation: CRASH if a signal killed it (a segfault, for example) after the
   library's request reached the replay server, otherwise HARNESS.
3. The observation is invalid, the library called a different endpoint or stream mode than the fixture serves, or
   it returned or raised without a request reaching the replay server: HARNESS.
4. The shim reports, with a documentation link, that the library can't express the call: UNSUPPORTED.
5. The library raised an internal error: CRASH.
6. A MUST requirement is CONTRADICTED: WRONG.
7. A MUST requirement is NOT_OBSERVABLE: LOSSY.
8. Otherwise: PASS.

"Served" is the last logged request's `response_complete`, including a fixture's deliberate early close.
"Received" means at least one request reached replay. A signal after receipt can grade CRASH before the response
is complete. A timeout before the response is served grades HARNESS.

Invalid stdout is distinguished from no observation: it reaches rule 3, even if parsing produced no observation
object. The runner also supplies endpoint or stream mismatches. The grader does not inspect processes, sockets
or files.

A library that returned, or raised, with no request in the replay log talked to something else: a wrong base
URL, a closed port, or the real API. The cell can't say how the library reports this fixture, so it grades
HARNESS. Two errors are exceptions, because a library raises them before it sends anything: `validation` (it
refused the call) and `internal` (it crashed building the call). The later rules grade those.

Rule 5 needs a raised outcome: an `internal` error object on a returned result is graded by the requirements.

Rule 4 requires a matching manifest `unsupported` entry for this scenario and provider. An entry without a
provider list covers every provider. Without a matching entry, the result is HARNESS and a note says that the
manifest does not list it. A manifest entry alone does not override a supported observation.

Rules 1–4 return no requirement results and a note explaining why they applied. From rule 5 on, every applicable
requirement is evaluated, even for CRASH. A requirement with no provider list applies to all providers in its
scenario. A provider the scenario doesn't list, or one that no requirement applies to, raises `ValueError` before any
rule, so no cell can PASS with nothing checked. Unknown checks and bad arguments raise `ValueError` when evaluated.

## Evidence and result cells

SHOULD requirements never change the grade. Every requirement that is NOT_OBSERVABLE or CONTRADICTED adds a note
with its level, id, status and evidence, in this format: `<level> <id>: <status>: <evidence>`. The MUST notes say
why a cell isn't PASS, and `wiretruth run` prints every note under the grade table. A satisfied requirement adds
no note.
The shim's own notes stay in the observation, which the results cell stores in full.

`CellGrade.grade`, `CellGrade.requirements` and `CellGrade.notes` map to the corresponding results/v1 cell fields.
Every `RequirementResult` has exactly `id`, `level`, `status`, `evidence`. The one-based `CellGrade.rule` explains
which rule won for the caller; it is not an extra results/v1 field. The runner supplies the remaining cell fields,
including observation and request count; see [File formats](formats.md).

Evidence names the reported value, surface and source when known, alongside the expected value. For example,
`finish.value="stop" (primary: choices[0].finish_reason); truth.finish="length"` makes the contradiction explicit.
Request evidence names the request index and JSON path. Long values are shortened in evidence; the full
observation remains available in the results cell.
