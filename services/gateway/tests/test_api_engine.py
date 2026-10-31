"""Realtime engine end to end over the API-mode ASR and TTS adapters, against the FAKE provider servers of
test_api_adapters.py (OpenAI realtime transcription, Deepgram /v1/speak) and test_api_tts_openrouter.py (OpenRouter
/audio/speech, the default speech path). FakeLlm from conftest writes the reply.

Checks the wiring the unit tests cannot: partials reach the browser as replacing `asr.partial` events, the
adapter's final becomes `asr.final`, provider audio streams as output frames, and a barge-in cancels the provider
request (Deepgram: Clear; OpenRouter: the HTTP response is closed) with no stale frames afterwards (PROTOCOL §6.3).
The Deepgram tests inject `DeepgramTts` directly (the opt-in `tts_provider = "deepgram"` fallback).
"""

from __future__ import annotations

import asyncio

import httpx
import numpy as np
import pytest
from conftest import Gateway, silence, tone
from test_api_adapters import VOICES, FakeDeepgram, FakeOpenAiRealtime
from test_api_tts_openrouter import VOICES as OR_VOICES
from test_api_tts_openrouter import FakeOpenRouterSpeech
from test_realtime import speak_turn, start
from websockets.asyncio.server import serve

from vr_gateway.adapters.asr_openai import OpenAiAsr
from vr_gateway.adapters.tts_deepgram import DeepgramTts
from vr_gateway.adapters.tts_openrouter import OpenRouterTts

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def api_gw(tmp_path):
    oa, dg = FakeOpenAiRealtime(), FakeDeepgram()
    async with serve(oa.handler, "127.0.0.1", oa.port, max_size=2**24), \
            serve(dg.handler, "127.0.0.1", dg.port, max_size=2**24):
        g = await asyncio.to_thread(Gateway, tmp_path, mode="api", shared=True)
        g.svc.asr = OpenAiAsr("sk-test", url=f"ws://127.0.0.1:{oa.port}/v1/realtime?intent=transcription")
        g.svc.tts = DeepgramTts("dg-test", VOICES, ws_url=f"ws://127.0.0.1:{dg.port}/v1/speak",
                                rest_url="http://deepgram.fake/v1/speak")
        # Opening lines go through the REST endpoint: answer locally, never over the network.
        await g.svc.tts.http.aclose()
        g.svc.tts.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=tone(300, rate=24000).tobytes())))
        g.oa, g.dg = oa, dg
        try:
            yield g
        finally:
            await asyncio.to_thread(g.stop)


async def test_turn_over_api_adapters(api_gw):
    g = api_gw
    g.dg.delay = 0.0
    session, rt = await start(g)
    # Real-time pacing: partials come back from the provider while the learner is still speaking.
    pcm = np.concatenate([silence(320), tone(1600)])
    for i in range(0, len(pcm), 512):
        await rt.audio(pcm[i : i + 512])
        await asyncio.sleep(0.032)
    await rt.audio(silence(1100))
    final = await rt.until("asr.final", timeout=5)
    assert final["text"] == "Can I get a latte, please?"
    partials = [e["text"] for e in rt.events if e["type"] == "asr.partial"]
    assert partials and partials[0] == "Can" and all(b.startswith(a) for a, b in zip(partials, partials[1:]))
    resp = await rt.until("response.started")
    await rt.until("response.done", response_id=resp["response_id"], timeout=10)
    frames = [h for h, _ in rt.frames if h["response_id"] == resp["response_id"]]
    assert frames and all(h["sample_rate"] == 24000 and h["sample_count"] <= 2400 for h in frames)
    # One Deepgram connection per realtime session, model-improvement opt-out on it.
    live = [c for c in g.dg.connections if c["query"].get("mip_opt_out") == ["true"]]
    assert live == g.dg.connections and len(g.dg.connections) >= 1
    await rt.ws.close()


