# 0002. Shims are processes that speak JSON over stdin and stdout

- Status: Accepted
- Date: 2026-10-01

## Context

The suite tests libraries in at least two languages, and it tests several versions of the same library. Those
libraries pin conflicting dependencies, such as different major versions of pydantic or of an HTTP client. Something
has to translate the suite's neutral call into an idiomatic call for each library and report back what the library
did. A library under test can also crash, hang or print to stdout. toml-test solved the same problem for TOML
parsers with a small stdin/stdout contract that any language can implement.

## Decision

Each library gets a shim: a separate program that runs in that library's own environment (a uv virtualenv for
Python, an npm prefix for JavaScript).

- The runner starts the shim with the command from its manifest and sends one call spec as JSON on stdin.
- The shim prints exactly one observation as JSON on stdout and exits 0, also when the library raised. Everything
  else, including anything the library prints, goes to stderr.
- The shim also catches failures that escape the library call, such as an unhandled promise rejection, and reports
  them in the observation.
- The runner enforces a time limit and turns a missing or invalid observation into a grade, using the rules in
  [0004](0004-grades-and-surfaces.md): a hang after the library received the response is HANG, a process killed by
  a signal after that point is CRASH, and everything else is HARNESS.

## Consequences

- Shims can be written in any language, and each library version runs in an isolated environment.
- A library that crashes or hangs can't take the runner down with it.
- Every cell pays for a process start, which counts against the time limit. Because HANG needs the library to have
  received the response, slow start-up is graded HARNESS, not blamed on the library.
- The contract has to be documented precisely, and observations are validated against a JSON Schema on every run.

## Alternatives considered

- **In-process plugins:** dependency conflicts between libraries in one interpreter, Python only, and a library
  failure can affect the runner.
- **One long-running shim server per library:** faster per cell, but adds a protocol, state between cells, and
  more ways for one cell to affect the next.
