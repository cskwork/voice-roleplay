"""API-mode speech through OpenRouter (`OpenRouterTts`) against a FAKE `/audio/speech` server (no network, no keys).

FakeOpenRouterSpeech is a local HTTP/1.1 server that answers the documented request shape (OpenAI Speech API: JSON
body in, raw audio bytes out) with a chunked body, so streaming, cancellation, the speed retry, WAV header stripping
and error mapping can be checked deterministically. It also records when the client closed the connection.
It is a test double, not the provider: the real endpoint's sample rate, header and pacing are unverified here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
import wave
from io import BytesIO

import numpy as np
import pytest
from conftest import free_port, tone

from vr_gateway.adapters.tts_openrouter import OpenRouterTts, PcmBody, clamp_speed
from vr_gateway.config import REPO_ROOT
from vr_gateway.workers import WorkerError

VOICES = REPO_ROOT / "config/voices.openrouter.json"
KEY = "or-test-KEY-123"


def wav_header(rate: int, *, data_size: int = 0xFFFFFFFF, extra_chunk: bool = True) -> bytes:
    """A streamed-WAV preamble: RIFF/WAVE, an optional LIST chunk (odd size, padded), fmt, data with a placeholder size."""
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    out = b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
    if extra_chunk:
        out += b"LIST" + struct.pack("<I", 5) + b"INFOx" + b"\0"
    return out + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_size)


class FakeOpenRouterSpeech:
    """FAKE: `POST /api/v1/audio/speech` -> `chunks` pieces of 100 ms PCM16 24 kHz tone, `delay` apart (chunked).

    `body`/`piece` replace the tones with given bytes cut into `piece`-byte parts; `status` != 200 or
    `reject_speed` (400 when the request carries `speed`) answer a JSON error; `header_delay` holds the headers back."""

    def __init__(self):
        self.port = free_port()
        self.chunks = 5
        self.delay = 0.01
        self.status = 200
        self.reject_speed = False
        self.header_delay = 0.0
        self.body: bytes | None = None
        self.piece = 1000
        self.requests: list[dict] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v1"

    def pieces(self) -> list[bytes]:
        if self.body is None:
            return [tone(100, rate=24000).tobytes()] * self.chunks
        return [self.body[i : i + self.piece] for i in range(0, len(self.body), self.piece)]

    async def handler(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req: dict = {"closed": asyncio.Event(), "sent": 0}
        watch = None
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            lines = head.split("\r\n")
            req["method"], req["path"], _ = lines[0].split(" ")
            req["headers"] = {k.strip().lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ln)}
            req["json"] = json.loads(await reader.readexactly(int(req["headers"].get("content-length", 0))))
            self.requests.append(req)
            watch = asyncio.create_task(self._watch(reader, req))
            if self.header_delay:
                try:
                    await asyncio.wait_for(req["closed"].wait(), self.header_delay)
                    return  # the client gave up before the headers
                except TimeoutError:
                    pass
            status = 400 if self.reject_speed and "speed" in req["json"] else self.status
            if status != 200:
                msg = json.dumps({"error": {"code": status, "message": "fake provider error"}}).encode()
                writer.write(f"HTTP/1.1 {status} Error\r\nContent-Type: application/json\r\nContent-Length: {len(msg)}"
                             "\r\nConnection: close\r\n\r\n".encode() + msg)
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/pcm\r\nTransfer-Encoding: chunked\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            for piece in self.pieces():
                await asyncio.sleep(self.delay)
                if req["closed"].is_set():
                    return
                writer.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                await writer.drain()
                req["sent"] += 1
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if watch is not None:
                watch.cancel()
            writer.close()

    @staticmethod
    async def _watch(reader: asyncio.StreamReader, req: dict) -> None:
        """The client sends nothing after its request, so end of stream means it closed the connection."""
        try:
            await reader.read()
        except ConnectionError:
            pass
        req["closed"].set()


@pytest.fixture
async def speech_fake():
    fake = FakeOpenRouterSpeech()
    server = await asyncio.start_server(fake.handler, "127.0.0.1", fake.port)
    async with server:
        yield fake


def tts_for(fake: FakeOpenRouterSpeech, key: str = KEY) -> OpenRouterTts:
    return OpenRouterTts(key, VOICES, base_url=fake.base_url)


async def collect(gen) -> list[tuple[str, object]]:
    return [item async for item in gen]


async def test_stream_request_body_headers_and_frames(speech_fake):
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    events = await collect(stream.synthesize("r1", "libritts_r_4992_f", "Hello there.", 1.0))
    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and events[0][1] == 24000 and kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"audio"}
    audio = b"".join(v for k, v in events if k == "audio")
    assert audio == tone(100, rate=24000).tobytes() * 5
    info = events[-1][1]
    assert info["request_id"] == "r1" and info["audio_ms"] == 500
    assert info["first_chunk_ms"] is not None and info["elapsed_ms"] >= info["first_chunk_ms"]
    req = speech_fake.requests[0]
    assert req["method"] == "POST" and req["path"] == "/api/v1/audio/speech"
    assert req["headers"]["authorization"] == f"Bearer {KEY}"
    assert req["headers"]["x-openrouter-title"] == "voice-roleplay"
    # The scenario's local voice id maps to a hosted voice; no `speed` at the default; zero data retention.
    assert req["json"] == {"model": "deepgram/aura-2", "input": "Hello there.", "voice": "aura-2-thalia-en",
                           "response_format": "pcm", "provider": {"zdr": True, "data_collection": "deny"}}
    # Sequential requests on one stream.
    assert [k for k, _ in await collect(stream.synthesize("r2", "aura2_apollo_m", "Again.", 1.0))][-1] == "done"
    assert speech_fake.requests[1]["json"]["voice"] == "aura-2-apollo-en"
    await stream.close()
    await tts.aclose()


async def test_speed_is_clamped_and_sent_only_when_not_default(speech_fake):
    assert clamp_speed(0.5) == 0.7 and clamp_speed(2.0) == 1.5 and clamp_speed(0.85) == 0.85
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    for rid, speed in (("r1", 0.85), ("r2", 0.5), ("r3", 1.0), ("r4", 2.0)):
        await collect(stream.synthesize(rid, "aura2_apollo_m", "One.", speed))
    assert [r["json"].get("speed") for r in speech_fake.requests] == [0.85, 0.7, None, 1.5]
    assert all(r["json"]["voice"] == "aura-2-apollo-en" for r in speech_fake.requests)
    await tts.aclose()


async def test_cancel_mid_stream_closes_the_response_and_next_request_is_clean(speech_fake):
    speech_fake.chunks, speech_fake.delay = 50, 0.02
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    seen: list[str] = []
    cancelled = False
    async for kind, _ in stream.synthesize("r1", "aura2_thalia_f", "A long sentence.", 1.0):
        if cancelled and kind == "audio":
            raise AssertionError("audio after cancel")
        seen.append(kind)
        if seen.count("audio") == 2:
            await stream.cancel("r1")
            cancelled = True
    assert seen == ["start", "audio", "audio", "cancelled"]
    first = speech_fake.requests[0]
    await asyncio.wait_for(first["closed"].wait(), 1.0)  # the HTTP response was closed promptly
    assert first["sent"] < 10
    speech_fake.chunks = 3
    events = await collect(stream.synthesize("r2", "aura2_thalia_f", "Next.", 1.0))
    assert [k for k, _ in events] == ["start", "audio", "audio", "audio", "done"]
    await stream.close()
    await tts.aclose()


async def test_cancel_from_another_task_while_waiting_for_audio(speech_fake):
    """The barge-in path: the realtime engine cancels from another task while the stream waits for the next chunk."""
    speech_fake.chunks, speech_fake.delay = 20, 0.3
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    kinds: list[str] = []

    async def consume():
        async for kind, _ in stream.synthesize("r1", "aura2_thalia_f", "Slow provider.", 1.0):
            kinds.append(kind)

    task = asyncio.create_task(consume())
    while "audio" not in kinds:
        await asyncio.sleep(0.01)
    started = time.monotonic()
    await stream.cancel("r1")
    await asyncio.wait_for(task, 1.0)
    assert time.monotonic() - started < 0.25  # did not wait for the provider's next chunk
    assert kinds == ["start", "audio", "cancelled"]
    await asyncio.wait_for(speech_fake.requests[0]["closed"].wait(), 1.0)
    await tts.aclose()


async def test_cancel_while_queued_or_before_headers(speech_fake):
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    await stream.cancel("r9")
    assert await collect(stream.synthesize("r9", "aura2_thalia_f", "Never.", 1.0)) == [("cancelled", None)]
    assert speech_fake.requests == []  # nothing was sent
    # Cancelled while the provider has not answered yet: the request is abandoned at once.
    speech_fake.header_delay = 5.0
    task = asyncio.create_task(collect(stream.synthesize("r10", "aura2_thalia_f", "Slow.", 1.0)))
    while not speech_fake.requests:
        await asyncio.sleep(0.01)
    await stream.cancel("r10")
    assert await asyncio.wait_for(task, 1.0) == [("cancelled", None)]
    await asyncio.wait_for(speech_fake.requests[0]["closed"].wait(), 1.0)
    await tts.aclose()


async def test_400_with_speed_is_retried_without_it_once(speech_fake, caplog):
    caplog.set_level(logging.DEBUG)
    speech_fake.reject_speed = True
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    events = await collect(stream.synthesize("r1", "aura2_thalia_f", "Slowly please.", 0.85))
    assert [k for k, _ in events][-1] == "done"
    assert [r["json"].get("speed") for r in speech_fake.requests] == [0.85, None]
    assert tts.speed_supported is False
    # Remembered: the next slow request goes out without `speed` straight away.
    await collect(stream.synthesize("r2", "aura2_thalia_f", "Again slowly.", 0.85))
    assert len(speech_fake.requests) == 3 and "speed" not in speech_fake.requests[2]["json"]
    assert "tts_speed_unsupported" in caplog.text
    assert "Slowly please" not in caplog.text and KEY not in caplog.text
    await tts.aclose()


async def test_400_without_speed_and_other_errors_are_tts_failed(speech_fake):
    tts = tts_for(speech_fake)
    stream = tts.stream_session()
    for status in (400, 401, 500):
        speech_fake.status = status
        before = len(speech_fake.requests)
        with pytest.raises(WorkerError) as err:
            await collect(stream.synthesize(f"r{status}", "aura2_thalia_f", "Hi.", 1.0))
        assert err.value.code == "TTS_FAILED" and len(speech_fake.requests) == before + 1  # no retry
    unreachable = OpenRouterTts(KEY, VOICES, base_url=f"http://127.0.0.1:{free_port()}/api/v1")
    with pytest.raises(WorkerError) as err:
        await collect(unreachable.stream_session().synthesize("r1", "aura2_thalia_f", "Hi.", 1.0))
    assert err.value.code == "TTS_FAILED"
    await unreachable.aclose()
    await tts.aclose()


async def test_wav_header_is_stripped_and_frames_stay_sample_aligned(speech_fake):
    pcm = np.random.default_rng(7).integers(-8000, 8000, 22050, dtype=np.int16).tobytes()  # 1 s at 22.05 kHz
    speech_fake.delay = 0.0
    speech_fake.body, speech_fake.piece = wav_header(22050) + pcm, 777  # odd pieces, header split across them
    tts = tts_for(speech_fake)
    events = await collect(tts.stream_session().synthesize("r1", "aura2_thalia_f", "Header.", 1.0))
    assert events[0] == ("start", 22050)  # the header's rate wins over the voices file's 24 kHz
    frames = [v for k, v in events if k == "audio"]
    assert frames and all(len(f) % 2 == 0 for f in frames)
    assert b"".join(frames) == pcm and events[-1][1]["audio_ms"] == 1000
    # Raw PCM in odd pieces: same alignment, a dangling last byte is dropped.
    speech_fake.body, speech_fake.piece = pcm + b"\x01", 333
    events = await collect(tts.stream_session().synthesize("r2", "aura2_thalia_f", "Raw.", 1.0))
    assert events[0] == ("start", 24000)
    frames = [v for k, v in events if k == "audio"]
    assert all(len(f) % 2 == 0 for f in frames) and b"".join(frames) == pcm
    await tts.aclose()


def test_pcm_body_parser_byte_by_byte():
    pcm = bytes(range(256)) * 4
    body = PcmBody(24000)
    out = b"".join(body.feed(data[i : i + 1]) for data in [wav_header(16000, data_size=len(pcm)) + pcm]
                   for i in range(len(data)))
    assert body.wav and body.rate == 16000 and out + body.finish() == pcm
    truncated = PcmBody(24000)
    assert truncated.feed(wav_header(16000)[:20]) == b"" and not truncated.ready and truncated.finish() == b""
    stereo = PcmBody(24000)
    with pytest.raises(ValueError):
        stereo.feed(b"RIFF\0\0\0\0WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 24000, 96000, 4, 16))


async def test_synthesize_wav_and_health(speech_fake, caplog):
    caplog.set_level(logging.DEBUG)
    speech_fake.body = tone(200, rate=24000).tobytes()
    tts = tts_for(speech_fake)
    wav = await tts.synthesize_wav("libritts_r_1188_m", "Could I have it to go?", 0.85)
    with wave.open(BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 4800)
    assert speech_fake.requests[0]["json"] == {
        "model": "deepgram/aura-2", "input": "Could I have it to go?", "voice": "aura-2-apollo-en",
        "response_format": "pcm", "provider": {"zdr": True, "data_collection": "deny"}, "speed": 0.85}
    speech_fake.body = wav_header(22050, extra_chunk=False) + tone(100, rate=22050).tobytes()
    with wave.open(BytesIO(await tts.synthesize_wav("aura2_thalia_f", "Header.", 1.0))) as w:
        assert (w.getframerate(), w.getnframes()) == (22050, 2205)
    speech_fake.status = 500
    with pytest.raises(WorkerError) as err:
        await tts.synthesize_wav("aura2_thalia_f", "Failing sentence.", 1.0)
    assert err.value.code == "WORKER_FAILED"
    health = await tts.health()
    assert health["ready"] is True and health["provider"] == "openrouter"
    assert health["model_id"] == "openrouter/deepgram/aura-2" and health["sample_rate"] == 24000
    assert [v["voice_id"] for v in health["voices"]] == ["aura2_thalia_f", "aura2_apollo_m"]
    assert all("OpenRouter" in v["license_note"] for v in health["voices"])
    assert KEY not in json.dumps(health)
    # Never the synthesized text or the key in logs (PROTOCOL §2).
    for secret in (KEY, "Could I have it to go", "Failing sentence"):
        assert secret not in caplog.text
    await tts.aclose()


async def test_without_key_nothing_is_sent(speech_fake):
    tts = OpenRouterTts("", VOICES, base_url=speech_fake.base_url)
    assert (await tts.health())["ready"] is False
    with pytest.raises(WorkerError) as err:
        await tts.synthesize_wav("aura2_thalia_f", "Hi.")
    assert err.value.code == "MODEL_NOT_READY"
    with pytest.raises(WorkerError) as err:
        await collect(tts.stream_session().synthesize("r1", "aura2_thalia_f", "Hi.", 1.0))
    assert err.value.code == "TTS_FAILED" and speech_fake.requests == []
    await tts.aclose()
