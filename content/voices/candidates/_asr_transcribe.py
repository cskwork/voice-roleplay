"""Helper for evaluate.py, run in the ASR worker's venv: transcribe WAV files with Qwen3-ASR-0.6B (offline).

    workers/asr/.venv/bin/python content/voices/candidates/_asr_transcribe.py a.wav b.wav ...

Writes one JSON list of transcripts to stdout for the calling process (a pipe); nothing is logged or stored.
"""

import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

REPO_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_DIR / "workers" / "asr"))

from asr_worker.backends import TransformersBackend  # noqa: E402


def main():
    backend = TransformersBackend(REPO_DIR / "models" / "Qwen3-ASR-0.6B", "auto")
    out = []
    for path in sys.argv[1:]:
        audio, sr = sf.read(path, dtype="float32", always_2d=True)
        out.append(backend.transcribe([resample_poly(audio.mean(axis=1), 16000, sr).astype(np.float32)], "")[0])
    sys.stdout.write("\n" + json.dumps(out) + "\n")  # last line; libraries may print before it


if __name__ == "__main__":
    main()
