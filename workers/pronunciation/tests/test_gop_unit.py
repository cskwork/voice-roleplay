"""Unit tests for /assess internals without the phone model: lexicon mapping, CTC GOP maths, calibration.

The GOP tests use FAKE log-posterior matrices (hand-built "the model heard exactly this" frames), not model output.
"""
import json
from pathlib import Path

import numpy as np
import pytest
from pron_worker.aligner import AlignedWord
from pron_worker.calibration import Calibration, load_calibration
from pron_worker.config import Settings
from pron_worker.gop import load_scorer, reference_tokens
from pron_worker.lexicon import Lexicon, arpabet_units, inventory
from pron_worker.phones import DELETION, INVENTORY, LABELS, score_words

VOCAB = Path(__file__).resolve().parents[3] / "models" / "wav2vec2-lv-60-espeak-cv-ft" / "vocab.json"


def ipas(units):
    return [u.ipa for u in units]


def test_arpabet_units_follow_espeak_conventions():
    assert ipas(arpabet_units(["K", "AA1", "R"])) == ["k", "ɑːɹ"]
    assert [u.src for u in arpabet_units(["K", "AA1", "R"])] == [(0,), (1, 2)]
    assert ipas(arpabet_units(["F", "AY1", "ER0"])) == ["f", "aɪɚ"]
    assert ipas(arpabet_units(["B", "ER1", "D"])) == ["b", "ɜː", "d"]
    assert ipas(arpabet_units(["DH", "AH0"])) == ["ð", "ə"]
    assert ipas(arpabet_units(["B", "AH1", "T"])) == ["b", "ʌ", "t"]
    assert ipas(arpabet_units(["M", "AE1", "R", "IY0"])) == ["m", "æ", "ɹ", "iː"]  # AE + R is not merged
    assert "ɾ" in arpabet_units(["T"])[0].accepted
    assert ipas(arpabet_units(["S", "IH1", "T"])) == ["s", "ɪ", "t"] and "iː" not in arpabet_units(["IH1"])[0].accepted
    with pytest.raises(KeyError):
        arpabet_units(["XX"])


@pytest.mark.skipif(not VOCAB.is_file(), reason="phone model not downloaded")
def test_inventory_is_in_the_model_vocabulary():
    vocab = json.loads(VOCAB.read_text())
    assert [u for u in inventory() if u not in vocab] == []


def test_lexicon(tmp_path):
    p = tmp_path / "cmudict.dict"
    p.write_text("hello HH AH0 L OW1\nhello(2) HH EH0 L OW1\ni'd AY1 D\nwell W EH1 L\nknown N OW1 N\nparis P EH1 R IH0 S # place\n")
    lex = Lexicon(p)
    assert [ipas(v) for v in lex.variants("Hello,")] == [["h", "ə", "l", "oʊ"], ["h", "ɛ", "l", "oʊ"]]
    assert ipas(lex.variants("I’d")[0]) == ["aɪ", "d"]
    assert ipas(lex.variants("well-known.")[0]) == ["w", "ɛ", "l", "n", "oʊ", "n"]
    assert ipas(lex.variants("Paris")[0]) == ["p", "ɛɹ", "ɪ", "s"]
    assert ipas(lex.variants("'well'.")[0]) == ["w", "ɛ", "l"]
    assert lex.variants("Bartholomew") == [] and lex.variants("42") == []  # numbers are not expanded


def test_reference_tokens():
    words = [AlignedWord(i, w, 0, 0) for i, w in enumerate(["wellknown", "Id", "say"])]
    assert reference_tokens("well-known, I'd — say!", words) == ["well-known,", "I'd", "say!"]
    assert reference_tokens("a b", words) == ["wellknown", "Id", "say"]  # counts differ: aligner words


def fake_log_probs(frames: list[str | None], confidence: float = 0.9) -> np.ndarray:
    """FAKE posteriors: each frame puts `confidence` on one label (None = CTC blank), the rest spread evenly."""
    k = len(INVENTORY) + 1
    lp = np.full((len(frames), k), np.log((1 - confidence) / (k - 1)))
    for t, label in enumerate(frames):
        lp[t, 0 if label is None else INVENTORY.index(label) + 1] = np.log(confidence)
    return lp.astype(np.float32)


def spoken(*units: str, frames_per_unit: int = 4) -> list[str | None]:
    out: list[str | None] = [None] * 3
    for u in units:
        out += [u] * frames_per_unit + [None]
    return out + [None] * 3


CAR = [arpabet_units(["K", "AA1", "R"])]


