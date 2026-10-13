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
from wiretruth.replay import BASE_PATHS, LoggedRequest
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
        path = BASE_PATHS[cell.provider] + request.path
        log = [LoggedRequest("POST", path, {}, {}, request.body, None, "", 0, True)]
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
    # Anthropic result snapshots are added by the coordinator after a socket-enabled run.
    # The loop above still checks every reference cell against all its requirements.
    compared = [
        cell
        for cell in results
        if cell.provider != "anthropic" or (cell.scenario, cell.provider) in expected
    ]
    assert compare_grades(compared, expected) == ()


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


@pytest.mark.parametrize(
    "provider_id,probe_name,kind",
    [("ollama", "n-text-stream", "ndjson"), ("ollama-openai", "o-text-stream-usage", "sse")],
)
def test_reference_counts_ollama_stream_lines_and_liar_applies(
    monkeypatch, provider_id, probe_name, kind
):
    from ollama_samples import PROBES

    spec = find_cells(REPO, "truncation.text-stream")[0].call_spec
    spec = replace(spec, provider=provider_id)
    probe = PROBES[probe_name]
    response = Response(
        200, {"content-type": probe["content_type"]}, [probe["body"].encode()], "clean"
    )
    observe = REFERENCE["observe"]
    monkeypatch.setitem(observe.__globals__, "send", MagicMock(return_value=response))
    monkeypatch.setenv("WIRETRUTH_PROVIDER", provider_id)
    monkeypatch.setenv(
        "WIRETRUTH_BASE_URL_" + provider_id.upper().replace("-", "_"), "http://127.0.0.1:4321"
    )
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    doc = observe(spec)
    obs = observation_from_document(doc)
    expected = len(probe["body"].splitlines()) if kind == "ndjson" else probe["body"].count("\n\n")
    assert obs.stream.chunks_seen == expected
    assert obs.finish.value == "length"
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.finish.value == "stop" and liar.finish.raw is None
    assert liar.usage.output is None and liar.stream.completed is True


@pytest.mark.parametrize("provider", ["ollama", "ollama-openai"])
@pytest.mark.parametrize("stream", [False, True])
def test_any_llm_ollama_mapping_offline(monkeypatch, capsys, provider, stream):
    import asyncio
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    from wiretruth.formats import load_scenario

    scenario = load_scenario(REPO / "suite/scenarios/finish/stop-sequence.json")
    spec = replace(
        find_cells(REPO)[0].call_spec,
        provider=provider,
        model="qwen3.5:0.8b",
        call=replace(scenario.call_for(provider), stream=stream, extra={"include_usage": True}),
    )
    doc = call_spec_document(spec)
    doc["tools"] = [{"name": "get_weather", "description": "Weather", "parameters": {}}]
    message = SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                index=0,
                id="library-id",
                function=SimpleNamespace(name="get_weather", arguments='{"city":"Zürich"}'),
            )
        ],
        parsed=None,
    )
    result = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=message, delta=message)],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )
    completion = MagicMock(return_value=iter([result]) if stream else result)
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(completion=completion))
    monkeypatch.setitem(sys.modules, "pydantic", SimpleNamespace(create_model=MagicMock()))
    monkeypatch.setenv("WIRETRUTH_PROVIDER", provider)
    env_name = "WIRETRUTH_BASE_URL_" + provider.upper().replace("-", "_")
    monkeypatch.setenv(
        env_name, "http://127.0.0.1:4321" + ("/v1" if provider == "ollama-openai" else "")
    )
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    monkeypatch.chdir(REPO / "shims/python/any-llm")
    with (
        patch("importlib.metadata.version", return_value="1.33.0"),
        patch("threading.excepthook"),
        patch.object(asyncio, "set_event_loop_policy"),
    ):
        runpy.run_path("shim.py", run_name="__main__")
    sent = completion.call_args.kwargs
    assert sent["provider"] == ("ollama" if provider == "ollama" else "openai")
    assert sent["api_base"] == "http://127.0.0.1:4321" + (
        "/v1" if provider == "ollama-openai" else ""
    )
    assert sent["api_key"] == "wt-test-key"
    assert sent["max_tokens"] == 120 and sent["stop"] == ["END"]
    assert sent["reasoning_effort"] == "none"
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["tools"] == [{"type": "function", "function": doc["tools"][0]}]
    obs = observation_from_document(parse_json(capsys.readouterr().out.encode()))
    assert obs.finish.value == "stop" and obs.finish.raw == "stop"
    assert obs.tool_calls[0].id == "library-id"
    assert obs.usage.output == 3
    assert obs.stream.completed is None


