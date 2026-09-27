import numpy as np

from .config import SAMPLE_RATE

FRAME = SAMPLE_RATE * 30 // 1000  # 30 ms
MIN_VOICED_FRAMES = 3


def pcm16_to_float(data: bytes | bytearray | memoryview) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _frame_rms(x: np.ndarray, frame: int) -> np.ndarray:
    n = len(x) // frame
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = x[: n * frame].reshape(n, frame)
    return np.sqrt(np.mean(frames * frames, axis=1))


def is_silent(x: np.ndarray, dbfs: float) -> bool:
    """True if fewer than ~90 ms of the audio rise above `dbfs` RMS.

    With the language forced to English the model hallucinates short words ("Okay.") on silence,
    so near-silent audio never reaches the model.
    """
    threshold = 10 ** (dbfs / 20)
    return int(np.count_nonzero(_frame_rms(x, FRAME) > threshold)) < MIN_VOICED_FRAMES


def quietest_cut(x: np.ndarray, lo: int, hi: int) -> int:
    """Sample index of the centre of the quietest 100 ms frame within x[lo:hi]."""
    frame = SAMPLE_RATE // 10
    rms = _frame_rms(x[lo:hi], frame)
    if len(rms) == 0:
        return hi
    return lo + int(np.argmin(rms)) * frame + frame // 2
