"""Session registry: one active realtime session, in-memory transcripts, opt-in persistence, end summary."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import ApiError

if TYPE_CHECKING:
    from .app import Services

log = logging.getLogger("vr_gateway.sessions")

MODES = ("realtime", "turn_based")
DIFFICULTIES = ("easy", "normal", "hard")
DEFAULT_SILENCE_MS = {"easy": 1200, "normal": 900, "hard": 700}


@dataclass
class Session:
    session_id: str
    mode: str
    scenario: dict
    difficulty: str
    history_opt_in: bool
    feedback_policy: str
    voice_id: str
    silence_ms: int
    created_at: float = field(default_factory=time.time)
    state: str = "created"  # created | active | ended
    epoch: int = 0
    turn_counter: int = 0
    turns: list[dict] = field(default_factory=list)  # user + assistant records in order
    history: list[dict] = field(default_factory=list)  # [{role, text}] for the LLM
    # PRD §11 rolling summary of history[:summarized_upto] (turns older than the prompt window) and details
    # the learner stated there. The facts are separate state: a new summary never overwrites them.
    context_summary: str = ""
    summarized_upto: int = 0
    learner_facts: dict[str, dict] = field(default_factory=dict)
    goals: list[dict] = field(default_factory=list)
    summary: dict | None = None
    ended_at: float | None = None
    last_seen: float = field(default_factory=time.monotonic)
    engine: Any = None
    end_task: asyncio.Task | None = None

    def next_turn_index(self) -> int:
        self.turn_counter += 1
        return self.turn_counter

    def meta(self) -> dict:
        return {
            "session_id": self.session_id,
            "mode": self.mode,
            "scenario_id": self.scenario["scenario_id"],
            "scenario_version": self.scenario.get("version"),
            "difficulty": self.difficulty,
            "history_opt_in": self.history_opt_in,
            "feedback_policy": self.feedback_policy,
            "voice_id": self.voice_id,
            "silence_ms": self.silence_ms,
            "state": self.state,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
        }

    def user_turns(self) -> list[dict]:
        return [
            {"turn_id": t["turn_id"], "text": t["text"], "transcript_revision": t.get("transcript_revision", 1)}
            for t in self.turns
            if t["role"] == "user" and (t.get("text") or "").strip()
        ]

    def record_turn(self, services: Services, turn: dict) -> None:
        """Append or update a turn record; persist only with opt-in."""
        for i, existing in enumerate(self.turns):
            if existing["turn_id"] == turn["turn_id"]:
                self.turns[i] = turn
                break
        else:
            self.turns.append(turn)
        if self.history_opt_in:
            services.db.upsert_turn({**turn, "session_id": self.session_id})


def merge_goals(old: list[dict], new: list[dict]) -> list[dict]:
    """Goals only move pending -> done within a session. `new` may cover only some goals (the pending ones)."""
    done = {g["goal_id"]: g for g in new if g.get("status") == "done"}
    return [g if g.get("status") == "done" else done.get(g["goal_id"], g) for g in old]


class SessionManager:
    def __init__(self, services: Services):
        self.services = services
        self.sessions: dict[str, Session] = {}

    def create(self, body: dict, settings: dict) -> Session:
        mode = body.get("mode")
        if mode not in MODES:
            raise ApiError("UNSUPPORTED_MODE")
        scenario = self.services.scenarios.get(body.get("scenario_id", ""))
        if scenario is None:
            raise ApiError("NOT_FOUND")
        difficulty = body.get("difficulty") or settings.get("difficulty", "normal")
        if difficulty not in DIFFICULTIES:
            raise ApiError("INVALID_REQUEST")
        policy = body.get("feedback_policy", "session_end")
        if policy not in ("session_end", "per_turn"):
            raise ApiError("INVALID_REQUEST")
        if mode == "realtime":
            if self.active_realtime() is not None:
                raise ApiError("LOCAL_BUSY")
            if not self.services.health.realtime_available():
                raise ApiError("MODEL_NOT_READY")
        silence = settings.get("silence_ms") or scenario.get("difficulty", {}).get(difficulty, {}).get(
            "silence_ms", DEFAULT_SILENCE_MS[difficulty]
        )
        session = Session(
            session_id="s_" + uuid.uuid4().hex[:16],
            mode=mode,
            scenario=scenario,
            difficulty=difficulty,
            history_opt_in=bool(body.get("history_opt_in", settings.get("history_opt_in", False))),
            feedback_policy=policy,
            voice_id=body.get("voice_id") or settings.get("voice_id") or scenario.get("default_voice_id", ""),
            silence_ms=max(700, min(1400, int(silence))),
            goals=[{"goal_id": g["goal_id"], "status": "pending"} for g in scenario.get("goals", [])],
        )
        self.sessions[session.session_id] = session
        if session.history_opt_in:
            self.services.db.upsert_session(session.meta())
        log.info("session_created id=%s mode=%s opt_in=%s", session.session_id, mode, session.history_opt_in)
        return session

    def get(self, session_id: str) -> Session:
        self.purge()
        session = self.sessions.get(session_id)
        if session is None:
            raise ApiError("NOT_FOUND")
        return session

    def active_realtime(self) -> Session | None:
        grace = self.services.config.realtime_idle_grace_s
        for s in self.sessions.values():
            if s.mode != "realtime" or s.state == "ended":
                continue
            if s.engine is not None or time.monotonic() - s.last_seen < grace:
                return s
            # Abandoned without a connection: end it in the background.
            self.end_in_background(s)
        return None

    def end_in_background(self, session: Session) -> None:
        if session.end_task is None:
            session.end_task = asyncio.create_task(self._finish(session))

    async def end(self, session: Session) -> dict:
        self.end_in_background(session)
        return await asyncio.shield(session.end_task)

    async def _finish(self, session: Session) -> dict:
        started = time.perf_counter()
        session.state = "ended"
        session.ended_at = time.time()
        if session.engine is not None:
            await session.engine.shutdown(reason="session_end")
        svc = self.services
        turns = session.user_turns()
        await svc.health.snapshot()
        if turns and svc.health.llm_ready():
            try:
                result = await svc.brain.session_summary(
                    svc.llm, turns=turns, scenario=session.scenario, model_revision=svc.config.llm_model_revision
                )
            except Exception as exc:  # summary is best-effort; the transcript stays available
                log.error("summary_failed session=%s error=%s", session.session_id, type(exc).__name__)
                result = {"items": [], "goals": session.goals, "status": "unavailable", "reason": "error"}
        else:
            result = {"items": [], "goals": session.goals,
                      "status": "held" if not turns else "unavailable",
                      "reason": "no_turns" if not turns else "llm_not_ready"}
        session.goals = merge_goals(session.goals, result.get("goals") or session.goals)
        session.summary = {
            "items": result.get("items", []),
            "goals": session.goals,
            "status": result.get("status", "ok"),
            "reason": result.get("reason"),
            "turns": [
                {k: t.get(k) for k in ("turn_id", "turn_index", "role", "text", "transcript_revision",
                                        "spoken_segments", "playback_status", "metrics")}
                for t in session.turns
            ],
            "pronunciation_score": None,
            "pronunciation_status": "assessment_unavailable",
        }
        if session.history_opt_in:
            with svc.db.tx() as conn:
                svc.db.upsert_session({**session.meta(), "summary": session.summary}, conn=conn)
                for item in session.summary["items"]:
                    if item.get("suggestion"):
                        svc.db.add_review_item(
                            {"item_id": "r_" + uuid.uuid4().hex[:16], "text_en": item["suggestion"],
                             "text_ko": item.get("explanation_ko"), "source_type": "session",
                             "source_id": session.session_id},
                            conn=conn,
                        )
        log.info("session_ended id=%s turns=%d summary_ms=%d", session.session_id, len(turns),
                 int((time.perf_counter() - started) * 1000))
        return self.summary_payload(session)

    def summary_payload(self, session: Session) -> dict:
        return {"session_id": session.session_id, "state": session.state, "summary": session.summary}

    def purge(self) -> None:
        """Drop ended sessions after the 15-minute summary window (opt-in data lives on in SQLite)."""
        ttl = self.services.config.summary_ttl_s
        now = time.time()
        for sid in [s.session_id for s in self.sessions.values() if s.ended_at and now - s.ended_at > ttl]:
            del self.sessions[sid]
