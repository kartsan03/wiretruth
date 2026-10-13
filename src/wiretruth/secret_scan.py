"""The secret scan: credentials that must never reach a fixture or any other committed file.

The recorder runs it before it writes a fixture, and ``wiretruth lint`` runs it on every file it
reads. A finding names the pattern and where it matched, never the matched text, so the scan's own
output can't leak what it found.

A credential can hide behind JSON escapes (``\\u0073k-…`` is ``sk-…`` to any JSON parser), and a
fixture holds JSON inside JSON strings: a stream event, a body stored as text. So the text scan
also reads each layer of escapes decoded, and the header scan reads the JSON inside strings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from wiretruth.formats import JsonValue, json_path

# Prefixes are matched only at the start of a word, so prose such as "desk-based-research-notes"
# doesn't count as an "sk-" key. Files hold JSON (and JSON inside JSON strings, in stream events),
# so a JSON escape such as \n or \u00a0 also starts a word: "Key:\nsk-..." is caught.
_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[bfnrt])|(?<=\\u[0-9A-Fa-f]{4}))"

PATTERNS: dict[str, re.Pattern[str]] = {
    "an sk- API key": re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}"),
    # The names are printed, so they must not match a pattern themselves.
    "an OpenAI project key (sk-proj prefix)": re.compile(_START + r"sk-proj-"),
    "an Anthropic key (sk-ant prefix)": re.compile(_START + r"sk-ant-"),
    "a Google API key (AIza…)": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "a Google API key (AQ.…)": re.compile(r"AQ\.[0-9A-Za-z_-]{30,}"),
    "a bearer token": re.compile(r"Bearer [A-Za-z0-9._-]{16,}", re.IGNORECASE),
    "an OpenAI organization id (org-…)": re.compile(_START + r"org-[A-Za-z0-9]{16,}"),
    "an OpenAI project id (proj_…)": re.compile(_START + r"proj_[A-Za-z0-9]{16,}"),
}

# Header names that carry credentials or account ids. Found in any "headers" object, wherever it
# sits in a document, they are a finding whatever their value. The replay server leaves the same
# names out of its request log.
SECRET_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-goog-api-key",
        "api-key",
        "x-auth-token",
        "cookie",
        "set-cookie",
        "openai-organization",
        "openai-project",
        "x-goog-user-project",
    }
)


@dataclass(frozen=True)
class Finding:
    """``where`` is ``line N`` in scanned text, or a JSON path for a header name."""

    where: str
    what: str

    def __str__(self) -> str:
        return f"{self.where}: {self.what}"


# One JSON string escape. \n and \r stay as they are when decoded (see layers).
_ESCAPE = re.compile(r'\\(?:u([0-9A-Fa-f]{4})|(["\\/bfnrt]))')
_SIMPLE = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "t": "\t"}

# How many layers of escapes the scan decodes, and how many levels of JSON inside strings the
# header scan opens. A fixture needs about four: a tool call's arguments, in a stream event, in the
# fixture. The bound keeps the work linear: each layer is a pass over the whole text.
MAX_LAYERS = 16


def _decode(match: re.Match[str]) -> str:
    char = _SIMPLE.get(match[2]) if match[1] is None else chr(int(match[1], 16))
    return match[0] if char is None or char in "\n\r" else char


def layers(text: str) -> Iterator[str]:
    """``text``, then ``text`` with one more layer of JSON string escapes decoded, until no escape
    is left to decode or MAX_LAYERS are decoded. Newline escapes stay escaped, so a line number
    holds in every layer; a pattern counts an escape as the start of a word anyway."""
    for _ in range(MAX_LAYERS):
        yield text
        decoded = _ESCAPE.sub(_decode, text)
        if decoded == text:
            return
        text = decoded
    yield text


def scan_text(text: str) -> list[Finding]:
    """Every pattern match in ``text`` and in its decoded layers, in order of position. A match
    that only a decoded layer shows is marked as JSON-escaped. Escapes nested deeper than
    MAX_LAYERS are a finding too, since the scan can't see what they hide."""
    found: dict[tuple[int, str, str], tuple[int, Finding]] = {}
    layer = text
    for depth, layer in enumerate(layers(text)):
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(layer):
                line = layer.count("\n", 0, match.start()) + 1
                what = f"looks like {name}" + (", JSON-escaped" if depth else "")
                found.setdefault(
                    (line, name, match[0]), (match.start(), Finding(f"line {line}", what))
                )
    deeper = next((match for match in _ESCAPE.finditer(layer) if _decode(match) != match[0]), None)
    if deeper is not None:
        line = layer.count("\n", 0, deeper.start()) + 1
        what = f"JSON escapes nested more than {MAX_LAYERS} deep, which the scan doesn't read"
        found[(line, "", "")] = (deeper.start(), Finding(f"line {line}", what))
    ordered = sorted(found.items(), key=lambda item: (item[0][0], item[1][0]))
    return [finding for _, (_, finding) in ordered]


