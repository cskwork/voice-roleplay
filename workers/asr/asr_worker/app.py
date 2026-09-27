import asyncio
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .audio import pcm16_to_float
from .backends import Backend, load_backend
from .config import (
    LANGUAGE,
    MAX_CONTEXT_CHARS,
    MAX_TRANSCRIBE_BYTES,
    MODEL_ID,
    MODEL_REVISION,
    SAMPLE_RATE,
    Settings,
)
from .engine import AsrEngine
from .stream import StreamSession, StreamTooLong

log = logging.getLogger("asr")


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)


def _error_code(exc: Exception) -> str:
    return "OUT_OF_MEMORY" if "out of memory" in str(exc).lower() else "WORKER_FAILED"


def _check_params(language: str, context: str) -> str | None:
    if language != LANGUAGE:
        return "UNSUPPORTED_LANGUAGE"
    if len(context) > MAX_CONTEXT_CHARS:
        return "CONTEXT_TOO_LONG"
    return None


def create_app(settings: Settings, backend_factory: Callable[[], Backend] | None = None) -> FastAPI:
    backend_factory = backend_factory or (lambda: load_backend(settings.backend, settings.model_dir, settings.device))
    state: dict = {"engine": None, "device": settings.device, "backend": settings.backend, "load_error": None}

    def load() -> AsrEngine:
        started = time.monotonic()
        engine = AsrEngine(backend_factory(), settings.silence_dbfs)
        loaded = time.monotonic()
        engine.warmup()
        log.info(
            "model.ready backend=%s device=%s load_ms=%d warmup_ms=%d",
            engine.backend.name, engine.backend.device,
            (loaded - started) * 1000, (time.monotonic() - loaded) * 1000,
        )
        return engine

    async def load_in_background() -> None:
        try:
            engine = await asyncio.to_thread(load)
        except Exception as exc:
            log.error("model.load_failed error=%s: %s", type(exc).__name__, exc)
            if settings.exit_on_load_failure:
                os._exit(3)  # let the supervising gateway see the failure instead of a worker that never gets ready
            state["load_error"] = type(exc).__name__
            return
        state.update(engine=engine, device=engine.backend.device, backend=engine.backend.name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loader = asyncio.create_task(load_in_background())
        yield
        loader.cancel()
        if state["engine"]:
            state["engine"].gate.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.asr = state

    def authorized(headers) -> bool:
        return hmac.compare_digest(headers.get("x-worker-token", "").encode(), settings.token.encode())

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not authorized(request.headers):
            return _error(401, "AUTH_REQUIRED")
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {
            "ready": state["engine"] is not None,
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "device": state["device"],
            "backend": state["backend"],
            "streaming_mode": "incremental_redecode",
        }

    @app.post("/transcribe")
    async def transcribe(request: Request, language: str = LANGUAGE, context: str = ""):
        if code := _check_params(language, context):
            return _error(400, code)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/octet-stream":
            return _error(415, "AUDIO_INVALID")
        if int(request.headers.get("content-length") or 0) > MAX_TRANSCRIBE_BYTES:
            return _error(413, "AUDIO_TOO_LONG")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_TRANSCRIBE_BYTES:
                return _error(413, "AUDIO_TOO_LONG")
        if len(body) % 2:
            return _error(400, "AUDIO_INVALID")
        engine: AsrEngine | None = state["engine"]
        if engine is None:
            return _error(503, "MODEL_NOT_READY")

        started = time.monotonic()
        audio = pcm16_to_float(body)
        del body
        try:
            text = await engine.transcribe(audio, context, final=True)
        except Exception as exc:
            log.error("transcribe.failed error=%s", type(exc).__name__)
            return _error(500, _error_code(exc))
        audio_ms = len(audio) * 1000 // SAMPLE_RATE
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info("transcribe.done audio_ms=%d elapsed_ms=%d", audio_ms, elapsed_ms)
        return {"text": text, "language": LANGUAGE, "audio_ms": audio_ms, "elapsed_ms": elapsed_ms}

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        if not authorized(ws.headers):
            await ws.send_denial_response(_error(401, "AUTH_REQUIRED"))
            return
        await ws.accept()
        context = ws.query_params.get("context", "")
        engine: AsrEngine | None = state["engine"]
        code = _check_params(ws.query_params.get("language", LANGUAGE), context)
        if code or engine is None:
            await ws.send_json({"type": "error", "code": code or "MODEL_NOT_READY"})
            await ws.close()
            return

        send_lock = asyncio.Lock()

        async def send(msg: dict) -> None:
            async with send_lock:
                await ws.send_json(msg)

        session = StreamSession(
            engine,
            send,
            context=context,
            interval_ms=settings.partial_interval_ms,
            min_new_ms=settings.partial_min_new_ms,
            window_s=settings.partial_window_s,
        )
        log.info("stream.open")
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    log.info("stream.disconnect samples=%d", session.samples)
                    return
                if msg.get("bytes") is not None:
                    data = msg["bytes"]
                    if len(data) % 2:
                        await send({"type": "error", "code": "AUDIO_INVALID"})
                        break
                    session.add_audio(data)
                    continue
                try:
                    kind = json.loads(msg.get("text") or "").get("type")
                except (ValueError, AttributeError):
                    kind = None
                if kind == "commit":
                    await session.commit()
                    break
                if kind == "cancel":
                    log.info("stream.cancel samples=%d", session.samples)
                    break
                await send({"type": "error", "code": "INVALID_MESSAGE"})
        except StreamTooLong:
            log.info("stream.too_long samples=%d", session.samples)
            await send({"type": "error", "code": "AUDIO_TOO_LONG"})
        except WebSocketDisconnect:
            log.info("stream.disconnect samples=%d", session.samples)
            return
        except Exception as exc:
            log.error("stream.failed error=%s", type(exc).__name__)
            await send({"type": "error", "code": _error_code(exc)})
        finally:
            session.close()
        await ws.close()

    return app


def main() -> None:
    import uvicorn

    from .config import check_model_files

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    check_model_files(settings.model_dir)
    # Access logs are off: query strings carry the scenario `context` text (PROTOCOL §2).
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
