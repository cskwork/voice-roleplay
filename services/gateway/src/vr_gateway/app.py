"""FastAPI app: HTTP API (PRD §13.1, PROTOCOL §6.2), realtime WebSocket, static web build."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import review as reviewmod
from .audio import parse_wav
from .auth import LocalAuth, LocalAuthMiddleware, cookie_sid
from .brain import Brain
from .config import Config
from .db import Database
from .errors import ApiError, error_response
from .health import HealthMonitor
from .jobs import JobManager
from .pronunciation import GuideIndex
from .realtime import RealtimeEngine
from .scenarios import ScenarioStore
from .sessions import CLOSE_REASONS, SessionManager
from .tts_cache import VOICE_RE, TtsCache

log = logging.getLogger("vr_gateway")

ENGLISH_TEXT = re.compile(r"^[\x20-\x7E‘’“”–—…\n]+$")
DEFAULT_SETTINGS = {
    "difficulty": "normal",
    "silence_ms": None,
    "history_opt_in": False,
    "voice_id": None,
    "slow": False,
    "auto_barge_in": True,
    "feedback_policy": "session_end",
    "input_device_id": None,
    "output_device_id": None,
    "profile": "default",
}
CSP = (
    "default-src 'self'; script-src 'self' blob:; worker-src 'self' blob:; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self' {ws}; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
MISSING_BUILD = """<!doctype html><html lang="ko"><meta charset="utf-8"><title>voice-roleplay</title>
<body><h1>웹 빌드가 없습니다</h1><p><code>apps/web</code>를 빌드한 뒤 다시 열어 주세요.
(<code>apps/web/dist</code>가 필요합니다.)</p></body></html>"""


@dataclass
class Services:
    config: Config
    db: Database
    scenarios: ScenarioStore
    brain: Brain
    asr: Any
    tts: Any
    llm: Any
    vad_model: Any
    # Background LLM work during a realtime session (goals, hints, per-turn feedback, rolling summary) is
    # pinned to llama-server slot 1 so it never evicts the roleplay prefix cached in slot 0 (PROTOCOL §5).
    llm_bg: Any = None
    # Optional pronunciation worker client (PROTOCOL §12); None when not configured.
    pron: Any = None
    pron_guide: GuideIndex = None
    auth: LocalAuth = None
    health: HealthMonitor = None
    sessions: SessionManager = None
    jobs: JobManager = None
    tts_cache: TtsCache = None
    background: set = field(default_factory=set)

    def settings(self) -> dict:
        return {**DEFAULT_SETTINGS, **self.db.get_settings()}

    def voice_for(self, scenario: dict | None, wanted: str | None) -> str:
        """`wanted` (request or saved setting) if the TTS worker offers it, else the scenario default.

        A saved setting can name a voice that has since been removed (e.g. the former dev voices); that falls back
        instead of failing every synthesis. Unchecked while the worker's voice list is unknown."""
        default = (scenario or {}).get("default_voice_id", "")
        known = self.health.tts_voice_ids() if self.health else None
        if wanted and (known is None or wanted in known):
            return wanted
        if wanted:
            log.warning("voice_unavailable voice=%s fallback=%s", wanted, default)
        return default


# ---------------------------------------------------------------- request models


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsIn(Strict):
    difficulty: Literal["easy", "normal", "hard"] | None = None
    silence_ms: int | None = Field(None, ge=700, le=1400)
    history_opt_in: bool | None = None
    voice_id: str | None = Field(None, pattern=VOICE_RE.pattern)
    slow: bool | None = None
    auto_barge_in: bool | None = None
    feedback_policy: Literal["session_end", "per_turn"] | None = None
    input_device_id: str | None = Field(None, max_length=256)
    output_device_id: str | None = Field(None, max_length=256)
    profile: str | None = Field(None, max_length=64)


class SessionIn(Strict):
    mode: str
    scenario_id: str = Field(max_length=64)
    difficulty: Literal["easy", "normal", "hard"] | None = None
    history_opt_in: bool | None = None
    feedback_policy: Literal["session_end", "per_turn"] = "session_end"
    voice_id: str | None = Field(None, pattern=VOICE_RE.pattern)


