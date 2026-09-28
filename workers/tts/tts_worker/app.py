"""TTS worker HTTP/WS API (PROTOCOL §4). Binds 127.0.0.1:8712 via ``python -m tts_worker``."""

import asyncio
import contextlib
import hmac
import io
import json
import logging
import os
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .backend import create_engine
from .engine import MAX_SPEED, MIN_SPEED, MODEL_ID, Engine, SetupError
from .textnorm import TextRejected, normalize

log = logging.getLogger("vr.tts.api")

FRAME_SAMPLES_MS = 100  # binary frames carry at most 100 ms of audio
MAX_QUEUED = 8
MAX_BODY_BYTES = 16 * 1024


class RequestError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def _authorized(headers, token: str) -> bool:
    given = headers.get("x-worker-token", "")
    return bool(token) and hmac.compare_digest(given.encode(), token.encode())


def _prepare(engine: Engine | None, voice_id, text, speed) -> tuple[str, str, float]:
    """Validate a synthesis request; return (normalized text, voice id, speed)."""
    if engine is None or not engine.ready:
        raise RequestError("MODEL_NOT_READY", 503)
    if not isinstance(voice_id, str) or voice_id not in engine.voices:
        raise RequestError("VOICE_NOT_FOUND", 404)
    if not isinstance(text, str):
        raise RequestError("BAD_REQUEST")
    try:
        speed = float(1.0 if speed is None else speed)
    except (TypeError, ValueError):
        raise RequestError("INVALID_SPEED") from None
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise RequestError("INVALID_SPEED")
    try:
        return normalize(text), voice_id, speed
    except TextRejected as e:
        raise RequestError(e.code) from None


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


@dataclass
class _Job:
    request_id: str
    voice_id: object
    text: object
    speed: object
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False


