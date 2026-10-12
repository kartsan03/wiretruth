import base64
import json
from copy import deepcopy
from pathlib import Path

import pytest

from wiretruth.formats import Body, FormatError, build_exchange, load_document, validate
from wiretruth.providers import PROVIDERS, body_text, event_object, openai_chat, openai_responses
from wiretruth.recorder import _dump
from wiretruth.transforms import MAX_CHUNKS, TRANSFORMS, derive

ROOT = Path(__file__).resolve().parents[2]
CHAT = "openai-chat/truncation.text-stream-usage"
RESPONSES = "openai-responses/truncation.text-stream"
TOOLS = "openai-chat/tools.parallel-calls-stream"


def source(name=CHAT):
    return load_document("fixture", ROOT / "suite" / "fixtures" / f"{name}.json")


def response(doc):
    return doc["exchanges"][-1]["response"]


def run(name, args=None, doc=None, **options):
    doc = source() if doc is None else doc
    return derive(
        doc,
        transform=name,
        out=options.pop("out", f"{doc.get('provider', 'openai-chat')}/stream.derived-test"),
        args={} if args is None else args,
        **options,
    )


CASES = [
    ("cut_stream_after", {"n": 3, "end": "abort"}, CHAT),
    ("strip_finish", {}, CHAT),
    ("inject_error_event", {"after": 2, "error": {"error": {"message": "Try again"}}}, CHAT),
    ("rechunk_inside_utf8", {"points": [1596]}, TOOLS),
    ("replace_finish_value", {"value": "future_finish"}, CHAT),
    ("prepend_http_error", {"status": 429, "headers": {"retry-after": "1"}, "body": {}}, CHAT),
]


@pytest.mark.parametrize("name,args,fixture", CASES)
def test_determinism_provenance_and_source_unchanged(name, args, fixture):
    doc = source(fixture)
    original = deepcopy(doc)
    original_args = deepcopy(args)
    first = run(name, args, doc)
    second = run(name, args, doc)
    assert _dump(first) == _dump(second)
    assert doc == original
    assert args == original_args
    assert first["format"] == "wiretruth.fixture/v1"
    assert first["tier"] == "derived"
    assert first["id"] == f"{doc['provider']}/stream.derived-test"
    assert first["scenario"] == "stream.derived-test"
    assert first["provenance"] == {
        key: doc["provenance"][key] for key in ("requested_model", "returned_model", "request")
    }
    assert first["derived_from"] == {
        "fixture": fixture,
        "transform": name,
        "args": args,
        "rationale": TRANSFORMS[name].rationale,
        "evidence_url": TRANSFORMS[name].evidence_url,
    }
    assert validate("fixture", first, "derived") == []
    assert first["truth"] == PROVIDERS[doc["provider"]].extract_truth(
        [build_exchange(exchange) for exchange in first["exchanges"]],
        first["provenance"].get("request"),
    )
    first["derived_from"]["args"]["extra"] = True
    first["provenance"]["request"]["model"] = "changed"
    assert doc == original and args == original_args


@pytest.mark.parametrize("end", ["clean", "abort"])
@pytest.mark.parametrize("fixture", [CHAT, RESPONSES])
def test_cut_keeps_exact_prefix_of_last_exchange(fixture, end):
    doc = source(fixture)
    doc["exchanges"].insert(0, deepcopy(doc["exchanges"][0]))
    result = run("cut_stream_after", {"n": 3, "end": end}, doc)
    assert result["exchanges"][0] == doc["exchanges"][0]
    assert response(result)["body"]["sse"] == response(doc)["body"]["sse"][:3]
    assert response(result)["end"] == end
    assert result["truth"]["stream_completed"] is False


def test_cut_ndjson_keeps_lines():
    doc = source()
    lines = "".join(response(doc)["body"]["sse"]).splitlines()
    response(doc)["body"] = {"ndjson": lines}
    result = run("cut_stream_after", {"n": 4, "end": "abort"}, doc)
    assert response(result)["body"] == {"ndjson": lines[:4]}
    assert result["truth"]["stream_completed"] is False


@pytest.mark.parametrize("fixture,removed", [(CHAT, 3), (RESPONSES, 1)])
def test_strip_finish_recomputes_truth_and_drops_tail(fixture, removed):
    doc = source(fixture)
    doc["truth"]["text"] = "This stored truth is deliberately stale."
    response(doc)["end"] = "abort"
    result = run("strip_finish", doc=doc)
    assert response(result)["body"]["sse"] == response(doc)["body"]["sse"][:-removed]
    assert response(result)["end"] == "clean"
    assert result["truth"]["finish"] == "none"
    assert result["truth"]["raw_finish"] is None
    assert result["truth"]["stream_completed"] is False
    assert result["truth"]["text"] != doc["truth"]["text"]
    assert result["truth"]["usage"]["raw"] is None