class AttemptIn(Strict):
    exercise_type: str
    scenario_id: str | None = Field(None, max_length=64)  # required except for drill
    target_text: str | None = Field(None, min_length=1, max_length=400)  # drill only
    text_id: str | None = Field(None, max_length=128)
    exercise_id: str | None = Field(None, max_length=128)
    session_id: str | None = Field(None, max_length=64)
    history_opt_in: bool | None = None


class TranscriptIn(Strict):
    text: str = Field(min_length=1, max_length=2000)


class TtsIn(Strict):
    voice_id: str = Field(pattern=VOICE_RE.pattern)
    text: str = Field(min_length=1, max_length=400)
    speed: float = Field(1.0, ge=0.5, le=1.5)


class GradeIn(Strict):
    result: Literal["again", "good"]


class ReviewIn(Strict):
    text_en: str = Field(min_length=1, max_length=400)
    text_ko: str | None = Field(None, max_length=400)
    source_type: Literal["model_expression", "session", "attempt"] = "model_expression"
    source_id: str | None = Field(None, max_length=128)


class ExportIn(Strict):
    format: Literal["json", "markdown"] = "json"


async def read_body(request: Request, limit: int, code: str) -> bytes:
    """Read the body into memory with a hard cap; never spools to disk."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise ApiError(code)
    return bytes(buf)


async def read_model(request: Request, model: type[BaseModel], svc: Services, allow_empty: bool = False):
    raw = await read_body(request, svc.config.max_json_bytes, "BODY_TOO_LARGE")
    if not raw.strip() and allow_empty:
        raw = b"{}"
    try:
        return model.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        raise ApiError("INVALID_REQUEST") from None


def wav_response(data: bytes) -> Response:
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})


class WsTransport:
    def __init__(self, ws: WebSocket):
        self.ws = ws

    async def send_text(self, data: str) -> None:
        await self.ws.send_text(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self, code: int, reason: str) -> None:
        await self.ws.close(code=code, reason=reason)


# ---------------------------------------------------------------- factory


def build_services(config: Config, *, asr=None, tts=None, llm=None, llm_bg=None, brain=None, vad_model=None,
                   pron=None) -> Services:
    if not config.worker_token:
        config.worker_token = secrets.token_urlsafe(32)
    if asr is None or tts is None:
        from .workers import AsrClient, TtsClient

        asr = asr or AsrClient(config.asr_url, config.worker_token)
        tts = tts or TtsClient(config.tts_url, config.worker_token)
    if pron is None and config.pron_url:
        from .workers import PronClient

        pron = PronClient(config.pron_url, config.worker_token)
    if llm is None:
        from vr_feedback.llm import LlmClient

        # llama-server runs with LLAMA_API_KEY = the worker token (config/llm/server.json).
        llm = LlmClient(config.llm_url, timeout_s=60, api_key=config.worker_token)
        llm_bg = llm_bg or LlmClient(config.llm_url, timeout_s=60, api_key=config.worker_token, default_slot=1)
    if brain is None:
        from .brain import default_brain

        brain = default_brain()
    if vad_model is None:
        from .vad import SileroModel

        vad_model = SileroModel(config.vad_model)
    svc = Services(
        config=config, db=Database(config.db_path),
        scenarios=ScenarioStore.load(config.scenarios_dir, config.scenario_schema),
        brain=brain, asr=asr, tts=tts, llm=llm, llm_bg=llm_bg or llm, vad_model=vad_model, pron=pron,
        pron_guide=GuideIndex(config.pron_guide, config.pron_lexicon),
    )
    svc.auth = LocalAuth(config)
    svc.health = HealthMonitor(svc)
    svc.sessions = SessionManager(svc)
    svc.jobs = JobManager(svc)
    svc.tts_cache = TtsCache(svc, config.cache_dir / "tts")
    return svc


async def _warm_tts_cache(svc: Services) -> None:
    """Pre-synthesize reviewed texts once the TTS worker is ready (service asset cache)."""
    for _ in range(360):
        snap = await svc.health.snapshot(force=True)
        if snap["workers"]["tts"]["ready"]:
            await svc.tts_cache.warm()
            return
        await asyncio.sleep(5)


def create_app(config: Config, *, services: Services | None = None, supervisor=None, warm_cache: bool = True) -> FastAPI:
    svc = services or build_services(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if supervisor is not None:
            supervisor.start_all()
        svc.jobs.start()
        if warm_cache:
            task = asyncio.create_task(_warm_tts_cache(svc))
            svc.background.add(task)
        log.info("gateway_started port=%d scenarios=%d", config.port, len(svc.scenarios.scenarios))
        try:
            yield
        finally:
            for task in svc.background:
                task.cancel()
            for session in list(svc.sessions.sessions.values()):
                if session.engine is not None:
                    await session.engine.shutdown("gateway_stop")
            await svc.jobs.stop()
            for client in {id(c): c for c in (svc.asr, svc.tts, svc.llm, svc.llm_bg, svc.pron) if c}.values():
                close = getattr(client, "aclose", None)
                if close:
                    await close()
            svc.db.close()
            if supervisor is not None:
                supervisor.stop_all()

    app = FastAPI(title="voice-roleplay gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc

    @app.exception_handler(ApiError)
    async def _api_error(request, exc: ApiError):
        return error_response(exc.code, exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc):
        return error_response("INVALID_REQUEST")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request, exc: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "INVALID_REQUEST"}.get(exc.status_code, "INVALID_REQUEST")
        return error_response(code, exc.status_code)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/api/"):
            ws = " ".join(f"ws://{h}" for h in sorted(config.allowed_hosts))
            response.headers["Content-Security-Policy"] = CSP.format(ws=ws)
        else:
            # Route template only (ids stay out of logs as a habit); no bodies, no queries.
            route = request.scope.get("route")
            log.info("http method=%s route=%s status=%d ms=%d", request.method,
                     getattr(route, "path", "-"), response.status_code, int((time.perf_counter() - started) * 1000))
        return response

    # ------------------------------------------------------------ meta

    @app.get("/api/health")
    async def health():
        return {**await svc.health.snapshot(), "tts_cache": svc.tts_cache.progress()}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        sid = cookie_sid({k.lower(): v for k, v in request.scope["headers"]})
        return {"csrf_token": svc.auth.csrf_for(sid), "protocol_version": 1}

    @app.get("/api/scenarios")
    async def scenarios():
        return {"scenarios": svc.scenarios.summaries()}

    @app.get("/api/scenarios/{scenario_id}")
    async def scenario(scenario_id: str):
        sc = svc.scenarios.get(scenario_id)
        if sc is None:
            raise ApiError("NOT_FOUND")
        return sc

    @app.get("/api/settings")
    async def get_settings():
        return svc.settings()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await read_model(request, SettingsIn, svc)
        svc.db.put_settings(body.model_dump(exclude_unset=True))
        return svc.settings()

    # ------------------------------------------------------------ sessions

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request):
        body = await read_model(request, SessionIn, svc)
        if body.mode == "realtime":
            await svc.health.snapshot(force=True)
        session = await svc.sessions.create(body.model_dump(exclude_none=True), svc.settings())
        return {**session.meta(), "scenario": session.scenario, "goals": session.goals,
                "realtime_url": f"/api/sessions/{session.session_id}/realtime" if session.mode == "realtime" else None}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        session = svc.sessions.get(session_id)
        engine = session.engine
        return {**session.meta(), "goals": session.goals, "summary": session.summary,
                "connected": engine is not None,
                "realtime": {"state": engine.state, "input_state": engine.input_state,
                             "output_state": engine.output_state, "epoch": session.epoch} if engine else None,
                "turns": [{k: t.get(k) for k in ("turn_id", "turn_index", "role", "text", "transcript_revision",
                                                 "playback_status", "spoken_segments")} for t in session.turns]}

    @app.post("/api/sessions/{session_id}/end")
    async def end_session(session_id: str):
        session = svc.sessions.get(session_id)
        return await svc.sessions.end(session)

    @app.websocket("/api/sessions/{session_id}/realtime")
    async def realtime(ws: WebSocket, session_id: str):
        session = svc.sessions.sessions.get(session_id)
        if session is None or session.mode != "realtime" or session.state == "ended":
            await ws.close(code=4404)
            return
        if session.engine is not None:
            await ws.close(code=4409)  # one connection per session
            return
        await ws.accept()
        if session.state == "ended":  # ended while the handshake was in flight
            await ws.close(code=CLOSE_REASONS[session.end_reason][0], reason=CLOSE_REASONS[session.end_reason][1])
            return
        engine = RealtimeEngine(session, svc, WsTransport(ws))
        session.engine = engine
        log.info("ws_connected session=%s", session_id)
        disconnected = False
        try:
            while not engine.closed:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    disconnected = True
                    break
                if msg.get("bytes") is not None:
                    await engine.on_binary(msg["bytes"])
                elif msg.get("text") is not None:
                    await engine.on_text(msg["text"])
        except WebSocketDisconnect:
            disconnected = True
        finally:
            await engine.shutdown("disconnect")
            if not disconnected:
                try:
                    await ws.close()
                except (RuntimeError, WebSocketDisconnect):
                    pass
            log.info("ws_closed session=%s", session_id)

    # ------------------------------------------------------------ attempts & jobs

    @app.post("/api/attempts", status_code=201)
    async def create_attempt(request: Request):
        body = await read_model(request, AttemptIn, svc)
        if body.target_text is not None and not ENGLISH_TEXT.match(body.target_text):
            raise ApiError("INVALID_REQUEST", message_ko="영어 문장만 연습할 수 있습니다.")
        attempt = await svc.jobs.create_attempt(body.model_dump(exclude_none=True))
        return {"attempt_id": attempt.attempt_id, "attempt_index": attempt.attempt_index,
                "exercise_type": attempt.exercise_type, "target_en": attempt.target_en,
                "question_en": attempt.question_en, "history_opt_in": attempt.history_opt_in,
                "limits": {"max_seconds": config.max_audio_s, "max_bytes": config.max_wav_bytes}}

    @app.put("/api/attempts/{attempt_id}/audio")
    async def put_audio(attempt_id: str, request: Request):
        attempt = svc.jobs.get_attempt(attempt_id)
        data = await read_body(request, config.max_wav_bytes, "AUDIO_TOO_LARGE")
        decoded = await asyncio.to_thread(parse_wav, data, config.max_audio_s)
        del data
        return {"attempt_id": attempt_id, **(await svc.jobs.set_audio(attempt, decoded))}

    @app.post("/api/attempts/{attempt_id}/submit", status_code=202)
    async def submit(attempt_id: str, request: Request):
        attempt = svc.jobs.get_attempt(attempt_id)
        key = request.headers.get("idempotency-key") or f"auto:{attempt_id}:submit"
        if len(key) > 128:
            raise ApiError("INVALID_REQUEST")
        job, created = await svc.jobs.submit(attempt, key)
        return JSONResponse(job, status_code=202 if created else 200)

    @app.get("/api/attempts/{attempt_id}/result")
    async def attempt_result(attempt_id: str):
        return svc.jobs.result(svc.jobs.get_attempt(attempt_id))

    @app.patch("/api/attempts/{attempt_id}/transcript", status_code=202)
    async def patch_transcript(attempt_id: str, request: Request):
        attempt = svc.jobs.get_attempt(attempt_id)
        body = await read_model(request, TranscriptIn, svc)
        key = request.headers.get("idempotency-key") or f"auto:{attempt_id}:rev:{uuid.uuid4().hex}"
        if len(key) > 128:
            raise ApiError("INVALID_REQUEST")
        job, created = await svc.jobs.resubmit_transcript(attempt, body.text, key)
        return JSONResponse(job, status_code=202 if created else 200)

    @app.get("/api/attempts/{attempt_id}/model-audio/{audio_id}")
    async def model_audio(attempt_id: str, audio_id: str):
        entry = svc.jobs.get_attempt(attempt_id).model_audio.get(audio_id)
        if entry is None:
            raise ApiError("NOT_FOUND")
        return wav_response(entry["wav"])

    @app.get("/api/pronunciation/guide")
    async def pronunciation_guide():
        """Pronunciation guide content (content/pronunciation/guide.json, PA-8): static, not about the learner."""
        try:
            data = await asyncio.to_thread(config.pron_guide.read_bytes)
        except OSError:
            raise ApiError("NOT_FOUND") from None
        return Response(data, media_type="application/json", headers={"Cache-Control": "no-cache"})

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        return svc.jobs.get_job(job_id).public()

    @app.delete("/api/jobs/{job_id}")
    async def cancel_job(job_id: str):
        return svc.jobs.cancel(job_id).public()

    # ------------------------------------------------------------ history & review

    @app.get("/api/history")
    async def history():
        return svc.db.history()

    @app.delete("/api/history")
    async def delete_history():
        svc.db.delete_history()
        return {"deleted": True}

    @app.post("/api/history/export")
    async def export_history(request: Request):
        body = await read_model(request, ExportIn, svc, allow_empty=True)
        data = svc.db.history()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if body.format == "json":
            content = json.dumps(data, ensure_ascii=False, indent=2)
            return Response(content, media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="history-{stamp}.json"'})
        return Response(_markdown(data), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="history-{stamp}.md"'})

    @app.get("/api/review/due")
    async def review_due():
        return {"items": reviewmod.due_items(svc.db)}

    @app.post("/api/review", status_code=201)
    async def review_add(request: Request):
        body = await read_model(request, ReviewIn, svc)
        if not svc.settings().get("history_opt_in"):
            raise ApiError("INVALID_STATE", message_ko="기록 저장에 동의한 경우에만 복습 표현을 저장할 수 있습니다.")
        item = {"item_id": "r_" + uuid.uuid4().hex[:16], **body.model_dump()}
        svc.db.add_review_item(item)
        return item

    @app.post("/api/review/{item_id}/grade")
    async def review_grade(item_id: str, request: Request):
        body = await read_model(request, GradeIn, svc)
        return reviewmod.grade(svc.db, item_id, body.result)

    # ------------------------------------------------------------ TTS

    @app.get("/api/tts/cached")
    async def tts_cached(voice_id: str, text_id: str, slow: bool = False):
        speed = 0.85 if slow else 1.0
        cached = svc.tts_cache.get(voice_id, text_id, speed)
        if cached is not None:
            return wav_response(cached)
        await svc.health.snapshot()
        if not svc.health.tts_ready():
            raise ApiError("MODEL_NOT_READY")
        try:
            return wav_response(await svc.tts_cache.ensure(voice_id, text_id, speed))
        except ApiError:
            raise
        except Exception as exc:
            log.error("tts_cached_failed error=%s", type(exc).__name__)
            raise ApiError("WORKER_FAILED") from None

    @app.post("/api/tts")
    async def tts(request: Request):
        body = await read_model(request, TtsIn, svc)
        if not ENGLISH_TEXT.match(body.text):
            raise ApiError("INVALID_REQUEST", message_ko="영어 문장만 합성할 수 있습니다.")
        await svc.health.snapshot()
        if not svc.health.tts_ready():
            raise ApiError("MODEL_NOT_READY")
        try:
            wav = await svc.tts.synthesize_wav(body.voice_id, svc.brain.normalize_for_tts(body.text), body.speed)
        except Exception as exc:
            log.error("tts_failed error=%s", type(exc).__name__)
            raise ApiError("WORKER_FAILED") from None
        return wav_response(wav)

    # Outermost, so it also guards WebSocket upgrades and rejects oversize bodies before routing.
    app.add_middleware(LocalAuthMiddleware, auth=svc.auth)

    # ------------------------------------------------------------ static web build

    dist = config.web_dist

    @app.get("/{path:path}", include_in_schema=False)
    async def static(path: str):
        if path.startswith("api/"):
            raise ApiError("NOT_FOUND")
        index = dist / "index.html"
        if not index.exists():
            return HTMLResponse(MISSING_BUILD)
        target = (dist / path).resolve()
        if path and target.is_file() and target.is_relative_to(dist.resolve()):
            return FileResponse(target)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def _markdown(data: dict) -> str:
    lines = ["# 학습 기록", ""]
    for s in data["sessions"]:
        lines += [f"## 회화 {s['scenario_id']} ({time.strftime('%Y-%m-%d %H:%M', time.localtime(s['created_at']))})", ""]
        for t in s["turns"]:
            who = "나" if t["role"] == "user" else "AI"
            lines.append(f"- **{who}**: {t.get('text') or ''}")
        for item in (s.get("summary") or {}).get("items", []):
            lines.append(f"  - 제안: {item.get('evidence_quote')} → {item.get('suggestion')} ({item.get('explanation_ko')})")
        lines.append("")
    for a in data["attempts"]:
        lines += [f"## 연습 {a['exercise_type']} {a.get('exercise_ref') or ''}", ""]
        for r in a["revisions"]:
            lines.append(f"- 전사 r{r['revision']} ({r['source']}): {r['text']}")
        for item in a["feedback"]:
            lines.append(f"  - 제안: {item.get('evidence_quote')} → {item.get('suggestion')} ({item.get('explanation_ko')})")
        lines.append("")
    return "\n".join(lines)

