"""Word tokenization shared by metrics, reading diff and echo detection."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*(?:[.,][0-9]+)*")


def words(text: str) -> list[str]:
    """Words as written (punctuation stripped), e.g. "I'd like $4.50." -> ["I'd", "like", "4.50"]."""
    return _WORD.findall(text or "")


def norm_word(word: str) -> str:
    return word.lower().replace("’", "'")
