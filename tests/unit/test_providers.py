import base64
import json
from dataclasses import replace

import pytest

from wiretruth.formats import Body, Exchange
from wiretruth.providers import PROVIDERS


@pytest.fixture(params=["openai-chat", "openai-responses"])
def stream(request):
    provider = PROVIDERS[request.param]

    def event(text):
        if request.param == "openai-chat":
            data = {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        else:
            data = {"type": "response.output_text.delta", "delta": text}
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    exchange = Exchange(
        "POST",
        "/",
        200,
        {"content-type": "text/event-stream"},
        Body("chunks_b64", None, ()),
        "abort",
    )
    return provider, event, exchange


@pytest.mark.parametrize("char", ["é", "東", "🌸"])
@pytest.mark.parametrize("end", ["abort", "clean"])
def test_stream_ending_inside_utf8_keeps_only_complete_events(stream, char, end):
    provider, event, exchange = stream
    complete = event("Café ")
    following = event(char)
    cut = following.index(char.encode()) + len(char.encode()) - 1
    parts = tuple(base64.b64encode(part).decode() for part in (complete, following[:cut]))
    exchange = replace(exchange, body=Body("chunks_b64", None, parts), end=end)
    if end == "clean":
        with pytest.raises(UnicodeDecodeError):
            provider.extract_truth([exchange], None)
    else:
        expected = replace(exchange, body=Body("chunks_b64", None, parts[:1]))
        truth = provider.extract_truth([exchange], None)
        assert truth == provider.extract_truth([expected], None)
        assert truth["text"] == "Café "
        assert truth["finish"] == "none"
        assert truth["stream_completed"] is False


@pytest.mark.parametrize("bad", [b"\xff", b"\xc3x", b"\xe0\x80\x80", b"\xed\xa0\x80"])
def test_aborted_stream_still_rejects_invalid_utf8(stream, bad):
    provider, event, exchange = stream
    raw = event("Café ") + b"data: " + bad + b"\n\n\xf0\x9f"
    parts = (base64.b64encode(raw).decode(),)
    exchange = replace(exchange, body=Body("chunks_b64", None, parts))
    with pytest.raises(UnicodeDecodeError):
        provider.extract_truth([exchange], None)
