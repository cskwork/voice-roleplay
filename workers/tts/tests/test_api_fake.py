"""Protocol tests against a FAKE engine (sine-wave chunks, no model). Real-model tests: test_real_model.py."""

import io
import json
import threading
import time
import wave

import httpx
import numpy as np
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from conftest import TOKEN, serve
from tts_worker.app import create_app
from tts_worker.engine import Voice

SR = 24000
CHUNK_MS = 300


class FakeEngine:
    """FAKE: stands in for the CosyVoice engine; yields 300 ms sine chunks every 50 ms and honours cancel."""

    ready = True
    sample_rate = SR
    model_id = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
    revision = "fake-rev"
    device = "cpu"
    placement = {"llm": "cpu", "flow": "cpu", "hift": "cpu"}
    load_seconds = 0.0

    def __init__(self, chunks=6):
        self.chunks = chunks
        self.voices = {"dev_voice_a": Voice("dev_voice_a", "A", "note", "zero_shot", None, "")}
        self.lock = threading.Lock()
        self.calls = []

    def synthesize(self, text, voice_id, speed, cancel, stream=True):
        with self.lock:
            self.calls.append((text, voice_id, speed, stream))
            t = np.arange(SR * CHUNK_MS // 1000) / SR
            pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2")
            for _ in range(self.chunks if stream else 1):
                if cancel.is_set():
                    return
                time.sleep(0.05)
                yield pcm


H = {"X-Worker-Token": TOKEN}


@pytest.fixture
def fake():
    eng = FakeEngine()
    with serve(create_app(engine=eng, token=TOKEN)) as addr:
        yield eng, addr


def ws(addr, headers=H):
    return connect(f"ws://{addr}/synthesize", additional_headers=headers, open_timeout=5)


def recv_until(conn, pred, timeout=5.0):
    """Collect messages until pred(msg) is true for a JSON message; returns (json msgs, frames before it)."""
    msgs, frames = [], []
    deadline = time.time() + timeout
    while True:
        m = conn.recv(timeout=max(0.01, deadline - time.time()))
        if isinstance(m, bytes):
            frames.append(m)
            continue
        msgs.append(json.loads(m))
        if pred(msgs[-1]):
            return msgs, frames


def test_http_requires_token(fake):
    _, addr = fake
    assert httpx.get(f"http://{addr}/health").status_code == 401
    assert httpx.get(f"http://{addr}/health", headers={"X-Worker-Token": "wrong"}).status_code == 401
    r = httpx.post(f"http://{addr}/synthesize", json={"voice_id": "dev_voice_a", "text": "Hi there."})
    assert r.status_code == 401 and r.json() == {"error": {"code": "AUTH_REQUIRED"}}


def test_ws_requires_token(fake):
    _, addr = fake
    for headers in ({}, {"X-Worker-Token": "wrong"}):
        with pytest.raises(InvalidStatus) as e:
            ws(addr, headers)
        assert e.value.response.status_code == 401


def test_health(fake):
    _, addr = fake
    body = httpx.get(f"http://{addr}/health", headers=H).json()
    assert body["ready"] is True and body["sample_rate"] == SR
    assert body["model_id"] == "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
    assert body["voices"] == [{"voice_id": "dev_voice_a", "label": "A", "license_note": "note"}]


def test_post_returns_wav_and_normalizes(fake):
    eng, addr = fake
    r = httpx.post(f"http://{addr}/synthesize", headers=H, json={"voice_id": "dev_voice_a", "text": "It's $4.50.", "speed": 0.8})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SR)
        assert w.getnframes() == SR * CHUNK_MS // 1000
    assert eng.calls[-1] == ("It's four dollars and fifty cents.", "dev_voice_a", 0.8, False)


@pytest.mark.parametrize("body, status, code", [
    ({"voice_id": "nope", "text": "Hi."}, 404, "VOICE_NOT_FOUND"),
    ({"voice_id": "dev_voice_a", "text": "안녕"}, 400, "TEXT_NOT_ENGLISH"),
    ({"voice_id": "dev_voice_a", "text": "x" * 401}, 400, "TEXT_TOO_LONG"),
    ({"voice_id": "dev_voice_a", "text": "Hi.", "speed": 3}, 400, "INVALID_SPEED"),
    ({"voice_id": "dev_voice_a"}, 400, "BAD_REQUEST"),
    ({"voice_id": "dev_voice_a", "text": "Hi.", "pad": "x" * 20000}, 413, "BAD_REQUEST"),
])
def test_post_validation(fake, body, status, code):
    _, addr = fake
    r = httpx.post(f"http://{addr}/synthesize", headers=H, json=body)
    assert r.status_code == status and r.json()["error"]["code"] == code


