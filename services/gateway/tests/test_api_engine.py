"""Realtime engine end to end over the API-mode ASR and TTS adapters, against the FAKE provider servers of
test_api_adapters.py (OpenAI realtime transcription, Deepgram /v1/speak). FakeLlm from conftest writes the reply.

Checks the wiring the unit tests cannot: partials reach the browser as replacing `asr.partial` events, the
adapter's final becomes `asr.final`, Deepgram audio streams as output frames, and a barge-in cancels the Deepgram
request (Clear) with no stale frames afterwards (PROTOCOL §6.3).
"""

from __future__ import annotations

import asyncio

import httpx
import numpy as np
import pytest
from conftest import Gateway, silence, tone
from test_api_adapters import VOICES, FakeDeepgram, FakeOpenAiRealtime
from test_realtime import speak_turn, start
from websockets.asyncio.server import serve

from vr_gateway.adapters.asr_openai import OpenAiAsr
from vr_gateway.adapters.tts_deepgram import DeepgramTts

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
