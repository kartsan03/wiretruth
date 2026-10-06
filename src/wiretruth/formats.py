"""wiretruth's JSON formats: bundled schemas, loaders and typed records.

Each loader reads one file, validates it against its schema (Draft 2020-12), runs the checks a
schema can't express, and only then builds frozen dataclasses. Lists become tuples and string maps
become read-only mappings. Free-form JSON values (output schemas, tool parameters, recorded bodies,
raw usage) keep their parsed form; callers must not change them.

Every problem in a file is reported, one per line: ``<file>: <JSON path>: <message>``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Literal, TypeAlias, cast, get_args

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from referencing import Registry, Resource

JsonValue: TypeAlias = "bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None"
JsonObject: TypeAlias = "dict[str, JsonValue]"

Kind = Literal[
    "fixture",
    "scenario",
    "call",
    "observation",
    "shim-manifest",
    "results",
    "upstream",
    "bug-classes",
]
KINDS: tuple[Kind, ...] = get_args(Kind)

Tier = Literal["recorded", "documented", "derived"]
Finish = Literal[
    "stop", "length", "tool_calls", "content_filter", "refusal", "error", "other", "none"
]
ErrorCategory = Literal[
    "rate_limit",
    "context_length",
    "truncation",
    "content_filter",
    "parse",
    "validation",
    "api",
    "network",
    "internal",
    "other",
]
Surface = Literal["primary", "raw", "absent"]
Language = Literal["python", "javascript"]
Level = Literal["MUST", "SHOULD"]
Priority = Literal["P0", "P1", "P2"]
Role = Literal["system", "user", "assistant"]
Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
Outcome = Literal["returned", "raised", "unsupported"]
BodyKind = Literal["json", "sse", "ndjson", "chunks_b64", "text"]
End = Literal["clean", "abort"]


# --- Problems -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    """One thing wrong with one file."""

    file: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}: {self.where}: {self.message}"


class FormatError(ValueError):
    """A file failed to parse or validate. ``problems`` lists everything wrong with it."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("\n".join(str(problem) for problem in self.problems))


# --- Reading JSON ---------------------------------------------------------------------------------