def test_gop_high_when_the_expected_units_were_heard():
    lp = fake_log_probs(spoken("k", "ɑːɹ"))
    [(units, res)] = score_words(lp, [AlignedWord(0, "car", 0, len(lp) * 20)], [CAR])
    assert ipas(units) == ["k", "ɑːɹ"]
    assert all(r.gop > -0.1 for r in res), res
    assert [r.heard[0][0] for r in res] == ["k", "ɑːɹ"]
    assert all(0 < p <= 1 for r in res for _, p in r.heard) and all(sum(p for _, p in r.heard) <= 1.001 for r in res)


def test_gop_low_for_a_substituted_unit_and_names_what_was_heard():
    lp = fake_log_probs(spoken("k", "æ"))  # said "cat"-like vowel instead of "ɑːɹ"
    [(_, res)] = score_words(lp, [AlignedWord(0, "car", 0, len(lp) * 20)], [CAR])
    assert res[0].gop > -0.1
    assert res[1].gop < -3 and res[1].heard[0][0] == "æ"


def test_gop_low_for_a_deleted_unit():
    lp = fake_log_probs(spoken("k"))
    [(_, res)] = score_words(lp, [AlignedWord(0, "car", 0, len(lp) * 20)], [CAR])
    assert res[1].gop < -3 and res[1].heard[0][0] == DELETION


def test_accepted_variant_is_not_penalised():
    lp = fake_log_probs(spoken("b", "ʌ", "ɾ"))  # flapped t
    [(_, res)] = score_words(lp, [AlignedWord(0, "but", 0, len(lp) * 20)], [[arpabet_units(["B", "AH1", "T"])]])
    assert res[2].gop > -0.1 and res[2].heard[0][0] == "ɾ"


def test_best_pronunciation_variant_is_used_and_neighbours_are_context():
    frames = spoken("ð", "iː", "k", "ɑːɹ")
    lp = fake_log_probs(frames)
    half = len(lp) * 10
    words = [AlignedWord(0, "the", 0, half), AlignedWord(1, "car", half, len(lp) * 20)]
    the = [arpabet_units(["DH", "AH0"]), arpabet_units(["DH", "IY0"])]
    (u0, r0), (_, r1) = score_words(lp, words, [the, CAR])
    assert ipas(u0) == ["ð", "iː"] and all(r.gop > -0.1 for r in r0 + r1)


def test_words_without_phones_and_empty_windows():
    lp = fake_log_probs(spoken("k", "ɑːɹ"))
    out = score_words(lp, [AlignedWord(0, "Bartholomew", 0, 100), AlignedWord(1, "car", 0, len(lp) * 20)], [[], CAR])
    assert out[0] == ([], []) and len(out[1][1]) == 2
    [(_, res)] = score_words(lp, [AlignedWord(0, "car", 10_000, 10_000)], [CAR])  # past the end of the audio
    assert [r.gop for r in res] == [None, None] and res[0].to_json() == {"expected_ipa": "k", "heard_candidates": [], "gop": None}
    assert LABELS[-1] == DELETION


CAL = {
    "calibration_version": "test-cal",
    "phone": {"features": ["intercept", "gop", "posterior"], "coef": [0.0, 0.0, 2.0], "unit_bias": {"k": 0.0}},
    "word": {"features": ["intercept", "mean_phone", "min_phone"], "coef": [0.0, 5.0, 0.0]},
    "bands": {"practice_below": 4.0, "check_below": 7.0},
}


def test_calibration_maps_gop_to_bands(tmp_path):
    cal = Calibration.from_json(CAL)
    assert cal.phone_score("k", 0.0) == pytest.approx(2.0) and cal.phone_score("k", None) is None
    assert cal.band(cal.word_score([("k", 0.0), ("ɑːɹ", 0.0)])) == "good"  # 10
    assert cal.band(cal.word_score([("k", np.log(0.6))])) == "check"  # 6
    assert cal.band(cal.word_score([("k", -5.0)])) == "practice"
    assert cal.word_score([("k", None)]) is None and cal.band(None) is None
    with pytest.raises(ValueError):
        Calibration.from_json({**CAL, "word": {**CAL["word"], "features": ["intercept"]}})
    assert load_calibration(tmp_path / "missing.json") is None
    (tmp_path / "c.json").write_text(json.dumps(CAL))
    assert load_calibration(tmp_path / "c.json").version == "test-cal"


def test_load_scorer_without_phone_model_files(tmp_path):
    s = Settings(token="t", port=0, model_dir=None, device="cpu", experimental=False,
                 phones_dir=tmp_path, lexicon_path=tmp_path / "cmudict.dict")
    assert load_scorer(s) is None