@pytest.mark.parametrize("provider", ["ollama", "ollama-openai"])
def test_any_llm_unmapped_ollama_tool_choice_exits(monkeypatch, provider):
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    doc = call_spec_document(find_cells(REPO, "tools.parallel-calls")[0].call_spec)
    doc["provider"] = provider
    doc["tool_choice"] = "required"
    completion = MagicMock()
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(completion=completion))
    monkeypatch.setitem(sys.modules, "pydantic", SimpleNamespace(create_model=MagicMock()))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    monkeypatch.setenv("WIRETRUTH_PROVIDER", provider)
    monkeypatch.chdir(REPO / "shims/python/any-llm")
    with (
        patch("importlib.metadata.version", return_value="1.33.0"),
        pytest.raises(SystemExit, match="not mapped yet"),
    ):
        runpy.run_path("shim.py", run_name="__main__")
    completion.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", [None, "none", "minimal", "medium", "high"])
@pytest.mark.parametrize("provider", ["gemini", "anthropic"])
def test_any_llm_provider_mapping_offline(monkeypatch, capsys, stream, effort, provider):
    import asyncio
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    from wiretruth.formats import Reasoning, load_scenario

    model = "gemini-3.5-flash-lite" if provider == "gemini" else "claude-sonnet-5-5"
    scenario = load_scenario(REPO / "suite/scenarios/tools/parallel-calls.json")
    spec = replace(
        find_cells(REPO)[0].call_spec,
        provider=provider,
        model=model,
        call=replace(
            scenario.call_for(provider),
            stream=stream,
            stop=("END",),
            reasoning=None if effort is None else Reasoning(effort),
        ),
    )
    doc = call_spec_document(spec)
    message = SimpleNamespace(
        content=None,
        parsed=None,
        tool_calls=[
            SimpleNamespace(
                index=0,
                id="library-id",
                function=SimpleNamespace(name="get_weather", arguments='{"city":"Zürich"}'),
            )
        ],
    )
    result = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="tool_calls", message=message, delta=message)],
        usage=SimpleNamespace(
            prompt_tokens=9,
            completion_tokens=11,
            total_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
    )
    completion = MagicMock(return_value=iter([result]) if stream else result)
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(completion=completion))
    monkeypatch.setitem(sys.modules, "pydantic", SimpleNamespace(create_model=MagicMock()))
    monkeypatch.setenv("WIRETRUTH_PROVIDER", provider)
    monkeypatch.setenv("WIRETRUTH_BASE_URL_" + provider.upper(), "http://127.0.0.1:4321")
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    monkeypatch.chdir(REPO / "shims/python/any-llm")
    with (
        patch("importlib.metadata.version", return_value="1.33.0"),
        patch("threading.excepthook"),
        patch.object(asyncio, "set_event_loop_policy"),
    ):
        runpy.run_path("shim.py", run_name="__main__")
    sent = completion.call_args.kwargs
    assert sent["provider"] == provider and sent["api_base"] == "http://127.0.0.1:4321"
    assert sent["model"] == model and sent["api_key"] == "wt-test-key"
    assert sent["stream"] is stream and sent["stop"] == ["END"]
    assert sent["tool_choice"] == "auto"
    assert sent["tools"] == [{"type": "function", "function": doc["tools"][0]}]
    if effort is None or (effort == "none" and provider == "gemini"):
        assert "reasoning_effort" not in sent
    else:
        assert sent["reasoning_effort"] == effort
    obs = observation_from_document(parse_json(capsys.readouterr().out.encode()))
    assert bool(obs.notes) is (effort == "none" and provider == "gemini")
    assert obs.finish.value == "tool_calls" and obs.finish.raw == "tool_calls"
    assert obs.tool_calls[0].id == "library-id"
    assert (
        obs.usage.input,
        obs.usage.cached_input,
        obs.usage.output,
        obs.usage.reasoning,
        obs.usage.total,
    ) == (9, 4, 11, 3, 20)
    assert obs.stream.completed is None


def test_any_llm_gemini_manifest_pins_extra_dependency():
    manifest = load_shim_manifest(REPO / "shims/python/any-llm/wiretruth-shim.json")
    assert "gemini" in manifest.providers
    assert manifest.packages["google-genai"] == "2.26.0"
    assert any("reasoning_effort none" in note for note in manifest.config_notes)


