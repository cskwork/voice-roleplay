"""Measure TTS load time, first-chunk latency, RTF, playback stalls and memory on this machine.

    .venv/bin/python bench.py --backend mlx                                   # MLX fp16, defaults
    .venv/bin/python bench.py --backend mlx --flow-steps 10,6,5 --token-hop 25,50  # interleaved comparison
    .venv/bin/python bench.py --backend mlx --variant 8bit
    .venv/bin/python bench.py --backend torch --device hybrid

Engine level (no HTTP/WS), 12 sentences of 10-26 words x 2 voices, streaming and non-streaming. Writes JSON to
benchmarks/<utc>-<backend>.json and the streamed audio to var/tts-bench/<utc>-<backend>/ (gitignored) for
the ASR round trip (asr_roundtrip.py). Never prints the synthesized text. With several MLX flow-step/chunk
settings, the settings take turns on every sentence (rotating order), so they share the same machine
conditions; summaries are per setting.
"""

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import psutil

from tts_worker.backend import create_engine
from tts_worker.logsafe import configure_logging
from tts_worker.textnorm import normalize

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parents[1]
# 12 sentences of 10-26 words, no digits or number words (so ASR number formatting cannot skew the WER).
SENTENCES = json.loads((HERE / "benchmarks" / "sentences.json").read_text())


def footprint_mb() -> float | None:
    """macOS phys_footprint of this process (includes Metal/unified allocations that RSS misses)."""
    try:
        out = subprocess.run(["footprint", str(os.getpid())], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", out)
    return round(float(m[1]) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m[2]]) if m else None


def run_once(engine, text, voice, stream, wav_path=None):
    """Timings of one request. ``stall_s``: time a player starting at the first chunk would wait for audio."""
    t0 = time.perf_counter()
    first, samples, stall, play_end, pcm = None, 0, 0.0, None, []
    for chunk in engine.synthesize(text, voice, 1.0, threading.Event(), stream=stream):
        now = time.perf_counter() - t0
        first = first if first is not None else now
        if play_end is not None and now > play_end:
            stall += now - play_end
        play_end = max(play_end or now, now) + len(chunk) / engine.sample_rate
        samples += len(chunk)
        pcm.append(chunk.tobytes())
    elapsed = time.perf_counter() - t0
    audio = samples / engine.sample_rate
    if wav_path:
        with wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(engine.sample_rate), w.writeframes(b"".join(pcm))
    return {"first_chunk_s": round(first, 3), "elapsed_s": round(elapsed, 3), "audio_s": round(audio, 3),
            "rtf": round(elapsed / audio, 3), "stall_s": round(stall, 3)}


def pct(values, q):
    values = sorted(values)
    return values[round(q * (len(values) - 1))]


def summary(rows):
    rtf, first = [r["rtf"] for r in rows], [r["first_chunk_s"] for r in rows]
    return {"n": len(rows), "rtf_p50": round(statistics.median(rtf), 3), "rtf_p95": pct(rtf, 0.95),
            "rtf_max": max(rtf), "first_chunk_p50_s": round(statistics.median(first), 3),
            "first_chunk_p95_s": pct(first, 0.95), "runs_with_stall": sum(r["stall_s"] > 0 for r in rows),
            "stall_total_s": round(sum(r["stall_s"] for r in rows), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch"])
    ap.add_argument("--variant", default="fp16", help="MLX checkpoint: fp16|8bit")
    ap.add_argument("--flow-steps", default="5", help="MLX only, comma list (upstream: 10)")
    ap.add_argument("--token-hop", default="50", help="MLX only, comma list: first streaming chunk in tokens (upstream: 25)")
    ap.add_argument("--device", default="auto", help="torch only: auto|hybrid|cpu")
    ap.add_argument("--threads", type=int, default=4, help="torch only")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()
    configure_logging()
    engine = create_engine({"VR_TTS_BACKEND": args.backend, "VR_TTS_MLX_VARIANT": args.variant,
                            "VR_TTS_DEVICE": args.device, "VR_TTS_CPU_THREADS": str(args.threads)})
    backend = "mlx" if engine.device == "mlx" else "torch"
    settings = ([(int(s), int(h)) for s in args.flow_steps.split(",") for h in args.token_hop.split(",")]
                if backend == "mlx" else [None])
    engine.load()
    utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    name = f"{utc.replace(':', '')}-{backend}{'-' + args.tag if args.tag else ''}"
    wav_dir = REPO_DIR / "var" / "tts-bench" / name
    wav_dir.mkdir(parents=True, exist_ok=True)
    load_avg_start = [round(x, 1) for x in psutil.getloadavg()]

    rows = []
    for voice in engine.voices:
        for stream in (True, False):
            for i, text in enumerate(SENTENCES):
                norm = normalize(text)
                for run in range(args.runs):
                    k = (i + run) % len(settings)
                    for setting in settings[k:] + settings[:k]:
                        cfg = f"s{setting[0]}-h{setting[1]}" if setting else "default"
                        if setting:
                            engine.flow_steps, engine.token_hop = setting
                        wav = wav_dir / f"{cfg}_{voice}_s{i:02d}_r{run}.wav" if stream else None
                        r = run_once(engine, norm, voice, stream, wav)
                        rows.append({"cfg": cfg, "voice": voice, "stream": stream, "sentence": i,
                                     "words": len(text.split()), "run": run,
                                     "wav": str(wav.relative_to(REPO_DIR)) if wav else None, **r})
                        print(json.dumps({k: v for k, v in rows[-1].items() if k != "wav"}), flush=True)

    chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    result = {
        "utc": utc,
        "machine": {"chip": chip, "ram_gb": round(psutil.virtual_memory().total / 2**30), "os": platform.platform(),
                    "load_avg_start": load_avg_start, "load_avg_end": [round(x, 1) for x in psutil.getloadavg()]},
        "backend": backend, "model_id": engine.model_id, "model_revision": engine.revision,
        "placement": engine.placement,
        "settings": ({"variant": args.variant, "flow_steps": args.flow_steps, "token_hop": args.token_hop}
                     if backend == "mlx" else {"device": args.device, "cpu_threads": args.threads}),
        "note": "RTF = wall time / audio duration per request; stall = playback wait after the first chunk",
        "load_s_incl_warmup": round(engine.load_seconds, 1),
        "rss_mb": round(psutil.Process().memory_info().rss / 2**20),
        "phys_footprint_mb": footprint_mb(),
        "summary": {cfg: {"stream": summary([r for r in rows if r["cfg"] == cfg and r["stream"]]),
                          "non_stream": summary([r for r in rows if r["cfg"] == cfg and not r["stream"]])}
                    for cfg in dict.fromkeys(r["cfg"] for r in rows)},
        "wav_dir": str(wav_dir.relative_to(REPO_DIR)),
        "runs": rows,
    }
    if backend == "mlx":
        import mlx.core as mx
        result["mlx_peak_memory_mb"] = round(mx.get_peak_memory() / 2**20)
    else:
        import torch
        if engine.device == "mps":
            result["mps_driver_allocated_mb"] = round(torch.mps.driver_allocated_memory() / 2**20)
    out = HERE / "benchmarks" / f"{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}, indent=1))
    print(f"written {out}")


if __name__ == "__main__":
    main()
