from __future__ import annotations

import base64
import hashlib
import json
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from wiretruth import __version__, recorder, replay
from wiretruth.cli import main
from wiretruth.formats import (
    Body,
    BodyKind,
    End,
    Exchange,
    FormatError,
    JsonObject,
    Scenario,
    load_fixture,
    load_scenario,
)
from wiretruth.lint import lint
from wiretruth.providers import Provider, openai_chat
from wiretruth.recorder import (
    RecordError,
    api_key,
    describe_request,
    find_scenarios,
    record_scenario,
    summary,
)
from wiretruth.replay import ReplayServer

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "tests" / "samples" / "valid"
KEY = "sk-test-Ab3dEf6hIj9kLm2nOp5qRs8t"  # made up
CHAT = "/v1/chat/completions"
EVENTS = (
    'data: {"id":"chatcmpl-1","model":"gpt-6-luna-2026-09-22","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"Київ"},"finish_reason":null}]}\n\n',
    'data: {"id":"chatcmpl-1","model":"gpt-6-luna-2026-09-22","choices":[{"index":0,'
    '"delta":{},"finish_reason":"length"}]}\n\n',
    "data: [DONE]\n\n",
)
COMPLETION: JsonObject = {
    "id": "chatcmpl-2",
    "object": "chat.completion",
    "model": "gpt-6-luna-2026-09-22",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Kyiv", "refusal": None},
            "finish_reason": "length",
        }
    ],
    "usage": {"prompt_tokens": 21, "completion_tokens": 16, "total_tokens": 37},
}


def _exchange(
    kind: BodyKind,
    value: object,
    *,
    status: int = 200,
    end: End = "clean",
    headers: Mapping[str, str] | None = None,
) -> Exchange:
    is_list = kind in ("sse", "ndjson", "chunks_b64")
    if headers is None:
        content_type = "text/event-stream; charset=utf-8" if kind == "sse" else "application/json"
        headers = {"content-type": content_type}
    return Exchange(
        method="POST",
        path=CHAT,
        status=status,
        headers=MappingProxyType(dict(headers)),
        body=Body(kind, None if is_list else value, tuple(value) if is_list else ()),  # type: ignore[arg-type]
        end=end,
    )


def _fake_openai(*exchanges: Exchange, gap_ms: int = 0) -> ReplayServer:
    fixture = load_fixture(SAMPLES / "fixture" / "recorded-sse.json")
    return ReplayServer(replace(fixture, exchanges=exchanges), event_gap_ms=gap_ms)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    suite = tmp_path / "suite"
    (suite / "scenarios" / "truncation").mkdir(parents=True)
    shutil.copy(REPO / "suite" / "bug-classes.json", suite)
    shutil.copy(
        REPO / "suite" / "scenarios" / "truncation" / "text.json",
        suite / "scenarios" / "truncation",
    )
    return tmp_path


@pytest.fixture
def scenario() -> Scenario:
    return load_scenario(REPO / "suite" / "scenarios" / "truncation" / "text.json")


def _record(
    server: ReplayServer,
    root: Path,
    scenario: Scenario,
    *,
    provider: Provider = openai_chat,
    env: Mapping[str, str] | None = None,
    **options: bool,
) -> recorder.Recording:
    return record_scenario(
        provider,
        scenario,
        model="gpt-6-luna",
        key=KEY,
        root=root,
        base_url=server.base_urls()["openai-chat"],
        env=env or {},
        **options,
    )


# --- Recording ------------------------------------------------------------------------------------


def test_a_stream_is_recorded_event_by_event(
    root: Path, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replay, "REDACTED_HEADERS", frozenset())  # to see the key arrive
    with _fake_openai(_exchange("sse", EVENTS)) as server:
        recording = _record(server, root, scenario)
        (logged,) = server.request_log
    path = root / "suite" / "fixtures" / "openai-chat" / "truncation.text.json"
    assert recording.path == path
    assert recording.warnings == ()
    text = path.read_text(encoding="utf-8")
    assert KEY not in text
    assert "Київ" in text  # written as UTF-8, not \u escapes
    fixture = load_fixture(path)
    assert fixture.id == "openai-chat/truncation.text"
    assert fixture.tier == "recorded"
    (exchange,) = fixture.exchanges
    assert (exchange.method, exchange.path, exchange.status) == ("POST", CHAT, 200)
    assert dict(exchange.headers) == {"content-type": "text/event-stream; charset=utf-8"}
    assert (exchange.body.kind, exchange.body.parts, exchange.end) == ("sse", EVENTS, "clean")
    provenance = fixture.provenance
    assert provenance.requested_model == "gpt-6-luna"
    assert provenance.returned_model == "gpt-6-luna-2026-09-22"
    assert provenance.recorder == f"wiretruth {__version__}"
    assert provenance.raw_sha256 == hashlib.sha256("".join(EVENTS).encode()).hexdigest()
    assert provenance.ci_run is None
    assert provenance.source_commit is None  # tmp_path is no git checkout
    assert provenance.request == logged.json_body
    assert (fixture.truth.finish, fixture.truth.text) == ("length", "Київ")
    assert fixture.truth.stream_completed is True
    # What the server got is what --dry-run describes, with the key in place of ***.
    request = openai_chat.build_request(scenario.call_for(openai_chat.ID), "gpt-6-luna")
    described = describe_request(openai_chat, request, server.base_urls()["openai-chat"])
    head, _, body = described.partition("\n\n")
    assert json.loads(body) == logged.json_body
    expected = dict(line.split(": ", 1) for line in head.splitlines()[1:])
    assert expected["Authorization"] == "Bearer ***"
    assert logged.headers["authorization"] == f"Bearer {KEY}"
    assert {name: logged.headers[name] for name in logged.headers if name != "authorization"} == {
        name.lower(): value for name, value in expected.items() if name != "Authorization"
    }
    assert lint(root).problems == []