def test_reference_gemini_stream_counts_events_and_liar_still_lies(monkeypatch):
    from gemini_samples import MODEL, STREAM

    from wiretruth.formats import load_scenario

    scenario = load_scenario(REPO / "suite/scenarios/truncation/text-stream.json")
    spec = replace(
        find_cells(REPO)[0].call_spec,
        provider="gemini",
        model=MODEL,
        call=scenario.call_for("gemini"),
    )
    response = Response(
        200, {"content-type": "text/event-stream"}, ["".join(STREAM).encode()], "clean"
    )
    observe = REFERENCE["observe"]
    send = MagicMock(return_value=response)
    monkeypatch.setitem(observe.__globals__, "send", send)
    monkeypatch.setenv("WIRETRUTH_PROVIDER", "gemini")
    monkeypatch.setenv("WIRETRUTH_BASE_URL_GEMINI", "http://127.0.0.1:4321")
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    doc = observe(spec)
    obs = observation_from_document(doc)
    assert obs.finish.value == "length" and obs.finish.raw == "MAX_TOKENS"
    assert obs.stream.chunks_seen == len(STREAM) and obs.stream.completed
    assert obs.usage.output == 11 and obs.text.value == "Café καλημέρα 東京 🌸"
    assert send.call_args.args[1].path.endswith(":streamGenerateContent?alt=sse")
    liar = observation_from_document(LIAR["lie"](doc))
    assert liar.finish.value == "stop" and liar.usage.output is None
    assert liar.text.value.startswith("Caf\ufffd")


@pytest.mark.parametrize(
    "sample", ["tools", "thinking", "blocked", "empty", "error", "unknown", "cut"]
)
def test_gemini_reference_and_liar_expose_checkable_differences(sample):
    from gemini_samples import (
        BAD_REQUEST,
        BLOCKED,
        EMPTY_CANDIDATE,
        STREAM,
        THINKING,
        TOOLS,
        exchange,
    )

    from wiretruth.formats import _build_truth
    from wiretruth.providers import gemini

    payload = {
        "tools": TOOLS,
        "thinking": THINKING,
        "blocked": BLOCKED,
        "empty": EMPTY_CANDIDATE,
        "error": BAD_REQUEST,
        "unknown": {"candidates": [{"finishReason": "FUTURE"}]},
    }.get(sample)
    ex = exchange(events=STREAM[:1]) if sample == "cut" else exchange(payload)
    result = gemini.extract_truth([ex], None)
    spec = replace(find_cells(REPO)[0].call_spec, provider="gemini")
    doc = REFERENCE["observation_from_truth"](spec, result)
    if sample == "cut":
        doc["stream"]["used"] = True
    obs = observation_from_document(doc)
    liar = observation_from_document(LIAR["lie"](doc))
    if sample == "tools":
        from wiretruth.checks import tool_calls_match_truth

        with_id = deepcopy(doc)
        with_id["tool_calls"][0]["id"] = "library-generated"
        assert (
            tool_calls_match_truth(
                observation_from_document(with_id), _build_truth(result), [], {}
            ).status
            == "SATISFIED"
        )
        assert tool_calls_match_truth(liar, _build_truth(result), [], {}).status == "CONTRADICTED"
    elif sample == "thinking":
        assert obs.usage.output == 41 and liar.usage.output is None
    elif sample == "blocked":
        # The liar's existing behavior retains filters, but still loses the input count.
        assert liar.finish.value == "content_filter" and liar.usage.input is None
    elif sample == "cut":
        assert obs.stream.completed is False and liar.stream.completed is True
    else:
        assert obs.finish.value != "stop" and liar.finish.value == "stop"


@pytest.mark.parametrize(
    "total,reasoning,expected",
    [
        (65, None, "SATISFIED"),
        (28, None, "CONTRADICTED"),
        (None, 37, "SATISFIED"),
        (None, None, "CONTRADICTED"),
    ],
)
def test_gemini_candidates_alternative_still_requires_full_total(total, reasoning, expected):
    from gemini_samples import THINKING, exchange

    from wiretruth.checks import usage_matches_truth, usage_total_reconciles
    from wiretruth.formats import _build_truth
    from wiretruth.providers import gemini

    result = gemini.extract_truth([exchange(THINKING)], None)
    doc = REFERENCE["observation_from_truth"](find_cells(REPO)[0].call_spec, result)
    doc["usage"].update(output=4, reasoning=reasoning, total=total)
    for field in ("reasoning", "total"):
        doc["usage"]["surface"][field] = "primary" if doc["usage"][field] is not None else "absent"
    obs, truth = observation_from_document(doc), _build_truth(result)
    assert usage_matches_truth(obs, truth, [], {"fields": ["output"]}).status == "SATISFIED"
    assert usage_total_reconciles(obs, truth, [], {}).status == expected


