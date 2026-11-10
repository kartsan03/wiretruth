from pathlib import Path

import pytest

from wiretruth.formats import load_fixture, load_scenario
from wiretruth.providers import PROVIDERS

REPO = Path(__file__).resolve().parents[2]
# Recorded with the prompts of the first openai-chat run, which the scenarios no longer send.
# Strict, so the test fails once these fixtures are recorded again and the set must go.
EARLIER_PROMPTS = {
    "openai-chat/truncation.text",
    "openai-chat/truncation.text-stream",
    "openai-chat/truncation.structured-output",
}
EARLIER = pytest.mark.xfail(strict=True, reason="recorded with an earlier prompt")


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(path, marks=EARLIER)
        if f"{path.parent.name}/{path.stem}" in EARLIER_PROMPTS
        else path
        for path in sorted((REPO / "suite/fixtures").glob("*/*.json"))
        if "@" not in path.stem and path.parent.name in PROVIDERS
    ],
    ids=lambda path: f"{path.parent.name}/{path.stem}",
)
def test_scenario_request_matches_fixture_provenance(path):
    fixture = load_fixture(path)
    family, name = fixture.scenario.split(".", 1)
    scenario = load_scenario(REPO / "suite/scenarios" / family / f"{name}.json")
    request = PROVIDERS[fixture.provider].build_request(
        scenario.call_for(fixture.provider), fixture.provenance.requested_model
    )
    assert request.body == fixture.provenance.request
