import base64
import http.client
import json
import re
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO

import pytest

from wiretruth import replay
from wiretruth.formats import Body, BodyKind, End, Exchange, Fixture, load_fixture, load_schema
from wiretruth.replay import HOST, ReplayServer

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "valid" / "fixture"
CHAT = "/v1/chat/completions"


def _sample(name: str) -> Fixture:
    return load_fixture(SAMPLES / f"{name}.json")


def _exchange(
    path: str = CHAT,
    *,
    kind: BodyKind = "sse",
    parts: tuple[str, ...] = (),
    data: object = None,
    status: int = 200,
    end: End = "clean",
    method: str = "POST",
) -> Exchange:
    return Exchange(
        method=method,
        path=path,
        status=status,
        headers=MappingProxyType({"content-type": "text/event-stream"}),
        body=Body(kind=kind, data=data, parts=parts),  # type: ignore[arg-type]
        end=end,
    )


def _fixture(*exchanges: Exchange) -> Fixture:
    return replace(_sample("recorded-sse"), exchanges=exchanges)


def _events(count: int) -> tuple[str, ...]:
    return tuple(f'data: {{"n": {n}}}\n\n' for n in range(count))


def _post(
    server: ReplayServer,
    path: str = CHAT,
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        response = conn.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, headers, response.read()
    finally:
        conn.close()


@contextmanager
def _raw(server: ReplayServer, request: bytes) -> Iterator[BinaryIO]:
    """A raw socket that sends ``request``, so tests see the bytes the server writes."""
    with socket.create_connection((HOST, server.port), timeout=5) as sock:
        sock.sendall(request)
        with sock.makefile("rb") as reader:
            yield reader


def _request(path: str = CHAT, body: bytes = b"{}", extra: bytes = b"") -> bytes:
    head = f"POST {path} HTTP/1.1\r\nHost: test\r\nContent-Length: {len(body)}\r\n"
    return head.encode() + extra + b"\r\n" + body


def _read_head(reader: BinaryIO) -> tuple[int, dict[str, str]]:
    status = int(reader.readline().split()[1])
    headers = {}
    while (line := reader.readline()) not in (b"\r\n", b""):
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


def _read_chunks(reader: BinaryIO) -> tuple[list[bytes], bool]:
    """Parses chunked framing by hand: the chunks, and whether the terminating chunk came."""
    chunks = []
    while line := reader.readline():
        size = int(line, 16)
        if size == 0:
            assert reader.readline() == b"\r\n"
            return chunks, True
        chunks.append(reader.read(size))
        assert reader.read(2) == b"\r\n"
    return chunks, False


# --- Bodies ---------------------------------------------------------------------------------------


def test_json_body_is_sent_whole_with_content_length() -> None:
    fixture = _sample("documented-json")
    with ReplayServer(fixture) as server:
        status, headers, body = _post(server, "/v1/messages")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert int(headers["content-length"]) == len(body)
    assert "transfer-encoding" not in headers
    assert json.loads(body) == fixture.exchanges[0].body.data
    assert "Kyiv grew" in body.decode()  # not \u-escaped, as the recorder stores it


def test_sse_events_arrive_intact_and_in_order() -> None:
    fixture = _sample("recorded-sse")
    with ReplayServer(fixture) as server, _raw(server, _request()) as reader:
        status, headers = _read_head(reader)
        chunks, terminated = _read_chunks(reader)
    assert status == 200
    assert headers["transfer-encoding"] == "chunked"
    assert headers["content-type"] == fixture.exchanges[0].headers["content-type"]
    assert chunks == [event.encode() for event in fixture.exchanges[0].body.parts]
    assert terminated


def test_ndjson_is_one_chunk_per_line() -> None:
    fixture = _sample("recorded-ndjson")
    exchange = fixture.exchanges[0]
    request = _request(exchange.path)
    with ReplayServer(fixture) as server, _raw(server, request) as reader:
        _read_head(reader)
        chunks, terminated = _read_chunks(reader)
    assert chunks == [(line + "\n").encode() for line in exchange.body.parts]
    assert terminated


def test_chunks_b64_are_byte_exact() -> None:
    fixture = _sample("derived-chunks-abort")
    expected = [base64.b64decode(part) for part in fixture.exchanges[0].body.parts]
    with ReplayServer(fixture) as server, _raw(server, _request()) as reader:
        _read_head(reader)
        chunks, terminated = _read_chunks(reader)
    # Each stored chunk is its own HTTP chunk, split inside the two bytes of a Cyrillic letter.
    assert len(chunks) == len(expected) == 2
    assert chunks == expected
    assert expected[0].endswith(b"\xd0")
    assert not terminated, "an abort ending sends no terminating chunk"


def test_abort_surfaces_as_an_incomplete_read() -> None:
    with ReplayServer(_sample("derived-chunks-abort")) as server:
        conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
        conn.request("POST", CHAT, body=b"{}")
        response = conn.getresponse()
        with pytest.raises(http.client.IncompleteRead):
            response.read()
        conn.close()
    (logged,) = server.request_log
    assert logged.response_complete


# --- Matching -------------------------------------------------------------------------------------


def test_exchanges_are_served_in_order_and_the_last_repeats() -> None:
    fixture = _sample("derived-retry")
    path = "/v1beta/models/gemini-3.5-flash-lite:generateContent"
    with ReplayServer(fixture) as server:
        first = _post(server, path)
        second = _post(server, path)
        third = _post(server, path + "?alt=json")
    assert first[0] == 429
    assert first[1]["retry-after"] == "1"
    assert first[2] == b"Resource has been exhausted"
    assert second[0] == third[0] == 200
    assert second[2] == third[2]
    assert [request.path for request in server.request_log] == [path] * 3
    assert [request.exchange for request in server.request_log] == [0, 1, 1]


def test_a_request_that_matches_nothing_gets_a_404_and_is_logged() -> None:
    fixture = _fixture(_exchange(CHAT, kind="json", data={"ok": True}))
    with ReplayServer(fixture) as server:
        missing = _post(server, "/v1/responses")
        found = _post(server, CHAT)
    assert missing[0] == 404
    assert json.loads(missing[2]) == {"wiretruth": "no fixture for POST /v1/responses"}
    assert found[0] == 200, "a 404 doesn't use up the next exchange"
    log = server.request_log
    assert [(request.path, request.exchange) for request in log] == [
        ("/v1/responses", None),
        (CHAT, 0),
    ]
    assert [request.response_complete for request in log] == [False, True], "a 404 isn't served"


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "PROPFIND"])
def test_any_method_is_logged(method: str) -> None:
    with ReplayServer(_sample("recorded-sse")) as server:
        conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
        conn.request(method, CHAT)
        assert conn.getresponse().status == 404
        conn.close()
    (logged,) = server.request_log
    assert (logged.method, logged.path, logged.exchange) == (method, CHAT, None)


