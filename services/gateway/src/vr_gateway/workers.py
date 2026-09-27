"""Clients for the ASR (PROTOCOL §3) and TTS (PROTOCOL §4) workers."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

log = logging.getLogger("vr_gateway.workers")


class WorkerError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


PASSTHROUGH_CODES = {"OUT_OF_MEMORY", "MODEL_NOT_READY", "AUDIO_TOO_LONG"}


def _error_code(resp: httpx.Response) -> str:
    try:
        code = resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        code = None
    return code if code in PASSTHROUGH_CODES else "WORKER_FAILED"


def _ws_url(base: str, path: str) -> str:
    return base.replace("http://", "ws://", 1).rstrip("/") + path


async def get_health(http: httpx.AsyncClient, url: str) -> dict | None:
    try:
        resp = await http.get(url, timeout=1.5)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


class AsrClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Worker-Token": token}
        self.http = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=httpx.Timeout(120, connect=3))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def health(self) -> dict | None:
        return await get_health(self.http, "/health")

    async def transcribe(self, pcm16: bytes, context: str | None = None) -> dict:
        params = {"language": "English"}
        if context:
            params["context"] = context[:300]
        try:
            resp = await self.http.post(
                "/transcribe", content=pcm16, params=params, headers={"Content-Type": "application/octet-stream"}
            )
        except httpx.HTTPError as exc:
            raise WorkerError("WORKER_FAILED") from exc
        if resp.status_code != 200:
            raise WorkerError(_error_code(resp))
        return resp.json()

    async def open_stream(self, on_partial: Callable[[str], None]) -> AsrStream:
        try:
            ws = await connect(_ws_url(self.base_url, "/stream"), additional_headers=self.headers, max_size=2**20)
        except (OSError, WebSocketException) as exc:
            raise WorkerError("ASR_FAILED") from exc
        return AsrStream(ws, on_partial)


class AsrStream:
    """One utterance. Partials are delivered through the callback; commit() returns the final dict."""

    def __init__(self, ws: ClientConnection, on_partial: Callable[[str], None]):
        self.ws = ws
        self.on_partial = on_partial
        self._final: asyncio.Future = asyncio.get_running_loop().create_future()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    continue
                data = json.loads(msg)
                kind = data.get("type")
                if kind == "partial":
                    self.on_partial(data.get("text", ""))
                elif kind == "final":
                    if not self._final.done():
                        self._final.set_result(data)
                    return
                elif kind == "error":
                    if not self._final.done():
                        self._final.set_exception(WorkerError(data.get("code") or "ASR_FAILED"))
                    return
        except (WebSocketException, ValueError, OSError):
            pass
        if not self._final.done():
            self._final.set_exception(WorkerError("ASR_FAILED"))

    async def send_audio(self, pcm16: bytes) -> None:
        try:
            await self.ws.send(pcm16)
        except WebSocketException as exc:
            raise WorkerError("ASR_FAILED") from exc

    async def commit(self, timeout_s: float = 30.0) -> dict:
        try:
            await self.ws.send(json.dumps({"type": "commit"}))
            return await asyncio.wait_for(asyncio.shield(self._final), timeout_s)
        except (TimeoutError, WebSocketException) as exc:
            raise WorkerError("ASR_FAILED") from exc
        finally:
            await self.close()

    async def cancel(self) -> None:
        try:
            await self.ws.send(json.dumps({"type": "cancel"}))
        except WebSocketException:
            pass
        await self.close()

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self.ws.close()
        except WebSocketException:
            pass
        if not self._final.done():
            self._final.cancel()


class TtsClient:
    """HTTP for whole-sentence WAV; one lazily opened WS per realtime session for streaming."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Worker-Token": token}
        self.http = httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=httpx.Timeout(120, connect=3))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def health(self) -> dict | None:
        return await get_health(self.http, "/health")

    async def synthesize_wav(self, voice_id: str, text: str, speed: float = 1.0) -> bytes:
        try:
            resp = await self.http.post("/synthesize", json={"voice_id": voice_id, "text": text, "speed": speed})
        except httpx.HTTPError as exc:
            raise WorkerError("WORKER_FAILED") from exc
        if resp.status_code != 200:
            raise WorkerError(_error_code(resp))
        return resp.content

    def stream_session(self) -> TtsStream:
        return TtsStream(_ws_url(self.base_url, "/synthesize"), self.headers)


class TtsStream:
    """Sequential synthesize requests on one WS connection (PROTOCOL §4)."""

    def __init__(self, url: str, headers: dict):
        self.url = url
        self.headers = headers
        self.ws: ClientConnection | None = None
        self._lock = asyncio.Lock()

    async def _conn(self) -> ClientConnection:
        if self.ws is None:
            self.ws = await connect(self.url, additional_headers=self.headers, max_size=2**22)
        return self.ws

    async def synthesize(
        self, request_id: str, voice_id: str, text: str, speed: float = 1.0
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("start", sample_rate), ("audio", bytes)..., then ("done", info) | ("cancelled", None).

        Raises WorkerError on error/connection loss (the connection is then dropped)."""
        async with self._lock:
            try:
                ws = await self._conn()
                await ws.send(json.dumps(
                    {"type": "synthesize", "request_id": request_id, "voice_id": voice_id, "text": text, "speed": speed}
                ))
                current = None
                async for msg in ws:
                    if isinstance(msg, bytes):
                        if current == request_id:
                            yield "audio", msg
                        continue
                    data = json.loads(msg)
                    if data.get("request_id") != request_id:
                        continue
                    kind = data.get("type")
                    if kind == "start":
                        current = request_id
                        yield "start", int(data["sample_rate"])
                    elif kind == "done":
                        yield "done", data
                        return
                    elif kind == "cancelled":
                        yield "cancelled", None
                        return
                    elif kind == "error":
                        raise WorkerError(data.get("code") or "TTS_FAILED")
                raise WorkerError("TTS_FAILED")
            except (OSError, WebSocketException, ValueError) as exc:
                await self.close()
                raise WorkerError("TTS_FAILED") from exc
            except WorkerError:
                raise
            except BaseException:
                # Cancelled mid-request: the connection state is unknown, drop it.
                await self.close()
                raise

    async def cancel(self, request_id: str) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "cancel", "request_id": request_id}))
        except WebSocketException:
            pass

    async def close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            try:
                await ws.close()
            except WebSocketException:
                pass


def elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
