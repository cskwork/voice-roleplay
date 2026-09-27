"""Load versioned scenario files from content/scenarios/*.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("vr_gateway.scenarios")


class ScenarioStore:
    def __init__(self, scenarios: dict[str, dict]):
        self.scenarios = scenarios
        self._texts: dict[str, dict] = {}
        for sc in scenarios.values():
            for entry in text_entries(sc):
                self._texts[entry["text_id"]] = {**entry, "scenario_id": sc["scenario_id"]}

    @classmethod
    def load(cls, directory: Path, schema_path: Path | None = None) -> ScenarioStore:
        validator = None
        if schema_path and schema_path.exists():
            import jsonschema

            schema = json.loads(schema_path.read_text())
            validator = jsonschema.Draft202012Validator(schema)
        scenarios: dict[str, dict] = {}
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            try:
                data = json.loads(path.read_text())
                if validator:
                    validator.validate(data)
                if len(data.get("goals", [])) != 3:
                    raise ValueError("scenario must have exactly 3 goals")
            except Exception as exc:  # invalid content is skipped, not fatal
                log.error("scenario_invalid file=%s error=%s", path.name, type(exc).__name__)
                continue
            scenarios[data["scenario_id"]] = data
        log.info("scenarios_loaded count=%d", len(scenarios))
        return cls(scenarios)

    def get(self, scenario_id: str) -> dict | None:
        return self.scenarios.get(scenario_id)

    def text(self, text_id: str) -> dict | None:
        """{text_id, en, ko, goal_id?, scenario_id} for any reviewed text in any scenario."""
        return self._texts.get(text_id)

    def free_answer(self, scenario_id: str, exercise_id: str) -> dict | None:
        sc = self.get(scenario_id) or {}
        for ex in sc.get("exercises", {}).get("free_answer", []):
            if ex.get("exercise_id") == exercise_id:
                return ex
        return None

    def summaries(self) -> list[dict]:
        return list(self.scenarios.values())


def text_entries(scenario: dict) -> list[dict]:
    entries = [scenario["opening_line"]] if scenario.get("opening_line") else []
    entries += scenario.get("model_expressions", [])
    ex = scenario.get("exercises", {})
    entries += ex.get("reading", []) + ex.get("shadowing", [])
    return [e for e in entries if e.get("text_id") and e.get("en")]


def asr_context(scenario: dict | None) -> str | None:
    """Short scenario hint for ASR (never a target sentence)."""
    if not scenario:
        return None
    return f"{scenario.get('title_en', '')}. Conversation with a {scenario.get('ai_role', '')}."[:300]
