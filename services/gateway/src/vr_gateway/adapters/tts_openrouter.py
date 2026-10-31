"""TTS adapter for API mode: Deepgram Aura-2 through OpenRouter's speech endpoint, PROTOCOL §4.1.

Same interface as `workers.TtsClient` and `DeepgramTts`: `health()`, `synthesize_wav(voice_id, text, speed)` and
`stream_session()` returning a stream whose `synthesize(request_id, voice_id, text, speed)` yields ("start", rate),
("audio", bytes)..., then ("done", info) | ("cancelled", None); plus `cancel(request_id)` and `close()`.

- One `POST {base_url}/audio/speech` per sentence (OpenAI Speech API shape): `{model, input, voice,
  response_format: "pcm", provider: {zdr: true, data_collection: "deny"}}`, plus `speed` only when it is not 1.0. The
  body is read as it arrives and forwarded as PCM16 mono frames.
- OpenRouter documents "pcm" as 16-bit little-endian but not its sample rate per provider. The voices file's
  `sample_rate` (24 kHz, Deepgram's linear16 default) is assumed; a body that starts with a WAV header has the header
  stripped and its `fmt ` rate used instead. Frames keep 16-bit alignment across network chunks.
- `speed` is clamped to 0.7-1.5 (Aura-2's range). OpenRouter says providers without speed support may answer 400 to a
  non-default value: such a request is retried once without `speed`, and this client stops sending it.
- Cancel closes the HTTP response (or abandons the request while it waits for headers); after `cancel` the generator
  yields no further audio frames. A request cancelled while queued is never sent.
- Voice ids come from `config/voices.openrouter.json` (`voices[].voice_id -> voice`, plus `aliases` so scenarios that
  name a local voice still get a hosted one).

Never logs synthesized text (PROTOCOL §2); the key goes only into the Authorization header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path

import httpx

from ..audio import encode_wav
from ..workers import WorkerError

log = logging.getLogger("vr_gateway.adapters.tts_openrouter")

SPEED_MIN, SPEED_MAX = 0.7, 1.5
SAMPLE_RATE = 24000
DEFAULT_MODEL = "deepgram/aura-2"
OPENROUTER_URL = "https://openrouter.ai/api/v1"
WAV_HEADER_MAX = 64 * 1024  # longest RIFF preamble accepted before the `data` chunk
_WOKEN = object()  # `_race` result when `cancel()` came first
_END = object()  # end of the response body


def clamp_speed(speed: float) -> float:
    return round(min(SPEED_MAX, max(SPEED_MIN, float(speed))), 2)


def load_voices(path: Path) -> tuple[list[dict], dict[str, str], int, str | None]:
    data = json.loads(Path(path).read_text())
    voices = [dict(v) for v in data["voices"]]
    if not voices:
        raise ValueError(f"no voices in {path}")
    return voices, dict(data.get("aliases") or {}), int(data.get("sample_rate", SAMPLE_RATE)), data.get("model")


class PcmBody:
    """Turns a response body into aligned PCM16: strips a leading WAV header, carries an odd trailing byte.

    `feed(chunk)` returns the PCM that is ready (possibly b""); `ready` turns true once it is known whether a WAV
    header is present (and `rate` is final); `finish()` returns what is left at the end of the body."""

    def __init__(self, default_rate: int):
        self.rate = default_rate
        self.ready = False
        self.wav = False
        self._head = b""
        self._carry = b""

    def feed(self, chunk: bytes) -> bytes:
        if not self.ready:
            self._head += chunk
            payload = self._parse_head()
            if payload is None:
                return b""
            chunk = payload
        data = self._carry + chunk
        cut = len(data) & ~1
        self._carry = data[cut:]
        return data[:cut]

    def finish(self) -> bytes:
        """End of body: an incomplete WAV header yields no audio; an odd last byte is dropped."""
        # Not ready here means the body ended inside a WAV header (raw PCM is ready from its first byte).
        self.ready = True
        self._head = self._carry = b""
        return b""

    def _parse_head(self) -> bytes | None:
        """PCM after the header once known, or None while more bytes are needed."""
        head = self._head
        if head[:4] != b"RIFF"[: len(head[:4])]:  # not a WAV header: raw PCM from the first byte
            return self._done(head)
        if len(head) < 12:
            return None
        if head[8:12] != b"WAVE":
            return self._done(head)
        off = 12
        while True:
            if len(head) < off + 8:
                break
            cid, size = head[off : off + 4], struct.unpack_from("<I", head, off + 4)[0]
            if cid == b"data":  # streamed WAVs may carry a placeholder size: everything after it is audio
                self.wav = True
                return self._done(head[off + 8 :])
            if len(head) < off + 8 + size:
                break
            if cid == b"fmt ":
                if size < 16:
                    raise ValueError("short WAV fmt chunk")
                fmt_tag, channels, rate = struct.unpack_from("<HHI", head, off + 8)
                bits = struct.unpack_from("<H", head, off + 22)[0]
                if fmt_tag not in (1, 0xFFFE) or channels != 1 or bits != 16:
                    log.warning("tts_unexpected_format tag=%d channels=%d bits=%d", fmt_tag, channels, bits)
                    raise ValueError("unsupported WAV format")
                self.rate = int(rate)
            off += 8 + size + (size & 1)
        if len(head) > WAV_HEADER_MAX:
            raise ValueError("WAV header too long")
        return None

    def _done(self, payload: bytes) -> bytes:
        self.ready = True
        self._head = b""
        return payload


class OpenRouterTts:
    def __init__(self, api_key: str, voices_path: Path, *, base_url: str = OPENROUTER_URL, model: str | None = None,
                 timeout_s: float = 30.0):
        self._key = api_key
        self.voices, self.aliases, self.sample_rate, file_model = load_voices(voices_path)
        self.model = model or file_model or DEFAULT_MODEL
        self._voice_names = {v["voice_id"]: v["voice"] for v in self.voices}
        self.url = f"{base_url.rstrip('/')}/audio/speech"
        self.speed_supported = True  # turned off after a 400 to a request that carried `speed`
        # Read timeout = longest silence between body chunks before the request counts as failed.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0),
                                      headers={"Authorization": f"Bearer {api_key}",
                                               "X-OpenRouter-Title": "voice-roleplay"})

    async def aclose(self) -> None:
        await self.http.aclose()

    def voice_for(self, voice_id: str) -> str:
        """Provider voice for a voice id (or an alias of one); unknown ids fall back to the first voice."""
        voice_id = self.aliases.get(voice_id, voice_id)
        return self._voice_names.get(voice_id) or self.voices[0]["voice"]

    def body(self, voice_id: str, text: str, speed: float) -> dict:
        body = {"model": self.model, "input": text, "voice": self.voice_for(voice_id), "response_format": "pcm",
                "provider": {"zdr": True, "data_collection": "deny"}}
        speed = clamp_speed(speed)
        if speed != 1.0 and self.speed_supported:
            body["speed"] = speed
        return body

    def speed_rejected(self) -> None:
        if self.speed_supported:
            self.speed_supported = False
            log.warning("tts_speed_unsupported model=%s status=400; retrying without speed", self.model)

    async def health(self) -> dict:
        """No network call (no cost): ready when a key is configured. Same shape as the local worker (PROTOCOL §4)."""
        return {"ready": bool(self._key), "model_id": f"openrouter/{self.model}", "revision": "api", "device": "api",
                "sample_rate": self.sample_rate, "provider": "openrouter",
                "voices": [{"voice_id": v["voice_id"], "label": v["label"], "license_note": v.get("license_note", "")}
                           for v in self.voices]}

    async def synthesize_wav(self, voice_id: str, text: str, speed: float = 1.0) -> bytes:
        """One sentence as a WAV file (model sentences, cache warm-up). Raw PCM is requested and wrapped locally."""
        if not self._key:
            raise WorkerError("MODEL_NOT_READY")
        for attempt in (1, 2):
            body = self.body(voice_id, text, speed)
            try:
                resp = await self.http.post(self.url, json=body)
            except httpx.HTTPError as exc:
                log.warning("tts_rest_failed error=%s", type(exc).__name__)
                raise WorkerError("WORKER_FAILED") from exc
            if resp.status_code == 400 and "speed" in body and attempt == 1:
                self.speed_rejected()
                continue
            if resp.status_code != 200:
                log.warning("tts_rest_failed status=%d", resp.status_code)
                raise WorkerError("WORKER_FAILED")
            pcm = PcmBody(self.sample_rate)
            try:
                audio = pcm.feed(resp.content) + pcm.finish()
            except ValueError as exc:
                raise WorkerError("WORKER_FAILED") from exc
            return encode_wav(audio, pcm.rate)
        raise AssertionError("unreachable")

    def stream_session(self) -> OpenRouterTtsStream:
        return OpenRouterTtsStream(self)


class OpenRouterTtsStream:
    """Sequential synthesize requests for one realtime session (one HTTP request each, at most one at a time)."""

    def __init__(self, owner: OpenRouterTts):
        self.owner = owner
        self._lock = asyncio.Lock()
        self._current: str | None = None
        self._cancelled: set[str] = set()
        self._wake = asyncio.Event()

    async def _race(self, aw: Awaitable):
        """Result of `aw`, `_END` when it is an exhausted iterator step, or `_WOKEN` when `cancel()` came first
        (`aw` is then cancelled)."""
        task = asyncio.ensure_future(aw)
        wake = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({task, wake}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wake.cancel()
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - the abandoned request's own outcome does not matter
                    pass
        if task.cancelled():
            return _WOKEN
        try:
            return task.result()
        except StopAsyncIteration:
            return _END

    async def _open(self, voice_id: str, text: str, speed: float) -> httpx.Response | None:
        """The 200 response with its body unread, or None when cancelled first. A 400 to a request that carried
        `speed` is retried once without it."""
        http = self.owner.http
        for attempt in (1, 2):
            body = self.owner.body(voice_id, text, speed)
            resp = await self._race(http.send(http.build_request("POST", self.owner.url, json=body), stream=True))
            if resp is _WOKEN:
                return None
            if resp.status_code == 200:
                return resp
            await resp.aclose()
            if resp.status_code == 400 and "speed" in body and attempt == 1:
                self.owner.speed_rejected()
                continue
            log.warning("tts_stream_failed status=%d", resp.status_code)
            raise WorkerError("TTS_FAILED")
        raise AssertionError("unreachable")

    async def synthesize(self, request_id: str, voice_id: str, text: str,
                         speed: float = 1.0) -> AsyncIterator[tuple[str, object]]:
        """Yield ("start", sample_rate), ("audio", bytes)..., then ("done", info) | ("cancelled", None).

        Raises WorkerError("TTS_FAILED") on provider errors, non-200 answers or connection loss."""
        async with self._lock:
            if request_id in self._cancelled:  # cancelled while queued: nothing was sent
                self._cancelled.discard(request_id)
                yield "cancelled", None
                return
            if not self.owner._key:
                raise WorkerError("TTS_FAILED")
            started = time.perf_counter()
            first_chunk_ms = None
            audio_bytes = 0
            self._current = request_id
            self._wake.clear()
            resp: httpx.Response | None = None
            chunks: AsyncIterator[bytes] | None = None
            try:
                resp = await self._open(voice_id, text, speed)
                if resp is None:
                    yield "cancelled", None
                    return
                pcm = PcmBody(self.owner.sample_rate)
                chunks = resp.aiter_bytes()
                start_sent = False
                ended = False
                while not ended:
                    if request_id in self._cancelled:
                        yield "cancelled", None
                        return
                    chunk = await self._race(anext(chunks))
                    if chunk is _WOKEN:
                        continue  # the loop head yields "cancelled"
                    if chunk is _END:
                        ended = True
                        audio = pcm.finish()
                    else:
                        audio = pcm.feed(chunk)
                    if pcm.ready and not start_sent:
                        start_sent = True
                        yield "start", pcm.rate
                    if audio and request_id not in self._cancelled:  # never forward audio after cancel
                        if first_chunk_ms is None:
                            first_chunk_ms = int((time.perf_counter() - started) * 1000)
                        audio_bytes += len(audio)
                        yield "audio", audio
                if request_id in self._cancelled:
                    yield "cancelled", None
                    return
                yield "done", {"request_id": request_id, "audio_ms": audio_bytes * 1000 // (2 * pcm.rate),
                               "elapsed_ms": int((time.perf_counter() - started) * 1000),
                               "first_chunk_ms": first_chunk_ms}
            except WorkerError:
                raise
            except (httpx.HTTPError, OSError, ValueError) as exc:
                log.warning("tts_stream_failed error=%s", type(exc).__name__)
                raise WorkerError("TTS_FAILED") from exc
            finally:
                self._current = None
                self._cancelled.discard(request_id)
                if chunks is not None:
                    await chunks.aclose()
                if resp is not None:
                    await resp.aclose()

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)
        if self._current == request_id:
            self._wake.set()

    async def close(self) -> None:
        """Nothing to keep open between requests; an in-flight request is closed by its own generator."""
