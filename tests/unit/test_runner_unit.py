import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wiretruth import runner
from wiretruth.cli import build_parser, main
from wiretruth.formats import (
    FormatError,
    call_spec_document,
    load_shim_manifest,
    observation_from_document,
)
from wiretruth.grading import CellGrade, RequirementResult
from wiretruth.providers import openai_chat
from wiretruth.recorder import RecordError
from wiretruth.replay import BASE_PATHS, LoggedRequest
from wiretruth.runner import (
    CellResult,
    ExpectedGrade,
    RunError,
    RunReport,
    build_env,
    compare_grades,
    find_cells,
    load_expected_grades,
    process_to_run,
    run_cell,
    run_suite,
)

REPO = Path(__file__).resolve().parents[2]
SHIM = REPO / "shims" / "reference"
REQUEST = LoggedRequest("POST", "/v1/chat/completions", {}, {}, {}, None, "", 0, True)
OBSERVATION = {
    "format": "wiretruth.observation/v1",
    "library": {"name": "sample", "version": "1", "language": "python", "api": "sample.call"},
    "outcome": "returned",
    "unsupported_reason": None,
    "error": None,
    "finish": {"value": "length", "raw": "length", "surface": "primary", "source": "finish"},
    "text": {"value": "Crème brûlée 🍮", "surface": "primary"},
    "structured": {"attempted": False, "returned_as_success": False, "value": None},
    "tool_calls": [],
    "usage": None,
    "stream": {"used": False, "completed": None, "chunks_seen": None},
    "notes": [],
    "elapsed_ms": 1,
}
STDOUT = json.dumps(OBSERVATION, ensure_ascii=False).encode()


def interpret(stdout=STDOUT, **changes):
    return process_to_run(
        **{
            "timed_out": False,
            "returncode": 0,
            "stdout": stdout,
            "request_log": (REQUEST,),
            "stream": False,
            "provider": "openai-chat",
            **changes,
        }
    )


def test_stdout_is_one_utf8_observation_with_optional_whitespace():
    run = interpret(b" \r\n" + STDOUT + b"\t ")
    assert run.observation == observation_from_document(OBSERVATION)
    assert run.invalid is run.signal is run.mismatch is None
    assert not run.timed_out


def test_empty_stdout_has_no_observation_and_no_invalid_reason():
    run = interpret(b"")
    assert run.observation is run.invalid is None


