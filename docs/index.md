# Documentation

This is the map of wiretruth's documentation. Start with Concepts if you're new.

## Understand

- [Concepts](concepts.md): providers, scenarios, fixtures, truth, shims, observations, surfaces and grades, with one
  example followed from fixture to grade.
- [FAQ](faq.md): fairness to libraries, recorded versus live responses, fixture tiers, LOSSY grades, providers,
  library CI, what the suite doesn't catch and who pays for recording.
- [Validation against fixed bugs](validation.md): committed grades before and after upstream fixes, with the
  fixtures that reproduce them.
- [Disclosure](disclosure.md): when findings are reported upstream, what each upstream status means, documented
  behavior and grade disputes.

## Reference

- [Scenario catalog](scenarios.md): scenarios, requirements and fixture tiers by provider.
- [Grading](grading.md): requirement checks, evidence, surfaces and the ordered rules for cell grades.
- [File formats](formats.md): every field of scenarios, fixtures, call specs, observations, shim manifests, results,
  upstream tracking and bug classes, and what `wiretruth lint` checks.
- [Recording fixtures](recording.md): what the recorder sends and stores, recording in CI or locally, the secret
  scan, and how the truth is read from each provider's responses.
- [Derived fixtures](derived-fixtures.md): the six transforms, their evidence and arguments, and how to create a
  fixture with recomputed truth.

## Contribute

- [Writing a shim](writing-a-shim.md): the rules, the environment, what to read and print, a complete example,
  local testing and submitting a library.

## Decisions

Each decision record gives the context, the decision, its consequences and the alternatives that were rejected.

- [0001. Verbatim HTTP replay](adr/0001-verbatim-http-replay.md)
- [0002. Shims are processes that speak JSON over stdin and stdout](adr/0002-shims-as-processes.md)
- [0003. Suite data is JSON validated by JSON Schema](adr/0003-json-suite-data.md)
- [0004. Grades and surfaces](adr/0004-grades-and-surfaces.md)
- [0005. Provenance tiers for fixtures](adr/0005-provenance-tiers.md)
- [0006. Libraries run with their default configuration](adr/0006-default-library-config.md)
- [Template for new records](adr/0000-template.md)
