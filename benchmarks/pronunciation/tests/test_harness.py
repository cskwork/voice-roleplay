"""Loader and run_eval tests on a FAKE miniature corpus (synthetic tones, invented scores) and a FAKE scorer."""

import json
import wave

import numpy as np
import pytest

import run_eval
import so762

FAKE_SCORES = {
    "000010001": {"text": "WE CALL IT.", "accuracy": 8, "completeness": 10, "fluency": 9, "prosodic": 9, "total": 8,
                  "words": [
                      {"text": "WE", "phones": ["W", "IY0"], "phones-accuracy": [2, 2], "accuracy": 10, "stress": 10, "total": 10},
                      {"text": "CALL", "phones": ["K", "AO0", "L"], "phones-accuracy": [2, 0.4, 1], "accuracy": 5, "stress": 10, "total": 5,
                       "mispronunciations": [{"canonical-phone": "AO0", "index": 1, "pronounced-phone": "AA0"}]},
                      {"text": "IT", "phones": ["IH0", "T"], "phones-accuracy": [2, 2], "accuracy": 10, "stress": 10, "total": 10}]},
    "000020001": {"text": "BEAR", "accuracy": 3, "completeness": 10, "fluency": 4, "prosodic": 4, "total": 3,
                  "words": [{"text": "BEAR", "phones": ["B", "EH0", "R"], "phones-accuracy": [2, 0, 0], "accuracy": 2, "stress": 10, "total": 2}]},
    "000030001": {"text": "IT'S OK", "accuracy": 10, "completeness": 10, "fluency": 10, "prosodic": 10, "total": 10,
                  "words": [
                      {"text": "IT'S", "phones": ["IH0", "T", "S"], "phones-accuracy": [2, 2, 2], "accuracy": 10, "stress": 10, "total": 10},
                      {"text": "OK", "phones": ["OW0", "K", "EY1"], "phones-accuracy": [2, 2, 2], "accuracy": 10, "stress": 10, "total": 10}]},
}
SPLIT_OF = {"0001": "train", "0002": "test", "0003": "test"}


def write_wav(path, seconds=0.5, rate=16_000, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.repeat(pcm, channels).tobytes())


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "so762"
    (root / "resource").mkdir(parents=True)
    (root / "resource" / "scores.json").write_text(json.dumps(FAKE_SCORES))
    for split in so762.SPLITS:
        d = root / split
        d.mkdir()
        utts = [u for u in FAKE_SCORES if SPLIT_OF[u[1:5]] == split]
        spks = sorted({u[1:5] for u in utts})
        (d / "text").write_text("".join(f"{u}\t{FAKE_SCORES[u]['text']}\n" for u in utts))
        (d / "wav.scp").write_text("".join(f"{u}\tWAVE/SPEAKER{u[1:5]}/{u}.WAV\n" for u in utts))
        (d / "utt2spk").write_text("".join(f"{u} {u[1:5]}\n" for u in utts))
        (d / "spk2age").write_text("".join(f"{s}\t{8 if s == '0002' else 30}\n" for s in spks))
        (d / "spk2gender").write_text("".join(f"{s}\tf\n" for s in spks))
        for u in utts:
            (root / "WAVE" / f"SPEAKER{u[1:5]}").mkdir(parents=True, exist_ok=True)
            write_wav(root / "WAVE" / f"SPEAKER{u[1:5]}" / f"{u}.WAV")
    return root


class FakeScorer:
    """FAKE scorer: returns the human scores back (optionally flipped) so metric values are predictable."""

    name = "fake"

    def __init__(self, flip=False, drop_phone=False):
        self.flip, self.drop_phone, self.calls = flip, drop_phone, 0

    def info(self):
        return {"method": "fake", "flip": self.flip}

    def score(self, wav, reference_text, ref_words):
        assert wav.dtype == np.float32 and len(wav) == 8000
        self.calls += 1
        sign = -1 if self.flip else 1
        return {
            "sentence": {"accuracy": 1.0 * len(reference_text)},
            "words": [
                {"accuracy": sign * w.accuracy, "flagged": w.accuracy < 10,
                 "phones": list(w.phone_accuracy[:-1] if self.drop_phone else w.phone_accuracy)}
                for w in ref_words
            ],
        }


