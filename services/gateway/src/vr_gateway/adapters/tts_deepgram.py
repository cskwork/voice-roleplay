"""TTS adapter for API mode: Deepgram Aura-2, PROTOCOL §4.1.

Same interface as `workers.TtsClient`: `health()`, `synthesize_wav(voice_id, text, speed)` and `stream_session()`
returning a stream whose `synthesize(request_id, voice_id, text, speed)` yields ("start", rate), ("audio", bytes)...,
then ("done", info) | ("cancelled", None); plus `cancel(request_id)` and `close()`.

- Streaming: one WebSocket per realtime session (`wss://api.deepgram.com/v1/speak`), PCM16 (`linear16`) 24 kHz.
  Voice and speed are fixed per connection on Deepgram, so the connection is reopened when either changes (the
  learner toggles "slow"). Each request is `Speak` + `Flush`; `Flushed` ends it.
- Cancel sends `Clear` and waits for `Cleared` (or drops the connection after a short wait), so no stale audio can
  reach the next request; after `cancel` the generator yields no further audio frames.
- Every request carries `mip_opt_out=true` (no Deepgram model-improvement retention) and `speed` clamped to 0.7-1.5
  (Aura-2's range).
- Voice ids come from `config/voices.api.json` (`voices[].voice_id -> model`, plus `aliases` so scenarios that name a
  local voice still get a hosted one).

Never logs synthesized text (PROTOCOL §2); the key goes only into the Authorization header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from ..audio import encode_wav
from ..workers import WorkerError

log = logging.getLogger("vr_gateway.adapters.tts_deepgram")

SPEED_MIN, SPEED_MAX = 0.7, 1.5
SAMPLE_RATE = 24000
CLEAR_WAIT_S = 2.0  # longest wait for `Cleared` after a cancel before the connection is dropped
IDLE_TIMEOUT_S = 20.0  # no message at all for this long during a request = failure


def clamp_speed(speed: float) -> float:
    return round(min(SPEED_MAX, max(SPEED_MIN, float(speed))), 2)


def load_voices(path: Path) -> tuple[list[dict], dict[str, str], int]:
    data = json.loads(Path(path).read_text())
    voices = [dict(v) for v in data["voices"]]
    if not voices:
        raise ValueError(f"no voices in {path}")
    return voices, dict(data.get("aliases") or {}), int(data.get("sample_rate", SAMPLE_RATE))


class DeepgramTts:
    def __init__(self, api_key: str, voices_path: Path, *, ws_url: str = "wss://api.deepgram.com/v1/speak",
                 rest_url: str = "https://api.deepgram.com/v1/speak", timeout_s: float = 30.0):
        self._key = api_key
        self.voices, self.aliases, self.sample_rate = load_voices(voices_path)
        self._models = {v["voice_id"]: v["model"] for v in self.voices}
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0),
                                      headers={"Authorization": f"Token {api_key}"})

    async def aclose(self) -> None:
        await self.http.aclose()

    def model_for(self, voice_id: str) -> str:
        """Hosted model for a voice id (or an alias of one); unknown ids fall back to the first voice."""
        voice_id = self.aliases.get(voice_id, voice_id)
        return self._models.get(voice_id) or self.voices[0]["model"]

    def params(self, voice_id: str, speed: float, **extra) -> dict:
        return {"model": self.model_for(voice_id), "encoding": "linear16", "sample_rate": str(self.sample_rate),
                "speed": str(clamp_speed(speed)), "mip_opt_out": "true", **extra}

    async def health(self) -> dict:
        """No network call (no cost): ready when a key is configured. Same shape as the local worker (PROTOCOL §4)."""
        return {"ready": bool(self._key), "model_id": "deepgram/aura-2", "revision": "api", "device": "api",
                "sample_rate": self.sample_rate, "provider": "deepgram",
                "voices": [{"voice_id": v["voice_id"], "label": v["label"], "license_note": v.get("license_note", "")}
                           for v in self.voices]}

    async def synthesize_wav(self, voice_id: str, text: str, speed: float = 1.0) -> bytes:
        """One sentence as a WAV file (model sentences, cache warm-up). Raw PCM is requested and wrapped locally."""
        if not self._key:
            raise WorkerError("MODEL_NOT_READY")
        try:
            resp = await self.http.post(self.rest_url, params=self.params(voice_id, speed, container="none"),
                                        json={"text": text})
        except httpx.HTTPError as exc:
            raise WorkerError("WORKER_FAILED") from exc
        if resp.status_code != 200:
            log.warning("tts_rest_failed status=%d", resp.status_code)
            raise WorkerError("WORKER_FAILED")
        return encode_wav(resp.content, self.sample_rate)

    def stream_session(self) -> DeepgramTtsStream:
        return DeepgramTtsStream(self)


class DeepgramTtsStream:
    """Sequential synthesize requests on one Deepgram WebSocket (reopened when voice or speed changes)."""

    def __init__(self, owner: DeepgramTts):
        self.owner = owner
        self.ws: ClientConnection | None = None
        self._ws_key: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        self._current: str | None = None
        self._cancelled: set[str] = set()
        self._wake = asyncio.Event()
        # After a Clear, a `Flushed` for the cleared request may still arrive after `Cleared`; the next request
        # ignores one `Flushed` that comes before any of its own audio.
        self._stale_flushed = False

    async def _conn(self, voice_id: str, speed: float) -> ClientConnection:
        key = (self.owner.model_for(voice_id), clamp_speed(speed))
        if self.ws is not None and self._ws_key != key:
            await self.close()
        if self.ws is None:
            self._stale_flushed = False
            url = f"{self.owner.ws_url}?{urlencode(self.owner.params(voice_id, speed))}"
            self.ws = await asyncio.wait_for(
                connect(url, additional_headers={"Authorization": f"Token {self.owner._key}"}, max_size=2**22), 10)
            self._ws_key = key
        return self.ws

    async def _recv(self, ws: ClientConnection, timeout_s: float):
        """Next message, or None when `cancel()` woke us up first."""
        recv = asyncio.ensure_future(ws.recv())
        wake = asyncio.ensure_future(self._wake.wait())
        try:
            done, _ = await asyncio.wait({recv, wake}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        finally:
            wake.cancel()
        if recv in done:
            return recv.result()
        recv.cancel()
        try:
            await recv
        except (asyncio.CancelledError, WebSocketException):
            pass
        if not done:
            raise TimeoutError
        return None

    async def synthesize(self, request_id: str, voice_id: str, text: str,
                         speed: float = 1.0) -> AsyncIterator[tuple[str, object]]:
        """Yield ("start", sample_rate), ("audio", bytes)..., then ("done", info) | ("cancelled", None).

        Raises WorkerError("TTS_FAILED") on provider errors or connection loss (the connection is then dropped)."""
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
            try:
                ws = await self._send_request(voice_id, text, speed)
                yield "start", self.owner.sample_rate
                clearing = False
                flushed_seen = False
                deadline = None
                while True:
                    if request_id in self._cancelled and not clearing:
                        clearing = True
                        deadline = time.monotonic() + CLEAR_WAIT_S
                        await ws.send(json.dumps({"type": "Clear"}))
                    timeout = max(0.0, deadline - time.monotonic()) if clearing else IDLE_TIMEOUT_S
                    try:
                        msg = await self._recv(ws, timeout)
                    except TimeoutError:
                        if clearing:  # no `Cleared` in time: drop the connection so nothing stale can follow
                            await self.close()
                            yield "cancelled", None
                            return
                        raise
                    if msg is None:
                        self._wake.clear()
                        continue
                    if isinstance(msg, bytes):
                        if clearing or request_id in self._cancelled:
                            continue  # never forward audio after cancel
                        self._stale_flushed = False
                        if first_chunk_ms is None:
                            first_chunk_ms = int((time.perf_counter() - started) * 1000)
                        audio_bytes += len(msg)
                        yield "audio", msg
                        continue
                    data = json.loads(msg)
                    kind = data.get("type")
                    if kind == "Flushed" and clearing:
                        flushed_seen = True  # the flush finished before the clear landed; `Cleared` follows
                        continue
                    if kind == "Cleared" and clearing:
                        self._stale_flushed = not flushed_seen
                        yield "cancelled", None
                        return
                    if kind == "Flushed" and audio_bytes == 0 and self._stale_flushed:
                        self._stale_flushed = False  # belongs to the previous, cleared request
                        continue
                    if kind == "Flushed":
                        yield "done", {"request_id": request_id, "audio_ms": audio_bytes * 1000 // (2 * self.owner.sample_rate),
                                       "elapsed_ms": int((time.perf_counter() - started) * 1000),
                                       "first_chunk_ms": first_chunk_ms}
                        return
                    if kind == "Warning":
                        log.warning("tts_provider_warning code=%s", data.get("code"))
                    # Metadata and anything else: ignore.
            except (OSError, WebSocketException, ValueError, TimeoutError) as exc:
                log.warning("tts_stream_failed error=%s", type(exc).__name__)
                await self.close()
                raise WorkerError("TTS_FAILED") from exc
            except WorkerError:
                raise
            except BaseException:
                # Closed or cancelled mid-request: the connection may still carry this request's audio, drop it.
                await self.close()
                raise
            finally:
                self._current = None
                self._cancelled.discard(request_id)

    async def _send_request(self, voice_id: str, text: str, speed: float) -> ClientConnection:
        """Speak + Flush; a connection the provider closed while idle is reopened once."""
        for attempt in (1, 2):
            ws = await self._conn(voice_id, speed)
            try:
                await ws.send(json.dumps({"type": "Speak", "text": text}))
                await ws.send(json.dumps({"type": "Flush"}))
                return ws
            except WebSocketException:
                await self.close()
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)
        if self._current == request_id:
            self._wake.set()

    async def close(self) -> None:
        ws, self.ws, self._ws_key = self.ws, None, None
        if ws is not None:
            try:
                await ws.close()
            except WebSocketException:
                pass