def scan_headers(doc: JsonValue) -> list[Finding]:
    """Credential header names in any ``headers`` object of a parsed document, including the JSON
    inside its strings (a whole string, or a line of one, such as a stream event's ``data:``), up
    to MAX_LAYERS levels deep."""
    found: list[Finding] = []
    # Depth first, in document order, without recursion: a document can nest deeper than the
    # Python stack. ``outer`` locates the string a value was parsed from; ``level`` counts those.
    todo: list[Finding | tuple[JsonValue, list[str | int], str, int]] = [(doc, [], "", 0)]
    while todo:
        item = todo.pop()
        if isinstance(item, Finding):
            found.append(item)
            continue
        value, path, outer, level = item
        children: list[Finding | tuple[JsonValue, list[str | int], str, int]] = []
        if isinstance(value, str) and level < MAX_LAYERS:
            if docs := _embedded(value):
                where = f"{outer}{_where(path)}, in its JSON at "
                children = [(inner, [], where, level + 1) for inner in docs]
        elif isinstance(value, list):
            children = [(child, [*path, i], outer, level) for i, child in enumerate(value)]
        elif isinstance(value, dict):
            for key, child in value.items():
                if key == "headers" and isinstance(child, dict):
                    children += [
                        Finding(
                            outer + _where([*path, key, name]),
                            f"the {name.lower()} header carries credentials",
                        )
                        for name in child
                        if name.lower() in SECRET_HEADERS
                    ]
                children.append((child, [*path, key], outer, level))
        todo.extend(reversed(children))
    return found


def _embedded(text: str) -> list[JsonValue]:
    whole = _json(text)
    if whole is not None:
        return [whole]
    lines = text.splitlines()
    docs: list[JsonValue] = [doc for line in lines if (doc := _json(line)) is not None]
    # A stream event can split its JSON over several data lines, which join with newlines.
    data: list[str] = []
    for line in [*lines, ""]:
        if line.startswith("data:"):
            data.append(line.removeprefix("data:").removeprefix(" "))
        elif not line:
            if len(data) > 1 and (doc := _json("\n".join(data))) is not None:
                docs.append(doc)
            data = []
    return docs


def _json(text: str) -> JsonValue | None:
    """``text`` as a JSON object or array, after an optional ``data:`` field name; else None."""
    text = text.strip().removeprefix("data:").strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return cast(JsonValue, json.loads(text))
    except (ValueError, RecursionError):
        return None


def _where(path: list[str | int]) -> str:
    """The JSON path, unless one of its keys is what the text scan looks for. JSON inside a string
    can decode to a lone surrogate, which UTF-8 can't print, so it's written as its escape."""
    where = json_path(path).encode("utf-8", "backslashreplace").decode("utf-8")
    return "a path the secret scan flags (not shown)" if scan_text(where) else where
