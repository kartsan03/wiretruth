# 0003. Suite data is JSON validated by JSON Schema

- Status: Accepted
- Date: 2026-10-01

## Context

Scenarios and fixtures should be useful outside wiretruth. Library maintainers should be able to vendor them into
their own test suites, in any language, without depending on wiretruth's code. JSON-Schema-Test-Suite and
Wycheproof work this way: the data is the product, and every implementation reads it with tools it already has.
Fixtures also hold raw byte chunks where exact bytes matter, so the format has to represent them without ambiguity.

## Decision

Everything wiretruth reads or writes is JSON with a published schema: scenarios, fixtures, results and shim
manifests as files, and call specs and observations as the messages between the runner and a shim.

- Each of these documents has a `format` field with a version, for example `wiretruth.fixture/v1`. Registries
  that are plain lists, such as the bug classes, have a schema but no `format` field.
- The schemas (Draft 2020-12) ship inside the package.
- `wiretruth lint` validates the suite data and the results in CI, and recomputes each fixture's truth from its
  bytes.
- Byte chunks that must be exact are stored as base64.

## Consequences

- Any language can read the data with its standard library.
- The core package has one runtime dependency, `jsonschema`.
- JSON is tedious to write by hand, especially escaped event streams. The recorder and the `derive` command write
  fixtures, so people rarely have to.
- A breaking format change needs a new version (`/v2`) and a migration note in the changelog.

## Alternatives considered

- **YAML:** nicer to write and supports comments, but every consumer needs a YAML parser, and implicit typing can
  change values.
- **TOML:** awkward for deeply nested arrays of objects, which fixtures are full of.
- **Python modules:** only Python could read them.
