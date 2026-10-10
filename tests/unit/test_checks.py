from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import get_args

import pytest

from wiretruth.checks import CHECKS, Status
from wiretruth.formats import (
    ErrorCategory,
    JsonObject,
    JsonValue,
    LibraryInfo,
    Observation,
    ObservedError,
    ObservedFinish,
    ObservedText,
    ObservedUsage,
    StreamInfo,
    StructuredResult,
    Surface,
    ToolCall,
    Truth,
    Usage,
    load_fixture,
    load_scenario,
)
from wiretruth.replay import LoggedRequest

REPO = Path(__file__).resolve().parents[2]
FIELDS = ("input", "cached_input", "output", "reasoning", "total")
TEXT = "Soupe au café · σούπα · 味噌汁 🍲"
CALL = ToolCall("tide-1", "tide_times", '{"port":"Brest"}', True, {"port": "Brest"})
PARTIAL = replace(CALL, arguments_raw='{"port":"Bre', arguments_parsed_ok=False, arguments=None)
TRUTH = Truth(
    "length", "length", TEXT, (CALL,), None, None, False, Usage(10, 2, 5, 1, 15, {}, None)
)
OBS = Observation(
    LibraryInfo("sample", "1.0", "python", "sample.call"),
    "returned",
    None,
    None,
    ObservedFinish("length", "length", "primary", "choices[0].finish_reason"),
    ObservedText(TEXT, "primary"),
    StructuredResult(True, False, None),
    (CALL,),
    ObservedUsage(10, 2, 5, 1, 15, dict.fromkeys(FIELDS, "primary")),
    StreamInfo(True, False, 3),
    (),
    1,
)


def raised(category: ErrorCategory = "parse", **changes: object) -> Observation:
    return replace(
        OBS,
        outcome="raised",
        error=ObservedError("sample.ReadError", "Incomplete tide times", category, None, None),
        **changes,
    )


def usage(**changes: object) -> Observation:
    assert OBS.usage is not None
    return replace(OBS, usage=replace(OBS.usage, **changes))


def request(body: JsonValue = None) -> LoggedRequest:
    return LoggedRequest("POST", "/v1/chat/completions", {}, {}, body, None, "", 0, True)


