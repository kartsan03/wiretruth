from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest

from wiretruth import replay
from wiretruth.cli import main
from wiretruth.formats import json_path
from wiretruth.lint import lint
from wiretruth.secret_scan import PATTERNS, SECRET_HEADERS, scan_headers, scan_text

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "tests" / "samples" / "valid"

# Made-up values of the right shape. None of them is, or was, a real credential.
ALNUM = "Ab3dEf6hIj9kLm2nOp5qRs8t"
SECRETS = {
    "an sk- API key": f"sk-{ALNUM}",
    "an OpenAI project key prefix (sk-proj-)": "sk-proj-",
    "an Anthropic key prefix (sk-ant-)": "sk-ant-",
    "a Google API key (AIza…)": "AIza" + "x" * 35,
    "a bearer token": f"Bearer {ALNUM}",
    "an OpenAI organization id (org-…)": f"org-{ALNUM}",
    "an OpenAI project id (proj_…)": f"proj_{ALNUM}",
}


def test_every_pattern_has_a_case() -> None:
    assert set(SECRETS) == set(PATTERNS)


@pytest.mark.parametrize(("name", "secret"), SECRETS.items())
def test_each_pattern_is_caught(name: str, secret: str) -> None:
    text = f'{{\n  "note": "{secret}"\n}}'
    findings = scan_text(text)
    assert f"looks like {name}" in [finding.what for finding in findings]
    assert {finding.where for finding in findings} == {"line 2"}
    assert all(ALNUM not in str(finding) for finding in findings)


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: bearer {ALNUM}",
        f"key=sk-{ALNUM}",
        f'"sk-proj-{ALNUM}"',
        f"x-goog-api-key: AIza{'Q' * 35}",
        f'"Key:\\nsk-{ALNUM}"',  # after a JSON escape
        f'"\\tproj_{ALNUM}"',
        f'"\\u00a0org-{ALNUM}"',
        '"data: {\\"content\\":\\"x\\\\nsk-ant-"',  # an escape inside an escaped event
    ],
)
def test_secrets_in_context(text: str) -> None:
    assert scan_text(text)


def test_the_replay_log_leaves_out_the_same_headers() -> None:
    assert replay.REDACTED_HEADERS is SECRET_HEADERS


@pytest.mark.parametrize(
    "text",
    [
        "desk-based-research-notes-and-more",  # "sk-" inside a word
        "task-specific-instructions-for-agents",
        "Bearer ***",  # how --dry-run prints the key
        "Bearer short",
        f"sk-{'a' * 15}",  # one character too short
        "chatcmpl-B9MBs8CjcvOU2jLn4n570S5qMJKcT",
        "fp_0123456789abcdef call_Ab3dEf6hIj9kLm2nOp5qRs8t resp_Ab3dEf6hIj9kLm2nOp5qRs8t",
        "Київ — the capital of Ukraine",
    ],
)
def test_ordinary_text_passes(text: str) -> None:
    assert scan_text(text) == []


def test_findings_are_in_order() -> None:
    text = f"org-{ALNUM}\nsk-{ALNUM}\n"
    assert [str(finding) for finding in scan_text(text)] == [
        "line 1: looks like an OpenAI organization id (org-…)",
        "line 2: looks like an sk- API key",
    ]


@pytest.mark.parametrize("name", sorted(SECRET_HEADERS))
def test_credential_header_names_anywhere(name: str) -> None:
    doc = {"a": [{"headers": {"content-type": "x", name.title(): "v"}}]}
    (finding,) = scan_headers(doc)
    assert finding.where == json_path(["a", 0, "headers", name.title()])
    assert finding.what == f"the {name} header carries credentials"


def test_header_values_and_other_objects_pass() -> None:
    assert scan_headers({"headers": {"content-type": "authorization"}}) == []
    assert scan_headers({"authorization": "x", "headers": "authorization"}) == []
    assert scan_headers([1, "headers", None]) == []


# --- In wiretruth lint ----------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A suite with one valid openai-chat fixture and its scenario."""
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy(REPO / "suite" / "bug-classes.json", suite)
    scenario = json.loads((SAMPLES / "scenario" / "structured-output.json").read_text())
    scenario["id"] = "truncation.text-stream"
    (suite / "scenarios" / "truncation").mkdir(parents=True)
    (suite / "scenarios" / "truncation" / "text-stream.json").write_text(json.dumps(scenario))
    (suite / "fixtures" / "openai-chat").mkdir(parents=True)
    shutil.copy(
        SAMPLES / "fixture" / "recorded-sse.json",
        suite / "fixtures" / "openai-chat" / "truncation.text-stream.json",
    )
    return tmp_path