def test_a_json_response_keeps_its_key_order(root: Path, scenario: Scenario) -> None:
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        recording = _record(server, root, scenario)
    fixture = recording.fixture
    (exchange,) = fixture["exchanges"]  # type: ignore[misc]
    stored = exchange["response"]["body"]["json"]  # type: ignore[index]
    assert list(stored) == list(COMPLETION)  # type: ignore[arg-type]
    sent = json.dumps(COMPLETION, ensure_ascii=False).encode()  # how the replay server sends it
    assert fixture["provenance"]["raw_sha256"] == hashlib.sha256(sent).hexdigest()  # type: ignore[index]
    assert summary(recording) == (
        f"{recording.path}: HTTP 200, finish length (raw length), input 21 / output 16 tokens, "
        "model gpt-6-luna-2026-09-22"
    )


def test_only_allowlisted_headers_are_stored(root: Path, scenario: Scenario) -> None:
    error = {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit"}}
    headers = {
        "content-type": "application/json",
        "retry-after": "1",
        "openai-organization": "acme",
        "openai-project": "proj",
        "x-request-id": "req_123",
        "x-ratelimit-remaining-requests": "0",
        "set-cookie": "__cf_bm=abc; path=/",
        "cf-ray": "8c1",
    }
    with _fake_openai(_exchange("json", error, status=429, headers=headers)) as server:
        recording = _record(server, root, scenario)
    response = recording.fixture["exchanges"][0]["response"]  # type: ignore[index]
    assert response["headers"] == {"content-type": "application/json", "retry-after": "1"}  # type: ignore[index]
    assert response["status"] == 429  # type: ignore[index]
    truth = recording.fixture["truth"]
    assert truth["finish"] == "error"  # type: ignore[index]
    assert truth["error"]["status"] == 429  # type: ignore[index]
    assert recording.fixture["provenance"]["returned_model"] is None  # type: ignore[index]


def test_a_stream_cut_between_events_stays_events(root: Path, scenario: Scenario) -> None:
    with _fake_openai(_exchange("sse", EVENTS[:2], end="abort")) as server:
        recording = _record(server, root, scenario)
    response = recording.fixture["exchanges"][0]["response"]  # type: ignore[index]
    assert response["body"] == {"sse": list(EVENTS[:2])}  # type: ignore[index]
    assert response["end"] == "abort"  # type: ignore[index]
    assert recording.warnings == (
        "the connection closed before the response ended: stored as end: abort",
    )
    assert recording.fixture["truth"]["stream_completed"] is False  # type: ignore[index]


def test_a_stream_cut_inside_an_event_is_stored_as_read(root: Path, scenario: Scenario) -> None:
    raw = "".join(EVENTS).encode()
    parts = [base64.b64encode(raw[:40]).decode(), base64.b64encode(raw[40:-5]).decode()]
    headers = {"content-type": "text/event-stream"}
    with _fake_openai(_exchange("chunks_b64", parts, end="abort", headers=headers)) as server:
        recording = _record(server, root, scenario)
    response = recording.fixture["exchanges"][0]["response"]  # type: ignore[index]
    stored = b"".join(base64.b64decode(part) for part in response["body"]["chunks_b64"])  # type: ignore[index]
    assert stored == raw[:-5]
    assert response["end"] == "abort"  # type: ignore[index]
    assert recording.warnings == (
        "the event stream doesn't split into events that each end with one blank line: stored "
        "as read",
        "the connection closed before the response ended: stored as end: abort",
    )


def test_a_stream_with_an_extra_blank_line_is_stored_as_read(
    root: Path, scenario: Scenario
) -> None:
    raw = ("".join(EVENTS) + "\n").encode()
    headers = {"content-type": "text/event-stream"}
    exchange = _exchange("chunks_b64", [base64.b64encode(raw).decode()], headers=headers)
    with _fake_openai(exchange) as server:
        recording = _record(server, root, scenario)
    body = recording.fixture["exchanges"][0]["response"]["body"]  # type: ignore[index]
    assert list(body) == ["chunks_b64"]  # type: ignore[call-overload]
    assert recording.fixture["truth"]["stream_completed"] is True  # type: ignore[index]


def test_keep_bytes_stores_the_chunks_read(root: Path, scenario: Scenario) -> None:
    with _fake_openai(_exchange("sse", EVENTS), gap_ms=20) as server:
        recording = _record(server, root, scenario, keep_bytes=True)
    parts = recording.fixture["exchanges"][0]["response"]["body"]["chunks_b64"]  # type: ignore[index]
    pieces = [base64.b64decode(part) for part in parts]  # type: ignore[union-attr]
    assert b"".join(pieces) == "".join(EVENTS).encode()
    assert len(pieces) >= len(EVENTS)  # one read never spans two HTTP chunks
    assert recording.fixture["truth"]["text"] == "Київ"  # type: ignore[index]


@pytest.mark.parametrize(
    ("content_type", "body", "stored", "warning"),
    [
        (
            "text/plain",
            b"\xff\xfe",
            "chunks_b64",
            "the body isn't UTF-8: stored as the chunks read",
        ),
        (
            "application/json",
            b'{"a": 1, "a": 2}',
            "text",
            "the JSON body doesn't parse strictly: stored as text",
        ),
        ("text/html", b"<h1>502 Bad Gateway</h1>", "text", None),
        ("application/problem+json", b'{"error": {"message": "m"}}', "json", None),
    ],
)
def test_other_bodies(
    root: Path,
    scenario: Scenario,
    content_type: str,
    body: bytes,
    stored: str,
    warning: str | None,
) -> None:
    parts = [base64.b64encode(body).decode()]
    exchange = _exchange("chunks_b64", parts, status=502, headers={"content-type": content_type})
    with _fake_openai(exchange) as server:
        recording = _record(server, root, scenario)
    assert list(recording.fixture["exchanges"][0]["response"]["body"]) == [stored]  # type: ignore[index, call-overload]
    assert recording.warnings == (() if warning is None else (warning,))


@contextmanager
def _raw_server(response: bytes) -> Iterator[str]:
    """Answers one request with ``response`` and closes the connection."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data or not data.endswith(b"}\n"):
                data += connection.recv(65536)
            connection.sendall(response)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
    finally:
        thread.join(5)
        listener.close()


def test_a_body_shorter_than_its_content_length_is_an_abort(root: Path, scenario: Scenario) -> None:
    response = (
        b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 50\r\n\r\n{"a"'
    )
    with _raw_server(response) as base_url:
        recording = record_scenario(
            openai_chat, scenario, model="m", key=KEY, root=root, base_url=base_url, env={}
        )
    body = recording.fixture["exchanges"][0]["response"]["body"]  # type: ignore[index]
    assert body == {"chunks_b64": [base64.b64encode(b'{"a"').decode()]}
    assert recording.fixture["exchanges"][0]["response"]["end"] == "abort"  # type: ignore[index]


def test_a_stalled_body_times_out(
    root: Path, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder, "TIMEOUT_SECONDS", 0.2)
    server = _fake_openai(_exchange("sse", EVENTS), gap_ms=1000)
    with server, pytest.raises(RecordError, match=r"no data for 0\.2 s inside the response"):
        _record(server, root, scenario)
    assert not (root / "suite" / "fixtures").exists()


def test_a_connection_that_fails(root: Path, scenario: Scenario) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    with pytest.raises(
        RecordError, match=rf"POST http://127\.0\.0\.1:{port}/v1/chat/completions failed"
    ):
        record_scenario(
            openai_chat,
            scenario,
            model="m",
            key=KEY,
            root=root,
            base_url=f"http://127.0.0.1:{port}/v1",
            env={},
        )


@pytest.mark.parametrize("base_url", ["ftp://example.com/v1", "https:///v1", "api.openai.com"])
def test_a_base_url_that_isnt_http(root: Path, scenario: Scenario, base_url: str) -> None:
    with pytest.raises(RecordError, match="must be http:// or https:// with a host"):
        record_scenario(
            openai_chat, scenario, model="m", key=KEY, root=root, base_url=base_url, env={}
        )


@pytest.mark.parametrize("keep_bytes", [False, True])
def test_a_secret_in_the_response_stops_the_recording(
    root: Path, scenario: Scenario, keep_bytes: bool
) -> None:
    message = {"choices": [{"index": 0, "message": {"content": f"Bearer {KEY[8:]}"}}]}
    server = _fake_openai(_exchange("json", message))
    with server, pytest.raises(RecordError) as caught:
        _record(server, root, scenario, keep_bytes=keep_bytes)
    text = str(caught.value)
    assert "the secret scan stopped the recording, nothing was written" in text
    assert "raw body line 1: looks like a bearer token" in text
    assert KEY[8:] not in text
    assert not (root / "suite" / "fixtures").exists()


SECRET = "sk-proj-Zz9yXw8vUt7sRq6pOn5mLk4j"  # made up


@pytest.mark.parametrize(
    "exchange",
    [
        _exchange("json", {"choices": [{"index": 0, "message": {"content": f"Key:\n{SECRET}"}}]}),
        _exchange("json", {"choices": [{"index": 0, "message": {"content": f"\u00a0{SECRET}"}}]}),
        _exchange(
            "sse",
            (f'data: {{"choices":[{{"index":0,"delta":{{"content":"x\\n{SECRET}"}}}}]}}\n\n',),
        ),
    ],
    ids=["json-newline", "json-nbsp", "sse-newline"],
)
def test_a_key_after_an_escape_is_caught(
    root: Path, scenario: Scenario, exchange: Exchange
) -> None:
    server = _fake_openai(exchange)
    with server, pytest.raises(RecordError, match="looks like an sk- API key"):
        _record(server, root, scenario)
    assert not (root / "suite" / "fixtures").exists()


def test_the_exact_key_is_caught_whatever_it_looks_like(root: Path, scenario: Scenario) -> None:
    key = "plain0key0with0no0known0prefix"
    message = {"choices": [{"index": 0, "message": {"content": f"echo: {key}"}}]}
    server = _fake_openai(_exchange("json", message))
    with server, pytest.raises(RecordError) as caught:
        record_scenario(
            openai_chat,
            scenario,
            model="m",
            key=key,
            root=root,
            base_url=server.base_urls()["openai-chat"],
            env={},
        )
    assert "the API key itself" in str(caught.value)
    assert key not in str(caught.value)


def _events_with(field: str) -> tuple[str, ...]:
    """EVENTS, with ``field`` (JSON members and a comma) added to the first event's data."""
    return (EVENTS[0].replace('{"id"', "{" + field + '"id"', 1), *EVENTS[1:])


@pytest.mark.parametrize("keep_bytes", [False, True])
@pytest.mark.parametrize(
    ("key", "found"),
    [
        (KEY, "raw body line 1: looks like an sk- API key, JSON-escaped"),
        ("plain0key0with0no0known0prefix", "raw body: the API key itself"),
    ],
    ids=["pattern", "exact"],
)
def test_an_escaped_key_in_a_field_no_truth_reads_is_caught(
    root: Path, scenario: Scenario, keep_bytes: bool, key: str, found: str
) -> None:
    escaped = "\\u00" + format(ord(key[0]), "x") + key[1:]
    server = _fake_openai(_exchange("sse", _events_with(f'"obfuscation":"{escaped}",')))
    with server, pytest.raises(RecordError) as caught:
        record_scenario(
            openai_chat,
            scenario,
            model="m",
            key=key,
            root=root,
            base_url=server.base_urls()["openai-chat"],
            env={},
            keep_bytes=keep_bytes,
        )
    assert found in str(caught.value)
    assert key[1:] not in str(caught.value)
    assert not (root / "suite" / "fixtures").exists()


def test_a_credential_header_inside_an_event_is_caught(root: Path, scenario: Scenario) -> None:
    server = _fake_openai(_exchange("sse", _events_with('"headers":{"x-api-key":"k"},')))
    with server, pytest.raises(RecordError) as caught:
        _record(server, root, scenario, keep_bytes=True)
    assert (
        'raw body $, in its JSON at $.headers["x-api-key"]: the x-api-key header carries '
        "credentials" in str(caught.value)
    )


def test_a_credential_header_in_a_multi_line_event_is_caught(
    root: Path, scenario: Scenario
) -> None:
    event = 'data: {"headers": {"x-api-key": "k"},\ndata: "choices": []}\n\n'
    server = _fake_openai(_exchange("sse", (event, "data: [DONE]\n\n")))
    with server, pytest.raises(RecordError) as caught:
        _record(server, root, scenario)
    assert "the x-api-key header carries credentials" in str(caught.value)
    assert not (root / "suite" / "fixtures").exists()


def test_no_finding_quotes_a_key_in_its_path(root: Path, scenario: Scenario) -> None:
    # An object key with the API key in it, which no pattern sees after the x.
    server = _fake_openai(_exchange("sse", _events_with(f'"x{KEY}":{{"headers":{{"cookie":1}}}},')))
    with server, pytest.raises(RecordError) as caught:
        _record(server, root, scenario)
    assert KEY not in str(caught.value)
    assert "raw body: a finding whose path holds the API key (not shown)" in str(caught.value)


@pytest.mark.parametrize("duplicate", [SECRET, KEY], ids=["pattern", "exact"])
def test_no_error_quotes_a_key_from_the_response(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    duplicate: str,
) -> None:
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    events = _events_with(f'"{duplicate}":1,"{duplicate}":2,')  # the extractor can't read it
    with _fake_openai(_exchange("sse", events)) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        assert main(["record", "--provider", "openai-chat", "--scenario", "*"]) == 2
    out, err = capsys.readouterr()
    assert duplicate[8:] not in out + err
    assert "the secret scan stopped the recording, nothing was written" in err


def test_a_request_that_cant_be_sent_keeps_its_reason(root: Path, scenario: Scenario) -> None:
    with pytest.raises(RecordError, match=r"failed: URL can't contain control characters"):
        record_scenario(
            openai_chat,
            scenario,
            model="m",
            key=KEY,
            root=root,
            base_url="http://127.0.0.1:9/a b/v1",
            env={},
        )


def test_a_server_that_closes_without_an_answer(root: Path, scenario: Scenario) -> None:
    # RemoteDisconnected is an OSError and an HTTPException; its message is fixed text.
    with (
        _raw_server(b"") as base_url,
        pytest.raises(RecordError, match=r"failed: Remote end closed connection without response$"),
    ):
        record_scenario(
            openai_chat, scenario, model="m", key=KEY, root=root, base_url=base_url, env={}
        )


def test_a_status_line_that_isnt_http_is_not_quoted(root: Path, scenario: Scenario) -> None:
    response = f"SSH-2.0 {SECRET}\r\n\r\n".encode()
    with _raw_server(response) as base_url, pytest.raises(RecordError) as caught:
        record_scenario(
            openai_chat, scenario, model="m", key=KEY, root=root, base_url=base_url, env={}
        )
    message = "failed: the response isn't valid HTTP (BadStatusLine)"
    assert str(caught.value).endswith(message)
    assert SECRET[8:] not in str(caught.value)


def _write_fixture(path: Path, **changes: object) -> None:
    doc = json.loads((SAMPLES / "fixture" / "recorded-sse.json").read_text(encoding="utf-8"))
    doc.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_a_new_recording_moves_the_old_one_to_its_date(root: Path, scenario: Scenario) -> None:
    folder = root / "suite" / "fixtures" / "openai-chat"
    primary = folder / "truncation.text.json"
    _write_fixture(primary, id="openai-chat/truncation.text", scenario="truncation.text")
    old_text = primary.read_text(encoding="utf-8")
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        recording = _record(server, root, scenario)
    dated = folder / "truncation.text@2026-10-10.json"  # the sample's recorded_at
    assert recording.path == primary
    assert recording.warnings == (f"kept the previous recording as {dated}",)
    assert recording.fixture["id"] == "openai-chat/truncation.text"
    assert load_fixture(primary).truth.text == "Kyiv"
    assert dated.read_text(encoding="utf-8") == old_text.replace(
        '"id": "openai-chat/truncation.text"', '"id": "openai-chat/truncation.text@2026-10-10"'
    )
    assert lint(root).problems == []


def test_a_rotation_that_cant_happen_stops_before_the_request(
    root: Path, scenario: Scenario
) -> None:
    folder = root / "suite" / "fixtures" / "openai-chat"
    _write_fixture(folder / "truncation.text.json", id="openai-chat/truncation.text")
    _write_fixture(folder / "truncation.text@2026-10-10.json")
    server = _fake_openai(_exchange("json", COMPLETION))
    with server, pytest.raises(RecordError, match="which exists; pass --replace to overwrite"):
        _record(server, root, scenario)
    assert server.request_log == []
    with server:
        replaced = _record(server, root, scenario, replace=True)
    assert replaced.warnings == ()
    assert load_fixture(replaced.path).truth.text == "Kyiv"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"{oops", "can't read .* to keep it as a dated variant"),
        (b"[]", "has no recorded_at to date it by"),
        (b'{"provenance": {"source_url": "https://example.com"}}', "has no recorded_at"),
    ],
)
def test_an_old_fixture_that_cant_be_dated(
    root: Path, scenario: Scenario, content: bytes, message: str
) -> None:
    path = root / "suite" / "fixtures" / "openai-chat" / "truncation.text.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    server = _fake_openai(_exchange("json", COMPLETION))
    with server, pytest.raises(RecordError, match=message):
        _record(server, root, scenario)
    assert server.request_log == []


