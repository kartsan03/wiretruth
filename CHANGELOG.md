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
