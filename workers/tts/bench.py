"""Measure TTS load time, first-chunk latency, RTF and memory on this machine.

    .venv/bin/python bench.py --device hybrid --runs 3

Writes JSON to benchmarks/<utc>-<device>.json next to this file. Timings are engine-level (no HTTP/WS).
"""

import argparse
import json
import platform
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from tts_worker.engine import Engine
from tts_worker.logsafe import configure_logging
from tts_worker.textnorm import normalize

SENTENCES = [
    "Hi there!",
    "Sure, the latte is $4.50.",
    "I'll call you back if I can't make it, okay?",
    "Could you tell me where the nearest subway station is?",
    "Well, you could take the bus, which is cheaper but slower, or a taxi, which gets you there in twenty minutes.",
]


def run_once(engine, text, voice, stream):
    t0 = time.perf_counter()
    first, samples = None, 0
    for pcm in engine.synthesize(text, voice, 1.0, threading.Event(), stream=stream):
        first = first if first is not None else time.perf_counter() - t0
        samples += len(pcm)
    elapsed = time.perf_counter() - t0
    audio = samples / engine.sample_rate
    return {"first_chunk_s": round(first, 3), "elapsed_s": round(elapsed, 3), "audio_s": round(audio, 3),
            "rtf": round(elapsed / audio, 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="auto")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    configure_logging()
    proc = psutil.Process()
    engine = Engine(device=args.device, cpu_threads=args.threads)
    engine.load()
    import torch

    rows = []
    for voice in engine.voices:
        for stream in (True, False):
            for text in SENTENCES:
                norm = normalize(text)
                for run in range(args.runs):
                    r = run_once(engine, norm, voice, stream)
                    rows.append({"voice": voice, "stream": stream, "chars": len(norm), "run": run, **r})
                    print(json.dumps(rows[-1]), flush=True)

    def summary(stream):
        sel = [r for r in rows if r["stream"] == stream]
        rtf = sorted(r["rtf"] for r in sel)
        first = sorted(r["first_chunk_s"] for r in sel)
        return {"n": len(sel), "rtf_p50": round(statistics.median(rtf), 3), "rtf_p95": rtf[int(0.95 * (len(rtf) - 1))],
                "first_chunk_p50_s": round(statistics.median(first), 3), "first_chunk_p95_s": first[int(0.95 * (len(first) - 1))]}

    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    result = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": {"chip": chip, "ram_gb": round(psutil.virtual_memory().total / 2**30), "os": platform.platform(),
                    "load_avg_at_end": [round(x, 1) for x in psutil.getloadavg()]},
        "torch": torch.__version__, "device": args.device, "placement": engine.placement, "cpu_threads": args.threads,
        "model_revision": engine.revision,
        "load_s_incl_warmup": round(engine.load_seconds, 1),
        "rss_mb": round(proc.memory_info().rss / 2**20),
        "mps_driver_allocated_mb": round(torch.mps.driver_allocated_memory() / 2**20) if engine.device == "mps" else None,
        "stream": summary(True), "non_stream": summary(False), "runs": rows,
    }
    out = Path(__file__).parent / "benchmarks" / f"{result['utc'].replace(':', '')}-{args.device}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}, indent=1))
    print(f"written {out}")


if __name__ == "__main__":
    main()
