"""PA-3: fit the GOP calibration on speechocean762 train and evaluate it on test.

    workers/pronunciation/.venv/bin/python benchmarks/pronunciation/fit_calibration.py

Inputs: the cached raw outputs of `run_eval.py --system gop_ctc` for both splits (data/pron_runs/gop_ctc-{train,test}.jsonl).
Everything is fitted and chosen on train only (coefficients, band thresholds); test is used once, for the report.
Outputs:
  workers/pronunciation/calibration/<version>.json   phone + word models, band thresholds, train/test metrics
  results/<version>-sentence-model.json               benchmark-only utterance model (the worker has no sentence score)
  results/<version>-fit.json                          everything the fit measured (numbers only)

Models and features: pron_worker/calibration.py. Metrics: run_eval.evaluate (same code as the harness), so a later
`run_eval.py --system gop_ctc_cal --split test` must reproduce the test numbers.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_eval  # noqa: E402
import so762  # noqa: E402
from scorers.gop_ctc import SENTENCE_FEATURES, SENTENCE_MODEL, WORKER, sentence_accuracy, sentence_features  # noqa: E402
from pron_worker.calibration import (  # noqa: E402
    BANDS,
    PHONE_FEATURES,
    WORD_FEATURES,
    Calibration,
    phone_features,
    word_features,
)
from pron_worker.config import ALIGNER_ID, ALIGNER_REVISION, CALIBRATION_VERSION, PHONES_ID, PHONES_REVISION  # noqa: E402

RIDGE = 1.0  # on every coefficient except the intercept
# Human word classes for choosing band thresholds (corpus rubric): 0-3 "more than 30 % of phones wrong / other word /
# unintelligible", 4-6 "less than 30 % wrong", 7-10 correct (7-9 with an accent).
HUMAN_BAND = (("practice", 3.0), ("check", 6.0), ("good", 10.0))
THRESHOLD_GRID = np.round(np.arange(0.0, 10.01, 0.1), 1)
PUBLISHED = {
    "GOPT (Gong et al., ICASSP 2022), speechocean762 test": {"phone_accuracy_pcc": 0.612, "word_accuracy_pcc": 0.549,
                                                              "utterance_accuracy_pcc": 0.742},
}


def read_cache(path: Path) -> dict[str, dict]:
    lines = path.read_text().splitlines()
    return {r["utt_id"]: r for r in map(json.loads, lines[1:])}


def ridge(X: np.ndarray, y: np.ndarray, lam: float = RIDGE) -> np.ndarray:
    pen = np.eye(X.shape[1]) * lam
    pen[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + pen, X.T @ y)


def unit_rows(utts, cache):
    """(ipa, gop, human phone accuracy averaged over the ARPAbet phones the unit covers) for every scored unit."""
    for u in utts:
        units = cache[u.utt_id]["output"].get("extra", {}).get("units")
        if units is None:  # near-silent audio: no output
            continue
        for ref, word_units in zip(u.words, units):
            for ipa, gop, src in word_units:
                if gop is not None:
                    yield ipa, gop, float(np.mean([ref.phone_accuracy[k] for k in src]))


def fit_phone(utts, cache) -> tuple[list[float], dict[str, float]]:
    rows = list(unit_rows(utts, cache))
    units = sorted({ipa for ipa, _, _ in rows})
    col = {ipa: k for k, ipa in enumerate(units)}
    X = np.zeros((len(rows), len(PHONE_FEATURES) + len(units)))
    y = np.empty(len(rows))
    for n, (ipa, gop, h) in enumerate(rows):
        X[n, : len(PHONE_FEATURES)] = phone_features(gop)
        X[n, len(PHONE_FEATURES) + col[ipa]] = 1.0
        y[n] = h
    beta = ridge(X, y)
    return [round(float(b), 6) for b in beta[: len(PHONE_FEATURES)]], {ipa: round(float(beta[len(PHONE_FEATURES) + col[ipa]]), 6) for ipa in units}


def word_unit_pairs(u, cache):
    units = cache[u.utt_id]["output"].get("extra", {}).get("units")
    if units is None:
        return None
    return [[(ipa, gop) for ipa, gop, _ in wu] for wu in units]


def fit_word(utts, cache, cal: Calibration) -> list[float]:
    X, y = [], []
    for u in utts:
        pairs = word_unit_pairs(u, cache)
        for ref, wp in zip(u.words, pairs or []):
            scores = [s for s in (cal.phone_score(ipa, g) for ipa, g in wp) if s is not None]
            if scores:
                X.append(word_features(scores))
                y.append(ref.accuracy)
    return [round(float(b), 6) for b in ridge(np.array(X), np.array(y))]


def word_predictions(utts, cache, cal: Calibration):
    """[(utterance, [(human word accuracy, predicted word accuracy or None)])]"""
    out = []
    for u in utts:
        pairs = word_unit_pairs(u, cache)
        out.append((u, [(ref.accuracy, cal.word_score(wp)) for ref, wp in zip(u.words, pairs)] if pairs else None))
    return out


def fit_sentence(preds) -> list[float]:
    X, y = [], []
    for u, words in preds:
        ws = [p for _, p in words or [] if p is not None]
        if ws:
            X.append(sentence_features(ws))
            y.append(u.sentence["accuracy"])
    return [round(float(b), 6) for b in ridge(np.array(X), np.array(y))]


def human_band(accuracy: float) -> str:
    return next(name for name, hi in HUMAN_BAND if accuracy <= hi)


def choose_thresholds(preds) -> dict:
    """Band thresholds on train: the pair (practice_below <= check_below) with the best macro-F1 over the 3 human classes."""
    h, p = [], []
    for _, words in preds:
        for acc, pred in words or []:
            if pred is not None:
                h.append(BANDS.index(human_band(acc)))
                p.append(pred)
    h, p = np.array(h), np.array(p)
    best = None
    for t1, t2 in itertools.combinations_with_replacement(THRESHOLD_GRID, 2):
        band = np.where(p < t1, 2, np.where(p < t2, 1, 0))
        f1s = []
        for c in range(3):
            tp = np.sum((band == c) & (h == c))
            prec = tp / max(np.sum(band == c), 1)
            rec = tp / max(np.sum(h == c), 1)
            f1s.append(0.0 if tp == 0 else 2 * prec * rec / (prec + rec))
        score = float(np.mean(f1s))
        if best is None or score > best[0]:
            best = (score, float(t1), float(t2))
    return {"practice_below": best[1], "check_below": best[2], "train_macro_f1": round(best[0], 4)}


def band_confusion(preds, cal: Calibration) -> dict:
    """Rows = human class, columns = predicted band (words with a prediction)."""
    m = {hb: dict.fromkeys(BANDS, 0) for hb in BANDS}
    for _, words in preds:
        for acc, pred in words or []:
            b = cal.band(pred)
            if b is not None:
                m[human_band(acc)][b] += 1
    return m


def records(utts, cache, cal: Calibration, sentence_coef) -> dict[str, dict]:
    """Harness records of the calibrated system, computed from the raw cache (same maths as scorers.gop_ctc_cal)."""
    out = {}
    for u in utts:
        units = cache[u.utt_id]["output"].get("extra", {}).get("units")
        if units is None:
            out[u.utt_id] = {**cache[u.utt_id], "output": {"words": [{"accuracy": None, "flagged": None, "phones": None}] * len(u.words)}}
            continue
        words, scores = [], []
        for ref, wu in zip(u.words, units):
            phones = [None] * len(ref.phones)
            for ipa, gop, src in wu:
                for k in src:
                    phones[k] = cal.phone_score(ipa, gop)
            ws = cal.word_score([(ipa, gop) for ipa, gop, _ in wu])
            band = cal.band(ws)
            if ws is not None:
                scores.append(ws)
            words.append({"accuracy": ws, "flagged": None if band is None else band != "good", "phones": phones})
        sent = {"accuracy": sentence_accuracy(sentence_coef, scores)} if scores else {}
        out[u.utt_id] = {**cache[u.utt_id], "output": {"sentence": sent, "words": words}}
    return out


def raw_records(utts, cache) -> dict[str, dict]:
    return {u.utt_id: cache[u.utt_id] for u in utts}


def summary(rep: dict) -> dict:
    s = {"phone_pearson": rep["phone"]["pearson"], "word_pearson": rep["word"]["pearson"]}
    if rep.get("sentence"):
        s["sentence_accuracy_pearson"] = rep["sentence"]["accuracy"]["pearson"]
    if "flags" in rep:
        d = rep["flags"]["mispronounced_word_accuracy_le_6"]
        s.update(flag_miss_rate_le_6=d["miss_rate"], flag_precision_le_6=d["precision"],
                 flag_rate_on_words_scored_10=rep["flags"]["flag_rate_on_words_scored_10"])
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache-dir", type=Path, default=run_eval.ROOT / "data" / "pron_runs")
    ap.add_argument("--root", type=Path, default=so762.DEFAULT_ROOT)
    args = ap.parse_args()

    so762.check_speaker_disjoint(args.root)
    train, test = so762.load("train", args.root), so762.load("test", args.root)
    cache = {s: read_cache(args.cache_dir / f"gop_ctc-{s}.jsonl") for s in ("train", "test")}
    for s, utts in (("train", train), ("test", test)):
        missing = [u.utt_id for u in utts if u.utt_id not in cache[s]]
        if missing:
            raise SystemExit(f"gop_ctc-{s} cache lacks {len(missing)} utterances; run run_eval.py --system gop_ctc --split {s}")

    # --- fit on train only ---
    phone_coef, unit_bias = fit_phone(train, cache["train"])
    stub = Calibration(CALIBRATION_VERSION, tuple(phone_coef), unit_bias, (0.0, 1.0, 0.0), 0.0, 0.0)
    word_coef = fit_word(train, cache["train"], stub)
    cal = Calibration(CALIBRATION_VERSION, tuple(phone_coef), unit_bias, tuple(word_coef), 0.0, 0.0)
    train_preds = word_predictions(train, cache["train"], cal)
    sentence_coef = fit_sentence(train_preds)
    bands = choose_thresholds(train_preds)
    cal = Calibration(CALIBRATION_VERSION, tuple(phone_coef), unit_bias, tuple(word_coef),
                      bands["practice_below"], bands["check_below"])

    # --- evaluate (test used only here) ---
    ev = {}
    for s, utts in (("train", train), ("test", test)):
        raw = run_eval.evaluate(utts, raw_records(utts, cache[s]))
        calibrated = run_eval.evaluate(utts, records(utts, cache[s], cal, sentence_coef))
        ev[s] = {"raw_gop": raw, "calibrated": calibrated,
                 "band_confusion_rows_human_cols_predicted": band_confusion(word_predictions(utts, cache[s], cal), cal)}

    meta = json.loads((args.cache_dir / "gop_ctc-train.jsonl").read_text().splitlines()[0])["meta"]
    fitted_on = {
        "dataset": "speechocean762", "split": "train", "speakers": len({u.speaker for u in train}),
        "speaker_l1": "Mandarin (all speakers)", "utterances": len(train),
        "source": run_eval._verified(args.root),
    }
    calibration = {
        "calibration_version": CALIBRATION_VERSION,
        "created": datetime.now(timezone.utc).date().isoformat(),
        "status": "experimental: fitted on Mandarin-L1 speech only; NOT validated on Korean learners (TRIAGE PA-0); "
                  "bands are off unless VR_PRON_EXPERIMENTAL=1",
        "fitted_on": fitted_on,
        "korean_learner_validation": None,
        "models": {"phones": {"model_id": PHONES_ID, "revision": PHONES_REVISION},
                   "aligner": {"model_id": ALIGNER_ID, "revision": ALIGNER_REVISION}, "gop_method": meta["method"]},
        "expected_phones_during_fit": "speechocean762 canonical ARPAbet (the worker uses CMUdict at run time)",
        "phone": {"features": list(PHONE_FEATURES), "coef": phone_coef, "unit_bias": unit_bias,
                  "target": "human phone accuracy 0-2 (mean over the ARPAbet phones a unit covers)", "ridge": RIDGE},
        "word": {"features": list(WORD_FEATURES), "coef": word_coef,
                 "target": "human word accuracy 0-10", "ridge": RIDGE},
        "bands": {**bands, "chosen_on": "speechocean762 train",
                  "criterion": "max macro-F1 of predicted band vs human class practice (0-3) / check (4-6) / good (7-10)"},
        "eval": {s: {"raw_gop": summary(ev[s]["raw_gop"]), "calibrated": summary(ev[s]["calibrated"]),
                     "band_confusion_rows_human_cols_predicted": ev[s]["band_confusion_rows_human_cols_predicted"]}
                 for s in ev},
        "published_baselines": PUBLISHED,
    }
    cal_path = WORKER / "calibration" / f"{CALIBRATION_VERSION}.json"
    cal_path.parent.mkdir(exist_ok=True)
    cal_path.write_text(json.dumps(calibration, indent=2, ensure_ascii=False) + "\n")
    SENTENCE_MODEL.write_text(json.dumps({
        "calibration_version": CALIBRATION_VERSION, "features": list(SENTENCE_FEATURES), "coef": sentence_coef,
        "target": "human sentence accuracy 0-10", "fitted_on": "speechocean762 train",
        "note": "benchmark only, for comparison with published utterance-level results; the worker never computes a sentence score",
    }, indent=2) + "\n")
    fit_path = HERE / "results" / f"{CALIBRATION_VERSION}-fit.json"
    fit_path.write_text(json.dumps({"calibration_version": CALIBRATION_VERSION, "scorer": meta, "eval": ev}, indent=2) + "\n")
    print(json.dumps(calibration["eval"], indent=1))
    print(cal_path, SENTENCE_MODEL, fit_path, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
