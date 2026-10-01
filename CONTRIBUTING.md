# Contributing to wiretruth

Thanks for helping. wiretruth is at an early stage, so this guide only covers working on the code itself. Guides
for adding libraries and scenarios, and for disputing a grade, will be added later.

## Development setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kartsan03/wiretruth && cd wiretruth
uv sync --extra dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy src
```

## Pull requests

- One concern per PR, ideally under 400 changed lines (fixtures, lockfiles and generated docs don't count).
- The title follows [Conventional Commits](https://www.conventionalcommits.org/): `feat(replay): …`,
  `fix(grading): …`, `docs: …`. PRs are squash-merged, so the title becomes the commit.
- Fill in the PR template, including how you tested it.
- Add a line to `CHANGELOG.md` under `Unreleased` for anything a user would notice.
- CI must be green: lint, types and tests on Python 3.11, 3.12 and 3.13.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
