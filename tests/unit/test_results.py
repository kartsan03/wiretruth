import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from wiretruth import __version__, cli, results
from wiretruth.envs import RunError
from wiretruth.formats import (
    FormatError,
    Problem,
    load_observation,
    load_shim_manifest,
    observation_document,
    validate,
)
from wiretruth.grading import CellGrade, RequirementResult
from wiretruth.results import (
    collect_results,
    load_results_grades,
    package_changes,
    results_document,
    results_path,
    summary_lines,
    write_results,
)
from wiretruth.runner import GLYPHS, CellResult, ExpectedGrade, RunReport, compare_grades

REPO = Path(__file__).resolve().parents[2]
AT = datetime(2026, 10, 10, 15, 4, 5, 123456, tzinfo=timezone(timedelta(hours=2)))
SHA = "f7033ff"


@pytest.fixture
def manifest():
    manifest = load_shim_manifest(REPO / "shims/reference/wiretruth-shim.json")
    return replace(
        manifest,
        name="sample",
        packages={"dependency": "2.0", "sample-sdk": "1.2.3"},
        primary_package="sample-sdk",
    )


@pytest.fixture
def cell():
    observation = load_observation(REPO / "tests/samples/valid/observation/returned.json")
    observation = replace(observation, text=replace(observation.text, value="Crème brûlée"))
    return CellResult(
        "truncation.text",
        "openai-chat",
        "openai-chat/truncation.text",
        "recorded",
        CellGrade(
            "PASS",
            8,
            (
                RequirementResult("finish-length", "MUST", "SATISFIED", "length is exposed"),
                RequirementResult("usage", "SHOULD", "NOT_OBSERVABLE", "usage is absent"),
            ),
            ("grade note",),
        ),
        observation,
        2,
        ("cell note",),
    )


@pytest.fixture
def report(cell):
    return RunReport(
        (cell, replace(cell, scenario="truncation.structured-output", observation=None)),
        Path(".wiretruth/runs/stamp"),
    )


@pytest.fixture
def document(report, manifest):
    return results_document(
        report,
        manifest,
        at=AT,
        git_sha=SHA,
        ci_run=None,
        packages={"sample-sdk": "1.2.3", "dependency": "2.0", "transitive": "3.0"},
    )


def test_results_builder_metadata_cells_and_observations(document, report):
    assert validate("results", document, "results") == []
    assert set(document) == {"format", "suite_version", "library", "run", "cells"}
    assert document["format"] == "wiretruth.results/v1"
    assert document["suite_version"] == __version__
    assert document["library"] == {"name": "sample", "version": "1.2.3", "language": "python"}
    assert document["run"] == {
        "at": "2026-10-10T13:04:05Z",
        "git_sha": SHA,
        "ci_run": None,
        "runner": f"wiretruth {__version__}",
        "packages": {"sample-sdk": "1.2.3", "dependency": "2.0", "transitive": "3.0"},
    }
    assert [item["scenario"] for item in document["cells"]] == [c.scenario for c in report.cells]
    assert document["cells"][0] == {
        "scenario": "truncation.text",
        "provider": "openai-chat",
        "fixture": "openai-chat/truncation.text",
        "fixture_tier": "recorded",
        "grade": "PASS",
        "requirements": [
            {
                "id": "finish-length",
                "level": "MUST",
                "status": "SATISFIED",
                "evidence": "length is exposed",
            },
            {
                "id": "usage",
                "level": "SHOULD",
                "status": "NOT_OBSERVABLE",
                "evidence": "usage is absent",
            },
        ],
        "notes": ["cell note"],
        "observation": observation_document(report.cells[0].observation),
        "request_count": 2,
    }
    assert document["cells"][1]["observation"] is None