async def test_barge_in_over_deepgram_has_no_stale_frames(api_gw):
    g = api_gw
    session, rt = await start(g)
    g.dg.chunks, g.dg.delay = 30, 0.03
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started", timeout=5)
    while not any(h["response_id"] == resp["response_id"] for h, _ in rt.frames):
        await rt.recv(5)
    await rt.event("playback.started", response_id=resp["response_id"], segment_id=0)
    await rt.audio(np.concatenate([silence(64), tone(700), silence(1100)]))
    cancelled = await rt.until("response.cancelled", response_id=resp["response_id"], timeout=5)
    assert cancelled["epoch"] == 1
    idx = next(i for i, (k, x) in enumerate(rt.log) if k == "event" and x["type"] == "response.cancelled")
    new = await rt.until("response.started", timeout=10)
    await rt.until("response.done", response_id=new["response_id"], timeout=15)
    after = rt.log[idx + 1 :]
    assert [x for k, x in after if x.get("response_id") == resp["response_id"] and x.get("type") != "error"] == []
    assert all(x["epoch"] == 1 for k, x in after if k == "frame")
    assert any({"type": "Clear"} in c["messages"] for c in g.dg.connections)  # the provider was told to stop
    await rt.ws.close()


# ---------------------------------------------------------------- default speech path: Aura-2 through OpenRouter


@pytest.fixture
async def or_gw(tmp_path):
    oa, speech = FakeOpenAiRealtime(), FakeOpenRouterSpeech()
    server = await asyncio.start_server(speech.handler, "127.0.0.1", speech.port)
    async with server, serve(oa.handler, "127.0.0.1", oa.port, max_size=2**24):
        g = await asyncio.to_thread(Gateway, tmp_path, mode="api", shared=True)
        g.svc.asr = OpenAiAsr("sk-test", url=f"ws://127.0.0.1:{oa.port}/v1/realtime?intent=transcription")
        g.svc.tts = OpenRouterTts("or-test", OR_VOICES, base_url=speech.base_url)
        g.oa, g.speech = oa, speech
        try:
            yield g
        finally:
            await asyncio.to_thread(g.stop)
            await g.svc.tts.aclose()


async def test_turn_over_openrouter_tts(or_gw):
    g = or_gw
    g.speech.delay = 0.0
    session, rt = await start(g)
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started", timeout=5)
    await rt.until("response.done", response_id=resp["response_id"], timeout=10)
    frames = [h for h, _ in rt.frames if h["response_id"] == resp["response_id"]]
    assert frames and all(h["sample_rate"] == 24000 and h["sample_count"] <= 2400 for h in frames)
    assert g.speech.requests
    assert all(r["json"]["provider"] == {"zdr": True, "data_collection": "deny"} for r in g.speech.requests)
    await rt.ws.close()


async def test_barge_in_over_openrouter_tts_has_no_stale_frames(or_gw):
    g = or_gw
    session, rt = await start(g)
    g.speech.chunks, g.speech.delay = 30, 0.03
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started", timeout=5)
    while not any(h["response_id"] == resp["response_id"] for h, _ in rt.frames):
        await rt.recv(5)
    await rt.event("playback.started", response_id=resp["response_id"], segment_id=0)
    await rt.audio(np.concatenate([silence(64), tone(700), silence(1100)]))
    cancelled = await rt.until("response.cancelled", response_id=resp["response_id"], timeout=5)
    assert cancelled["epoch"] == 1
    idx = next(i for i, (k, x) in enumerate(rt.log) if k == "event" and x["type"] == "response.cancelled")
    new = await rt.until("response.started", timeout=10)
    await rt.until("response.done", response_id=new["response_id"], timeout=15)
    after = rt.log[idx + 1 :]
    assert [x for k, x in after if x.get("response_id") == resp["response_id"] and x.get("type") != "error"] == []
    assert all(x["epoch"] == 1 for k, x in after if k == "frame")
    # The provider request of the interrupted reply was closed before it finished.
    assert any(r["closed"].is_set() and r["sent"] < 30 for r in g.speech.requests)
    await rt.ws.close()