def test_load_parses_scores_and_metadata(corpus):
    test = so762.load("test", corpus)
    assert [u.utt_id for u in test] == ["000020001", "000030001"]
    u = test[0]
    assert (u.speaker, u.age, u.gender, u.text) == ("0002", 8, "f", "BEAR")
    assert u.words[0].phones == ("B", "EH0", "R") and u.words[0].phone_accuracy == (2.0, 0.0, 0.0)
    assert u.sentence["fluency"] == 4.0
    assert u.load_audio().shape == (8000,)
    train = so762.load("train", corpus)
    assert train[0].words[1].mispronunciations[0]["pronounced-phone"] == "AA0"
    so762.check_speaker_disjoint(corpus)


def test_speaker_overlap_and_bad_wav_are_errors(corpus):
    with open(corpus / "test" / "utt2spk", "a") as f:
        f.write("000010001 0001\n")
    with pytest.raises(ValueError, match="both train and test"):
        so762.check_speaker_disjoint(corpus)
    write_wav(corpus / "WAVE" / "SPEAKER0002" / "000020001.WAV", channels=2)
    with pytest.raises(ValueError, match="16 kHz mono"):
        so762.load("test", corpus)[0].load_audio()


def test_missing_corpus_says_how_to_fetch(tmp_path):
    with pytest.raises(FileNotFoundError, match="fetch_speechocean762"):
        so762.load("test", tmp_path)


def test_run_scorer_caches_and_resumes(corpus, tmp_path):
    utts = so762.load("test", corpus) + so762.load("train", corpus)
    cache = tmp_path / "cache.jsonl"
    first = FakeScorer()
    run_eval.run_scorer(first, utts[:1], cache, fresh=False)
    second = FakeScorer()
    records = run_eval.run_scorer(second, utts, cache, fresh=False)
    assert (first.calls, second.calls) == (1, 2)
    assert set(records) == {u.utt_id for u in utts}
    with pytest.raises(SystemExit, match="--fresh"):
        run_eval.run_scorer(FakeScorer(flip=True), utts, cache, fresh=False)
    run_eval.run_scorer(FakeScorer(flip=True), utts, cache, fresh=True)


def test_evaluate_perfect_and_inverted_scorer(corpus, tmp_path):
    utts = so762.load("test", corpus) + so762.load("train", corpus)
    rec = run_eval.run_scorer(FakeScorer(), utts, tmp_path / "a.jsonl", fresh=False)
    rep = run_eval.evaluate(utts, rec)
    assert rep["word"]["pearson"] == pytest.approx(1.0) and rep["word"]["n"] == 6
    assert rep["phone"]["pearson"] == pytest.approx(1.0) and rep["phone"]["coverage"] == 1.0
    fl = rep["flags"]["mispronounced_word_accuracy_le_6"]
    assert (fl["positives"], fl["missed_positives"], fl["precision"]) == (2, 0, 1.0)
    assert rep["flags"]["severe_word_accuracy_le_3"]["positives"] == 1
    assert rep["flags"]["flag_rate_on_words_scored_10"] == 0.0
    assert set(rep["by_age_group"]) == {"child", "adult"}
    assert "sentence" in run_eval.to_markdown({
        "system": "fake", "split": "test", "generated_at": "-", "machine": "-", "scorer": {}, "dataset": {},
        "results": rep,
    })

    inv = run_eval.evaluate(utts, run_eval.run_scorer(FakeScorer(flip=True), utts, tmp_path / "b.jsonl", False))
    assert inv["word"]["pearson"] == pytest.approx(-1.0)


def test_phone_lists_of_wrong_length_are_not_covered(corpus, tmp_path):
    utts = so762.load("test", corpus)
    rep = run_eval.evaluate(utts, run_eval.run_scorer(FakeScorer(drop_phone=True), utts, tmp_path / "c.jsonl", False))
    assert rep["phone"] is None


def test_wrong_word_count_is_an_error(corpus, tmp_path):
    utts = so762.load("test", corpus)
    rec = run_eval.run_scorer(FakeScorer(), utts, tmp_path / "d.jsonl", False)
    rec[utts[1].utt_id]["output"]["words"].pop()
    with pytest.raises(ValueError, match="expected 2"):
        run_eval.evaluate(utts, rec)
