"""Intelligibility check: transcribe bench.py's streamed WAVs with the ASR worker's model and report WER.

    ../asr/.venv/bin/python asr_roundtrip.py benchmarks/<run>.json [...]

Runs in the ASR worker's venv (Qwen3-ASR-0.6B, transformers backend, offline). Adds ``asr`` (per-run word errors)
and ``asr_summary`` to each JSON file. Transcripts are compared in memory and never printed or stored.
"""

import json
import os
import re
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parents[1]
sys.path.insert(0, str(REPO_DIR / "workers" / "asr"))

from asr_worker.backends import TransformersBackend  # noqa: E402

SENTENCES = json.loads((HERE / "benchmarks" / "sentences.json").read_text())


def words(text: str) -> list[str]:
    text = text.lower().replace("-", " ").replace("’", "'")
    return re.sub(r"[^a-z' ]+", " ", text).split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1]


def wer(runs: list[dict]) -> dict:
    by_voice = {v: [p for p in runs if p["voice"] == v] for v in sorted({p["voice"] for p in runs})}
    return {"n": len(runs), "wer": round(sum(p["errors"] for p in runs) / sum(p["ref_words"] for p in runs), 4),
            "sentences_with_errors": sum(p["errors"] > 0 for p in runs),
            "by_voice": {v: round(sum(p["errors"] for p in ps) / sum(p["ref_words"] for p in ps), 4)
                         for v, ps in by_voice.items()}}


def main():
    backend = TransformersBackend(REPO_DIR / "models" / "Qwen3-ASR-0.6B", "auto")
    for path in map(Path, sys.argv[1:]):
        result = json.loads(path.read_text())
        runs = [r for r in result["runs"] if r.get("wav")]
        per_run = []
        for r in runs:
            audio, sr = sf.read(str(REPO_DIR / r["wav"]), dtype="float32")
            audio = resample_poly(audio, 16000, sr).astype(np.float32)
            ref = words(SENTENCES[r["sentence"]])
            e = edit_distance(ref, words(backend.transcribe([audio], "")[0]))
            per_run.append({"cfg": r.get("cfg", "default"), "voice": r["voice"], "sentence": r["sentence"],
                            "errors": e, "ref_words": len(ref)})
        result["asr"] = per_run
        result["asr_summary"] = {"model": "Qwen/Qwen3-ASR-0.6B",
                                 **{cfg: wer([p for p in per_run if p["cfg"] == cfg]) for cfg in dict.fromkeys(
                                     p["cfg"] for p in per_run)}}
        path.write_text(json.dumps(result, indent=1))
        print(path.name, json.dumps(result["asr_summary"]))


if __name__ == "__main__":
    main()
