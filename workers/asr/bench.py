"""In-process ASR benchmark on the real model: load time, warm full-decode latency, partial lag, memory.

    uv run python bench.py [--device auto|mps|cpu] [--out results.json]

Uses the fixtures from tests/fixtures/make_fixtures.sh. Prints JSON; never prints transcripts.
"""
import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import psutil  # noqa: E402

from asr_worker.audio import pcm16_to_float  # noqa: E402
from asr_worker.config import REPO_ROOT  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "audio"
SR = 16000


def load_pcm(name: str) -> bytes:
    with wave.open(str(FIXTURES / f"{name}.wav")) as w:
        return w.readframes(w.getnframes())


def phys_footprint_mb() -> float | None:
    """macOS phys_footprint (includes GPU/unified allocations that RSS misses)."""
    try:
        out = subprocess.run(["footprint", str(os.getpid())], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", out)
    if not m:
        return None
    return round(float(m[1]) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m[2]], 1)


async def stream_lag(engine, pcm: bytes) -> dict:
    from asr_worker.stream import StreamSession

    received: list[tuple[float, dict]] = []

    async def send(msg):
        received.append((time.monotonic(), msg))

    session = StreamSession(engine, send)
    step = SR * 20 // 1000 * 2
    t0 = time.monotonic()
    for k, i in enumerate(range(0, len(pcm), step)):
        session.add_audio(pcm[i : i + step])
        await asyncio.sleep(max(0.0, t0 + (k + 1) * 0.02 - time.monotonic()))
    committed = time.monotonic()
    await session.commit()
    lags = sorted((t - (t0 + m["audio_ms"] / 1000)) * 1000 for t, m in received if m["type"] == "partial")
    return {
        "audio_s": round(len(pcm) / 2 / SR, 2),
        "partials_sent": len(lags),
        "partial_decodes": session.partial_decodes,
        "partial_lag_ms_median": round(statistics.median(lags)) if lags else None,
        "partial_lag_ms_max": round(lags[-1]) if lags else None,
        "commit_to_final_ms": round((received[-1][0] - committed) * 1000),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="auto")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out")
    args = ap.parse_args()

    t = time.monotonic()
    import torch

    from asr_worker.backends import TransformersBackend
    from asr_worker.engine import AsrEngine

    import_s = time.monotonic() - t
    t = time.monotonic()
    backend = TransformersBackend(REPO_ROOT / "models" / "Qwen3-ASR-0.6B", args.device)
    load_s = time.monotonic() - t
    engine = AsrEngine(backend, -40)
    t = time.monotonic()
    engine.warmup()
    warmup_s = time.monotonic() - t

    passage = load_pcm("passage_30s")
    inputs = {"5s": passage[: 5 * SR * 2], "30s(28.5s)": passage}
    full = {}
    for label, pcm in inputs.items():
        audio = pcm16_to_float(pcm)
        times = []
        for _ in range(args.runs + 1):  # first run warms this input shape
            t = time.monotonic()
            await engine.transcribe(audio, "")
            times.append((time.monotonic() - t) * 1000)
        full[label] = {"median_ms": round(statistics.median(times[1:])), "first_ms": round(times[0])}

    streams = {name: await stream_lag(engine, load_pcm(name)) for name in ("mid_pause", "passage_30s")}

    result = {
        "machine": f"{platform.machine()} {platform.mac_ver()[0]} {psutil.cpu_count()} cpu {round(psutil.virtual_memory().total / 2**30)} GB",
        "device": backend.device,
        "dtype": str(next(backend._model.model.parameters()).dtype),
        "torch": torch.__version__,
        "import_s": round(import_s, 2),
        "model_load_s": round(load_s, 2),
        "warmup_s": round(warmup_s, 2),
        "warm_full_decode": full,
        "stream_realtime": streams,
        "rss_mb": round(psutil.Process().memory_info().rss / 2**20),
        "phys_footprint_mb": phys_footprint_mb(),
        "mps_driver_allocated_mb": round(torch.mps.driver_allocated_memory() / 2**20) if backend.device == "mps" else None,
        "note": "other GPU workloads may have been running concurrently; see report",
    }
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n")
    engine.gate.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
