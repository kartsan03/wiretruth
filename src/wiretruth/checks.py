"""Pure requirement checks against an observation, wire truth and the replay request log.

Checks validate scenario arguments before looking at the observation. Only primary values count;
raw metadata cannot satisfy or contradict a value comparison. Independent signals (such as a
raised error) can still satisfy a check. Within a check, satisfaction precedes contradiction.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias, cast, get_args

from wiretruth.formats import (
    ErrorCategory,
    Finish,
    JsonObject,
    JsonValue,
    Observation,
    Surface,
    Truth,
)
from wiretruth.replay import LoggedRequest

Status = Literal["SATISFIED", "CONTRADICTED", "NOT_OBSERVABLE"]


@dataclass(frozen=True)
class CheckResult:
    status: Status
    evidence: str


Check: TypeAlias = Callable[[Observation, Truth, Sequence[LoggedRequest], JsonObject], CheckResult]
_USAGE_FIELDS = ("input", "cached_input", "output", "reasoning", "total")


def _keys(args: JsonObject, required: tuple[str, ...] = (), optional: tuple[str, ...] = ()) -> None:
    missing = set(required) - args.keys()
    unknown = args.keys() - set(required + optional)
    if missing or unknown:
        raise ValueError(
            f"invalid check args: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _finish_arg(args: JsonObject) -> Finish:
    _keys(args, ("value",))
    if args["value"] not in get_args(Finish):
        raise ValueError("value must be a canonical finish")
    return cast(Finish, args["value"])


def _list_arg(args: JsonObject, key: str, choices: Sequence[str]) -> list[str]:
    value = args[key]
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or item not in choices for item in value)
    ):
        raise ValueError(f"{key} must be a non-empty list of {', '.join(choices)}")
    return cast(list[str], value)


def _path_arg(args: JsonObject) -> str:
    value = args["path"]
    if not isinstance(value, str) or not all(value.split(".")):
        raise ValueError("path must be a non-empty dotted path without empty segments")
    return value


def _show(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 160 else text[:159] + "…"


def _finish_evidence(obs: Observation, truth: Truth) -> str:
    finish = obs.finish
    reported = (
        "finish absent"
        if finish is None
        else (
            f"finish.value={_show(finish.value)} ({finish.surface}"
            f"{': ' + finish.source if finish.source else ''})"
        )
    )
    return f"{reported}; truth.finish={_show(truth.finish)}"


def _primary_finish(obs: Observation) -> Finish | None:
    return obs.finish.value if obs.finish and obs.finish.surface == "primary" else None


def _fold(results: Sequence[CheckResult]) -> CheckResult:
    status: Status = "SATISFIED"
    if any(result.status == "CONTRADICTED" for result in results):
        status = "CONTRADICTED"
    elif any(result.status == "NOT_OBSERVABLE" for result in results):
        status = "NOT_OBSERVABLE"
    return CheckResult(status, "; ".join(result.evidence for result in results))


def finish_is(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Compare a primary finish, including one exposed on an error, with ``value``.

    Raw, absent and null finishes are NOT_OBSERVABLE, whatever the outcome.
    """
    expected = _finish_arg(args)
    value = _primary_finish(obs)
    evidence = f"{_finish_evidence(obs, truth)}; required finish={_show(expected)}"
    if value == expected:
        return CheckResult("SATISFIED", evidence)
    if value is not None:
        return CheckResult("CONTRADICTED", evidence)
    return CheckResult("NOT_OBSERVABLE", evidence)