def test_after_the_exchanges_run_out_the_last_matching_one_repeats() -> None:
    fixture = _fixture(
        _exchange("/a", kind="text", data="A"),
        _exchange("/b", kind="text", data="B"),
    )
    with ReplayServer(fixture) as server:
        bodies = [_post(server, path)[2] for path in ("/a", "/b", "/a", "/b", "/c")]
    assert bodies[:4] == [b"A", b"B", b"A", b"B"]
    assert json.loads(bodies[4]) == {"wiretruth": "no fixture for POST /c"}
    assert [request.exchange for request in server.request_log] == [0, 1, 0, 1, None]


def test_path_globs_match_the_whole_path_and_the_method_must_match() -> None:
    fixture = _fixture(_exchange("/v1beta/models/*:generateContent", kind="text", data="ok"))
    with ReplayServer(fixture) as server:
        assert _post(server, "/v1beta/models/m:generateContentX")[0] == 404
        assert _post(server, "/x/v1beta/models/m:generateContent")[0] == 404
        conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
        conn.request("GET", "/v1beta/models/m:generateContent")
        assert conn.getresponse().status == 404
        conn.close()
        assert _post(server, "/v1beta/models/a/b:generateContent")[0] == 200


def test_health_endpoint_is_not_logged() -> None:
    fixture = _sample("recorded-sse")
    with ReplayServer(fixture) as server:
        conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
        conn.request("GET", replay.HEALTH_PATH)
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"wiretruth": "ok", "fixture": fixture.id}
        conn.close()
    assert server.request_log == []