class _Rejected(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    obj: JsonObject = {}
    for key, value in pairs:
        if key in obj:
            raise _Rejected(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> JsonValue:
    raise _Rejected(f"{name} is not allowed")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise _Rejected(f"{text} is out of range")
    return value


def read_json(path: Path) -> JsonValue:
    """Read a JSON file strictly.

    UTF-8 only; no duplicate keys; no NaN, Infinity or numbers that overflow to them; no lone
    surrogates in \\u escapes. Parsers in other languages disagree on all of these.
    """
    try:
        text = path.read_bytes().decode("utf-8")
        doc = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        # Encoding fails on a lone surrogate, which only a \u escape can produce here.
        json.dumps(doc, ensure_ascii=False).encode("utf-8")
        return cast(JsonValue, doc)
    except OSError as error:
        message = f"cannot read the file: {error.strerror}"
    except UnicodeDecodeError as error:
        message = f"not valid UTF-8 (byte {error.start})"
    except UnicodeEncodeError:
        message = "invalid JSON: a \\u escape is a lone surrogate"
    except json.JSONDecodeError as error:
        message = f"invalid JSON: {error.msg} (line {error.lineno}, column {error.colno})"
    except RecursionError:
        message = "invalid JSON: nested too deeply"
    except ValueError as error:  # _Rejected, or an integer too long to convert
        message = f"invalid JSON: {error}"
    raise FormatError([Problem(str(path), "$", message)])


# --- Schemas --------------------------------------------------------------------------------------


def schema_names() -> list[str]:
    """File names of the bundled schemas."""
    folder = resources.files("wiretruth") / "schemas"
    return sorted(entry.name for entry in folder.iterdir() if entry.name.endswith(".schema.json"))


@cache
def load_schema(name: str) -> JsonObject:
    """A bundled schema by file name, e.g. ``fixture.schema.json``."""
    text = (resources.files("wiretruth") / "schemas" / name).read_text(encoding="utf-8")
    return cast(JsonObject, json.loads(text))


@cache
def _registry() -> Registry[bool | Mapping[str, object]]:
    # $ref resolves only between the bundled schemas: the registry has no retrieval function.
    schemas = [load_schema(name) for name in schema_names()]
    return Registry().with_resources(
        (cast(str, schema["$id"]), Resource.from_contents(schema)) for schema in schemas
    )


@cache
def _end_anchored(pattern: str) -> re.Pattern[str]:
    # Schema patterns are ECMA-262 regexes, where `$` matches only at the end of the input.
    # Python's `$` also matches before a final newline, so "abc\n" would pass "^abc$".
    # (A `$` inside a character class would break this; the bundled schemas have none.)
    return re.compile(re.sub(r"(?<!\\)\$", r"\\Z", pattern))


def _pattern(
    validator: Validator, pattern: str, instance: object, schema: Mapping[str, object]
) -> Iterator[ValidationError]:
    if isinstance(instance, str) and not _end_anchored(pattern).search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


# The stubs leave extend() untyped; it returns a validator class like Draft202012Validator.
_Validator = cast(
    "type[Validator]",
    validators.extend(Draft202012Validator, {"pattern": _pattern}),  # type: ignore[no-untyped-call]
)


@cache
def _validator(kind: Kind) -> Validator:
    return _Validator(load_schema(f"{kind}.schema.json"), registry=_registry())


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_MESSAGE = 300


def json_path(parts: Iterable[str | int]) -> str:
    """Render a location as a JSON path: ``$.requirements[0].level``, ``$.headers["x-key"]``."""
    where = "$"
    for part in parts:
        if isinstance(part, int):
            where += f"[{part}]"
        elif _NAME.fullmatch(part):
            where += f".{part}"
        else:
            where += f"[{json.dumps(part, ensure_ascii=False)}]"
    return where


def _sort_key(parts: Iterable[str | int]) -> tuple[tuple[int, int, str], ...]:
    return tuple((0, part, "") if isinstance(part, int) else (1, 0, part) for part in parts)


def _explain(error: ValidationError) -> list[ValidationError]:
    """For an anyOf failure, the errors of the one branch whose type fits the value.

    "Not valid under any of the given schemas" doesn't say what to fix. When exactly one branch
    accepts the kind of value given (object, string, null, ...), its errors do.
    """
    branches: dict[str | int, list[ValidationError]] = {}
    for sub in error.context or ():
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    fitting = [
        errors
        for errors in branches.values()
        if not any(sub.validator == "type" and not sub.relative_path for sub in errors)
    ]
    if len(fitting) != 1:
        return [error]
    return [found for sub in fitting[0] for found in _explain(sub)]


def _schema_problems(kind: Kind, doc: JsonValue, file: str) -> list[Problem]:
    found: list[tuple[tuple[tuple[int, int, str], ...], Problem]] = []
    for top in _validator(kind).iter_errors(doc):
        for error in _explain(top):
            message = error.message
            if len(message) > _MAX_MESSAGE:
                message = message[: _MAX_MESSAGE - 1] + "…"
            problem = Problem(file, json_path(error.absolute_path), message)
            found.append((_sort_key(error.absolute_path), problem))
    found.sort(key=lambda item: item[0])
    return list(dict.fromkeys(problem for _, problem in found))


# --- Checks a schema can't express ----------------------------------------------------------------


Findings: TypeAlias = Iterator[tuple[str, str]]


def _check_fixture(doc: JsonObject) -> Findings:
    fixture_id = _str(doc, "id")
    expected = f"{_str(doc, 'provider')}/{_str(doc, 'scenario')}"
    if fixture_id.split("@", 1)[0] != expected:
        yield "$.id", f"{fixture_id!r} must be {expected!r} or {expected + '@<variant>'!r}"


def _check_scenario(doc: JsonObject) -> Findings:
    providers = set(_strings(doc, "providers"))
    seen: set[str] = set()
    for i, requirement in enumerate(_objects(doc, "requirements")):
        requirement_id = _str(requirement, "id")
        if requirement_id in seen:
            yield f"$.requirements[{i}].id", f"duplicate requirement id {requirement_id!r}"
        seen.add(requirement_id)
        for j, provider in enumerate(_strings(requirement, "providers")):
            if provider not in providers:
                yield f"$.requirements[{i}].providers[{j}]", f"{provider!r} is not in $.providers"
    for provider in _object(doc, "call_overrides"):
        if provider not in providers:
            yield json_path(["call_overrides", provider]), f"{provider!r} is not in $.providers"


def _check_shim_manifest(doc: JsonObject) -> Findings:
    primary = _str(doc, "primary_package")
    if primary not in _object(doc, "packages"):
        yield "$.primary_package", f"{primary!r} is not in $.packages"
    # Patterns run in the shim's language, and only Python's regex syntax can be checked here.
    if doc["language"] != "python":
        return
    for i, entry in enumerate(_objects(doc, "error_map")):
        pattern = _opt_str(entry, "message_regex")
        if pattern is None:
            continue
        try:
            re.compile(pattern)
        except re.error as error:
            yield f"$.error_map[{i}].message_regex", f"invalid regular expression: {error}"


def _check_bug_classes(doc: JsonValue) -> Findings:
    seen: set[str] = set()
    for i, entry in enumerate(cast("list[JsonObject]", doc)):
        class_id = _str(entry, "id")
        if class_id in seen:
            yield f"$[{i}].id", f"duplicate bug class id {class_id!r}"
        seen.add(class_id)


_CHECKS: dict[Kind, Callable[[JsonValue], Findings]] = {
    "fixture": lambda doc: _check_fixture(cast(JsonObject, doc)),
    "scenario": lambda doc: _check_scenario(cast(JsonObject, doc)),
    "shim-manifest": lambda doc: _check_shim_manifest(cast(JsonObject, doc)),
    "bug-classes": _check_bug_classes,
}


def validate(kind: Kind, doc: JsonValue, file: str) -> list[Problem]:
    """Every problem with a parsed document. The extra checks run once the schema passes."""
    problems = _schema_problems(kind, doc, file)
    if not problems and kind in _CHECKS:
        problems = [Problem(file, where, message) for where, message in _CHECKS[kind](doc)]
    return problems


def load_document(kind: Kind, path: Path | str) -> JsonValue:
    """Read and validate one file. Raises FormatError listing every problem found."""
    path = Path(path)
    doc = read_json(path)
    problems = validate(kind, doc, str(path))
    if problems:
        raise FormatError(problems)
    return doc


# --- Accessors for validated documents ------------------------------------------------------------


def _str(obj: JsonObject, key: str) -> str:
    return cast(str, obj[key])


def _opt_str(obj: JsonObject, key: str) -> str | None:
    return cast("str | None", obj.get(key))


def _opt_int(obj: JsonObject, key: str) -> int | None:
    return cast("int | None", obj.get(key))


def _bool(obj: JsonObject, key: str) -> bool:
    return cast(bool, obj[key])


def _object(obj: JsonObject, key: str) -> JsonObject:
    return cast(JsonObject, obj.get(key) or {})


def _opt_object(obj: JsonObject, key: str) -> JsonObject | None:
    return cast("JsonObject | None", obj.get(key))


def _objects(obj: JsonObject, key: str) -> list[JsonObject]:
    return cast("list[JsonObject]", obj.get(key) or [])


def _strings(obj: JsonObject, key: str) -> tuple[str, ...]:
    return tuple(cast("list[str]", obj.get(key) or []))


def _string_map(obj: JsonObject, key: str) -> Mapping[str, str]:
    return MappingProxyType(dict(cast("dict[str, str]", obj.get(key) or {})))


# --- Call -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: JsonObject


@dataclass(frozen=True)
class ForcedTool:
    """``tool_choice: {"name": ...}``: the model must call this tool."""

    name: str


@dataclass(frozen=True)
class Reasoning:
    effort: Effort


ToolChoice: TypeAlias = "Literal['auto', 'required', 'none'] | ForcedTool | None"


@dataclass(frozen=True)
class Call:
    """A provider-neutral call. Fields a scenario leaves out are None or empty."""

    stream: bool
    messages: tuple[Message, ...]
    max_output_tokens: int | None
    stop: tuple[str, ...] | None
    tools: tuple[Tool, ...]
    tool_choice: ToolChoice
    output_schema: JsonObject | None
    reasoning: Reasoning | None
    extra: JsonObject


@dataclass(frozen=True)
class CallSpec:
    """What the runner sends to a shim: a call for one scenario, provider and model."""

    scenario: str
    provider: str
    model: str
    call: Call


def _build_call(obj: JsonObject) -> Call:
    stop = obj.get("stop")
    choice = obj.get("tool_choice")
    reasoning = _opt_object(obj, "reasoning")
    return Call(
        stream=_bool(obj, "stream"),
        messages=tuple(
            Message(cast(Role, message["role"]), _str(message, "content"))
            for message in _objects(obj, "messages")
        ),
        max_output_tokens=_opt_int(obj, "max_output_tokens"),
        stop=None if stop is None else tuple(cast("list[str]", stop)),
        tools=tuple(
            Tool(_str(tool, "name"), _str(tool, "description"), _object(tool, "parameters"))
            for tool in _objects(obj, "tools")
        ),
        tool_choice=(
            ForcedTool(_str(choice, "name"))
            if isinstance(choice, dict)
            else cast("Literal['auto', 'required', 'none'] | None", choice)
        ),
        output_schema=_opt_object(obj, "output_schema"),
        reasoning=None if reasoning is None else Reasoning(cast(Effort, reasoning["effort"])),
        extra=_object(obj, "extra"),
    )


def load_call_spec(path: Path | str) -> CallSpec:
    doc = cast(JsonObject, load_document("call", path))
    return CallSpec(
        scenario=_str(doc, "scenario"),
        provider=_str(doc, "provider"),
        model=_str(doc, "model"),
        call=_build_call(doc),
    )


# --- Scenario -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    id: str
    level: Level
    check: str
    text: str
    args: JsonObject
    providers: tuple[str, ...] | None  # None: every provider of the scenario


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    bug_class: str
    priority: Priority
    summary: str
    call: Call
    providers: tuple[str, ...]
    requirements: tuple[Requirement, ...]
    call_overrides: Mapping[str, Call]  # the full call, override applied, per overridden provider
    grading_notes: str | None
    references: tuple[str, ...]

    def call_for(self, provider: str) -> Call:
        return self.call_overrides.get(provider, self.call)


def load_scenario(path: Path | str) -> Scenario:
    doc = cast(JsonObject, load_document("scenario", path))
    call = _object(doc, "call")
    return Scenario(
        id=_str(doc, "id"),
        title=_str(doc, "title"),
        bug_class=_str(doc, "bug_class"),
        priority=cast(Priority, doc["priority"]),
        summary=_str(doc, "summary"),
        call=_build_call(call),
        providers=_strings(doc, "providers"),
        requirements=tuple(
            Requirement(
                id=_str(requirement, "id"),
                level=cast(Level, requirement["level"]),
                check=_str(requirement, "check"),
                text=_str(requirement, "text"),
                args=_object(requirement, "args"),
                providers=_strings(requirement, "providers") or None,
            )
            for requirement in _objects(doc, "requirements")
        ),
        call_overrides=MappingProxyType(
            {
                provider: _build_call({**call, **cast(JsonObject, override)})
                for provider, override in _object(doc, "call_overrides").items()
            }
        ),
        grading_notes=_opt_str(doc, "grading_notes"),
        references=_strings(doc, "references"),
    )


# --- Fixture --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """A recorded response body.

    ``data`` holds the value of a ``json`` or ``text`` body. ``parts`` holds the events, lines or
    base64 chunks of an ``sse``, ``ndjson`` or ``chunks_b64`` body.
    """

    kind: BodyKind
    data: JsonValue
    parts: tuple[str, ...]


@dataclass(frozen=True)
class Exchange:
    method: str
    path: str
    status: int
    headers: Mapping[str, str]
    body: Body
    end: End


@dataclass(frozen=True)
class Provenance:
    recorded_at: str | None
    requested_model: str | None
    returned_model: str | None
    recorder: str | None
    source_commit: str | None
    ci_run: str | None
    raw_sha256: str | None
    request: JsonObject | None
    source_url: str | None
    notes: str


@dataclass(frozen=True)
class DerivedFrom:
    fixture: str
    transform: str
    args: JsonObject
    rationale: str
    evidence_url: str


@dataclass(frozen=True)
class ToolCall:
    id: str | None
    name: str
    arguments_raw: str | None
    arguments_parsed_ok: bool
    arguments: JsonValue


@dataclass(frozen=True)
class TruthError:
    message: str
    status: int | None
    type: str | None
    code: str | None


@dataclass(frozen=True)
class Usage:
    input: int | None
    cached_input: int | None
    output: int | None
    reasoning: int | None
    total: int | None
    alternatives: Mapping[str, Mapping[str, int]]
    raw: JsonObject | None


@dataclass(frozen=True)
class Truth:
    finish: Finish
    raw_finish: str | None
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    structured: JsonValue
    error: TruthError | None
    stream_completed: bool | None
    usage: Usage


@dataclass(frozen=True)
class Fixture:
    id: str
    scenario: str
    provider: str
    tier: Tier
    provenance: Provenance
    derived_from: DerivedFrom | None
    exchanges: tuple[Exchange, ...]
    truth: Truth


def _build_tool_call(obj: JsonObject) -> ToolCall:
    return ToolCall(
        id=_opt_str(obj, "id"),
        name=_str(obj, "name"),
        arguments_raw=_opt_str(obj, "arguments_raw"),
        arguments_parsed_ok=_bool(obj, "arguments_parsed_ok"),
        arguments=obj["arguments"],
    )


def _build_exchange(obj: JsonObject) -> Exchange:
    match = _object(obj, "match")
    response = _object(obj, "response")
    ((kind, value),) = _object(response, "body").items()
    is_list = kind in ("sse", "ndjson", "chunks_b64")
    return Exchange(
        method=_str(match, "method"),
        path=_str(match, "path"),
        status=cast(int, response["status"]),
        headers=_string_map(response, "headers"),
        body=Body(
            kind=cast(BodyKind, kind),
            data=None if is_list else value,
            parts=tuple(cast("list[str]", value)) if is_list else (),
        ),
        end=cast(End, response.get("end", "clean")),
    )


def _build_truth(obj: JsonObject) -> Truth:
    error = _opt_object(obj, "error")
    usage = _object(obj, "usage")
    return Truth(
        finish=cast(Finish, obj["finish"]),
        raw_finish=_opt_str(obj, "raw_finish"),
        text=_opt_str(obj, "text"),
        tool_calls=tuple(_build_tool_call(call) for call in _objects(obj, "tool_calls")),
        structured=obj["structured"],
        error=None
        if error is None
        else TruthError(
            message=_str(error, "message"),
            status=_opt_int(error, "status"),
            type=_opt_str(error, "type"),
            code=_opt_str(error, "code"),
        ),
        stream_completed=cast("bool | None", obj["stream_completed"]),
        usage=Usage(
            input=_opt_int(usage, "input"),
            cached_input=_opt_int(usage, "cached_input"),
            output=_opt_int(usage, "output"),
            reasoning=_opt_int(usage, "reasoning"),
            total=_opt_int(usage, "total"),
            alternatives=MappingProxyType(
                {
                    field: MappingProxyType(dict(cast("dict[str, int]", readings)))
                    for field, readings in _object(usage, "alternatives").items()
                }
            ),
            raw=_opt_object(usage, "raw"),
        ),
    )


def load_fixture(path: Path | str) -> Fixture:
    doc = cast(JsonObject, load_document("fixture", path))
    provenance = _object(doc, "provenance")
    derived = _opt_object(doc, "derived_from")
    return Fixture(
        id=_str(doc, "id"),
        scenario=_str(doc, "scenario"),
        provider=_str(doc, "provider"),
        tier=cast(Tier, doc["tier"]),
        provenance=Provenance(
            recorded_at=_opt_str(provenance, "recorded_at"),
            requested_model=_opt_str(provenance, "requested_model"),
            returned_model=_opt_str(provenance, "returned_model"),
            recorder=_opt_str(provenance, "recorder"),
            source_commit=_opt_str(provenance, "source_commit"),
            ci_run=_opt_str(provenance, "ci_run"),
            raw_sha256=_opt_str(provenance, "raw_sha256"),
            request=_opt_object(provenance, "request"),
            source_url=_opt_str(provenance, "source_url"),
            notes=_opt_str(provenance, "notes") or "",
        ),
        derived_from=None
        if derived is None
        else DerivedFrom(
            fixture=_str(derived, "fixture"),
            transform=_str(derived, "transform"),
            args=_object(derived, "args"),
            rationale=_str(derived, "rationale"),
            evidence_url=_str(derived, "evidence_url"),
        ),
        exchanges=tuple(_build_exchange(exchange) for exchange in _objects(doc, "exchanges")),
        truth=_build_truth(_object(doc, "truth")),
    )


# --- Observation ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LibraryInfo:
    name: str
    version: str
    language: Language
    api: str


@dataclass(frozen=True)
class ObservedError:
    type: str
    message: str
    category: ErrorCategory
    retry_after: float | None
    stack_top: str | None


@dataclass(frozen=True)
class ObservedFinish:
    value: Finish | None
    raw: str | None
    surface: Surface
    source: str | None


@dataclass(frozen=True)
class ObservedText:
    value: str | None
    surface: Surface


@dataclass(frozen=True)
class StructuredResult:
    attempted: bool
    returned_as_success: bool
    value: JsonValue


@dataclass(frozen=True)
class ObservedUsage:
    input: int | None
    cached_input: int | None
    output: int | None
    reasoning: int | None
    total: int | None
    surface: Mapping[str, Surface]


@dataclass(frozen=True)
class StreamInfo:
    used: bool
    completed: bool | None
    chunks_seen: int | None


@dataclass(frozen=True)
class Observation:
    library: LibraryInfo
    outcome: Outcome
    unsupported_reason: str | None
    error: ObservedError | None
    finish: ObservedFinish | None
    text: ObservedText | None
    structured: StructuredResult
    tool_calls: tuple[ToolCall, ...]
    usage: ObservedUsage | None
    stream: StreamInfo
    notes: tuple[str, ...]
    elapsed_ms: float


def _opt_float(obj: JsonObject, key: str) -> float | None:
    value = cast("float | None", obj.get(key))
    return None if value is None else float(value)


def load_observation(path: Path | str) -> Observation:
    doc = cast(JsonObject, load_document("observation", path))
    library = _object(doc, "library")
    error = _opt_object(doc, "error")
    finish = _opt_object(doc, "finish")
    text = _opt_object(doc, "text")
    structured = _object(doc, "structured")
    usage = _opt_object(doc, "usage")
    stream = _object(doc, "stream")
    return Observation(
        library=LibraryInfo(
            name=_str(library, "name"),
            version=_str(library, "version"),
            language=cast(Language, library["language"]),
            api=_str(library, "api"),
        ),
        outcome=cast(Outcome, doc["outcome"]),
        unsupported_reason=_opt_str(doc, "unsupported_reason"),
        error=None
        if error is None
        else ObservedError(
            type=_str(error, "type"),
            message=_str(error, "message"),
            category=cast(ErrorCategory, error["category"]),
            retry_after=_opt_float(error, "retry_after"),
            stack_top=_opt_str(error, "stack_top"),
        ),
        finish=None
        if finish is None
        else ObservedFinish(
            value=cast("Finish | None", finish["value"]),
            raw=_opt_str(finish, "raw"),
            surface=cast(Surface, finish["surface"]),
            source=_opt_str(finish, "source"),
        ),
        text=None
        if text is None
        else ObservedText(value=_opt_str(text, "value"), surface=cast(Surface, text["surface"])),
        structured=StructuredResult(
            attempted=_bool(structured, "attempted"),
            returned_as_success=_bool(structured, "returned_as_success"),
            value=structured["value"],
        ),
        tool_calls=tuple(_build_tool_call(call) for call in _objects(doc, "tool_calls")),
        usage=None
        if usage is None
        else ObservedUsage(
            input=_opt_int(usage, "input"),
            cached_input=_opt_int(usage, "cached_input"),
            output=_opt_int(usage, "output"),
            reasoning=_opt_int(usage, "reasoning"),
            total=_opt_int(usage, "total"),
            surface=MappingProxyType(dict(cast("dict[str, Surface]", usage["surface"]))),
        ),
        stream=StreamInfo(
            used=_bool(stream, "used"),
            completed=cast("bool | None", stream["completed"]),
            chunks_seen=_opt_int(stream, "chunks_seen"),
        ),
        notes=_strings(doc, "notes"),
        elapsed_ms=float(cast(float, doc["elapsed_ms"])),
    )


# --- Shim manifest --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorMapping:
    type: str
    category: ErrorCategory
    message_regex: str | None
    source: str | None
    note: str | None


@dataclass(frozen=True)
class UnsupportedEntry:
    scenario: str
    providers: tuple[str, ...] | None  # None: every provider
    reason: str
    docs: str


@dataclass(frozen=True)
class ShimManifest:
    name: str
    language: Language
    packages: Mapping[str, str]
    primary_package: str
    entry: tuple[str, ...]
    env: Mapping[str, str]
    providers: Mapping[str, str]
    surfaces: Mapping[str, Mapping[str, str]]
    finish_map: Mapping[str, Finish]
    error_map: tuple[ErrorMapping, ...]
    unsupported: tuple[UnsupportedEntry, ...]
    config_notes: tuple[str, ...]
    docs: tuple[str, ...]


def load_shim_manifest(path: Path | str) -> ShimManifest:
    doc = cast(JsonObject, load_document("shim-manifest", path))
    return ShimManifest(
        name=_str(doc, "name"),
        language=cast(Language, doc["language"]),
        packages=_string_map(doc, "packages"),
        primary_package=_str(doc, "primary_package"),
        entry=_strings(doc, "entry"),
        env=_string_map(doc, "env"),
        providers=_string_map(doc, "providers"),
        surfaces=MappingProxyType(
            {
                fact: MappingProxyType(dict(cast("dict[str, str]", where)))
                for fact, where in _object(doc, "surfaces").items()
            }
        ),
        finish_map=MappingProxyType(dict(cast("dict[str, Finish]", doc["finish_map"]))),
        error_map=tuple(
            ErrorMapping(
                type=_str(entry, "type"),
                category=cast(ErrorCategory, entry["category"]),
                message_regex=_opt_str(entry, "message_regex"),
                source=_opt_str(entry, "source"),
                note=_opt_str(entry, "note"),
            )
            for entry in _objects(doc, "error_map")
        ),
        unsupported=tuple(
            UnsupportedEntry(
                scenario=_str(entry, "scenario"),
                providers=_strings(entry, "providers") or None,
                reason=_str(entry, "reason"),
                docs=_str(entry, "docs"),
            )
            for entry in _objects(doc, "unsupported")
        ),
        config_notes=_strings(doc, "config_notes"),
        docs=_strings(doc, "docs"),
    )


# --- Bug classes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class BugClass:
    id: str
    title: str
    description: str
    effect: str
    references: tuple[str, ...]


def load_bug_classes(path: Path | str) -> tuple[BugClass, ...]:
    doc = cast("list[JsonObject]", load_document("bug-classes", path))
    return tuple(
        BugClass(
            id=_str(entry, "id"),
            title=_str(entry, "title"),
            description=_str(entry, "description"),
            effect=_str(entry, "effect"),
            references=_strings(entry, "references"),
        )
        for entry in doc
    )
