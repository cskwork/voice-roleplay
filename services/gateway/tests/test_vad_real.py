"""Real Silero VAD (ONNX) on macOS `say` speech; the realtime engine is driven with the real VAD model.

ASR/TTS/LLM stay FAKE here; only the VAD is real.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from conftest import REPO, Gateway, connect_rt, create_session, silence

from vr_gateway.audio import parse_wav
from vr_gateway.config import ASSETS_DIR
from vr_gateway.vad import SileroModel, analyze

FIXTURES = REPO / "tests" / "fixtures" / "audio"
TEXT = "Hi, could I get a large latte with oat milk, please? And how much is a blueberry muffin?"


def speech_fixture() -> Path:
    path = FIXTURES / "hello_16k.wav"
    if path.exists():
        return path
    if not (shutil.which("say") and shutil.which("ffmpeg")):
        pytest.skip("needs macOS `say` and ffmpeg to generate speech")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    aiff = FIXTURES / "hello.aiff"
    subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), TEXT], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "16000", "-ac", "1",
                    "-sample_fmt", "s16", str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def model():
    return SileroModel(ASSETS_DIR / "silero_vad.onnx")


def test_silero_finds_speech_and_ignores_silence(model):
    audio = parse_wav(speech_fixture().read_bytes(), 120)
    segs = analyze(model, audio.pcm16k)
    assert segs, "no speech found"
    assert segs[0][0] < 0.5 and segs[-1][1] > audio.duration_s - 0.5
    assert analyze(model, silence(3000)) == []
    noise = (np.random.default_rng(0).normal(0, 200, 16000 * 3)).astype(np.int16)
    assert analyze(model, noise) == []


def test_silero_stream_state_is_per_stream(model):
    audio = parse_wav(speech_fixture().read_bytes(), 120).pcm16k
    a, b = model.stream(), model.stream()
    pa = [p for p, _ in a.process(audio[:16000])]
    b.process(silence(2000))
    pb = [p for p, _ in b.process(audio[:16000])]
    fresh = [p for p, _ in model.stream().process(audio[:16000])]
    assert np.allclose(pa, fresh, atol=1e-5)
    assert len(pb) == len(pa)


def test_silero_window_latency(model):
    stream = model.stream()
    audio = parse_wav(speech_fixture().read_bytes(), 120).pcm16k
    started = time.perf_counter()
    n = len(stream.process(audio))
    per_window_ms = (time.perf_counter() - started) * 1000 / n
    print(f"\nsilero_vad onnx cpu: {n} windows, {per_window_ms:.3f} ms/window (32 ms audio each)")
    assert per_window_ms < 5


@pytest.mark.asyncio
async def test_engine_turn_with_real_vad(tmp_path):
    g = Gateway(tmp_path, real_vad=True)
    try:
        audio = parse_wav(speech_fixture().read_bytes(), 120).pcm16k
        session = await asyncio.to_thread(create_session, g)
        rt = await connect_rt(g, session["session_id"])
        await rt.event("session.start")
        opening = await rt.until("response.started")
        await rt.until("response.done")
        await rt.event("playback.completed", response_id=opening["response_id"], segment_id=0)
        await rt.until("session.state", state="LISTENING", output_state="idle")
        rt.mark_seen()
        await rt.audio(np.concatenate([silence(500), audio, silence(1500)]), frame=320)  # 20 ms frames
        started = await rt.until("speech.started")
        ended = await rt.until("speech.ended", turn_id=started["turn_id"])
        assert ended.get("discarded") is None and ended["reason"] == "silence"
        await rt.until("asr.final")
        await rt.until("response.done")
        assert rt.types().count("speech.started") == 1  # short in-sentence pauses did not split the turn
        stream = g.asr.streams[0]
        # Pre-roll + speech reached the ASR stream (>= speech length minus trailing silence cut).
        assert stream.bytes >= (len(audio) - 16000) * 2
        await rt.ws.close()
    finally:
        g.stop()
