import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import get_args

import pytest

from wiretruth.formats import (
    Call,
    JsonObject,
    LibraryInfo,
    Observation,
    ObservedError,
    ObservedFinish,
    Requirement,
    Scenario,
    StreamInfo,
    StructuredResult,
    ToolCall,
    Truth,
    UnsupportedEntry,
    Usage,
    load_scenario,
    load_schema,
)
from wiretruth.grading import RULES, CellGrade, Grade, ShimRun, grade_cell
from wiretruth.replay import LoggedRequest

REPO = Path(__file__).resolve().parents[2]
OBS = Observation(
    LibraryInfo("sample", "1.0", "python", "sample.call"),
    "returned",
    None,
    None,
    ObservedFinish("length", "length", "primary", "finish_reason"),
    None,
    StructuredResult(True, False, None),
    (),
    None,
    StreamInfo(True, False, 3),
    (),
    1,
)
TRUTH = Truth(
    "length", "length", "A soup recipe", (), None, None, False, Usage(10, 0, 5, 0, 15, {}, None)
)
REQUIREMENT = Requirement(
    "cause-is-truncation", "MUST", "truncation_is_signalled", "Reports truncation.", {}, None
)
SCENARIO = Scenario(
    "truncation.text",
    "Truncated soup recipe",
    "silent-truncation",
    "P0",
    "Soup recipe cut short.",
    Call(False, (), None, None, (), None, None, None, {}),
    ("openai-chat", "gemini"),
    (REQUIREMENT,),
    {},
    None,
    (),
)
REQUEST = LoggedRequest("POST", "/v1/chat/completions", {}, {}, {}, None, "", 0, True)
RUN = ShimRun(False, None, OBS, None, None, (REQUEST,))
UNSUPPORTED = UnsupportedEntry(
    SCENARIO.id, None, "No structured output", "https://example.org/features"
)
INTERNAL = replace(
    OBS,
    outcome="raised",
    error=ObservedError("IndexError", "No first item", "internal", None, "parser.py:8"),
)
UNKNOWN = replace(OBS, finish=None)
WRONG = replace(OBS, finish=ObservedFinish("stop", "stop", "primary", "finish_reason"))
NOT_SUPPORTED = replace(
    OBS,
    outcome="unsupported",
    unsupported_reason="No structured output: https://example.org/features",
)
SHOULD = Requirement(
    "usage-reported",
    "SHOULD",
    "usage_matches_truth",
    "Reports usage.",
    {"fields": ["output"]},
    None,
)


def grade(run: ShimRun = RUN, **kwargs: object) -> CellGrade:
    return grade_cell(
        run,
        **{
            "scenario": SCENARIO,
            "provider": "openai-chat",
            "truth": TRUTH,
            "unsupported": (),
            **kwargs,
        },
    )


@pytest.mark.parametrize(
    ("run", "unsupported", "expected", "rule"),
    [
        (replace(RUN, timed_out=True), (), "HANG", 1),
        (replace(RUN, timed_out=True, request_log=()), (), "HARNESS", 1),
        (replace(RUN, observation=None, signal=11), (), "CRASH", 2),
        (replace(RUN, observation=None), (), "HARNESS", 2),
        (replace(RUN, observation=None, invalid="stdout is not JSON"), (), "HARNESS", 3),
        (replace(RUN, mismatch="stream=true but fixture is JSON"), (), "HARNESS", 3),
        (replace(RUN, observation=NOT_SUPPORTED), (UNSUPPORTED,), "UNSUPPORTED", 4),
        (replace(RUN, observation=NOT_SUPPORTED), (), "HARNESS", 4),
        (replace(RUN, observation=INTERNAL), (), "CRASH", 5),
        (replace(RUN, observation=WRONG), (), "WRONG", 6),
        (replace(RUN, observation=UNKNOWN), (), "LOSSY", 7),
        (RUN, (), "PASS", 8),
    ],
)
def test_eight_rules_and_early_branches(
    run: ShimRun, unsupported: tuple[UnsupportedEntry, ...], expected: Grade, rule: int
) -> None:
    result = grade(run, unsupported=unsupported)
    assert (result.grade, result.rule) == (expected, rule)
    if rule <= 4:
        assert result.requirements == ()
        assert result.notes
    else:
        assert len(result.requirements) == 1


