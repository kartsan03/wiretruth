# 0005. Provenance tiers for fixtures

- Status: Accepted
- Date: 2026-10-01

## Context

The best fixture is a response recorded from the real API. Some important shapes are hard or impossible to get on
demand: an error in the middle of a stream, a dropped connection, a refusal, a new finish value. Some providers
can't be recorded without keys the project doesn't have. Readers need to know how much weight to give a cell,
and that depends on where its fixture came from.

## Decision

Every fixture has one of three tiers, stored in the fixture and shown on every cell:

- **recorded:** sent by the real API and saved by the recorder. Provenance includes the requested and returned
  model, the time, the recorder version and a hash of the raw body, plus the CI run when it was recorded in CI.
- **documented:** transcribed from the provider's official documentation. Provenance includes the source URL and
  notes on what was filled in, such as ids.
- **derived:** another fixture changed by a named transform (for example, cut the stream after five events). The
  fixture stores the source fixture, the transform and its arguments, the reason, and a link showing that the
  shape occurs in practice. A derived fixture's source can be recorded or documented, and the cell shows both.

Whatever the tier, the truth is recomputed from the fixture's bytes by the provider's reference parser.

Fixtures never contain credentials. Keys travel only in request headers, and the recorder keeps only the
`content-type` and `retry-after` response headers. The recorder refuses to write, and `wiretruth lint` rejects, a
fixture that contains anything that looks like a key or an authentication header.

Each cell uses one fixture, `suite/fixtures/<provider>/<scenario>.json`. A fixture is replaced only by one of the
same or a better tier, where recorded is better than documented and documented is better than derived. The
replaced file can be kept as a dated variant (`<scenario>@<date>.json`), which no cell uses by default.

## Consequences

- Coverage reaches failure shapes that can't be recorded on demand, including for providers that only have
  documented fixtures.
- The labels are honest: a reader can always tell a real response from a constructed one.
- Derived fixtures must justify themselves with evidence. Documented fixtures can drift from real behavior and get
  replaced by recordings when possible.
- `wiretruth lint` enforces the required provenance fields for each tier.

## Alternatives considered

- **Recorded fixtures only:** can't cover mid-stream errors, dropped connections or providers without keys.
- **Hand-written fixtures without labels:** readers couldn't judge how real a cell is.
- **Fixtures generated from API schemas:** produce shapes the provider may never send.