def finish_is_not(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Only a primary finish equal to ``value``, without a raised outcome, contradicts.

    A raised outcome satisfies first. A raw-only or absent finish also satisfies: the library
    doesn't report the forbidden value.
    """
    forbidden = _finish_arg(args)
    evidence = (
        f"outcome={obs.outcome}; {_finish_evidence(obs, truth)}; "
        f"forbidden finish={_show(forbidden)}"
    )
    if obs.outcome != "raised" and _primary_finish(obs) == forbidden:
        return CheckResult("CONTRADICTED", evidence)
    return CheckResult("SATISFIED", evidence)


def truncation_is_signalled(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Primary length or a truncation error satisfies, even with structured success.

    Read obs.finish whatever the outcome: a documented finish on an error is primary, with
    source naming that error field. The manifest-declared path is already in the observation;
    this check does no manifest lookup. Raw/absent finishes do not count. Satisfaction is tested
    before contradiction; no_success_structured_value judges structured success separately.
    """
    _keys(args)
    value = _primary_finish(obs)
    category = obs.error.category if obs.error else None
    evidence = (
        f"{_finish_evidence(obs, truth)}; error.category={_show(category)}; "
        f"structured.returned_as_success={_show(obs.structured.returned_as_success)}"
    )
    if value == "length" or category == "truncation":
        return CheckResult("SATISFIED", evidence)
    if value in ("stop", "tool_calls") or obs.structured.returned_as_success:
        return CheckResult("CONTRADICTED", evidence)
    return CheckResult("NOT_OBSERVABLE", evidence)


def text_matches_truth(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Compare primary text exactly; null or empty truth is matched by reporting no text.

    Raw text stays NOT_OBSERVABLE. For non-empty truth, absent or null text is NOT_OBSERVABLE.
    """
    _keys(args)
    value = obs.text.value if obs.text else None
    surface = obs.text.surface if obs.text else "absent"
    evidence = f"text.value={_show(value)} ({surface}); truth.text={_show(truth.text)}"
    if surface == "raw":
        return CheckResult("NOT_OBSERVABLE", evidence)
    if truth.text in (None, ""):
        status: Status = (
            "SATISFIED" if surface == "absent" or value in (None, "") else "CONTRADICTED"
        )
        return CheckResult(status, evidence)
    if surface != "primary" or value is None:
        return CheckResult("NOT_OBSERVABLE", evidence)
    return CheckResult("SATISFIED" if value == truth.text else "CONTRADICTED", evidence)


def no_replacement_chars(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Look for U+FFFD in primary text; raw, absent or null text is NOT_OBSERVABLE."""
    _keys(args)
    value = obs.text.value if obs.text else None
    surface = obs.text.surface if obs.text else "absent"
    evidence = f"text.value={_show(value)} ({surface})"
    if surface != "primary" or value is None:
        return CheckResult("NOT_OBSERVABLE", evidence)
    count = value.count("\ufffd")
    return CheckResult(
        "CONTRADICTED" if count else "SATISFIED", f"{evidence}; U+FFFD count={count}"
    )


def no_success_structured_value(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """An attempted structured result must not be returned as success.

    If structured output was not attempted, the check is NOT_OBSERVABLE.
    """
    _keys(args)
    structured = obs.structured
    evidence = (
        f"structured.attempted={_show(structured.attempted)}; "
        f"structured.returned_as_success={_show(structured.returned_as_success)}"
    )
    if not structured.attempted:
        return CheckResult("NOT_OBSERVABLE", evidence)
    return CheckResult("CONTRADICTED" if structured.returned_as_success else "SATISFIED", evidence)


def _json_equal(left: JsonValue, right: JsonValue) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def tool_calls_match_truth(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Compare calls in order by count, id, name and parsed arguments, ignoring raw formatting.

    A null truth id (the provider sends none) accepts any id the library generated. Two empty
    lists satisfy. No calls against expected ones contradict on a returned outcome without an
    error, and are NOT_OBSERVABLE otherwise. Known mismatches contradict; otherwise unavailable
    parsed arguments are NOT_OBSERVABLE.
    """
    _keys(args)
    evidence = (
        f"outcome={obs.outcome}; tool_calls count={len(obs.tool_calls)}; "
        f"truth count={len(truth.tool_calls)}"
    )
    if not obs.tool_calls and truth.tool_calls and (obs.outcome != "returned" or obs.error):
        return CheckResult("NOT_OBSERVABLE", evidence)
    if len(obs.tool_calls) != len(truth.tool_calls):
        return CheckResult("CONTRADICTED", evidence)
    results = []
    for index, (call, expected) in enumerate(zip(obs.tool_calls, truth.tool_calls, strict=True)):
        detail = (
            f"tool_calls[{index}] id={_show(call.id)}, name={_show(call.name)}, "
            f"args={_show(call.arguments)}, parsed={_show(call.arguments_parsed_ok)}; "
            f"truth id={_show(expected.id)}, name={_show(expected.name)}, "
            f"args={_show(expected.arguments)}, parsed={_show(expected.arguments_parsed_ok)}"
        )
        status: Status = "SATISFIED"
        if (expected.id is not None and call.id != expected.id) or call.name != expected.name:
            status = "CONTRADICTED"
        elif not call.arguments_parsed_ok or not expected.arguments_parsed_ok:
            status = "NOT_OBSERVABLE"
        elif not _json_equal(call.arguments, expected.arguments):
            status = "CONTRADICTED"
        results.append(CheckResult(status, detail))
    return _fold(results)


def no_complete_tool_call_from_partial_args(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Reject parsed calls returned without an error when their truth arguments were partial.

    A raised outcome, an error object or a primary ``error`` finish satisfies first. Match truth
    by id, falling back to position when no id matches. Unparsed arguments are visibly partial
    and satisfy; truncation_is_signalled separately checks whether the cut was signalled.
    """
    _keys(args)
    if obs.outcome == "raised" or obs.error is not None or _primary_finish(obs) == "error":
        return CheckResult("SATISFIED", f"outcome={obs.outcome}; error signalled")
    for index, call in enumerate(obs.tool_calls):
        expected = next(
            (item for item in truth.tool_calls if item.id is not None and item.id == call.id),
            truth.tool_calls[index] if index < len(truth.tool_calls) else None,
        )
        if call.arguments_parsed_ok and expected is not None and not expected.arguments_parsed_ok:
            return CheckResult(
                "CONTRADICTED",
                f"tool_calls[{index}] id={_show(call.id)} returned without error; "
                f"arguments_parsed_ok={_show(call.arguments_parsed_ok)}, "
                "truth.arguments_parsed_ok="
                f"{_show(expected.arguments_parsed_ok if expected else None)}",
            )
    return CheckResult(
        "SATISFIED", f"{len(obs.tool_calls)} tool calls; no partial args made complete"
    )


def _usage_value(obs: Observation, field: str) -> tuple[int | None, Surface]:
    if obs.usage is None:
        return None, "absent"
    return cast("int | None", getattr(obs.usage, field)), obs.usage.surface.get(field, "absent")


def _usage_field(obs: Observation, truth: Truth, field: str) -> CheckResult:
    value, surface = _usage_value(obs, field)
    expected = cast("int | None", getattr(truth.usage, field))
    alternatives = tuple(truth.usage.alternatives.get(field, {}).values())
    evidence = f"usage.{field}={_show(value)} ({surface}); truth.usage.{field}={_show(expected)}"
    if alternatives:
        evidence += f"; alternatives={_show(alternatives)}"
    if surface == "raw":
        return CheckResult("NOT_OBSERVABLE", evidence)
    if expected is None and (surface == "absent" or value is None):
        return CheckResult(
            "SATISFIED", f"{evidence}; wire carried no {field}; library reported none"
        )
    if surface != "primary" or value is None:
        return CheckResult("NOT_OBSERVABLE", evidence)
    if value == expected or value in alternatives:
        return CheckResult("SATISFIED", evidence)
    if expected is None:
        evidence += f"; library reported {field}={value} but the wire carried no {field}"
    return CheckResult("CONTRADICTED", evidence)


def usage_matches_truth(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Compare primary fields with truth and every declared alternative for that field.

    Raw-only values are NOT_OBSERVABLE. Null truth accepts primary null or absence; a primary
    number (including zero) contradicts unless it is an alternative. Fold fields as CONTRADICTED
    before NOT_OBSERVABLE before SATISFIED, with evidence for each field.
    """
    _keys(args, ("fields",))
    fields = _list_arg(args, "fields", _USAGE_FIELDS)
    return _fold([_usage_field(obs, truth, field) for field in fields])


def usage_total_reconciles(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Compare primary total, or reconstruct it from primary input and output.

    A present primary total is authoritative; alternatives are accepted. With null truth.total,
    apply the same reports-none rule as usage_matches_truth, without reconstructing. Raw total
    alone is NOT_OBSERVABLE, but primary components can independently reconstruct a known total.
    Missing input/output is NOT_OBSERVABLE. Try input + output with either, both or neither of
    the separately reported primary cached_input and reasoning counts added.
    """
    _keys(args)
    total, surface = _usage_value(obs, "total")
    if truth.usage.total is None or (surface == "primary" and total is not None):
        return _usage_field(obs, truth, "total")
    values = {field: _usage_value(obs, field) for field in _USAGE_FIELDS[:-1]}
    evidence = f"usage.total={_show(total)} ({surface}); " + "; ".join(
        f"usage.{field}={_show(value)} ({where})" for field, (value, where) in values.items()
    )
    evidence += f"; truth.usage.total={truth.usage.total}"
    input_value, input_surface = values["input"]
    output_value, output_surface = values["output"]
    if (
        input_surface != "primary"
        or output_surface != "primary"
        or (input_value is None or output_value is None)
    ):
        return CheckResult("NOT_OBSERVABLE", evidence)
    totals = {input_value + output_value}
    for field in ("cached_input", "reasoning"):
        value, where = values[field]
        if where == "primary" and value is not None:
            totals |= {total + value for total in totals}
    accepted = {truth.usage.total, *truth.usage.alternatives.get("total", {}).values()}
    evidence += f"; reconstructed totals={sorted(totals)}; accepted totals={sorted(accepted)}"
    return CheckResult("SATISFIED" if totals & accepted else "CONTRADICTED", evidence)


def outcome_is_error(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """A raised outcome must have a listed category, if categories were requested.

    Raised with another category is NOT_OBSERVABLE: it failed loudly with the wrong cause.
    A returned outcome with an error object or primary error finish is also NOT_OBSERVABLE,
    rather than silent success.
    """
    _keys(args, optional=("categories",))
    categories = (
        _list_arg(args, "categories", get_args(ErrorCategory)) if "categories" in args else None
    )
    category = obs.error.category if obs.error else None
    evidence = (
        f"outcome={obs.outcome}; error.category={_show(category)}; {_finish_evidence(obs, truth)}"
    )
    if categories is not None:
        evidence += f"; required categories={_show(categories)}"
    if obs.outcome == "raised" and (categories is None or category in categories):
        return CheckResult("SATISFIED", evidence)
    if obs.outcome == "returned" and obs.error is None and _primary_finish(obs) != "error":
        return CheckResult("CONTRADICTED", evidence)
    return CheckResult("NOT_OBSERVABLE", evidence)


def stream_not_reported_complete(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """A raised outcome satisfies before a true completion signal can contradict."""
    _keys(args)
    evidence = (
        f"stream.completed={_show(obs.stream.completed)}; outcome={obs.outcome}; "
        f"truth.stream_completed={_show(truth.stream_completed)}"
    )
    if obs.stream.completed is False or obs.outcome == "raised":
        return CheckResult("SATISFIED", evidence)
    if obs.stream.completed is True:
        return CheckResult("CONTRADICTED", evidence)
    return CheckResult("NOT_OBSERVABLE", evidence)


def _at_path(body: JsonValue, path: str) -> tuple[bool, JsonValue]:
    for part in path.split("."):
        if part.isascii() and part.isdecimal():
            index = int(part)
            if not isinstance(body, list) or index >= len(body):
                return False, None
            body = body[index]
        elif isinstance(body, dict) and part in body:
            body = body[part]
        else:
            return False, None
    return True, body


def request_field_equals(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Every logged request must have the requested JSON value at a dotted path.

    Numeric segments index lists. No requests or non-JSON bodies are NOT_OBSERVABLE. A missing
    path in a JSON body or a different value contradicts. JSON booleans and numbers differ,
    including inside arrays and objects.
    """
    _keys(args, ("path", "value"))
    path = _path_arg(args)
    if not req_log:
        return CheckResult(
            "NOT_OBSERVABLE", f"no logged requests; required {path}={_show(args['value'])}"
        )
    results = []
    for index, request in enumerate(req_log):
        present, value = _at_path(request.json_body, path)
        status: Status = "NOT_OBSERVABLE"
        if present:
            status = "SATISFIED" if _json_equal(value, args["value"]) else "CONTRADICTED"
        elif request.json_body is not None:
            status = "CONTRADICTED"
        evidence = f"request[{index}].json_body.{path}={_show(value) if present else 'absent'}"
        results.append(CheckResult(status, f"{evidence}; required={_show(args['value'])}"))
    return _fold(results)


def request_field_absent(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """A path must be absent from every request; non-JSON bodies and no requests satisfy."""
    _keys(args, ("path",))
    path = _path_arg(args)
    for index, request in enumerate(req_log):
        present, value = _at_path(request.json_body, path)
        if present:
            return CheckResult("CONTRADICTED", f"request[{index}].json_body.{path}={_show(value)}")
    return CheckResult("SATISFIED", f"{path} absent from all {len(req_log)} logged requests")


def request_count_at_most(
    obs: Observation, truth: Truth, req_log: Sequence[LoggedRequest], args: JsonObject
) -> CheckResult:
    """Count every logged request, including retries and non-JSON bodies."""
    _keys(args, ("n",))
    n = args["n"]
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("n must be an integer >= 0")
    count = len(req_log)
    return CheckResult(
        "SATISFIED" if count <= n else "CONTRADICTED", f"request count={count}; maximum={n}"
    )


CHECKS: Mapping[str, Check] = MappingProxyType(
    {
        check.__name__: check
        for check in (
            finish_is,
            finish_is_not,
            truncation_is_signalled,
            text_matches_truth,
            no_replacement_chars,
            no_success_structured_value,
            tool_calls_match_truth,
            no_complete_tool_call_from_partial_args,
            usage_matches_truth,
            usage_total_reconciles,
            outcome_is_error,
            stream_not_reported_complete,
            request_field_equals,
            request_field_absent,
            request_count_at_most,
        )
    }
)
