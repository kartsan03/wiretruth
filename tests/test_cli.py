import re
import subprocess
import sys
from importlib.metadata import entry_points

import pytest

from wiretruth import __version__
from wiretruth.cli import COMMANDS, main

EXPECTED_COMMANDS = ["run", "serve", "record", "derive", "lint", "catalog", "site", "envs"]


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


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_commands_are_not_implemented_yet(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([command]) == 2
    assert "not implemented yet" in capsys.readouterr().err


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
