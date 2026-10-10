import json
import os
import shutil
from pathlib import Path

import pytest

from wiretruth.runner import closed_port, run_suite

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "mode,listed,grade,rule,requests",
    [
        ("hang-after", False, "HANG", 1, 1),
        ("hang-child", False, "HANG", 1, 1),
        ("hang-before", False, "HARNESS", 1, 0),
        ("crash-after", False, "CRASH", 2, 1),
        ("crash-before", False, "HARNESS", 2, 0),
        ("exit", False, "HARNESS", 2, 1),
        ("empty", False, "HARNESS", 2, 1),
        ("junk", False, "HARNESS", 3, 1),
        ("wrong-path", False, "HARNESS", 3, 1),
        ("wrong-stream", False, "HARNESS", 3, 1),
        ("unsupported", True, "UNSUPPORTED", 4, 0),
        ("unsupported", False, "HARNESS", 4, 0),
        ("internal", False, "CRASH", 5, 0),
        ("wrong", False, "WRONG", 6, 1),
        ("absent", False, "LOSSY", 7, 1),
        ("ok", False, "PASS", 8, 1),
    ],
)
def test_grading_through_real_processes(tmp_path, mode, listed, grade, rule, requests):
    if os.name != "posix" and (mode.startswith("crash") or mode == "hang-child"):
        pytest.skip("requires POSIX signals or process groups")
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    shim = tmp_path / "shim"
    shim.mkdir()
    shutil.copy(REPO / "tests" / "samples" / "shims" / "shim.py", shim)
    manifest = json.loads((REPO / "shims" / "reference" / "wiretruth-shim.json").read_text())
    manifest["env"] = {"MODE": mode}
    if listed:
        manifest["unsupported"] = [
            {
                "scenario": "truncation.text",
                "providers": ["openai-chat"],
                "reason": "No feature",
                "docs": "https://example.org/features",
            }
        ]
    (shim / "wiretruth-shim.json").write_text(json.dumps(manifest))
    report = run_suite(tmp_path, shim, pattern="truncation.text", jobs=1, timeout=1)
    (cell,) = report.cells
    assert (cell.grade.grade, cell.grade.rule, cell.request_count) == (grade, rule, requests)
    stderr = report.stderr_dir / "openai-chat.truncation.text.stderr"
    assert stderr.is_file()
    if mode == "exit":
        assert cell.observation is None
        assert cell.notes[-1] == "the shim exited with code 3"
        assert "sample stderr" in stderr.read_text()
    if mode in {"wrong-path", "wrong-stream"}:
        assert any(note.startswith("mismatch:") for note in cell.notes)
    if mode.startswith("hang"):
        assert "waiting" in stderr.read_text()
    if rule <= 4:
        assert cell.grade.requirements == ()


def test_reserved_port_refuses_connections_and_cannot_be_reused():
    import socket

    with closed_port() as port:
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1)
        with socket.socket() as other, pytest.raises(OSError):
            other.bind(("127.0.0.1", port))
