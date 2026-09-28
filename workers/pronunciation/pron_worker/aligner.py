"""Word-level forced alignment behind one interface: align(16 kHz float32 audio, text) -> [(word, start_s, end_s)]."""
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import LANGUAGE, SAMPLE_RATE

# The aligner keeps letters, digits and apostrophes of each whitespace-separated token (qwen-asr
# Qwen3ForceAlignProcessor.clean_token); text without any of those has nothing to align.
_ALIGNABLE = re.compile(r"[^\W_]", re.UNICODE)


@dataclass(frozen=True)
class AlignedWord:
    i: int
    word: str
    start_ms: int
    end_ms: int

    def to_json(self) -> dict:
        return asdict(self)


class Aligner(Protocol):
    device: str

    def align(self, audio: np.ndarray, text: str) -> list[tuple[str, float, float]]: ...


def has_alignable_text(text: str) -> bool:
    return bool(_ALIGNABLE.search(text))


def align_words(aligner: Aligner, audio: np.ndarray, text: str) -> list[AlignedWord]:
    """Run the aligner and convert to integer ms. Times past the end of the audio are clamped to it
    (the model predicts in 80 ms steps, so the last word can overshoot); order is left as the model gave it."""
    total_ms = len(audio) * 1000 // SAMPLE_RATE
    words = []
    for i, (word, start_s, end_s) in enumerate(aligner.align(audio, text)):
        start = min(max(round(start_s * 1000), 0), total_ms)
        end = min(max(round(end_s * 1000), start), total_ms)
        words.append(AlignedWord(i=i, word=word, start_ms=start, end_ms=end))
    return words


def resolve_device(requested: str) -> str:
    import torch

    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    return requested


class QwenForcedAligner:
    """Qwen/Qwen3-ForcedAligner-0.6B via the official qwen-asr package (`qwen_asr.Qwen3ForcedAligner`).

    One forward pass (no generation): the model predicts a timestamp (in 80 ms steps) for the start and
    end of each word; qwen-asr repairs non-monotonic predictions (longest increasing subsequence + fill).
    """

    def __init__(self, model_dir: Path, device: str):
        import torch
        from qwen_asr import Qwen3ForcedAligner
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        self.device = resolve_device(device)
        # bf16 on MPS (timings matched CPU fp32 on 155 of 156 fixture words, ~3-6x faster); fp32 on CPU.
        dtype = torch.float32 if self.device == "cpu" else torch.bfloat16
        self._model = Qwen3ForcedAligner.from_pretrained(str(model_dir), dtype=dtype, device_map=self.device)

    def align(self, audio: np.ndarray, text: str) -> list[tuple[str, float, float]]:
        result = self._model.align(audio=(audio, SAMPLE_RATE), text=text, language=LANGUAGE)[0]
        return [(it.text, float(it.start_time), float(it.end_time)) for it in result.items]