@pytest.mark.parametrize(
    "completed,expected", [((True, False), "HARNESS"), ((False, True), "HANG")]
)
def test_timeout_uses_last_request(completed: tuple[bool, ...], expected: Grade) -> None:
    log = tuple(replace(REQUEST, response_complete=value) for value in completed)
    result = grade(replace(RUN, timed_out=True, request_log=log))
    assert result.grade == expected
    assert "timed out" in result.notes[0]


@pytest.mark.parametrize("provider,expected", [("ollama", "HANG"), ("openai-chat", "HARNESS")])
def test_timeout_after_ollama_model_lookup(provider: str, expected: Grade) -> None:
    chat = replace(REQUEST, path="/api/chat", exchange=0)
    lookup = replace(REQUEST, path="/api/show", exchange=None, response_complete=False)
    scenario = replace(SCENARIO, providers=("openai-chat", "ollama"))
    run = replace(RUN, timed_out=True, request_log=(chat, lookup))
    assert grade(run, scenario=scenario, provider=provider).grade == expected


@pytest.mark.parametrize(
    "signal,log,expected",
    [
        (11, (), "HARNESS"),
        (None, (REQUEST,), "HARNESS"),
        (11, (replace(REQUEST, response_complete=False),), "CRASH"),
        (11, (replace(REQUEST, exchange=None, response_complete=False),), "CRASH"),
    ],
)
def test_no_observation_needs_signal_and_received_request(
    signal: int | None, log: tuple[LoggedRequest, ...], expected: Grade
) -> None:
    result = grade(replace(RUN, observation=None, signal=signal, request_log=log))
    assert (result.grade, result.rule) == (expected, 2)


@pytest.mark.parametrize(
    ("run", "expected", "rule"),
    [
        (replace(RUN, timed_out=True, observation=None, signal=11), "HANG", 1),
        (replace(RUN, observation=None, signal=11, mismatch="wrong endpoint"), "CRASH", 2),
        (replace(RUN, observation=NOT_SUPPORTED, mismatch="wrong endpoint"), "HARNESS", 3),
        (replace(RUN, observation=replace(NOT_SUPPORTED, error=INTERNAL.error)), "UNSUPPORTED", 4),
        (replace(RUN, observation=replace(INTERNAL, finish=WRONG.finish)), "CRASH", 5),
        (replace(RUN, observation=WRONG), "WRONG", 6),
        (replace(RUN, observation=UNKNOWN), "LOSSY", 7),
        (replace(RUN, timed_out=True, observation=INTERNAL), "HANG", 1),
    ],
)
def test_rule_precedence(run: ShimRun, expected: Grade, rule: int) -> None:
    scenario = replace(SCENARIO, requirements=(REQUIREMENT, replace(SHOULD, level="MUST")))
    result = grade(run, scenario=scenario, unsupported=(UNSUPPORTED,))
    assert (result.grade, result.rule) == (expected, rule)
    if rule == 5:
        assert result.requirements[0].status == "CONTRADICTED"
    if rule == 6:
        assert [item.status for item in result.requirements] == ["CONTRADICTED", "NOT_OBSERVABLE"]


def test_invalid_stdout_reaches_rule_three_even_with_signal() -> None:
    result = grade(replace(RUN, observation=None, invalid="missing finish", signal=11))
    assert (result.grade, result.rule) == ("HARNESS", 3)
    assert result.notes == ("invalid observation: missing finish",)


@pytest.mark.parametrize(
    "entries,expected",
    [
        ((UNSUPPORTED,), "UNSUPPORTED"),
        ((replace(UNSUPPORTED, providers=("openai-chat",)),), "UNSUPPORTED"),
        ((replace(UNSUPPORTED, providers=("gemini",)),), "HARNESS"),
        ((replace(UNSUPPORTED, scenario="truncation.structured-output"),), "HARNESS"),
        ((replace(UNSUPPORTED, providers=()), UNSUPPORTED), "UNSUPPORTED"),
    ],
)
def test_unsupported_must_match_scenario_and_provider(
    entries: tuple[UnsupportedEntry, ...], expected: Grade
) -> None:
    result = grade(replace(RUN, observation=NOT_SUPPORTED), unsupported=entries)
    assert result.grade == expected
    if expected == "HARNESS":
        assert "manifest doesn't list truncation.text" in result.notes[0]
        assert "openai-chat" in result.notes[0]
    else:
        assert UNSUPPORTED.reason in result.notes[0]
        assert UNSUPPORTED.docs in result.notes[0]


