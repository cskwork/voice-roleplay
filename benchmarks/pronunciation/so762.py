"""Loader for speechocean762 (OpenSLR 101, CC BY 4.0) as fetched by fetch_speechocean762.py.

Splits are the published Kaldi-style train/ and test/ directories (125 speakers each, speaker-disjoint).
Human scores come from resource/scores.json (the per-item average/median of five experts):
  phone accuracy 0-2 (2 correct, 1 accented, 0 wrong or missing)
  word accuracy 0-10, word stress {5, 10}, word total
  sentence accuracy / fluency / prosodic / total 0-10, completeness 0-10 (as stored in scores.json)
"""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "speechocean762"
SPLITS = ("train", "test")
SENTENCE_KEYS = ("accuracy", "completeness", "fluency", "prosodic", "total")


@dataclass(frozen=True)
class Word:
    text: str
    phones: tuple[str, ...]  # canonical ARPAbet with stress digits, e.g. ("B", "EH0", "R")
    phone_accuracy: tuple[float, ...]  # human, 0-2, aligned to phones
    accuracy: float  # human, 0-10
    stress: float
    total: float
    mispronunciations: tuple[dict, ...] = ()  # {"canonical-phone", "index", "pronounced-phone"}


@dataclass(frozen=True)
class Utterance:
    utt_id: str
    split: str
    speaker: str
    age: int
    gender: str
    text: str  # reference sentence as shown to the speaker (may contain punctuation)
    words: tuple[Word, ...]
    sentence: dict = field(default_factory=dict)  # SENTENCE_KEYS -> human score
    wav_path: Path = Path()

    def load_audio(self) -> np.ndarray:
        """16 kHz mono float32 in [-1, 1)."""
        return read_wav(self.wav_path)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 16_000):
            raise ValueError(f"{path}: expected 16 kHz mono PCM16")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _kv(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if line.strip():
            k, v = line.split(maxsplit=1)
            out[k] = v.strip()
    return out


def load(split: str, root: Path = DEFAULT_ROOT) -> list[Utterance]:
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}")
    if not (root / "resource" / "scores.json").is_file():
        raise FileNotFoundError(f"{root} is missing; run benchmarks/pronunciation/fetch_speechocean762.py")
    scores = json.loads((root / "resource" / "scores.json").read_text())
    d = root / split
    texts, wavs, utt2spk = _kv(d / "text"), _kv(d / "wav.scp"), _kv(d / "utt2spk")
    ages, genders = _kv(d / "spk2age"), _kv(d / "spk2gender")
    out = []
    for utt_id in sorted(texts):
        s = scores[utt_id]
        words = tuple(
            Word(
                text=w["text"],
                phones=tuple(w["phones"]),
                phone_accuracy=tuple(float(x) for x in w["phones-accuracy"]),
                accuracy=float(w["accuracy"]),
                stress=float(w["stress"]),
                total=float(w["total"]),
                mispronunciations=tuple(w.get("mispronunciations", ())),
            )
            for w in s["words"]
        )
        for w in words:
            if len(w.phones) != len(w.phone_accuracy):
                raise ValueError(f"{utt_id}/{w.text}: phones and phone scores differ in length")
        spk = utt2spk[utt_id]
        out.append(
            Utterance(
                utt_id=utt_id,
                split=split,
                speaker=spk,
                age=int(ages[spk]),
                gender=genders[spk],
                text=s["text"],
                words=words,
                sentence={k: float(s[k]) for k in SENTENCE_KEYS},
                wav_path=root / wavs[utt_id],
            )
        )
    return out


def check_speaker_disjoint(root: Path = DEFAULT_ROOT) -> None:
    spk = {s: set(_kv(root / s / "utt2spk").values()) for s in SPLITS}
    both = spk["train"] & spk["test"]
    if both:
        raise ValueError(f"speakers in both train and test: {sorted(both)[:5]}")
