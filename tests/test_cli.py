import http.client
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

SAMPLES = Path(__file__).resolve().parent / "samples"
EXPECTED_COMMANDS = ["run", "serve", "record", "lint", "envs"]


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