def test_results_path_is_shared_by_writer_and_reader(tmp_path, document, report):
    assert results_path(Path(), "sample", "1.2.3") == Path("results/sample/1.2.3.json")
    path = results_path(tmp_path, "sample", "1.2.3")
    assert write_results(tmp_path, document) == path
    assert path.read_bytes() == (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    assert "Crème brûlée" in path.read_text(encoding="utf-8")
    saved = load_results_grades(path)
    assert (saved.name, saved.version) == ("sample", "1.2.3")
    assert saved.packages == document["run"]["packages"]
    assert saved.grades == {
        (cell.scenario, cell.provider): ExpectedGrade(
            "PASS", {"finish-length": "SATISFIED", "usage": "NOT_OBSERVABLE"}
        )
        for cell in report.cells
    }
    assert compare_grades(report.cells, saved.grades) == ()
    document["cells"][0]["grade"] = "WRONG"
    assert write_results(tmp_path, document) == path
    assert load_results_grades(path).grades[("truncation.text", "openai-chat")].grade == "WRONG"


@pytest.mark.parametrize("packages", [None, [], {"sample": ""}, {"sample": 1}])
def test_results_packages_are_required_nonempty_strings(packages, document, tmp_path):
    if packages is None:
        del document["run"]["packages"]
    else:
        document["run"]["packages"] = packages
    with pytest.raises(FormatError, match="packages"):
        write_results(tmp_path, document)
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("text", ["{}", "invalid", '{"format": 1, "format": 2}'])
def test_results_reader_validates_before_extracting_grades(text, tmp_path):
    path = tmp_path / "results.json"
    path.write_text(text)
    with pytest.raises(FormatError):
        load_results_grades(path)


def test_results_reader_rejects_duplicate_cells(document, tmp_path):
    document["cells"].append(document["cells"][0])
    path = write_results(tmp_path, document)
    with pytest.raises(FormatError) as error:
        load_results_grades(path)
    assert error.value.problems == (
        Problem(str(path), "$.cells[2]", "duplicate cell truncation.text openai-chat"),
    )


@pytest.mark.parametrize(
    "missing", [None, "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"]
)
def test_collect_results_captures_checkout_time_ci_and_packages(
    missing, tmp_path, report, manifest, monkeypatch
):
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, SHA + "\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    resolve = MagicMock(return_value="/env/bin/python")
    freeze = MagicMock(return_value={"sample-sdk": "1.2.3", "dependency": "2.0"})
    monkeypatch.setattr(results.subprocess, "run", run)
    monkeypatch.setattr(results, "resolve_interpreter", resolve)
    monkeypatch.setattr(results, "installed_packages", freeze)
    for name, value in {
        "GITHUB_SERVER_URL": "https://github.example.org",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_RUN_ID": "42",
    }.items():
        monkeypatch.setenv(name, value)
    if missing:
        monkeypatch.delenv(missing)
    before = datetime.now(UTC).replace(microsecond=0)
    document = collect_results(tmp_path, report, manifest)
    after = datetime.now(UTC)
    assert before <= datetime.fromisoformat(document["run"]["at"]) <= after
    assert document["run"]["ci_run"] == (
        "https://github.example.org/example/project/actions/runs/42" if missing is None else None
    )
    assert document["run"]["git_sha"] == SHA
    assert document["run"]["packages"] == freeze.return_value
    assert run.call_args_list == [
        call(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=False
        ),
        call(
            ["git", "status", "--porcelain", "--", ".", ":(exclude)results"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        ),
    ]
    resolve.assert_called_once_with(tmp_path, manifest)
    freeze.assert_called_once_with("/env/bin/python")


@pytest.mark.parametrize(
    "problem", ["exit", "missing-git", "status-exit", "status-oserror", "javascript"]
)
def test_collect_results_setup_errors(problem, tmp_path, report, manifest, monkeypatch):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 128, "", "not a checkout"))
    if problem == "missing-git":
        run.side_effect = FileNotFoundError("git")
    elif problem in {"status-exit", "status-oserror", "javascript"}:
        status = subprocess.CompletedProcess([], 0, "", "")
        if problem == "status-exit":
            status = subprocess.CompletedProcess([], 128, "", "not a checkout")
        elif problem == "status-oserror":
            status = OSError("cannot execute git")
        else:
            manifest = replace(manifest, language="javascript")
        run.side_effect = [subprocess.CompletedProcess([], 0, SHA, ""), status]
    monkeypatch.setattr(results.subprocess, "run", run)
    match = "Python shim environment" if problem == "javascript" else "needs a git checkout"
    with pytest.raises(RunError, match=match):
        collect_results(tmp_path, report, manifest)


