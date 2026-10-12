import json
import runpy
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wiretruth import __version__
from wiretruth.formats import (
    call_spec_document,
    load_shim_manifest,
    observation_document,
    observation_from_document,
    parse_json,
)
from wiretruth.grading import grade_cell
from wiretruth.providers import PROVIDERS, openai_chat
from wiretruth.recorder import Response
from wiretruth.replay import LoggedRequest
from wiretruth.runner import (
    CellResult,
    compare_grades,
    find_cells,
    load_expected_grades,
    process_to_run,
)

REPO = Path(__file__).resolve().parents[2]
REFERENCE = runpy.run_path(str(REPO / "shims" / "reference" / "shim.py"))
LIAR = runpy.run_path(str(REPO / "shims" / "liar" / "shim.py"))


@pytest.mark.parametrize("name", ["reference", "liar"])
def test_builtin_manifests_load_and_pin_the_package_version(name):
    manifest = load_shim_manifest(REPO / "shims" / name / "wiretruth-shim.json")
    assert manifest.name == name
    assert manifest.packages == {"wiretruth": __version__}
    assert manifest.primary_package == "wiretruth"
    assert manifest.entry == ("python", "shim.py")
    assert set(manifest.providers) == set(PROVIDERS)
    assert manifest.error_map == manifest.unsupported == manifest.config_notes == ()


@pytest.mark.parametrize("name", ["reference", "liar"])
def test_builtin_observations_match_hand_derived_expected_grades_without_sockets(name):
    results = []
    for cell in find_cells(REPO):
        spec = cell.call_spec
        provider = PROVIDERS[cell.provider]
        request = provider.build_request(spec.call, spec.model)
        truth = provider.extract_truth(cell.fixture.exchanges, request.body)
        doc = REFERENCE["observation_from_truth"](spec, truth)
        if name == "liar":
            doc = LIAR["lie"](doc)
        log = [
            LoggedRequest("POST", "/v1/chat/completions", {}, {}, request.body, None, "", 0, True)
        ]
        run = process_to_run(
            timed_out=False,
            returncode=0,
            stdout=json.dumps(doc).encode(),
            request_log=log,
            stream=spec.call.stream,
            provider=cell.provider,
        )
        assert run.invalid is run.mismatch is None
        assert observation_from_document(observation_document(run.observation)) == run.observation
        grade = grade_cell(
            run,
            scenario=cell.scenario,
            provider=cell.provider,
            truth=cell.fixture.truth,
            unsupported=(),
        )
        results.append(
            CellResult(
                cell.scenario.id,
                cell.provider,
                cell.fixture.id,
                cell.fixture.tier,
                grade,
                run.observation,
                1,
                grade.notes,
            )
        )
        if name == "reference":
            assert grade.grade == "PASS"
            assert all(item.status == "SATISFIED" for item in grade.requirements)
    expected = load_expected_grades(REPO / "tests" / "expected-grades" / f"{name}.json")
    assert compare_grades(results, expected) == ()


def sample_truth():
    return {
        "finish": "stop",
        "raw_finish": "stop",
        "text": '{"flavor":"café"}',
        "structured": {"flavor": "café"},
        "error": None,
        "stream_completed": None,
        "tool_calls": [
            {
                "id": "call-1",
                "name": "temperature",
                "arguments_raw": '{"c":18}',
                "arguments_parsed_ok": True,
                "arguments": {"c": 18},
            }
        ],
        "usage": {
            "input": 0,
            "cached_input": None,
            "output": 2,
            "reasoning": None,
            "total": 2,
            "alternatives": {},
            "raw": {"prompt_tokens": 0},
        },
    }


@pytest.mark.parametrize(
    "schema,finish,value,success",
    [
        ({}, "stop", {}, True),
        ({}, "stop", False, True),
        ({}, "stop", None, False),
        (None, "stop", {}, False),
        ({}, "length", {}, False),
    ],
)
def test_reference_structured_success_needs_schema_stop_and_value(schema, finish, value, success):
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    spec = replace(spec, call=replace(spec.call, output_schema=schema))
    truth = {**sample_truth(), "finish": finish, "structured": value}
    obs = observation_from_document(REFERENCE["observation_from_truth"](spec, truth))
    assert obs.structured.returned_as_success is success
    assert obs.structured.attempted is (schema is not None)
    assert obs.structured.value == (value if success else None)


def test_reference_primary_absent_and_raw_fields_and_liar_drops_raw_values():
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    truth = sample_truth()
    doc = REFERENCE["observation_from_truth"](spec, truth)
    saved = deepcopy(doc)
    obs = observation_from_document(doc)
    assert obs.finish.raw == "stop" and obs.finish.surface == "primary"
    assert obs.usage.input == 0 and obs.usage.surface["input"] == "primary"
    assert obs.usage.cached_input is None and obs.usage.surface["cached_input"] == "absent"
    assert obs.tool_calls[0].arguments_raw == '{"c":18}'
    assert obs.text.value == truth["text"] and obs.text.surface == "primary"
    lying = observation_from_document(LIAR["lie"](doc))
    assert doc == saved
    assert lying.finish.raw is None and lying.tool_calls[0].arguments_raw is None
    assert set(lying.usage.surface.values()) == {"absent"}
    assert all(getattr(lying.usage, name) is None for name in REFERENCE["USAGE_FIELDS"])
    absent = REFERENCE["observation_from_truth"](spec, {**truth, "text": None, "finish": None})
    assert observation_from_document(absent).text.surface == "absent"
    assert observation_from_document(absent).finish.surface == "absent"


