"""Measure roleplay TTFT (cold vs warm prefix cache), decode speed and feedback JSON latency
against a running llama-server. Prints a summary and writes JSON (no prompts or model text).

    .venv/bin/python bench/bench_llm.py [--url http://127.0.0.1:8713] [--trials 5] [--out results.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from conftest import CAFE  # noqa: E402

from vr_feedback.feedback import generate_feedback, session_summary  # noqa: E402
from vr_feedback.goals import evaluate_goals  # noqa: E402
from vr_feedback.llm import LlmClient  # noqa: E402
from vr_feedback.prompts import build_opening_warmup, build_roleplay_messages  # noqa: E402

USER_1 = "Hi. Yesterday I go to other cafe but today I want a latte."
USER_2 = "Large with oat milk, please. How much is it?"
TURNS = [
    {"turn_id": "t1", "text": "Hi, I want a latte.", "transcript_revision": 1},
    {"turn_id": "t2", "text": "Large with oat milk please.", "transcript_revision": 1},
    {"turn_id": "t3", "text": "How much it is?", "transcript_revision": 1},
]


def fresh_scenario() -> dict:
    # A unique title changes the system prompt near its start, so nothing is cached for it yet.
    return {**CAFE, "title_en": f"{CAFE['title_en']} ({uuid.uuid4().hex[:8]})"}


async def timed_stream(llm: LlmClient, messages: list[dict], **kw) -> dict:
    t0 = time.perf_counter()
    ttft = None
    n_chunks = 0
    async for _ in llm.stream_chat(messages, **kw):
        if ttft is None:
            ttft = time.perf_counter() - t0
        n_chunks += 1
    total = time.perf_counter() - t0
    tm = llm.last_timings or {}
    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "total_ms": round(total * 1000, 1),
        "prompt_n": tm.get("prompt_n"),
        "cache_n": tm.get("cache_n"),
        "predicted_n": tm.get("predicted_n"),
        "decode_tok_s": round(tm["predicted_per_second"], 1) if tm.get("predicted_per_second") else None,
        "prompt_tok_s": round(tm["prompt_per_second"], 1) if tm.get("prompt_per_second") else None,
    }


async def timed(coro) -> float:
    t0 = time.perf_counter()
    await coro
    return round((time.perf_counter() - t0) * 1000, 1)


def summarize(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {}
    return {"median": round(statistics.median(vals), 1), "min": min(vals), "max": max(vals), "n": len(vals)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("VR_LLM_URL", "http://127.0.0.1:8713"))
    ap.add_argument("--api-key", default=os.environ.get("VR_LLM_API_KEY"))
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args()

    llm = LlmClient(args.url, api_key=args.api_key)
    if not await llm.health():
        sys.exit(f"llama-server not reachable at {args.url}")

    cold, warm_first, second_turn, warm_ms, long_gen, fb_ms, goals_ms, summary_ms = [], [], [], [], [], [], [], []
    for _ in range(args.trials):
        sc = fresh_scenario()
        cold.append(await timed_stream(llm, build_roleplay_messages(sc, "normal", [], [], USER_1), slot_id=0))

        sc = fresh_scenario()
        warm_ms.append(await timed(llm.warm(build_opening_warmup(sc, "normal"), slot_id=0)))
        first = await timed_stream(llm, build_roleplay_messages(sc, "normal", [], [], USER_1), slot_id=0)
        warm_first.append(first)
        history = [{"role": "user", "text": USER_1}, {"role": "assistant", "text": "Sure, one latte. Regular or large?"}]
        second_turn.append(await timed_stream(llm, build_roleplay_messages(sc, "normal", [], history, USER_2), slot_id=0))

        long_gen.append(await timed_stream(
            llm, [{"role": "user", "content": "Describe a busy cafe morning in about 150 words."}], max_tokens=128, slot_id=1))

        fb_ms.append(await timed(generate_feedback(
            llm, transcript="Yesterday I go to a cafe.", transcript_revision=1, source={"attempt_id": "bench"},
            context={"exercise_type": "free_answer"}, model_revision="bench")))
        goals_ms.append(await timed(evaluate_goals(llm, CAFE, TURNS)))
        summary_ms.append(await timed(session_summary(llm, turns=TURNS, scenario=CAFE, model_revision="bench")))
    await llm.aclose()

    ver = subprocess.run(["llama-server", "--version"], capture_output=True, text=True)
    result = {
        "benchmark": "llm_roleplay_feedback",
        "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": f"{platform.machine()} {platform.mac_ver()[0]} " + subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
        "llama_server": (ver.stdout + ver.stderr).strip().splitlines()[-2:],
        "model": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "server_flags": "-c 8192 -np 2 --no-kv-unified --jinja -ngl 99 --no-webui --cache-prompt",
        "trials": args.trials,
        "roleplay_cold_prefix": {"ttft_ms": summarize(cold, "ttft_ms"), "total_ms": summarize(cold, "total_ms"),
                                 "prompt_n": summarize(cold, "prompt_n"), "cache_n": summarize(cold, "cache_n")},
        "prefix_warmup_ms": summarize([{"v": v} for v in warm_ms], "v"),
        "roleplay_warm_prefix_first_turn": {"ttft_ms": summarize(warm_first, "ttft_ms"), "total_ms": summarize(warm_first, "total_ms"),
                                            "prompt_n": summarize(warm_first, "prompt_n"), "cache_n": summarize(warm_first, "cache_n")},
        "roleplay_second_turn": {"ttft_ms": summarize(second_turn, "ttft_ms"), "total_ms": summarize(second_turn, "total_ms"),
                                 "prompt_n": summarize(second_turn, "prompt_n"), "cache_n": summarize(second_turn, "cache_n")},
        "roleplay_reply_tokens": summarize(cold + warm_first + second_turn, "predicted_n"),
        "roleplay_decode_tok_s": summarize(cold + warm_first + second_turn, "decode_tok_s"),
        "cold_prompt_eval_tok_s": summarize(cold, "prompt_tok_s"),
        "decode_128_tokens": {"tok_s": summarize(long_gen, "decode_tok_s"), "total_ms": summarize(long_gen, "total_ms")},
        "feedback_json_ms": summarize([{"v": v} for v in fb_ms], "v"),
        "goals_eval_ms": summarize([{"v": v} for v in goals_ms], "v"),
        "session_summary_ms": summarize([{"v": v} for v in summary_ms], "v"),
    }
    text = json.dumps(result, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
