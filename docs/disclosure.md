# Disclosure

This page is for maintainers of the libraries wiretruth tests, and for anyone reading a red cell. It explains when
a finding is reported upstream, what each status in `results/upstream.json` means, and how to dispute a grade.

## Report before publishing

A cell can turn WRONG, CRASH or HANG on `main` only after the finding is on record in `results/upstream.json`: with
an upstream issue (one this project opened, or an existing one it links to), or, when the library already documents
the behavior, with a reference to that documentation. These are ordinary bugs, so a public issue is the usual
channel. Until the entry exists, the results with that cell stay off `main`.

Every issue this project opens works without wiretruth: it has a reproduction that uses the library alone, the
provider fields that matter, verbatim, and a link to the fixture. It follows the library's own issue template and
contribution rules.

## Security-relevant findings

If a finding looks security-relevant, for example a library that sends a key to the wrong host or leaks request
content across calls, it goes to the library's private advisory channel instead of a public issue. Its results stay
off `main` until a fix ships or 90 days pass. Security issues in wiretruth itself are covered by the
[security policy](https://github.com/kartsan03/wiretruth/blob/main/SECURITY.md).

## When a fix ships

A weekly workflow runs every library's latest releases and proposes the new results as a pull request, so a fixed
release changes the grade without a hand edit. The changelog's Results section names the fix, and
[Validation against fixed bugs](validation.md) lists fixed bugs with their grades before and after.

## Documented behavior

If the maintainers say a behavior is intended and documented, or the library's documentation already says so, the
cell keeps its computed grade. It gets a "documented behavior" note that cites the statement or documentation, and
the upstream status becomes `documented-behavior`. Grades are never edited by hand.

## Disputing a grade

Open a [grade dispute](https://github.com/kartsan03/wiretruth/issues/new?template=grade-dispute.yml) with the cell
and what you think the library does. You'll get a response within 7 days.

- If the scenario or its checks were too strict, they are fixed in a pull request that cites the dispute, and the
  changelog says so.
- If the shim called the library wrong, the shim is fixed and the dispute is credited.
- If the behavior is intended, the cell gets a documented-behavior note as above.

## Upstream statuses

Each entry in [`results/upstream.json`](https://github.com/kartsan03/wiretruth/blob/main/results/upstream.json)
covers a set of cells and links the upstream issue and the fix pull request when they exist. A
`documented-behavior` entry without an issue cites the library's documentation in its notes. The site shows the entries on each
affected cell page. When the root cause is in a dependency of the library or in the provider, the entry links that
project's issue and its notes say so. All fields are described in [File formats](formats.md#upstream-tracking).

| Status | Meaning |
|---|---|
| `reported` | The upstream issue exists. No maintainer has confirmed it yet. |
| `confirmed` | A maintainer has confirmed the bug. |
| `fix-merged` | A fix is merged upstream but isn't in a release that wiretruth tests yet. |
| `released` | The fix is in a release. `fixed_in` names the first version with it. |
| `wontfix` | The maintainers decided not to change the behavior. |
| `disputed` | The finding is disputed and the dispute is still open. |
| `documented-behavior` | The maintainers say the behavior is intended and documented. The cell keeps its grade. |
