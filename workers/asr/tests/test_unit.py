"""Unit tests with a FAKE backend (no model): scheduling, windowing, cancel, auth, limits.

The fake returns canned text and sleeps to imitate decode time; it does not pretend to be a model.
"""
import asyncio
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from asr_worker.app import create_app
from asr_worker.audio import is_silent, quietest_cut
from asr_worker.config import MAX_STREAM_SAMPLES, MAX_TRANSCRIBE_BYTES, Settings
from asr_worker.engine import AsrEngine, DecodeGate
from asr_worker.stream import StreamSession, StreamTooLong

SR = 16000


class FakeBackend:
    """FAKE: records segment lengths, returns 'seg<len_ms>' per segment after `delay` seconds."""

    name = "fake"
    device = "cpu"

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls: list[list[int]] = []

    def transcribe(self, audios, context):
        self.calls.append([len(a) for a in audios])
        time.sleep(self.delay)
        return [f"seg{len(a) * 1000 // SR}" for a in audios]


def tone(seconds: float, amp: float = 0.3) -> bytes:
    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * 220 * t) * amp * 32767).astype("<i2").tobytes()


def settings(**kw) -> Settings:
    base = dict(
        token="secret", port=0, model_dir=None, backend="fake", device="cpu",
        partial_interval_ms=700, partial_min_new_ms=300, partial_window_s=12.0, silence_dbfs=-40.0,
        exit_on_load_failure=False,
    )
    return Settings(**{**base, **kw})


def test_silence_gate_and_cut():
    assert is_silent(np.zeros(SR, np.float32), -40)
    assert is_silent(np.full(SR, 0.001, np.float32), -40)
    x = np.frombuffer(tone(1.0), "<i2").astype(np.float32) / 32768
    assert not is_silent(x, -40)
    x[8000:9600] = 0  # a 100 ms hole
    assert 8000 <= quietest_cut(x, 0, len(x)) <= 9600


async def test_gate_serializes_and_prioritizes_finals():
    gate = DecodeGate()
    order, running = [], []

    def job(name):
        def run():
            running.append(name)
            assert len(running) == 1, "decodes overlapped"
            time.sleep(0.05)
            order.append(name)
            running.remove(name)
        return run

    first = asyncio.create_task(gate.run(job("p1"), final=False))
    await asyncio.sleep(0.01)
    p2 = asyncio.create_task(gate.run(job("p2"), final=False))
    await asyncio.sleep(0)
    f = asyncio.create_task(gate.run(job("final"), final=True))
    await asyncio.gather(first, p2, f)
    assert order == ["p1", "final", "p2"]
    assert not gate.busy


async def test_gate_cancelled_caller_does_not_release_early():
    gate = DecodeGate()
    running = []

    def slow():
        running.append(1)
        time.sleep(0.1)
        running.pop()
        return "slow"

    t = asyncio.create_task(gate.run(slow, final=False))
    await asyncio.sleep(0.02)
    t.cancel()
    assert await gate.run(lambda: "ok" if running == [] else "overlap", final=True) == "ok"


async def feed_realtime(session: StreamSession, seconds: float, frame_ms: int = 20, speed: float = 1.0):
    pcm = tone(seconds)
    step = SR * frame_ms // 1000 * 2
    for i in range(0, len(pcm), step):
        session.add_audio(pcm[i : i + step])
        await asyncio.sleep(frame_ms / 1000 / speed)


async def test_partials_are_bounded_and_never_queue():
    backend = FakeBackend(delay=0.2)
    sent = []

    async def send(msg):
        sent.append((time.monotonic(), msg))

    s = StreamSession(AsrEngine(backend, -40), send, interval_ms=700, min_new_ms=300)
    await feed_realtime(s, 3.0)
    await s.commit()
    partial_times = [t for t, m in sent if m["type"] == "partial"]
    finals = [m for _, m in sent if m["type"] == "final"]
    assert len(finals) == 1 and sent[-1][1]["type"] == "final"
    # 3 s of audio with >= 700 ms between decode starts: at most 5 partial decodes.
    # A partial still decoding at commit time is decoded but never sent.
    partial_decodes = len(backend.calls) - 1
    assert 2 <= len(partial_times) <= partial_decodes <= 5
    assert all(b - a >= 0.69 for a, b in zip(partial_times, partial_times[1:]))
    assert finals[0]["audio_ms"] == 3000


async def test_no_partial_below_min_new_audio():
    backend = FakeBackend()

    async def send(msg):
        pass

    s = StreamSession(AsrEngine(backend, -40), send, min_new_ms=300)
    s.add_audio(tone(0.2))
    await asyncio.sleep(0.05)
    assert backend.calls == []
    s.add_audio(tone(0.15))
    await asyncio.sleep(0.05)
    assert backend.calls == [[int(0.35 * SR)]]
    s.close()