def test_fixtures_derived_from_the_old_recording_are_named(root: Path, scenario: Scenario) -> None:
    folder = root / "suite" / "fixtures" / "openai-chat"
    _write_fixture(folder / "truncation.text.json", id="openai-chat/truncation.text")
    derived = json.loads((SAMPLES / "fixture" / "derived-chunks-abort.json").read_text())
    derived["derived_from"]["fixture"] = "openai-chat/truncation.text"
    (folder / "stream.utf8-split.json").write_text(json.dumps(derived))
    (folder / "broken.json").write_text("{")
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        recording = _record(server, root, scenario)
    assert recording.warnings[1] == (
        f"derived from openai-chat/truncation.text, which is now the new recording: "
        f"{folder / 'stream.utf8-split.json'}; derive them again, or point them at "
        "openai-chat/truncation.text@2026-10-10"
    )


def test_ci_run_and_source_commit(root: Path, scenario: Scenario) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    env = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "kartsan03/wiretruth",
        "GITHUB_RUN_ID": "123",
    }
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        provenance = _record(server, root, scenario, env=env).fixture["provenance"]
    assert provenance["source_commit"] == head  # type: ignore[index]
    assert provenance["ci_run"] == "https://github.com/kartsan03/wiretruth/actions/runs/123"  # type: ignore[index]


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_SERVER_URL": "http://ghe.local", "GITHUB_REPOSITORY": "a/b", "GITHUB_RUN_ID": "1"},
        {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "1"},
        {
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "a/b",
            "GITHUB_RUN_ID": "x",
        },
    ],
)
def test_no_ci_run_without_a_usable_environment(env: dict[str, str]) -> None:
    assert recorder._ci_run(env) is None


