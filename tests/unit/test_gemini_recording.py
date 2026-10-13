import base64
import hashlib
import json
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from gemini_samples import MODEL, PATH, STREAM, STREAM_PATH, THINKING, source

from wiretruth import recorder
from wiretruth.cli import main
from wiretruth.formats import build_exchange, load_fixture
from wiretruth.providers import body_text, gemini
from wiretruth.recorder import RecordError, Response
from wiretruth.replay import LoggedRequest, ReplayServer, _chunks, _headers, _query

REPO = Path(__file__).resolve().parents[2]


def request(stream=False):
    scenario = recorder.find_scenarios(REPO, "truncation.text")[0][0]
    return gemini.build_request(replace(scenario.call_for("gemini"), stream=stream), MODEL)


def test_gemini_sse_recording_round_trips_and_omits_query_from_match():
    raw = "".join(STREAM).encode()
    response = Response(
        200, {"content-type": "text/event-stream; charset=utf-8"}, [raw[:13], raw[13:]], "clean"
    )
    body, warnings = recorder.response_body(response, False)
    assert body == {"sse": STREAM} and warnings == []
    ex = build_exchange(recorder.exchange_document(request(True), gemini.API_BASE, response, body))
    assert ex.path == STREAM_PATH
    assert body_text(ex).encode() == b"".join(_chunks(ex.body)) == raw
    assert gemini.extract_truth([ex], None)["stream_completed"] is True


def test_gemini_aborted_event_preserves_received_chunks():
    raw = (STREAM[0] + STREAM[1][:-10]).encode()
    pieces = [raw[:20], raw[20:]]
    response = Response(200, {"content-type": "text/event-stream"}, pieces, "abort")
    body, warnings = recorder.response_body(response, False)
    assert body == {"chunks_b64": [base64.b64encode(piece).decode() for piece in pieces]}
    assert warnings
    ex = build_exchange(recorder.exchange_document(request(True), gemini.API_BASE, response, body))
    assert gemini.extract_truth([ex], None)["text"] == "Café "