def test_ws_stream_order(fake):
    _, addr = fake
    with ws(addr) as c:
        c.send(json.dumps({"type": "synthesize", "request_id": "r1", "voice_id": "dev_voice_a", "text": "Hello there."}))
        msgs, frames = recv_until(c, lambda m: m["type"] == "done")
    assert msgs[0] == {"type": "start", "request_id": "r1", "sample_rate": SR}
    done = msgs[-1]
    assert done["request_id"] == "r1" and done["first_chunk_ms"] is not None
    assert sum(map(len, frames)) == 6 * SR * CHUNK_MS // 1000 * 2
    assert max(map(len, frames)) <= SR // 10 * 2  # ≤ 100 ms per frame
    assert done["audio_ms"] == 6 * CHUNK_MS


def test_ws_cancel_active_then_next_request(fake):
    _, addr = fake
    with ws(addr) as c:
        c.send(json.dumps({"type": "synthesize", "request_id": "r1", "voice_id": "dev_voice_a", "text": "One."}))
        c.send(json.dumps({"type": "synthesize", "request_id": "r2", "voice_id": "dev_voice_a", "text": "Two."}))
        assert json.loads(c.recv(timeout=5))["type"] == "start"
        assert isinstance(c.recv(timeout=5), bytes)
        c.send(json.dumps({"type": "cancel", "request_id": "r1"}))
        msgs, _ = recv_until(c, lambda m: m["type"] == "cancelled")
        assert msgs[-1] == {"type": "cancelled", "request_id": "r1"}
        # Nothing for r1 after "cancelled": the next message must be r2's start, with no frames in between.
        after, frames = recv_until(c, lambda m: m["type"] == "start")
        assert frames == [] and after == [{"type": "start", "request_id": "r2", "sample_rate": SR}]
        msgs, _ = recv_until(c, lambda m: m["type"] in ("done", "cancelled", "error"))
        assert msgs[-1]["type"] == "done" and msgs[-1]["request_id"] == "r2"


def test_ws_cancel_queued_request(fake):
    _, addr = fake
    with ws(addr) as c:
        for rid in ("r1", "r2"):
            c.send(json.dumps({"type": "synthesize", "request_id": rid, "voice_id": "dev_voice_a", "text": "Hi."}))
        c.send(json.dumps({"type": "cancel", "request_id": "r2"}))
        msgs, _ = recv_until(c, lambda m: m["type"] == "done")
        assert {"type": "cancelled", "request_id": "r2"} in msgs
        assert all(m.get("request_id") != "r2" or m["type"] == "cancelled" for m in msgs)
        with pytest.raises(TimeoutError):
            c.recv(timeout=0.5)  # r2 never starts


def test_ws_errors(fake):
    _, addr = fake
    with ws(addr) as c:
        c.send(json.dumps({"type": "synthesize", "request_id": "r1", "voice_id": "nope", "text": "Hi."}))
        assert json.loads(c.recv(timeout=5)) == {"type": "error", "request_id": "r1", "code": "VOICE_NOT_FOUND"}
        c.send("not json")
        assert json.loads(c.recv(timeout=5))["code"] == "BAD_REQUEST"
        c.send(b"\x00\x01")  # binary from the client is not part of the protocol
        assert json.loads(c.recv(timeout=5))["code"] == "BAD_REQUEST"
        c.send("[1, 2]")
        assert json.loads(c.recv(timeout=5))["code"] == "BAD_REQUEST"
        c.send(json.dumps({"type": "cancel", "request_id": "unknown"}))  # ignored
        c.send(json.dumps({"type": "synthesize", "request_id": "r2", "voice_id": "dev_voice_a", "text": "안녕"}))
        assert json.loads(c.recv(timeout=5)) == {"type": "error", "request_id": "r2", "code": "TEXT_NOT_ENGLISH"}


def test_not_ready_reports_model_not_ready():
    eng = FakeEngine()
    eng.ready = False
    with serve(create_app(engine=eng, token=TOKEN)) as addr:
        # the startup loader calls eng.load(); FakeEngine has none, so health reports the error
        body = httpx.get(f"http://{addr}/health", headers=H).json()
        assert body["ready"] is False
        r = httpx.post(f"http://{addr}/synthesize", headers=H, json={"voice_id": "dev_voice_a", "text": "Hi."})
        assert r.status_code == 503 and r.json()["error"]["code"] == "MODEL_NOT_READY"
