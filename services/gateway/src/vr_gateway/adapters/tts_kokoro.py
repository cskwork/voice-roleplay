"""TTS adapter for API mode without a speech key: Kokoro-82M (ONNX int8) inside the gateway process, PROTOCOL §4.1.

Same interface as `workers.TtsClient` and `OpenRouterTts`: `health()`, `synthesize_wav(voice_id, text, speed)` and
`stream_session()` returning a stream whose `synthesize(request_id, voice_id, text, speed)` yields ("start", rate),
("audio", bytes)..., then ("done", info) | ("cancelled", None); plus `cancel(request_id)` and `close()`.

- Model files: models.lock.json "Kokoro-82M ONNX int8" (`./app setup --api` downloads and checks them). The model loads
  in a thread when `health()` is first asked (the gateway's cache warm-up does that at start) and says one short word
  so the first real sentence does not pay the ONNX warm-up (about 2.5 s on an M4, then ~0.25x real time).
- One sentence at a time on this machine (a lock), synthesized in a worker thread; the PCM16 result is sent in 100 ms
  frames. A cancel drops everything not yet sent; a sentence already being synthesized finishes in its thread and is
  thrown away.
- Voice ids come from `config/voices.kokoro.json` (`voices[].voice_id -> voice`, plus `aliases` so scenarios that name
  a local CosyVoice or hosted voice still get a Kokoro one). Nothing leaves this machine.

Never logs synthesized text (PROTOCOL §2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from ..audio import encode_wav
from ..workers import WorkerError
from .tts_openrouter import clamp_speed, load_voices

log = logging.getLogger("vr_gateway.adapters.tts_kokoro")

FRAME_MS = 100
# Reported in /api/health (a plain id: Content Forge shows only names of this shape).
MODEL_ID = "local/kokoro-82m-v1.0-int8"
MAX_CHARS = 400
# Model control-like markup and tags are never read out.
_MARKUP = re.compile(r"<\|[^|>]*\|>|<[^<>]{0,40}>|\[[A-Za-z_ ]{1,30}\]")


def clean_text(text: str) -> str:
    return " ".join(_MARKUP.sub(" ", text).split())[:MAX_CHARS]


def to_pcm16(samples) -> bytes:
    audio = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (audio * 32767.0).astype("<i2").tobytes()


class KokoroTts:
    def __init__(self, model_path: Path, voices_bin: Path, voices_path: Path, *, engine_factory=None):
        self.model_path, self.voices_bin = Path(model_path), Path(voices_bin)
        self.voices, self.aliases, self.sample_rate, file_model = load_voices(voices_path)
        self.model = file_model or "Kokoro-82M"
        self._voice_names = {v["voice_id"]: v["voice"] for v in self.voices}
        self._factory = engine_factory  # tests inject a fake engine (create(text, voice, speed, lang) -> (samples, sr))
        self._engine = None
        self._load_task: asyncio.Task | None = None
        self._error: str | None = None
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._load_task and not self._load_task.done():
            self._load_task.cancel()

    def voice_for(self, voice_id: str) -> str:
        voice_id = self.aliases.get(voice_id, voice_id)
        return self._voice_names.get(voice_id) or self.voices[0]["voice"]

    def files_present(self) -> bool:
        return self.model_path.is_file() and self.voices_bin.is_file()

    def _build(self):
        if self._factory is not None:
            engine = self._factory()
        else:
            from kokoro_onnx import Kokoro  # imported only when this engine is used

            # With LOG_LEVEL=DEBUG the package logs the phonemes of every line: never in the gateway log (PROTOCOL §2)
            logging.getLogger("kokoro_onnx").setLevel(logging.WARNING)
            engine = Kokoro(str(self.model_path), str(self.voices_bin))
        engine.create("Hello.", voice=self.voices[0]["voice"], speed=1.0, lang="en-us")  # ONNX warm-up
        return engine

    async def _load(self) -> None:
        started = time.perf_counter()
        try:
            self._engine = await asyncio.to_thread(self._build)
            log.info("tts_kokoro_ready ms=%d", int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - reported through health, never raised into the gateway
            self._error = type(exc).__name__
            log.warning("tts_kokoro_load_failed error=%s", self._error)

    def _start_loading(self) -> None:
        if self._engine is None and self._error is None and self._load_task is None and self.files_present():
            self._load_task = asyncio.create_task(self._load())

    async def ready(self) -> bool:
        """Wait for the model when it is loading; False when the files are missing or loading failed."""
        self._start_loading()
        if self._load_task is not None:
            await asyncio.shield(self._load_task)
        return self._engine is not None

    async def health(self) -> dict:
        self._start_loading()
        info = {"ready": self._engine is not None, "model_id": MODEL_ID, "revision": "model-files-v1.0",
                "device": "cpu", "sample_rate": self.sample_rate, "provider": "kokoro",
                "voices": [{"voice_id": v["voice_id"], "label": v["label"], "license_note": v.get("license_note", "")}
                           for v in self.voices]}
        if not self.files_present():
            info["error"] = "model files missing (run ./app setup --api)"
        elif self._error:
            info["error"] = f"model load failed ({self._error})"
        return info

    async def _pcm(self, voice_id: str, text: str, speed: float, skip=None) -> tuple[bytes, int] | None:
        """PCM16 and its rate; None when `skip()` turned true while waiting for the model (nothing synthesized)."""
        if not await self.ready():
            raise WorkerError("MODEL_NOT_READY")
        text = clean_text(text)
        if not text:
            return b"", self.sample_rate
        voice, speed = self.voice_for(voice_id), clamp_speed(speed)
        async with self._lock:
            if skip is not None and skip():
                return None
            work = asyncio.ensure_future(asyncio.to_thread(self._engine.create, text, voice=voice, speed=speed,
                                                           lang="en-us"))
            try:
                samples, rate = await asyncio.shield(work)
            except asyncio.CancelledError:
                # The caller went away, but the thread keeps the model busy: hold the lock until it is done so two
                # sentences never run on the engine at once.
                with contextlib.suppress(BaseException):
                    await work
                raise
        return to_pcm16(samples), int(rate)

    async def synthesize_wav(self, voice_id: str, text: str, speed: float = 1.0) -> bytes:
        try:
            pcm, rate = await self._pcm(voice_id, text, speed)  # type: ignore[misc] - no skip: never None
        except WorkerError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("tts_kokoro_failed error=%s", type(exc).__name__)
            raise WorkerError("WORKER_FAILED") from exc
        return encode_wav(pcm, rate)

    def stream_session(self) -> KokoroTtsStream:
        return KokoroTtsStream(self)


class KokoroTtsStream:
    """Sequential synthesize requests for one realtime session."""

    def __init__(self, owner: KokoroTts):
        self.owner = owner
        self._lock = asyncio.Lock()
        self._cancelled: set[str] = set()

    async def synthesize(self, request_id: str, voice_id: str, text: str,
                         speed: float = 1.0) -> AsyncIterator[tuple[str, object]]:
        async with self._lock:
            try:
                if request_id in self._cancelled:
                    yield "cancelled", None
                    return
                started = time.perf_counter()
                try:
                    out = await self.owner._pcm(voice_id, text, speed, skip=lambda: request_id in self._cancelled)
                except WorkerError as exc:
                    raise WorkerError("TTS_FAILED") from exc
                except Exception as exc:  # noqa: BLE001
                    log.warning("tts_kokoro_failed error=%s", type(exc).__name__)
                    raise WorkerError("TTS_FAILED") from exc
                if out is None or request_id in self._cancelled:
                    yield "cancelled", None
                    return
                pcm, rate = out
                first_ms = int((time.perf_counter() - started) * 1000)
                yield "start", rate
                step = rate * 2 * FRAME_MS // 1000
                for i in range(0, len(pcm), step):
                    if request_id in self._cancelled:
                        yield "cancelled", None
                        return
                    yield "audio", pcm[i:i + step]
                yield "done", {"request_id": request_id, "audio_ms": len(pcm) * 1000 // (2 * rate),
                               "elapsed_ms": int((time.perf_counter() - started) * 1000), "first_chunk_ms": first_ms}
            finally:
                self._cancelled.discard(request_id)

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    async def close(self) -> None:
        """Nothing is kept open between requests."""
