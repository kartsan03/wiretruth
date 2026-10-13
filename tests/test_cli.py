import http.client
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from wiretruth import __version__
from wiretruth.cli import main
from wiretruth.formats import load_document
from wiretruth.lint import lint

SAMPLES = Path(__file__).resolve().parent / "samples"
EXPECTED_COMMANDS = ["run", "serve", "record", "derive", "lint", "catalog", "envs"]


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    listed = re.findall(r"^    (\S+)  ", out, re.MULTILINE)
    assert listed == EXPECTED_COMMANDS


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"wiretruth {__version__}"


def test_unknown_command_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["no-such-command"])
    assert exc.value.code == 2


def test_no_command_prints_help_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage: wiretruth" in capsys.readouterr().err


def test_module_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "wiretruth", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"wiretruth {__version__}"


def test_console_script_points_at_main() -> None:
    (script,) = entry_points(group="console_scripts", name="wiretruth")
    assert script.load() is main


def test_serve_without_a_fixture_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["serve", "openai-chat/truncation.text"]) == 2
    err = capsys.readouterr().err
    assert "no fixture suite/fixtures/openai-chat/truncation.text.json" in err


def test_serve_an_invalid_fixture_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    path = SAMPLES / "invalid" / "fixture" / "abort-json-body.json"
    assert main(["serve", str(path)]) == 1
    assert "'abort' needs sse, ndjson or chunks_b64" in capsys.readouterr().err


@pytest.mark.parametrize("port", ["taken", "70000"])
def test_serve_on_a_port_it_cant_use_exits_2(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    fixture = SAMPLES / "valid" / "fixture" / "recorded-sse.json"
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        number = str(taken.getsockname()[1]) if port == "taken" else port
        assert main(["serve", str(fixture), "--port", number]) == 2
    assert f"can't listen on 127.0.0.1:{number}" in capsys.readouterr().err


def _ignore_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT")
def test_serve_runs_until_ctrl_c(tmp_path: Path) -> None:
    fixtures = tmp_path / "suite" / "fixtures" / "openai-chat"
    fixtures.mkdir(parents=True)
    shutil.copy(
        SAMPLES / "valid" / "fixture" / "recorded-sse.json",
        fixtures / "truncation.text-stream.json",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "wiretruth", "serve", "openai-chat/truncation.text-stream"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Started the way a background job is: Ctrl-C must still stop it.
        preexec_fn=_ignore_sigint,
    )
    watchdog = threading.Timer(20, process.kill)  # so a stuck child can't hang the test run
    watchdog.start()
    try:
        assert process.stdout is not None
        banner = [process.stdout.readline() for _ in range(3)]
        url = re.fullmatch(r"openai-chat base URL: (http://127\.0\.0\.1:(\d+)/v1)\n", banner[1])
        assert url, banner
        assert (
            banner[0] == f"wiretruth serve: openai-chat/truncation.text-stream on {url[1][:-3]}\n"
        )
        assert banner[2] == "Press Ctrl-C to stop.\n"
        conn = http.client.HTTPConnection("127.0.0.1", int(url[2]), timeout=5)
        conn.request("POST", "/v1/chat/completions", body=b"{}")
        assert conn.getresponse().read().endswith(b"data: [DONE]\n\n")
        conn.close()
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=10)
    finally:
        watchdog.cancel()
        process.kill()
    assert process.returncode == 0, err
    assert out == (
        "wiretruth serve: stopped after 1 request\n"
        "  POST /v1/chat/completions: exchange 1 of 1, sent in full\n"
    )


@pytest.fixture
def derive_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "suite", tmp_path / "suite")
    scenario = {
        "format": "wiretruth.scenario/v1",
        "id": "stream.derived-test",
        "title": "A derived stream",
        "bug_class": "usage-accounting",
        "priority": "P1",
        "summary": "A transformed response exercises the stream reader.",
        "call": {"stream": True, "messages": [{"role": "user", "content": "Say hello."}]},
        "providers": ["openai-chat", "openai-responses"],
        "requirements": [
            {
                "id": "text",
                "level": "SHOULD",
                "check": "text_matches_truth",
                "text": "Report the text sent.",
            }
        ],
    }
    path = tmp_path / "suite" / "scenarios" / "stream" / "derived-test.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(scenario), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _derive_command(provider="openai-chat", transform="strip_finish", source=None, args=None):
    if source is None:
        source = (
            f"{provider}/truncation.text-stream-usage"
            if provider == "openai-chat"
            else f"{provider}/truncation.text-stream"
        )
    command = [
        "derive",
        "--from",
        source,
        "--transform",
        transform,
        "--out",
        f"{provider}/stream.derived-test",
    ]
    if args is not None:
        command += ["--args", json.dumps(args)]
    return command


