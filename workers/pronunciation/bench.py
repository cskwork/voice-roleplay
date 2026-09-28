"""In-process benchmark on the real models: aligner and GOP scorer load time, warm /align, /assess (scoring part) and
prosody latency, memory.

    uv run python bench.py [--device auto|mps|cpu] [--runs 5] [--out benchmarks/<name>.json]

Uses the `say` fixtures from tests/fixtures/make_fixtures.sh (price ~6.2 s, passage_30s ~28.5 s). Prints JSON;
never prints text or timings of individual words.
"""
import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import time
from datetime import datetime, timezone

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import numpy as np  # noqa: E402
import psutil  # noqa: E402
from pron_worker import prosody  # noqa: E402
from pron_worker.aligner import QwenForcedAligner, align_words  # noqa: E402
from pron_worker.config import ALIGNER_ID, ALIGNER_REVISION, PHONES_ID, PHONES_REVISION, REPO_ROOT, Settings  # noqa: E402
from pron_worker.gop import CtcGopScorer  # noqa: E402
from tests.helpers import fixture_pcm, fixture_text  # noqa: E402


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


def memory() -> dict:
    return {"rss_mb": round(psutil.Process().memory_info().rss / 2**20), "phys_footprint_mb": phys_footprint_mb()}


def ms_stats(values: list[float]) -> dict:
    return {"runs_ms": [round(v) for v in values], "median_ms": round(statistics.median(values))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="auto")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args()

    result: dict = {
        "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": f"{platform.machine()} {platform.mac_ver()[0] or platform.platform()}",
        "model": {"id": ALIGNER_ID, "revision": ALIGNER_REVISION},
        "phones_model": {"id": PHONES_ID, "revision": PHONES_REVISION},
        "note": "Other processes may share the machine; numbers are indicative. Fixtures are macOS `say` speech.",
        "memory_before_load": memory(),
    }
    started = time.monotonic()
    aligner = QwenForcedAligner(REPO_ROOT / "models" / "Qwen3-ForcedAligner-0.6B", args.device)
    result["device"] = aligner.device
    result["load_ms"] = round((time.monotonic() - started) * 1000)
    started = time.monotonic()
    aligner.align((np.random.default_rng(0).standard_normal(16000) * 0.05).astype(np.float32), "hello")
    result["warmup_ms"] = round((time.monotonic() - started) * 1000)
    result["memory_after_load"] = memory()
    started = time.monotonic()
    settings = Settings(token="-", port=0, model_dir=None, device=args.device, experimental=False,
                        phones_dir=REPO_ROOT / "models" / "wav2vec2-lv-60-espeak-cv-ft",
                        lexicon_path=REPO_ROOT / "models" / "cmudict" / "cmudict.dict", calibration_path=None)
    scorer = CtcGopScorer(settings, args.device)
    result["scorer_load_ms"] = round((time.monotonic() - started) * 1000)
    result["scorer_device"] = scorer.model.device
    result["memory_after_scorer_load"] = memory()

    for name in ("price", "passage_30s", "passage_30s_x4"):
        base = name.removesuffix("_x4")
        reps = 4 if name.endswith("_x4") else 1
        pcm, text = fixture_pcm(base) * reps, " ".join([fixture_text(base)] * reps)
        audio = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        key = f"{name}_{len(audio) / 16000:.1f}s"
        words = align_words(aligner, audio, text)
        times = []
        for _ in range(args.runs if reps == 1 else 1):
            t = time.monotonic()
            out = scorer.assess(audio, text, words, "scripted")
            times.append((time.monotonic() - t) * 1000)
        gops = [p["gop"] for w in out for p in w["phones"]]
        result[f"assess_scoring_{key}"] = {
            **ms_stats(times), "units": len(gops), "mean_gop": round(float(np.mean(gops)), 3),
            "top1_is_expected": round(float(np.mean([p["heard_candidates"][0]["ipa"] == p["expected_ipa"]
                                                    for w in out for p in w["phones"]])), 3),
            **({"gops": gops} if name == "price" else {}),  # numbers only, for comparing devices
        }
    result["memory_after_assess"] = memory()

    for name in ("price", "passage_30s"):
        pcm, text = fixture_pcm(name), fixture_text(name)
        audio = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
        key = f"{name}_{len(audio) / 16000:.1f}s"
        times = []
        for _ in range(args.runs):
            t = time.monotonic()
            words = align_words(aligner, audio, text)
            times.append((time.monotonic() - t) * 1000)
        result[f"align_{key}"] = {**ms_stats(times), "words": len(words)}
        times = []
        for _ in range(min(args.runs, 3)):
            t = time.monotonic()
            prosody.f0_contour(audio)
            times.append((time.monotonic() - t) * 1000)
        result[f"prosody_{key}"] = ms_stats(times)
    result["memory_after_runs"] = memory()

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