async def test_no_partial_decodes_during_trailing_silence():
    backend = FakeBackend()

    async def send(msg):
        pass

    s = StreamSession(AsrEngine(backend, -40), send, interval_ms=700)
    await feed_realtime(s, 1.0, speed=2.0)
    await asyncio.sleep(0.8)
    decodes_after_speech = len(backend.calls)
    assert decodes_after_speech >= 1
    silence = b"\0" * (SR * 2 // 50)
    for _ in range(100):  # 2 s of silence in 20 ms frames
        s.add_audio(silence)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.8)
    assert len(backend.calls) == decodes_after_speech
    s.close()


async def test_long_utterance_uses_bounded_windows():
    backend = FakeBackend()
    sent = []

    async def send(msg):
        sent.append(msg)

    s = StreamSession(AsrEngine(backend, -40), send, interval_ms=700, window_s=12.0)
    await feed_realtime(s, 30.0, frame_ms=100, speed=4.0)
    await s.commit()
    partial_calls, final_call = backend.calls[:-1], backend.calls[-1]
    assert all(n <= 12 * SR for call in partial_calls for n in call)
    assert any(len(call) == 2 for call in partial_calls), "window roll-over never happened"
    assert final_call == [30 * SR]
    late = [m for m in sent if m["type"] == "partial" and m["audio_ms"] > 13000]
    # Later partials keep the frozen text of earlier windows as prefix.
    assert late and all(len(m["text"].split()) >= 2 for m in late)


async def test_stream_length_cap():
    async def send(msg):
        pass

    s = StreamSession(AsrEngine(FakeBackend(), -40), send, min_new_ms=10**9)
    s.add_audio(b"\0" * (MAX_STREAM_SAMPLES * 2))
    with pytest.raises(StreamTooLong):
        s.add_audio(b"\0\0")
    s.close()


async def test_cancel_frees_buffer_and_drops_partials():
    backend = FakeBackend(delay=0.3)
    sent = []

    async def send(msg):
        sent.append(msg)

    s = StreamSession(AsrEngine(backend, -40), send)
    s.add_audio(tone(1.0))
    await asyncio.sleep(0.05)  # partial decode now running
    s.close()
    assert s.samples == 0
    await asyncio.sleep(0.4)
    assert sent == []


@pytest.fixture
def client():
    app = create_app(settings(), backend_factory=lambda: FakeBackend())
    with TestClient(app) as c:
        for _ in range(100):
            if c.get("/health", headers={"X-Worker-Token": "secret"}).json()["ready"]:
                break
            time.sleep(0.02)
        yield c


AUTH = {"X-Worker-Token": "secret", "Content-Type": "application/octet-stream"}


def test_http_auth(client):
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"X-Worker-Token": "wrong"}).status_code == 401
    r = client.post("/transcribe", content=tone(0.5), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "AUTH_REQUIRED"


def test_transcribe_limits(client):
    r = client.post("/transcribe", content=b"\0" * (MAX_TRANSCRIBE_BYTES + 2), headers=AUTH)
    assert r.status_code == 413 and r.json()["error"]["code"] == "AUDIO_TOO_LONG"
    assert client.post("/transcribe", content=b"\0\0\0", headers=AUTH).status_code == 400
    assert client.post("/transcribe?language=Korean", content=tone(0.5), headers=AUTH).status_code == 400
    r = client.post("/transcribe", params={"context": "x" * 301}, content=tone(0.5), headers=AUTH)
    assert r.status_code == 400
    r = client.post("/transcribe", content=b"\0" * MAX_TRANSCRIBE_BYTES, headers=AUTH)
    assert r.status_code == 200 and r.json()["text"] == ""  # exactly 120 s is accepted
    r = client.post("/transcribe", content=tone(0.5), headers=AUTH).json()
    assert (r["text"], r["language"], r["audio_ms"]) == ("seg500", "English", 500)


def test_ws_auth_rejected(client):
    from starlette.testclient import WebSocketDenialResponse

    with pytest.raises(WebSocketDenialResponse) as exc:
        with client.websocket_connect("/stream"):
            pass
    assert exc.value.status_code == 401


def test_ws_commit_and_cancel(client):
    with client.websocket_connect("/stream", headers={"X-Worker-Token": "secret"}) as ws:
        ws.send_bytes(tone(1.0))
        ws.send_json({"type": "commit"})
        while (m := ws.receive_json())["type"] != "final":
            assert m["type"] == "partial"
        assert m["text"] == "seg1000"
        assert ws.receive()["type"] == "websocket.close"
    with client.websocket_connect("/stream", headers={"X-Worker-Token": "secret"}) as ws:
        ws.send_bytes(tone(0.2))
        ws.send_json({"type": "cancel"})
        assert ws.receive()["type"] == "websocket.close"