# --- Request log ----------------------------------------------------------------------------------


def test_the_request_log_leaves_out_credentials() -> None:
    credentials = (
        b"Authorization: Bearer sk-secret\r\n"
        b"Proxy-Authorization: Basic c2VjcmV0\r\n"
        b"X-Api-Key: secret\r\n"
        b"x-goog-api-key: secret\r\n"
        b"api-key: secret\r\n"
        b"Cookie: session=secret\r\n"
        b"X-Stainless-Lang: python\r\n"
        b"X-Repeated: a\r\n"
        b"X-Repeated: b\r\n"
    )
    path = "/v1beta/models/m:generateContent"
    request = _request(
        f"{path}?key=secret&Key=secret&api_key=secret&access_token=secret&alt=sse&alt=json&empty=",
        extra=credentials,
    )
    with ReplayServer(_sample("derived-retry")) as server, _raw(server, request) as reader:
        assert _read_head(reader)[0] == 429
    (logged,) = server.request_log
    assert "secret" not in repr(logged)
    assert logged.path == path
    assert logged.query == {"alt": ("sse", "json"), "empty": ("",)}
    assert logged.headers == {
        "host": "test",
        "content-length": "2",
        "x-stainless-lang": "python",
        "x-repeated": "a, b",
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", logged.received_at)


@pytest.mark.parametrize(
    ("body", "json_body", "raw_body_b64"),
    [
        (b'{"model": "m", "stream": true}', {"model": "m", "stream": True}, None),
        (b"not json \xff", None, base64.b64encode(b"not json \xff").decode()),
        (b"", None, ""),
        (b'{"a": 1, "a": 2}', None, base64.b64encode(b'{"a": 1, "a": 2}').decode()),
        (b'{"t": NaN}', None, base64.b64encode(b'{"t": NaN}').decode()),
    ],
    ids=["json", "not-json", "empty", "duplicate-key", "nan"],
)
def test_the_request_log_keeps_the_body(
    body: bytes, json_body: object, raw_body_b64: str | None
) -> None:
    with ReplayServer(_sample("recorded-sse")) as server:
        _post(server, body=body)
    (logged,) = server.request_log
    assert (logged.method, logged.json_body, logged.raw_body_b64) == (
        "POST",
        json_body,
        raw_body_b64,
    )


def test_a_chunked_request_body_is_read() -> None:
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\nTransfer-Encoding: chunked\r\n\r\n"
        b'5;ext=1\r\n{"a":\r\n3\r\n 1}\r\n0\r\nX-Trailer: t\r\n\r\n'
    )
    with ReplayServer(_sample("recorded-sse")) as server, _raw(server, request) as reader:
        assert _read_head(reader)[0] == 200
        _read_chunks(reader)
    assert server.request_log[0].json_body == {"a": 1}


def test_a_transfer_coding_before_chunked_is_kept_as_bytes() -> None:
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: test\r\n"
        b"Transfer-Encoding: gzip, chunked\r\n\r\n2\r\n\x1f\x8b\r\n0\r\n\r\n"
    )
    with ReplayServer(_sample("recorded-sse")) as server, _raw(server, request) as reader:
        assert _read_head(reader)[0] == 200
        _read_chunks(reader)
    assert server.request_log[0].raw_body_b64 == base64.b64encode(b"\x1f\x8b").decode()


@pytest.mark.parametrize(
    "request_bytes",
    [
        _request(extra=b"Content-Length: x\r\n").replace(b"Content-Length: 2\r\n", b""),
        _request(extra=b"Content-Length: -1\r\n").replace(b"Content-Length: 2\r\n", b""),
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",
        _request(extra=b"Content-Length: 99999999999999999999\r\n").replace(
            b"Content-Length: 2\r\n", b""
        ),
        b"POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\nffffffffffffffff\r\n",
        b"POST / HTTP/1.1\r\nTransfer-Encoding: gzip\r\n\r\n",
    ],
    ids=["content-length", "negative-length", "chunk-size", "huge-length", "huge-chunk", "gzip"],
)
def test_an_unreadable_body_gets_a_400_and_is_not_logged(request_bytes: bytes) -> None:
    with ReplayServer(_sample("recorded-sse")) as server, _raw(server, request_bytes) as reader:
        assert _read_head(reader)[0] == 400
    assert server.request_log == []


