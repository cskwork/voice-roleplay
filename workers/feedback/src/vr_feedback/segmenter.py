"""Turn a streamed LLM reply into speakable segments for TTS (PROTOCOL §6.3).

The first segment may be a clause (>= 4 words ending in , ; or a dash) so audio can start early;
after that only full sentences are emitted. A boundary is only accepted once the character after
it has arrived, so "$4." never splits before "50" shows up.
"""

from __future__ import annotations

import re
import unicodedata

# Never end a sentence: titles and Latin shorthands that are always followed by more words.
_NEVER_FINAL = {"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "e.g", "i.e", "approx"}
# May end a sentence, but only when the next word is capitalized ("at 7 a.m. See you").
_MAYBE_FINAL = {"a.m", "p.m", "etc", "u.s", "u.k", "inc", "ltd", "co", "no"}

_CLOSERS = "\"'”’)]"
_CLAUSE_PUNCT = ",;—–"
_MAX_CHARS = 300  # TTS accepts <= 400; force a split well before that


class SpeechSegmenter:
    def __init__(self) -> None:
        self._buf = ""
        self._emitted = 0

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            self._take(cut, out)
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        self._take(len(self._buf), out)
        return out

    def _take(self, cut: int, out: list[str]) -> None:
        raw, self._buf = self._buf[:cut], self._buf[cut:].lstrip()
        text = clean_for_speech(raw)
        if text:
            out.append(text)
            self._emitted += 1

    def _find_cut(self) -> int | None:
        buf = self._buf
        in_double = False
        open_curly = 0
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if ch == '"':
                in_double = not in_double
            elif ch == "“":
                open_curly += 1
            elif ch == "”":
                open_curly = max(0, open_curly - 1)
            quoted = in_double or open_curly > 0

            if ch in ".!?" and not quoted:
                j = i
                while j + 1 < n and buf[j + 1] in ".!?":
                    j += 1
                while j + 1 < n and buf[j + 1] in _CLOSERS:
                    j += 1
                if j + 1 >= n:
                    return None  # need the next character to decide
                if buf[j + 1].isspace():
                    nxt = _next_word_start(buf, j + 1)
                    if nxt is None:
                        return None
                    if self._is_sentence_end(buf, i, j, nxt):
                        return j + 1
                i = j + 1
                continue

            if (
                ch in _CLAUSE_PUNCT
                and not quoted
                and self._emitted == 0
                and i + 1 < n
                and buf[i + 1].isspace()
                and len(_words(buf[:i])) >= 4
            ):
                return i + 1
            i += 1

        if n > _MAX_CHARS:
            cut = buf.rfind(" ", 0, _MAX_CHARS)
            return cut if cut > 0 else _MAX_CHARS
        return None

    @staticmethod
    def _is_sentence_end(buf: str, dot: int, end: int, nxt: str) -> bool:
        if buf[dot] != ".":
            return True
        if end > dot and buf[dot + 1] == "." and not nxt.isupper():
            return False  # ellipsis mid-thought: "Well... let me see"
        word = re.search(r"([A-Za-z.]+)$", buf[:dot])
        token = word.group(1).lower().strip(".") if word else ""
        if token in _NEVER_FINAL:
            return False
        if token in _MAYBE_FINAL or re.fullmatch(r"[a-z](\.[a-z])+", token) or re.fullmatch(r"[a-z]", token):
            return nxt.isupper()
        return True


def _next_word_start(buf: str, i: int) -> str | None:
    while i < len(buf) and buf[i].isspace():
        i += 1
    return buf[i] if i < len(buf) else None


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text)


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_ACTION = re.compile(r"(?<!\*)\*(?!\*)[^*\n]{1,80}\*(?!\*)")  # *smiles*
_BRACKETED = re.compile(r"\[[^\]]{0,80}\]")  # [pause]
_LINE_MARKUP = re.compile(r"^[ \t]*(#+|[-•*]|>)[ \t]+", re.MULTILINE)  # headings, bullets, quotes
_MARKUP_CHARS = re.compile(r"[*_`~|^\\]+")
_SPACES = re.compile(r"\s+")


_EXTRA_SPEAKABLE = set("‘’“”—–…€£¥°\u00a0")


def _speakable_char(ch: str) -> bool:
    if ch.isascii():
        return ch.isprintable() or ch.isspace()
    if unicodedata.category(ch).startswith("L"):
        return unicodedata.name(ch, "").startswith("LATIN")
    return ch in _EXTRA_SPEAKABLE


def clean_for_speech(text: str) -> str:
    """Strip markdown, tags, stage directions, emoji and non-Latin script; collapse whitespace."""
    text = _LINE_MARKUP.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _ACTION.sub(" ", text)
    text = _BRACKETED.sub(" ", text)
    text = _MARKUP_CHARS.sub("", text)
    text = "".join(ch if _speakable_char(ch) else " " for ch in text)
    text = _SPACES.sub(" ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    if not re.search(r"[A-Za-z0-9]", text):
        return ""
    return text
