import json
from copy import deepcopy

import pytest
from gemini_samples import MODEL, STREAM, STREAM_ERROR, STREAM_PATH, TEXT, event, exchange

from wiretruth.formats import Body, build_exchange, validate
from wiretruth.providers import body_text, gemini
from wiretruth.transforms import derive


def source():
    return {
        "format": "wiretruth.fixture/v1",
        "id": "gemini/truncation.text-stream",
        "scenario": "truncation.text-stream",
        "provider": "gemini",
        "tier": "documented",
        "provenance": {
            "source_url": "https://ai.google.dev/api/generate-content",
            "notes": "Synthetic unit-test stream; repeated usage placement is assumed.",
            "requested_model": MODEL,
            "returned_model": MODEL,
            "request": {},
        },
        "derived_from": None,
        "exchanges": [
            {
                "match": {"method": "POST", "path": STREAM_PATH},
                "response": {
                    "status": 200,
                    "headers": {"content-type": "text/event-stream"},
                    "body": {"sse": STREAM},
                    "end": "clean",
                },
            }
        ],
        "truth": gemini.extract_truth([exchange(events=STREAM)], None),
    }


def run(transform, args, doc=None):
    result = derive(
        source() if doc is None else doc,
        transform=transform,
        args=args,
        out="gemini/stream.derived-test",
    )
    assert validate("fixture", result, "unit-test") == []
    return result


def test_gemini_strip_finish_and_cut_stream():
    for transform, args in [("strip_finish", {}), ("cut_stream_after", {"n": 2, "end": "abort"})]:
        doc = run(transform, args)
        response = doc["exchanges"][0]["response"]
        assert response["body"] == {"sse": STREAM[:2]}
        assert response["end"] == ("abort" if args else "clean")
        assert doc["truth"]["finish"] == "none"
        assert doc["truth"]["stream_completed"] is False


def test_gemini_inject_error_event():
    error = json.loads(STREAM_ERROR.removeprefix("data: "))
    result = run("inject_error_event", {"after": 1, "error": error})
    assert result["exchanges"][0]["response"]["body"] == {"sse": [STREAM[0], STREAM_ERROR]}
    assert result["truth"]["finish"] == "error"
    assert result["truth"]["error"]["status"] is None


def test_gemini_finish_replacement_changes_only_first_candidate_field():
    doc = source()
    saved = deepcopy(doc)
    result = run("replace_finish_value", {"value": "WIRETRUTH_UNKNOWN_REASON"}, doc)
    assert doc == saved
    assert result["exchanges"][0]["response"]["body"]["sse"] == [
        *STREAM[:-1],
        STREAM[-1].replace('"MAX_TOKENS"', '"WIRETRUTH_UNKNOWN_REASON"'),
    ]
    assert result["truth"]["finish"] == "other"
    assert result["truth"]["raw_finish"] == "WIRETRUTH_UNKNOWN_REASON"
    payload = deepcopy(TEXT)
    payload["candidates"].append({"finishReason": "MAX_TOKENS"})
    changed = gemini.replace_finish(Body("json", payload, ()), "FUTURE")
    assert changed["json"]["candidates"][0]["finishReason"] == "FUTURE"
    assert changed["json"]["candidates"][1]["finishReason"] == "MAX_TOKENS"
    assert payload["candidates"][0]["finishReason"] == "STOP"


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": "), (",", ": ")])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_gemini_rewrite_keeps_framing_and_json_serialization(separators, ascii_only):
    payload = {
        "candidates": [{"content": {"parts": [{"text": "Café 東京 🌸"}]}, "finishReason": "STOP"}]
    }
    original = (
        "id: frame-3\r\nevent: message\r\ndata: "
        + json.dumps(payload, separators=separators, ensure_ascii=ascii_only)
        + "\r\n\r\n"
    )
    body = Body("sse", None, (": heartbeat\n\n", original, "data: [DONE]\n\n"))
    assert gemini.finish_index(body.parts) == 1
    changed = gemini.replace_finish(body, "FUTURE")
    assert changed == {
        "sse": [body.parts[0], original.replace('"STOP"', '"FUTURE"'), body.parts[2]]
    }


def test_gemini_rechunk_inside_utf8_keeps_truth_and_bytes():
    raw = "".join(STREAM).encode()
    points = [raw.index(char.encode()) + 1 for char in "éκ東🌸"]
    result = run("rechunk_inside_utf8", {"points": points})
    ex = build_exchange(result["exchanges"][0])
    assert ex.body.kind == "chunks_b64" and body_text(ex).encode() == raw
    assert result["truth"] == source()["truth"]


def test_gemini_prepend_http_error_leaves_final_truth():
    result = run(
        "prepend_http_error",
        {
            "status": 429,
            "headers": {"retry-after": "1"},
            "body": {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
        },
    )
    assert result["exchanges"][1:] == source()["exchanges"]
    assert result["truth"] == source()["truth"]


def test_gemini_transform_helpers_reject_missing_fields_and_wrong_body_kinds():
    assert gemini.finish_index([event({}), "data: [DONE]\n\n"]) is None
    for helper, argument in [(gemini.finish_index, []), (gemini.error_event, {"error": {}})]:
        with pytest.raises(ValueError, match="sse body"):
            helper(argument, kind="ndjson")
    with pytest.raises(ValueError, match="error object"):
        gemini.error_event({"error": "busy"})
    with pytest.raises(ValueError, match="json or sse"):
        gemini.replace_finish(Body("ndjson", None, ("{}",)), "FUTURE")
    for body in [Body("json", {}, ()), Body("json", TEXT, ()), Body("sse", None, (event(TEXT),))]:
        with pytest.raises(ValueError, match="no finish value"):
            gemini.replace_finish(body, "STOP")
