# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Fixture, scenario and result formats are versioned
separately (`wiretruth.fixture/v1`, …).

The **Results** section lists grade changes for tested libraries (for example, a library release that fixes a
finding).

## [Unreleased]

### Added
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
