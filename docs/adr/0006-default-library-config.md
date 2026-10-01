# 0006. Libraries run with their default configuration

- Status: Accepted
- Date: 2026-10-01

## Context

Most libraries have options that change how they handle edge cases: strict parsing, retries, custom finish-reason
mappers, raw response access. If each shim tuned those options, the results would describe our configuration,
not what users get when they follow the library's documentation. Some libraries still need small changes to run
offline, for example to stop a network fetch at import time.

## Decision

- Shims call each library the way its documentation shows, with default settings and public APIs only.
- Any deviation that's needed to run, such as an environment variable, is declared in the shim's manifest and
  shown with the results.
- A feature the library can't express is reported as unsupported, with a documentation link. The shim never works
  around it. One exception: when a library can't send a requested reasoning setting (such as reasoning effort
  `none`), the shim makes the call without it and records a note. The replayed response is the same either way,
  so the library's handling of it is still graded.
- Shims never retry, repair output or read private attributes.

## Consequences

- Results describe what users get by default.
- When a documented option changes a grade, the cell can carry a note pointing to it. Testing a non-default
  configuration would need its own, clearly labeled shim variant.
- A library that changes its defaults between versions shows a different grade for the new version, which is
  correct.

## Alternatives considered

- **A tuned configuration per library:** would test our choice of options, not the library.
- **Every combination of options:** multiplies the matrix without telling users what they get by default.
