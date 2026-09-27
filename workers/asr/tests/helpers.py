import re
import wave
from pathlib import Path

import jiwer

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "audio"


def fixture_pcm(name: str) -> bytes:
    """PCM16 mono 16 kHz bytes of a generated fixture (run tests/fixtures/make_fixtures.sh first)."""
    path = FIXTURES / f"{name}.wav"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run tests/fixtures/make_fixtures.sh")
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2), name
        return w.readframes(w.getnframes())


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text().strip()


def _norm(s: str) -> str:
    s = s.lower().replace("-", " ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", "", s)).strip()


def wer(ref: str, hyp: str) -> float:
    return jiwer.wer(_norm(ref), _norm(hyp))