def test_manifest_entry_does_not_override_supported_observation() -> None:
    assert grade(unsupported=(UNSUPPORTED,)).grade == "PASS"


@pytest.mark.parametrize("fabricated,expected", [(False, "PASS"), (True, "WRONG")])
def test_partial_tool_args_with_fabricated_extra_call(fabricated: bool, expected: Grade) -> None:
    partial = ToolCall("call-1", "create_event", '{"title":"Lunar ecl', False, None)
    calls = (partial,)
    if fabricated:
        complete = '{"title":"Lunar eclipse"}'
        calls += (ToolCall("invented", "create_event", complete, True, {"title": "Lunar eclipse"}),)
    scenario = load_scenario(REPO / "suite/scenarios/truncation/tool-call-args.json")
    result = grade_cell(
        replace(RUN, observation=replace(OBS, tool_calls=calls)),
        scenario=scenario,
        provider="openai-responses",
        truth=replace(TRUTH, tool_calls=(partial,)),
        unsupported=(),
    )
    assert result.grade == expected


@pytest.mark.parametrize(
    "status,requirement",
    [
        ("NOT_OBSERVABLE", SHOULD),
        ("CONTRADICTED", replace(SHOULD, check="finish_is", args={"value": "stop"})),
        ("SATISFIED", replace(SHOULD, check="finish_is", args={"value": "length"})),
    ],
)
def test_should_status_notes_never_change_grade(status: str, requirement: Requirement) -> None:
    result = grade(scenario=replace(SCENARIO, requirements=(REQUIREMENT, requirement)))
    assert result.grade == "PASS"
    item = result.requirements[1]
    assert item.status == status
    expected = (
        () if status == "SATISFIED" else (f"SHOULD usage-reported: {status}: {item.evidence}",)
    )
    assert result.notes == expected


def test_crash_carries_all_requirements_and_their_notes() -> None:
    obs = replace(INTERNAL, finish=WRONG.finish, notes=("Optional setting omitted",))
    result = grade(
        replace(RUN, observation=obs),
        scenario=replace(SCENARIO, requirements=(REQUIREMENT, SHOULD)),
    )
    assert result.grade == "CRASH"
    assert [item.status for item in result.requirements] == ["CONTRADICTED", "NOT_OBSERVABLE"]
    assert "IndexError" in result.notes[0]
    assert result.notes[1:] == (
        f"MUST {REQUIREMENT.id}: CONTRADICTED: {result.requirements[0].evidence}",
        f"SHOULD usage-reported: NOT_OBSERVABLE: {result.requirements[1].evidence}",
    )


@pytest.mark.parametrize(
    "finish,grade_name,status",
    [(WRONG.finish, "WRONG", "CONTRADICTED"), (None, "LOSSY", "NOT_OBSERVABLE")],
)
def test_must_notes_say_why_a_cell_is_not_pass(finish, grade_name, status) -> None:
    result = grade(replace(RUN, observation=replace(OBS, finish=finish)))
    assert result.grade == grade_name
    assert result.notes == (f"MUST {REQUIREMENT.id}: {status}: {result.requirements[0].evidence}",)


def test_internal_error_on_a_returned_outcome_is_graded_by_requirements() -> None:
    result = grade(replace(RUN, observation=replace(INTERNAL, outcome="returned")))
    assert (result.grade, result.rule) == ("PASS", 8)


@pytest.mark.parametrize("category", ["parse", "validation", "network", "api"])
def test_noninternal_errors_are_graded_by_requirements(category: str) -> None:
    assert INTERNAL.error is not None
    obs = replace(INTERNAL, finish=None, error=replace(INTERNAL.error, category=category))
    assert grade(replace(RUN, observation=obs)).grade == "LOSSY"


def test_provider_filtering_skips_inapplicable_bad_checks() -> None:
    bad = replace(REQUIREMENT, id="other-provider", check="unknown", providers=("gemini",))
    should = replace(SHOULD, providers=("gemini",))
    scenario = replace(SCENARIO, requirements=(bad, REQUIREMENT, should))
    result = grade(scenario=scenario)
    assert result.grade == "PASS"
    assert [item.id for item in result.requirements] == [REQUIREMENT.id]
    assert result.notes == ()
    with pytest.raises(ValueError, match="unknown check"):
        grade(scenario=scenario, provider="gemini")


