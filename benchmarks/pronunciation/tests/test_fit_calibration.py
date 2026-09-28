"""fit_calibration.py maths on FAKE inputs (invented utterances, GOP values and scores; no model, no corpus).

Needs pron_worker importable: run with workers/pronunciation/.venv/bin/python (skipped elsewhere).
"""

import numpy as np
import pytest

fit = pytest.importorskip("fit_calibration", reason="run with workers/pronunciation/.venv/bin/python", exc_type=ImportError)

import run_eval  # noqa: E402
import so762  # noqa: E402
from pron_worker.calibration import Calibration  # noqa: E402


def fake_word(text, phones, phone_acc, acc):
    return so762.Word(text=text, phones=tuple(phones), phone_accuracy=tuple(phone_acc), accuracy=acc, stress=10, total=acc)


FAKE_UTT = so762.Utterance(
    utt_id="u1", split="test", speaker="s1", age=30, gender="f", text="CAR IT",
    words=(fake_word("CAR", ["K", "AA1", "R"], [2, 0, 1], 3.0), fake_word("IT", ["IH0", "T"], [2, 2], 10.0)),
    sentence={"accuracy": 6.0},
)
# FAKE cache record: units as scorers.gop_ctc stores them ([ipa, gop, arpabet positions]); "ɑːɹ" covers AA1 + R.
FAKE_CACHE = {"u1": {"utt_id": "u1", "audio_ms": 1000, "elapsed_ms": 10, "output": {"extra": {"units": [
    [["k", 0.0, [0]], ["ɑːɹ", -6.0, [1, 2]]],
    [["ɪ", -0.1, [0]], ["t", 0.0, [1]]],
]}}}}
CAL = Calibration("fake", (0.0, 0.0, 2.0), {}, (0.0, 0.0, 5.0), 4.0, 7.0)  # phone = 2*posterior, word = 5*min


def test_ridge_leaves_the_intercept_unpenalised():
    X = np.column_stack([np.ones(50), np.linspace(-1, 1, 50)])
    beta = fit.ridge(X, 3.0 + 0 * X[:, 1], lam=100.0)
    assert beta[0] == pytest.approx(3.0) and beta[1] == pytest.approx(0.0)


def test_unit_rows_average_the_covered_human_phones():
    rows = list(fit.unit_rows([FAKE_UTT], FAKE_CACHE))
    assert rows == [("k", 0.0, 2.0), ("ɑːɹ", -6.0, 0.5), ("ɪ", -0.1, 2.0), ("t", 0.0, 2.0)]


def test_records_apply_the_calibration_like_the_scorer():
    rec = fit.records([FAKE_UTT], FAKE_CACHE, CAL, [0.0, 1.0, 0.0])["u1"]["output"]
    car, it = rec["words"]
    assert car["phones"][0] == pytest.approx(2.0) and car["phones"][1] == car["phones"][2] == pytest.approx(2 * np.exp(-6.0))
    assert car["flagged"] is True and it["flagged"] is False  # CAR: 5*min ~ 0.02 -> practice; IT: ~9.05 -> good
    assert CAL.band(car["accuracy"]) == "practice" and CAL.band(it["accuracy"]) == "good"
    assert rec["sentence"]["accuracy"] == pytest.approx((car["accuracy"] + it["accuracy"]) / 2)
    rep = run_eval.evaluate([FAKE_UTT], {"u1": {**FAKE_CACHE["u1"], "output": rec}})
    assert rep["phone"]["n"] == 5 and rep["flags"]["mispronounced_word_accuracy_le_6"]["flagged_positives"] == 1


def test_thresholds_are_chosen_by_macro_f1():
    preds = [(None, [(2.0, 1.0), (3.0, 2.0), (5.0, 5.0), (6.0, 5.5), (10.0, 9.0), (8.0, 9.5), (10.0, 9.9)])]
    t = fit.choose_thresholds(preds)
    assert t["train_macro_f1"] == 1.0
    assert 2.0 < t["practice_below"] <= 5.0 and 5.5 < t["check_below"] <= 9.0
    assert [fit.human_band(a) for a in (0, 3, 4, 6, 7, 10)] == ["practice", "practice", "check", "check", "good", "good"]
