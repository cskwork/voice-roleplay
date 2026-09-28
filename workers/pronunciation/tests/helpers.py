import base64
import wave
from pathlib import Path

import numpy as np

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "audio"


def fixture_pcm(name: str) -> bytes:
    """PCM16 mono 16 kHz bytes of a fixture; 24 kHz TTS fixtures are resampled (run tests/fixtures/make_fixtures.sh first)."""
    path = FIXTURES / f"{name}.wav"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run tests/fixtures/make_fixtures.sh")
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2), name
        rate, data = w.getframerate(), w.readframes(w.getnframes())
    if rate == 16000:
        return data
    from math import gcd

    from scipy.signal import resample_poly

    x = np.frombuffer(data, "<i2").astype(np.float64)
    g = gcd(16000, rate)
    y = resample_poly(x, 16000 // g, rate // g)
    return np.clip(np.round(y), -32768, 32767).astype("<i2").tobytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text().strip()


def b64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode()


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> bytes:
    t = np.arange(int(seconds * 16000)) / 16000
    return (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2").tobytes()


def buzz(seconds: float, freq: float = 200.0) -> bytes:
    """Harmonic-rich periodic signal (Harvest treats a pure sine as unvoiced, like most speech F0 trackers)."""
    t = np.arange(int(seconds * 16000)) / 16000
    x = sum(0.3 / k * np.sin(2 * np.pi * freq * k * t) for k in range(1, 20))
    return (x * 0.5 * 32767).astype("<i2").tobytes()