@pytest.mark.parametrize("changed", [None, "notes.txt", "results/sample/1.2.3.json"])
def test_collect_results_requires_clean_checkout_except_results(
    changed, tmp_path, report, manifest, monkeypatch
):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    if changed is not None:
        path = tmp_path / changed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed")
    git = results._git
    # The temporary checkout has no commits; use real Git for the status and path exclusion.
    monkeypatch.setattr(
        results,
        "_git",
        lambda root, *args: SHA if args == ("rev-parse", "HEAD") else git(root, *args),
    )
    resolve = MagicMock(return_value="/env/bin/python")
    freeze = MagicMock(return_value={"sample-sdk": "1.2.3"})
    monkeypatch.setattr(results, "resolve_interpreter", resolve)
    monkeypatch.setattr(results, "installed_packages", freeze)
    if changed == "notes.txt":
        with pytest.raises(RunError) as error:
            collect_results(tmp_path, report, manifest)
        assert str(error.value) == (
            "--write-results needs a clean checkout outside results/; commit or stash: notes.txt"
        )
        resolve.assert_not_called()
        freeze.assert_not_called()
    else:
        assert collect_results(tmp_path, report, manifest)["run"]["git_sha"] == SHA
        freeze.assert_called_once_with("/env/bin/python")


def test_collect_results_names_first_dirty_path(tmp_path, report, manifest, monkeypatch):
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess([], 0, SHA, ""),
            subprocess.CompletedProcess([], 0, " M src/changed file.py\n?? notes.txt\n", ""),
        ]
    )
    monkeypatch.setattr(results.subprocess, "run", run)
    with pytest.raises(RunError) as error:
        collect_results(tmp_path, report, manifest)
    assert str(error.value) == (
        "--write-results needs a clean checkout outside results/; "
        "commit or stash: src/changed file.py"
    )


@pytest.mark.parametrize(
    "saved,current,expected",
    [
        ({"pydantic": "2.14.0"}, {"pydantic": "2.15.0"}, ("pydantic 2.14.0 → 2.15.0",)),
        ({}, {"newpkg": "1.0"}, ("+newpkg 1.0",)),
        ({"oldpkg": "2.0"}, {}, ("-oldpkg 2.0",)),
        ({"same": "1.0"}, {"same": "1.0"}, ()),
        ({}, {}, ()),
        (
            {"pydantic": "2.14.0", "oldpkg": "2.0", "same": "1.0"},
            {"pydantic": "2.15.0", "same": "1.0", "newpkg": "1.0"},
            ("+newpkg 1.0", "-oldpkg 2.0", "pydantic 2.14.0 → 2.15.0"),
        ),
    ],
)
def test_package_changes_are_sorted_notes(saved, current, expected):
    assert package_changes(saved, current) == expected


