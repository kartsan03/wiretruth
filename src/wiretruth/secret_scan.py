"""The secret scan: credentials that must never reach a fixture or any other committed file.

The recorder runs it before it writes a fixture, and ``wiretruth lint`` runs it on every file it
reads. A finding names the pattern and where it matched, never the matched text, so the scan's own
output can't leak what it found.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from wiretruth.formats import JsonValue, json_path

# Prefixes are matched only at the start of a word, so prose such as "desk-based-research-notes"
# doesn't count as an "sk-" key. Files hold JSON (and JSON inside JSON strings, in stream events),
# so a JSON escape such as \n or \u00a0 also starts a word: "Key:\nsk-..." is caught.
_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[bfnrt])|(?<=\\u[0-9A-Fa-f]{4}))"

PATTERNS: dict[str, re.Pattern[str]] = {
    "an sk- API key": re.compile(_START + r"sk-[A-Za-z0-9_-]{16,}"),
    "an OpenAI project key prefix (sk-proj-)": re.compile(_START + r"sk-proj-"),
    "an Anthropic key prefix (sk-ant-)": re.compile(_START + r"sk-ant-"),
    "a Google API key (AIza…)": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
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


def scan_text(text: str) -> list[Finding]:
    """Every pattern match in ``text``, in order of position."""
    found: list[tuple[int, Finding]] = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append((match.start(), Finding(f"line {line}", f"looks like {name}")))
    return [finding for _, finding in sorted(found, key=lambda item: item[0])]


def scan_headers(doc: JsonValue) -> list[Finding]:
    """Credential header names in any ``headers`` object of a parsed document."""
    return list(_headers(doc, []))


def _headers(value: JsonValue, path: list[str | int]) -> Iterator[Finding]:
    if isinstance(value, list):
        for i, item in enumerate(value):
            yield from _headers(item, [*path, i])
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "headers" and isinstance(item, dict):
                for name in item:
                    if name.lower() in SECRET_HEADERS:
                        where = json_path([*path, key, name])
                        yield Finding(where, f"the {name.lower()} header carries credentials")
            yield from _headers(item, [*path, key])
