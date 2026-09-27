"""The real scenario files load and validate against contracts/scenario.schema.json."""

from __future__ import annotations

import json

import pytest

from vr_gateway.config import REPO_ROOT
from vr_gateway.scenarios import ScenarioStore

SCEN = REPO_ROOT / "content" / "scenarios"
SCHEMA = REPO_ROOT / "contracts" / "scenario.schema.json"


@pytest.mark.skipif(not SCEN.exists(), reason="content/scenarios not present")
def test_repository_scenarios_load():
    store = ScenarioStore.load(SCEN, SCHEMA)
    files = sorted(p.stem for p in SCEN.glob("*.json"))
    assert sorted(store.scenarios) == files  # none rejected by the schema
    for sc in store.scenarios.values():
        assert store.text(sc["opening_line"]["text_id"])["scenario_id"] == sc["scenario_id"]


def test_invalid_scenario_is_skipped(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({"scenario_id": "bad", "goals": []}))
    (tmp_path / "broken.json").write_text("{")
    assert ScenarioStore.load(tmp_path, None).scenarios == {}