@pytest.mark.parametrize(
    "scenario_id",
    [
        "truncation.tool-call-args-stream",
        "usage.cached-tokens",
        "truncation.structured-output-forced-tool",
        "truncation.structured-output",
    ],
)
def test_any_llm_anthropic_scenario_options_offline(monkeypatch, capsys, scenario_id):
    import asyncio
    import io
    from types import SimpleNamespace
    from unittest.mock import patch

    cell = next(
        cell for cell in find_cells(REPO, provider="anthropic") if cell.scenario.id == scenario_id
    )
    doc = call_spec_document(cell.call_spec)
    result = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=1536,
            completion_tokens=21,
            total_tokens=1557,
            prompt_tokens_details=SimpleNamespace(cached_tokens=1536, cache_write_tokens=0),
            completion_tokens_details=None,
        ),
    )
    completion = MagicMock(return_value=iter([result]) if doc["stream"] else result)
    create_model = MagicMock()
    monkeypatch.setitem(sys.modules, "any_llm", SimpleNamespace(completion=completion))
    monkeypatch.setitem(sys.modules, "pydantic", SimpleNamespace(create_model=create_model))
    monkeypatch.setenv("WIRETRUTH_PROVIDER", "anthropic")
    monkeypatch.setenv("WIRETRUTH_BASE_URL_ANTHROPIC", "http://127.0.0.1:4321")
    monkeypatch.setenv("WIRETRUTH_API_KEY", "wt-test-key")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(doc)))
    monkeypatch.chdir(REPO / "shims/python/any-llm")
    with (
        patch("importlib.metadata.version", return_value="1.33.0"),
        patch("threading.excepthook"),
        patch.object(asyncio, "set_event_loop_policy"),
    ):
        if scenario_id == "truncation.structured-output-forced-tool":
            with pytest.raises(SystemExit) as stopped:
                runpy.run_path("shim.py", run_name="__main__")
            assert stopped.value.code == 0
        else:
            runpy.run_path("shim.py", run_name="__main__")
    if scenario_id == "truncation.structured-output-forced-tool":
        completion.assert_not_called()
        obs = observation_from_document(parse_json(capsys.readouterr().out.encode()))
        assert obs.outcome == "unsupported"
        assert "native output_config.format" in obs.unsupported_reason
        assert not obs.structured.attempted
        return
    sent = completion.call_args.kwargs
    assert sent["provider"] == "anthropic" and sent["api_base"] == "http://127.0.0.1:4321"
    if scenario_id == "usage.cached-tokens":
        assert sent["cache_control"] == {"type": "ephemeral"}
    elif scenario_id == "truncation.tool-call-args-stream":
        assert sent["tool_choice"] == "auto"
        assert sent["messages"][0]["content"].startswith("Call create_event.")
        assert sent["extra_body"]["tools"] == [
            {
                "name": "create_event",
                "description": doc["tools"][0]["description"],
                "input_schema": doc["tools"][0]["parameters"],
                "eager_input_streaming": True,
            }
        ]
    else:
        assert sent["response_format"] is create_model.return_value
    obs = observation_from_document(parse_json(capsys.readouterr().out.encode()))
    assert (obs.usage.input, obs.usage.cached_input, obs.usage.output, obs.usage.total) == (
        1536,
        1536,
        21,
        1557,
    )
    assert obs.usage.reasoning is None and obs.usage.surface["reasoning"] == "absent"


def test_any_llm_anthropic_manifest_pins_sdk_and_documents_surfaces():
    manifest = load_shim_manifest(REPO / "shims/python/any-llm/wiretruth-shim.json")
    assert manifest.packages["anthropic"] == "0.125.0"
    assert "anthropic" in manifest.providers
    for raw, expected in [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("refusal", "refusal"),
        ("pause_turn", "other"),
        ("model_context_window_exceeded", "length"),
    ]:
        assert manifest.finish_map[raw] == expected
    assert any("thinking disabled" in note for note in manifest.config_notes)
    assert any(
        item.type == "anthropic.APIError" and item.category == "api" for item in manifest.error_map
    )
