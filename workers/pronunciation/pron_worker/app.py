import asyncio
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import prosody
from .aligner import AlignedWord, Aligner, QwenForcedAligner, align_words, has_alignable_text
from .audio import AudioError, audio_ms, decode_audio_b64
from .config import ALIGNER_ID, ALIGNER_REVISION, MAX_BODY_BYTES, MAX_TEXT_CHARS, Settings
from .gop import GopScorer, load_scorer

log = logging.getLogger("pron")

SILENCE_DBFS = -40.0
MIN_VOICED_FRAMES = 3  # of 30 ms: audio with < ~90 ms above SILENCE_DBFS has nothing to align


class RequestError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code}}, status_code=status)


def _error_code(exc: Exception) -> str:
    return "OUT_OF_MEMORY" if "out of memory" in str(exc).lower() else "WORKER_FAILED"


def is_silent(x: np.ndarray) -> bool:
    frame = 480  # 30 ms at 16 kHz
    n = len(x) // frame
    if n == 0:
        return True
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    return int(np.count_nonzero(rms > 10 ** (SILENCE_DBFS / 20))) < MIN_VOICED_FRAMES


async def _read_json(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise RequestError(415, "BAD_REQUEST")
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise RequestError(413, "AUDIO_TOO_LONG")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise RequestError(413, "AUDIO_TOO_LONG")
    try:
        data = json.loads(body)
    except ValueError:
        raise RequestError(400, "BAD_REQUEST") from None
    if not isinstance(data, dict):
        raise RequestError(400, "BAD_REQUEST")
    return data


def _text_field(data: dict, key: str) -> str:
    text = data.get(key)
    if not isinstance(text, str):
        raise RequestError(400, "BAD_REQUEST")
    if len(text) > MAX_TEXT_CHARS:
        raise RequestError(400, "BAD_REQUEST")
    if not has_alignable_text(text):
        raise RequestError(400, "TEXT_EMPTY")
    return text


def _words_field(data: dict, total_ms: int) -> list[dict]:
    """Optional `words` of /prosody: [{i, start_ms, end_ms, ...}] as returned by /align."""
    words = data.get("words")
    if words is None:
        return []
    if not isinstance(words, list):
        raise RequestError(400, "BAD_REQUEST")
    out = []
    for w in words:
        ok = isinstance(w, dict) and all(type(w.get(k)) is int for k in ("i", "start_ms", "end_ms"))
        if not ok or not 0 <= w["start_ms"] <= w["end_ms"] <= total_ms:
            raise RequestError(400, "BAD_REQUEST")
        out.append({"i": w["i"], "start_ms": w["start_ms"], "end_ms": w["end_ms"]})
    return out


def create_app(
    settings: Settings,
    aligner_factory: Callable[[], Aligner] | None = None,
    scorer_factory: Callable[[], GopScorer | None] | None = None,
) -> FastAPI:
    aligner_factory = aligner_factory or (lambda: QwenForcedAligner(settings.model_dir, settings.device))
    scorer_factory = scorer_factory or (lambda: load_scorer(settings, settings.device))
    state: dict = {"aligner": None, "scorer": None, "device": settings.device}
    # Requests are served one at a time (PROTOCOL §12.2): model calls and pitch analysis share one thread.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pron-worker")

    def load() -> None:
        started = time.monotonic()
        aligner = aligner_factory()
        loaded = time.monotonic()
        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(16_000) * 0.05).astype(np.float32)
        aligner.align(noise, "hello")  # first MPS pass compiles kernels
        warmed = time.monotonic()
        scorer = scorer_factory()
        if scorer:
            scorer.assess(noise, "hello", [AlignedWord(0, "hello", 0, 1000)], "scripted")  # load + warm-up
        log.info(
            "model.ready device=%s load_ms=%d warmup_ms=%d assess=%s scorer_ms=%d calibration=%s",
            aligner.device, (loaded - started) * 1000, (warmed - loaded) * 1000,
            "available" if scorer else "not_implemented", (time.monotonic() - warmed) * 1000,
            "loaded" if scorer and scorer.calibration_version else "none",
        )
        state.update(aligner=aligner, scorer=scorer, device=aligner.device)

    async def load_in_background() -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(pool, load)
        except Exception as exc:
            log.error("model.load_failed error=%s", type(exc).__name__)
            if settings.exit_on_load_failure:
                os._exit(3)  # the supervising gateway sees the failure instead of a worker that never gets ready
            state["load_error"] = type(exc).__name__

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loader = asyncio.create_task(load_in_background())
        yield
        loader.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.pron = state

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get("x-worker-token", "").encode(), settings.token.encode()):
            return _error(401, "AUTH_REQUIRED")
        return await call_next(request)

    @app.exception_handler(RequestError)
    async def request_error(request: Request, exc: RequestError):
        return _error(exc.status, exc.code)

    def bands_enabled(scorer: GopScorer | None) -> bool:
        """Bands need both a loaded calibration and VR_PRON_EXPERIMENTAL=1 (PROTOCOL §12.3)."""
        return bool(scorer and scorer.calibration_version is not None and settings.experimental)

    async def run(fn):
        return await asyncio.get_running_loop().run_in_executor(pool, fn)

    def ready_aligner() -> Aligner:
        if state["aligner"] is None:
            raise RequestError(503, "MODEL_NOT_READY")
        return state["aligner"]

    @app.get("/health")
    async def health():
        scorer: GopScorer | None = state["scorer"]
        return {
            "ready": state["aligner"] is not None,
            "device": state["device"],
            "models": {
                "aligner": {"model_id": ALIGNER_ID, "revision": ALIGNER_REVISION},
                "phones": scorer.phones_model if scorer else None,
            },
            "prosody_method": prosody.METHOD,
            "calibration_version": scorer.calibration_version if scorer else None,
            "bands_enabled": bands_enabled(scorer),
        }

    @app.post("/align")
    async def align(request: Request):
        data = await _read_json(request)
        audio = _audio_field(data)
        text = _text_field(data, "text")
        aligner = ready_aligner()
        if is_silent(audio):
            raise RequestError(422, "NO_SPEECH")
        started = time.monotonic()
        try:
            words = await run(lambda: align_words(aligner, audio, text))
        except Exception as exc:
            log.error("align.failed error=%s", type(exc).__name__)
            return _error(500, _error_code(exc))
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info("align.done audio_ms=%d words=%d elapsed_ms=%d", audio_ms(audio), len(words), elapsed_ms)
        return {"words": [w.to_json() for w in words], "model_revision": ALIGNER_REVISION, "elapsed_ms": elapsed_ms}

    @app.post("/prosody")
    async def prosody_endpoint(request: Request):
        data = await _read_json(request)
        audio = _audio_field(data)
        words = _words_field(data, audio_ms(audio))
        ready_aligner()  # not needed for pitch, but the contract is: nothing is served before the worker is ready
        started = time.monotonic()
        try:
            f0 = await run(lambda: prosody.f0_contour(audio))
        except Exception as exc:
            log.error("prosody.failed error=%s", type(exc).__name__)
            return _error(500, _error_code(exc))
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info("prosody.done audio_ms=%d frames=%d words=%d elapsed_ms=%d", audio_ms(audio), len(f0), len(words), elapsed_ms)
        return {
            "f0_hz": [round(float(v), 1) for v in f0],
            "hop_ms": prosody.HOP_MS,
            "per_word": prosody.word_stats(f0, words),
            "method": prosody.METHOD,
            "elapsed_ms": elapsed_ms,
        }

    @app.post("/assess")
    async def assess(request: Request):
        scorer: GopScorer | None = state["scorer"]
        if scorer is None:
            return _error(501, "NOT_IMPLEMENTED")
        data = await _read_json(request)
        audio = _audio_field(data)
        reference_text = _text_field(data, "reference_text")
        mode = data.get("mode")
        if mode not in ("scripted", "unscripted"):
            raise RequestError(400, "BAD_REQUEST")
        aligner = ready_aligner()
        if is_silent(audio):
            raise RequestError(422, "NO_SPEECH")
        started = time.monotonic()

        def assess_words():
            words = align_words(aligner, audio, reference_text)
            return scorer.assess(audio, reference_text, words, mode)

        try:
            words = await run(assess_words)
        except Exception as exc:
            log.error("assess.failed error=%s", type(exc).__name__)
            return _error(500, _error_code(exc))
        log.info("assess.done audio_ms=%d words=%d elapsed_ms=%d", audio_ms(audio), len(words), (time.monotonic() - started) * 1000)
        enabled = bands_enabled(scorer)
        if not enabled:
            words = [{**w, "band": None} for w in words]
        return {
            "words": words,
            "calibration_version": scorer.calibration_version,
            "bands_enabled": enabled,
            "model_revisions": {"aligner": ALIGNER_REVISION, "phones": scorer.phones_model["revision"]},
        }

    return app


def _audio_field(data: dict) -> np.ndarray:
    try:
        return decode_audio_b64(data.get("audio_b64"))
    except AudioError as exc:
        raise RequestError(exc.status, exc.code) from None


def main() -> None:
    import uvicorn

    from .config import check_model_files

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    check_model_files(settings.model_dir)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
