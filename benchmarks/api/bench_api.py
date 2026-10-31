"""S0 measurement for API mode: final-transcript latency and WER, TTS first byte, LLM first token.

    services/gateway/.venv/bin/python benchmarks/api/bench_api.py [--only asr,tts,llm] [--fast] [--out DIR]

Needs OPENAI_API_KEY (ASR) and OPENROUTER_API_KEY (LLM and TTS) in the environment (DEEPGRAM_API_KEY instead for TTS
with VR_API_TTS_PROVIDER=deepgram); a component whose key is missing is skipped, and with no key at all the script
exits with code 2 before any network call.
Calls are billed by the providers (one run: 12 short clips ~ 0.05 USD of transcription, ~1.5k characters of TTS,
~6 short chat completions).

Inputs are the repo's synthetic clips `tests/fixtures/audio/e2e_cafe_*.wav` (macOS `say`, 16 kHz) with their `.txt`
references; they are not learner speech, so WER here says nothing about Korean-accented English.

- ASR: each clip is streamed through the same adapter the gateway uses (`OpenAiAsr`), paced in real time unless
  `--fast`; "final latency" = commit sent -> final transcript received. WER is word-level after lowercasing and
  removing punctuation.
- TTS: `OpenRouterTts` (Deepgram Aura-2 through OpenRouter; `DeepgramTts` with VR_API_TTS_PROVIDER=deepgram)
  streaming session; "first byte" = request sent -> first audio frame. Also records the sample rate(s) the provider
  answered with, total time per sentence (first byte close to total = not streamed progressively) and whether the
  slow sentence's `speed` was accepted - the points not yet verified against the real OpenRouter endpoint.
- LLM: `LlmClient.openrouter` streaming with the cafe scenario's roleplay prompt; "first token" = request sent ->
  first content delta.

Writes `<out>/<date>-api.json` (default benchmarks/api/results) and prints p50/p95. Never prints keys, transcripts
of anything but the synthetic clips, or prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/gateway/src"))
sys.path.insert(0, str(ROOT / "workers/feedback/src"))

# Speech goes through OpenRouter by default; VR_API_TTS_PROVIDER=deepgram measures Deepgram directly (opt-in fallback).
TTS_PROVIDER = os.environ.get("VR_API_TTS_PROVIDER", "").strip().lower() or "openrouter"
KEYS = {"asr": "OPENAI_API_KEY", "tts": "DEEPGRAM_API_KEY" if TTS_PROVIDER == "deepgram" else "OPENROUTER_API_KEY",
        "llm": "OPENROUTER_API_KEY"}
CLIPS = sorted((ROOT / "tests/fixtures/audio").glob("e2e_cafe_*.wav"))
TTS_SENTENCES = [
    "Hi there! What can I get for you today?",
    "Sure, one large latte coming right up.",
    "That will be four dollars and fifty cents.",
    "Would you like anything else with that?",
    "Here is your receipt. Have a great day!",
]


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = words(reference), words(hypothesis)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(1, len(ref))


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 0.5), "p95": pct(values, 0.95),
            "mean": round(statistics.fmean(values), 1) if values else None}


def read_pcm16(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            raise SystemExit(f"{path.name}: expected 16 kHz mono PCM16")
        return w.readframes(w.getnframes())


async def bench_asr(fast: bool) -> dict:
    from vr_gateway.adapters.asr_openai import OpenAiAsr

    asr = OpenAiAsr(os.environ["OPENAI_API_KEY"])
    latencies, wers, rows = [], [], []
    for clip in CLIPS:
        pcm = read_pcm16(clip)
        reference = clip.with_suffix(".txt").read_text().strip()
        first_partial: list[float] = []
        started = time.perf_counter()
        stream = await asr.open_stream(lambda _t: first_partial.append(time.perf_counter()) if not first_partial else None)
        for i in range(0, len(pcm), 640):  # 20 ms frames like the gateway
            await stream.send_audio(pcm[i : i + 640])
            if not fast:
                await asyncio.sleep(0.02)
        committed = time.perf_counter()
        final = await stream.commit(timeout_s=30)
        latency = (time.perf_counter() - committed) * 1000
        error = wer(reference, final["text"])
        latencies.append(latency)
        wers.append(error)
        rows.append({"clip": clip.name, "final_ms": round(latency), "wer": round(error, 3),
                     "first_partial_ms": round((first_partial[0] - started) * 1000) if first_partial else None,
                     "reference": reference, "hypothesis": final["text"]})
        print(f"  asr {clip.name}: final {latency:.0f} ms, WER {error:.2f}", flush=True)
    return {"model": f"openai/{asr.model}", "paced": not fast, "final_ms": summary(latencies),
            "wer_mean": round(statistics.fmean(wers), 3) if wers else None, "clips": rows}


def make_tts():
    if TTS_PROVIDER == "deepgram":
        from vr_gateway.adapters.tts_deepgram import DeepgramTts

        return DeepgramTts(os.environ["DEEPGRAM_API_KEY"], ROOT / "config/voices.api.json"), "deepgram/aura-2-thalia-en"
    from vr_gateway.adapters.tts_openrouter import OpenRouterTts

    tts = OpenRouterTts(os.environ["OPENROUTER_API_KEY"], ROOT / "config/voices.openrouter.json")
    return tts, f"openrouter:{tts.model} (aura-2-thalia-en)"


async def bench_tts() -> dict:
    tts, model = make_tts()
    stream = tts.stream_session()
    firsts, totals, rtf, rates = [], [], [], set()
    try:
        # The last sentence is slow (0.85, the learner's "slow" setting): shows whether `speed` is accepted.
        for i, text in enumerate(TTS_SENTENCES):
            speed = 0.85 if i == len(TTS_SENTENCES) - 1 else 1.0
            started = time.perf_counter()
            first = None
            async for kind, value in stream.synthesize(f"bench-{i}", "aura2_thalia_f", text, speed):
                if kind == "start":
                    rates.add(value)
                elif kind == "audio" and first is None:
                    first = (time.perf_counter() - started) * 1000
                elif kind == "done":
                    total = (time.perf_counter() - started) * 1000
                    totals.append(total)
                    audio_ms = value["audio_ms"] or 1
                    rtf.append(total / audio_ms)
            firsts.append(first or 0.0)
            print(f"  tts sentence {i + 1}: first byte {first:.0f} ms", flush=True)
    finally:
        await stream.close()
        await tts.aclose()
    # first byte close to total = the provider sent the audio in one piece rather than streaming it.
    return {"model": model, "first_byte_ms": summary(firsts), "total_ms": summary(totals),
            "rtf": summary([round(x, 3) for x in rtf]), "sample_rates": sorted(rates),
            "speed_supported": getattr(tts, "speed_supported", True)}


async def bench_llm() -> dict:
    from vr_feedback.llm import LlmClient
    from vr_feedback.prompts import build_roleplay_messages

    scenario = json.loads((ROOT / "content/scenarios/cafe_order.json").read_text())
    model = os.environ.get("VR_API_LLM_MODEL", "openai/gpt-4.1-mini")
    llm = LlmClient.openrouter(os.environ["OPENROUTER_API_KEY"], model)
    turns = ["Hi, can I get a latte?", "A large one, please.", "With oat milk.", "How much is it?",
             "I'll pay by card.", "Thank you!"]
    firsts = []
    history: list[dict] = []
    try:
        for turn in turns:
            goals = [{"goal_id": g["goal_id"], "status": "pending"} for g in scenario.get("goals", [])]
            messages = build_roleplay_messages(scenario, "normal", goals, history, turn)
            started = time.perf_counter()
            first = None
            reply = ""
            async for delta in llm.stream_chat(messages, max_tokens=128):
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                reply += delta
            firsts.append(first or 0.0)
            history += [{"role": "user", "text": turn}, {"role": "assistant", "text": reply}]
            print(f"  llm turn {len(firsts)}: first token {first:.0f} ms", flush=True)
    finally:
        await llm.aclose()
    return {"model": f"openrouter:{model}", "first_token_ms": summary(firsts)}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default="asr,tts,llm")
    parser.add_argument("--fast", action="store_true", help="send ASR audio as fast as possible instead of real time")
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks/api/results")
    args = parser.parse_args()
    wanted = [c.strip() for c in args.only.split(",") if c.strip()]
    missing = [KEYS[c] for c in wanted if not os.environ.get(KEYS[c], "").strip()]
    runnable = [c for c in wanted if os.environ.get(KEYS[c], "").strip()]
    if not runnable:
        print(f"No API keys set ({', '.join(missing)}): nothing measured, no network calls made.\n"
              "Export the keys for the components to measure and run again.", file=sys.stderr)
        return 2
    if missing:
        print(f"Skipping components without keys: {', '.join(missing)}")
    results: dict = {"date": time.strftime("%Y-%m-%d"), "machine": os.uname().machine, "components": {}}
    runners = {"asr": lambda: bench_asr(args.fast), "tts": bench_tts, "llm": bench_llm}
    for component in runnable:
        print(f"== {component}", flush=True)
        results["components"][component] = await runners[component]()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{results['date']}-api.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for name, data in results["components"].items():
        metric = {"asr": "final_ms", "tts": "first_byte_ms", "llm": "first_token_ms"}[name]
        extra = f", WER mean {data['wer_mean']}" if name == "asr" else ""
        print(f"{name}: {metric} p50 {data[metric]['p50']:.0f} ms, p95 {data[metric]['p95']:.0f} ms{extra}")
    print(f"saved {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
