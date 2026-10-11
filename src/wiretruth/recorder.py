"""``wiretruth record``: send a scenario's call to a provider and save the response as a fixture.

No SDK sits between the provider and the fixture. The provider module builds the native request,
``http.client`` sends it, and the response is stored as it came (docs/recording.md):

- the status, and only the ``content-type`` and ``retry-after`` headers;
- the body: an event stream split into events, JSON re-serialized with its key order, anything
  else as text, or as the chunks read (``--keep-bytes``, or when it isn't UTF-8 or ends early);
- the SHA-256 of the raw body, the request body (credentials travel in headers, which are never
  stored), the models, the recorder version, the commit and the CI run;
- the truth, computed by the provider module.

The API key is read from the environment and goes only into the request's headers. The secret
scan checks the raw body before anything parses it, and the fixture before it is written; a finding
stops the recording. No error the recorder raises repeats what the response said.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from wiretruth import __version__
from wiretruth.formats import (
    FormatError,
    JsonObject,
    JsonValue,
    Scenario,
    build_exchange,
    load_scenario,
    parse_json,
    validate,
)
from wiretruth.providers import Provider, RawRequest, json_object, json_str
from wiretruth.secret_scan import layers, scan_headers, scan_text

STORED_HEADERS = ("content-type", "retry-after")
TIMEOUT_SECONDS = 120.0
_READ_SIZE = 65536
_HEADER_SAFE = re.compile(r"[\x21-\x7e]+")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_EVENT_END = re.compile(r"\r\n\r\n|\n\n")
_RECORDED_DAY = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T")


class RecordError(Exception):
    """Recording can't go on: no key, no scenario, a network failure, a secret in the response."""


@dataclass(frozen=True)
class Recording:
    path: Path
    fixture: JsonObject
    warnings: tuple[str, ...]


# --- Choosing scenarios ---------------------------------------------------------------------------


def find_scenarios(
    root: Path, pattern: str, provider: str | None = None
) -> tuple[list[Scenario], list[str]]:
    """Union of comma-separated ids or globs, limited to those listing ``provider`` if given.

    Also returns ids skipped by the provider filter. Raises FormatError for an invalid scenario
    file, and RecordError when any pattern matches nothing or the provider filter leaves none.
    """
    folder = root / "suite" / "scenarios"
    scenarios = [load_scenario(path) for path in sorted(folder.glob("*/*.json"))]
    matched_ids = set()
    for part in pattern.split(","):
        matches = {scenario.id for scenario in scenarios if fnmatchcase(scenario.id, part)}
        if not matches:
            raise RecordError(f"no scenario in {folder} matches {part!r}")
        matched_ids.update(matches)
    matching = [scenario for scenario in scenarios if scenario.id in matched_ids]
    if provider is None:
        return matching, []
    chosen = [scenario for scenario in matching if provider in scenario.providers]
    skipped = [scenario.id for scenario in matching if provider not in scenario.providers]
    if not chosen:
        raise RecordError(f"no scenario that matches {pattern!r} lists {provider}")
    return chosen, skipped


# --- The request ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    scheme: str
    host: str
    port: int | None
    path: str  # the request path: the base URL's path plus the provider's

    @property
    def url(self) -> str:
        port = "" if self.port is None else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}{self.path}"


def _target(base_url: str, request: RawRequest) -> _Target:
    url = urlsplit(base_url)
    if url.scheme not in ("http", "https") or not url.hostname:
        raise RecordError(f"base URL {base_url!r} must be http:// or https:// with a host")
    return _Target(url.scheme, url.hostname, url.port, url.path.rstrip("/") + request.path)


