import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from wiretruth.envs import MARKER
from wiretruth.runner import closed_port, run_suite

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_dependency_free_javascript_shim_runs_in_its_directory(tmp_path):
    shutil.copytree(REPO / "suite", tmp_path / "suite")
    shim = tmp_path / "shims/js/sample"
    modules = shim / "node_modules"
    modules.mkdir(parents=True)
    manifest = json.loads((REPO / "tests/samples/valid/shim-manifest/vercel-ai.json").read_text())
    manifest.update(
        name="sample",
        packages={"sample-sdk": "1.0"},
        primary_package="sample-sdk",
        env={},
        unsupported=[],
    )
    (shim / "wiretruth-shim.json").write_text(json.dumps(manifest))
    (shim / "package.json").write_text('{"private":true,"type":"module","dependencies":{}}')
    lock = b'{"lockfileVersion":3,"packages":{"":{"dependencies":{}}}}'
    (shim / "package-lock.json").write_bytes(lock)
    (modules / MARKER).write_text(hashlib.sha256(lock).hexdigest())
    observation = json.loads((REPO / "tests/samples/valid/observation/returned.json").read_text())
    observation.update(
        library={"name": "sample", "version": "1.0", "language": "javascript", "api": "fetch"},
        tool_calls=[],
        text={"value": None, "surface": "absent"},
        stream={"used": False, "completed": None, "chunks_seen": None},
    )
    (shim / "observation.json").write_text(json.dumps(observation))
    (shim / "shim.mjs").write_text("""
import { readFileSync } from 'node:fs';
const call = JSON.parse(readFileSync(0, 'utf8'));
const response = await fetch(`${process.env.WIRETRUTH_BASE_URL_OPENAI_CHAT}/chat/completions`, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ model: call.model, messages: call.messages,
    max_completion_tokens: call.max_output_tokens }),
});
const result = await response.json();
const observation = JSON.parse(readFileSync('observation.json', 'utf8'));
observation.finish.value = result.choices[0].finish_reason;
console.log(JSON.stringify(observation));
""")
    report = run_suite(tmp_path, shim, pattern="request.max-tokens-field", jobs=1)
    (cell,) = report.cells
    assert (cell.grade.grade, cell.request_count) == ("PASS", 1)
    assert cell.observation.library.language == "javascript"


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
        ("wrong-base-url", False, "HARNESS", 3, 0),
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
    # Only the hanging modes need a short limit; the others get time for a slow interpreter start.
    timeout = 1 if mode.startswith("hang") else 10
    report = run_suite(
        tmp_path, shim, pattern="truncation.text", provider="openai-chat", jobs=1, timeout=timeout
    )
    (cell,) = report.cells
    assert (cell.grade.grade, cell.grade.rule, cell.request_count) == (grade, rule, requests)
    stderr = report.stderr_dir / "openai-chat.truncation.text.stderr"
    assert stderr.is_file()
    if mode == "exit":
        assert cell.observation is None
        assert cell.notes[-1] == "the shim exited with code 3"
        assert "sample stderr" in stderr.read_text()
    if mode in {"wrong-path", "wrong-stream", "wrong-base-url"}:
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