# Each check and every status it can return.
CASES = [
    ("finish_is", OBS, TRUTH, (), {"value": "length"}, "SATISFIED"),
    ("finish_is", OBS, TRUTH, (), {"value": "stop"}, "CONTRADICTED"),
    ("finish_is", replace(OBS, finish=None), TRUTH, (), {"value": "length"}, "NOT_OBSERVABLE"),
    ("finish_is_not", OBS, TRUTH, (), {"value": "stop"}, "SATISFIED"),
    ("finish_is_not", OBS, TRUTH, (), {"value": "length"}, "CONTRADICTED"),
    ("truncation_is_signalled", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "truncation_is_signalled",
        replace(OBS, finish=ObservedFinish("stop", "stop", "primary", "finish_reason")),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    ("truncation_is_signalled", raised(finish=None), TRUTH, (), {}, "NOT_OBSERVABLE"),
    ("text_matches_truth", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "text_matches_truth",
        replace(OBS, text=ObservedText("Chess: 1. e4", "primary")),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    ("text_matches_truth", replace(OBS, text=None), TRUTH, (), {}, "NOT_OBSERVABLE"),
    ("no_replacement_chars", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "no_replacement_chars",
        replace(OBS, text=ObservedText("Caf\ufffd", "primary")),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    ("no_replacement_chars", replace(OBS, text=None), TRUTH, (), {}, "NOT_OBSERVABLE"),
    ("no_success_structured_value", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "no_success_structured_value",
        replace(OBS, structured=StructuredResult(True, True, {"soup": "miso"})),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    (
        "no_success_structured_value",
        replace(OBS, structured=StructuredResult(False, False, None)),
        TRUTH,
        (),
        {},
        "NOT_OBSERVABLE",
    ),
    ("tool_calls_match_truth", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "tool_calls_match_truth",
        replace(OBS, tool_calls=(replace(CALL, name="chess_opening"),)),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    ("tool_calls_match_truth", replace(OBS, tool_calls=()), TRUTH, (), {}, "NOT_OBSERVABLE"),
    ("no_complete_tool_call_from_partial_args", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "no_complete_tool_call_from_partial_args",
        replace(OBS, tool_calls=(PARTIAL,)),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    ("usage_matches_truth", OBS, TRUTH, (), {"fields": ["input", "output"]}, "SATISFIED"),
    ("usage_matches_truth", usage(output=7), TRUTH, (), {"fields": ["output"]}, "CONTRADICTED"),
    (
        "usage_matches_truth",
        replace(OBS, usage=None),
        TRUTH,
        (),
        {"fields": ["output"]},
        "NOT_OBSERVABLE",
    ),
    ("usage_total_reconciles", OBS, TRUTH, (), {}, "SATISFIED"),
    ("usage_total_reconciles", usage(total=99), TRUTH, (), {}, "CONTRADICTED"),
    ("usage_total_reconciles", replace(OBS, usage=None), TRUTH, (), {}, "NOT_OBSERVABLE"),
    ("outcome_is_error", raised(), TRUTH, (), {}, "SATISFIED"),
    ("outcome_is_error", OBS, TRUTH, (), {}, "CONTRADICTED"),
    ("outcome_is_error", raised(), TRUTH, (), {"categories": ["truncation"]}, "NOT_OBSERVABLE"),
    ("stream_not_reported_complete", OBS, TRUTH, (), {}, "SATISFIED"),
    (
        "stream_not_reported_complete",
        replace(OBS, stream=StreamInfo(True, True, 3)),
        TRUTH,
        (),
        {},
        "CONTRADICTED",
    ),
    (
        "stream_not_reported_complete",
        replace(OBS, stream=StreamInfo(True, None, 3)),
        TRUTH,
        (),
        {},
        "NOT_OBSERVABLE",
    ),
    (
        "request_field_equals",
        OBS,
        TRUTH,
        (request({"stream": True}),),
        {"path": "stream", "value": True},
        "SATISFIED",
    ),
    (
        "request_field_equals",
        OBS,
        TRUTH,
        (request({"stream": False}),),
        {"path": "stream", "value": True},
        "CONTRADICTED",
    ),
    (
        "request_field_equals",
        OBS,
        TRUTH,
        (request({}),),
        {"path": "stream", "value": True},
        "NOT_OBSERVABLE",
    ),
    ("request_field_absent", OBS, TRUTH, (request({}),), {"path": "stream"}, "SATISFIED"),
    (
        "request_field_absent",
        OBS,
        TRUTH,
        (request({"stream": None}),),
        {"path": "stream"},
        "CONTRADICTED",
    ),
    ("request_count_at_most", OBS, TRUTH, (request(),), {"n": 1}, "SATISFIED"),
    ("request_count_at_most", OBS, TRUTH, (request(),), {"n": 0}, "CONTRADICTED"),
]


@pytest.mark.parametrize(
    ("name", "obs", "truth", "log", "args", "status"),
    CASES,
    ids=[f"{row[0]}-{row[-1]}" for row in CASES],
)
def test_check_statuses(
    name: str,
    obs: Observation,
    truth: Truth,
    log: Sequence[LoggedRequest],
    args: JsonObject,
    status: Status,
) -> None:
    result = CHECKS[name](obs, truth, log, args)
    assert result.status == status
    assert result.evidence


def test_registry_and_status_table_are_complete() -> None:
    expected = {
        "finish_is",
        "finish_is_not",
        "truncation_is_signalled",
        "text_matches_truth",
        "no_replacement_chars",
        "no_success_structured_value",
        "tool_calls_match_truth",
        "no_complete_tool_call_from_partial_args",
        "usage_matches_truth",
        "usage_total_reconciles",
        "outcome_is_error",
        "stream_not_reported_complete",
        "request_field_equals",
        "request_field_absent",
        "request_count_at_most",
    }
    assert set(CHECKS) == expected
    two_statuses = {
        "finish_is_not",
        "no_complete_tool_call_from_partial_args",
        "request_field_absent",
        "request_count_at_most",
    }
    for name in CHECKS:
        assert {row[-1] for row in CASES if row[0] == name} == set(get_args(Status)) - (
            {"NOT_OBSERVABLE"} if name in two_statuses else set()
        )


@pytest.mark.parametrize("name", CHECKS)
def test_unknown_args_are_rejected_before_checking_observation(name: str) -> None:
    args = next(row[4] for row in CASES if row[0] == name)
    with pytest.raises(ValueError, match="invalid check args"):
        CHECKS[name](OBS, TRUTH, (), {**args, "typo": True})


@pytest.mark.parametrize(
    ("name", "args"),
    [
        *[
            (name, args)
            for name in ("finish_is", "finish_is_not")
            for args in ({}, {"value": None}, {"value": []}, {"value": 1}, {"value": "MAX_TOKENS"})
        ],
        *[
            ("usage_matches_truth", args)
            for args in (
                {},
                {"fields": []},
                {"fields": "input"},
                {"fields": [1]},
                {"fields": ["tokens"]},
                {"fields": None},
            )
        ],
        *[
            ("outcome_is_error", {"categories": value})
            for value in (None, [], "parse", [True], ["timeout"])
        ],
        *[
            (name, {"path": path, **({"value": True} if name == "request_field_equals" else {})})
            for name in ("request_field_equals", "request_field_absent")
            for path in (None, 1, [], "", ".stream", "stream.", "stream..used")
        ],
        ("request_field_equals", {"path": "stream"}),
        ("request_field_equals", {"value": True}),
        ("request_field_absent", {}),
        *[("request_count_at_most", {"n": value}) for value in (-1, True, 1.0, "1", None)],
        ("request_count_at_most", {}),
    ],
)
def test_bad_args_raise_value_error(name: str, args: JsonObject) -> None:
    with pytest.raises(ValueError):
        CHECKS[name](OBS, TRUTH, (), args)


@pytest.mark.parametrize("surface", ["raw", "absent"])
@pytest.mark.parametrize("value", ["length", "stop", None])
@pytest.mark.parametrize("name", ["finish_is", "truncation_is_signalled"])
def test_nonprimary_finish_neither_satisfies_nor_contradicts(
    name: str, surface: Surface, value: str | None
) -> None:
    obs = replace(OBS, finish=ObservedFinish(value, value, surface, "provider_details.finish"))
    args = {} if name == "truncation_is_signalled" else {"value": "length"}
    assert CHECKS[name](obs, TRUTH, (), args).status == "NOT_OBSERVABLE"


@pytest.mark.parametrize("surface", ["raw", "absent"])
def test_finish_is_not_counts_a_nonprimary_finish_as_satisfied(surface: Surface) -> None:
    obs = replace(
        OBS, finish=ObservedFinish("length", "length", surface, "provider_details.finish")
    )
    assert CHECKS["finish_is_not"](obs, TRUTH, (), {"value": "length"}).status == "SATISFIED"
    assert (
        CHECKS["finish_is_not"](replace(OBS, finish=None), TRUTH, (), {"value": "length"}).status
        == "SATISFIED"
    )


@pytest.mark.parametrize("name", ["finish_is", "truncation_is_signalled"])
def test_documented_error_side_finish(name: str) -> None:
    obs = raised(finish=ObservedFinish("length", "length", "primary", "error.finishReason"))
    args = {"value": "length"} if name == "finish_is" else {}
    result = CHECKS[name](obs, TRUTH, (), args)
    assert result.status == "SATISFIED"
    assert "primary: error.finishReason" in result.evidence


@pytest.mark.parametrize("category", ["parse", "validation"])
def test_generic_error_without_finish_does_not_signal_truncation(category: ErrorCategory) -> None:
    assert (
        CHECKS["truncation_is_signalled"](raised(category, finish=None), TRUTH, (), {}).status
        == "NOT_OBSERVABLE"
    )


@pytest.mark.parametrize("error_signal", [False, True])
def test_truncation_satisfaction_precedes_structured_success(error_signal: bool) -> None:
    obs = raised("truncation", finish=None) if error_signal else OBS
    obs = replace(obs, structured=StructuredResult(True, True, {"recipe": "soup"}))
    assert CHECKS["truncation_is_signalled"](obs, TRUTH, (), {}).status == "SATISFIED"
    assert CHECKS["no_success_structured_value"](obs, TRUTH, (), {}).status == "CONTRADICTED"


def test_truncation_error_precedes_primary_stop() -> None:
    obs = raised("truncation", finish=ObservedFinish("stop", "stop", "primary", "finish"))
    assert CHECKS["truncation_is_signalled"](obs, TRUTH, (), {}).status == "SATISFIED"


@pytest.mark.parametrize("value", ["stop", "tool_calls"])
def test_primary_success_finish_contradicts_truncation(value: str) -> None:
    obs = replace(OBS, finish=ObservedFinish(value, value, "primary", "finish"))
    assert CHECKS["truncation_is_signalled"](obs, TRUTH, (), {}).status == "CONTRADICTED"


def test_raised_precedes_finish_and_stream_contradictions() -> None:
    obs = raised(stream=StreamInfo(True, True, 3))
    assert CHECKS["finish_is_not"](obs, TRUTH, (), {"value": "length"}).status == "SATISFIED"
    assert CHECKS["stream_not_reported_complete"](obs, TRUTH, (), {}).status == "SATISFIED"


@pytest.mark.parametrize("name", ["text_matches_truth", "no_replacement_chars"])
@pytest.mark.parametrize(
    "text",
    [
        ObservedText(TEXT, "raw"),
        ObservedText("\ufffd", "raw"),
        ObservedText(TEXT, "absent"),
        ObservedText(None, "primary"),
    ],
)
def test_text_requires_primary_nonnull_value(name: str, text: ObservedText) -> None:
    assert CHECKS[name](replace(OBS, text=text), TRUTH, (), {}).status == "NOT_OBSERVABLE"


@pytest.mark.parametrize("text", ["", "Café", "σούπα", "将棋", "🌊"])
def test_text_compares_exact_unicode(text: str) -> None:
    obs = replace(OBS, text=ObservedText(text, "primary"))
    truth = replace(TRUTH, text=text)
    assert CHECKS["text_matches_truth"](obs, truth, (), {}).status == "SATISFIED"
    assert CHECKS["no_replacement_chars"](obs, truth, (), {}).status == "SATISFIED"


def test_text_does_not_normalize_unicode_or_treat_empty_as_null() -> None:
    obs = replace(OBS, text=ObservedText("café", "primary"))
    assert (
        CHECKS["text_matches_truth"](obs, replace(TRUTH, text="cafe\u0301"), (), {}).status
        == "CONTRADICTED"
    )
    obs = replace(OBS, text=ObservedText("", "primary"))
    assert (
        CHECKS["text_matches_truth"](obs, replace(TRUTH, text=None), (), {}).status
        == "CONTRADICTED"
    )


@pytest.mark.parametrize("field", FIELDS)
def test_each_usage_field_accepts_all_alternatives(field: str) -> None:
    truth = replace(
        TRUTH, usage=replace(TRUTH.usage, alternatives={field: {"reading_a": 7, "reading_b": 9}})
    )
    for value in (7, 9):
        assert (
            CHECKS["usage_matches_truth"](
                usage(**{field: value}), truth, (), {"fields": [field]}
            ).status
            == "SATISFIED"
        )


@pytest.mark.parametrize("surface", ["raw", "absent"])
@pytest.mark.parametrize("value", [5, 99])
def test_usage_comparisons_require_primary(surface: Surface, value: int) -> None:
    obs = usage(output=value, surface={**dict.fromkeys(FIELDS, "primary"), "output": surface})
    assert (
        CHECKS["usage_matches_truth"](obs, TRUTH, (), {"fields": ["output"]}).status
        == "NOT_OBSERVABLE"
    )


@pytest.mark.parametrize(
    "check,args,field",
    [
        ("usage_matches_truth", {"fields": ["output"]}, "output"),
        ("usage_total_reconciles", {}, "total"),
    ],
)
def test_real_null_truth_distinguishes_none_from_invented_usage(
    check: str, args: JsonObject, field: str
) -> None:
    truth = load_fixture(REPO / "suite/fixtures/openai-chat/truncation.text-stream.json").truth
    assert getattr(truth.usage, field) is None
    for obs in (replace(OBS, usage=None), usage(**{field: None})):
        result = CHECKS[check](obs, truth, (), args)
        assert result.status == "SATISFIED"
        assert "library reported none" in result.evidence
    for value in (0, 15):
        result = CHECKS[check](usage(**{field: value}), truth, (), args)
        assert result.status == "CONTRADICTED"
        assert "wire carried no" in result.evidence


@pytest.mark.parametrize(
    "check,args,field",
    [
        ("usage_matches_truth", {"fields": ["output"]}, "output"),
        ("usage_total_reconciles", {}, "total"),
    ],
)
def test_null_truth_raw_usage_remains_unobservable(
    check: str, args: JsonObject, field: str
) -> None:
    truth = replace(TRUTH, usage=replace(TRUTH.usage, **{field: None}))
    obs = usage(surface={**dict.fromkeys(FIELDS, "primary"), field: "raw"})
    assert CHECKS[check](obs, truth, (), args).status == "NOT_OBSERVABLE"


@pytest.mark.parametrize("output,status", [(5, "NOT_OBSERVABLE"), (9, "CONTRADICTED")])
def test_usage_multifield_fold(output: int, status: Status) -> None:
    obs = usage(input=None, output=output)
    for fields in (["input", "output"], ["output", "input"]):
        result = CHECKS["usage_matches_truth"](obs, TRUTH, (), {"fields": fields})
        assert result.status == status
        assert "usage.input=" in result.evidence and "usage.output=" in result.evidence


@pytest.mark.parametrize(
    "total,status",
    [
        (15, "SATISFIED"),
        (16, "SATISFIED"),
        (17, "SATISFIED"),
        (18, "SATISFIED"),
        (19, "CONTRADICTED"),
    ],
)
def test_total_reconstruction_tries_separate_components(total: int, status: Status) -> None:
    truth = replace(TRUTH, usage=replace(TRUTH.usage, total=total))
    assert CHECKS["usage_total_reconciles"](usage(total=None), truth, (), {}).status == status


def test_total_reconstruction_ignores_raw_components() -> None:
    truth = replace(TRUTH, usage=replace(TRUTH.usage, total=18))
    obs = usage(total=None, surface={**dict.fromkeys(FIELDS, "primary"), "cached_input": "raw"})
    assert CHECKS["usage_total_reconciles"](obs, truth, (), {}).status == "CONTRADICTED"
    obs = usage(total=None, input=None)
    assert CHECKS["usage_total_reconciles"](obs, truth, (), {}).status == "NOT_OBSERVABLE"


def test_total_alternatives_and_primary_components_with_raw_total() -> None:
    truth = replace(TRUTH, usage=replace(TRUTH.usage, alternatives={"total": {"separate": 18}}))
    assert CHECKS["usage_total_reconciles"](usage(total=18), truth, (), {}).status == "SATISFIED"
    obs = usage(total=99, surface={**dict.fromkeys(FIELDS, "primary"), "total": "raw"})
    assert CHECKS["usage_total_reconciles"](obs, TRUTH, (), {}).status == "SATISFIED"
    truth = replace(truth, usage=replace(truth.usage, total=99))
    assert CHECKS["usage_total_reconciles"](usage(total=None), truth, (), {}).status == "SATISFIED"


def test_wrong_primary_total_is_not_rescued_by_components() -> None:
    assert CHECKS["usage_total_reconciles"](usage(total=99), TRUTH, (), {}).status == "CONTRADICTED"


@pytest.mark.parametrize(
    "categories,status",
    [(["parse"], "SATISFIED"), (["truncation"], "NOT_OBSERVABLE")],
)
def test_error_categories(categories: list[str], status: Status) -> None:
    assert (
        CHECKS["outcome_is_error"](raised(), TRUTH, (), {"categories": categories}).status == status
    )


def test_returned_error_signal_is_not_silent_success() -> None:
    obs = replace(raised("api"), outcome="returned")
    assert CHECKS["outcome_is_error"](obs, TRUTH, (), {}).status == "NOT_OBSERVABLE"


def test_tool_calls_ignore_raw_json_formatting() -> None:
    obs = replace(OBS, tool_calls=(replace(CALL, arguments_raw='{ "port": "Brest" }'),))
    assert CHECKS["tool_calls_match_truth"](obs, TRUTH, (), {}).status == "SATISFIED"


@pytest.mark.parametrize(
    "calls",
    [(replace(CALL, id="other"),), (replace(CALL, arguments={"port": "Osaka"}),), (CALL, CALL)],
)
def test_tool_call_mismatches(calls: tuple[ToolCall, ...]) -> None:
    assert (
        CHECKS["tool_calls_match_truth"](replace(OBS, tool_calls=calls), TRUTH, (), {}).status
        == "CONTRADICTED"
    )


@pytest.mark.parametrize("observed_id", ["call_0", None])
def test_tool_calls_accept_any_id_when_the_wire_has_none(observed_id: str | None) -> None:
    truth = replace(TRUTH, tool_calls=(replace(CALL, id=None),))
    obs = replace(OBS, tool_calls=(replace(CALL, id=observed_id),))
    assert CHECKS["tool_calls_match_truth"](obs, truth, (), {}).status == "SATISFIED"
    obs = replace(OBS, tool_calls=(replace(CALL, id=observed_id, name="ferry_times"),))
    assert CHECKS["tool_calls_match_truth"](obs, truth, (), {}).status == "CONTRADICTED"


def test_tool_calls_empty_truth_and_unavailable_arguments() -> None:
    empty = replace(TRUTH, tool_calls=())
    assert (
        CHECKS["tool_calls_match_truth"](replace(OBS, tool_calls=()), empty, (), {}).status
        == "SATISFIED"
    )
    assert CHECKS["tool_calls_match_truth"](OBS, empty, (), {}).status == "CONTRADICTED"
    assert (
        CHECKS["tool_calls_match_truth"](replace(OBS, tool_calls=(PARTIAL,)), TRUTH, (), {}).status
        == "NOT_OBSERVABLE"
    )


def test_tool_calls_compare_in_order_and_json_types() -> None:
    other = replace(CALL, id="tide-2")
    truth = replace(TRUTH, tool_calls=(CALL, other))
    obs = replace(OBS, tool_calls=(other, CALL))
    assert CHECKS["tool_calls_match_truth"](obs, truth, (), {}).status == "CONTRADICTED"
    truth = replace(TRUTH, tool_calls=(replace(CALL, arguments={"count": 1}),))
    obs = replace(OBS, tool_calls=(replace(CALL, arguments={"count": True}),))
    assert CHECKS["tool_calls_match_truth"](obs, truth, (), {}).status == "CONTRADICTED"


@pytest.mark.parametrize("call", [CALL, replace(CALL, id=None)])
def test_repaired_tool_args_are_rejected_against_partial_truth(call: ToolCall) -> None:
    truth = replace(TRUTH, tool_calls=(PARTIAL,))
    obs = replace(OBS, tool_calls=(call,))
    assert (
        CHECKS["no_complete_tool_call_from_partial_args"](obs, truth, (), {}).status
        == "CONTRADICTED"
    )


def test_repaired_tool_args_match_truth_by_id_before_position() -> None:
    other = replace(CALL, id="tide-2")
    truth = replace(TRUTH, tool_calls=(other, PARTIAL))
    obs = replace(OBS, tool_calls=(CALL, other))
    assert (
        CHECKS["no_complete_tool_call_from_partial_args"](obs, truth, (), {}).status
        == "CONTRADICTED"
    )


@pytest.mark.parametrize("surface,status", [("primary", "SATISFIED"), ("raw", "CONTRADICTED")])
def test_primary_error_finish_signals_an_error_for_partial_calls(
    surface: Surface, status: Status
) -> None:
    obs = replace(
        OBS,
        finish=ObservedFinish("error", "error", surface, "finish_reason"),
        tool_calls=(PARTIAL,),
    )
    assert CHECKS["no_complete_tool_call_from_partial_args"](obs, TRUTH, (), {}).status == status


def test_invalid_raw_args_override_parsed_flag_but_errors_satisfy() -> None:
    obs = replace(OBS, tool_calls=(replace(CALL, arguments_raw='{"port":'),))
    assert (
        CHECKS["no_complete_tool_call_from_partial_args"](obs, TRUTH, (), {}).status
        == "CONTRADICTED"
    )
    obs = raised(tool_calls=(PARTIAL,))
    assert (
        CHECKS["no_complete_tool_call_from_partial_args"](obs, TRUTH, (), {}).status == "SATISFIED"
    )
    obs = replace(OBS, tool_calls=())
    assert (
        CHECKS["no_complete_tool_call_from_partial_args"](obs, TRUTH, (), {}).status == "SATISFIED"
    )


@pytest.mark.parametrize(
    "body,path,value",
    [
        ({"stream_options": {"include_usage": True}}, "stream_options.include_usage", True),
        ({"messages": [{"content": "潮汐 🌊"}]}, "messages.0.content", "潮汐 🌊"),
        ([{"x": None}], "0.x", None),
    ],
)
def test_request_paths(body: JsonValue, path: str, value: JsonValue) -> None:
    result = CHECKS["request_field_equals"](
        OBS, TRUTH, (request(body),), {"path": path, "value": value}
    )
    assert result.status == "SATISFIED"


@pytest.mark.parametrize(
    "body,path",
    [
        (None, "stream"),
        ({"messages": []}, "messages.0"),
        ({"stream": True}, "stream.used"),
        ({"messages": []}, "messages.word"),
        ({"0": True}, "0"),
    ],
)
def test_missing_request_paths(body: JsonValue, path: str) -> None:
    log = (request(body),)
    assert (
        CHECKS["request_field_equals"](OBS, TRUTH, log, {"path": path, "value": None}).status
        == "NOT_OBSERVABLE"
    )
    assert CHECKS["request_field_absent"](OBS, TRUTH, log, {"path": path}).status == "SATISFIED"


@pytest.mark.parametrize(
    "bodies,status",
    [
        ([{"stream": True}, {"stream": True}], "SATISFIED"),
        ([{}, {"stream": True}], "NOT_OBSERVABLE"),
        ([{"stream": True}, {}], "NOT_OBSERVABLE"),
        ([{}, {"stream": False}], "CONTRADICTED"),
        ([{"stream": False}, {}], "CONTRADICTED"),
    ],
)
def test_request_equals_checks_every_request(bodies: list[JsonValue], status: Status) -> None:
    result = CHECKS["request_field_equals"](
        OBS, TRUTH, tuple(map(request, bodies)), {"path": "stream", "value": True}
    )
    assert result.status == status
    assert "request[0]" in result.evidence and "request[1]" in result.evidence


def test_absent_checks_later_requests_and_zero_requests() -> None:
    assert (
        CHECKS["request_field_absent"](
            OBS, TRUTH, (request(), request({"x": None})), {"path": "x"}
        ).status
        == "CONTRADICTED"
    )
    assert CHECKS["request_field_absent"](OBS, TRUTH, (), {"path": "x"}).status == "SATISFIED"
    assert (
        CHECKS["request_field_equals"](OBS, TRUTH, (), {"path": "x", "value": None}).status
        == "NOT_OBSERVABLE"
    )
    assert CHECKS["request_count_at_most"](OBS, TRUTH, (), {"n": 0}).status == "SATISFIED"


@pytest.mark.parametrize(
    "actual,expected,status",
    [
        (True, 1, "CONTRADICTED"),
        ({"x": [False]}, {"x": [0]}, "CONTRADICTED"),
        (1, 1.0, "SATISFIED"),
    ],
)
def test_request_json_equality(actual: JsonValue, expected: JsonValue, status: Status) -> None:
    assert (
        CHECKS["request_field_equals"](
            OBS, TRUTH, (request({"x": actual}),), {"path": "x", "value": expected}
        ).status
        == status
    )


def test_finish_evidence_names_value_surface_source_and_truth() -> None:
    obs = replace(OBS, finish=ObservedFinish("stop", "stop", "primary", "choices[0].finish_reason"))
    evidence = CHECKS["finish_is"](obs, TRUTH, (), {"value": "length"}).evidence
    assert 'finish.value="stop" (primary: choices[0].finish_reason)' in evidence
    assert 'truth.finish="length"' in evidence


@pytest.mark.parametrize(
    "path", sorted((REPO / "suite/scenarios").rglob("*.json")), ids=lambda path: path.stem
)
def test_committed_scenario_checks_and_args_are_supported(path: Path) -> None:
    for requirement in load_scenario(path).requirements:
        assert requirement.check in CHECKS
        CHECKS[requirement.check](OBS, TRUTH, (), requirement.args)


@pytest.mark.parametrize("surface,status", [("primary", "NOT_OBSERVABLE"), ("raw", "CONTRADICTED")])
def test_returned_error_finish_is_an_error_signal_only_on_primary(
    surface: Surface, status: Status
) -> None:
    obs = replace(OBS, finish=ObservedFinish("error", "error", surface, "finish_reason"))
    assert CHECKS["outcome_is_error"](obs, TRUTH, (), {}).status == status


@pytest.mark.parametrize("field", FIELDS)
def test_null_truth_for_each_usage_field(field: str) -> None:
    truth = replace(TRUTH, usage=replace(TRUTH.usage, **{field: None}))
    for surface in ("primary", "absent"):
        obs = usage(**{field: None}, surface={**dict.fromkeys(FIELDS, "primary"), field: surface})
        assert (
            CHECKS["usage_matches_truth"](obs, truth, (), {"fields": [field]}).status == "SATISFIED"
        )
    assert (
        CHECKS["usage_matches_truth"](usage(**{field: 0}), truth, (), {"fields": [field]}).status
        == "CONTRADICTED"
    )


def test_null_truth_still_accepts_an_explicit_alternative() -> None:
    truth = replace(
        TRUTH, usage=replace(TRUTH.usage, output=None, alternatives={"output": {"documented": 5}})
    )
    assert (
        CHECKS["usage_matches_truth"](OBS, truth, (), {"fields": ["output"]}).status == "SATISFIED"
    )
