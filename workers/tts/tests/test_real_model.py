"""Real-model tests: start the worker exactly as in production (``python -m tts_worker``) and talk to it.

Skipped when the model files are missing. Takes a few minutes on an M3 Pro.
"""

import io
import json
import os
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import psutil
import pytest
from websockets.sync.client import connect

from conftest import TOKEN
from tts_worker.engine import DEFAULT_MODEL_DIR, REQUIRED_MODEL_FILES

pytestmark = pytest.mark.skipif(
    any(not (DEFAULT_MODEL_DIR / f).exists() for f in REQUIRED_MODEL_FILES), reason="model files not downloaded")

WORKER_DIR = Path(__file__).resolve().parents[1]
H = {"X-Worker-Token": TOKEN}
SECRET_PHRASE = "Quibbling zephyr marmalade forty-seven"  # must never appear in logs


@pytest.fixture(scope="module")
def worker(tmp_path_factory):
    log_path = tmp_path_factory.mktemp("tts") / "worker.log"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = {**os.environ, "VR_WORKER_TOKEN": TOKEN, "VR_TTS_PORT": str(port)}
    with open(log_path, "wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "tts_worker"], cwd=WORKER_DIR, env=env,
                                stdout=log, stderr=subprocess.STDOUT)
    base = f"127.0.0.1:{port}"
    t0 = time.time()
    try:
        while time.time() - t0 < 300:
            try:
                body = httpx.get(f"http://{base}/health", headers=H, timeout=2).json()
                if body["ready"] or body.get("error"):
                    break
            except httpx.TransportError:
                pass
            assert proc.poll() is None, log_path.read_text(errors="replace")[-3000:]
            time.sleep(1)
        assert body["ready"], body
        yield {"base": base, "proc": proc, "log": log_path, "health": body, "ready_s": time.time() - t0}
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def wav_to_array(data: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        return np.frombuffer(w.readframes(w.getnframes()), "<i2"), w.getframerate()


def assert_speech_like(pcm: np.ndarray, sr: int, n_words: int) -> float:
    dur = len(pcm) / sr
    assert 0.12 * n_words <= dur <= 1.0 * n_words + 1.0, f"duration {dur:.2f}s for {n_words} words"
    frames = pcm[: len(pcm) // (sr // 20) * (sr // 20)].reshape(-1, sr // 20).astype(np.float32) / 32768
    rms = np.sqrt((frames ** 2).mean(axis=1))
    assert (rms > 0.01).mean() > 0.4, "mostly silent"
    assert np.abs(pcm).max() < 32767, "clipped"
    return dur


def post(worker, text, voice="dev_voice_a", speed=1.0):
    r = httpx.post(f"http://{worker['base']}/synthesize", headers=H, timeout=180,
                   json={"voice_id": voice, "text": text, "speed": speed})
    assert r.status_code == 200, r.text
    return wav_to_array(r.content)


def test_health(worker):
    h = worker["health"]
    assert h["sample_rate"] == 24000
    assert h["revision"] == "29e01c4e8d000f4bcd70751be16fa94bf3d85a18"
    assert {v["voice_id"] for v in h["voices"]} == {"dev_voice_a", "dev_voice_b"}
    assert all("출시 전 권리 확인된 음성으로 교체 필요" in v["license_note"] for v in h["voices"])
    print(f"\n[measure] health={json.dumps(h)} process_ready_s={worker['ready_s']:.1f}")


@pytest.mark.parametrize("name, voice, text, n_words", [
    ("price", "dev_voice_a", "The latte is $4.50, and the muffin is $3.25.", 16),
    ("date_time", "dev_voice_b", "Let's meet on March 3rd at 3:30 p.m.", 10),
    ("contraction", "dev_voice_a", "I'll call you back if I can't make it, okay?", 10),
])
def test_post_sentences(worker, audio_dir, name, voice, text, n_words):
    t = time.perf_counter()
    pcm, sr = post(worker, text, voice)
    elapsed = time.perf_counter() - t
    dur = assert_speech_like(pcm, sr, n_words)
    out = audio_dir / f"tts_{name}_{voice}.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr), w.writeframes(pcm.tobytes())
    print(f"\n[measure] POST {name} voice={voice} audio_s={dur:.2f} elapsed_s={elapsed:.2f} rtf={elapsed / dur:.2f} -> {out}")


def test_slower_speed_is_longer(worker):
    text = "Could you tell me where the nearest subway station is?"
    normal, sr = post(worker, text)
    slow, _ = post(worker, text, speed=0.7)
    assert_speech_like(slow, sr, 10)
    ratio = len(slow) / len(normal)
    print(f"\n[measure] speed 0.7 duration ratio={ratio:.2f}")
    assert ratio > 1.2


def ws_conn(worker):
    return connect(f"ws://{worker['base']}/synthesize", additional_headers=H, open_timeout=10, max_size=None)


def test_ws_streams_first_chunk_before_done(worker):
    text = ("Sure, I can help with that. The coffee shop is two blocks north of the station, "
            "right next to the bookstore, and it opens at 7 a.m. every day.")
    with ws_conn(worker) as c:
        t0 = time.perf_counter()
        c.send(json.dumps({"type": "synthesize", "request_id": "s1", "voice_id": "dev_voice_a", "text": text}))
        assert json.loads(c.recv(timeout=30))["type"] == "start"
        first_frame_at, arrivals, pcm = None, [], bytearray()
        while True:
            m = c.recv(timeout=180)
            if isinstance(m, bytes):
                now = time.perf_counter() - t0
                first_frame_at = first_frame_at or now
                arrivals.append(now)
                pcm += m
                continue
            done = json.loads(m)
            done_at = time.perf_counter() - t0
            break
    assert done["type"] == "done" and done["request_id"] == "s1"
    assert first_frame_at < done_at - 0.5, "first audio should arrive well before the end"
    audio = np.frombuffer(bytes(pcm), "<i2")
    dur = assert_speech_like(audio, 24000, 30)
    assert done["audio_ms"] == len(audio) * 1000 // 24000
    print(f"\n[measure] WS stream first_frame_s={first_frame_at:.2f} done_s={done_at:.2f} audio_s={dur:.2f} "
          f"rtf={done_at / dur:.2f} server_first_chunk_ms={done['first_chunk_ms']} frames={len(arrivals)}")


def test_ws_cancel_mid_stream(worker):
    text = ("Well, there are a few options. You could take the bus, which is cheaper but slower, or you could "
            "take a taxi, which costs about $25 and gets you there in twenty minutes.")
    with ws_conn(worker) as c:
        c.send(json.dumps({"type": "synthesize", "request_id": "c1", "voice_id": "dev_voice_a", "text": text}))
        assert json.loads(c.recv(timeout=30))["type"] == "start"
        assert isinstance(c.recv(timeout=120), bytes)  # first chunk arrived
        t_cancel = time.perf_counter()
        c.send(json.dumps({"type": "cancel", "request_id": "c1"}))
        while True:  # frames already in flight may precede "cancelled"
            m = c.recv(timeout=30)
            if isinstance(m, str):
                assert json.loads(m) == {"type": "cancelled", "request_id": "c1"}
                break
        ack_ms = (time.perf_counter() - t_cancel) * 1000
        # No frames (and no "done") for c1 after "cancelled"; the next request is served normally.
        c.send(json.dumps({"type": "synthesize", "request_id": "c2", "voice_id": "dev_voice_a", "text": "Okay, no problem."}))
        m = c.recv(timeout=60)
        assert json.loads(m) == {"type": "start", "request_id": "c2", "sample_rate": 24000}
        next_start_ms = (time.perf_counter() - t_cancel) * 1000
        while True:
            m = c.recv(timeout=120)
            if isinstance(m, str):
                assert json.loads(m)["type"] == "done"
                break
    print(f"\n[measure] cancel ack_ms={ack_ms:.0f} next_request_start_ms={next_start_ms:.0f}")


def test_no_text_in_logs_and_no_external_connections(worker):
    post(worker, SECRET_PHRASE + ".")
    with ws_conn(worker) as c:
        c.send(json.dumps({"type": "synthesize", "request_id": "p1", "voice_id": "dev_voice_b", "text": SECRET_PHRASE + "."}))
        while not (isinstance(m := c.recv(timeout=120), str) and json.loads(m)["type"] != "start"):
            pass
    time.sleep(0.5)
    log = worker["log"].read_text(errors="replace")
    assert "synthesize_ws" in log and "synthesize_wav" in log  # our event lines are there
    for needle in ("Quibbling", "zephyr", "marmalade", "forty-seven", "synthesis text"):
        assert needle.lower() not in log.lower(), f"{needle!r} leaked into worker logs"
    proc = psutil.Process(worker["proc"].pid)
    for conn in proc.net_connections(kind="inet"):
        assert conn.laddr.ip == "127.0.0.1", conn
        assert not conn.raddr or conn.raddr.ip == "127.0.0.1", conn
    print(f"\n[measure] worker rss_mb={proc.memory_info().rss / 2**20:.0f}")
