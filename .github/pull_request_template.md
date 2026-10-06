## Summary

<!-- 1–3 sentences: what this PR does. -->

Closes #

## Why

<!-- The problem, or the issue it resolves. -->

## What changed

-

## How tested

<!-- Commands you ran and the relevant output. -->

```text

```

## Results impact

<!-- Grades that changed (library, scenario, provider: old → new), or "none". -->

none

## Checklist

- [ ] Tests added or updated
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q` pass
- [ ] `uv run wiretruth lint` passes
- [ ] `CHANGELOG.md` updated under `Unreleased` (if user-facing)
- [ ] Docs updated (if behavior or formats changed)