def test_no_source_commit_without_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    assert recorder._source_commit(tmp_path) is None


def test_a_truth_the_extractor_cant_compute(root: Path, scenario: Scenario) -> None:
    server = _fake_openai(_exchange("json", [1, 2]))
    with server, pytest.raises(RecordError, match=r"^can't compute the truth: the body is not"):
        _record(server, root, scenario)
    assert not (root / "suite" / "fixtures").exists()


def test_an_invalid_fixture_is_never_written(root: Path, scenario: Scenario) -> None:
    def bad_truth(exchanges: object, request: object) -> JsonObject:
        return {"finish": "maybe"}

    broken = SimpleNamespace(**{name: getattr(openai_chat, name) for name in dir(openai_chat)})
    broken.extract_truth = bad_truth
    with _fake_openai(_exchange("json", COMPLETION)) as server, pytest.raises(FormatError):
        _record(server, root, scenario, provider=broken)  # type: ignore[arg-type]
    assert not (root / "suite" / "fixtures" / "openai-chat" / "truncation.text.json").exists()


# --- Keys, scenarios, dry runs --------------------------------------------------------------------


def test_the_key_comes_from_the_environment() -> None:
    assert api_key(openai_chat, {"OPENAI_API_KEY": f"  {KEY}\n"}) == KEY
    with pytest.raises(RecordError, match=r"^OPENAI_API_KEY is not set$"):
        api_key(openai_chat, {})
    with pytest.raises(RecordError, match=r"^OPENAI_API_KEY is not set$"):
        api_key(openai_chat, {"OPENAI_API_KEY": " \n"})


