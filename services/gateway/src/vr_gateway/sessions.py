"""Session registry: one active realtime session, in-memory transcripts, opt-in persistence, end summary.

Starting something new (a realtime session, a recorded-practice attempt, upload, submit or transcript re-analysis)
ends the running realtime session first instead of refusing (PRD §7, product decision 2026-09-29).

Shared mode (PROTOCOL §7): the same rule applies per learner (`owner` = the browser's vr_sid). Learners never end
or see each other's sessions, at most `max_sessions` realtime sessions are live at once (CAPACITY_FULL beyond that),
each realtime session ends after `max_session_s`, and nothing is persisted (history off).
"""

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

# WebSocket close codes for a session the gateway ended (PROTOCOL §6.3).
CLOSE_ENDED = 4000  # POST /api/sessions/{id}/end, `session.end`, or an abandoned session
CLOSE_SUPERSEDED = 4001  # ended because the learner started something new
CLOSE_REASONS = {"ended": (CLOSE_ENDED, "session_ended"), "superseded": (CLOSE_SUPERSEDED, "session_superseded"),
                 # shared mode: the realtime session reached max_session_s
                 "time_limit": (CLOSE_ENDED, "session_time_limit")}
# Longest a new request waits for the previous realtime engine to stop (cancel LLM/TTS/ASR, close the socket).
STOP_TIMEOUT_S = 5.0


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
    end_reason: str = "ended"  # ended | superseded | time_limit
    stopped: asyncio.Event = field(default_factory=asyncio.Event)  # set once the engine stopped and the socket closed
    owner: str | None = None  # shared mode: the learner's vr_sid; None in single-learner mode
    time_limit: asyncio.TimerHandle | None = None  # shared mode: ends the realtime session after max_session_s

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
        # Serializes "end the running realtime session, then start the new thing" so two quick requests can never
        # leave two realtime sessions. Only taken by create() and end_realtime_for_new_work(); _finish never takes it.
        self._switch_lock = asyncio.Lock()

    def owns(self, session: Session, owner: str | None) -> bool:
        """Single-learner mode: every session is the learner's. Shared mode: only sessions created by this vr_sid."""
        return not self.services.config.shared or session.owner == owner

    async def create(self, body: dict, settings: dict, owner: str | None = None) -> Session:
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
        if mode == "realtime" and not self.services.health.realtime_available():
            raise ApiError("MODEL_NOT_READY")  # checked first: a refused start leaves the running session alone
        if mode != "realtime":
            return self._register(mode, scenario, difficulty, policy, body, settings, owner)
        async with self._switch_lock:
            await self._end_live_realtime(owner)
            self._check_capacity()
            return self._register(mode, scenario, difficulty, policy, body, settings, owner)

    def _check_capacity(self) -> None:
        """Shared mode: refuse a new realtime session when max_sessions are live (the learner's own previous session
        was ended just before, so it never counts against them)."""
        cfg = self.services.config
        if not cfg.shared:
            return
        self.active_realtime(all_owners=True)  # ends sessions abandoned without a connection past the idle grace
        if len(self.live_realtime()) >= cfg.max_sessions:
            log.warning("session_refused reason=capacity live=%d cap=%d", len(self.live_realtime()), cfg.max_sessions)
            raise ApiError("CAPACITY_FULL")

    def _register(self, mode: str, scenario: dict, difficulty: str, policy: str, body: dict, settings: dict,
                  owner: str | None = None) -> Session:
        silence = settings.get("silence_ms") or scenario.get("difficulty", {}).get(difficulty, {}).get(
            "silence_ms", DEFAULT_SILENCE_MS[difficulty]
        )
        session = Session(
            session_id="s_" + uuid.uuid4().hex[:16],
            mode=mode,
            scenario=scenario,
            difficulty=difficulty,
            history_opt_in=bool(body.get("history_opt_in", settings.get("history_opt_in", False)))
            and self.services.config.history_enabled,
            feedback_policy=policy,
            voice_id=self.services.voice_for(scenario, body.get("voice_id") or settings.get("voice_id")),
            silence_ms=max(700, min(1400, int(silence))),
            goals=[{"goal_id": g["goal_id"], "status": "pending"} for g in scenario.get("goals", [])],
            owner=owner,
        )
        self.sessions[session.session_id] = session
        cfg = self.services.config
        if cfg.shared and mode == "realtime" and cfg.max_session_s > 0:
            session.time_limit = asyncio.get_running_loop().call_later(
                cfg.max_session_s, self._time_limit_reached, session)
        if session.history_opt_in:
            self.services.db.upsert_session(session.meta())
        log.info("session_created id=%s mode=%s opt_in=%s", session.session_id, mode, session.history_opt_in)
        return session

    def get(self, session_id: str, owner: str | None = None) -> Session:
        self.purge()
        session = self.sessions.get(session_id)
        if session is None or not self.owns(session, owner):
            raise ApiError("NOT_FOUND")
        return session

    def _time_limit_reached(self, session: Session) -> None:
        if session.state != "ended":
            log.info("session_time_limit id=%s", session.session_id)
            self.end_in_background(session, reason="time_limit")

    def active_realtime(self, *, all_owners: bool = False) -> Session | None:
        """The first live realtime session (connected, or within the reconnect grace); sessions abandoned without a
        connection past the grace are ended on the way. `all_owners=True` sweeps every session before answering."""
        grace = self.services.config.realtime_idle_grace_s
        found = None
        for s in list(self.sessions.values()):
            if s.mode != "realtime" or s.state == "ended":
                continue
            if s.engine is not None or time.monotonic() - s.last_seen < grace:
                if not all_owners:
                    return s
                found = found or s
                continue
            # Abandoned without a connection: end it in the background.
            self.end_in_background(s)
        return found

    def live_realtime(self, owner: str | None = None, *, all_owners: bool = True) -> list[Session]:
        """Every realtime session that has not ended, connected or not (reconnect grace included).
        `all_owners=False`: only `owner`'s sessions in shared mode (everything in single-learner mode)."""
        return [s for s in self.sessions.values() if s.mode == "realtime" and s.state != "ended"
                and (all_owners or self.owns(s, owner))]

    async def end_realtime_for_new_work(self, owner: str | None = None) -> None:
        """Recorded practice is about to start: end the learner's running realtime session first (same path as
        POST end). In shared mode other learners' sessions are left alone."""
        if not self.live_realtime(owner, all_owners=False):
            return
        async with self._switch_lock:
            await self._end_live_realtime(owner)

    async def _end_live_realtime(self, owner: str | None = None) -> None:
        """Caller holds _switch_lock. Waits until the engines are stopped and the sockets closed; the end summary
        keeps being built in the background (same task POST /end awaits)."""
        ended = self.live_realtime(owner, all_owners=False)
        for s in ended:
            self.end_in_background(s, reason="superseded")
        for s in ended:
            try:
                await asyncio.wait_for(asyncio.shield(s.stopped.wait()), STOP_TIMEOUT_S)
            except TimeoutError:
                log.error("session_stop_timeout id=%s", s.session_id)
                raise ApiError("LOCAL_BUSY") from None

    def end_in_background(self, session: Session, reason: str = "ended") -> None:
        if session.end_task is None:
            # Marked ended synchronously, so the next request already sees no live session.
            session.state = "ended"
            session.ended_at = time.time()
            session.end_reason = reason
            if session.time_limit is not None:
                session.time_limit.cancel()
            session.end_task = asyncio.create_task(self._finish(session))

    async def end(self, session: Session) -> dict:
        self.end_in_background(session)
        return await asyncio.shield(session.end_task)

    async def _finish(self, session: Session) -> dict:
        started = time.perf_counter()
        engine = session.engine
        try:
            if engine is not None:
                await engine.shutdown(reason="session_end" if session.end_reason in ("ended", "time_limit")
                                      else session.end_reason)
                await engine.close_transport(*CLOSE_REASONS[session.end_reason])
        finally:
            session.stopped.set()
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
        log.info("session_ended id=%s reason=%s turns=%d summary_ms=%d", session.session_id, session.end_reason,
                 len(turns), int((time.perf_counter() - started) * 1000))
        return self.summary_payload(session)

    def summary_payload(self, session: Session) -> dict:
        return {"session_id": session.session_id, "state": session.state, "summary": session.summary}

    def purge(self) -> None:
        """Drop ended sessions after the 15-minute summary window (opt-in data lives on in SQLite)."""
        ttl = self.services.config.summary_ttl_s
        now = time.time()
        for sid in [s.session_id for s in self.sessions.values() if s.ended_at and now - s.ended_at > ttl]:
            del self.sessions[sid]
