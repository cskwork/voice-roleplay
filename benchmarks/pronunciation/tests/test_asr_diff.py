"""Mapping of the reading diff onto corpus words (no model is loaded)."""

import pytest

from scorers.asr_diff import flags_per_word  # also puts workers/asr and workers/feedback/src on sys.path
from vr_feedback.reading import diff_target


def flags(target: str, heard: str) -> list[bool]:
    return flags_per_word(diff_target(target, heard), target.replace(",", "").replace("!", "").split())


def test_punctuation_and_case_do_not_flag():
    assert flags("AND FOR ALL YOU KNOW, YOU COULD BE RIGHT!", "And for all you know you could be right.") == [False] * 9


def test_substitution_deletion_and_insertion():
    assert flags("WE CALL IT BEAR", "We call it bike") == [False, False, False, True]
    assert flags("WE CALL IT BEAR", "We it bear") == [False, True, False, False]
    assert flags("WE CALL IT BEAR", "We call uh it bear") == [False] * 4  # insertions flag no target word


def test_apostrophe_word_is_one_token():
    assert flags("IT'S MARY'S", "its Mary's") == [True, False]


def test_misaligned_words_raise():
    with pytest.raises(ValueError):
        flags_per_word(diff_target("A B", "A B"), ["A"])