def test_summary_sorts_rows_and_columns_keeps_blank_cells_and_note_order(cell, manifest):
    first = replace(
        cell,
        notes=("the shim exited with code 3",),
        observation=replace(cell.observation, notes=("effort not sent",)),
    )
    second = replace(
        cell,
        provider="gemini",
        notes=(),
        observation=replace(cell.observation, notes=("usage absent",)),
    )
    third = replace(cell, scenario="truncation.structured-output", notes=(), observation=None)
    report = RunReport((first, second, third), Path(".wiretruth/runs/stamp"))
    lines = summary_lines(report, manifest)
    assert lines == (
        "scenario                      gemini  openai-chat",
        "truncation.structured-output          ✓ PASS",
        "truncation.text               ✓ PASS  ✓ PASS",
        "",
        "truncation.text openai-chat",
        "  the shim exited with code 3",
        "  shim: effort not sent",
        "",
        "truncation.text gemini",
        "  shim: usage absent",
        "",
        "sample 1.2.3: 3 cells, 3 PASS",
        "stderr: .wiretruth/runs/stamp",
    )
    assert all(line == line.rstrip() for line in lines)


def test_summary_singular_and_no_notes_section(cell, manifest):
    cell = replace(cell, notes=(), observation=None)
    assert summary_lines(RunReport((cell,), Path("logs")), manifest) == (
        "scenario         openai-chat",
        "truncation.text  ✓ PASS",
        "",
        "sample 1.2.3: 1 cell, 1 PASS",
        "stderr: logs",
    )


def test_summary_totals_follow_glyph_order_and_omit_zeros(cell, manifest):
    cells = tuple(
        replace(
            cell,
            scenario=f"sample.cell-{index}",
            grade=replace(cell.grade, grade=grade),
            notes=(),
            observation=None,
        )
        for index, grade in enumerate(reversed(GLYPHS))
    )
    lines = summary_lines(RunReport(cells, Path("logs")), manifest)
    for index, grade in enumerate(reversed(GLYPHS)):
        assert lines[index + 1].endswith(f"{GLYPHS[grade]} {grade}")
    assert lines[-2] == (
        "sample 1.2.3: 7 cells, 1 PASS, 1 LOSSY, 1 WRONG, 1 CRASH, 1 HANG, 1 UNSUPPORTED, 1 HARNESS"
    )
    assert summary_lines(RunReport((), Path("logs")), manifest)[-2] == "sample 1.2.3: 0 cells"


@pytest.fixture
def run_cli(tmp_path, manifest, report, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shim = tmp_path / "shims/python/sample"
    shim.mkdir(parents=True)
    document = json.loads((REPO / "shims/reference/wiretruth-shim.json").read_text())
    document.update(
        name=manifest.name,
        packages=dict(manifest.packages),
        primary_package=manifest.primary_package,
    )
    (shim / "wiretruth-shim.json").write_text(json.dumps(document))
    run = MagicMock(return_value=report)
    monkeypatch.setattr(cli, "run_suite", run)
    monkeypatch.setattr(cli, "resolve_interpreter", MagicMock(return_value="/env/bin/python"))
    monkeypatch.setattr(
        cli,
        "installed_packages",
        MagicMock(return_value={**manifest.packages, "transitive": "3.0"}),
    )
    return run


@pytest.mark.parametrize(
    "flags",
    [
        ["--write-results", "--check-against"],
        ["--write-results", "--check-grades", "grades.json"],
        ["--check-against", "--check-grades", "grades.json"],
    ],
)
def test_results_cli_flags_are_mutually_exclusive(flags, run_cli):
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "--lib", "sample", *flags])
    assert error.value.code == 2
    run_cli.assert_not_called()


@pytest.mark.parametrize("flag", ["--write-results", "--check-against", "--check-grades"])
@pytest.mark.parametrize(
    "filters",
    [["--scenario", "truncation.text"], ["--provider", "openai-chat"]],
)
def test_results_cli_filters_exit_two_before_running(flag, filters, run_cli, capsys):
    flags = [flag, "grades.json"] if flag == "--check-grades" else [flag]
    assert cli.main(["run", "--lib", "sample", *flags, *filters]) == 2
    assert (
        capsys.readouterr().err
        == f"wiretruth run: {flag} runs every cell; drop --scenario and --provider\n"
    )
    run_cli.assert_not_called()