@pytest.mark.parametrize("bad", [f"{KEY}\nX-Injected: 1", f"{KEY} {KEY}", f"{KEY}é"])
def test_a_key_a_header_cant_carry_is_never_printed(bad: str) -> None:
    with pytest.raises(RecordError) as caught:
        api_key(openai_chat, {"OPENAI_API_KEY": bad})
    assert str(caught.value) == "OPENAI_API_KEY holds characters an HTTP header can't carry"


def test_find_scenarios(root: Path) -> None:
    other = json.loads((root / "suite" / "scenarios" / "truncation" / "text.json").read_text())
    other.pop("call_overrides", None)  # its override names openai-chat, no longer listed
    other.update(id="truncation.text-stream", providers=["gemini"])
    (root / "suite" / "scenarios" / "truncation" / "text-stream.json").write_text(json.dumps(other))
    chosen, skipped = find_scenarios(root, "truncation.*", "openai-chat")
    assert [scenario.id for scenario in chosen] == ["truncation.text"]
    assert skipped == ["truncation.text-stream"]
    with pytest.raises(RecordError, match=r"no scenario in .* matches 'tools\.\*'"):
        find_scenarios(root, "tools.*", "openai-chat")
    with pytest.raises(
        RecordError, match=r"no scenario that matches 'truncation\.text-stream' lists openai-chat"
    ):
        find_scenarios(root, "truncation.text-stream", "openai-chat")


