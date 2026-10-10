import shutil
from pathlib import Path

import pytest

from wiretruth.runner import compare_grades, load_expected_grades, run_suite

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["reference", "liar"])
def test_builtin_shim_against_real_suite(name, tmp_path):
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    report = run_suite(tmp_path, REPO / "shims" / name, jobs=3)
    expected = load_expected_grades(REPO / "tests" / "expected-grades" / f"{name}.json")
    assert compare_grades(report.cells, expected) == ()
    assert all(cell.request_count == 1 for cell in report.cells)
    assert all(cell.observation is not None for cell in report.cells)
    assert len(list(report.stderr_dir.glob("*.stderr"))) == len(expected)
