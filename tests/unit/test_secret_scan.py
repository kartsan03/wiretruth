from __future__ import annotations

import base64
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from wiretruth import replay
from wiretruth.cli import main
from wiretruth.formats import JsonValue, json_path
from wiretruth.lint import lint
from wiretruth.secret_scan import (
    MAX_LAYERS,
    PATTERNS,
    SECRET_HEADERS,
    layers,
    scan_headers,
    scan_text,
)

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "tests" / "samples" / "valid"

# Made-up values of the right shape. None of them is, or was, a real credential.
ALNUM = "Ab3dEf6hIj9kLm2nOp5qRs8t"
SECRETS = {
    "an sk- API key": f"sk-{ALNUM}",
    "an OpenAI project key (sk-proj prefix)": "sk-proj-",
    "an Anthropic key (sk-ant prefix)": "sk-ant-",
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


def test_layers_decode_one_level_of_escapes_at_a_time() -> None:
    text = r'"a\\u0073k \u0073 \"q\" \/ \t \\n \n \u000A"'
    assert list(layers(text)) == [
        text,
        r'"a\u0073k s "q" / ' + "\t" + r' \n \n \u000A"',
        r'"ask s "q" / ' + "\t" + r' \n \n \u000A"',
    ]


@pytest.mark.parametrize(
    "text",
    [
        rf'"\u0073k-{ALNUM}"',  # a fixture's notes
        rf'"data: {{\"x\":\"\\u0073k-{ALNUM}\"}}"',  # an event in a fixture: two layers
        rf'"s\u006B-{ALNUM}"',
        rf'"Bearer\u0020{ALNUM}"',
        rf'"\u0041Iza{"x" * 35}"',
        rf'"\u006frg-{ALNUM}"',
    ],
)
def test_secrets_behind_json_escapes(text: str) -> None:
    findings = scan_text(text)
    assert findings
    assert all(finding.what.endswith(", JSON-escaped") for finding in findings)
    assert all(ALNUM not in str(finding) for finding in findings)


def test_a_line_number_holds_in_every_layer() -> None:
    text = r'{"a": "x\ny",' + "\n" + rf' "b": "\u000a\u0073k-{ALNUM}"}}'
    assert [str(finding) for finding in scan_text(text)] == [
        "line 2: looks like an sk- API key, JSON-escaped"
    ]


def test_a_plain_secret_is_reported_once() -> None:
    text = rf'"\"sk-{ALNUM}\""'
    assert [str(finding) for finding in scan_text(text)] == ["line 1: looks like an sk- API key"]


def _behind(layers: int) -> str:
    """An sk- key that only the given number of decoded layers shows: \\u005cu…u0073k-…"""
    return "\\u005c" + "u005c" * (layers - 2) + f"u0073k-{ALNUM}"


def test_escapes_are_read_to_the_maximum_depth() -> None:
    assert [finding.what for finding in scan_text(_behind(MAX_LAYERS))] == [
        "looks like an sk- API key, JSON-escaped"
    ]


@pytest.mark.parametrize("depth", [MAX_LAYERS + 1, 20_000])
def test_escapes_nested_deeper_are_a_finding(depth: int) -> None:
    text = _behind(depth)
    assert len(list(layers(text))) == MAX_LAYERS + 1
    assert [str(finding) for finding in scan_text(text)] == [
        f"line 1: JSON escapes nested more than {MAX_LAYERS} deep, which the scan doesn't read"
    ]


def test_what_lint_prints_about_a_finding_passes_the_scan() -> None:
    for name in PATTERNS:
        printed = f"looks like {name}, JSON-escaped (decoded, line 1); remove it"
        assert scan_text(printed) == [], name


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


@pytest.mark.parametrize(
    ("doc", "where"),
    [
        (
            {"sse": ['data: {"headers":{"Cookie":"c"}}\n\n']},
            "$.sse[0], in its JSON at $.headers.Cookie",
        ),
        ({"text": '{"headers":{"Cookie":"c"}}'}, "$.text, in its JSON at $.headers.Cookie"),
        ('{"a":1}\n[{"headers":{"Cookie":"c"}}]\n', "$, in its JSON at $[0].headers.Cookie"),
        (
            ['data: {"args":"{\\"headers\\":{\\"Cookie\\":1}}"}'],
            "$[0], in its JSON at $.args, in its JSON at $.headers.Cookie",
        ),
    ],
    ids=["event", "text", "lines", "twice"],
)
def test_credential_headers_inside_strings(doc: JsonValue, where: str) -> None:
    assert [str(finding) for finding in scan_headers(doc)] == [
        f"{where}: the cookie header carries credentials"
    ]


@pytest.mark.parametrize(
    "text", ["headers", '{"headers": {"cookie"', "data: [DONE]", '{"headers": ["cookie"]}']
)
def test_strings_without_credential_headers_pass(text: str) -> None:
    assert scan_headers(text) == []


def _nested(levels: int) -> JsonValue:
    """A headers object as JSON in a string, as JSON in a string, ``levels`` times."""
    doc: JsonValue = {"headers": {"cookie": "c"}}
    for _ in range(levels):
        doc = {"a": json.dumps(doc)}
    return doc


def test_json_inside_strings_is_opened_to_the_maximum_depth() -> None:
    assert len(scan_headers(_nested(MAX_LAYERS))) == 1
    deeper = _nested(MAX_LAYERS + 1)
    assert scan_headers(deeper) == []
    assert "nested more than" in scan_text(json.dumps(deeper))[0].what


def test_a_document_deeper_than_the_python_stack() -> None:
    doc: JsonValue = {"headers": {"cookie": "c"}}
    for _ in range(5000):
        doc = [doc]
    (finding,) = scan_headers({"doc": doc})
    assert finding.where == "$.doc" + "[0]" * 5000 + ".headers.cookie"


def test_a_path_that_holds_a_secret_is_not_shown() -> None:
    (finding,) = scan_headers({f"sk-{ALNUM}": {"headers": {"cookie": "c"}}})
    assert finding.where == "a path the secret scan flags (not shown)"


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


def _escaped(text: str) -> str:
    """``text`` with the s of each sk- written as a JSON escape."""
    return text.replace("sk-", "\\u0073k-")


def test_lint_finds_an_escaped_key_without_printing_it(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    doc["provenance"]["notes"] = f"recorded with sk-proj-{ALNUM}"
    text = _escaped(json.dumps(doc, indent=2))
    path.write_text(text)
    line = next(n for n, found in enumerate(text.splitlines(), 1) if '"notes"' in found)
    assert main(["lint", str(root)]) == 1
    out, err = capsys.readouterr()
    assert ALNUM not in out + err
    assert f"{path}: line {line}: looks like an sk- API key, JSON-escaped; remove it" in out


@pytest.mark.parametrize("stored", ["sse", "chunks_b64"])
def test_lint_finds_an_escaped_key_in_a_field_no_truth_reads(root: Path, stored: str) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    body = doc["exchanges"][0]["response"]["body"]
    events = [
        event.replace('{"id"', _escaped(f'{{"obfuscation":"sk-{ALNUM}","id"'))
        for event in body["sse"]
    ]
    if stored == "chunks_b64":
        events = [base64.b64encode(event.encode()).decode() for event in events]
    doc["exchanges"][0]["response"]["body"] = {stored: events}
    path.write_text(json.dumps(doc, indent=2))
    problems = lint(root).problems
    assert problems
    assert all("looks like an sk- API key, JSON-escaped" in problem.message for problem in problems)


@pytest.mark.parametrize("stored", ["sse", "chunks_b64"])
def test_lint_finds_a_credential_header_inside_an_event(root: Path, stored: str) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    body = doc["exchanges"][0]["response"]["body"]
    events = [body["sse"][0].replace('{"id"', '{"headers":{"Cookie":"c"},"id"'), *body["sse"][1:]]
    if stored == "chunks_b64":
        events = [base64.b64encode(event.encode()).decode() for event in events]
    doc["exchanges"][0]["response"]["body"] = {stored: events}
    path.write_text(json.dumps(doc, indent=2))
    problems = [(problem.where, problem.message) for problem in lint(root).problems]
    advice = "remove it, and revoke the credential if it is real"
    if stored == "sse":
        where = "$.exchanges[0].response.body.sse[0], in its JSON at $.headers.Cookie"
        assert problems == [(where, f"the cookie header carries credentials; {advice}")]
    else:
        where = "$, in its JSON at $.headers.Cookie"
        assert problems == [
            (
                "$.exchanges[0].response.body.chunks_b64",
                f"the cookie header carries credentials (decoded, {where}); {advice}",
            )
        ]


def _schema_value(doc: JsonValue, text: str) -> str:
    doc["tier"] = f"sk-proj-{ALNUM}"  # type: ignore[index, call-overload]
    return json.dumps(doc, indent=2)


def _schema_key(doc: JsonValue, text: str) -> str:
    doc["exchanges"][0]["response"]["headers"][f"sk-proj-{ALNUM}"] = 5  # type: ignore[index, call-overload]
    return json.dumps(doc, indent=2)


def _duplicate_key(doc: JsonValue, text: str) -> str:
    return text.replace('"notes": ""', f'"sk-proj-{ALNUM}": 1, "sk-proj-{ALNUM}": 2, "notes": ""')


def _duplicate_key_in_an_event(doc: JsonValue, text: str) -> str:
    duplicate = f'\\"sk-proj-{ALNUM}\\":1,\\"sk-proj-{ALNUM}\\":2,'
    return text.replace('{\\"id\\"', "{" + duplicate + '\\"id\\"', 1)


@pytest.mark.parametrize(
    "change", [_schema_value, _schema_key, _duplicate_key, _duplicate_key_in_an_event]
)
def test_no_lint_message_repeats_a_key(
    root: Path, capsys: pytest.CaptureFixture[str], change: Callable[[JsonValue, str], str]
) -> None:
    path = _fixture_path(root)
    text = path.read_text()
    path.write_text(change(json.loads(text), text))
    assert main(["lint", str(root)]) == 1
    out, err = capsys.readouterr()
    assert ALNUM not in out + err
    assert "looks like an OpenAI project key (sk-proj prefix); remove it" in out
    assert "the secret scan flags (not shown)" in out


def test_lint_survives_deep_json_in_a_string(root: Path) -> None:
    path = _fixture_path(root)
    doc = json.loads(path.read_text())
    doc["provenance"]["notes"] = "[" * 1100 + "]" * 1100
    path.write_text(json.dumps(doc, indent=2))
    assert lint(root).problems == []
