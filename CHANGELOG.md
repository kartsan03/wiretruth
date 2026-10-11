# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Fixture, scenario and result formats are versioned
separately (`wiretruth.fixture/v1`, …).

The **Results** section lists grade changes for tested libraries (for example, a library release that fixes a
finding).

## [Unreleased]

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
  behind JSON escapes or in the JSON of a stream event. The recorder runs the same scan on the raw body before it
  parses it, and on the fixture before it writes. Neither prints a flagged value in any other message.
- The first scenario, `truncation.text`: plain text cut off by the output token limit.
- Two more truncation scenarios: `truncation.text-stream`, the same text streamed, and
  `truncation.structured-output`, a JSON object for a strict schema cut off halfway.
- The first recorded fixtures: the three truncation scenarios on `openai-chat`, recorded in CI against
  `gpt-6-luna`.

### Results
- any-llm 1.33.0, the first library results: PASS on all three truncation scenarios on `openai-chat`
  ([results/any-llm/1.33.0.json](results/any-llm/1.33.0.json)).
