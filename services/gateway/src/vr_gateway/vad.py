"""Silero VAD (ONNX, CPU) with per-stream state, plus offline voiced-segment extraction."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import onnxruntime

WINDOW = 512  # samples at 16 kHz = 32 ms
CONTEXT = 64
SAMPLE_RATE = 16000
WINDOW_MS = WINDOW * 1000 // SAMPLE_RATE


class SileroModel:
    """One shared ONNX session; state lives in each VadStream."""

    def __init__(self, path: Path):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = onnxruntime.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        self._lock = threading.Lock()
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)

    def run(self, window_with_context: np.ndarray, state: np.ndarray) -> tuple[float, np.ndarray]:
        with self._lock:
            out, new_state = self.session.run(
                None, {"input": window_with_context[None, :], "state": state, "sr": self._sr}
            )
        return float(out[0][0]), new_state

    def stream(self) -> VadStream:
        return VadStream(self)


class VadStream:
    """Accepts arbitrary-size int16 chunks, returns one speech probability per complete 512-sample window."""

    def __init__(self, model: SileroModel):
        self.model = model
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.int16)

    def process(self, pcm: np.ndarray) -> list[tuple[float, np.ndarray]]:
        """Return [(prob, window_int16)] for each complete window."""
        buf = np.concatenate([self._pending, pcm]) if self._pending.size else pcm
        out = []
        n = buf.shape[0] // WINDOW
        for i in range(n):
            window = buf[i * WINDOW : (i + 1) * WINDOW]
            x = np.concatenate([self._context, window.astype(np.float32) / 32768.0])
            prob, self._state = self.model.run(x, self._state)
            self._context = x[-CONTEXT:]
            out.append((prob, window))
        self._pending = buf[n * WINDOW :].copy()
        return out


def voiced_segments(
    probs: list[float],
    threshold: float = 0.5,
    neg_threshold: float = 0.35,
    min_speech_ms: int = 250,
    min_silence_ms: int = 100,
) -> list[tuple[float, float]]:
    """Hysteresis over per-window probabilities -> [(start_s, end_s)] voiced spans."""
    segs: list[list[float]] = []
    in_speech = False
    start = 0
    silence_windows = 0
    min_silence_w = max(1, min_silence_ms // WINDOW_MS)
    for i, p in enumerate(probs):
        if not in_speech:
            if p >= threshold:
                in_speech, start, silence_windows = True, i, 0
        elif p < neg_threshold:
            silence_windows += 1
            if silence_windows >= min_silence_w:
                segs.append([start, i - silence_windows + 1])
                in_speech = False
        else:
            silence_windows = 0
    if in_speech:
        segs.append([start, len(probs)])
    w = WINDOW / SAMPLE_RATE
    return [(s * w, e * w) for s, e in segs if (e - s) * WINDOW_MS >= min_speech_ms]


def analyze(model: SileroModel, pcm16k: np.ndarray) -> list[tuple[float, float]]:
    stream = model.stream()
    probs = [p for p, _ in stream.process(pcm16k)]
    return voiced_segments(probs)
