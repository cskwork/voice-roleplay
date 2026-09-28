"""Baseline: the v0.1 reading diff (Qwen3-ASR transcript vs target, vr_feedback.reading.diff_target).

Mirrors the product path for reading exercises (contracts/PROTOCOL.md §7): the target is NOT given to ASR as
context, near-silent audio is not sent to the model (worker behaviour), and a target word counts as flagged when
the diff marks it "missing" or "different". It produces flags only, no scores: in the product these flags are
"다르게 인식된 부분", never a pronunciation judgement (PRD §8.2). Benchmark use only.

Runs the ASR model in-process (no live worker): use workers/asr/.venv/bin/python. Env: ASR_MODEL_DIR,
ASR_DEVICE (default auto), ASR_SILENCE_DBFS (default -40), same meaning as in the worker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parents[3]
for p in (ROOT / "workers" / "asr", ROOT / "workers" / "feedback" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from asr_worker.audio import is_silent  # noqa: E402
from asr_worker.config import MODEL_ID, MODEL_REVISION  # noqa: E402
from vr_feedback.reading import diff_target  # noqa: E402
from vr_feedback.textutil import words as split_words  # noqa: E402


def flags_per_word(diff: list[dict], ref_words: list[str]) -> list[bool]:
    """Map diff items that carry a target token back onto ref_words (one ref word may hold several tokens)."""
    target_ops = [d["op"] for d in diff if d["target"] is not None]
    flags, k = [], 0
    for w in ref_words:
        n = len(split_words(w))
        flags.append(any(op != "equal" for op in target_ops[k : k + n]))
        k += n
    if k != len(target_ops):
        raise ValueError("reference words and diff target tokens do not line up")
    return flags


class AsrDiffScorer:
    name = "asr_diff"

    def __init__(self) -> None:
        from asr_worker.backends import TransformersBackend

        model_dir = Path(os.environ.get("ASR_MODEL_DIR", ROOT / "models" / "Qwen3-ASR-0.6B"))
        self.silence_dbfs = float(os.environ.get("ASR_SILENCE_DBFS", "-40"))
        self.backend = TransformersBackend(model_dir, os.environ.get("ASR_DEVICE", "auto"))

    def info(self) -> dict:
        return {
            "method": "Qwen3-ASR transcript (no context) + vr_feedback.reading.diff_target; flags only",
            "asr_model": MODEL_ID,
            "asr_revision": MODEL_REVISION,
            "device": self.backend.device,
            "silence_dbfs": self.silence_dbfs,
        }

    def score(self, wav, reference_text: str, ref_words) -> dict:
        text = "" if is_silent(wav, self.silence_dbfs) else self.backend.transcribe([wav], "")[0].strip()
        diff = diff_target(reference_text, text)
        flags = flags_per_word(diff, [w.text for w in ref_words])
        return {
            "words": [{"accuracy": None, "flagged": f, "phones": None} for f in flags],
            "extra": {"transcript": text, "extra_words": sum(d["op"] == "extra" for d in diff)},
        }