@pytest.mark.parametrize(
    "stdout",
    [
        b"junk",
        b" \n\t",
        b"\v\f",
        b"\xff",
        b"{}",
        b"[]",
        b"null",
        b"true",
        STDOUT + STDOUT,
        b"log\n" + STDOUT,
        b'{"format": "wiretruth.observation/v1", "format": "duplicate"}',
        STDOUT.replace(b'"elapsed_ms": 1', b'"elapsed_ms": NaN'),
        STDOUT.replace(b'"elapsed_ms": 1', b'"elapsed_ms": 1e400'),
        STDOUT.replace(b'"notes": []', b'"notes": ["\\ud800"]'),
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_invalid_stdout_is_distinct_from_no_observation(stdout):
    run = interpret(stdout)
    assert run.observation is None
    assert run.invalid and len(run.invalid) <= 300


@pytest.mark.parametrize("returncode", [3, -11])
@pytest.mark.parametrize("stdout", [STDOUT, b"junk", b""])
def test_nonzero_exit_discards_stdout_without_calling_it_invalid(returncode, stdout):
    run = interpret(stdout, returncode=returncode)
    assert run.observation is run.invalid is None
    assert run.signal == (11 if returncode < 0 else None)


def test_timeout_and_signal_are_independent_facts():
    run = interpret(timed_out=True, returncode=-9)
    assert run.timed_out and run.signal == 9


def test_mismatches_include_every_wrong_endpoint_and_stream_mode():
    log = [
        replace(REQUEST, path="/wrong", exchange=None, json_body={"stream": True}),
        replace(REQUEST, json_body={"stream": True}),
    ]
    run = interpret(request_log=log)
    assert run.mismatch == (
        "request 0: no fixture for POST /wrong; "
        "request 0: /wrong requested stream=True, expected False; "
        "request 1: /v1/chat/completions requested stream=True, expected False"
    )
    log.clear()
    assert len(run.request_log) == 2


def test_unstreamed_request_against_streamed_call_is_a_mismatch():
    assert "expected True" in interpret(stream=True).mismatch


@pytest.mark.parametrize("outcome", ["returned", "raised", "unsupported"])
def test_only_returned_without_a_request_is_a_mismatch(outcome):
    doc = {**OBSERVATION, "outcome": outcome}
    if outcome == "raised":
        doc["error"] = {
            "type": "ConnectionError",
            "message": "No connection",
            "category": "network",
            "retry_after": None,
            "stack_top": None,
        }
    if outcome == "unsupported":
        doc["unsupported_reason"] = "No schema support: https://example.org/features"
    run = interpret(json.dumps(doc).encode(), request_log=())
    assert run.invalid is None
    assert bool(run.mismatch) == (outcome == "returned")


@pytest.mark.parametrize(
    "body,expected",
    [
        (None, False),
        ([], False),
        (True, False),
        ({}, False),
        ({"stream": False}, False),
        ({"stream": 1}, False),
        ({"stream": "true"}, False),
        ({"stream": True}, True),
    ],
)
def test_requested_stream_requires_json_true(body, expected):
    assert openai_chat.requested_stream("/v1/chat/completions", body) is expected


def test_environment_allowlist_provider_urls_and_manifest_precedence():
    inherited = dict.fromkeys(runner.INHERITED_ENV, "kept")
    inherited.update(
        {
            "OPENAI_API_KEY": "not-inherited",
            "OPENAI_BASE_URL": "https://example.org",
            "HTTP_PROXY": "https://example.org",
            "HTTPS_PROXY": "https://example.org",
            "ALL_PROXY": "https://example.org",
            "http_proxy": "https://example.org",
            "no_proxy": "wrong",
            "NO_PROXY": "wrong",
            "PYTHONPATH": "/wrong",
            "WIRETRUTH_API_KEY": "wrong",
            "SDK_SETTING": "wrong",
        }
    )
    env = build_env(
        "openai-chat", "http://127.0.0.1:4321", 4322, {"LANG": "C", "MODE": "ok"}, inherited
    )
    expected = {name: "kept" for name in runner.INHERITED_ENV}
    expected.update(
        LANG="C",
        MODE="ok",
        WIRETRUTH_API_KEY="wt-test-key",
        WIRETRUTH_PROVIDER="openai-chat",
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    for provider, path in BASE_PATHS.items():
        port = 4321 if provider == "openai-chat" else 4322
        expected[f"WIRETRUTH_BASE_URL_{provider.upper().replace('-', '_')}"] = (
            f"http://127.0.0.1:{port}{path}"
        )
    assert env == expected
    empty = build_env("openai-chat", "http://127.0.0.1:4321", 4322, {"NO_PROXY": "explicit"}, {})
    assert not set(runner.INHERITED_ENV) & empty.keys()
    assert empty["NO_PROXY"] == "explicit"


def test_closed_port_binds_without_listening_and_stays_open(monkeypatch):
    factory = MagicMock()
    reserved = factory.return_value.__enter__.return_value
    reserved.getsockname.return_value = ("127.0.0.1", 4322)
    monkeypatch.setattr(runner.socket, "socket", factory)
    with runner.closed_port() as port:
        assert port == 4322
        factory.assert_called_once_with(runner.socket.AF_INET, runner.socket.SOCK_STREAM)
        reserved.bind.assert_called_once_with(("127.0.0.1", 0))
        reserved.listen.assert_not_called()
        factory.return_value.__exit__.assert_not_called()
    factory.return_value.__exit__.assert_called_once()


@pytest.fixture
def suite(tmp_path):
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    return tmp_path


def test_discovery_uses_scenario_ids_filters_and_undated_fixtures(suite):
    cells = find_cells(suite)
    assert [(c.scenario.id, c.provider) for c in cells] == [
        ("truncation.structured-output", "openai-chat"),
        ("truncation.text", "openai-chat"),
        ("truncation.text-stream", "openai-chat"),
    ]
    assert [c.scenario.id for c in find_cells(suite, "*.text*", "openai-chat")] == [
        "truncation.text",
        "truncation.text-stream",
    ]
    folder = suite / "suite" / "fixtures" / "openai-chat"
    # A dated file must never be loaded, even if it is the only file for a scenario.
    (folder / "truncation.text@2026-10-01.json").write_text("invalid dated file")
    (folder / "truncation.text.json").unlink()
    assert len(find_cells(suite)) == 2
    with pytest.raises(RunError, match="no cell matches"):
        find_cells(suite, "truncation.text")
    with pytest.raises(RunError, match="no cell matches"):
        find_cells(suite, provider="gemini")
    with pytest.raises(RecordError, match="no scenario"):
        find_cells(suite, "finish.*")
    with pytest.raises(RecordError, match="lists"):
        find_cells(suite, provider="unlisted")


def test_discovery_rejects_an_incorrect_fixture_identity(suite):
    folder = suite / "suite" / "fixtures" / "openai-chat"
    shutil.copy(folder / "truncation.text-stream.json", folder / "truncation.text.json")
    with pytest.raises(RunError, match="expected fixture"):
        find_cells(suite, "truncation.text")


def test_discovery_sorts_multiple_providers_and_requires_their_modules(suite, monkeypatch):
    fixtures = suite / "suite" / "fixtures"
    doc = json.loads((fixtures / "openai-chat" / "truncation.text.json").read_text())
    doc.update(id="gemini/truncation.text", provider="gemini")
    (fixtures / "gemini").mkdir()
    (fixtures / "gemini" / "truncation.text.json").write_text(json.dumps(doc))
    with pytest.raises(RunError, match="no provider module for gemini"):
        find_cells(suite, "truncation.text")
    monkeypatch.setitem(runner.PROVIDERS, "gemini", openai_chat)
    assert [cell.provider for cell in find_cells(suite, "truncation.text")] == [
        "gemini",
        "openai-chat",
    ]
    assert [cell.provider for cell in find_cells(suite, provider="gemini")] == ["gemini"]


def test_call_spec_uses_provenance_model_default_and_provider_override():
    cell = find_cells(REPO, "truncation.text")[0]
    assert cell.call_spec.model == cell.fixture.provenance.requested_model
    changed = replace(cell.scenario.call, stream=True)
    cell = replace(
        cell,
        scenario=replace(cell.scenario, call_overrides={cell.provider: changed}),
        fixture=replace(
            cell.fixture, provenance=replace(cell.fixture.provenance, requested_model=None)
        ),
    )
    assert cell.call_spec.model == openai_chat.DEFAULT_MODEL
    assert cell.call_spec.call is changed


@pytest.mark.parametrize(
    "mode,language,entry",
    [(mode, "python", "python") for mode in ["ok", "exit", "signal", "timeout", "fallback", "gone"]]
    + [("ok", "python", "/custom/python"), ("ok", "javascript", "python")],
)
def test_process_lifecycle_and_final_log(mode, language, entry, tmp_path, monkeypatch):
    cell = find_cells(REPO, "truncation.text")[0]
    manifest = load_shim_manifest(SHIM / "wiretruth-shim.json")
    manifest = replace(manifest, language=language, entry=(entry, "shim.py"))
    active = False

    class Server:
        url = "http://127.0.0.1:4321"

        def __init__(self, fixture):
            assert fixture is cell.fixture

        def __enter__(self):
            nonlocal active
            active = True
            return self

        def __exit__(self, *args):
            nonlocal active
            active = False

        @property
        def request_log(self):
            assert not active
            return [REQUEST]

    factory = MagicMock()
    process = factory.return_value.__enter__.return_value
    process.pid = 1234
    process.returncode = {"ok": 0, "exit": 3, "signal": -11}.get(mode, -9)
    timed_out = mode in {"timeout", "fallback", "gone"}
    if timed_out:
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("shim", 1),
            (STDOUT, b"log\xff"),
        ]
    else:
        process.communicate.return_value = STDOUT, b"log\xff"
    killpg = MagicMock(side_effect=ProcessLookupError if mode == "gone" else None)
    monkeypatch.setattr(runner, "ReplayServer", Server)
    monkeypatch.setattr(runner.subprocess, "Popen", factory)
    monkeypatch.setattr(runner.os, "killpg", killpg, raising=False)
    if mode == "fallback":
        monkeypatch.delattr(runner.os, "killpg")
    result = run_cell(
        cell, shim_dir=SHIM, manifest=manifest, stderr_dir=tmp_path, reserved_port=4322, timeout=1
    )
    args, kwargs = factory.call_args
    executable = sys.executable if language == "python" and entry == "python" else entry
    assert args == ([executable, "shim.py"],)
    assert kwargs["cwd"] == SHIM
    assert kwargs["start_new_session"] is True
    assert all(kwargs[name] == subprocess.PIPE for name in ("stdin", "stdout", "stderr"))
    assert kwargs["env"]["WIRETRUTH_API_KEY"] == "wt-test-key"
    assert process.communicate.call_args_list[0].kwargs == {"timeout": 1}
    assert json.loads(process.communicate.call_args_list[0].args[0]) == call_spec_document(
        cell.call_spec
    )
    assert (tmp_path / "openai-chat.truncation.text.stderr").read_bytes() == b"log\xff"
    assert result.fixture == cell.fixture.id and result.fixture_tier == cell.fixture.tier
    assert result.request_count == 1
    assert result.grade.grade == {"ok": "PASS", "exit": "HARNESS", "signal": "CRASH"}.get(
        mode, "HANG"
    )
    assert result.notes == result.grade.notes + (
        ("the shim exited with code 3",) if mode == "exit" else ()
    )
    if timed_out:
        assert process.communicate.call_args_list[1].args == ()
        if mode == "fallback":
            process.kill.assert_called_once()
        else:
            killpg.assert_called_once_with(1234, signal.SIGKILL)
    else:
        killpg.assert_not_called()


def test_parallel_cells_hold_closed_port_and_results_stay_in_cell_order(tmp_path, monkeypatch):
    active = False
    barrier = threading.Barrier(2, timeout=5)
    last_started = threading.Event()

    @contextmanager
    def reserve():
        nonlocal active
        active = True
        yield 4322
        active = False

    def execute(cell, **kwargs):
        assert active and kwargs["reserved_port"] == 4322
        assert kwargs["timeout"] == 1
        barrier.wait()
        if cell.scenario.id == "truncation.text":
            assert last_started.wait(5)
        else:
            last_started.set()
        return make_result(cell.scenario.id)

    monkeypatch.setattr(runner, "closed_port", reserve)
    monkeypatch.setattr(runner, "run_cell", execute)
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    report = run_suite(tmp_path, SHIM, pattern="truncation.text*", jobs=2, timeout=1)
    assert [cell.scenario for cell in report.cells] == ["truncation.text", "truncation.text-stream"]
    assert report.stderr_dir.parent == tmp_path / ".wiretruth" / "runs"
    assert report.stderr_dir.name.endswith("Z")
    assert not active


@pytest.mark.parametrize("library", [False, True])
def test_interpreter_is_resolved_once_before_workers(library, tmp_path, monkeypatch):
    manifest = load_shim_manifest(SHIM / "wiretruth-shim.json")
    if library:
        manifest = replace(manifest, primary_package="sample", packages={"sample": "1.0"})
    interpreter = "/env/bin/python" if library else sys.executable

    def resolve(root, selected):
        assert threading.current_thread() is threading.main_thread()
        assert root == tmp_path and selected is manifest
        return interpreter

    @contextmanager
    def reserve():
        resolver.assert_called_once()
        yield 4322

    resolver = MagicMock(side_effect=resolve)
    server = MagicMock()
    server.__enter__.return_value = server
    server.url = "http://127.0.0.1:4321"
    server.request_log = [REQUEST]
    process = MagicMock()
    process.__enter__.return_value = process
    process.communicate.return_value = STDOUT, b""
    process.returncode = 0
    popen = MagicMock(return_value=process)
    monkeypatch.setattr(runner, "load_shim_manifest", lambda path: manifest)
    monkeypatch.setattr(runner, "resolve_interpreter", resolver)
    monkeypatch.setattr(runner, "closed_port", reserve)
    monkeypatch.setattr(runner, "ReplayServer", lambda fixture: server)
    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    report = run_suite(tmp_path, SHIM, jobs=2)
    assert len(report.cells) == popen.call_count == 3
    assert all(call.args[0] == [interpreter, "shim.py"] for call in popen.call_args_list)
    resolver.assert_called_once_with(tmp_path, manifest)


def make_result(scenario="truncation.text", grade="PASS", statuses=None):
    if statuses is None:
        statuses = {"cause-is-truncation": "SATISFIED"}
    graded = CellGrade(
        grade,
        8,
        tuple(RequirementResult(k, "MUST", v, "evidence") for k, v in statuses.items()),
        (),
    )
    return CellResult(
        scenario, "openai-chat", f"openai-chat/{scenario}", "recorded", graded, None, 1, ()
    )


def test_compare_grades_reports_each_kind_of_difference():
    cells = [make_result(), make_result("truncation.text-stream")]
    expected = {
        ("truncation.text", "openai-chat"): ExpectedGrade(
            "WRONG", {"usage": "SATISFIED", "cause-is-truncation": "CONTRADICTED"}
        ),
        ("truncation.structured-output", "openai-chat"): ExpectedGrade("PASS", {}),
    }
    assert compare_grades(cells, expected) == (
        "truncation.structured-output openai-chat: missing from the run",
        "truncation.text openai-chat: grade PASS, expected WRONG",
        "truncation.text openai-chat: requirement cause-is-truncation: "
        "SATISFIED, expected CONTRADICTED",
        "truncation.text openai-chat: requirement usage: <missing>, expected SATISFIED",
        "truncation.text-stream openai-chat: not listed in expected grades",
    )
    assert compare_grades(
        [make_result()], {("truncation.text", "openai-chat"): ExpectedGrade("PASS", {})}
    ) == (
        "truncation.text openai-chat: requirement cause-is-truncation: "
        "SATISFIED, expected <missing>",
    )


def test_comparison_checks_status_even_when_the_grade_does_not_change():
    cell = make_result(statuses={"usage": "NOT_OBSERVABLE"})
    expected = {(cell.scenario, cell.provider): ExpectedGrade("PASS", {"usage": "SATISFIED"})}
    assert len(compare_grades([cell], expected)) == 1
    assert compare_grades([make_result(statuses={"usage": "SATISFIED"})], expected) == ()
    assert (
        compare_grades(
            [make_result(grade="HARNESS", statuses={})],
            {(cell.scenario, cell.provider): ExpectedGrade("HARNESS", {})},
        )
        == ()
    )


@pytest.mark.parametrize(
    "text",
    [
        "junk",
        "{}",
        "[]",
        '{"cells": {}}',
        '{"cells": [null]}',
        '{"cells": [], "extra": 0}',
        '{"cells": [], "cells": []}',
        '{"cells": NaN}',
    ],
)
def test_expected_grades_rejects_malformed_documents(tmp_path, text):
    path = tmp_path / "expected.json"
    path.write_text(text)
    with pytest.raises((RunError, FormatError)):
        load_expected_grades(path)


@pytest.mark.parametrize(
    "change",
    [
        {"scenario": ""},
        {"provider": None},
        {"grade": "MAYBE"},
        {"grade": []},
        {"requirements": []},
        {"requirements": {"usage": "PASS"}},
        {"requirements": {"": "SATISFIED"}},
        {"extra": True},
    ],
)
def test_expected_grades_rejects_malformed_cells(tmp_path, change):
    cell = {
        "scenario": "truncation.text",
        "provider": "openai-chat",
        "grade": "PASS",
        "requirements": {},
    }
    path = tmp_path / "expected.json"
    path.write_text(json.dumps({"cells": [{**cell, **change}]}))
    with pytest.raises(RunError):
        load_expected_grades(path)


def test_expected_grades_duplicate_and_unreadable_files(tmp_path):
    with pytest.raises(FormatError, match="cannot read"):
        load_expected_grades(tmp_path / "missing.json")
    doc = json.loads((REPO / "tests" / "expected-grades" / "reference.json").read_text())
    doc["cells"].append(doc["cells"][0])
    path = tmp_path / "expected.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(RunError, match="duplicate cell"):
        load_expected_grades(path)


@pytest.mark.parametrize(
    "problem",
    [
        "missing-manifest",
        "bad-manifest",
        "no-cell",
        "missing-expected",
        "bad-expected",
        "jobs",
        "filtered-check",
    ],
)
def test_run_cli_setup_errors_exit_two(problem, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    args = ["run", "--shim", str(SHIM)]
    if problem == "missing-manifest":
        args[-1] = str(tmp_path)
    elif problem == "bad-manifest":
        args[-1] = str(tmp_path)
        (tmp_path / "wiretruth-shim.json").write_text("{}")
    elif problem == "no-cell":
        args += ["--scenario", "nothing.*"]
    elif problem in {"missing-expected", "bad-expected"}:
        path = tmp_path / "expected.json"
        if problem == "bad-expected":
            path.write_text("{}")
        args += ["--check-grades", str(path)]
    elif problem == "filtered-check":
        expected = REPO / "tests" / "expected-grades" / "reference.json"
        args += ["--scenario", "truncation.text", "--check-grades", str(expected)]
    else:
        args += ["--jobs", "0"]
    assert main(args) == 2
    assert capsys.readouterr().err.startswith("wiretruth run: ")


def test_run_cli_requires_shim_and_defaults_jobs():
    with pytest.raises(SystemExit) as error:
        main(["run"])
    assert error.value.code == 2
    assert build_parser().parse_args(["run", "--shim", "."]).jobs == min(4, os.cpu_count() or 1)


@pytest.mark.parametrize("check,expected_code", [(None, 0), ("same", 0), ("different", 1)])
def test_run_cli_output_and_check_exit_codes(check, expected_code, tmp_path, monkeypatch, capsys):
    from wiretruth import cli

    observation = replace(observation_from_document(OBSERVATION), notes=("effort not sent",))
    cell = replace(make_result(), observation=observation, notes=("first note", "second note"))
    monkeypatch.setattr(cli, "run_suite", lambda *args, **kwargs: RunReport((cell,), tmp_path))
    args = ["run", "--shim", str(SHIM)]
    if check is not None:
        path = tmp_path / "expected.json"
        path.write_text(
            json.dumps(
                {
                    "cells": [
                        {
                            "scenario": cell.scenario,
                            "provider": cell.provider,
                            "grade": "PASS" if check == "same" else "WRONG",
                            "requirements": {"cause-is-truncation": "SATISFIED"},
                        }
                    ]
                }
            )
        )
        args += ["--check-grades", str(path)]
    assert main(args) == expected_code
    lines = capsys.readouterr().out.splitlines()
    assert lines[:5] == [
        "✓ PASS truncation.text openai-chat",
        "  first note",
        "  second note",
        "  shim: effort not sent",
        f"stderr: {tmp_path}",
    ]
    assert len(lines) == (6 if check == "different" else 5)
