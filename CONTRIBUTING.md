# Contributing to wiretruth

Thanks for helping. There are four common ways to contribute, and each has its own path below:

1. [Dispute a grade](#disputing-a-grade)
2. [Add a library](#adding-a-library)
3. [Add a scenario](#adding-a-scenario)
4. [Fix or improve wiretruth itself](#development-setup)

Questions and ideas go to [Discussions](https://github.com/kartsan03/wiretruth/discussions).

## Disputing a grade

If you maintain or use a library and think a grade is wrong, open a
[grade dispute](https://github.com/kartsan03/wiretruth/issues/new?template=grade-dispute.yml). Include the cell
link and what you think the library does. You'll get a response within 7 days. If the scenario or the shim was
wrong, it gets fixed and the changelog credits you. If the behavior is intended and documented, the cell gets a
note linking to your documentation. The full policy is in [Disclosure](docs/disclosure.md).

## Adding a library

1. Read [Writing a shim](docs/writing-a-shim.md).
2. Create `shims/<language>/<library>/` with `wiretruth-shim.json` and the shim program.
3. Run `uv run wiretruth run --lib <library>` until there are no HARNESS (`?`) cells.
4. Open a pull request with the shim, its manifest and the results file it produced, as described in
   [Submitting](docs/writing-a-shim.md#submitting). If the run shows WRONG, CRASH or HANG cells, say so in the pull
   request; those cells reach `main` only after the library has an upstream issue for them.

Shims call the library the way its own documentation shows, with default settings. If the library can't express a
scenario that way, mark it unsupported in the manifest with a documentation link instead of working around it.

## Adding a scenario

1. Open a [scenario proposal](https://github.com/kartsan03/wiretruth/issues/new?template=new-scenario.yml) first:
   what the provider sends, what goes wrong for users, and evidence that it happens.
2. Add `suite/scenarios/<family>/<name>.json`. See [File formats](docs/formats.md#scenario--wiretruthscenariov1)
   and the existing scenarios.
3. Add a fixture for each provider the scenario lists:
   - **Recorded:** a maintainer runs the record workflow, which holds the API keys
     ([Recording fixtures](docs/recording.md)).
   - **Derived:** `uv run wiretruth derive …`, with a rationale and an evidence link
     ([Derived fixtures](docs/derived-fixtures.md)).
   - **Documented:** transcribed from official API docs, with the source URL.
4. Add the reference and liar cells to `tests/expected-grades/`. The reference shim must PASS the new scenario.
5. Regenerate the catalog with `uv run wiretruth catalog`, and run `uv run wiretruth lint`.
6. New cells need new results: every library's results are re-run in the same pull request, and shims that can't
   express the scenario get an unsupported entry. A maintainer can do this step.

## Development setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). JavaScript shims also need Node.js 22.

```bash
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv sync --extra dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run wiretruth lint
```

## Pull requests

- One concern per PR, ideally under 400 changed lines (fixtures, lockfiles and generated docs don't count).
- The title follows [Conventional Commits](https://www.conventionalcommits.org/): `feat(replay): …`,
  `fix(grading): …`, `docs: …`. PRs are squash-merged, so the title becomes the commit.
- Fill in the PR template, including how you tested it and whether any grades changed.
- Add a line to `CHANGELOG.md` under `Unreleased` for anything a user would notice.
- CI must be green: lint, types, tests on Python 3.11, 3.12 and 3.13, `wiretruth lint` on the suite data, the
  reference and liar self-tests, and, when shims, suite data, `src/` or results change, a conformance run of every
  library against its committed results.
- A maintainer may request an automated review by commenting `/review`. Its comments are suggestions to
  consider, not merge requirements. What it checks is described in `.github/review/`.

## Code style

- Python 3.11 or newer, typed (`mypy --strict` on `src/`), formatted and linted with ruff.
- The core has one runtime dependency, `jsonschema`. Adding another needs a short decision record in `docs/adr/`.
- Unit tests go in `tests/unit/`, end-to-end runs in `tests/integration/`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