@pytest.mark.parametrize("provider", ["openai-chat", "openai-responses"])
@pytest.mark.parametrize(
    "transform",
    [
        "cut_stream_after",
        "strip_finish",
        "inject_error_event",
        "rechunk_inside_utf8",
        "replace_finish_value",
        "prepend_http_error",
    ],
)
def test_derive_output_passes_lint(derive_suite, provider, transform, capsys):
    arguments = {
        "cut_stream_after": {"n": 3, "end": "abort"},
        "strip_finish": {},
        "inject_error_event": {
            "after": 2,
            "error": {"error": {"message": "Try again"}}
            if provider == "openai-chat"
            else {"type": "error", "message": "Try again"},
        },
        "rechunk_inside_utf8": {"points": [1596 if provider == "openai-chat" else 3278]},
        "replace_finish_value": {"value": "future_finish"},
        "prepend_http_error": {
            "status": 429,
            "headers": {"content-type": "application/json", "retry-after": "1"},
            "body": {"error": {"message": "Try again"}},
        },
    }
    source = (
        f"{provider}/tools.parallel-calls-stream" if transform == "rechunk_inside_utf8" else None
    )
    assert main(_derive_command(provider, transform, source, arguments[transform])) == 0
    path = derive_suite / "suite" / "fixtures" / provider / "stream.derived-test.json"
    document = load_document("fixture", path)
    assert document["tier"] == "derived"
    assert document["derived_from"]["args"] == arguments[transform]
    assert path.read_text() == json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    assert lint(derive_suite).problems == []
    output = capsys.readouterr()
    assert output.out.strip() == f"wrote suite/fixtures/{provider}/stream.derived-test.json"
    assert output.err == ""


def test_derive_defaults_overrides_and_replace(derive_suite, capsys):
    command = _derive_command()
    assert main(command) == 0
    path = derive_suite / "suite" / "fixtures" / "openai-chat" / "stream.derived-test.json"
    original = path.read_bytes()
    assert main(command) == 2
    assert "pass --replace" in capsys.readouterr().err
    assert path.read_bytes() == original
    assert (
        main(
            [
                *command,
                "--replace",
                "--rationale",
                "Test the finish signal.",
                "--evidence-url",
                "https://example.com/evidence",
            ]
        )
        == 0
    )
    document = load_document("fixture", path)
    assert document["derived_from"]["args"] == {}
    assert document["derived_from"]["rationale"] == "Test the finish signal."
    assert document["derived_from"]["evidence_url"] == "https://example.com/evidence"
    assert lint(derive_suite).problems == []


@pytest.mark.parametrize("tier", ["recorded", "documented"])
@pytest.mark.parametrize("replace", [False, True])
def test_derive_never_overwrites_source_tiers(derive_suite, tier, replace, capsys):
    folder = derive_suite / "suite" / "fixtures" / "openai-chat"
    document = load_document("fixture", folder / "truncation.text-stream-usage.json")
    document.update(id="openai-chat/stream.derived-test", scenario="stream.derived-test", tier=tier)
    if tier == "documented":
        document["provenance"].update(
            source_url="https://example.com/docs", notes="Transcribed example."
        )
    path = folder / "stream.derived-test.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes()
    command = _derive_command() + (["--replace"] if replace else [])
    assert main(command) == 2
    assert f"cannot overwrite a {tier} fixture" in capsys.readouterr().err
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "extra,code,message",
    [
        (["--from", "openai-chat/stream.missing"], 2, "unknown fixture"),
        (["--from", "../outside"], 2, "fixture id"),
        (["--transform", "unknown"], 2, "unknown transform"),
        (["--args", "[]"], 2, "JSON object"),
        (["--args", "{"], 2, "wiretruth derive:"),
        (["--args", '{"n":NaN}'], 2, "not allowed"),
        (["--args", '{"n":1,"n":2}'], 2, "duplicate key"),
        (["--args", '{"unexpected":1}'], 2, "requires exactly"),
        (["--transform", "cut_stream_after", "--args", '{"n":0,"end":"clean"}'], 2, "n must"),
        (["--from", "openai-chat/request.max-tokens-field"], 2, "sse or ndjson"),
        (["--out", "openai-responses/stream.derived-test"], 2, "output provider"),
        (["--out", "openai-chat/../escape"], 1, "wiretruth derive:"),
        (["--rationale", ""], 1, "wiretruth derive:"),
        (["--evidence-url", "http://example.com"], 1, "wiretruth derive:"),
    ],
)
def test_derive_errors_exit_without_writing(derive_suite, extra, code, message, capsys):
    before = set((derive_suite / "suite").rglob("*.json"))
    assert main(_derive_command() + extra) == code
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err.splitlines()) == 1
    assert message in output.err
    assert set((derive_suite / "suite").rglob("*.json")) == before


def test_derive_invalid_source_exits_1(derive_suite, capsys):
    path = derive_suite / "suite" / "fixtures" / "openai-chat" / "truncation.text-stream-usage.json"
    path.write_text("{}", encoding="utf-8")
    assert main(_derive_command()) == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_derive_refuses_derived_source(derive_suite, capsys):
    assert main(_derive_command()) == 0
    assert (
        main(
            [
                *_derive_command(source="openai-chat/stream.derived-test"),
                "--out",
                "openai-chat/stream.another",
            ]
        )
        == 2
    )
    assert "not derived" in capsys.readouterr().err


def test_derive_write_error_exits_2(derive_suite, capsys):
    path = derive_suite / "suite" / "fixtures" / "openai-chat" / ".stream.derived-test.json.new"
    path.mkdir()
    assert main(_derive_command()) == 2
    assert len(capsys.readouterr().err.splitlines()) == 1


@pytest.mark.parametrize("option", ["--from", "--transform", "--out"])
def test_derive_requires_options(option):
    command = _derive_command()
    index = command.index(option)
    del command[index : index + 2]
    with pytest.raises(SystemExit) as error:
        main(command)
    assert error.value.code == 2
