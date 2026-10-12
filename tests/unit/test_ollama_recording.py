import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from ollama_samples import PROBES

from wiretruth import recorder
from wiretruth.cli import main
from wiretruth.formats import build_exchange, load_fixture
from wiretruth.providers import body_text, ollama, ollama_openai
from wiretruth.recorder import Response, response_body
from wiretruth.replay import _chunks

REPO = Path(__file__).resolve().parents[2]


def test_ndjson_recording_preserves_lines_and_round_trips():
    raw = PROBES["n-text-stream"]["body"].encode()
    response = Response(
        200, {"content-type": "Application/X-NDJSON; charset=utf-8"}, [raw[:37], raw[37:]], "clean"
    )
    body, warnings = response_body(response, False)
    assert body == {"ndjson": raw.decode().splitlines()}
    assert warnings == []
    request = ollama.build_request(
        recorder.find_scenarios(REPO, "truncation.text-stream")[0][0].call_for("ollama"),
        ollama.DEFAULT_MODEL,
    )
    ex = build_exchange(recorder.exchange_document(request, ollama.API_BASE, response, body))
    assert body_text(ex).encode() == raw
    assert b"".join(_chunks(ex.body)) == raw


@pytest.mark.parametrize(
    "raw,end,warns",
    [
        (b"{}\n", "abort", False),
        (b"{}", "clean", True),
        (b"{}\n\n", "clean", True),
        (b"{}\r\n", "clean", True),
        (b"no-json\n", "clean", True),
        (b"", "clean", True),
        (b"\xff\n", "clean", True),
    ],
)
def test_ndjson_abort_or_odd_framing_keeps_chunks(raw, end, warns):
    pieces = [raw[:1], raw[1:]]
    body, warnings = response_body(
        Response(200, {"content-type": "application/x-ndjson"}, pieces, end), False
    )
    assert body == {"chunks_b64": [base64.b64encode(p).decode() for p in pieces]}
    assert bool(warnings) is warns


@pytest.mark.parametrize("provider,probe", [(ollama, "n-text"), (ollama_openai, "o-text")])
@pytest.mark.parametrize("dry_run", [False, True])
def test_no_key_provider_records_and_dry_runs(
    provider, probe, dry_run, tmp_path, monkeypatch, capsys
):
    folder = tmp_path / "suite/scenarios/truncation"
    folder.mkdir(parents=True)
    scenario = json.loads((REPO / "suite/scenarios/truncation/text.json").read_text())
    scenario.pop("call_overrides", None)
    (folder / "text.json").write_text(json.dumps(scenario))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    data = PROBES[probe]
    send = MagicMock(
        return_value=Response(
            data["status"], {"content-type": data["content_type"]}, [data["body"].encode()], "clean"
        )
    )
    monkeypatch.setattr(recorder, "send", send)
    monkeypatch.setattr(recorder, "_source_commit", lambda _: None)
    assert recorder.api_key(provider, {}) == ""
    args = ["record", "--provider", provider.ID, "--scenario", "truncation.text"]
    assert main([*args, "--dry-run"] if dry_run else args) == 0
    output = capsys.readouterr().out
    assert "Authorization" not in output
    if dry_run:
        send.assert_not_called()
        assert "http://127.0.0.1:11434" in output
        assert "qwen3.5:0.8b" in output
    else:
        assert send.call_args.args[-1] == ""
        assert (
            load_fixture(
                tmp_path / "suite/fixtures" / provider.ID / "truncation.text.json"
            ).truth.finish
            == "length"
        )
    request = provider.build_request(
        recorder.find_scenarios(tmp_path, "truncation.text")[0][0].call_for(provider.ID),
        provider.DEFAULT_MODEL,
    )
    assert "Authorization" not in recorder._headers(
        provider, request, recorder._target(provider.API_BASE, request), "", 0
    )


def test_empty_key_does_not_match_everything_or_disable_secret_scan():
    assert recorder._secrets("body", '{"done":true}', {"done": True}, "") == []
    found = recorder._secrets("body", "sk-" + "a" * 20, {}, "")
    assert found and "not shown" not in found[0]
    assert "the API key itself" not in " ".join(found)
