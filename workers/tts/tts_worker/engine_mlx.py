"""MLX backend: the same Fun-CosyVoice3-0.5B-2512 weights, converted to MLX by mlx-community.

The conversion (``mlx-community/Fun-CosyVoice3-0.5B-2512-*``) was made with mlx-audio-plus, whose MLX port of the
model classes we use (``mlx_audio.tts.models.cosyvoice3``, mlx-audio-plus 0.1.8). Upstream mlx-audio has no
CosyVoice3. We do not use its ``Model.generate`` wrapper: it has no streaming or speed, resamples and trims the
prompt audio differently from CosyVoice and computes the prompt mel with fmax=8000 where cosyvoice3.yaml uses
fmax=null (12 kHz). Instead this module mirrors the upstream PyTorch path (vendor/CosyVoice @ 074ca6dc):

- Prompt features are computed once per voice at load (like ``add_zero_shot_spk``): speech tokens from the
  official ``speech_tokenizer_v3.onnx`` loaded into mlx-audio-plus's S3TokenizerV3, CAM++ speaker embedding
  from the ``campplus.*`` weights in the MLX checkpoint, 80-bin 24 kHz mel; tokens and mel trimmed to 2 frames
  per token.
- Text: zero-shot voices get ``split_paragraph`` like ``frontend.text_normalize``; cross-lingual voices get the
  prompt prefix prepended and are not split (upstream skips its text frontend when ``<|`` is present).
- Streaming follows ``CosyVoice2Model.tts``: the first chunk after ``token_hop`` tokens (+ prompt padding) and
  3 lookahead tokens, the hop doubles up to 4x; each chunk re-runs flow over all tokens and the vocoder over
  all mel so far and emits only the new samples. The final pass runs without the streaming mask. Unlike
  upstream the LLM runs in the same thread as flow/vocoder: in a separate thread it gained ~5% RTF (it is starved
  while the flow pass occupies the GPU) and cost ~0.2 s first-chunk latency.
- Flow ODE steps default to 5 (upstream: 10): 10 steps miss the RTF target on an M3 Pro (see README).
- Speed: non-stream only, mel time-stretch before the vocoder (``F.interpolate(mode='linear')`` upstream).
- Flow noise is a fixed buffer made once with its own key, as upstream's ``rand_noise``; the port's fallback
  would call ``mx.random.seed(0)`` on every flow pass and reset the LLM sampler's RNG with it.
- Cancel is checked after every LLM token and after every chunk, so a cancelled request stops within one token
  or one flow pass.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

import numpy as np

from .engine import (DEFAULT_MODEL_DIR, DEFAULT_VOICES_DIR, PROMPT_PREFIX, VENDOR_DIR, SetupError, load_voices,
                     model_revision)

REPO_DIR = Path(__file__).resolve().parents[3]
MLX_VARIANTS = {"fp16": "mlx-community/Fun-CosyVoice3-0.5B-2512-fp16",
                "8bit": "mlx-community/Fun-CosyVoice3-0.5B-2512-8bit"}
REQUIRED_MLX_FILES = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json")
SPEECH_TOKENIZER = DEFAULT_MODEL_DIR / "speech_tokenizer_v3.onnx"  # official weights, shared with the torch backend
SAMPLE_RATE = 24000
# From CosyVoice3Model (vendor/CosyVoice/cosyvoice/cli/model.py): silent/breath tokens, at most 5 in a row.
SILENT_TOKENS = frozenset({1, 2, 28, 29, 55, 248, 494, 2241, 2242, 2322, 2323})
MAX_SILENT_TOKENS = 5

log = logging.getLogger("vr.tts.engine")


def mlx_model_dir(variant: str) -> Path:
    return REPO_DIR / "models" / f"Fun-CosyVoice3-0.5B-2512-mlx-{variant}"


def mlx_files_present(variant: str) -> bool:
    d = mlx_model_dir(variant)
    return SPEECH_TOKENIZER.exists() and all((d / f).exists() for f in REQUIRED_MLX_FILES)


class _Cancelled(Exception):
    pass


def _stretch(mel: np.ndarray, speed: float) -> np.ndarray:
    """Linear time-stretch of (1, 80, T) like ``F.interpolate(mel, size=int(T / speed), mode='linear')``."""
    t_in = mel.shape[-1]
    t_out = int(t_in / speed)
    src = np.maximum((np.arange(t_out) + 0.5) * (t_in / t_out) - 0.5, 0.0)
    i0 = np.minimum(src.astype(np.int64), t_in - 1)
    i1 = np.minimum(i0 + 1, t_in - 1)
    w = (src - i0).astype(mel.dtype)
    return mel[..., i0] * (1 - w) + mel[..., i1] * w


class MlxEngine:
    """Same interface as ``engine.Engine`` (voices, ready, load, synthesize, health fields)."""

    def __init__(self, variant: str = "fp16", voices_dir: Path = DEFAULT_VOICES_DIR, flow_steps: int = 5,
                 token_hop: int = 50):
        if variant not in MLX_VARIANTS:
            raise SetupError(f"unknown MLX variant {variant!r}; use {'|'.join(MLX_VARIANTS)}")
        self.model_id = MLX_VARIANTS[variant]
        self.model_dir = mlx_model_dir(variant)
        self.voices = load_voices(Path(voices_dir))
        self.flow_steps = flow_steps
        self.token_hop = token_hop
        self.revision = model_revision(self.model_dir, "config.json")
        self.sample_rate = SAMPLE_RATE
        self.placement = {"llm": "mlx", "flow": "mlx", "hift": "mlx"}
        self.device = "mlx"
        self.load_seconds: float | None = None
        self._ready = False
        self._lock = threading.Lock()
        self._prompts: dict[str, dict] = {}

    @property
    def ready(self) -> bool:
        return self._ready

    def load(self) -> None:
        missing = [str(self.model_dir / f) for f in REQUIRED_MLX_FILES if not (self.model_dir / f).exists()]
        missing += [] if SPEECH_TOKENIZER.exists() else [str(SPEECH_TOKENIZER)]
        if missing:
            raise SetupError(f"model files missing: {missing}; download {self.model_id} (see workers/tts/README.md)")
        if not (VENDOR_DIR / "cosyvoice").exists():
            raise SetupError(f"{VENDOR_DIR} missing; run workers/tts/setup.sh")
        for key, value in (("HF_HUB_OFFLINE", "1"), ("TRANSFORMERS_OFFLINE", "1"), ("TQDM_DISABLE", "1"),
                           ("TOKENIZERS_PARALLELISM", "false")):
            os.environ.setdefault(key, value)

        t0 = time.perf_counter()
        import mlx.core as mx
        from mlx_audio.codec.models.s3tokenizer import S3TokenizerV3
        from mlx_audio.tts.models.cosyvoice2.speaker_encoder import CAMPlusSpeakerEncoder
        from mlx_audio.tts.models.cosyvoice3 import load_cosyvoice3
        from transformers import AutoTokenizer
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        self._mx = mx
        self._model = load_cosyvoice3(str(self.model_dir))
        # Fixed flow noise like upstream CausalConditionalCFM.rand_noise (1, 80, 50 * 300).
        self._model.flow.decoder._rand_noise = mx.random.normal((1, 80, 50 * 300), key=mx.random.key(0))
        # Materialize now: lazy arrays are bound to this thread's streams, and synthesis runs in other threads.
        mx.eval(self._model.parameters(), self._model.flow.decoder._rand_noise)
        self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        # Same id (151646) as upstream CosyVoice3Tokenizer; other special tokens are stripped by textnorm.
        self._tokenizer.add_special_tokens({"additional_special_tokens": ["<|endofprompt|>"]})
        speech_tokenizer = S3TokenizerV3.from_onnx(str(SPEECH_TOKENIZER))
        speaker = CAMPlusSpeakerEncoder()
        weights = mx.load(str(self.model_dir / "model.safetensors"))
        speaker.model.load_weights([(k[len("campplus."):], v) for k, v in weights.items() if k.startswith("campplus.")])
        speaker.model.eval()
        speaker._loaded = True
        del weights
        for v in self.voices.values():
            self._prompts[v.voice_id] = self._prompt_features(v, speech_tokenizer, speaker)
        del speech_tokenizer, speaker
        mx.clear_cache()

        for _ in self.synthesize("Hello there.", next(iter(self.voices)), 1.0, threading.Event()):
            pass  # warm-up: first run compiles Metal kernels
        self.load_seconds = time.perf_counter() - t0
        self._ready = True
        log.info("model_loaded backend=mlx model=%s flow_steps=%d token_hop=%d load_s=%.1f", self.model_id,
                 self.flow_steps, self.token_hop, self.load_seconds)

    def _prompt_features(self, voice, speech_tokenizer, speaker) -> dict:
        import soundfile as sf
        from mlx_audio.codec.models.s3gen.mel import mel_spectrogram
        from mlx_audio.codec.models.s3tokenizer import log_mel_spectrogram_compat
        from scipy.signal import resample_poly

        mx = self._mx
        audio, sr = sf.read(str(voice.prompt_wav), dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if sr < 16000:
            raise SetupError(f"voice {voice.voice_id}: prompt.wav sample rate must be >= 16 kHz")
        a16 = mx.array(resample_poly(audio, 16000, sr).astype(np.float32))
        a24 = mx.array(resample_poly(audio, SAMPLE_RATE, sr).astype(np.float32))
        if a16.shape[0] > 30 * 16000:
            raise SetupError(f"voice {voice.voice_id}: prompt.wav longer than 30 s")
        mel128 = log_mel_spectrogram_compat(a16, n_mels=128)[None]
        tokens, token_len = speech_tokenizer(mel128, mx.array([mel128.shape[2]]))
        # cosyvoice3.yaml feat_extractor: n_fft 1920, hop 480, win 1920, fmin 0, fmax null (= sr / 2), center False
        mel = mx.swapaxes(mel_spectrogram(a24, n_fft=1920, num_mels=80, sampling_rate=SAMPLE_RATE, hop_size=480,
                                          win_size=1920, fmin=0, fmax=SAMPLE_RATE // 2, center=False), 1, 2)
        n = min(mel.shape[1] // 2, int(token_len[0]))
        feats = {
            "speech_token": tokens[:, :n].astype(mx.int32),
            "speech_feat": mel[:, :2 * n, :],
            "embedding": speaker(a16, sample_rate=16000),
            "prompt_text": self._encode(PROMPT_PREFIX + voice.prompt_text),
        }
        mx.eval(*feats.values())
        return feats

    def _encode(self, text: str):
        return self._mx.array([self._tokenizer.encode(text, add_special_tokens=False)], dtype=self._mx.int32)

    def _segments(self, text: str, voice) -> list[str]:
        if voice.mode == "cross_lingual":
            return [PROMPT_PREFIX + text]
        if str(VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(VENDOR_DIR))
        from cosyvoice.utils.frontend_utils import is_only_punctuation, split_paragraph

        parts = split_paragraph(text.strip(), lambda s: self._tokenizer.encode(s, add_special_tokens=False), "en",
                                token_max_n=80, token_min_n=60, merge_len=20, comma_split=False)
        return [p for p in parts if not is_only_punctuation(p)]

    def synthesize(self, text: str, voice_id: str, speed: float, cancel: threading.Event,
                   stream: bool = True) -> Iterator[np.ndarray]:
        """Yield PCM16 chunks for already-normalized English text (same contract as ``Engine.synthesize``)."""
        voice = self.voices[voice_id]
        with self._lock:
            if cancel.is_set():
                return
            try:
                for segment in self._segments(text, voice):
                    for audio in self._synthesize_segment(segment, voice, speed, cancel, stream and speed == 1.0):
                        if cancel.is_set():
                            return
                        yield (np.clip(np.asarray(audio, dtype=np.float32).ravel(), -1.0, 1.0) * 32767).astype("<i2")
                        if cancel.is_set():
                            return
            except _Cancelled:
                return
            finally:
                self._mx.clear_cache()

    def _tokens(self, segment: str, voice, prompt: dict, cancel: threading.Event) -> Iterator[int]:
        mx = self._mx
        text = self._encode(segment)
        if voice.mode == "cross_lingual":  # upstream frontend_cross_lingual drops the LLM prompt
            prompt_text = prompt_speech = mx.zeros((1, 0), dtype=mx.int32)
        else:
            prompt_text, prompt_speech = prompt["prompt_text"], prompt["speech_token"]
        silent = 0
        for token in self._model.llm.inference(
                text=text, text_len=mx.array([text.shape[1]]), prompt_text=prompt_text,
                prompt_text_len=mx.array([prompt_text.shape[1]]), prompt_speech_token=prompt_speech,
                prompt_speech_token_len=mx.array([prompt_speech.shape[1]]), embedding=prompt["embedding"]):
            if cancel.is_set():
                raise _Cancelled
            if token in SILENT_TOKENS:
                silent += 1
                if silent > MAX_SILENT_TOKENS:
                    continue
            else:
                silent = 0
            yield token

    def _mel(self, tokens: list[int], prompt: dict, streaming: bool, finalize: bool):
        mx = self._mx
        self._model.flow.n_timesteps = self.flow_steps
        mel, _ = self._model.flow.inference(
            token=mx.array([tokens], dtype=mx.int32), token_len=mx.array([len(tokens)], dtype=mx.int32),
            prompt_token=prompt["speech_token"], prompt_token_len=mx.array([prompt["speech_token"].shape[1]]),
            prompt_feat=prompt["speech_feat"], prompt_feat_len=mx.array([prompt["speech_feat"].shape[1]]),
            embedding=prompt["embedding"], streaming=streaming, finalize=finalize)
        return mel

    def _synthesize_segment(self, segment: str, voice, speed: float, cancel: threading.Event, stream: bool):
        mx = self._mx
        prompt = self._prompts[voice.voice_id]
        if not stream:
            tokens = list(self._tokens(segment, voice, prompt, cancel))
            if not tokens:
                return
            mel = self._mel(tokens, prompt, streaming=False, finalize=True)
            if speed != 1.0:
                mel = mx.array(_stretch(np.array(mel), speed))
            audio, _ = self._model.hifigan(mel, finalize=True)
            mx.eval(audio)
            yield np.array(audio)
            return

        # CosyVoice2Model.tts stream path (vendor/CosyVoice/cosyvoice/cli/model.py)
        lookahead, ratio = self._model.flow.pre_lookahead_len, self._model.flow.token_mel_ratio
        n_prompt = prompt["speech_token"].shape[1]
        hop = self.token_hop
        pad = -(-n_prompt // hop) * hop - n_prompt  # align the first chunk to the hop boundary
        tokens: list[int] = []
        offset, emitted, mel_cache = 0, 0, None

        def vocode(mel, finalize):
            nonlocal mel_cache, emitted
            mel_cache = mel if mel_cache is None else mx.concatenate([mel_cache, mel], axis=2)
            audio, _ = self._model.hifigan(mel_cache, finalize=finalize)
            mx.eval(audio)
            new = np.array(audio[0, emitted:])
            emitted += new.shape[0]
            return new

        for token in self._tokens(segment, voice, prompt, cancel):
            tokens.append(token)
            this_hop = hop + pad if offset == 0 else hop
            if len(tokens) - offset >= this_hop + lookahead:
                mel = self._mel(tokens[:offset + this_hop + lookahead], prompt, streaming=True, finalize=False)
                new = vocode(mel[:, :, offset * ratio:], finalize=False)
                offset += this_hop
                hop = min(4 * self.token_hop, hop * 2)
                if new.size:
                    yield new
                if cancel.is_set():
                    raise _Cancelled
        if len(tokens) > offset:
            mel = self._mel(tokens, prompt, streaming=False, finalize=True)
            new = vocode(mel[:, :, offset * ratio:], finalize=True)
            if new.size:
                yield new
