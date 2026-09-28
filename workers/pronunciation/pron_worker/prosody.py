"""F0 contour (pyworld Harvest, 10 ms hop) and per-word pitch/duration statistics. Reference data only, no judgments."""
import numpy as np

from .config import SAMPLE_RATE

METHOD = "pyworld-harvest"
HOP_MS = 10
F0_FLOOR_HZ = 60.0
F0_CEIL_HZ = 500.0


def f0_contour(audio: np.ndarray) -> np.ndarray:
    """F0 in Hz per 10 ms frame (frame k is centred at k*10 ms); 0 = unvoiced."""
    import pyworld

    f0, _ = pyworld.harvest(
        audio.astype(np.float64), SAMPLE_RATE, f0_floor=F0_FLOOR_HZ, f0_ceil=F0_CEIL_HZ, frame_period=float(HOP_MS)
    )
    return f0


def word_stats(f0: np.ndarray, words: list[dict]) -> list[dict]:
    """Per word (PROTOCOL §12.2): mean F0 over voiced frames, F0 range 12*log2(max/min) in semitones over voiced
    frames, duration. Both F0 values are 0 when the word has no voiced frame."""
    out = []
    for w in words:
        lo = -(-w["start_ms"] // HOP_MS)  # first frame centred at or after start
        hi = -(-w["end_ms"] // HOP_MS)  # frames centred before end
        voiced = f0[lo:hi]
        voiced = voiced[voiced > 0]
        mean_f0 = round(float(voiced.mean()), 1) if len(voiced) else 0.0
        range_st = round(float(12 * np.log2(voiced.max() / voiced.min())), 2) if len(voiced) else 0.0
        out.append({"i": w["i"], "mean_f0": mean_f0, "f0_range_st": range_st, "duration_ms": w["end_ms"] - w["start_ms"]})
    return out
