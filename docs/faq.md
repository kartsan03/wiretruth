# FAQ

Short answers to the questions people ask most about wiretruth: how it treats libraries, where its fixtures come
from, what a grade means and how to take part. Each answer links to the page with the details.

## Isn't this a wall of shame?

No. The matrix is a list of edge cases with what each library reports, so you can see what your code has to
handle. A library gets a WRONG, CRASH or HANG cell on `main` only after the finding is on record upstream, as an
issue or in the library's own documentation, and fixed releases are named in the changelog. Grades are computed by the code in this repository and are never edited by
hand. Maintainers can dispute any grade. See [Disclosure](disclosure.md).

## Why recorded responses and not live calls?

Edge cases like a stream that stops early or a token limit hit inside a tool call are rare or impossible to trigger
on demand, and a live model answers differently each time. A recorded response gives every library the same bytes
on every run. Running the suite needs no API keys and costs nothing, and every grade can be reproduced from a
clone with `wiretruth serve`.

## What are derived and documented fixtures?

Most fixtures are recorded: real responses, captured by a public CI workflow. A **derived** fixture is a recorded
or documented response with one deterministic change, such as a stream cut before its finish event. Each one names the transform
and links evidence that the shape happens in practice ([Derived fixtures](derived-fixtures.md)). A **documented**
fixture is transcribed from the provider's API documentation when there is no key to record with (Anthropic today),
and links the page it comes from. Every cell shows its fixture's tier.

## My library behaves like this on purpose

Then say so in a [grade dispute](https://github.com/kartsan03/wiretruth/issues/new?template=grade-dispute.yml),
with a link to the documentation or the discussion where you decided it. The cell keeps its computed grade and
gets a "documented behavior" note that cites your statement, so readers see both. If the scenario or the shim is
wrong instead, it gets fixed and the dispute is credited. See [Disclosure](disclosure.md#documented-behavior).

## Why does a LOSSY grade exist?

Not every gap is a contradiction. A library that returns a cut-off stream without saying it is incomplete hasn't
reported anything false, but the caller can't tell either. LOSSY means the library doesn't contradict the provider,
but the fact is missing or only available in raw metadata. WRONG is kept for a reported value that contradicts what
the provider sent, such as `finish_reason: "stop"` on a truncated response. See [Grading](grading.md).

## How do I add a provider?

Suggest it in [Discussions](https://github.com/kartsan03/wiretruth/discussions/categories/ideas) first, because a
provider adds a column to every library. A provider is a module in
`src/wiretruth/providers/` that builds the native request from a scenario's call and reads the truth from the
response, registered in `src/wiretruth/providers/__init__.py`, plus its id in
`src/wiretruth/schemas/common.schema.json`, its base path in `src/wiretruth/replay.py` and its option in
`.github/workflows/record.yml`. Then its fixtures are recorded, each shim maps the provider or
declares it unsupported, and every library's results are re-run. See [Recording fixtures](recording.md) and
[Writing a shim](writing-a-shim.md).

## How do I run this in my library's CI?

The runner installs the exact versions pinned in the shim's manifest, so a run tests a release, not a branch. A CI
job can check out wiretruth at a release tag, change the pin (for JavaScript, also in the shim's `package.json` and
lock file), and run `uv run wiretruth run --lib <name>`. With
`--check-grades FILE`, the run exits 1 when any grade or requirement status differs from a file of expected grades
in the format of [`tests/expected-grades/`](https://github.com/kartsan03/wiretruth/tree/main/tests/expected-grades).
wiretruth's own weekly workflow runs every library's latest releases and proposes the new results as a pull
request. Testing an unreleased build isn't supported yet.

## What doesn't wiretruth catch?

It grades only the scenarios it has, so a library can pass every cell and still mishandle a case the suite doesn't
cover yet. A fixture is one moment of a provider: a later change in its responses shows up only when the fixture is
re-recorded, and by default the old one stays as a dated file. Shims use default settings, apart from changes
declared in their manifests, and public APIs only ([ADR 0006](adr/0006-default-library-config.md)), so other
options that change the outcome aren't graded. Responses are
served from `127.0.0.1` as recorded, so slow links, proxies and TLS aren't part of a run. Anthropic fixtures come
from its documentation, not from recordings.

## Is this related to spantruth?

No. [spantruth](https://github.com/Plexito-de/spantruth) is a separate project that was called wiretruth until
2026-10-12. It replays provider responses through LLM instrumentation libraries and checks what they write on
spans. wiretruth checks what the SDK returns to the caller.

## Who pays for recording?

The maintainer, from their own provider accounts. Recordings are small: short prompts, low output limits, and one
recording per scenario and provider, kept until it needs replacing. Gemini fixtures use its
[free tier](https://ai.google.dev/gemini-api/docs/pricing), Ollama runs in CI, and Anthropic fixtures are
transcribed, so only OpenAI calls cost money. Running the suite needs no keys at all.