def _encode(body: JsonObject) -> bytes:
    return (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode()


def _headers(
    provider: Provider, request: RawRequest, target: _Target, key: str, length: int
) -> dict[str, str]:
    # Every header is set here, so --dry-run prints exactly what http.client sends.
    host = target.host if target.port is None else f"{target.host}:{target.port}"
    return {
        "Host": host,
        **provider.auth_headers(key),
        **request.headers,
        "User-Agent": f"wiretruth/{__version__}",
        "Accept-Encoding": "identity",
        "Content-Length": str(length),
    }


def describe_request(provider: Provider, request: RawRequest, base_url: str) -> str:
    """The request as it would be sent, with ``***`` in place of the key."""
    target = _target(base_url, request)
    body = _encode(request.body)
    headers = _headers(provider, request, target, "***", len(body))
    lines = [f"{request.method} {target.url}"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    return "\n".join(lines) + "\n\n" + body.decode()


def api_key(provider: Provider, env: Mapping[str, str]) -> str:
    """The key from the environment. The error never includes the value."""
    key = env.get(provider.KEY_ENV, "").strip()
    if not key:
        raise RecordError(f"{provider.KEY_ENV} is not set")
    if not _HEADER_SAFE.fullmatch(key):
        raise RecordError(f"{provider.KEY_ENV} holds characters an HTTP header can't carry")
    return key


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    pieces: list[bytes]
    end: str


def send(provider: Provider, request: RawRequest, base_url: str, key: str) -> Response:
    """Send one raw HTTP request and collect the response, including an aborted body."""
    target = _target(base_url, request)
    body = _encode(request.body)
    headers = _headers(provider, request, target, key, len(body))
    connection_class = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(target.host, target.port, timeout=TIMEOUT_SECONDS)
    try:
        failed = f"{request.method} {target.url} failed"
        try:
            connection.request(request.method, target.path, body=body, headers=headers)
        except (OSError, http.client.HTTPException) as error:
            raise RecordError(f"{failed}: {error}") from None
        try:
            response = connection.getresponse()
        except OSError as error:
            raise RecordError(f"{failed}: {error}") from None
        except http.client.HTTPException as error:  # its text can quote the response, so not shown
            raise RecordError(
                f"{failed}: the response isn't valid HTTP ({type(error).__name__})"
            ) from None
        stored = {
            name: value
            for name in STORED_HEADERS
            if (value := response.getheader(name)) is not None
        }
        pieces: list[bytes] = []
        end = "clean"
        try:
            while piece := response.read1(_READ_SIZE):
                pieces.append(piece)
        except TimeoutError:
            message = f"{target.url}: no data for {TIMEOUT_SECONDS:g} s inside the response body"
            raise RecordError(message) from None
        except (OSError, http.client.IncompleteRead):
            end = "abort"
        except http.client.HTTPException as error:  # chunk framing http.client can't read
            message = f"{target.url}: the response body is malformed ({type(error).__name__})"
            raise RecordError(message) from None
        if response.length:  # a Content-Length body that stopped short
            end = "abort"
        return Response(response.status, stored, pieces, end)
    finally:
        connection.close()


# --- The fixture ----------------------------------------------------------------------------------


def _sse_events(text: str) -> list[str] | None:
    """The stream cut after each blank line, or None when something follows the last one."""
    events: list[str] = []
    position = 0
    for found in _EVENT_END.finditer(text):
        events.append(text[position : found.end()])
        position = found.end()
    return events if position == len(text) else None


def response_body(response: Response, keep_bytes: bool) -> tuple[JsonObject, list[str]]:
    """Store a response as JSON, events, text or the byte chunks read."""
    raw = b"".join(response.pieces)
    as_read: JsonObject = {
        "chunks_b64": [base64.b64encode(piece).decode() for piece in response.pieces]
    }
    if keep_bytes:
        return as_read, []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return as_read, ["the body isn't UTF-8: stored as the chunks read"]
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media == "text/event-stream":
        events = _sse_events(text)
        if events is None:
            message = "the event stream doesn't split into events that each end with one blank line"
            return as_read, [f"{message}: stored as read"]
        return {"sse": cast("list[JsonValue]", events)}, []
    if response.end == "abort":
        return as_read, []
    if media == "application/json" or media.endswith("+json"):
        try:
            return {"json": parse_json(raw)}, []
        except (ValueError, RecursionError):
            return {"text": text}, ["the JSON body doesn't parse strictly: stored as text"]
    return {"text": text}, []


def exchange_document(
    request: RawRequest, base_url: str, response: Response, body: JsonObject
) -> JsonObject:
    """The fixture exchange for a response and its stored body."""
    return {
        "match": {"method": request.method, "path": _target(base_url, request).path},
        "response": {
            "status": response.status,
            "headers": dict(response.headers),
            "body": body,
            "end": response.end,
        },
    }


def _source_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and _SHA.fullmatch(commit) else None


def _ci_run(env: Mapping[str, str]) -> str | None:
    server = env.get("GITHUB_SERVER_URL", "")
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if server.startswith("https://") and repository and run_id.isdigit():
        return f"{server}/{repository}/actions/runs/{run_id}"
    return None


@dataclass(frozen=True)
class Destination:
    """Where a new recording goes, and where the fixture it replaces moves, if it is kept."""

    path: Path
    previous: tuple[Path, JsonObject] | None  # the dated path, and the old fixture with its new id
    warnings: tuple[str, ...]


def destination(root: Path, provider: str, scenario: str, *, replace: bool) -> Destination:
    """Plan where a recording goes, before anything is sent.

    The new recording is always ``<scenario>.json``. An existing one moves to
    ``<scenario>@<the date it was recorded>.json``, unless ``replace`` is set. Raises RecordError
    when the existing fixture can't be dated or its dated path is taken.
    """
    folder = root / "suite" / "fixtures" / provider
    path = folder / f"{scenario}.json"
    if replace or not path.exists():
        return Destination(path, None, ())
    advice = f"pass --replace to overwrite {path}, or move it by hand"
    try:
        old = json_object(parse_json(path.read_bytes()))
    except (OSError, ValueError, RecursionError) as error:
        message = f"can't read {path} to keep it as a dated variant ({error}); {advice}"
        raise RecordError(message) from None
    day = _RECORDED_DAY.match(
        json_str(json_object((old or {}).get("provenance")), "recorded_at") or ""
    )
    if old is None or day is None:
        raise RecordError(f"{path} has no recorded_at to date it by; {advice}")
    dated = folder / f"{scenario}@{day[1]}.json"
    if dated.exists():
        raise RecordError(f"{path} would move to {dated}, which exists; {advice}")
    old_id = f"{provider}/{scenario}"
    new_id = f"{provider}/{dated.stem}"
    warnings = [f"kept the previous recording as {dated}"]
    derived = sorted(
        str(other)
        for other in (root / "suite" / "fixtures").glob("*/*.json")
        if _derived_from(other) == old_id
    )
    if derived:
        warnings.append(
            f"derived from {old_id}, which is now the new recording: {', '.join(derived)}; "
            f"derive them again, or point them at {new_id}"
        )
    return Destination(path, (dated, {**old, "id": new_id}), tuple(warnings))


def _derived_from(path: Path) -> str | None:
    try:
        doc = json_object(parse_json(path.read_bytes()))
    except (OSError, ValueError, RecursionError):
        return None
    return json_str(json_object((doc or {}).get("derived_from")), "fixture")


def _dump(doc: JsonObject) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def _write(path: Path, text: str, previous: tuple[Path, JsonObject] | None) -> None:
    """Write the new fixture and, if there is one, the dated old one: both or neither."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = path.with_name(f".{path.name}.new")
    started: list[Path] = []  # before each write, since a failed write can leave part of a file
    try:
        started.append(new)
        new.write_text(text, encoding="utf-8")
        if previous is not None:
            dated, old = previous
            started.append(dated)
            dated.write_text(_dump(old), encoding="utf-8")
        new.replace(path)
    except OSError:
        for leftover in started:
            leftover.unlink(missing_ok=True)
        raise


def _holds(text: str, key: str) -> bool:
    return any(key in layer for layer in layers(text))


def _secrets(where: str, text: str, doc: JsonValue, key: str) -> list[str]:
    """What the secret scan finds in ``text`` (and ``doc``, its parsed form), and the key itself,
    escaped or not."""
    found = [
        *([f"{where}: the API key itself"] if _holds(text, key) else []),
        *(f"{where} {finding}" for finding in scan_text(text)),
        *(f"{where} {finding}" for finding in scan_headers(doc)),
    ]
    # A header finding's path can run through an object key that holds the API key.
    hidden = f"{where}: a finding whose path holds the API key (not shown)"
    return [hidden if _holds(line, key) else line for line in found]


def _stop_on(findings: list[str]) -> None:
    if findings:
        raise RecordError(
            "the secret scan stopped the recording, nothing was written:\n  "
            + "\n  ".join(findings)
        )


def record_scenario(
    provider: Provider,
    scenario: Scenario,
    *,
    model: str,
    key: str,
    root: Path,
    base_url: str,
    env: Mapping[str, str],
    replace: bool = False,
    keep_bytes: bool = False,
) -> Recording:
    """Send the scenario's call, and write the response as a fixture (see ``destination``)."""
    request = provider.build_request(scenario.call_for(provider.ID), model)
    target = destination(
        root, provider.ID, scenario.id, replace=replace
    )  # before paying for a call
    recorded_at = datetime.now(UTC)
    response = send(provider, request, base_url, key)
    raw = b"".join(response.pieces)
    raw_text = raw.decode("utf-8", "replace")
    _stop_on(_secrets("raw body", raw_text, raw_text, key))  # before a parser error can quote it
    body, warnings = response_body(response, keep_bytes)
    if response.end == "abort":
        warnings.append("the connection closed before the response ended: stored as end: abort")
    exchange = exchange_document(request, base_url, response, body)
    try:
        truth = provider.extract_truth([build_exchange(exchange)], request.body)
    except ValueError as error:
        raise RecordError(f"can't compute the truth: {error}") from None
    path = target.path
    fixture: JsonObject = {
        "format": "wiretruth.fixture/v1",
        "id": f"{provider.ID}/{scenario.id}",
        "scenario": scenario.id,
        "provider": provider.ID,
        "tier": "recorded",
        "provenance": {
            "recorded_at": recorded_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "requested_model": model,
            "returned_model": provider.returned_model(build_exchange(exchange)),
            "recorder": f"wiretruth {__version__}",
            "source_commit": _source_commit(root),
            "ci_run": _ci_run(env),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "request": request.body,
            "notes": "",
        },
        "derived_from": None,
        "exchanges": [exchange],
        "truth": truth,
    }
    text = _dump(fixture)
    _stop_on(_secrets("fixture", text, fixture, key))
    problems = validate("fixture", fixture, str(path))
    if problems:  # a recorder bug: the fixture it built is invalid
        raise FormatError(problems)
    _write(path, text, target.previous)
    return Recording(path, fixture, (*warnings, *target.warnings))


def summary(recording: Recording) -> str:
    """One line about a written fixture: status, finish, usage, returned model."""
    fixture = recording.fixture
    truth = _object(fixture["truth"])
    usage = _object(truth["usage"])
    status = _object(_object(_list(fixture["exchanges"])[0])["response"])["status"]
    model = _object(fixture["provenance"])["returned_model"]
    return (
        f"{recording.path}: HTTP {status}, finish {truth['finish']} "
        f"(raw {truth['raw_finish']}), input {usage['input']} / output {usage['output']} tokens, "
        f"model {model}"
    )


def _object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def _list(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value
