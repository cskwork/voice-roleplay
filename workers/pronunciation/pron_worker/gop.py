"""Goodness-of-pronunciation scoring behind POST /assess (TRIAGE PA-2/PA-3).

Contract: contracts/PROTOCOL.md §12.2-12.3. Pipeline: reference text -> CMUdict units (lexicon.py) -> phone posteriors
of facebook/wav2vec2-lv-60-espeak-cv-ft -> CTC GOP per expected unit over a window around the forced-aligned word
(phones.py) -> optional calibration to a word band (calibration.py).

Rules (docs/PRD.md §8, TRIAGE design):
- Phones carry `expected_ipa`, `heard_candidates` [{ipa, p}] (ipa "" = nothing heard) and a raw `gop`; words carry
  `word_gop` (mean of the word's unit gops). These are uncalibrated model values, never for display.
- `band` stays None unless a calibration file exists AND the worker runs with VR_PRON_EXPERIMENTAL=1 (the app enforces
  this). There is no Korean-learner validation data, so bands are off by default.
- Words not in the lexicon get `phones: []`, `word_gop: null`, `band: null`.
- No overall pronunciation score, ever (pronunciation_score stays null in the gateway).
- Nothing about the audio, reference text or results may be logged beyond counts and durations.
- `mode` does not change the computation: "unscripted" only means reference_text is an ASR transcript, which the
  gateway treats as needing confirmation (PRD §8.3/§8.4).
"""
from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from .aligner import AlignedWord, has_alignable_text
from .calibration import Calibration, load_calibration
from .config import PHONES_FILES, PHONES_ID, PHONES_REVISION, Settings
from .lexicon import Lexicon

log = logging.getLogger("pron")


class GopScorer(Protocol):
    phones_model: dict[str, str]  # {"model_id", "revision"} of the phone recogniser (/health `models.phones`)
    calibration_version: str | None  # None = no calibration file loaded

    def assess(self, audio: np.ndarray, reference_text: str, words: list[AlignedWord], mode: str) -> list[dict]:
        """One dict per aligned word: {i, word, start_ms, end_ms, phones: [...], word_gop, band}.
        `words` come from the forced aligner on the same audio and reference text. Runs on the model thread."""
        ...


def reference_tokens(reference_text: str, words: list[AlignedWord]) -> list[str]:
    """The text token behind each aligned word (the aligner drops punctuation, e.g. the hyphen of "well-known"; the
    lexicon lookup wants the original token). Falls back to the aligner's words if the counts differ."""
    tokens = [t for t in reference_text.split() if has_alignable_text(t)]
    return tokens if len(tokens) == len(words) else [w.word for w in words]


class CtcGopScorer:
    phones_model = {"model_id": PHONES_ID, "revision": PHONES_REVISION}

    def __init__(self, settings: Settings, device: str):
        from .phones import PhoneModel

        self.lexicon = Lexicon(settings.lexicon_path)
        self.model = PhoneModel(settings.phones_dir, device)
        self.calibration: Calibration | None = load_calibration(settings.calibration_path)
        self.calibration_version = self.calibration.version if self.calibration else None

    def assess(self, audio: np.ndarray, reference_text: str, words: list[AlignedWord], mode: str) -> list[dict]:
        from .phones import score_words

        variants = [self.lexicon.variants(t) for t in reference_tokens(reference_text, words)]
        scored = score_words(self.model.log_probs(audio), words, variants)
        out = []
        for w, (_, results) in zip(words, scored):
            gops = [r.gop for r in results if r.gop is not None]
            band = None
            if self.calibration and results:
                band = self.calibration.band(self.calibration.word_score([(r.expected_ipa, r.gop) for r in results]))
            out.append({
                **w.to_json(),
                "phones": [r.to_json() for r in results],
                "word_gop": round(float(np.mean(gops)), 3) if gops else None,
                "band": band,
            })
        return out


def load_scorer(settings: Settings, device: str = "auto") -> GopScorer | None:
    """Load the phone recogniser, lexicon and calibration. None (with a log line) when the phone model or lexicon files
    are not installed: /assess then answers 501 and /align + /prosody work as before."""
    if settings.phones_dir is None or settings.lexicon_path is None:
        return None
    if not all((settings.phones_dir / n).is_file() for n in PHONES_FILES) or not settings.lexicon_path.is_file():
        log.warning("assess.unavailable reason=phone_model_or_lexicon_missing")
        return None
    return CtcGopScorer(settings, device)