def _fixture_path(root: Path) -> Path:
    return root / "suite" / "fixtures" / "openai-chat" / "truncation.text-stream.json"


def test_lint_finds_a_key_in_a_fixture_without_printing_it(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    doc["provenance"]["notes"] = f"recorded with sk-{ALNUM}"
    text = json.dumps(doc, indent=2)
    path.write_text(text)
    line = next(n for n, found in enumerate(text.splitlines(), 1) if '"notes"' in found)
    assert main(["lint", str(root)]) == 1
    out = capsys.readouterr().out
    assert ALNUM not in out
    assert (
        f"{path}: line {line}: looks like an sk- API key; remove it, and revoke the credential if "
        "it is real" in out
    )


def test_lint_finds_a_key_in_any_file(root: Path) -> None:
    scenario = root / "suite" / "scenarios" / "truncation" / "text-stream.json"
    scenario.write_text(scenario.read_text().replace("List ten", f"Bearer {ALNUM} List ten"))
    problems = [str(problem) for problem in lint(root).problems]
    assert problems == [
        f"{scenario}: line 1: looks like a bearer token; remove it, and revoke the credential if "
        "it is real"
    ]


def test_lint_finds_a_key_inside_chunks(root: Path) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    raw = f"data: Bearer {ALNUM}\n\n".encode()
    parts = [base64.b64encode(piece).decode() for piece in (raw[:12], raw[12:])]
    doc["exchanges"][0]["response"]["body"] = {"chunks_b64": parts}
    path.write_text(json.dumps(doc, indent=2))
    messages = [problem.message for problem in lint(root).problems]
    assert (
        "looks like a bearer token (decoded, line 1); remove it, and revoke the credential if it "
        "is real" in messages
    )


def test_lint_finds_a_credential_header_in_the_request(root: Path) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    doc["provenance"]["request"]["headers"] = {"Authorization": "Bearer ***"}
    path.write_text(json.dumps(doc, indent=2))
    problems = [(problem.where, problem.message) for problem in lint(root).problems]
    assert (
        "$.provenance.request.headers.Authorization",
        "the authorization header carries credentials; remove it, and revoke the credential if "
        "it is real",
    ) in problems


def test_lint_skips_chunks_the_schema_rejects(root: Path) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    doc["exchanges"][0]["response"]["body"] = {"chunks_b64": ["not base64!", 7]}
    path.write_text(json.dumps(doc, indent=2))
    problems = lint(root).problems
    assert problems and all("looks like" not in problem.message for problem in problems)


def test_lint_scans_a_file_that_isnt_json(root: Path) -> None:
    path = root / "suite" / "fixtures" / "openai-chat" / "truncation.text-stream.json"
    path.write_bytes(f"not json sk-{ALNUM} \xff".encode("latin-1"))
    messages = [problem.message for problem in lint(root).problems]
    assert any(message.startswith("looks like an sk- API key") for message in messages)


def test_the_repository_has_no_secrets() -> None:
    assert [problem for problem in lint(REPO).problems if "looks like" in problem.message] == []


def test_lint_scans_files_that_arent_json(root: Path) -> None:
    notes = root / "suite" / "fixtures" / "openai-chat" / "notes.txt"
    notes.write_text(f"recorded with\nsk-{ALNUM}\n")
    report = lint(root)
    assert [str(problem) for problem in report.problems] == [
        f"{notes}: line 2: looks like an sk- API key; remove it, and revoke the credential if it "
        "is real"
    ]
    assert report.files == 4


@pytest.mark.parametrize("name", ["truncation.text-stream.json", "notes.txt"])
def test_lint_reports_a_file_it_cant_read_once(root: Path, name: str) -> None:
    path = root / "suite" / "fixtures" / "openai-chat" / name
    path.touch()
    path.chmod(0)
    try:
        if _readable(path):
            pytest.skip("running as a user who can read any file")
        problems = [str(problem) for problem in lint(root).problems if problem.file == str(path)]
    finally:
        path.chmod(0o644)
    assert problems == [f"{path}: $: cannot read the file: Permission denied"]


def _readable(path: Path) -> bool:
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def test_a_truth_difference_never_shows_a_secret(root: Path) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    event = doc["exchanges"][0]["response"]["body"]["sse"][1]
    doc["exchanges"][0]["response"]["body"]["sse"][1] = event.replace(
        "was founded", f"key sk-{ALNUM}"
    )
    path.write_text(json.dumps(doc, indent=2))
    report = lint(root)
    assert all(ALNUM not in str(problem) for problem in report.problems)
    assert (
        "$.truth.text",
        "doesn't match the exchanges, which give a value the secret scan flags (not shown)",
    ) in [(problem.where, problem.message) for problem in report.problems]
