# 0004. Grades and surfaces

- Status: Accepted
- Date: 2026-10-01

## Context

Pass or fail hides the cases that matter most to users. A library can expose a fact only in raw provider metadata
that almost nobody reads. It can also fail loudly with the wrong cause, for example a JSON parse error when the
real cause was the token limit. A crash inside the library is a different problem from a wrong value. Problems in
our own harness must not be blamed on a library. A single score per library would invite ranking, which the
project avoids.

## Decision

- Each requirement's check returns SATISFIED, CONTRADICTED or NOT_OBSERVABLE, with a short evidence string.
- Each value in an observation records its surface: `primary` (a documented field of the main result), `raw`
  (provider metadata or the raw payload) or `absent`.
- A cell's grade comes from the first rule that applies:
  1. The shim's time limit ran out: HANG if the response had been served, otherwise HARNESS.
  2. The process ended without an observation: CRASH if a signal killed it after the response had been served,
     otherwise HARNESS.
  3. The observation is invalid, or the library called a different endpoint or stream mode than the fixture
     serves: HARNESS.
  4. The shim reports the call as unsupported, with a documentation link: UNSUPPORTED. A reasoning setting the
     library can't send is not a reason ([0006](0006-default-library-config.md)).
  5. The library raised an internal error, a language builtin raised from a frame inside its own code: CRASH. JSON
     decode and schema validation errors don't count, even where they subclass a builtin.
  6. A MUST requirement is CONTRADICTED: WRONG.
  7. A MUST requirement is NOT_OBSERVABLE: LOSSY.
  8. Otherwise: PASS.
- "Served" means the replay server finished sending the fixture's response, including the deliberate early close of
  an aborted-connection fixture. The server records this for every request.
- SHOULD requirements add notes, saying whether the requirement was NOT_OBSERVABLE or CONTRADICTED, and never change
  the grade. A value that is the subject of a scenario is a MUST there.
- There is no overall score and no ranking.

## Consequences

- Every grade explains itself through the requirement statuses and their evidence.
- Shims have to record where each value came from, and manifests declare the primary and raw surfaces of each
  library.
- A grade dispute has a clear shape: which requirement, which status, which evidence.
- Seven grades need explaining. That's the job of the grading documentation and the legend on every page.

## Alternatives considered

- **Pass or fail only:** hides the difference between "contradicts the truth" and "you have to dig for it".
- **Numeric scores or weighted totals:** invite leaderboards and hide which fact went wrong.
