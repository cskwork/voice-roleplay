"""Real-provider checks for API mode. Billed calls: they run only with VR_API_REAL=1 and the provider's key set,
and are skipped otherwise (CI and default local runs make no network calls).

    VR_API_REAL=1 OPENAI_API_KEY=... DEEPGRAM_API_KEY=... OPENROUTER_API_KEY=... .venv/bin/python -m pytest -q tests/test_api_real.py
"""

from __future__ import annotations

import os
import sys
import wave

import pytest
from conftest import REPO

from vr_gateway.adapters.asr_openai import OpenAiAsr
from vr_gateway.adapters.tts_deepgram import DeepgramTts

pytestmark = pytest.mark.asyncio
CLIP = REPO / "tests/fixtures/audio/e2e_cafe_01.wav"


def needs(var: str):
    return pytest.mark.skipif(not (os.environ.get("VR_API_REAL") == "1" and os.environ.get(var)),
                              reason=f"set VR_API_REAL=1 and {var} (billed provider call)")


@needs("OPENAI_API_KEY")
async def test_real_openai_transcription():
    sys.path.insert(0, str(REPO / "benchmarks/api"))
    from bench_api import wer

    with wave.open(str(CLIP)) as w:
        pcm = w.readframes(w.getnframes())
    result = await OpenAiAsr(os.environ["OPENAI_API_KEY"]).transcribe(pcm, context="Ordering at a cafe")
    assert wer(CLIP.with_suffix(".txt").read_text(), result["text"]) <= 0.3


@needs("DEEPGRAM_API_KEY")
async def test_real_deepgram_stream_and_cancel():
    tts = DeepgramTts(os.environ["DEEPGRAM_API_KEY"], REPO / "config/voices.api.json")
    stream = tts.stream_session()
    kinds = [k async for k, _ in stream.synthesize("r1", "aura2_thalia_f", "Hi there! What can I get for you?", 0.85)]
    assert kinds[0] == "start" and "audio" in kinds and kinds[-1] == "done"
    seen = []
    async for kind, _ in stream.synthesize("r2", "aura2_thalia_f", "This sentence is long enough to be cut off "
                                           "before it finishes, which is what a barge-in does.", 0.85):
        if seen and seen[-1] == "cancel-sent":
            assert kind != "audio"
        seen.append(kind)
        if kind == "audio" and "cancel-sent" not in seen:
            await stream.cancel("r2")
            seen.append("cancel-sent")
    assert seen[-1] in ("cancelled", "done")
    await stream.close()
    await tts.aclose()


@needs("OPENROUTER_API_KEY")
async def test_real_openrouter_stream_and_json():
    from vr_feedback.llm import LlmClient

    async with LlmClient.openrouter(os.environ["OPENROUTER_API_KEY"], "openai/gpt-4.1-mini") as llm:
        text = "".join([d async for d in llm.stream_chat([{"role": "user", "content": "Say hello in three words."}],
                                                         max_tokens=16)])
        assert text.strip()
        data = await llm.json_chat([{"role": "user", "content": "Translate 'latte, please' to Korean."}],
                                   {"type": "object", "properties": {"ko": {"type": "string"}}, "required": ["ko"],
                                    "additionalProperties": False}, max_tokens=64)
        assert data["ko"].strip()
