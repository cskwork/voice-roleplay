"""The guided lesson can use one short, authored expression for each scenario goal."""

from vr_gateway.config import REPO_ROOT
from vr_gateway.scenarios import ScenarioStore


EXPECTED = {
    "cafe_order", "directions", "hotel_checkin", "job_interview",
    "friend_intro", "snack_choice", "weekend_plan", "lost_and_found",
}


def test_eight_scenarios_have_three_short_playable_lesson_targets():
    store = ScenarioStore.load(REPO_ROOT / "content/scenarios", REPO_ROOT / "contracts/scenario.schema.json")
    assert set(store.scenarios) == EXPECTED
    for scenario in store.scenarios.values():
        selected = []
        for goal in scenario["goals"]:
            # The web lesson picks the first expression for a goal, in goal order.
            target = next(e for e in scenario["model_expressions"] if e["goal_id"] == goal["goal_id"])
            assert len(target["en"].split()) <= 12, (scenario["scenario_id"], target["text_id"])
            assert target["ko"].strip()
            assert store.text(target["text_id"])["en"] == target["en"]
            assert any(h["goal_id"] == goal["goal_id"] for h in scenario["hints"])
            selected.append(target["text_id"])
        assert len(selected) == len(set(selected)) == 3


def test_authored_lesson_targets_work_with_existing_shadowing_api(gw):
    store = ScenarioStore.load(REPO_ROOT / "content/scenarios", REPO_ROOT / "contracts/scenario.schema.json")
    gw.svc.scenarios = store
    for scenario in store.scenarios.values():
        for goal in scenario["goals"]:
            target = next(e for e in scenario["model_expressions"] if e["goal_id"] == goal["goal_id"])
            response = gw.call("POST", "/api/attempts", json={
                "exercise_type": "shadowing", "scenario_id": scenario["scenario_id"],
                "text_id": target["text_id"], "history_opt_in": False,
            })
            assert response.status_code == 201, response.text
            attempt = gw.svc.jobs.attempts[response.json()["attempt_id"]]
            assert attempt.target_en == target["en"]
            assert attempt.history_opt_in is False