@pytest.mark.parametrize("stream", [False, True])
def test_gemini_recording_validates_truth_provenance_and_secrets(stream, tmp_path, monkeypatch):
    scenario = recorder.find_scenarios(
        REPO, "truncation.text-stream" if stream else "usage.reasoning-tokens"
    )[0][0]
    raw = "".join(STREAM).encode() if stream else json.dumps(THINKING).encode()
    response = Response(
        200, {"content-type": "text/event-stream" if stream else "application/json"}, [raw], "clean"
    )
    send = MagicMock(return_value=response)
    monkeypatch.setattr(recorder, "send", send)
    monkeypatch.setattr(recorder, "_source_commit", lambda _: None)
    recording = recorder.record_scenario(
        gemini,
        scenario,
        model=MODEL if stream else "gemini-3.8-flash",
        key="unit-key",
        root=tmp_path,
        base_url=gemini.API_BASE,
        env={},
    )
    fixture = load_fixture(recording.path)
    assert fixture.provider == "gemini"
    assert fixture.exchanges[0].path == (
        STREAM_PATH if stream else "/v1beta/models/gemini-3.8-flash:generateContent"
    )
    assert recording.fixture["provenance"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert recording.fixture["provenance"]["returned_model"] == (
        MODEL if stream else "gemini-3.8-flash"
    )
    assert fixture.truth.finish == ("length" if stream else "stop")
    assert fixture.truth.usage.output == (11 if stream else 41)
    assert "unit-key" not in recording.path.read_text()
    assert "x-goog-api-key" not in recording.path.read_text()
    assert send.call_args.args[-1] == "unit-key"


def test_gemini_cli_dry_run_needs_no_key_and_uses_default_model(monkeypatch, capsys):
    monkeypatch.chdir(REPO)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    send = MagicMock()
    monkeypatch.setattr(recorder, "send", send)
    assert (
        main(
            ["record", "--provider", "gemini", "--scenario", "truncation.text-stream", "--dry-run"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"POST {gemini.API_BASE}{STREAM_PATH}?alt=sse" in output
    assert "x-goog-api-key: ***" in output
    assert '"thinkingLevel": "MINIMAL"' in output
    send.assert_not_called()


def test_gemini_api_key_validation():
    assert recorder.api_key(gemini, {"GEMINI_API_KEY": " unit-key "}) == "unit-key"
    with pytest.raises(RecordError, match="GEMINI_API_KEY is not set"):
        recorder.api_key(gemini, {"OPENAI_API_KEY": "unit-key"})
    with pytest.raises(RecordError, match="HTTP header can't carry"):
        recorder.api_key(gemini, {"GEMINI_API_KEY": "invalid\nkey"})


@pytest.mark.parametrize("query_name", ["key", "KEY", "%6bey"])
def test_gemini_credentials_are_redacted_from_descriptions_and_fixture_paths(query_name):
    raw_request = replace(
        request(True),
        path=STREAM_PATH + f"?alt=sse&{query_name}=unit-secret",
        headers={"X-Goog-Api-Key": "header-secret", "Content-Type": "application/json"},
    )
    description = recorder.describe_request(gemini, raw_request, gemini.API_BASE)
    assert "unit-secret" not in description and "header-secret" not in description
    assert f"{query_name}=***" in description and "X-Goog-Api-Key: ***" in description
    doc = recorder.exchange_document(
        raw_request, gemini.API_BASE, Response(200, {}, [], "clean"), {"json": {}}
    )
    assert doc["match"]["path"] == STREAM_PATH
    assert _query(f"alt=sse&{query_name}=unit-secret") == {"alt": ("sse",)}
    headers = Message()
    headers["X-Goog-Api-Key"] = "header-secret"
    headers["Content-Type"] = "application/json"
    assert _headers(headers) == {"content-type": "application/json"}


def test_gemini_send_transmits_header_and_stream_query_but_stores_only_allowlist(monkeypatch):
    response = MagicMock()
    response.status = 200
    response.length = 0
    response.getheader.side_effect = lambda name: {"content-type": "text/event-stream"}.get(name)
    response.read1.side_effect = [STREAM[0].encode(), b""]
    connection = MagicMock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(recorder.http.client, "HTTPSConnection", MagicMock(return_value=connection))
    result = recorder.send(gemini, request(True), gemini.API_BASE, "unit-key")
    sent = connection.request.call_args
    assert sent.args == ("POST", STREAM_PATH + "?alt=sse")
    assert sent.kwargs["headers"]["x-goog-api-key"] == "unit-key"
    assert "model" not in json.loads(sent.kwargs["body"])
    assert result.headers == {"content-type": "text/event-stream"}
    connection.close.assert_called_once()


def test_gemini_send_failure_does_not_echo_query_credential(monkeypatch):
    raw_request = replace(request(), path=PATH + "?key=unit-secret")
    connection = MagicMock()
    connection.request.side_effect = OSError("failed at ?key=unit-secret")
    monkeypatch.setattr(recorder.http.client, "HTTPSConnection", MagicMock(return_value=connection))
    with pytest.raises(RecordError) as failure:
        recorder.send(gemini, raw_request, gemini.API_BASE, "unit-key")
    assert "unit-secret" not in str(failure.value)
    assert "key=***" in str(failure.value)


def test_gemini_model_glob_and_colon_match_with_query_logged_separately(tmp_path):
    doc = source()
    doc["exchanges"][0]["match"]["path"] = "/v1beta/models/*:streamGenerateContent"
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(doc))
    server = ReplayServer(load_fixture(path))
    incoming = LoggedRequest(
        "POST",
        "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
        _query("alt=sse&key=unit-key"),
        {},
        {},
        None,
        "",
        None,
        False,
    )
    assert server._receive(incoming) == (0, 0)
    server._finish(0, complete=True)
    assert server.request_log[0].query == {"alt": ("sse",)}
    assert server.request_log[0].response_complete


def test_gemini_secret_scan_stops_before_writing(tmp_path, monkeypatch):
    scenario = recorder.find_scenarios(REPO, "truncation.text")[0][0]
    raw = json.dumps({"unused": "AQ." + "x" * 50}).encode()
    monkeypatch.setattr(
        recorder,
        "send",
        MagicMock(return_value=Response(200, {"content-type": "application/json"}, [raw], "clean")),
    )
    with pytest.raises(RecordError, match="secret scan stopped"):
        recorder.record_scenario(
            gemini,
            scenario,
            model=MODEL,
            key="unit-key",
            root=tmp_path,
            base_url=gemini.API_BASE,
            env={},
        )
    assert not list(tmp_path.rglob("*.json"))
