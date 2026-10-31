"""ASR adapter for API mode: OpenAI realtime transcription (`gpt-live-transcribe`), PROTOCOL §3.1.

Same interface as `workers.AsrClient`: `open_stream(on_partial) -> AsrStream{send_audio, commit, cancel, close}` and
`transcribe(pcm16, context)`. One WebSocket transcription session per utterance:

- connect `wss://api.openai.com/v1/realtime?intent=transcription` (no `model` in the URL: that would be a
  conversation session), then `session.update` with `session.type = "transcription"`, PCM 24 kHz input,
  `turn_detection: null` (the gateway's own VAD decides when a turn ends) and `languages: ["en"]`;
- audio arrives as PCM16 16 kHz mono and is resampled to 24 kHz (the only PCM rate the API accepts), then sent as
  base64 `input_audio_buffer.append`;
- `conversation.item.input_audio_transcription.delta` events carry newly available text; the adapter appends them and
  hands the whole text so far to `on_partial`, so each partial replaces the previous one (PROTOCOL §3);
- `commit()` sends `input_audio_buffer.commit` and waits for `...transcription.completed` (`transcript`).

Any provider error, a dropped connection, or a stream longer than the cap ends the stream with WorkerError
("ASR_FAILED"); `transcribe` raises WorkerError("WORKER_FAILED") like the local worker client so the job layer retries
once. Never logs transcripts or audio (PROTOCOL §2); the API key goes only into the Authorization header.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable

import numpy as np
import soxr
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from ..workers import WorkerError

log = logging.getLogger("vr_gateway.adapters.asr_openai")

IN_RATE = 16000
API_RATE = 24000
STREAM_CAP_S = 45.5  # the gateway auto-commits at 45 s; same slack as the local worker (PROTOCOL §3)
TRANSCRIBE_CAP_S = 120.0  # recorded practice limit (PROTOCOL §3 /transcribe)
CHUNK_SAMPLES = IN_RATE  # transcribe(): 1 s of audio per append event
PROMPT_CHARS = 300


class OpenAiAsr:
    def __init__(self, api_key: str, *, url: str = "wss://api.openai.com/v1/realtime?intent=transcription",
                 model: str = "gpt-live-transcribe", language: str = "en", connect_timeout_s: float = 10.0):
        self._key = api_key
        self.url = url
        self.model = model
        self.language = language
        self.connect_timeout_s = connect_timeout_s

    async def aclose(self) -> None:
        pass

    async def health(self) -> dict:
        """No network call (no cost): ready when a key is configured."""
        return {"ready": bool(self._key), "model_id": f"openai/{self.model}", "revision": "api", "device": "api",
                "backend": "openai-realtime", "streaming_mode": "native", "provider": "openai"}

    def session_update(self, prompt: str | None = None) -> dict:
        transcription: dict = {"model": self.model, "languages": [self.language]}
        if prompt:
            transcription["prompt"] = prompt[:PROMPT_CHARS]
        return {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": API_RATE},
            "transcription": transcription,
            "turn_detection": None,
        }}}}

    async def open_stream(self, on_partial: Callable[[str], None], *, prompt: str | None = None,
                          cap_s: float = STREAM_CAP_S) -> OpenAiAsrStream:
        if not self._key:
            raise WorkerError("ASR_FAILED")
        try:
            ws = await asyncio.wait_for(
                connect(self.url, additional_headers={"Authorization": f"Bearer {self._key}"}, max_size=2**22),
                self.connect_timeout_s)
            await ws.send(json.dumps(self.session_update(prompt)))
        except (OSError, WebSocketException, TimeoutError) as exc:
            log.warning("asr_connect_failed error=%s", type(exc).__name__)
            raise WorkerError("ASR_FAILED") from exc
        return OpenAiAsrStream(ws, on_partial, cap_s=cap_s)

    async def transcribe(self, pcm16: bytes, context: str | None = None) -> dict:
        """Whole recording (recorded practice): stream it through one session and commit. `context` is the scenario
        hint (never the target sentence, PROTOCOL §7) and becomes the transcription prompt."""
        if len(pcm16) % 2:
            raise WorkerError("AUDIO_INVALID")
        if len(pcm16) > TRANSCRIBE_CAP_S * IN_RATE * 2:
            raise WorkerError("AUDIO_TOO_LONG")
        started = time.perf_counter()
        try:
            stream = await self.open_stream(lambda _text: None, prompt=context, cap_s=TRANSCRIBE_CAP_S + 0.5)
            step = CHUNK_SAMPLES * 2
            for i in range(0, len(pcm16), step):
                await stream.send_audio(pcm16[i : i + step])
            final = await stream.commit(timeout_s=60.0)
        except WorkerError as exc:
            raise WorkerError("WORKER_FAILED") from exc
        return {"text": final.get("text", ""), "language": "English", "audio_ms": len(pcm16) // 32,
                "elapsed_ms": int((time.perf_counter() - started) * 1000)}


class OpenAiAsrStream:
    """One utterance on one transcription session. Partials go to the callback; commit() returns the final dict."""

    def __init__(self, ws: ClientConnection, on_partial: Callable[[str], None], *, cap_s: float = STREAM_CAP_S):
        self.ws = ws
        self.on_partial = on_partial
        self.cap_samples = int(cap_s * IN_RATE)
        self.samples_in = 0
        self.started = time.perf_counter()
        self._resampler = soxr.ResampleStream(IN_RATE, API_RATE, 1, dtype="int16", quality="HQ")
        self._item: str | None = None
        self._text = ""
        self._committed = False
        self._closed = False
        self._final: asyncio.Future = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read())

    # ------------------------------------------------------------ server events

    def _fail(self, reason: str) -> None:
        if not self._final.done():
            log.warning("asr_stream_failed reason=%s", reason)
            self._final.set_exception(WorkerError("ASR_FAILED"))

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    continue
                data = json.loads(msg)
                kind = data.get("type")
                if kind == "conversation.item.input_audio_transcription.delta":
                    item = data.get("item_id")
                    if item != self._item:  # a new item starts a new text
                        self._item, self._text = item, ""
                    self._text += data.get("delta") or ""
                    if self._text.strip():  # the gateway ignores partials once the turn is finalizing
                        self.on_partial(self._text.strip())
                elif kind == "conversation.item.input_audio_transcription.completed":
                    if not self._final.done():
                        self._final.set_result((data.get("transcript") or "").strip())
                    return
                elif kind == "conversation.item.input_audio_transcription.failed":
                    self._fail("transcription_failed")
                    return
                elif kind == "error":
                    err = data.get("error") or {}
                    self._fail(f"error:{err.get('type') or '-'}:{err.get('code') or '-'}")
                    return
        except (WebSocketException, ValueError, OSError):
            pass
        self._fail("connection_closed")

    # ------------------------------------------------------------ client side

    async def _append(self, pcm24: np.ndarray) -> None:
        if pcm24.size:
            await self.ws.send(json.dumps({"type": "input_audio_buffer.append",
                                           "audio": base64.b64encode(pcm24.astype("<i2").tobytes()).decode("ascii")}))

    async def send_audio(self, pcm16: bytes) -> None:
        if self._final.done():
            # The session already failed (or finished): surface it now instead of at commit.
            raise WorkerError("ASR_FAILED")
        samples = np.frombuffer(pcm16, dtype="<i2")
        self.samples_in += samples.size
        if self.samples_in > self.cap_samples:
            self._fail("too_long")
            raise WorkerError("ASR_FAILED")
        try:
            await self._append(self._resampler.resample_chunk(samples))
        except WebSocketException as exc:
            raise WorkerError("ASR_FAILED") from exc

    async def commit(self, timeout_s: float = 30.0) -> dict:
        try:
            if self.samples_in == 0:
                return {"type": "final", "text": "", "audio_ms": 0, "elapsed_ms": 0}  # empty buffer: nothing to commit
            await self._append(self._resampler.resample_chunk(np.zeros(0, dtype=np.int16), last=True))
            self._committed = True
            await self.ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            text = await asyncio.wait_for(asyncio.shield(self._final), timeout_s)
            return {"type": "final", "text": text, "audio_ms": self.samples_in * 1000 // IN_RATE,
                    "elapsed_ms": int((time.perf_counter() - self.started) * 1000)}
        except (TimeoutError, WebSocketException) as exc:
            raise WorkerError("ASR_FAILED") from exc
        finally:
            await self.close()

    async def cancel(self) -> None:
        if not self._closed:
            try:
                await self.ws.send(json.dumps({"type": "input_audio_buffer.clear"}))
            except WebSocketException:
                pass
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._reader.cancel()
        try:
            await self.ws.close()
        except WebSocketException:
            pass
        if not self._final.done():
            self._final.cancel()
        elif not self._final.cancelled():
            self._final.exception()  # mark retrieved: a failure nobody awaited is not an "unhandled" error
