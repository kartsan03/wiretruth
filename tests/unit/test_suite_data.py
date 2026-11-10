from pathlib import Path

import pytest

from wiretruth.formats import load_fixture, load_scenario
from wiretruth.providers import PROVIDERS

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "path",
    sorted(
        path
        for path in (REPO / "suite/fixtures").glob("*/*.json")
        if "@" not in path.stem and path.parent.name in PROVIDERS
    ),
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
