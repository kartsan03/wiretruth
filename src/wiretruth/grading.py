"""Pure cell grading: apply the ordered rules from docs/grading.md to runner data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from wiretruth.checks import CHECKS, Status
from wiretruth.formats import Level, Observation, Requirement, Scenario, Truth, UnsupportedEntry
from wiretruth.replay import LoggedRequest

Grade = Literal["PASS", "LOSSY", "WRONG", "CRASH", "HANG", "UNSUPPORTED", "HARNESS"]

RULES = (
    "The shim's time limit ran out: HANG if the response had been served, otherwise HARNESS.",
    "The shim's process ended without an observation: CRASH if a signal killed it (a segfault, "
    "for example) after the library's request reached the replay server, otherwise HARNESS.",
    "The observation is invalid, the library called a different endpoint or stream mode than "
    "the fixture serves, or it returned or raised without a request reaching the replay server: "
    "HARNESS.",
    "The shim reports, with a documentation link, that the library can't express the call: "
    "UNSUPPORTED.",
    "The library raised an internal error: CRASH.",
    "A MUST requirement is CONTRADICTED: WRONG.",
    "A MUST requirement is NOT_OBSERVABLE: LOSSY.",
    "Otherwise: PASS.",
)


@dataclass(frozen=True)
class ShimRun:
    """Runner facts, with no process or file access.

    ``invalid`` describes invalid stdout; ``mismatch`` describes an endpoint/stream mismatch.
    No observation and no invalid stdout means the process ended without an observation.
    The runner supplies a stable snapshot of the request log.
    """

    timed_out: bool
    signal: int | None
    observation: Observation | None
    invalid: str | None
    mismatch: str | None
    request_log: Sequence[LoggedRequest]


@dataclass(frozen=True)
class RequirementResult:
    """One results/v1 cell's requirements[] item."""

    id: str
    level: Level
    status: Status
    evidence: str


@dataclass(frozen=True)
class CellGrade:
    grade: Grade
    rule: int
    requirements: tuple[RequirementResult, ...]
    notes: tuple[str, ...]


def _applicable(scenario: Scenario, provider: str) -> tuple[Requirement, ...]:
    if provider not in scenario.providers:
        raise ValueError(f"{scenario.id} doesn't list provider {provider!r}")
    applicable = tuple(
        requirement
        for requirement in scenario.requirements
        if provider
        in (scenario.providers if requirement.providers is None else requirement.providers)
    )
    if not applicable:
        raise ValueError(f"no requirement of {scenario.id} applies to {provider!r}")
    return applicable


def _requirements(
    obs: Observation,
    req_log: Sequence[LoggedRequest],
    requirements: Sequence[Requirement],
    truth: Truth,
) -> tuple[RequirementResult, ...]:
    results = []
    for requirement in requirements:
        try:
            check = CHECKS[requirement.check]
        except KeyError:
            raise ValueError(
                f"unknown check {requirement.check!r} for {requirement.id!r}"
            ) from None
        result = check(obs, truth, req_log, requirement.args)
        results.append(
            RequirementResult(requirement.id, requirement.level, result.status, result.evidence)
        )
    return tuple(results)


def grade_cell(
    run: ShimRun,
    *,
    scenario: Scenario,
    provider: str,
    truth: Truth,
    unsupported: Sequence[UnsupportedEntry],
) -> CellGrade:
    """Grade one cell by the first of ``RULES`` that applies; ``CellGrade.rule`` is its number.

    Rules 1-4 evaluate no requirements and give the reason in a note. From rule 5 on, every
    requirement that applies to the provider is evaluated, so a CRASH cell still carries their
    statuses. Every requirement that isn't SATISFIED adds a note, so the reason for a grade is
    visible; SHOULD requirements never change the grade.
    A provider the scenario doesn't list, a provider no requirement applies to, an unknown check
    or bad check args raise ValueError: a runner or scenario bug, not a grade.
    """
    applicable = _applicable(scenario, provider)
    received = bool(run.request_log)
    served = received and run.request_log[-1].response_complete
    if run.timed_out:
        if served:
            return CellGrade("HANG", 1, (), ("timed out after the response was served",))
        return CellGrade("HARNESS", 1, (), ("timed out before the response was served",))
    obs = run.observation
    if obs is None and run.invalid is None:
        if run.signal is not None and received:
            reason = f"killed by signal {run.signal} after a request reached the replay server"
            return CellGrade("CRASH", 2, (), (f"no observation; {reason}",))
        if run.signal is not None:
            reason = f"killed by signal {run.signal} before any request reached the replay server"
            return CellGrade("HARNESS", 2, (), (f"no observation; {reason}",))
        return CellGrade("HARNESS", 2, (), ("the shim ended without an observation",))
    if run.invalid is not None or run.mismatch is not None:
        reasons = tuple(
            f"{label}: {value}"
            for label, value in (("invalid observation", run.invalid), ("mismatch", run.mismatch))
            if value is not None
        )
        return CellGrade("HARNESS", 3, (), reasons)
    assert obs is not None
    if obs.outcome == "unsupported":
        for entry in unsupported:
            if entry.scenario == scenario.id and (
                entry.providers is None or provider in entry.providers
            ):
                return CellGrade("UNSUPPORTED", 4, (), (f"{entry.reason} ({entry.docs})",))
        reason = f"unsupported, but the manifest doesn't list {scenario.id} on {provider}"
        return CellGrade("HARNESS", 4, (), (reason,))
    requirements = _requirements(obs, run.request_log, applicable, truth)
    notes = tuple(
        f"{result.level} {result.id}: {result.status}: {result.evidence}"
        for result in requirements
        if result.status != "SATISFIED"
    )
    must = {result.status for result in requirements if result.level == "MUST"}
    if obs.outcome == "raised" and obs.error is not None and obs.error.category == "internal":
        crash = f"internal error: {obs.error.type}: {obs.error.message}"
        return CellGrade("CRASH", 5, requirements, (crash, *notes))
    if "CONTRADICTED" in must:
        return CellGrade("WRONG", 6, requirements, notes)
    if "NOT_OBSERVABLE" in must:
        return CellGrade("LOSSY", 7, requirements, notes)
    return CellGrade("PASS", 8, requirements, notes)
