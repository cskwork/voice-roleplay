import base64
import binascii

import numpy as np

from .config import MAX_AUDIO_BYTES, SAMPLE_RATE


class AudioError(ValueError):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def decode_audio_b64(value: object) -> np.ndarray:
    """base64 PCM16 LE mono 16 kHz -> float32 in [-1, 1]. Raises AudioError (400 AUDIO_INVALID / 413 AUDIO_TOO_LONG)."""
    if not isinstance(value, str) or not value:
        raise AudioError(400, "AUDIO_INVALID")
    if len(value) * 3 // 4 > MAX_AUDIO_BYTES + 3:
        raise AudioError(413, "AUDIO_TOO_LONG")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise AudioError(400, "AUDIO_INVALID") from None
    if len(raw) > MAX_AUDIO_BYTES:
        raise AudioError(413, "AUDIO_TOO_LONG")
    if not raw or len(raw) % 2:
        raise AudioError(400, "AUDIO_INVALID")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def audio_ms(x: np.ndarray) -> int:
    return len(x) * 1000 // SAMPLE_RATE