def test_reference_raises_wire_errors_and_liar_swallows_them():
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    truth = {
        **sample_truth(),
        "finish": "error",
        "error": {
            "type": "provider.busy",
            "message": "Overloaded " * 80,
        },
    }
    doc = REFERENCE["observation_from_truth"](spec, truth)
    obs = observation_from_document(doc)
    assert obs.outcome == "raised" and obs.error.category == "api"
    assert len(obs.error.message) == 500
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.outcome == "returned" and liar.error is None
    assert liar.finish.value == "stop"


def test_liar_returns_partial_json_as_structured_success():
    spec = find_cells(REPO, "truncation.structured-output")[0].call_spec
    truth = {
        **sample_truth(),
        "finish": "length",
        "raw_finish": "length",
        "text": '{"flavor":"caf',
        "structured": None,
    }
    liar = observation_from_document(LIAR["lie"](REFERENCE["observation_from_truth"](spec, truth)))
    assert liar.finish.value == "stop"
    assert liar.structured.returned_as_success
    assert liar.structured.value == truth["text"]


def test_reference_reuses_recorder_request_response_and_exchange(monkeypatch):
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    raw = b'{"choices":[{"message":{"content":"Clouds"},"finish_reason":"length"}]}'
    response = Response(200, {"content-type": "application/json"}, [raw], "clean")
    send = MagicMock(return_value=response)
    observe = REFERENCE["observe"]
    monkeypatch.setitem(observe.__globals__, "send", send)
    monkeypatch.setenv("WIRETRUTH_PROVIDER", "openai-chat")
    monkeypatch.setenv("WIRETRUTH_BASE_URL_OPENAI_CHAT", "http://127.0.0.1:4321/v1")
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    obs = observation_from_document(observe(spec))
    request = openai_chat.build_request(spec.call, spec.model)
    send.assert_called_once_with(openai_chat, request, "http://127.0.0.1:4321/v1", "wt-test-key")
    assert obs.finish.value == "length" and obs.text.value == "Clouds"


@pytest.mark.parametrize("name", ["reference", "liar"])
@pytest.mark.parametrize("input_kind", ["invalid-call", "missing-environment"])
def test_unexpected_shim_failures_are_valid_observations_and_exit_zero(name, input_kind):
    stdin = b"invalid"
    if input_kind == "missing-environment":
        stdin = json.dumps(call_spec_document(find_cells(REPO)[0].call_spec)).encode()
    result = subprocess.run(
        [sys.executable, "shim.py"],
        cwd=REPO / "shims" / name,
        input=stdin,
        capture_output=True,
        env={},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0 and result.stderr == b""
    obs = observation_from_document(parse_json(result.stdout))
    assert obs.outcome == "raised" and obs.error.category == "internal"


def test_liar_repairs_partial_tool_arguments():
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    truth = sample_truth()
    truth["tool_calls"][0].update(arguments_raw='{"c":', arguments_parsed_ok=False, arguments=None)
    doc = REFERENCE["observation_from_truth"](spec, truth)
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.tool_calls[0].arguments_parsed_ok is True
    assert liar.tool_calls[0].arguments == {}
    assert doc["tool_calls"][0]["arguments_parsed_ok"] is False


def test_liar_drops_parallel_tool_calls():
    spec = find_cells(REPO, "truncation.text")[0].call_spec
    truth = sample_truth()
    truth["tool_calls"].append({**truth["tool_calls"][0], "id": "call-2"})
    doc = REFERENCE["observation_from_truth"](spec, truth)
    liar = observation_from_document(LIAR["lie"](doc))
    assert [call.id for call in liar.tool_calls] == ["call-1"]
    assert len(doc["tool_calls"]) == 2


@pytest.mark.parametrize("completed", [None, False, True])
@pytest.mark.parametrize("used", [False, True])
def test_liar_claims_every_used_stream_completed(used, completed):
    doc = REFERENCE["observation_from_truth"](
        find_cells(REPO, "truncation.text")[0].call_spec, sample_truth()
    )
    doc["stream"].update(used=used, completed=completed)
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.stream.completed is (True if used else completed)
    assert doc["stream"]["completed"] is completed


def test_liar_maps_unknown_finish_to_primary_stop():
    truth = {**sample_truth(), "finish": "other", "raw_finish": "wiretruth_unknown_reason"}
    doc = REFERENCE["observation_from_truth"](
        find_cells(REPO, "truncation.text")[0].call_spec, truth
    )
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.finish.value == "stop" and liar.finish.surface == "primary"
    assert liar.finish.raw is None
    assert doc["finish"]["value"] == "other"


@pytest.mark.parametrize(
    "text,expected",
    [
        (None, None),
        ("", ""),
        ("Hello", "Hello"),
        ("Café 東京 🌸", "Caf\ufffd 東京 🌸"),
        ("東京 🌸", "\ufffd京 🌸"),
        ("🌸🎉", "\ufffd🎉"),
    ],
)
def test_liar_replaces_only_the_first_non_ascii_character(text, expected):
    doc = REFERENCE["observation_from_truth"](
        find_cells(REPO, "truncation.text")[0].call_spec, {**sample_truth(), "text": text}
    )
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.text.value == expected
    assert LIAR["lie"](doc) == LIAR["lie"](doc)
    assert doc["text"]["value"] == text
