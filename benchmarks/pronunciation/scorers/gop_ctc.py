"""PA-2/PA-3: CTC-based GOP of the pronunciation worker (workers/pronunciation, pron_worker) on speechocean762.

Same pipeline as POST /assess except the expected phones: the benchmark uses the corpus's own canonical ARPAbet per word
(the phones the human scores refer to), mapped to model units with pron_worker.lexicon.arpabet_units; the worker looks
words up in CMUdict. Word timings come from the forced aligner on the reference words, as in the worker. Near-silent
audio gets no output (the worker answers 422 NO_SPEECH).

  gop_ctc      raw values: phone = unit gop (a merged unit such as "ɑːɹ" gives its gop to both ARPAbet phones),
               word = mean unit gop (the worker's word_gop). No calibration, no flags.
  gop_ctc_cal  the calibration file applied (pron_worker.calibration): phone/word = predicted human accuracy,
               flagged = band is not "good", sentence accuracy from a benchmark-only sentence model (never in the worker).
  gop_ctc_cal_cmudict  as gop_ctc_cal but expected phones from CMUdict like the worker (all pronunciations, best one
               chosen by likelihood). Word/sentence/flags only: CMUdict units do not map 1:1 onto the corpus phones.

Run with the worker's venv:
  PYTHONPATH=benchmarks/pronunciation workers/pronunciation/.venv/bin/python benchmarks/pronunciation/run_eval.py \
      --system gop_ctc --split train
Env: PRON_DEVICE (auto|mps|cpu), PRON_CALIBRATION (gop_ctc_cal; default = the worker's pinned calibration file).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

ROOT = Path(__file__).resolve().parents[3]
WORKER = ROOT / "workers" / "pronunciation"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

import numpy as np  # noqa: E402
from pron_worker.aligner import QwenForcedAligner, align_words  # noqa: E402
from pron_worker.app import is_silent  # noqa: E402
from pron_worker.calibration import Calibration, load_calibration, word_features  # noqa: E402
from pron_worker.config import ALIGNER_ID, ALIGNER_REVISION, CALIBRATION_VERSION, PHONES_ID, PHONES_REVISION  # noqa: E402
from pron_worker.lexicon import UNIT_TABLE_VERSION, Lexicon, arpabet_units  # noqa: E402
from pron_worker.phones import WINDOW_PAD_MS, PhoneModel, score_words  # noqa: E402

SENTENCE_MODEL = Path(__file__).resolve().parents[1] / "results" / f"{CALIBRATION_VERSION}-sentence-model.json"


def unit_rows(ref_words, results) -> list[list[list]]:
    """Per word: [[ipa, gop, [arpabet positions]], ...] (what the calibration fit needs; cached as `extra`)."""
    return [[[r.expected_ipa, r.gop, list(u.src)] for u, r in zip(units, res)] for units, res in results]


class GopCtcScorer:
    name = "gop_ctc"

    def __init__(self) -> None:
        device = os.environ.get("PRON_DEVICE", "auto")
        self.aligner = QwenForcedAligner(ROOT / "models" / "Qwen3-ForcedAligner-0.6B", device)
        self.phones = PhoneModel(ROOT / "models" / "wav2vec2-lv-60-espeak-cv-ft", device)

    def info(self) -> dict:
        return {
            "method": "segmentation-free CTC GOP over aligned word windows (prev+word+next, pad "
            f"{WINDOW_PAD_MS} ms); expected phones = corpus ARPAbet -> pron_worker.lexicon units",
            "phones_model": PHONES_ID,
            "phones_revision": PHONES_REVISION,
            "aligner_model": ALIGNER_ID,
            "aligner_revision": ALIGNER_REVISION,
            "unit_table": UNIT_TABLE_VERSION,
            "device": self.phones.device,
        }

    def units(self, wav, ref_words):
        """(word results of phones.score_words, or None for near-silent audio)."""
        if is_silent(wav):
            return None
        words = align_words(self.aligner, wav, " ".join(w.text for w in ref_words))
        if len(words) != len(ref_words):
            raise ValueError("aligner words do not match the reference words")
        return score_words(self.phones.log_probs(wav), words, self.expected(ref_words))

    def expected(self, ref_words):
        return [[arpabet_units(w.phones)] for w in ref_words]

    def score(self, wav, reference_text: str, ref_words) -> dict:
        results = self.units(wav, ref_words)
        if results is None:
            return {"words": [{"accuracy": None, "flagged": None, "phones": None} for _ in ref_words], "extra": {"silent": True}}
        words = []
        for ref, (units, res) in zip(ref_words, results):
            phones: list[float | None] = [None] * len(ref.phones)
            for u, r in zip(units, res):
                for k in u.src:
                    phones[k] = r.gop
            gops = [r.gop for r in res if r.gop is not None]
            words.append({"accuracy": float(np.mean(gops)) if gops else None, "flagged": None, "phones": phones})
        return {"words": words, "extra": {"units": unit_rows(ref_words, results)}}


class GopCtcCalibratedScorer(GopCtcScorer):
    name = "gop_ctc_cal"

    def __init__(self) -> None:
        path = Path(os.environ.get("PRON_CALIBRATION", WORKER / "calibration" / f"{CALIBRATION_VERSION}.json"))
        cal = load_calibration(path)
        if cal is None:
            raise SystemExit(f"calibration file {path} is missing; run fit_calibration.py first")
        self.cal: Calibration = cal
        self.sentence = json.loads(SENTENCE_MODEL.read_text())
        super().__init__()

    def info(self) -> dict:
        return {**super().info(), "calibration_version": self.cal.version, "sentence_model": self.sentence["coef"]}

    def score(self, wav, reference_text: str, ref_words) -> dict:
        results = self.units(wav, ref_words)
        if results is None:
            return {"words": [{"accuracy": None, "flagged": None, "phones": None} for _ in ref_words], "extra": {"silent": True}}
        words, word_scores, band_counts = [], [], {}
        for ref, (units, res) in zip(ref_words, results):
            phones = self.phone_scores(ref, units, res)
            ws = self.cal.word_score([(r.expected_ipa, r.gop) for r in res])
            band = self.cal.band(ws)
            band_counts[band] = band_counts.get(band, 0) + 1
            if ws is not None:
                word_scores.append(ws)
            words.append({"accuracy": ws, "flagged": None if band is None else band != "good", "phones": phones})
        sentence = {"accuracy": sentence_accuracy(self.sentence["coef"], word_scores)} if word_scores else {}
        return {"sentence": sentence, "words": words, "extra": {"bands": band_counts}}

    def phone_scores(self, ref, units, res) -> list[float | None] | None:
        phones: list[float | None] = [None] * len(ref.phones)
        for u, r in zip(units, res):
            for k in u.src:
                phones[k] = self.cal.phone_score(r.expected_ipa, r.gop)
        return phones


class GopCtcCmudictScorer(GopCtcCalibratedScorer):
    name = "gop_ctc_cal_cmudict"

    def __init__(self) -> None:
        self.lexicon = Lexicon(ROOT / "models" / "cmudict" / "cmudict.dict")
        super().__init__()

    def info(self) -> dict:
        return {**super().info(), "expected_phones": "CMUdict cmusphinx/cmudict@74790861 (worker path)"}

    def expected(self, ref_words):
        return [self.lexicon.variants(w.text) for w in ref_words]

    def phone_scores(self, ref, units, res) -> None:
        return None


SENTENCE_FEATURES = ("intercept", "mean_word", "min_word")


def sentence_features(word_scores: list[float]) -> list[float]:
    return word_features(word_scores)  # same shape: intercept, mean, min


def sentence_accuracy(coef: list[float], word_scores: list[float]) -> float:
    """Benchmark-only utterance accuracy (0-10) for comparison with published baselines; the worker has no such score."""
    return min(max(float(np.dot(coef, sentence_features(word_scores))), 0.0), 10.0)
