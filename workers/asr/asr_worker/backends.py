"""Model backends behind one interface: transcribe(list of 16 kHz float32 mono arrays, context) -> texts."""
import logging
import sys
from pathlib import Path
from typing import Protocol

import numpy as np

from .config import LANGUAGE, SAMPLE_RATE, ConfigError

log = logging.getLogger("asr.backend")


class Backend(Protocol):
    name: str
    device: str

    def transcribe(self, audios: list[np.ndarray], context: str) -> list[str]: ...


def resolve_device(requested: str) -> str:
    import torch

    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    return requested


class TransformersBackend:
    name = "transformers"

    def __init__(self, model_dir: Path, device: str):
        import torch
        from qwen_asr import Qwen3ASRModel
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        self.device = resolve_device(device)
        # Measured on M3 Pro: bf16 on MPS ~= fp16 and ~1.4x faster than fp32; bf16 on CPU is ~3x slower than fp32.
        dtype = torch.float32 if self.device == "cpu" else torch.bfloat16
        self._model = Qwen3ASRModel.from_pretrained(
            str(model_dir),
            dtype=dtype,
            device_map=self.device,
            max_inference_batch_size=4,
            max_new_tokens=512,
        )
        gen_cfg = self._model.model.generation_config
        if gen_cfg.pad_token_id is None:
            gen_cfg.pad_token_id = gen_cfg.eos_token_id

    def transcribe(self, audios: list[np.ndarray], context: str) -> list[str]:
        results = self._model.transcribe(
            audio=[(a, SAMPLE_RATE) for a in audios], context=context, language=LANGUAGE
        )
        return [r.text for r in results]


class VllmBackend:
    """UNTESTED path for Linux/NVIDIA hosts (PROTOCOL §0). Never exercised on the macOS dev machine.

    Uses qwen-asr's vLLM backend for full decodes; the worker still produces partials by incremental
    re-decoding (qwen-asr's native vLLM streaming API is not wired in).
    Requires `pip install qwen-asr[vllm]` in the worker venv.
    """

    name = "vllm"

    def __init__(self, model_dir: Path, device: str):
        if sys.platform == "darwin":
            raise ConfigError("ASR_BACKEND=vllm needs Linux + NVIDIA CUDA; use ASR_BACKEND=transformers on macOS.")
        from qwen_asr import Qwen3ASRModel

        self.device = "cuda"
        self._model = Qwen3ASRModel.LLM(model=str(model_dir), gpu_memory_utilization=0.3, max_new_tokens=512)

    def transcribe(self, audios: list[np.ndarray], context: str) -> list[str]:
        results = self._model.transcribe(
            audio=[(a, SAMPLE_RATE) for a in audios], context=context, language=LANGUAGE
        )
        return [r.text for r in results]


def load_backend(kind: str, model_dir: Path, device: str) -> Backend:
    if kind == "transformers":
        return TransformersBackend(model_dir, device)
    if kind == "vllm":
        return VllmBackend(model_dir, device)
    raise ConfigError(f"Unknown ASR_BACKEND={kind!r} (expected 'transformers' or 'vllm').")