def create_app(engine: Engine | None = None, token: str | None = None, load_in_background: bool = True) -> FastAPI:
    """``engine`` may be injected (tests); otherwise one is built from env and loaded at startup."""
    token = token if token is not None else os.environ.get("VR_WORKER_TOKEN", "")
    if not token:
        raise RuntimeError("VR_WORKER_TOKEN is required")
    state = {"engine": engine, "error": None}

    def _load():
        try:
            eng = state["engine"] or create_engine()
            state["engine"] = eng
            if not eng.ready:
                eng.load()
        except Exception as e:  # keep serving /health with the error code
            state["error"] = type(e).__name__
            # SetupError messages hold only paths and setup hints; other errors are logged by type only.
            log.error("model_load_failed error=%s%s", type(e).__name__, f" detail={e}" if isinstance(e, SetupError) else "")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if load_in_background:
            threading.Thread(target=_load, name="tts-load", daemon=True).start()
        else:
            _load()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not _authorized(request.headers, token):
            return JSONResponse({"error": {"code": "AUTH_REQUIRED"}}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health():
        eng: Engine | None = state["engine"]
        ready = bool(eng and eng.ready)
        body = {
            "ready": ready,
            "model_id": eng.model_id if eng else MODEL_ID,
            "revision": eng.revision if eng else None,
            "device": eng.device if ready else None,
            "placement": eng.placement if ready else None,
            "sample_rate": eng.sample_rate if ready else None,
            "voices": [{"voice_id": v.voice_id, "label": v.label, "license_note": v.license_note}
                       for v in (eng.voices.values() if eng else [])],
            "load_seconds": round(eng.load_seconds, 1) if ready and eng.load_seconds else None,
        }
        if state["error"]:
            body["error"] = state["error"]
        return body

    @app.post("/synthesize")
    async def synthesize_wav(request: Request):
        try:
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                raise RequestError("BAD_REQUEST", 413)
            try:
                req = json.loads(body)
            except ValueError:
                raise RequestError("BAD_REQUEST") from None
            if not isinstance(req, dict):
                raise RequestError("BAD_REQUEST")
            text, voice_id, speed = _prepare(state["engine"], req.get("voice_id"), req.get("text"), req.get("speed"))
        except RequestError as e:
            return JSONResponse({"error": {"code": e.code}}, status_code=e.status)
        eng: Engine = state["engine"]
        rid = uuid.uuid4().hex[:8]
        t0 = time.perf_counter()

        def run() -> bytes:
            # Non-stream mode is faster for a whole-utterance WAV (one flow pass instead of re-running per chunk).
            with contextlib.closing(eng.synthesize(text, voice_id, speed, threading.Event(), stream=False)) as gen:
                return b"".join(chunk.tobytes() for chunk in gen)

        try:
            pcm = await asyncio.to_thread(run)
        except Exception as e:
            log.error("synthesize_failed rid=%s error=%s", rid, type(e).__name__)
            return JSONResponse({"error": {"code": "WORKER_FAILED"}}, status_code=500)
        audio_ms = len(pcm) // 2 * 1000 // eng.sample_rate
        log.info("synthesize_wav rid=%s voice=%s chars=%d audio_ms=%d elapsed_ms=%d", rid, voice_id, len(text),
                 audio_ms, (time.perf_counter() - t0) * 1000)
        return Response(_wav_bytes(pcm, eng.sample_rate), media_type="audio/wav")

    @app.websocket("/synthesize")
    async def synthesize_ws(ws: WebSocket):
        if not _authorized(ws.headers, token):
            if "websocket.http.response" in ws.scope.get("extensions", {}):
                await ws.send_denial_response(JSONResponse({"error": {"code": "AUTH_REQUIRED"}}, status_code=401))
            else:
                await ws.close(code=1008)  # server without denial-response support: HTTP 403 on the upgrade
            return
        await ws.accept()
        conn = _Connection(ws, state)
        await conn.run()

    return app


class _Connection:
    """One WS connection: requests run in order; cancel may target the active or a queued request."""

    def __init__(self, ws: WebSocket, state: dict):
        self.ws, self.state = ws, state
        self.send_lock = asyncio.Lock()
        self.queue: asyncio.Queue[_Job] = asyncio.Queue()
        self.jobs: dict[str, _Job] = {}  # queued or active, by request_id
        self.active: _Job | None = None

    async def send_json(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(msg))

    async def run(self) -> None:
        worker = asyncio.create_task(self._worker())
        try:
            while True:
                event = await self.ws.receive()
                if event["type"] == "websocket.disconnect":
                    break
                try:
                    msg = json.loads(event.get("text") or "")
                    kind = msg.get("type") if isinstance(msg, dict) else None
                except ValueError:
                    kind, msg = None, {}
                if not isinstance(msg, dict):
                    msg = {}
                rid = msg.get("request_id")
                if kind == "synthesize" and isinstance(rid, str) and rid:
                    if rid in self.jobs:
                        await self.send_json({"type": "error", "request_id": rid, "code": "DUPLICATE_REQUEST"})
                        continue
                    if self.queue.qsize() >= MAX_QUEUED:
                        await self.send_json({"type": "error", "request_id": rid, "code": "QUEUE_FULL"})
                        continue
                    job = _Job(rid, msg.get("voice_id"), msg.get("text"), msg.get("speed", 1.0))
                    self.jobs[rid] = job
                    self.queue.put_nowait(job)
                elif kind == "cancel" and isinstance(rid, str):
                    await self._cancel(rid)
                else:
                    await self.send_json({"type": "error", "request_id": rid if isinstance(rid, str) else None,
                                          "code": "BAD_REQUEST"})
        except WebSocketDisconnect:
            pass
        finally:
            for job in self.jobs.values():
                job.cancel.set()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _cancel(self, rid: str) -> None:
        job = self.jobs.get(rid)
        async with self.send_lock:
            # Under the send lock: once "cancelled" is out, the worker sees the flag before any further frame.
            if job is None or job.finished or job.cancel.is_set():
                return  # unknown or already terminal: nothing to do
            job.cancel.set()
            await self.ws.send_text(json.dumps({"type": "cancelled", "request_id": rid}))
        log.info("cancel rid=%s active=%s", rid, job is self.active)

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            self.active = job
            try:
                if not job.cancel.is_set():
                    await self._run_job(job)
            finally:
                job.finished = True
                self.jobs.pop(job.request_id, None)
                self.active = None

    async def _run_job(self, job: _Job) -> None:
        try:
            text, voice_id, speed = _prepare(self.state["engine"], job.voice_id, job.text, job.speed)
        except RequestError as e:
            await self._finish(job, {"type": "error", "request_id": job.request_id, "code": e.code})
            return
        eng: Engine = self.state["engine"]
        sr = eng.sample_rate
        frame_bytes = sr * FRAME_SAMPLES_MS // 1000 * 2
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue = asyncio.Queue()

        def produce():
            try:
                with contextlib.closing(eng.synthesize(text, voice_id, speed, job.cancel)) as gen:
                    for pcm in gen:
                        loop.call_soon_threadsafe(chunks.put_nowait, ("audio", pcm.tobytes()))
                loop.call_soon_threadsafe(chunks.put_nowait, ("end", None))
            except Exception as e:
                loop.call_soon_threadsafe(chunks.put_nowait, ("error", type(e).__name__))

        async with self.send_lock:
            if job.cancel.is_set():
                return
            await self.ws.send_text(json.dumps({"type": "start", "request_id": job.request_id, "sample_rate": sr}))
        t0 = time.perf_counter()
        first_ms, sent_bytes = None, 0
        producer = loop.run_in_executor(None, produce)
        try:
            while True:
                kind, data = await chunks.get()
                if kind != "audio":
                    break
                if first_ms is None:
                    first_ms = int((time.perf_counter() - t0) * 1000)
                for i in range(0, len(data), frame_bytes):
                    async with self.send_lock:
                        if job.cancel.is_set():
                            break
                        await self.ws.send_bytes(data[i:i + frame_bytes])
                    sent_bytes += len(data[i:i + frame_bytes])
        finally:
            await producer  # the model lock is released only after the LLM thread has stopped
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        audio_ms = sent_bytes // 2 * 1000 // sr
        if kind == "error":
            log.error("synthesize_failed rid=%s error=%s", job.request_id, data)
            await self._finish(job, {"type": "error", "request_id": job.request_id, "code": "WORKER_FAILED"})
            return
        log.info("synthesize_ws rid=%s voice=%s chars=%d speed=%.2f audio_ms=%d first_chunk_ms=%s elapsed_ms=%d%s",
                 job.request_id, voice_id, len(text), speed, audio_ms, first_ms, elapsed_ms,
                 " cancelled" if job.cancel.is_set() else "")
        await self._finish(job, {"type": "done", "request_id": job.request_id, "audio_ms": audio_ms,
                                 "elapsed_ms": elapsed_ms, "first_chunk_ms": first_ms})

    async def _finish(self, job: _Job, msg: dict) -> None:
        async with self.send_lock:
            job.finished = True
            if not job.cancel.is_set():  # a cancelled job already got its terminal "cancelled"
                await self.ws.send_text(json.dumps(msg))
