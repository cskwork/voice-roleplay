"""API-mode adapters against FAKE provider servers (no network, no keys).

FakeOpenAiRealtime and FakeDeepgram below are local WebSocket servers that speak the documented provider protocols
(OpenAI realtime transcription session events; Deepgram /v1/speak Speak/Flush/Clear). They are test doubles, not
providers: they script text and tones so event order, partial replacement, cancellation and error mapping can be
checked deterministically.
"""

from __future__ import annotations

import asyncio
import base64
import json
import wave
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import httpx
import numpy as np
import pytest
from conftest import free_port, tone
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from vr_gateway.adapters import tts_deepgram
from vr_gateway.adapters.asr_openai import OpenAiAsr
from vr_gateway.adapters.tts_deepgram import DeepgramTts, clamp_speed
from vr_gateway.config import REPO_ROOT
from vr_gateway.workers import WorkerError

pytestmark = pytest.mark.asyncio
VOICES = REPO_ROOT / "config/voices.api.json"


# ---------------------------------------------------------------- FAKE OpenAI realtime transcription server


async def safe_send(ws, data) -> None:
    """Fake servers keep reading buffered client messages even after the client hung up."""
    try:
        await ws.send(data)
    except ConnectionClosed:
        pass


class FakeOpenAiRealtime:
    """FAKE: one word of delta per 0.25 s of 24 kHz audio, the scripted transcript on commit."""

    def __init__(self):
        self.words = ["Can", "I", "get", "a", "latte"]
        self.transcript = "Can I get a latte, please?"
        self.fail_on = None  # "commit" -> error event, "close" -> drop the connection on commit, "update" -> error
        self.connections: list[dict] = []
        self.port = free_port()

    async def handler(self, ws):
        conn = {"headers": dict(ws.request.headers), "path": ws.request.path, "events": [], "samples": 0}
        self.connections.append(conn)
        sent = 0
        async for raw in ws:
            ev = json.loads(raw)
            conn["events"].append(ev["type"] if ev["type"] != "session.update" else ev)
            if ev["type"] == "session.update":
                if self.fail_on == "update":
                    await safe_send(ws, json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                                          "code": "invalid_value", "message": "x"}}))
                    continue
                await safe_send(ws, json.dumps({"type": "session.updated", "session": {"type": "transcription"}}))
            elif ev["type"] == "input_audio_buffer.append":
                conn["samples"] += len(base64.b64decode(ev["audio"])) // 2
                while sent < len(self.words) and conn["samples"] >= (sent + 1) * 6000:
                    await safe_send(ws, json.dumps({"type": "conversation.item.input_audio_transcription.delta",
                                              "item_id": "item_1", "content_index": 0,
                                              "delta": ("" if sent == 0 else " ") + self.words[sent]}))
                    sent += 1
            elif ev["type"] == "input_audio_buffer.commit":
                if self.fail_on == "commit":
                    await safe_send(ws, json.dumps({"type": "error", "error": {"type": "server_error", "code": None,
                                                                          "message": "boom"}}))
                    continue
                if self.fail_on == "close":
                    await ws.close()
                    return
                await safe_send(ws, json.dumps({"type": "input_audio_buffer.committed", "item_id": "item_1",
                                          "previous_item_id": None}))
                await safe_send(ws, json.dumps({"type": "conversation.item.input_audio_transcription.completed",
                                          "item_id": "item_1", "content_index": 0, "transcript": self.transcript}))
            elif ev["type"] == "input_audio_buffer.clear":
                await safe_send(ws, json.dumps({"type": "input_audio_buffer.cleared"}))


@pytest.fixture
async def openai_fake():
    fake = FakeOpenAiRealtime()
    async with serve(fake.handler, "127.0.0.1", fake.port, max_size=2**24):
        yield fake


def asr_for(fake: FakeOpenAiRealtime, key: str = "sk-test") -> OpenAiAsr:
    return OpenAiAsr(key, url=f"ws://127.0.0.1:{fake.port}/v1/realtime?intent=transcription")


