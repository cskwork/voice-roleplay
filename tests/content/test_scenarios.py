"""Checks for content/scenarios/*.json against contracts/scenario.schema.json and content rules.

Run: uv run --with jsonschema --with pytest python -m pytest tests/content
"""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "scenario.schema.json"
SCENARIO_DIR = ROOT / "content" / "scenarios"

EXPECTED_IDS = {"cafe_order", "hotel_checkin", "directions", "job_interview"}
EXPECTED_VOICES = {
    "cafe_order": "dev_voice_a",
    "directions": "dev_voice_a",
    "hotel_checkin": "dev_voice_b",
    "job_interview": "dev_voice_b",
}
DEFAULT_SILENCE_MS = {"easy": 1200, "normal": 900, "hard": 700}

HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힣]")
DOLLAR_TOKEN = re.compile(r"\$\S*")
PRICE = re.compile(r"^\$(\d+\.\d{2})[,;.]?$")

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
PATHS = sorted(SCENARIO_DIR.glob("*.json"))


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def text_fields(node, path=""):
    """Yield (path, key, value) for every string in the document; list items inherit their parent key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                yield f"{path}.{key}", key, value
            else:
                for p, k, v in text_fields(value, f"{path}.{key}"):
                    yield p, (key if k is None else k), v
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                yield f"{path}[{i}]", None, item
            else:
                yield from text_fields(item, f"{path}[{i}]")


def is_korean_key(key):
    return key == "ko" or key.endswith("_ko")


def is_id_key(key):
    return key in {"scenario_id", "version", "default_voice_id"} or key.endswith("_id")


def speakable(scenario):
    """All lines that are sent to TTS as-is (opening, model expressions, reading, shadowing)."""
    ex = scenario["exercises"]
    return [scenario["opening_line"], *scenario["model_expressions"], *ex["reading"], *ex["shadowing"]]


def all_text_ids(scenario):
    return [item["text_id"] for item in speakable(scenario)]


@pytest.fixture(params=PATHS, ids=[p.stem for p in PATHS])
def scenario(request):
    return load(request.param)


def test_schema_is_valid_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


def test_expected_scenarios_present():
    assert {p.stem for p in PATHS} == EXPECTED_IDS


@pytest.mark.parametrize("path", PATHS, ids=[p.stem for p in PATHS])
def test_validates_against_schema(path):
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(load(path)), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_schema_rejects_unknown_and_missing_fields(scenario):
    validator = Draft202012Validator(SCHEMA)
    extra = {**scenario, "unexpected": 1}
    assert not validator.is_valid(extra)
    missing = {k: v for k, v in scenario.items() if k != "facts"}
    assert not validator.is_valid(missing)
    two_goals = {**scenario, "goals": scenario["goals"][:2]}
    assert not validator.is_valid(two_goals)


@pytest.mark.parametrize("path", PATHS, ids=[p.stem for p in PATHS])
def test_file_name_matches_id_and_voice(path):
    data = load(path)
    assert data["scenario_id"] == path.stem
    assert data["default_voice_id"] == EXPECTED_VOICES[path.stem]


def test_text_ids_globally_unique_and_prefixed():
    seen = {}
    for path in PATHS:
        data = load(path)
        ids = all_text_ids(data)
        ids += [h["hint_id"] for h in data["hints"]]
        ids += [f["exercise_id"] for f in data["exercises"]["free_answer"]]
        for text_id in ids:
            assert text_id.startswith(data["scenario_id"] + "_"), text_id
            assert text_id not in seen, f"{text_id} in {path.name} and {seen.get(text_id)}"
            seen[text_id] = path.name


def test_goal_references_and_coverage(scenario):
    goal_ids = [g["goal_id"] for g in scenario["goals"]]
    assert len(set(goal_ids)) == 3
    for item in scenario["hints"] + scenario["model_expressions"]:
        assert item["goal_id"] in goal_ids, item
    for goal_id in goal_ids:
        assert any(h["goal_id"] == goal_id for h in scenario["hints"]), f"no hint for {goal_id}"
        assert any(m["goal_id"] == goal_id for m in scenario["model_expressions"]), f"no expression for {goal_id}"


def test_content_counts(scenario):
    ex = scenario["exercises"]
    assert 4 <= len(scenario["hints"]) <= 6
    assert 6 <= len(scenario["model_expressions"]) <= 8
    assert len(ex["reading"]) == 4
    assert len(ex["shadowing"]) == 3
    assert len(ex["free_answer"]) == 2


def test_difficulty_silence_defaults(scenario):
    for level, ms in DEFAULT_SILENCE_MS.items():
        assert scenario["difficulty"][level]["silence_ms"] == ms


def test_language_of_fields(scenario):
    for path, key, value in text_fields(scenario):
        if key and is_korean_key(key):
            assert HANGUL.search(value), f"Korean field without Hangul: {path}"
        elif not (key and is_id_key(key)):
            assert not HANGUL.search(value), f"English field contains Hangul: {path}"


def test_opening_line_has_at_most_one_question(scenario):
    en = scenario["opening_line"]["en"]
    assert en.count("?") <= 1
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", en.strip()) if s]
    assert 1 <= len(sentences) <= 2, sentences


def test_speakable_lines_fit_tts_limit(scenario):
    for item in speakable(scenario):
        assert 0 < len(item["en"]) <= 400, item["text_id"]


def test_prices_in_facts_parse(scenario):
    for key, value in scenario["facts"].items():
        for token in DOLLAR_TOKEN.findall(value):
            match = PRICE.match(token)
            assert match, f"facts.{key}: malformed price {token!r}"
            assert Decimal(match.group(1)) > 0


def test_cafe_prices_are_concrete():
    facts = load(SCENARIO_DIR / "cafe_order.json")["facts"]
    prices = [t for v in facts.values() for t in DOLLAR_TOKEN.findall(v)]
    assert len(prices) >= 10