def test_write_results_requires_lib(run_cli, capsys):
    assert cli.main(["run", "--shim", "shims/python/sample", "--write-results"]) == 2
    assert "results files are for library shims" in capsys.readouterr().err
    run_cli.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
def test_check_against_missing_file_exits_two_with_write_hint(explicit, run_cli, capsys):
    flags = ["--check-against", "missing.json"] if explicit else ["--check-against"]
    assert cli.main(["run", "--lib", "sample", *flags]) == 2
    error = capsys.readouterr().err
    assert ("missing.json" if explicit else "results/sample/1.2.3.json") in error
    assert "write it with --write-results" in error
    run_cli.assert_not_called()


@pytest.mark.parametrize("field,value", [("name", "other"), ("version", "2.0")])
def test_check_against_wrong_library_exits_two_before_running(
    field, value, run_cli, document, tmp_path, capsys
):
    document["library"][field] = value
    path = write_results(tmp_path, document)
    assert cli.main(["run", "--lib", "sample", "--check-against", str(path)]) == 2
    assert "expected sample 1.2.3" in capsys.readouterr().err
    run_cli.assert_not_called()


@pytest.mark.parametrize("problem", ["json", "schema", "packages", "duplicate"])
def test_check_against_invalid_results_fail_before_running(
    problem, run_cli, document, tmp_path, capsys
):
    path = tmp_path / "expected.json"
    if problem == "packages":
        del document["run"]["packages"]
    elif problem == "schema":
        document["cells"][0]["grade"] = "MAYBE"
    elif problem == "duplicate":
        document["cells"].append(document["cells"][0])
    path.write_text("invalid" if problem == "json" else json.dumps(document))
    assert cli.main(["run", "--lib", "sample", "--check-against", str(path)]) == 1
    captured = capsys.readouterr()
    assert str(path) in captured.err and captured.out == ""
    run_cli.assert_not_called()


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("change", [None, "grade", "status", "missing", "extra"])
def test_check_against_uses_existing_comparer_and_prints_differences_last(
    explicit, change, run_cli, document, tmp_path, capsys
):
    if change == "grade":
        document["cells"][0]["grade"] = "WRONG"
    elif change == "status":
        document["cells"][0]["requirements"][0]["status"] = "CONTRADICTED"
    elif change == "missing":
        document["cells"].append({**document["cells"][0], "scenario": "truncation.text-stream"})
    elif change == "extra":
        document["cells"].pop()
    path = write_results(tmp_path, document)
    flags = ["--check-against"]
    if explicit:
        target = tmp_path / "baseline.json"
        path.rename(target)
        flags.append(str(target))
        path = target
    assert cli.main(["run", "--lib", "sample", *flags]) == (0 if change is None else 1)
    run_cli.assert_called_once_with(
        Path(), Path("shims/python/sample"), pattern="*", provider=None, jobs=cli.DEFAULT_JOBS
    )
    report = run_cli.return_value
    expected_lines = summary_lines(
        report, load_shim_manifest("shims/python/sample/wiretruth-shim.json")
    )
    differences = compare_grades(report.cells, load_results_grades(path).grades)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [*expected_lines, *differences]


@pytest.mark.parametrize("different_grade", [False, True])
def test_check_against_package_drift_is_a_note_before_grade_differences(
    different_grade, run_cli, document, tmp_path, capsys
):
    if different_grade:
        document["cells"][0]["grade"] = "WRONG"
    path = write_results(tmp_path, document)
    cli.installed_packages.return_value = {
        "sample-sdk": "1.2.3",
        "transitive": "4.0",
        "newpkg": "1.0",
    }
    assert cli.main(["run", "--lib", "sample", "--check-against", str(path)]) == int(
        different_grade
    )
    lines = capsys.readouterr().out.splitlines()
    index = lines.index("stderr: .wiretruth/runs/stamp")
    assert lines[index + 1 :] == [
        f"packages differ from {path}: -dependency 2.0, +newpkg 1.0, transitive 3.0 → 4.0",
        *compare_grades(run_cli.return_value.cells, load_results_grades(path).grades),
    ]
    cli.resolve_interpreter.assert_called_once()
    cli.installed_packages.assert_called_once_with("/env/bin/python")


