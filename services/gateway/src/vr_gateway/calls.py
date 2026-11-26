"""Character video calls (PROTOCOL §13): chat topics from content/calls/, plus every scenario as a role call.

A call is handed to the realtime engine as a scenario-shaped dict with a `call` entry, so sessions, captions, the TTS
cache and the opening line work unchanged; the engine only adds the turn plan.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .scenarios import ScenarioStore

log = logging.getLogger("vr_gateway.calls")


def day_part(hour: int | None = None) -> str:
    hour = time.localtime().tm_hour if hour is None else hour
    return "morning" if 5 <= hour < 11 else "day" if 11 <= hour < 17 else "evening"


def _object_particle(word: str) -> str:
    code = ord(word[-1]) - 0xAC00 if word else -1
    return "을" if 0 <= code < 11172 and code % 28 else "를"


class CallStore:
    def __init__(self, data: dict | None, scenarios: ScenarioStore):
        self.data = data
        self.scenarios = scenarios
        self.topics = {t["call_id"]: t for t in data["topics"]} if data else {}
        if data:
            for line in data["openings"].values():
                scenarios.add_text(line, scenario_id="call")

    @classmethod
    def load(cls, path: Path, schema_path: Path, scenarios: ScenarioStore) -> CallStore:
        data = None
        try:
            data = json.loads(path.read_text())
            if schema_path.exists():
                import jsonschema

                jsonschema.Draft202012Validator(json.loads(schema_path.read_text())).validate(data)
        except Exception as exc:  # no usable call content: the feature is off, everything else works
            log.error("calls_invalid file=%s error=%s", path.name, type(exc).__name__)
            data = None
        log.info("calls_loaded topics=%d", len(data["topics"]) if data else 0)
        return cls(data, scenarios)

    @property
    def available(self) -> bool:
        return bool(self.data)

    def summaries(self) -> list[dict]:
        if not self.data:
            return []
        out = [{"call_id": t["call_id"], "kind": "chat", "title_ko": t["title_ko"], "title_en": t["title_en"],
                "teaser_ko": t["teaser_ko"]} for t in self.data["topics"]]
        for sc in self.scenarios.summaries():
            role = sc["ai_role_ko"]
            out.append({"call_id": sc["scenario_id"], "kind": "role", "title_ko": sc["title_ko"], "title_en": sc["title_en"],
                        "teaser_ko": f"{self.data['name_ko']}가 {role}{_object_particle(role)} 맡아요"})
        return out

    def get(self, call_id: str, difficulty: str, hour: int | None = None) -> dict | None:
        """Scenario-shaped dict for one call, or None when there is no such call."""
        d = self.data
        if not d:
            return None
        common = {"name_en": d["name_en"], "name_ko": d["name_ko"], "persona_en": d["persona_en"],
                  "voice_pitch": d["voice_pitch"], "turns": d["turns"][difficulty]}
        topic = self.topics.get(call_id)
        if topic is not None:
            return {
                "scenario_id": call_id, "version": d["version"], "title_ko": topic["title_ko"], "title_en": topic["title_en"],
                "ai_role": d["name_en"], "ai_role_ko": d["name_ko"], "user_role_ko": "나", "setting_ko": topic["teaser_ko"],
                "default_voice_id": d["default_voice_id"], "facts": {}, "goals": [], "allowed_flow": [],
                "opening_line": d["openings"][day_part(hour)],
                "call": {**common, "kind": "chat", "first_question_en": topic["first_question_en"]},
            }
        sc = self.scenarios.get(call_id)
        if sc is None:
            return None
        # Goals stay out: a call is never graded, and the engine skips goal checks for an empty list.
        return {**{k: sc[k] for k in ("scenario_id", "version", "title_ko", "title_en", "ai_role_ko", "user_role_ko",
                                      "setting_ko", "facts", "allowed_flow", "opening_line")},
                "ai_role": d["name_en"], "role": sc["ai_role"], "default_voice_id": d["default_voice_id"], "goals": [],
                "call": {**common, "kind": "role"}}