@pytest.mark.parametrize(
    "fixture,error,prefix",
    [
        (CHAT, {"error": {"message": "Try again", "type": "server_error"}}, "data: "),
        (RESPONSES, {"type": "error", "message": "Try again"}, "event: error\ndata: "),
        (
            RESPONSES,
            {
                "type": "response.failed",
                "response": {"status": "failed", "error": {"message": "Try again"}},
            },
            "event: response.failed\ndata: ",
        ),
    ],
)
def test_inject_error_framing_and_truth(fixture, error, prefix):
    doc = source(fixture)
    result = run("inject_error_event", {"after": 2, "error": error}, doc)
    events = response(result)["body"]["sse"]
    assert events[:-1] == response(doc)["body"]["sse"][:2]
    assert (
        events[-1] == prefix + json.dumps(error, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    )
    assert response(result)["end"] == "clean"
    assert result["truth"]["finish"] == "error"
    assert result["truth"]["error"]["message"] == "Try again"
    assert result["truth"]["error"]["status"] is None


@pytest.mark.parametrize(
    "fixture,offsets",
    [
        (TOOLS, [1596, 3587]),
        (
            "openai-responses/tools.parallel-calls-stream",
            [3278, 3551, 3884, 4434, 4707, 5037, 5731, 5941],
        ),
    ],
)
@pytest.mark.parametrize("kind", ["sse", "text", "ndjson"])
def test_rechunk_preserves_bytes_and_splits_committed_utf8(fixture, offsets, kind):
    doc = source(fixture)
    raw = body_text(build_exchange(doc["exchanges"][-1])).encode()
    assert [i for i, byte in enumerate(raw) if byte & 0xC0 == 0x80] == offsets
    if kind == "text":
        response(doc)["body"] = {"text": raw.decode()}
    elif kind == "ndjson":
        response(doc)["body"] = {"ndjson": raw.decode().splitlines()}
    result = run("rechunk_inside_utf8", {"points": offsets}, doc)
    chunks = [base64.b64decode(part) for part in response(result)["body"]["chunks_b64"]]
    assert b"".join(chunks) == raw
    assert [sum(map(len, chunks[:i])) for i in range(1, len(chunks))] == offsets
    with pytest.raises(UnicodeDecodeError):
        chunks[0].decode()
    assert response(result)["headers"] == response(doc)["headers"]
    assert response(result)["end"] == response(doc)["end"]
    assert result["truth"] == doc["truth"]


def test_rechunk_every_and_limit_boundary():
    doc = source(TOOLS)
    result = run("rechunk_inside_utf8", {"every": 57}, doc)
    chunks = [base64.b64decode(part) for part in response(result)["body"]["chunks_b64"]]
    assert len(chunks) == 75
    assert all(len(chunk) == 57 for chunk in chunks[:-1])
    assert sum(map(len, chunks[:28])) == 1596
    points = [*range(1, MAX_CHUNKS - 1), 1596]
    result = run("rechunk_inside_utf8", {"points": points}, doc)
    assert len(response(result)["body"]["chunks_b64"]) == MAX_CHUNKS


@pytest.mark.parametrize(
    "fixture,old",
    [
        (CHAT, "length"),
        (RESPONSES, "max_output_tokens"),
        ("openai-chat/truncation.reasoning-only", "length"),
        ("openai-responses/truncation.reasoning-only", "max_output_tokens"),
    ],
)
def test_replace_finish_changes_only_the_finish(fixture, old):
    doc = source(fixture)
    result = run("replace_finish_value", {"value": "future_finish"}, doc)
    assert result["truth"]["raw_finish"] == "future_finish"
    assert result["truth"]["finish"] == "other"
    if "sse" in response(doc)["body"]:
        expected = [
            event.replace(f'"{old}"}}', '"future_finish"}')
            for event in response(doc)["body"]["sse"]
        ]
        assert response(result)["body"]["sse"] == expected
    else:
        assert _dump(result["exchanges"]) == _dump(doc["exchanges"]).replace(
            f': "{old}"', ': "future_finish"'
        )


@pytest.mark.parametrize(
    "body", [None, False, 3, "Retry later", [], {"error": {"message": "Retry later"}}]
)
def test_prepend_http_error_preserves_exchanges_and_final_truth(body):
    doc = source("openai-chat/request.max-tokens-field")
    doc["exchanges"].append(deepcopy(doc["exchanges"][0]))
    doc["exchanges"][-1]["match"]["path"] = "/v1/last"
    args = {
        "status": 503,
        "headers": {"content-type": "application/json", "retry-after": "1"},
        "body": body,
    }
    result = run("prepend_http_error", args, doc)
    assert result["exchanges"][1:] == doc["exchanges"]
    assert result["exchanges"][0] == {
        "match": doc["exchanges"][0]["match"],
        "response": {
            "status": 503,
            "headers": args["headers"],
            "body": {"json": body},
            "end": "clean",
        },
    }
    assert result["truth"] == doc["truth"]
    assert result["truth"]["error"] is None


def test_documented_source_optional_provenance_and_overrides():
    doc = source()
    doc["tier"] = "documented"
    doc["provenance"] = {
        "requested_model": "public-model",
        "source_url": "https://example.com/docs",
        "notes": "Transcribed example.",
    }
    result = run(
        "strip_finish",
        doc=doc,
        rationale="Test a missing finish.",
        evidence_url="https://example.com/evidence",
        out="openai-chat/stream.example@2026-10-12",
    )
    assert result["provenance"] == {"requested_model": "public-model"}
    assert result["scenario"] == "stream.example"
    assert result["derived_from"]["rationale"] == "Test a missing finish."
    assert result["derived_from"]["evidence_url"] == "https://example.com/evidence"


@pytest.mark.parametrize("name,args,fixture", CASES)
def test_all_transforms_reject_missing_or_extra_args(name, args, fixture):
    with pytest.raises(ValueError, match="requires exactly"):
        run(name, {**args, "unexpected": 1}, source(fixture))
    if args:
        with pytest.raises(ValueError, match="requires exactly"):
            run(name, {}, source(fixture))


@pytest.mark.parametrize(
    "name,args,message",
    [
        ("unknown", {}, "unknown transform"),
        ("strip_finish", [], "args must be a JSON object"),
        ("cut_stream_after", {"n": 1, "end": "unknown"}, "end must"),
        ("inject_error_event", {"after": 1, "error": []}, "error must"),
        ("inject_error_event", {"after": 1, "error": {}}, "does not read"),
        ("replace_finish_value", {"value": ""}, "non-empty string"),
        ("replace_finish_value", {"value": 1}, "non-empty string"),
        ("rechunk_inside_utf8", {"every": 1, "points": [1]}, "requires exactly"),
        ("rechunk_inside_utf8", {"every": 0}, "every must"),
        ("rechunk_inside_utf8", {"every": True}, "every must"),
        ("rechunk_inside_utf8", {"every": 1.5}, "every must"),
        ("rechunk_inside_utf8", {"every": 1}, "at most 100 chunks"),
        ("rechunk_inside_utf8", {"points": list(range(1, 101))}, "at most 100 chunks"),
        ("rechunk_inside_utf8", {"points": "1"}, "points must"),
        ("rechunk_inside_utf8", {"points": []}, "no chunk boundary"),
        ("rechunk_inside_utf8", {"points": [1]}, "no chunk boundary"),
        ("rechunk_inside_utf8", {"every": 100000}, "no chunk boundary"),
        ("rechunk_inside_utf8", {"points": [0]}, "each point must"),
        ("rechunk_inside_utf8", {"points": [True]}, "each point must"),
        ("rechunk_inside_utf8", {"points": [1.5]}, "each point must"),
        ("rechunk_inside_utf8", {"points": [1, 1]}, "each point must"),
        ("rechunk_inside_utf8", {"points": [2, 1]}, "each point must"),
        ("rechunk_inside_utf8", {"points": [99999]}, "each point must"),
    ],
)
def test_bad_arguments(name, args, message):
    with pytest.raises(ValueError, match=message):
        run(name, args)


@pytest.mark.parametrize(
    "name,key,extra",
    [("cut_stream_after", "n", {"end": "clean"}), ("inject_error_event", "after", {"error": {}})],
)
@pytest.mark.parametrize("number", [0, -1, True, 1.0, "1", 17, 999])
def test_invalid_event_indices(name, key, extra, number):
    with pytest.raises(ValueError, match=f"{key} must be an integer"):
        run(name, {key: number, **extra})


@pytest.mark.parametrize("status", [399, 600, True, 429.0, "429"])
def test_invalid_http_status(status):
    with pytest.raises(ValueError, match="status must"):
        run("prepend_http_error", {"status": status, "headers": {}, "body": None})


@pytest.mark.parametrize(
    "headers",
    [None, [], {"x-extra": "no"}, {"Content-Type": "application/json"}, {"retry-after": 1}],
)
def test_invalid_http_headers(headers):
    with pytest.raises(ValueError, match="headers must"):
        run("prepend_http_error", {"status": 429, "headers": headers, "body": None})


@pytest.mark.parametrize(
    "name,args",
    [
        ("cut_stream_after", {"n": 1, "end": "clean"}),
        ("strip_finish", {}),
        ("inject_error_event", {"after": 1, "error": {}}),
        ("rechunk_inside_utf8", {"every": 1}),
    ],
)
def test_wrong_body_kind(name, args):
    with pytest.raises(ValueError, match="body"):
        run(name, args, source("openai-chat/request.max-tokens-field"))


def test_ndjson_error_framing_is_unsupported():
    doc = source()
    response(doc)["body"] = {"ndjson": ["{}", "{}"]}
    with pytest.raises(ValueError, match="error events need an sse body"):
        run("inject_error_event", {"after": 1, "error": {}}, doc)


@pytest.mark.parametrize("fixture", [CHAT, RESPONSES])
def test_strip_finish_rejects_sse_stored_as_ndjson(fixture):
    doc = source(fixture)
    events = response(doc)["body"]["sse"]
    response(doc)["body"] = {"ndjson": "".join(events).splitlines()}
    with pytest.raises(ValueError, match="finish events need an sse body"):
        run("strip_finish", doc=doc)


@pytest.mark.parametrize("fixture", [CHAT, RESPONSES])
def test_missing_or_first_finish(fixture):
    doc = source(fixture)
    events = response(doc)["body"]["sse"]
    index = PROVIDERS[doc["provider"]].finish_index(events)
    response(doc)["body"]["sse"] = events[:index]
    with pytest.raises(ValueError, match="no finish event"):
        run("strip_finish", doc=doc)
    response(doc)["body"]["sse"] = events[index:]
    with pytest.raises(ValueError, match="leave no events"):
        run("strip_finish", doc=doc)


def test_source_and_output_validation():
    with pytest.raises(FormatError):
        run("strip_finish", doc={})
    with pytest.raises(ValueError, match="not derived"):
        run("strip_finish", doc=run("strip_finish"))
    with pytest.raises(ValueError, match="output provider"):
        run("strip_finish", out="openai-responses/stream.example")
    doc = source()
    doc["provider"] = "anthropic"
    doc["id"] = "anthropic/truncation.text-stream-usage"
    with pytest.raises(ValueError, match="does not support transforms"):
        run("strip_finish", doc=doc)
    for options in (
        {"out": "openai-chat/../escape"},
        {"out": CHAT},
        {"rationale": ""},
        {"evidence_url": "http://example.com"},
    ):
        with pytest.raises(FormatError):
            run("strip_finish", **options)


def test_replaced_value_must_be_visible_to_truth():
    doc = source()
    events = response(doc)["body"]["sse"]
    finish = json.loads(events[-3].removeprefix("data: "))
    finish["choices"][0]["index"] = 1
    events[-3] = "data: " + json.dumps(finish, separators=(",", ":")) + "\n\n"
    with pytest.raises(ValueError, match="does not read the replacement as raw_finish"):
        run("replace_finish_value", {"value": "future_finish"}, doc)


@pytest.mark.parametrize(
    "provider,fixture,index", [(openai_chat, CHAT, 14), (openai_responses, RESPONSES, 20)]
)
def test_provider_finish_index(provider, fixture, index):
    events = response(source(fixture))["body"]["sse"]
    assert provider.finish_index(events) == index
    assert provider.finish_index([": comment\n\n", *events[:index]]) is None


@pytest.mark.parametrize("kind", ["response.completed", "response.incomplete", "response.failed"])
def test_responses_finish_types(kind):
    event = f'event: {kind}\ndata: {{"type":"{kind}"}}\n\n'
    assert openai_responses.finish_index([event]) == 0


def test_provider_error_event_helpers():
    assert (
        openai_chat.error_event({"error": {"message": "Try again"}})
        == 'data: {"error":{"message":"Try again"}}\n\n'
    )
    assert (
        openai_responses.error_event({"type": "error", "message": "Try again"})
        == 'event: error\ndata: {"type":"error","message":"Try again"}\n\n'
    )


@pytest.mark.parametrize("kind", [None, 1, "", "error\nextra", "error\rextra"])
def test_responses_error_needs_safe_type(kind):
    with pytest.raises(ValueError, match="single-line type"):
        openai_responses.error_event({"type": kind})


def test_event_object_helper():
    assert event_object(": comment\n\n") is None
    assert event_object("data: [DONE]\n\n", terminator="[DONE]") is None
    assert event_object('data: {"value":1}\n\n') == {"value": 1}
    with pytest.raises(ValueError, match="not a JSON object"):
        event_object("data: []\n\n")
    with pytest.raises(ValueError):
        event_object("data: invalid\n\n")


@pytest.mark.parametrize("separators", [(",", ":"), (", ", ": "), (",", ": ")])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_provider_replace_matches_json_serialization(separators, ascii_only):
    payload = {
        "choices": [
            {"index": 0, "finish_reason": "length", "delta": {"content": "café"}},
            {"index": 1, "finish_reason": "stop"},
            {"index": 2, "finish_reason": None},
        ]
    }
    data = json.dumps(payload, ensure_ascii=ascii_only, separators=separators)
    event = f"id: 1\r\ndata: {data}\r\n\r\n"
    original = Body("sse", None, (": comment\n\n", event, "data: [DONE]\n\n"))
    changed = openai_chat.replace_finish(original, "future_finish")
    assert changed["sse"] == [
        original.parts[0],
        event.replace('"length"', '"future_finish"').replace('"stop"', '"future_finish"'),
        original.parts[2],
    ]


@pytest.mark.parametrize("provider", [openai_chat, openai_responses])
def test_provider_replace_rejects_missing_unchanged_and_wrong_bodies(provider):
    for body in (
        Body("json", None, ()),
        Body("json", {}, ()),
        Body("text", "{}", ()),
        Body("sse", None, (": comment\n\n",)),
    ):
        with pytest.raises(ValueError):
            provider.replace_finish(body, "future_finish")
    fixture = CHAT if provider is openai_chat else RESPONSES
    doc = source(fixture)
    body = build_exchange(doc["exchanges"][-1]).body
    with pytest.raises(ValueError, match="no finish value to change"):
        provider.replace_finish(body, doc["truth"]["raw_finish"])
    fixture = (
        "openai-chat/truncation.reasoning-only"
        if provider is openai_chat
        else "openai-responses/truncation.reasoning-only"
    )
    doc = source(fixture)
    with pytest.raises(ValueError, match="no finish value to change"):
        provider.replace_finish(
            build_exchange(doc["exchanges"][-1]).body, doc["truth"]["raw_finish"]
        )


def test_responses_changes_only_last_final_response():
    doc = source(RESPONSES)
    events = response(doc)["body"]["sse"]
    final = events[-1]
    body = Body("sse", None, (*events, final))
    changed = openai_responses.replace_finish(body, "future_finish")
    assert changed["sse"][:-1] == list(body.parts[:-1])
    assert changed["sse"][-1] == final.replace('"max_output_tokens"}', '"future_finish"}')
    completed = (
        "event: response.completed\ndata: "
        '{"type":"response.completed","response":{"status":"completed"}}\n\n'
    )
    with pytest.raises(ValueError, match="no finish value to change"):
        openai_responses.replace_finish(Body("sse", None, (*events, completed)), "future_finish")
    with pytest.raises(ValueError, match="no finish value to change"):
        openai_responses.replace_finish(Body("json", {"status": "completed"}, ()), "future_finish")


@pytest.mark.parametrize(
    "event",
    [
        'data: { "choices": [{"finish_reason": "length"}]}\n\n',
        'data: {"choices":\ndata: [{"finish_reason":"length"}]}\n\n',
    ],
)
def test_provider_replace_rejects_serialization_it_cannot_preserve(event):
    with pytest.raises(ValueError, match="preserve"):
        openai_chat.replace_finish(Body("sse", None, (event,)), "future_finish")


@pytest.mark.parametrize("provider", [openai_chat, openai_responses])
def test_provider_error_framing_rejects_ndjson(provider):
    with pytest.raises(ValueError, match="error events need an sse body"):
        provider.error_event({"type": "error", "error": {"message": "Try again"}}, kind="ndjson")


def test_inject_rejects_unchanged_exchanges():
    doc = source()
    error = {"error": {"message": "Try again"}}
    events = response(doc)["body"]["sse"]
    response(doc)["body"]["sse"] = [events[0], openai_chat.error_event(error)]
    with pytest.raises(ValueError, match="leave the exchanges unchanged"):
        run("inject_error_event", {"after": 1, "error": error}, doc)


def test_provider_replace_leaves_matching_json_in_comments_unchanged():
    data = '{"choices":[{"finish_reason":"length"}]}'
    event = f": {data}\ndata:{data}\n\n"
    result = openai_chat.replace_finish(Body("sse", None, (event,)), "future_finish")
    changed = data.replace('"length"', '"future_finish"')
    assert result == {"sse": [f": {data}\ndata:{changed}\n\n"]}
