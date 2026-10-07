"""The replay server: one fixture's recorded HTTP exchanges, served on 127.0.0.1 exactly as stored.

``json`` and ``text`` bodies are sent whole, with ``Content-Length``. ``sse``, ``ndjson`` and
``chunks_b64`` bodies use HTTP/1.1 chunked transfer encoding, framed by hand: one chunk per event,
line or stored chunk, each flushed on its own, with ``TCP_NODELAY`` and a short gap between chunks.
``end: abort`` shuts the connection down without the terminating chunk (ADR 0001).

Requests are matched against the exchanges in order: each matching request uses up one, and once
they have run out, the last exchange that matches repeats. A request that matches none gets a 404,
and is logged like any other. The log never holds credentials, and records whether each exchange
was sent in full (ADR 0004 uses that to tell a hang from a harness problem).

Clients must speak HTTP/1.1: HTTP/1.0 has no chunked encoding, so chunk boundaries and an aborted
ending can't be expressed. A request the server can't read (another HTTP version, unusable body
framing, a body over ``MAX_BODY_BYTES``) gets a 400 or 505 and is not logged.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from types import TracebackType
from typing import cast
from urllib.parse import parse_qsl

from wiretruth.formats import Body, Exchange, Fixture, JsonValue, parse_json
from wiretruth.secret_scan import SECRET_HEADERS

HOST = "127.0.0.1"
HEALTH_PATH = "/__wiretruth/health"

# Where each provider's API sits under http://127.0.0.1:<port> (02-specs §7).
BASE_PATHS: Mapping[str, str] = {
    "openai-chat": "/v1",
    "openai-responses": "/v1",
    "anthropic": "",
    "gemini": "",
    "gemini-interactions": "",
    "ollama": "",
    "ollama-openai": "/v1",
}

# Left out of the request log, so credentials and account identifiers never reach results or
# terminal output. Names are compared in lowercase.
REDACTED_HEADERS = SECRET_HEADERS
REDACTED_QUERY = frozenset({"key", "api_key", "api-key", "apikey", "access_token", "token"})

MAX_BODY_BYTES = 16 * 1024 * 1024
# How long stopping waits for responses still being written before it closes their connections.
DRAIN_SECONDS = 5.0

_HEX = re.compile(rb"[0-9A-Fa-f]{1,16}")


@dataclass(frozen=True)
class LoggedRequest:
    """One request the server received, without credentials.

    ``raw_body_b64`` is None when the body is strict JSON, and ``json_body`` holds it. Otherwise
    ``json_body`` is None and ``raw_body_b64`` holds the bytes (``""`` for no body).
    ``exchange`` is the index of the fixture exchange that answered, or None for the 404 when none
    matched. ``response_complete`` is true once that exchange was written in full, including an
    ``abort`` exchange's deliberate close. It stays false for a 404.
    """

    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    headers: Mapping[str, str]
    json_body: JsonValue
    raw_body_b64: str | None
    received_at: str
    exchange: int | None
    response_complete: bool


def _glob(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")))


def _chunks(body: Body) -> list[bytes]:
    if body.kind == "chunks_b64":
        return [base64.b64decode(part) for part in body.parts]
    newline = "\n" if body.kind == "ndjson" else ""
    return [(part + newline).encode() for part in body.parts]


def _hex_size(line: bytes) -> int:
    size = line.strip()
    if not _HEX.fullmatch(size):
        raise ValueError(f"bad chunk size {size!r}")
    return int(size, 16)


def _query(query: str) -> dict[str, tuple[str, ...]]:
    values: dict[str, list[str]] = {}
    for name, value in parse_qsl(query, keep_blank_values=True):
        if name.lower() not in REDACTED_QUERY:
            values.setdefault(name, []).append(value)
    return {name: tuple(found) for name, found in values.items()}


def _headers(message: Message) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in message.items():
        name = name.lower()
        if name in REDACTED_HEADERS:
            continue
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return headers


class ReplayServer:
    """Serves one fixture on 127.0.0.1 while the ``with`` block runs.

    The port is bound on entering the block (0 picks a free one). On leaving, the server stops
    accepting, gives responses still being written up to ``DRAIN_SECONDS`` to finish, closes every
    connection and waits for its threads, so the request log is final from then on.
    """

    def __init__(self, fixture: Fixture, *, port: int = 0, event_gap_ms: int = 5) -> None:
        if event_gap_ms < 0:
            raise ValueError(f"event_gap_ms must be 0 or more, not {event_gap_ms}")
        # formats.load_fixture rejects these too; this catches fixtures built in code.
        for i, exchange in enumerate(fixture.exchanges):
            kind = exchange.body.kind
            if exchange.end == "abort" and kind in ("json", "text"):
                raise ValueError(f"exchange {i}: a {kind} body is sent whole and can't abort")
            if kind == "chunks_b64" and "" in exchange.body.parts:
                raise ValueError(f"exchange {i}: an empty chunk would end the body early")
        self.fixture = fixture
        self._requested_port = port
        self._gap = event_gap_ms / 1000
        self._paths = [_glob(exchange.path) for exchange in fixture.exchanges]
        self._cursor = 0
        self._log: list[LoggedRequest] = []
        self._writing = 0
        self._state = threading.Condition()
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ReplayServer:
        self._httpd = _Server(self, self._requested_port)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._httpd is not None and self._thread is not None
        self._httpd.shutdown()
        try:
            with self._state:
                self._state.wait_for(lambda: self._writing == 0, timeout=DRAIN_SECONDS)
        finally:  # also after a second Ctrl-C, so no request thread is left running
            self._httpd.close_connections()
            self._httpd.server_close()
            self._thread.join()

    @property
    def port(self) -> int:
        assert self._httpd is not None, "the server runs only inside a with block"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def base_urls(self) -> dict[str, str]:
        """The base URL to give a library, per provider id."""
        return {provider: self.url + path for provider, path in BASE_PATHS.items()}

    @property
    def request_log(self) -> list[LoggedRequest]:
        """A copy of the log so far, in the order the requests arrived."""
        with self._state:
            return list(self._log)

    # Called from the request threads.

    def _receive(self, request: LoggedRequest) -> tuple[int, int | None]:
        """Logs the request and picks its exchange in one step, so the two orders agree."""
        exchanges = self.fixture.exchanges

        def matches(i: int) -> bool:
            same_method = exchanges[i].method == request.method
            return same_method and self._paths[i].fullmatch(request.path) is not None

        with self._state:
            chosen: int | None
            if self._cursor < len(exchanges):
                chosen = self._cursor if matches(self._cursor) else None
                if chosen is not None:
                    self._cursor += 1
            else:
                chosen = next((i for i in reversed(range(len(exchanges))) if matches(i)), None)
            self._log.append(replace(request, exchange=chosen))
            self._writing += 1
            return len(self._log) - 1, chosen

    def _finish(self, index: int, *, complete: bool) -> None:
        with self._state:
            if complete:
                self._log[index] = replace(self._log[index], response_complete=True)
            self._writing -= 1
            self._state.notify_all()


class _Server(ThreadingHTTPServer):
    daemon_threads = False  # so that server_close() waits for the request threads

    def __init__(self, replay: ReplayServer, port: int) -> None:
        self.replay = replay
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        super().__init__((HOST, port), _Handler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind would look the host name up; the address is enough here.
        TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]

    def process_request(self, request: socket.socket, client_address: object) -> None:  # type: ignore[override]
        # Runs in the accept loop, so every connection is known before shutdown() returns.
        with self._connections_lock:
            self._connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: socket.socket) -> None:  # type: ignore[override]
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def close_connections(self) -> None:
        """Ends keep-alive waits and any response still being written."""
        with self._connections_lock:
            connections = list(self._connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # TCP_NODELAY: each chunk leaves as soon as it's written
    server: _Server

    def __getattr__(self, name: str) -> Callable[[], None]:
        # Every method gets the same handling. Without this, BaseHTTPRequestHandler would answer
        # a method that has no do_<METHOD> with an unlogged 501.
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    def handle(self) -> None:
        # The client, or ReplayServer.__exit__, dropped the connection.
        with contextlib.suppress(OSError):
            super().handle()

    def log_message(self, format: str, *args: object) -> None:
        # The default access log would print the request line, query string and all.
        pass

    def _serve(self) -> None:
        received_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        path, _, query = self.path.partition("?")
        if self.request_version != "HTTP/1.1":
            self.send_error(505, "the replay server speaks HTTP/1.1 only")
            return
        if self.command == "GET" and path == HEALTH_PATH:
            self._send_json(200, {"wiretruth": "ok", "fixture": self.server.replay.fixture.id})
            return
        try:
            body = self._read_body()
        except ValueError as error:
            self.send_error(400, str(error))
            return
        try:
            json_body, raw_body_b64 = parse_json(body), None
        except (ValueError, RecursionError):
            json_body, raw_body_b64 = None, base64.b64encode(body).decode()
        replay = self.server.replay
        index, chosen = replay._receive(
            LoggedRequest(
                method=self.command,
                path=path,
                query=_query(query),
                headers=_headers(self.headers),
                json_body=json_body,
                raw_body_b64=raw_body_b64,
                received_at=received_at.replace("+00:00", "Z"),
                exchange=None,
                response_complete=False,
            )
        )
        if self.command == "HEAD":
            self.close_connection = True  # a HEAD client doesn't expect the body that follows
        complete = False
        try:
            if chosen is None:
                self._send_json(404, {"wiretruth": f"no fixture for {self.command} {path}"})
            else:
                self._send_exchange(replay.fixture.exchanges[chosen])
                complete = True
        except OSError:
            self.close_connection = True
        finally:
            replay._finish(index, complete=complete)

    def _read_body(self) -> bytes:
        """The request body.

        Raises ValueError when its framing is unusable, and ConnectionError when the connection
        closes before the body is complete.
        """
        coding = self.headers.get("transfer-encoding")
        if coding is not None:
            if coding.rsplit(",", 1)[-1].strip().lower() != "chunked":
                raise ValueError(f"unsupported Transfer-Encoding {coding!r}")
            body = bytearray()
            while size := _hex_size(self.rfile.readline(65537).split(b";")[0]):
                if len(body) + size > MAX_BODY_BYTES:
                    raise ValueError(f"request body over {MAX_BODY_BYTES} bytes")
                body += self._read_exactly(size)
                self.rfile.readline()
            while self.rfile.readline(65537).strip():
                pass  # a trailer field
            return bytes(body)
        length = self.headers.get("content-length", "0").strip()
        if not (length.isascii() and length.isdigit()):
            raise ValueError(f"bad Content-Length {length!r}")
        if int(length) > MAX_BODY_BYTES:
            raise ValueError(f"request body over {MAX_BODY_BYTES} bytes")
        return self._read_exactly(int(length))

    def _read_exactly(self, size: int) -> bytes:
        data = self.rfile.read(size)
        if len(data) < size:
            raise ConnectionError("the connection closed inside the request body")
        return data

    def _send_json(self, status: int, value: JsonValue) -> None:
        payload = json.dumps(value).encode()
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_exchange(self, exchange: Exchange) -> None:
        self.send_response_only(exchange.status)
        for name, value in exchange.headers.items():
            self.send_header(name, value)
        body = exchange.body
        if body.kind in ("json", "text"):
            text = json.dumps(body.data, ensure_ascii=False) if body.kind == "json" else body.data
            payload = cast(str, text).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i, chunk in enumerate(_chunks(body)):
                if i:
                    time.sleep(self.server.replay._gap)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            if exchange.end == "clean":
                self.wfile.write(b"0\r\n\r\n")
        if exchange.end == "abort":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
