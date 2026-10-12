import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/record.yml"


def start_script():
    text = WORKFLOW.read_text()
    step = text.split("      - name: Start Ollama\n", 1)[1].split("\n      # A scenario", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


STUBS = r"""
log() { printf '%s\n' "$*" >> "$RUNNER_TEMP/commands"; }
command() { if [ "$*" = '-v zstd' ]; then return 1; fi; builtin command "$@"; }
sudo() { log "$*"; }
curl() {
  log "curl $*"
  if [[ "$*" = *api/version* ]]; then
    probes=$((probes + 1))
    [ "$probes" -ge "$READY_AFTER" ]
  fi
}
sha256sum() { read -r digest archive; log "verify $digest $archive"; return "$BAD_DIGEST"; }
tar() { log "extract $*"; }
ollama() {
  if [ "$1" = serve ]; then echo 'server started'; return; fi
  log "ollama argc=$# $*"
}
sleep() { :; }
probes=0
"""


@pytest.mark.parametrize(
    "model,ready_after,bad_digest,success",
    [
        ("", 2, 0, True),
        ("custom model; $(touch injected)", 1, 0, True),
        ("", 61, 0, False),
        ("", 1, 1, False),
    ],
)
def test_ollama_ci_install_and_readiness_offline(tmp_path, model, ready_after, bad_digest, success):
    text = WORKFLOW.read_text()
    version = re.search(r"^  OLLAMA_VERSION: (.+)$", text, re.M)[1]
    digest = re.search(r"^  OLLAMA_SHA256: (.+)$", text, re.M)[1]
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail"],
        input=STUBS + start_script(),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "MODEL": model,
            "OLLAMA_VERSION": version,
            "OLLAMA_SHA256": digest,
            "READY_AFTER": str(ready_after),
            "BAD_DIGEST": str(bad_digest),
        },
        timeout=10,
        check=False,
    )
    assert (result.returncode == 0) is success
    commands = (tmp_path / "commands").read_text().splitlines()
    assert commands[:2] == ["apt-get update", "apt-get install -y zstd"]
    assert f"/releases/download/{version}/ollama-linux-amd64.tar.zst" in commands[2]
    assert commands[3] == f"verify {digest} {tmp_path}/ollama-linux-amd64.tar.zst"
    assert not (tmp_path / "injected").exists()
    if bad_digest:
        assert not any(line.startswith("extract") for line in commands)
    else:
        assert commands[4].startswith("extract --zstd -xf")
        polls = [line for line in commands if "api/version" in line]
        assert len(polls) == min(ready_after, 60)
    if success:
        assert commands[-1] == f"ollama argc=2 pull {model or 'qwen3.5:0.8b'}"
    else:
        assert not any("pull " in line for line in commands)
        if not bad_digest:
            assert "server started" in result.stdout


def test_record_workflow_credentials_stay_in_record_step():
    text = WORKFLOW.read_text()
    record, push = text.split("\n  push:\n", 1)
    assert "timeout-minutes: ${{ startsWith(inputs.provider, 'ollama') && 40 || 20 }}" in record
    assert "secrets." not in push and "OPENAI_API_KEY" not in push
    assert record.count("secrets.") == 1
    assert "(inputs.provider == 'openai-chat' || inputs.provider == 'openai-responses')" in record
    assert "${{ inputs." not in start_script()
    assert 'tail -n 100 "$RUNNER_TEMP/ollama.log"' in record
