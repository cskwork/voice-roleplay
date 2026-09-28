"""Adapter around the vendored Fun-CosyVoice3 inference code.

One model instance, synthesis serialized by a lock. Upstream behaviour we work around here (all
verified against vendor/CosyVoice @ 074ca6dc):
- The model classes pick ``cuda`` or ``cpu`` only; we place modules ourselves (see ``PLACEMENTS``).
- HiFT's F0 predictor runs in float64, which MPS does not support, so HiFT always stays on CPU.
- ``nucleus_sampling`` loops over a device tensor element by element; on MPS every step syncs,
  so sampling runs on a CPU copy of the scores (same algorithm, same distribution).
- ``CosyVoice2Model.tts`` grows ``token_hop_len`` on the instance and never resets it, which would
  make every request after the first wait for 100 tokens before its first chunk; reset per request.
- A closed ``tts`` generator leaves its LLM thread running; our wrapper stops it at the next token,
  and after a cancel the final flow/vocoder pass that ``tts`` would still run is skipped.
- CosyVoice3 needs ``<|endofprompt|>`` in the prompt text (asserted in CosyVoice3LM.inference).
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from .logsafe import configure_logging

REPO_DIR = Path(__file__).resolve().parents[3]
VENDOR_DIR = REPO_DIR / "vendor" / "CosyVoice"
DEFAULT_MODEL_DIR = REPO_DIR / "models" / "Fun-CosyVoice3-0.5B-2512"
DEFAULT_VOICES_DIR = REPO_DIR / "content" / "voices"
MODEL_ID = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"  # from vendor example.py (cosyvoice3_example)
MIN_SPEED, MAX_SPEED = 0.5, 2.0  # range offered by vendor webui.py
REQUIRED_MODEL_FILES = ("cosyvoice3.yaml", "llm.pt", "flow.pt", "hift.pt", "campplus.onnx",
                        "speech_tokenizer_v3.onnx", "CosyVoice-BlankEN/model.safetensors")
# device -> (llm, flow, hift). "hybrid" measured fastest on M3 Pro (see workers/tts/README.md).
# LLM and flow both on MPS crashes (Metal command-buffer assertion: they run in different threads).
PLACEMENTS = {"cpu": ("cpu", "cpu", "cpu"), "hybrid": ("cpu", "mps", "cpu")}

log = logging.getLogger("vr.tts.engine")


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Voice:
    voice_id: str
    label: str
    license_note: str
    mode: str  # "zero_shot" or "cross_lingual"
    prompt_wav: Path
    prompt_text: str


def load_voices(voices_dir: Path) -> dict[str, Voice]:
    voices = {}
    for d in sorted(p for p in voices_dir.iterdir() if p.is_dir()):
        meta = json.loads((d / "voice.json").read_text(encoding="utf-8"))
        mode = meta.get("mode", "zero_shot")
        if mode not in ("zero_shot", "cross_lingual"):
            raise SetupError(f"voice {d.name}: unknown mode")
        voices[d.name] = Voice(d.name, meta["label"], meta["license_note"], mode,
                               d / "prompt.wav", (d / "prompt.txt").read_text(encoding="utf-8").strip())
    if not voices:
        raise SetupError(f"no voices in {voices_dir}")
    return voices


def model_revision(model_dir: Path, filename: str = "cosyvoice3.yaml") -> str | None:
    """Commit recorded by huggingface_hub when the snapshot was downloaded."""
    meta = model_dir / ".cache" / "huggingface" / "download" / f"{filename}.metadata"
    return meta.read_text().split()[0] if meta.exists() else None


def to_pcm16(speech) -> np.ndarray:
    return (np.clip(speech.numpy().ravel(), -1.0, 1.0) * 32767).astype("<i2")


class Engine:
    model_id = MODEL_ID

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR, voices_dir: Path = DEFAULT_VOICES_DIR,
                 device: str = "auto", cpu_threads: int = 4):
        self.model_dir = Path(model_dir)
        self.voices = load_voices(Path(voices_dir))
        self.requested_device = device
        self.cpu_threads = cpu_threads
        self.revision = model_revision(self.model_dir)
        self.placement: dict[str, str] = {}
        self.sample_rate: int | None = None
        self.load_seconds: float | None = None
        self._cv = None
        self._ready = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._llm_idle = threading.Event()
        self._llm_idle.set()

    @property
    def ready(self) -> bool:
        return self._ready

    def load(self) -> None:
        missing = [f for f in REQUIRED_MODEL_FILES if not (self.model_dir / f).exists()]
        if missing:
            raise SetupError(f"model files missing in {self.model_dir}: {missing}; download {MODEL_ID} first")
        if not (VENDOR_DIR / "cosyvoice").exists():
            raise SetupError(f"{VENDOR_DIR} missing; run workers/tts/setup.sh")
        for key, value in (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"), ("TQDM_DISABLE", "1"),
                           ("TOKENIZERS_PARALLELISM", "false"), ("PYTORCH_ENABLE_MPS_FALLBACK", "1")):
            os.environ.setdefault(key, value)
        for p in (VENDOR_DIR / "third_party" / "Matcha-TTS", VENDOR_DIR):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        sys.modules.setdefault("wetext", None)  # its Normalizer downloads FSTs at runtime; we normalize ourselves

        t0 = time.perf_counter()
        import torch
        from cosyvoice.cli.cosyvoice import CosyVoice3

        configure_logging()  # vendor import ran logging.basicConfig(DEBUG); replace its handler
        torch.set_num_threads(self.cpu_threads)
        device = self.requested_device
        if device == "auto":
            device = "hybrid" if torch.backends.mps.is_available() else "cpu"
        if device not in PLACEMENTS:
            raise SetupError(f"unknown device {device!r}; use auto|cpu|hybrid")
        llm_dev, flow_dev, hift_dev = PLACEMENTS[device]

        cv = CosyVoice3(str(self.model_dir))
        model = cv.model
        model.llm.to(llm_dev)
        model.flow.to(flow_dev)
        model.hift.to(hift_dev)
        model.device = torch.device(flow_dev)  # token2wav moves flow inputs to model.device
        self._patch(model, torch, torch.device(llm_dev))
        for v in self.voices.values():
            cv.add_zero_shot_spk(PROMPT_PREFIX + v.prompt_text, str(v.prompt_wav), v.voice_id)
        self._cv = cv
        self.sample_rate = cv.sample_rate
        self.placement = {"llm": llm_dev, "flow": flow_dev, "hift": hift_dev}
        for _ in self.synthesize("Hello there.", next(iter(self.voices)), 1.0, threading.Event()):
            pass  # warm-up: first MPS run compiles kernels
        self.load_seconds = time.perf_counter() - t0
        self._ready = True
        log.info("model_loaded device=%s placement=%s load_s=%.1f", device, self.placement, self.load_seconds)

    @property
    def device(self) -> str:
        return self.placement.get("flow", "cpu")

    def _patch(self, model, torch, llm_device) -> None:
        llm = model.llm
        upstream_sampling, upstream_inference = llm.sampling, llm.inference
        upstream_hift = model.hift.inference
        upstream_token2wav = model.token2wav

        def sampling(weighted_scores, decoded_tokens, sampling_k):
            return upstream_sampling(weighted_scores.float().cpu(), decoded_tokens, sampling_k)

        def inference(**kwargs):
            self._llm_idle.clear()
            kwargs = {k: v.to(llm_device) if torch.is_tensor(v) else v for k, v in kwargs.items()}
            try:
                for token in upstream_inference(**kwargs):
                    if self._stop.is_set() or self._cancel.is_set():
                        return
                    yield token
            finally:
                self._llm_idle.set()

        def hift_inference(speech_feat, finalize=True):
            return upstream_hift(speech_feat=speech_feat.cpu(), finalize=finalize)

        def token2wav(*args, **kwargs):
            if self._stop.is_set() or self._cancel.is_set():
                return torch.zeros(1, 1)  # cancelled: skip the flow/vocoder pass tts() would still run
            return upstream_token2wav(*args, **kwargs)

        llm.sampling = sampling
        llm.inference = inference
        model.hift.inference = hift_inference
        model.token2wav = token2wav

    def synthesize(self, text: str, voice_id: str, speed: float, cancel: threading.Event,
                   stream: bool = True) -> Iterator[np.ndarray]:
        """Yield PCM16 chunks for already-normalized English text.

        Streams chunk by chunk when ``stream`` and speed is 1.0. Upstream supports speed only in non-stream
        mode (mel time-stretch in CosyVoice3Model.token2wav), so other speeds yield one chunk per sentence.
        Stops at the next chunk boundary (and the LLM at its next token) once ``cancel`` is set.
        """
        voice = self.voices[voice_id]
        with self._lock:
            cv, model = self._cv, self._cv.model
            if cancel.is_set():
                return
            self._cancel, self._stop = cancel, threading.Event()
            model.token_hop_len = 25  # value from CosyVoice3Model.__init__
            stream = stream and speed == 1.0
            if voice.mode == "cross_lingual":
                gen = cv.inference_cross_lingual(PROMPT_PREFIX + text, "", zero_shot_spk_id=voice_id,
                                                 stream=stream, speed=speed)
            else:
                gen = cv.inference_zero_shot(text, "", "", zero_shot_spk_id=voice_id, stream=stream, speed=speed)
            try:
                for out in gen:
                    if cancel.is_set():
                        break
                    yield to_pcm16(out["tts_speech"])
                    if cancel.is_set():
                        break
            except Exception:
                if not cancel.is_set():  # a cancelled request may end in an upstream error on empty tokens
                    raise
            finally:
                self._stop.set()
                gen.close()
                if not self._llm_idle.wait(timeout=30):
                    log.error("llm_thread_stuck")
                # tts() pops its per-request state only when it runs to completion
                model.tts_speech_token_dict.clear()
                model.llm_end_dict.clear()
                model.hift_cache_dict.clear()