@pytest.mark.parametrize("interpreter", ["/env/bin/python", None])
def test_check_against_skips_package_note_for_matching_packages_or_no_interpreter(
    interpreter, run_cli, document, tmp_path, capsys
):
    path = write_results(tmp_path, document)
    cli.resolve_interpreter.return_value = interpreter
    assert cli.main(["run", "--lib", "sample", "--check-against", str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == "" and "packages differ" not in captured.out
    if interpreter is None:
        cli.installed_packages.assert_not_called()
    else:
        cli.installed_packages.assert_called_once_with(interpreter)


def test_check_against_freeze_failure_exits_two(run_cli, document, tmp_path, capsys):
    path = write_results(tmp_path, document)
    cli.installed_packages.side_effect = RunError("uv pip freeze exited with code 7")
    assert cli.main(["run", "--lib", "sample", "--check-against", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == "wiretruth run: uv pip freeze exited with code 7\n"
    assert "stderr: .wiretruth/runs/stamp" in captured.out
    assert "packages differ" not in captured.out


def test_write_results_cli_writes_file_after_summary(
    run_cli, document, tmp_path, monkeypatch, capsys
):
    collect = MagicMock(return_value=document)
    monkeypatch.setattr(cli, "collect_results", collect)
    assert cli.main(["run", "--lib", "sample", "--write-results"]) == 0
    path = results_path(tmp_path, "sample", "1.2.3")
    assert json.loads(path.read_text()) == document
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "stderr: .wiretruth/runs/stamp",
        "wrote results/sample/1.2.3.json",
    ]
    collect.assert_called_once()
    assert collect.call_args.args[:2] == (Path(), run_cli.return_value)


@pytest.mark.parametrize("count", [1, 2])
def test_write_results_refuses_harness_cells_without_overwriting(
    count, run_cli, report, document, tmp_path, monkeypatch, capsys
):
    path = write_results(tmp_path, document)
    before = path.read_bytes()
    cells = tuple(
        replace(cell, grade=replace(cell.grade, grade="HARNESS")) if index < count else cell
        for index, cell in enumerate(report.cells)
    )
    run_cli.return_value = replace(report, cells=cells)
    collect = MagicMock()
    write = MagicMock()
    monkeypatch.setattr(cli, "collect_results", collect)
    monkeypatch.setattr(cli, "write_results", write)
    assert cli.main(["run", "--lib", "sample", "--write-results"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        f"wiretruth run: --write-results: {count} HARNESS cell{'' if count == 1 else 's'}; "
        "fix the shim or the harness first\n"
    )
    assert "wrote " not in captured.out
    collect.assert_not_called()
    write.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize("problem", ["git", "freeze", "invalid", "io"])
def test_write_results_cli_failures_have_expected_exit_codes(
    problem, run_cli, document, monkeypatch, capsys
):
    collect = MagicMock(return_value=document)
    monkeypatch.setattr(cli, "collect_results", collect)
    if problem in {"git", "freeze"}:
        collect.side_effect = RunError(
            "--write-results needs a git checkout"
            if problem == "git"
            else "uv pip freeze exited with code 7"
        )
    elif problem == "invalid":
        document["run"]["packages"] = {"sample-sdk": ""}
    else:
        monkeypatch.setattr(cli, "write_results", MagicMock(side_effect=OSError("cannot write")))
    assert cli.main(["run", "--lib", "sample", "--write-results"]) == (
        1 if problem == "invalid" else 2
    )
    captured = capsys.readouterr()
    assert captured.err and "wrote " not in captured.out