async def test_asr_partials_replace_and_final(openai_fake):
    partials: list[str] = []
    stream = await asr_for(openai_fake).open_stream(partials.append)
    pcm = tone(1000).tobytes()  # 1 s at 16 kHz
    for i in range(0, len(pcm), 640):  # 20 ms frames, like the gateway
        await stream.send_audio(pcm[i : i + 640])
    final = await stream.commit()
    assert final["type"] == "final" and final["text"] == "Can I get a latte, please?" and final["audio_ms"] == 1000
    # Each partial is the whole text so far (PROTOCOL §3 "replaces previous partial"), growing word by word.
    assert partials[:3] == ["Can", "Can I", "Can I get"]
    assert all(b.startswith(a) for a, b in zip(partials, partials[1:]))
    conn = openai_fake.connections[0]
    assert conn["headers"]["authorization"] == "Bearer sk-test"
    assert conn["path"] == "/v1/realtime?intent=transcription"
    update = conn["events"][0]
    audio_in = update["session"]["audio"]["input"]
    assert update["session"]["type"] == "transcription"
    assert audio_in["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_in["turn_detection"] is None
    assert audio_in["transcription"] == {"model": "gpt-live-transcribe", "languages": ["en"]}
    assert abs(conn["samples"] - 24000) <= 48  # 16 kHz in, 24 kHz out
    assert conn["events"][-1] == "input_audio_buffer.commit"


async def test_asr_error_and_dropped_connection_map_to_asr_failed(openai_fake):
    for mode in ("commit", "close"):
        openai_fake.fail_on = mode
        stream = await asr_for(openai_fake).open_stream(lambda _t: None)
        await stream.send_audio(tone(300).tobytes())
        with pytest.raises(WorkerError) as err:
            await stream.commit(timeout_s=3)
        assert err.value.code == "ASR_FAILED"


async def test_asr_rejected_session_fails_the_stream(openai_fake):
    openai_fake.fail_on = "update"
    stream = await asr_for(openai_fake).open_stream(lambda _t: None)
    await asyncio.sleep(0.2)
    with pytest.raises(WorkerError) as err:
        await stream.send_audio(tone(100).tobytes())
    assert err.value.code == "ASR_FAILED"
    await stream.close()


async def test_asr_45_second_cap(openai_fake):
    stream = await asr_for(openai_fake).open_stream(lambda _t: None)
    second = np.zeros(16000, dtype=np.int16).tobytes()
    with pytest.raises(WorkerError) as err:
        for _ in range(47):
            await stream.send_audio(second)
    assert err.value.code == "ASR_FAILED" and stream.samples_in > 45 * 16000
    await stream.close()


async def test_asr_cancel_clears_and_closes(openai_fake):
    stream = await asr_for(openai_fake).open_stream(lambda _t: None)
    await stream.send_audio(tone(200).tobytes())
    await stream.cancel()
    await asyncio.sleep(0.1)
    assert openai_fake.connections[0]["events"][-1] == "input_audio_buffer.clear"


async def test_asr_empty_commit_needs_no_provider_call(openai_fake):
    stream = await asr_for(openai_fake).open_stream(lambda _t: None)
    final = await stream.commit()
    assert final["text"] == "" and "input_audio_buffer.commit" not in openai_fake.connections[0]["events"]


async def test_asr_transcribe_recorded_take(openai_fake):
    asr = asr_for(openai_fake)
    result = await asr.transcribe(tone(2500).tobytes(), context="Ordering at a cafe")
    assert result["text"] == "Can I get a latte, please?" and result["audio_ms"] == 2500
    assert result["language"] == "English"
    update = openai_fake.connections[0]["events"][0]
    assert update["session"]["audio"]["input"]["transcription"]["prompt"] == "Ordering at a cafe"
    with pytest.raises(WorkerError) as err:
        await asr.transcribe(b"\0\0" * (121 * 16000))
    assert err.value.code == "AUDIO_TOO_LONG"
    openai_fake.fail_on = "commit"
    with pytest.raises(WorkerError) as err:
        await asr.transcribe(tone(500).tobytes())
    assert err.value.code == "WORKER_FAILED"  # the job layer retries once on this code


async def test_asr_without_key_or_server():
    asr = OpenAiAsr("", url="ws://127.0.0.1:9/v1/realtime?intent=transcription")
    assert (await asr.health())["ready"] is False
    with pytest.raises(WorkerError) as err:
        await asr.open_stream(lambda _t: None)
    assert err.value.code == "ASR_FAILED"
    unreachable = OpenAiAsr("sk-test", url=f"ws://127.0.0.1:{free_port()}/v1/realtime?intent=transcription")
    with pytest.raises(WorkerError) as err:
        await unreachable.open_stream(lambda _t: None)
    assert err.value.code == "ASR_FAILED"
    health = await OpenAiAsr("sk-test").health()
    assert health["ready"] is True and health["model_id"] == "openai/gpt-live-transcribe"
    assert "sk-test" not in json.dumps(health)


# ---------------------------------------------------------------- FAKE Deepgram /v1/speak server


class FakeDeepgram:
    """FAKE: per Speak+Flush, `chunks` 100 ms tones `delay` apart, then Flushed; Clear stops and answers Cleared."""

    def __init__(self):
        self.chunks = 5
        self.delay = 0.02
        self.ignore_clear = False
        self.late_flushed = False  # send a Flushed for the cleared request after Cleared
        self.drop_after: int | None = None  # close the socket with 1011 after this many chunks
        self.connections: list[dict] = []
        self.port = free_port()

    async def handler(self, ws):
        conn = {"headers": dict(ws.request.headers), "query": parse_qs(urlparse(ws.request.path).query),
                "messages": []}
        self.connections.append(conn)
        clear = asyncio.Event()
        sending: asyncio.Task | None = None

        async def produce():
            for i in range(self.chunks):
                if self.drop_after is not None and i == self.drop_after:
                    await ws.close(1011, "NET-0000")
                    return
                if clear.is_set():
                    return
                await asyncio.sleep(self.delay)
                if clear.is_set():
                    return
                await ws.send(tone(100, rate=24000).tobytes())
            await ws.send(json.dumps({"type": "Flushed", "sequence_id": 0}))

        async for raw in ws:
            msg = json.loads(raw)
            conn["messages"].append(msg)
            if msg["type"] == "Flush":
                clear.clear()
                sending = asyncio.create_task(produce())
            elif msg["type"] == "Clear" and not self.ignore_clear:
                clear.set()
                if sending:
                    await sending
                await ws.send(json.dumps({"type": "Cleared", "sequence_id": 0}))
                if self.late_flushed:
                    await ws.send(json.dumps({"type": "Flushed", "sequence_id": 0}))


@pytest.fixture
async def deepgram_fake():
    fake = FakeDeepgram()
    async with serve(fake.handler, "127.0.0.1", fake.port, max_size=2**24):
        yield fake


def tts_for(fake: FakeDeepgram, key: str = "dg-test") -> DeepgramTts:
    return DeepgramTts(key, VOICES, ws_url=f"ws://127.0.0.1:{fake.port}/v1/speak",
                       rest_url=f"http://127.0.0.1:{fake.port}/v1/speak")


async def collect(gen) -> list[tuple[str, object]]:
    return [item async for item in gen]


async def test_tts_event_order_and_request_params(deepgram_fake):
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    events = await collect(stream.synthesize("r1", "libritts_r_4992_f", "Hello there.", 1.0))
    kinds = [k for k, _ in events]
    assert kinds[0] == "start" and events[0][1] == 24000
    assert kinds[1:-1] == ["audio"] * 5 and kinds[-1] == "done"
    assert events[-1][1]["audio_ms"] == 500 and events[-1][1]["request_id"] == "r1"
    conn = deepgram_fake.connections[0]
    q = {k: v[0] for k, v in conn["query"].items()}
    # The scenario's local voice id maps to a hosted voice; model-improvement opt-out on every request.
    assert q == {"model": "aura-2-thalia-en", "encoding": "linear16", "sample_rate": "24000", "speed": "1.0",
                 "mip_opt_out": "true"}
    assert conn["headers"]["authorization"] == "Token dg-test"
    assert conn["messages"] == [{"type": "Speak", "text": "Hello there."}, {"type": "Flush"}]
    # Sequential requests reuse the connection.
    await collect(stream.synthesize("r2", "libritts_r_4992_f", "Again.", 1.0))
    assert len(deepgram_fake.connections) == 1
    await stream.close()
    await tts.aclose()


async def test_tts_speed_is_clamped_and_reconnects_per_speed(deepgram_fake):
    assert clamp_speed(0.5) == 0.7 and clamp_speed(2.0) == 1.5 and clamp_speed(0.85) == 0.85
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    await collect(stream.synthesize("r1", "aura2_apollo_m", "One.", 0.5))
    await collect(stream.synthesize("r2", "aura2_apollo_m", "Two.", 2.0))
    speeds = [c["query"]["speed"][0] for c in deepgram_fake.connections]
    assert speeds == ["0.7", "1.5"]
    assert all(c["query"]["model"] == ["aura-2-apollo-en"] for c in deepgram_fake.connections)
    await stream.close()
    await tts.aclose()


async def test_tts_cancel_stops_audio_and_next_request_is_clean(deepgram_fake):
    deepgram_fake.chunks, deepgram_fake.delay = 50, 0.02
    deepgram_fake.late_flushed = True
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    seen: list[str] = []
    cancelled_at = None
    async for kind, _ in stream.synthesize("r1", "aura2_thalia_f", "A long sentence.", 1.0):
        if cancelled_at is not None and kind == "audio":
            raise AssertionError("audio after cancel")
        seen.append(kind)
        if seen.count("audio") == 2:
            await stream.cancel("r1")
            cancelled_at = len(seen)
    assert seen[-1] == "cancelled" and seen.count("audio") == 2
    assert {"type": "Clear"} in deepgram_fake.connections[0]["messages"]
    # A late Flushed for the cleared request must not end the next one early.
    deepgram_fake.chunks = 3
    events = await collect(stream.synthesize("r2", "aura2_thalia_f", "Next.", 1.0))
    assert [k for k, _ in events] == ["start", "audio", "audio", "audio", "done"]
    assert len(deepgram_fake.connections) == 1
    await stream.close()
    await tts.aclose()


async def test_tts_cancel_without_cleared_drops_connection(deepgram_fake, monkeypatch):
    monkeypatch.setattr(tts_deepgram, "CLEAR_WAIT_S", 0.2)
    deepgram_fake.chunks, deepgram_fake.ignore_clear = 50, True
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    kinds = []
    async for kind, _ in stream.synthesize("r1", "aura2_thalia_f", "Long.", 1.0):
        kinds.append(kind)
        if kind == "audio":
            await stream.cancel("r1")
    assert kinds == ["start", "audio", "cancelled"] and stream.ws is None
    deepgram_fake.ignore_clear, deepgram_fake.chunks = False, 2
    assert [k for k, _ in await collect(stream.synthesize("r2", "aura2_thalia_f", "Hi.", 1.0))][-1] == "done"
    assert len(deepgram_fake.connections) == 2
    await stream.close()
    await tts.aclose()


async def test_tts_cancel_before_start_sends_nothing(deepgram_fake):
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    await stream.cancel("r9")
    assert await collect(stream.synthesize("r9", "aura2_thalia_f", "Never.", 1.0)) == [("cancelled", None)]
    assert deepgram_fake.connections == []
    await tts.aclose()


async def test_tts_connection_loss_is_tts_failed(deepgram_fake):
    deepgram_fake.drop_after = 2  # provider closes the socket (1011) mid-request, no Flushed
    tts = tts_for(deepgram_fake)
    stream = tts.stream_session()
    kinds = []
    with pytest.raises(WorkerError) as err:
        async for kind, _ in stream.synthesize("r1", "aura2_thalia_f", "Hello.", 1.0):
            kinds.append(kind)
    assert err.value.code == "TTS_FAILED" and kinds == ["start", "audio", "audio"] and stream.ws is None
    await tts.aclose()


async def test_tts_rest_wav_and_health(deepgram_fake):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=tone(200, rate=24000).tobytes())

    tts = tts_for(deepgram_fake)
    await tts.http.aclose()
    tts.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Token dg-test"})
    wav = await tts.synthesize_wav("libritts_r_1188_m", "Could I have it to go?", 0.85)
    with wave.open(BytesIO(wav)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (24000, 1, 2, 4800)
    params = dict(calls[0].url.params)
    assert params == {"model": "aura-2-apollo-en", "encoding": "linear16", "sample_rate": "24000", "speed": "0.85",
                      "mip_opt_out": "true", "container": "none"}
    assert json.loads(calls[0].content) == {"text": "Could I have it to go?"}
    health = await tts.health()
    assert health["ready"] is True and health["sample_rate"] == 24000
    assert [v["voice_id"] for v in health["voices"]] == ["aura2_thalia_f", "aura2_apollo_m"]
    assert "dg-test" not in json.dumps(health)
    await tts.aclose()
    keyless = DeepgramTts("", VOICES)
    assert (await keyless.health())["ready"] is False
    with pytest.raises(WorkerError):
        await keyless.synthesize_wav("aura2_thalia_f", "Hi.")
    await keyless.aclose()
