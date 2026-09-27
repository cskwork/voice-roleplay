"""Pure unit tests (no model)."""

import pytest

from vr_feedback.echo import echo_overlap
from vr_feedback.metrics import compute_metrics
from vr_feedback.reading import diff_target
from vr_feedback.segmenter import SpeechSegmenter, clean_for_speech
from vr_feedback.tts_text import normalize_for_tts, ordinal_words


def stream(text, step=1):
    seg = SpeechSegmenter()
    out = []
    for i in range(0, len(text), step):
        out += seg.feed(text[i : i + step])
    return out + seg.flush()


# --- segmenter ---------------------------------------------------------------

@pytest.mark.parametrize("step", [1, 3, 7, 1000])
def test_segmenter_numbers_and_abbreviations(step):
    text = "That's $4.50 for the latte. Mr. Kim opens at 7 a.m. every day. The U.S. office is 3.5 miles away, e.g. near the park. Thanks!"
    assert stream(text, step) == [
        "That's $4.50 for the latte.",
        "Mr. Kim opens at 7 a.m. every day.",
        "The U.S. office is 3.5 miles away, e.g. near the park.",
        "Thanks!",
    ]


def test_segmenter_waits_for_next_char_before_splitting():
    seg = SpeechSegmenter()
    assert seg.feed("It costs $4.") == []
    assert seg.feed("50 today. ") == []  # boundary not decided until the next word arrives
    assert seg.feed("Anything") == ["It costs $4.50 today."]
    assert seg.flush() == ["Anything"]


def test_segmenter_abbreviation_can_end_sentence_before_capital():
    assert stream("We open at 7 a.m. See you then.") == ["We open at 7 a.m.", "See you then."]


def test_segmenter_first_clause_only():
    text = "Sure, I can help with that, no problem at all, and here is more. Next one, still one sentence."
    # "Sure," has < 4 words so no clause; the first ",": after "help with that" (5 words) cuts.
    assert stream(text) == [
        "Sure, I can help with that,",
        "no problem at all, and here is more.",
        "Next one, still one sentence.",
    ]


def test_segmenter_does_not_split_inside_quotes():
    assert stream('She said "Wait. Stop." Then she left. Okay.') == ['She said "Wait. Stop." Then she left.', "Okay."]


def test_segmenter_strips_markup_emoji_and_other_scripts():
    assert stream("**Great** choice! 😊 *smiles* 감사합니다 Enjoy your [pause] coffee.") == ["Great choice!", "Enjoy your coffee."]
    assert clean_for_speech("# Title\n- item one") == "Title item one"
    assert clean_for_speech("😊 ✨") == ""


def test_segmenter_forces_split_on_runaway_text():
    out = stream("word " * 200)
    assert len(out) > 1 and all(len(s) <= 300 for s in out)


# --- tts_text ----------------------------------------------------------------

@pytest.mark.parametrize("src,expected", [
    ("That's $4.50.", "That's four dollars and fifty cents."),
    ("$1", "one dollar"),
    ("$0.75 extra", "seventy-five cents extra"),
    ("$1,200", "one thousand two hundred dollars"),
    ("$1.01", "one dollar and one cent"),
    ("£3.20", "three pounds and twenty pence"),
    ("at 7:30 a.m.", "at seven thirty a.m."),
    ("at 7pm", "at seven p.m."),
    ("by 10:05", "by ten oh five"),
    ("at 9:00", "at nine o'clock"),
    ("March 3rd, 2026", "March third, twenty twenty-six"),
    ("on 12/25/2025", "on December twenty-fifth, twenty twenty-five"),
    ("the 21st floor", "the twenty-first floor"),
    ("50% off", "50 percent off"),
    ("room #3", "room number 3"),
    ("Tom & Jerry", "Tom and Jerry"),
    ("I'd like a latte, please.", "I'd like a latte, please."),
    ("Room 1203 is ready.", "Room 1203 is ready."),
])
def test_normalize_for_tts(src, expected):
    assert normalize_for_tts(src) == expected


def test_ordinals():
    assert [ordinal_words(n) for n in (1, 2, 3, 4, 11, 12, 20, 22, 100, 101)] == [
        "first", "second", "third", "fourth", "eleventh", "twelfth", "twentieth", "twenty-second", "one hundredth", "one hundred first",
    ]


# --- metrics (hand-computed) ---------------------------------------------------

def test_metrics_basic():
    # span 1.0 -> 5.0 = 4 s; gaps 0.6 (pause) and 0.3 (not); 8 words -> 120 wpm
    m = compute_metrics([(1.0, 2.0), (2.6, 4.0), (4.3, 5.0)], "I would like a large latte with oat.")
    assert m["word_count"] == 8
    assert m["speech_span_s"] == 4.0
    assert m["wpm"] == 120.0
    assert m["pause_count"] == 1
    assert m["total_pause_s"] == pytest.approx(0.6)
    assert m["metrics_version"] == "m1"
    assert "500 ms" in m["definition_ko"]


def test_metrics_threshold_inclusive_and_overlap_merge():
    m = compute_metrics([(0.0, 1.0), (0.8, 1.5), (2.0, 3.0)], "one two three")
    assert m["pause_count"] == 1 and m["total_pause_s"] == 0.5
    assert m["wpm"] == 60.0  # 3 words / 3 s


def test_metrics_wpm_none_cases():
    assert compute_metrics([(0.0, 0.9)], "hi there")["wpm"] is None  # span < 1 s
    assert compute_metrics([(0.0, 3.0)], "")["wpm"] is None  # no words
    assert compute_metrics([], "hello")["wpm"] is None


# --- reading diff -------------------------------------------------------------

def test_diff_target_ops():
    d = diff_target("I'd like a large latte, please.", "i'd like the large latte please now")
    assert [x["op"] for x in d] == ["equal", "equal", "different", "equal", "equal", "equal", "extra"]
    assert d[2] == {"op": "different", "target": "a", "heard": "the", "label": "다르게 인식된 부분"}
    assert "pronunciation" not in str(d).lower()


def test_diff_target_missing():
    d = diff_target("Can I get a receipt?", "Can I a receipt")
    assert {"op": "missing", "target": "get", "heard": None, "label": "다르게 인식된 부분"} in d


# --- echo ---------------------------------------------------------------------

def test_echo_overlap():
    ai = "Would you like it in regular or large size?"
    assert echo_overlap("would you like it in regular", ai) == 1.0
    assert echo_overlap("I want a large latte", ai) == pytest.approx(1 / 5)
    assert echo_overlap("size large or regular", ai) == pytest.approx(1 / 4)  # reversed order counts once
    assert echo_overlap("", ai) == 0.0
