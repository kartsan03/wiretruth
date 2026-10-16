import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wiretruth import cli, envs
from wiretruth.envs import (
    RunError,
    build_javascript_env,
    find_library,
    installed_javascript_packages,
    installed_packages,
    list_envs,
    parse_installed_packages,
    prune_envs,
    resolve_interpreter,
)
from wiretruth.formats import load_shim_manifest
from wiretruth.runner import RunReport

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def library(tmp_path):
    doc = json.loads((REPO / "shims/reference/wiretruth-shim.json").read_text())
    doc.update(name="sample", primary_package="sample-sdk", packages={"sample-sdk": "1.2.3"})
    path = tmp_path / "shims/python/sample/wiretruth-shim.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc))
    return load_shim_manifest(path)


@pytest.fixture
def uv(monkeypatch):
    def run(command, **kwargs):
        assert kwargs == {"stdout": sys.stderr, "check": False}
        print("uv output", file=kwargs["stdout"])
        if command[1] == "venv":
            python = Path(command[-1]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
        return subprocess.CompletedProcess(command, 0)

    stub = MagicMock(side_effect=run)
    monkeypatch.setattr(envs.shutil, "which", lambda name: "/fake/uv")
    monkeypatch.setattr(envs.subprocess, "run", stub)
    return stub


def test_build_installs_all_pins_and_sends_uv_output_to_stderr(tmp_path, library, uv, capsys):
    library = replace(library, packages={"sample-sdk": "1.2.3", "dependency[extra]": "4.5.6"})
    interpreter = resolve_interpreter(tmp_path, library)
    path = tmp_path / ".wiretruth/envs/py/sample-1.2.3"
    assert interpreter == str(path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    assert [call.args[0] for call in uv.call_args_list] == [
        ["uv", "venv", "--python", "3.12", str(path)],
        [
            "uv",
            "pip",
            "install",
            "--python",
            interpreter,
            "--",
            "dependency[extra]==4.5.6",
            "sample-sdk==1.2.3",
        ],
    ]
    assert json.loads((path / envs.MARKER).read_text()) == {
        "python": "3.12",
        "packages": ["dependency[extra]==4.5.6", "sample-sdk==1.2.3"],
    }
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "uv output\nuv output\n"


def test_matching_env_is_reused_without_uv(tmp_path, library, uv, monkeypatch):
    interpreter = resolve_interpreter(tmp_path, library)
    monkeypatch.setattr(envs.shutil, "which", lambda name: None)
    assert resolve_interpreter(tmp_path, library) == interpreter
    assert uv.call_count == 2


@pytest.mark.parametrize(
    "change", ["missing-marker", "bad-marker", "missing-python", "pins", "python-version"]
)
def test_partial_or_changed_env_is_removed_and_rebuilt(change, tmp_path, library, uv, monkeypatch):
    interpreter = Path(resolve_interpreter(tmp_path, library))
    path = interpreter.parent.parent
    old = path / "old-install"
    old.touch()
    if change == "missing-marker":
        (path / envs.MARKER).unlink()
    elif change == "bad-marker":
        (path / envs.MARKER).write_bytes(b"\xff")
    elif change == "missing-python":
        interpreter.unlink()
    elif change == "python-version":
        monkeypatch.setattr(envs, "PYTHON", "3.13")
    else:
        library = replace(library, packages={**library.packages, "dependency": "2.0"})
    assert resolve_interpreter(tmp_path, library) == str(interpreter)
    assert uv.call_count == 4 and not old.exists()


def test_missing_uv_has_an_install_hint(tmp_path, library, monkeypatch):
    monkeypatch.setattr(envs.shutil, "which", lambda name: None)
    with pytest.raises(RunError, match="uv is not on PATH; install uv"):
        resolve_interpreter(tmp_path, library)


@pytest.mark.parametrize("step", ["venv", "pip"])
def test_failed_uv_reports_command_and_exit_code_without_marking_success(
    step, tmp_path, library, uv
):
    run = uv.side_effect
    uv.side_effect = lambda command, **kwargs: (
        subprocess.CompletedProcess(command, 7) if command[1] == step else run(command, **kwargs)
    )
    with pytest.raises(RunError, match=f"uv {step} .* exited with code 7"):
        resolve_interpreter(tmp_path, library)
    assert not list(tmp_path.rglob(envs.MARKER))
    uv.side_effect = run
    assert Path(resolve_interpreter(tmp_path, library)).is_file()


@pytest.mark.parametrize("name", ["reference", "liar"])
def test_builtins_keep_the_running_interpreter(name, tmp_path, uv):
    manifest = load_shim_manifest(REPO / "shims" / name / "wiretruth-shim.json")
    assert resolve_interpreter(tmp_path, manifest) == sys.executable
    uv.assert_not_called()
    assert not (tmp_path / ".wiretruth").exists()


def test_javascript_is_untouched(tmp_path, library, uv):
    assert resolve_interpreter(tmp_path, replace(library, language="javascript")) is None
    uv.assert_not_called()
    assert not (tmp_path / ".wiretruth").exists()


@pytest.fixture
def javascript(tmp_path):
    shim = tmp_path / "shims/js/sample"
    shim.mkdir(parents=True)
    (shim / "package-lock.json").write_text('{"lockfileVersion":3}')
    return shim


@pytest.mark.parametrize("change", [None, "lockfile", "marker", "missing-marker", "modules"])
def test_javascript_install_marker_skip_and_rebuild(change, javascript, monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(envs.subprocess, "run", run)
    modules = build_javascript_env(javascript)
    command = ["npm", "ci", "--ignore-scripts", "--prefix", str(javascript)]
    run.assert_called_once_with(command, stdout=sys.stderr, check=False)
    marker = modules / envs.MARKER
    assert (
        marker.read_text()
        == hashlib.sha256((javascript / "package-lock.json").read_bytes()).hexdigest()
    )
    if change == "lockfile":
        (javascript / "package-lock.json").write_text('{"lockfileVersion":3,"name":"café"}')
    elif change == "marker":
        marker.write_text("partial installation")
    elif change == "missing-marker":
        marker.unlink()
    elif change == "modules":
        marker.unlink()
        modules.rmdir()
    assert build_javascript_env(javascript) == modules
    assert run.call_count == (1 if change is None else 2)


@pytest.mark.parametrize("failure", [7, FileNotFoundError("npm")])
def test_javascript_failed_install_is_not_marked_and_can_retry(failure, javascript, monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(envs.subprocess, "run", run)
    modules = build_javascript_env(javascript)
    (javascript / "package-lock.json").write_text('{"name":"京都"}')
    if isinstance(failure, int):
        run.return_value = subprocess.CompletedProcess([], failure)
    else:
        run.side_effect = failure
    with pytest.raises(RunError, match=r"exited with code 7|JavaScript environment"):
        build_javascript_env(javascript)
    assert not (modules / envs.MARKER).exists()
    run.side_effect = None
    run.return_value = subprocess.CompletedProcess([], 0)
    assert build_javascript_env(javascript) == modules


def test_javascript_packages_include_only_top_level_scoped_and_unscoped(javascript):
    modules = javascript / "node_modules"
    modules.mkdir()
    (modules / ".package-lock.json").write_text(
        json.dumps(
            {
                "packages": {
                    "": {"name": "café"},
                    "node_modules/ai": {"version": "7.0.142"},
                    "node_modules/@ai-sdk/openai": {"version": "4.0.95"},
                    "node_modules/ai/node_modules/zod": {"version": "3.0.0"},
                    "node_modules/@ai-sdk/openai/node_modules/ai": {"version": "1.0.0"},
                }
            }
        )
    )
    assert installed_javascript_packages(javascript) == {
        "ai": "7.0.142",
        "@ai-sdk/openai": "4.0.95",
    }


@pytest.mark.parametrize(
    "document",
    [None, "{", "[]", "{}", '{"packages":[]}', '{"packages":{"node_modules/ai":{"version":null}}}'],
)
def test_javascript_package_read_errors_are_run_errors(document, javascript):
    modules = javascript / "node_modules"
    modules.mkdir()
    if document is not None:
        (modules / ".package-lock.json").write_text(document)
    with pytest.raises(RunError, match="cannot read installed packages"):
        installed_javascript_packages(javascript)


def test_envs_cli_builds_javascript(tmp_path, javascript, monkeypatch, capsys):
    doc = json.loads((REPO / "tests/samples/valid/shim-manifest/vercel-ai.json").read_text())
    doc["name"] = "sample"
    (javascript / "wiretruth-shim.json").write_text(json.dumps(doc))
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(envs.subprocess, "run", run)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["envs", "--build", "sample"]) == 0
    assert capsys.readouterr().out == f"{javascript / 'node_modules'}\n"
    assert list_envs(tmp_path) == prune_envs(tmp_path) == ()
    run.assert_called_once()


def test_freeze_parser_keeps_resolved_names_and_versions():
    assert parse_installed_packages("sample-sdk==1.2.3\ndependency==2.0rc1+local\n") == {
        "sample-sdk": "1.2.3",
        "dependency": "2.0rc1+local",
    }
    assert parse_installed_packages("") == {}


@pytest.mark.parametrize("line", ["package", "package @ file:///tmp/pkg", "package==", "==1", "\n"])
def test_freeze_parser_rejects_unpinned_lines(line):
    with pytest.raises(RunError, match="expected name==version"):
        parse_installed_packages(line)


def test_installed_packages_freezes_the_selected_interpreter(monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "sample-sdk==1.2.3\n", ""))
    monkeypatch.setattr(envs.subprocess, "run", run)
    assert installed_packages("/env/bin/python") == {"sample-sdk": "1.2.3"}
    run.assert_called_once_with(
        ["uv", "pip", "freeze", "--python", "/env/bin/python"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("problem", ["exit", "missing-uv", "unpinned"])
def test_installed_packages_failures_are_run_errors(problem, monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 7, "", "failure"))
    if problem == "missing-uv":
        run.side_effect = FileNotFoundError("uv is not on PATH")
    elif problem == "unpinned":
        run.return_value = subprocess.CompletedProcess([], 0, "unpinned", "")
    monkeypatch.setattr(envs.subprocess, "run", run)
    with pytest.raises(RunError, match="uv pip freeze"):
        installed_packages("/env/bin/python")


def test_list_and_prune_keep_only_matching_manifests(tmp_path, library, uv):
    assert list_envs(tmp_path) == prune_envs(tmp_path) == ()
    current = Path(resolve_interpreter(tmp_path, library)).parent.parent
    stale = Path(
        resolve_interpreter(tmp_path, replace(library, packages={"sample-sdk": "1.0"}))
    ).parent.parent
    partial = current.parent / "unfinished-1"
    partial.mkdir()
    (current.parent / "not-a-directory").touch()
    assert list_envs(tmp_path) == ((stale, False), (current, True), (partial, False))
    assert prune_envs(tmp_path) == (stale, partial)
    assert list_envs(tmp_path) == ((current, True),)
    manifest_path = tmp_path / "shims/python/sample/wiretruth-shim.json"
    doc = json.loads(manifest_path.read_text())
    doc["packages"]["dependency"] = "2.0"
    manifest_path.write_text(json.dumps(doc))
    assert list_envs(tmp_path) == ((current, False),)


def test_find_library_rejects_unknown_and_ambiguous_names(tmp_path, library):
    assert find_library(tmp_path, "sample") == tmp_path / "shims/python/sample"
    with pytest.raises(RunError, match="no library shim named"):
        find_library(tmp_path, "*")
    duplicate = tmp_path / "shims/javascript/sample/wiretruth-shim.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.touch()
    with pytest.raises(RunError, match="multiple library shims"):
        find_library(tmp_path, "sample")


@pytest.mark.parametrize(
    "args",
    [
        ["run"],
        ["run", "--shim", ".", "--lib", "sample"],
        ["envs", "--list", "--prune"],
        ["envs", "--list", "--build", "sample"],
        ["envs", "--prune", "--build", "sample"],
        ["envs", "--build"],
    ],
)
def test_cli_flags_are_exclusive_and_require_arguments(args):
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize("command,flag", [("run", "--lib"), ("envs", "--build")])
def test_cli_unknown_library_exits_two(command, flag, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main([command, flag, "unknown"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(f"wiretruth {command}: no library shim named 'unknown'")


def test_run_lib_selects_the_shim(tmp_path, library, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = MagicMock(return_value=RunReport((), tmp_path))
    monkeypatch.setattr(cli, "run_suite", run)
    assert cli.main(["run", "--lib", "sample"]) == 0
    assert run.call_args.args == (Path(), Path("shims/python/sample"))


def test_envs_cli_build_list_default_and_prune(tmp_path, library, uv, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    interpreter = resolve_interpreter(tmp_path, library)
    current = Path(interpreter).parent.parent
    stale = current.parent / "old-1"
    stale.mkdir()
    capsys.readouterr()
    assert cli.main(["envs", "--build", "sample"]) == 0
    assert capsys.readouterr().out == f"{interpreter}\n"
    for flags in ([], ["--list"]):
        assert cli.main(["envs", *flags]) == 0
        assert capsys.readouterr().out == f"stale {stale}\ncurrent {current}\n"
    assert cli.main(["envs", "--prune"]) == 0
    assert capsys.readouterr().out == f"removed {stale}\n"
    assert current.is_dir() and not stale.exists()
    assert uv.call_count == 2


@pytest.mark.parametrize("problem", ["javascript", "invalid-manifest", "missing-uv", "os-error"])
def test_envs_cli_setup_errors_exit_two(problem, tmp_path, library, uv, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "shims/python/sample/wiretruth-shim.json"
    if problem == "javascript":
        doc = json.loads(path.read_text())
        doc["language"] = "javascript"
        path.write_text(json.dumps(doc))
    elif problem == "invalid-manifest":
        path.write_text("{}")
    elif problem == "missing-uv":
        monkeypatch.setattr(envs.shutil, "which", lambda name: None)
    else:
        uv.side_effect = OSError("cannot execute")
    assert cli.main(["envs", "--build", "sample"]) == 2
    assert capsys.readouterr().err.startswith("wiretruth envs: ")