def test_none_requirement_providers_means_scenarios_providers() -> None:
    assert len(grade(provider="gemini").requirements) == 1


@pytest.mark.parametrize(
    "scenario,provider,message",
    [
        (SCENARIO, "anthropic", "doesn't list provider"),
        (replace(SCENARIO, requirements=()), "openai-chat", "no requirement"),
        (
            replace(SCENARIO, requirements=(replace(REQUIREMENT, providers=("gemini",)),)),
            "openai-chat",
            "no requirement",
        ),
    ],
)
@pytest.mark.parametrize("run", [RUN, replace(RUN, timed_out=True)])
def test_a_cell_with_nothing_to_check_is_a_value_error(
    scenario: Scenario, provider: str, message: str, run: ShimRun
) -> None:
    with pytest.raises(ValueError, match=message):
        grade(run, scenario=scenario, provider=provider)


@pytest.mark.parametrize(
    "check,args",
    [("misspelled", {}), ("finish_is", {}), ("truncation_is_signalled", {"typo": True})],
)
@pytest.mark.parametrize("obs", [OBS, INTERNAL])
def test_scenario_bugs_propagate_even_for_crash(
    check: str, args: JsonObject, obs: Observation
) -> None:
    scenario = replace(SCENARIO, requirements=(replace(REQUIREMENT, check=check, args=args),))
    with pytest.raises(ValueError):
        grade(replace(RUN, observation=obs), scenario=scenario)


@pytest.mark.parametrize(
    "run",
    [
        replace(RUN, timed_out=True),
        replace(RUN, observation=None),
        replace(RUN, invalid="bad JSON"),
        replace(RUN, observation=NOT_SUPPORTED),
    ],
)
def test_early_rules_do_not_evaluate_requirements(run: ShimRun) -> None:
    scenario = replace(SCENARIO, requirements=(replace(REQUIREMENT, check="unknown"),))
    result = grade(run, scenario=scenario)
    assert result.rule <= 4
    assert result.requirements == ()


def test_inputs_stay_unchanged() -> None:
    before = asdict(RUN), asdict(SCENARIO), asdict(TRUTH)
    result = grade()
    assert (result.grade, result.rule) == ("PASS", 8)
    assert before == (asdict(RUN), asdict(SCENARIO), asdict(TRUTH))
    assert result == grade()


def test_requirement_results_map_to_results_schema() -> None:
    result = grade(replace(RUN, observation=WRONG))
    item = asdict(result.requirements[0])
    assert item == {
        "id": REQUIREMENT.id,
        "level": "MUST",
        "status": "CONTRADICTED",
        "evidence": result.requirements[0].evidence,
    }
    schema = load_schema("results.schema.json")
    assert list(get_args(Grade)) == schema["$defs"]["cell"]["properties"]["grade"]["enum"]
    assert set(item) == set(
        schema["$defs"]["cell"]["properties"]["requirements"]["items"]["required"]
    )


def test_documented_rules_equal_module_rules_in_order() -> None:
    doc = (REPO / "docs/grading.md").read_text(encoding="utf-8")
    section = doc.split("## Grade rules\n", 1)[1].split("\n## ", 1)[0]
    entries = re.findall(r"^(\d+)\. (.*(?:\n   .*)*)", section, re.MULTILINE)
    assert [int(number) for number, _ in entries] == list(range(1, 9))
    assert tuple(" ".join(text.split()) for _, text in entries) == RULES
    assert [
        re.findall(r"\b(?:HANG|HARNESS|CRASH|UNSUPPORTED|WRONG|LOSSY|PASS)\b", rule)
        for rule in RULES
    ] == [
        ["HANG", "HARNESS"],
        ["CRASH", "HARNESS"],
        ["HARNESS"],
        ["UNSUPPORTED"],
        ["CRASH"],
        ["WRONG"],
        ["LOSSY"],
        ["PASS"],
    ]


def test_rule_wording_matches_concepts() -> None:
    concepts = " ".join((REPO / "docs/concepts.md").read_text(encoding="utf-8").split())
    for rule in RULES:
        assert rule in concepts
