import json
from copy import deepcopy

import pytest
from anthropic_samples import OVERLOADED, STREAM, TEXT, event, source

from wiretruth.formats import Body, build_exchange, validate
from wiretruth.providers import anthropic, body_text
from wiretruth.transforms import derive


def run(name, args, doc=None):
    result = derive(
        source() if doc is None else doc,
        transform=name,
        args=args,
        out="anthropic/stream.test-result",
    )
    assert validate("fixture", result, "test") == []
    return result


def test_anthropic_strip_finish_also_drops_message_stop():
    result = run("strip_finish", {})
    assert result["exchanges"][0]["response"]["body"]["sse"] == STREAM[:-2]
    assert result["truth"]["finish"] == "none" and result["truth"]["stream_completed"] is False
    assert run("cut_stream_after", {"n": 4, "end": "abort"})["truth"]["text"] == "Hello"


def test_anthropic_injects_literal_documented_error():
    result = run("inject_error_event", {"after": 4, "error": OVERLOADED})
    assert result["exchanges"][0]["response"]["body"]["sse"] == [
        *STREAM[:4],
        (
            'event: error\ndata: {"type": "error", "error": {"type": "overloade'
            'd_error", "message": "Overloaded"}}\n\n'
        ),
    ]
    assert result["truth"]["finish"] == "error" and result["truth"]["error"]["status"] is None


def test_anthropic_replace_finish_preserves_documented_mixed_spacing():
    original = source()
    saved = deepcopy(original)
    changed = run("replace_finish_value", {"value": "WIRETRUTH_UNKNOWN_REASON"}, original)
    assert original == saved
    assert changed["exchanges"][0]["response"]["body"]["sse"] == [
        *STREAM[:-2],
        STREAM[-2].replace('"end_turn"', '"WIRETRUTH_UNKNOWN_REASON"'),
        STREAM[-1],
    ]
    assert changed["truth"]["finish"] == "other" and changed["truth"]["stream_completed"]
    payload = deepcopy(TEXT)
    replaced = anthropic.replace_finish(Body("json", payload, ()), "FUTURE")
    assert replaced["json"] == {**TEXT, "stop_reason": "FUTURE"} and payload == TEXT


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": "), (",", ": ")])
def test_anthropic_replace_keeps_framing_and_escapes(separators):
    payload = {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 15},
    }
    frame = (
        "id: final\r\nevent: message_delta\r\ndata: "
        + json.dumps(payload, separators=separators)
        + "\r\n\r\n"
    )
    body = Body("sse", None, (": ping\n\n", frame, STREAM[-1]))
    assert anthropic.finish_index(body.parts) == 1
    assert anthropic.replace_finish(body, 'new"value')["sse"] == [
        body.parts[0],
        frame.replace('"end_turn"', json.dumps('new"value')),
        body.parts[2],
    ]
    payload["delta"] = {"stop_sequence": None, "stop_reason": "end_turn"}
    reordered = "event: message_delta\ndata: " + json.dumps(payload, separators=separators) + "\n\n"
    assert anthropic.replace_finish(Body("sse", None, (reordered,)), "FUTURE") == {
        "sse": [reordered.replace('"end_turn"', '"FUTURE"')]
    }


def test_anthropic_transform_rejections():
    assert (
        anthropic.finish_index([": heartbeat\n\n", event({"type": "message_delta", "delta": {}})])
        is None
    )
    for fn, arg in [(anthropic.finish_index, []), (anthropic.error_event, OVERLOADED)]:
        with pytest.raises(ValueError, match="sse body"):
            fn(arg, kind="ndjson")
    for payload in [{}, {"type": "error", "error": "busy"}]:
        with pytest.raises(ValueError, match="error object"):
            anthropic.error_event(payload)
    for body in [
        Body("json", {}, ()),
        Body("json", TEXT, ()),
        Body("sse", None, tuple(STREAM)),
        Body("sse", None, (": comment\n\n",)),
    ]:
        with pytest.raises(ValueError, match="no finish value"):
            anthropic.replace_finish(body, "end_turn")
    with pytest.raises(ValueError, match="json or sse"):
        anthropic.replace_finish(Body("text", "", ()), "new")
    escaped = STREAM[-2].replace("stop_reason", "stop\\u005freason")
    with pytest.raises(ValueError, match="can't preserve"):
        anthropic.replace_finish(Body("sse", None, (escaped,)), "new")


def test_anthropic_rechunk_preserves_bytes_and_truth():
    doc = source()
    doc["exchanges"][0]["response"]["body"]["sse"] = [
        part.replace("Hello", "Café 東京 🌸") for part in STREAM
    ]
    raw = body_text(build_exchange(doc["exchanges"][0])).encode()
    points = [raw.index(char.encode()) + 1 for char in "é東🌸"]
    result = run("rechunk_inside_utf8", {"points": points}, doc)
    assert body_text(build_exchange(result["exchanges"][0])).encode() == raw
    assert result["truth"]["text"] == "Café 東京 🌸!"
    assert result["truth"]["stream_completed"]