def test_http_1_0_gets_a_505_and_is_not_logged() -> None:
    request = _request().replace(b"HTTP/1.1", b"HTTP/1.0")
    with ReplayServer(_sample("recorded-sse")) as server, _raw(server, request) as reader:
        assert _read_head(reader)[0] == 505
    assert server.request_log == []


def test_a_request_cut_off_inside_its_body_is_not_logged() -> None:
    with (
        ReplayServer(_sample("recorded-sse")) as server,
        socket.create_connection((HOST, server.port), timeout=5) as sock,
    ):
        body = _request().replace(b"Content-Length: 2", b"Content-Length: 10")
        sock.sendall(body)
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(1) == b"", "no response"
    assert server.request_log == []


@pytest.mark.parametrize(
    ("exchange", "message"),
    [
        (_exchange(kind="json", data={}, end="abort"), "a json body is sent whole and can't abort"),
        (_exchange(kind="chunks_b64", parts=("aGk=", "")), "an empty chunk would end the body"),
    ],
)
def test_a_fixture_built_in_code_is_checked_too(exchange: Exchange, message: str) -> None:
    with pytest.raises(ValueError, match=f"exchange 0: {message}"):
        ReplayServer(_fixture(exchange))


def test_the_request_log_is_a_snapshot() -> None:
    with ReplayServer(_sample("documented-json")) as server:
        log = server.request_log
        _post(server, "/v1/messages")
        assert log == []
        assert len(server.request_log) == 1


# --- Whether a response was sent in full ----------------------------------------------------------


def test_response_complete_is_false_when_the_client_disconnects_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "DRAIN_SECONDS", 30)
    fixture = _fixture(_exchange(parts=_events(200)))  # 4 s to write in full
    with ReplayServer(fixture, event_gap_ms=20) as server:
        with _raw(server, _request()) as reader:
            _read_head(reader)
            reader.readline()  # the first chunk's size line, then hang up
        started = time.monotonic()
    assert time.monotonic() - started < 2, "the server noticed the disconnect by itself"
    (logged,) = server.request_log
    assert not logged.response_complete


def test_stopping_cuts_off_a_response_still_being_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(replay, "DRAIN_SECONDS", 0.1)
    server = ReplayServer(_fixture(_exchange(parts=_events(100))), event_gap_ms=50)
    server.__enter__()
    try:
        with _raw(server, _request()) as reader:
            _read_head(reader)
            started = time.monotonic()
            server.__exit__(None, None, None)  # while the client still holds the connection
            assert time.monotonic() - started < 1
    finally:
        if server._thread is not None and server._thread.is_alive():
            server.__exit__(None, None, None)
    (logged,) = server.request_log
    assert not logged.response_complete


def test_stopping_with_an_idle_keep_alive_connection_is_quick() -> None:
    fixture = _sample("documented-json")
    with ReplayServer(fixture) as server:
        conn = http.client.HTTPConnection(HOST, server.port, timeout=5)
        for _ in range(2):  # both requests go over the same connection
            conn.request("POST", "/v1/messages", body=b"{}")
            assert conn.getresponse().read()
        started = time.monotonic()
    assert time.monotonic() - started < 1
    conn.close()
    assert [request.response_complete for request in server.request_log] == [True, True]


# --- Addresses ------------------------------------------------------------------------------------


def test_base_urls_cover_every_provider() -> None:
    defs = load_schema("common.schema.json")["$defs"]
    assert isinstance(defs, dict)
    provider_ids = defs["provider_id"]
    assert isinstance(provider_ids, dict)
    with ReplayServer(_sample("recorded-sse")) as server:
        urls = server.base_urls()
        root = f"http://127.0.0.1:{server.port}"
        assert server.url == root
        assert server.port > 0
    assert list(urls) == provider_ids["enum"]
    assert urls["openai-chat"] == urls["openai-responses"] == urls["ollama-openai"] == root + "/v1"
    assert urls["anthropic"] == urls["gemini"] == urls["ollama"] == root
