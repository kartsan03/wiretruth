import json
from copy import deepcopy
from dataclasses import replace

import pytest
from ollama_samples import PROBES, exchange

from wiretruth.formats import Body, build_exchange
from wiretruth.providers import body_text, ollama
from wiretruth.transforms import derive


def source():
    probe = PROBES["n-text-stream"]
    return {
        "format": "wiretruth.fixture/v1",
        "id": "ollama/truncation.text-stream",
        "scenario": "truncation.text-stream",
        "provider": "ollama",
        "tier": "documented",
        "provenance": {
            "source_url": "https://docs.ollama.com/api/chat",
            "notes": "Unit-test body from n-text-stream, Ollama v0.40.3.",
            "requested_model": ollama.DEFAULT_MODEL,
            "returned_model": ollama.DEFAULT_MODEL,
            "request": probe["request"],
        },
        "derived_from": None,
        "exchanges": [
            {
                "match": {"method": "POST", "path": "/api/chat"},
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/x-ndjson"},
                    "body": {"ndjson": probe["body"].splitlines()},
                    "end": "clean",
                },
            }
        ],
        "truth": ollama.extract_truth([exchange("n-text-stream")], probe["request"]),
    }


def run(transform, args, doc=None):
    return derive(
        source() if doc is None else doc,
        transform=transform,
        args=args,
        out="ollama/stream.derived-test",
    )


def body(doc):
    return doc["exchanges"][-1]["response"]["body"]


def test_ndjson_cut_stream_after():
    doc = run("cut_stream_after", {"n": 3, "end": "abort"})
    assert body(doc)["ndjson"] == body(source())["ndjson"][:3]
    assert doc["truth"]["finish"] == "none"
    assert doc["truth"]["stream_completed"] is False
    assert doc["exchanges"][-1]["response"]["end"] == "abort"


def test_ndjson_strip_finish():
    doc = run("strip_finish", {})
    assert body(doc)["ndjson"] == body(source())["ndjson"][:-1]
    assert doc["truth"]["finish"] == "none"
    assert doc["truth"]["stream_completed"] is False


def test_ndjson_inject_error_event():
    doc = run(
        "inject_error_event",
        {"after": 3, "error": {"error": "an error was encountered while running the model"}},
    )
    assert body(doc)["ndjson"][:-1] == body(source())["ndjson"][:3]
    assert body(doc)["ndjson"][-1] == '{"error":"an error was encountered while running the model"}'
    assert doc["truth"]["error"]["status"] is None
    assert doc["truth"]["finish"] == "error"


def test_ndjson_replace_finish_value():
    original = source()
    saved = deepcopy(original)
    doc = run("replace_finish_value", {"value": "future_reason"}, original)
    assert original == saved
    assert body(doc)["ndjson"] == [
        *body(original)["ndjson"][:-1],
        body(original)["ndjson"][-1].replace(
            '"done_reason":"length"', '"done_reason":"future_reason"'
        ),
    ]
    assert (doc["truth"]["finish"], doc["truth"]["raw_finish"]) == ("other", "future_reason")


def test_ndjson_rechunk_inside_utf8():
    doc = source()
    # n-text-stream contains only ASCII; substitute a neutral multi-byte content piece.
    lines = body(doc)["ndjson"]
    lines[0] = lines[0].replace('"content":"The"', '"content":"Café 東京 🌸"')
    raw = body_text(build_exchange(doc["exchanges"][0])).encode()
    expected = ollama.extract_truth([build_exchange(doc["exchanges"][0])], None)
    points = [raw.index(char.encode()) + 1 for char in "é東🌸"]
    result = run("rechunk_inside_utf8", {"points": points}, doc)
    assert list(body(result)) == ["chunks_b64"]
    assert body_text(build_exchange(result["exchanges"][0])).encode() == raw
    assert result["truth"] == expected


def test_ndjson_prepend_http_error():
    doc = run("prepend_http_error", {"status": 503, "headers": {}, "body": {"error": "busy"}})
    assert doc["exchanges"][1:] == source()["exchanges"]
    assert doc["truth"] == source()["truth"]


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": "), (",", ": ")])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_ndjson_finish_rewrite_preserves_each_lines_serialization(separators, ascii_only):
    ex = exchange("n-text-stream")
    payload = json.loads(ex.body.parts[-1])
    payload["message"]["content"] = "Café 東京"
    line = json.dumps(payload, separators=separators, ensure_ascii=ascii_only)
    changed = ollama.replace_finish(replace(ex.body, parts=(*ex.body.parts[:-1], line)), "other")
    assert changed["ndjson"] == [*ex.body.parts[:-1], line.replace('"length"', '"other"')]


def test_native_finish_helpers_reject_unusable_bodies():
    assert ollama.finish_index(['{"done":false}']) is None
    for helper, args in [(ollama.finish_index, ([],)), (ollama.error_event, ({"error": "bad"},))]:
        with pytest.raises(ValueError, match="ndjson"):
            helper(*args, kind="sse")
    with pytest.raises(ValueError, match="error string"):
        ollama.error_event({"error": {"message": "bad"}})
    with pytest.raises(ValueError, match="json or ndjson"):
        ollama.replace_finish(Body("sse", None, ()), "stop")
    for b in [
        Body("json", {}, ()),
        Body("ndjson", None, ("{}",)),
        Body("ndjson", None, ('{"done":false,"done_reason":"stop"}',)),
        Body("ndjson", None, ('{"done":true,"done_reason":"stop"}',)),
    ]:
        with pytest.raises(ValueError, match="no finish value"):
            ollama.replace_finish(b, "stop")
    with pytest.raises(ValueError, match="serialization"):
        ollama.replace_finish(
            Body("ndjson", None, ('{ "done":true, "done_reason":"stop" }',)), "other"
        )
    assert ollama.replace_finish(
        Body("json", {"done": True, "done_reason": "stop"}, ()), "other"
    ) == {"json": {"done": True, "done_reason": "other"}}