def test_find_scenarios_unions_patterns_in_sorted_order(root: Path) -> None:
    folder = root / "suite" / "scenarios" / "truncation"
    other = json.loads((folder / "text.json").read_text())
    other.pop("call_overrides", None)  # its override names openai-chat, no longer listed
    other.update(id="truncation.text-stream", providers=["gemini"])
    (folder / "text-stream.json").write_text(json.dumps(other))
    pattern = "truncation.text, truncation.text*,truncation.text-stream "
    chosen, skipped = find_scenarios(root, pattern)
    assert [scenario.id for scenario in chosen] == ["truncation.text-stream", "truncation.text"]
    assert skipped == []
    chosen, skipped = find_scenarios(root, pattern, "openai-chat")
    assert [scenario.id for scenario in chosen] == ["truncation.text"]
    assert skipped == ["truncation.text-stream"]


@pytest.mark.parametrize("part", ["tools.*", "Truncation.text", ""])
def test_find_scenarios_rejects_each_unmatched_part(root: Path, part: str) -> None:
    with pytest.raises(RecordError) as caught:
        find_scenarios(root, f"truncation.text,{part}", "openai-chat")
    assert str(caught.value).endswith(f"matches {part!r}")


def test_dry_run_prints_the_exact_request(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    assert (
        main(["record", "--provider", "openai-chat", "--scenario", "truncation.text", "--dry-run"])
        == 0
    )
    out = capsys.readouterr().out
    assert KEY not in out
    body = (
        "{\n"
        '  "model": "gpt-6-luna",\n'
        '  "messages": [\n'
        "    {\n"
        '      "role": "user",\n'
        '      "content": "Write a 200-word paragraph about the history of Kyiv, in English."\n'
        "    }\n"
        "  ],\n"
        '  "max_completion_tokens": 16,\n'
        '  "reasoning_effort": "none"\n'
        "}\n"
    )
    assert out == (
        "# truncation.text\n"
        "POST https://api.openai.com/v1/chat/completions\n"
        "Host: api.openai.com\n"
        "Authorization: Bearer ***\n"
        "Content-Type: application/json\n"
        "Accept: application/json\n"
        f"User-Agent: wiretruth/{__version__}\n"
        "Accept-Encoding: identity\n"
        f"Content-Length: {len(body.encode())}\n"
        "\n" + body + "\n"
    )
    assert not (root / "suite" / "fixtures").exists()


def test_the_record_command(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    with _fake_openai(_exchange("sse", EVENTS[:2], end="abort")) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        code = main(
            [
                "record",
                "--provider",
                "openai-chat",
                "--scenario",
                "truncation.*",
                "--model",
                "gpt-x",
            ]
        )
    assert code == 0
    out = capsys.readouterr().out
    path = Path("suite/fixtures/openai-chat/truncation.text.json")
    assert out == (
        f"wrote {path}: HTTP 200, finish length (raw length), input None / output None tokens, "
        "model gpt-6-luna-2026-09-22\n"
        "  warning: the connection closed before the response ended: stored as end: abort\n"
    )
    assert load_fixture(root / path).provenance.requested_model == "gpt-x"


def test_the_record_command_without_a_key(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["record", "--provider", "openai-chat", "--scenario", "*"]) == 2
    assert capsys.readouterr().err == "wiretruth record: OPENAI_API_KEY is not set\n"


def test_the_record_command_with_bad_scenarios(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(root)
    path = root / "suite" / "scenarios" / "truncation" / "text.json"
    doc = json.loads(path.read_text())
    doc["call"]["extra"] = {"num_ctx": 4096}
    path.write_text(json.dumps(doc))
    args = ["record", "--provider", "openai-chat", "--scenario", "*", "--dry-run"]
    assert main(args) == 2
    assert "extra field 'num_ctx' has no meaning for openai-chat" in capsys.readouterr().err
    doc["priority"] = "P9"
    path.write_text(json.dumps(doc))
    assert main(args) == 1
    assert "$.priority" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    sorted((REPO / "tests" / "samples" / "recorded" / "openai-chat").glob("*.json")),
    ids=lambda path: path.stem,
)
def test_recording_a_replayed_recording_gives_the_same_fixture(
    root: Path, scenario: Scenario, path: Path
) -> None:
    original = load_fixture(path)
    request = original.provenance.request
    assert request is not None
    with ReplayServer(original, event_gap_ms=0) as server:
        again = _record(server, root, scenario).fixture
    response = again["exchanges"][0]["response"]  # type: ignore[index]
    (exchange,) = original.exchanges
    assert response["status"] == exchange.status  # type: ignore[index]
    assert response["headers"] == dict(exchange.headers)  # type: ignore[index]
    body = response["body"]  # type: ignore[index]
    if exchange.body.kind == "sse":
        assert body == {"sse": list(exchange.body.parts)}
        assert again["provenance"]["raw_sha256"] == original.provenance.raw_sha256  # type: ignore[index]
    else:
        assert body == {"json": exchange.body.data}
    # The truth depends on the request only for structured output, which this scenario lacks.
    expected = json.loads(path.read_text(encoding="utf-8"))["truth"]
    if request.get("response_format") is None:
        assert again["truth"] == expected


def test_keep_bytes_on_a_message_keeps_the_returned_model(root: Path, scenario: Scenario) -> None:
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        recording = _record(server, root, scenario, keep_bytes=True)
    assert recording.fixture["provenance"]["returned_model"] == "gpt-6-luna-2026-09-22"  # type: ignore[index]


def test_a_malformed_chunked_body(root: Path, scenario: Scenario) -> None:
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
        + b"1" * 70000
        + b"\r\n"
    )
    with (
        _raw_server(response) as base_url,
        pytest.raises(RecordError, match=r"the response body is malformed \(LineTooLong\)$"),
    ):
        record_scenario(
            openai_chat, scenario, model="m", key=KEY, root=root, base_url=base_url, env={}
        )


def _second_scenario(root: Path) -> None:
    doc = json.loads((root / "suite" / "scenarios" / "truncation" / "text.json").read_text())
    doc["id"] = "truncation.text-stream"
    doc["call"]["stream"] = True
    (root / "suite" / "scenarios" / "truncation" / "text-stream.json").write_text(json.dumps(doc))


def test_the_record_command_checks_every_scenario_before_sending(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _second_scenario(root)
    folder = root / "suite" / "fixtures" / "openai-chat"
    _write_fixture(folder / "truncation.text-stream.json", id="openai-chat/truncation.text-stream")
    _write_fixture(folder / "truncation.text-stream@2026-10-10.json")
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        assert main(["record", "--provider", "openai-chat", "--scenario", "truncation.*"]) == 2
        assert server.request_log == []
    assert "which exists; pass --replace" in capsys.readouterr().err


def test_the_record_command_goes_on_after_a_failed_scenario(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _second_scenario(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    # In path order: truncation.text-stream gets a body the extractor can't read, then
    # truncation.text gets a good response.
    exchanges = (_exchange("json", [1]), _exchange("json", COMPLETION))
    with _fake_openai(*exchanges) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        assert main(["record", "--provider", "openai-chat", "--scenario", "truncation.*"]) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "wiretruth record: truncation.text-stream: can't compute the truth: the body is not a "
        "JSON object\n"
    )
    assert captured.out.startswith("wrote suite/fixtures/openai-chat/truncation.text.json:")
    assert not (
        root / "suite" / "fixtures" / "openai-chat" / "truncation.text-stream.json"
    ).exists()


def test_the_record_command_reports_a_fixture_it_cant_write(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (root / "suite" / "fixtures").mkdir()
    (root / "suite" / "fixtures" / "openai-chat").write_text("not a folder")
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        assert main(["record", "--provider", "openai-chat", "--scenario", "truncation.text"]) == 2
    assert capsys.readouterr().err.startswith("wiretruth record: truncation.text: [Errno")


def test_the_record_command_reports_an_invalid_fixture(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def bad_truth(exchanges: object, request: object) -> JsonObject:
        return {"finish": "maybe"}

    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setattr(openai_chat, "extract_truth", bad_truth)
    with _fake_openai(_exchange("json", COMPLETION)) as server:
        monkeypatch.setattr(openai_chat, "API_BASE", server.base_urls()["openai-chat"])
        assert main(["record", "--provider", "openai-chat", "--scenario", "truncation.text"]) == 1
    assert "$.truth" in capsys.readouterr().err


@pytest.mark.parametrize("failing", ["write_text", "partial write_text", "replace"])
def test_a_rotation_that_fails_halfway_leaves_the_old_fixture(
    root: Path, scenario: Scenario, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    folder = root / "suite" / "fixtures" / "openai-chat"
    primary = folder / "truncation.text.json"
    _write_fixture(primary, id="openai-chat/truncation.text", scenario="truncation.text")
    old_text = primary.read_text(encoding="utf-8")
    method = failing.split()[-1]
    original = getattr(Path, method)

    def fail_late(self: Path, *args: object, **kwargs: object) -> object:
        # The new fixture's temporary file is written; the step after it fails.
        if method == "replace" or "@" in self.name:
            if failing.startswith("partial"):  # the disk fills up after the file is created
                original(self, "{", encoding="utf-8")
            raise OSError(28, "No space left on device")
        return original(self, *args, **kwargs)

    with _fake_openai(_exchange("json", COMPLETION)) as server:
        monkeypatch.setattr(Path, method, fail_late)
        with pytest.raises(OSError, match="No space left on device"):
            _record(server, root, scenario)
    monkeypatch.undo()
    assert sorted(path.name for path in folder.iterdir()) == ["truncation.text.json"]
    assert primary.read_text(encoding="utf-8") == old_text
