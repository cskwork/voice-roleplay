"""Calibration of raw GOP to word bands (TRIAGE PA-3). Fitted offline by benchmarks/pronunciation/fit_calibration.py.

Two small linear models (least squares with a light ridge penalty, fitted on speechocean762 train):
  phone: predicted human phone accuracy (0-2) = intercept + a*g + b*exp(g) + bias[expected unit],  g = max(gop, GOP_FLOOR)
  word:  predicted human word accuracy (0-10) = intercept + c*mean(phone predictions) + d*min(phone predictions)
Band of a word from the predicted word accuracy: < practice_below -> "practice", < check_below -> "check", else "good".

The numbers stay inside the worker: /assess returns only the band, and only when bands are enabled (PROTOCOL §12.3).
A file fitted on speechocean762 (Mandarin-L1 speakers) is no evidence of accuracy for Korean learners.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

GOP_FLOOR = -15.0
PHONE_FEATURES = ("intercept", "gop", "posterior")  # + one bias per expected unit
WORD_FEATURES = ("intercept", "mean_phone", "min_phone")
BANDS = ("good", "check", "practice")


def phone_features(gop: float) -> list[float]:
    g = max(gop, GOP_FLOOR)
    return [1.0, g, float(np.exp(g))]


def word_features(phone_scores: list[float]) -> list[float]:
    return [1.0, float(np.mean(phone_scores)), float(np.min(phone_scores))]


@dataclass(frozen=True)
class Calibration:
    version: str
    phone_coef: tuple[float, ...]
    unit_bias: dict[str, float]
    word_coef: tuple[float, ...]
    practice_below: float
    check_below: float

    def phone_score(self, ipa: str, gop: float | None) -> float | None:
        if gop is None:
            return None
        v = float(np.dot(self.phone_coef, phone_features(gop))) + self.unit_bias.get(ipa, 0.0)
        return min(max(v, 0.0), 2.0)

    def word_score(self, phones: list[tuple[str, float | None]]) -> float | None:
        """phones = (expected ipa, gop) per unit of the word; None when no unit has a gop."""
        scores = [s for s in (self.phone_score(ipa, g) for ipa, g in phones) if s is not None]
        if not scores:
            return None
        return min(max(float(np.dot(self.word_coef, word_features(scores))), 0.0), 10.0)

    def band(self, word_score: float | None) -> str | None:
        if word_score is None:
            return None
        if word_score < self.practice_below:
            return "practice"
        return "check" if word_score < self.check_below else "good"

    @classmethod
    def from_json(cls, data: dict) -> Calibration:
        if data.get("phone", {}).get("features") != list(PHONE_FEATURES) or data.get("word", {}).get("features") != list(WORD_FEATURES):
            raise ValueError("calibration file uses a different feature set")
        return cls(
            version=data["calibration_version"],
            phone_coef=tuple(data["phone"]["coef"]),
            unit_bias=dict(data["phone"]["unit_bias"]),
            word_coef=tuple(data["word"]["coef"]),
            practice_below=float(data["bands"]["practice_below"]),
            check_below=float(data["bands"]["check_below"]),
        )


def load_calibration(path: Path | None) -> Calibration | None:
    """None when there is no file. A file that exists but does not parse is an error (raised at load, exit 3)."""
    if path is None or not path.is_file():
        return None
    return Calibration.from_json(json.loads(path.read_text(encoding="utf-8")))
