"""English text normalization for TTS input (PROTOCOL §4).

Expands prices, percentages, times, dates, ordinals, decimals and plain numbers into words so the
model never has to guess how to read digits. Everything else is passed through unchanged.
CosyVoice control markup (``<|...|>`` tokens, ``[breath]``-style tags, HTML-ish tags) is stripped so
LLM output cannot inject model control tokens.
"""

import re

import inflect

MAX_CHARS = 400

_p = inflect.engine()

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
_CURRENCIES = {"$": ("dollar", "dollars", "cent", "cents"),
               "€": ("euro", "euros", "cent", "cents"),
               "£": ("pound", "pounds", "penny", "pence")}

_HANGUL = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")
_MARKUP = re.compile(r"<\|[^|>]*\|>|<[^<>]{0,40}>|\[[A-Za-z_ ]{1,30}\]")
_MONEY = re.compile(r"([$€£])\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b(?:\s?([ap])\.?m(\.?)(?![A-Za-z]))?", re.IGNORECASE)
_HOUR_AMPM = re.compile(r"\b(\d{1,2})\s?([ap])\.?m(\.?)(?![A-Za-z])", re.IGNORECASE)
_DECADE = re.compile(r"\b(1[1-9]\d0|20\d0)s\b")
_DATE = re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}})\b)?")
_YEAR_CONTEXT = re.compile(r"\b(in|since|by|of|year|until|from|before|after)\s+(1[1-9]\d\d|20\d\d)\b", re.IGNORECASE)
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_DECIMAL = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?!\d|\.\d)")
_PHONE = re.compile(r"\b\d{3}(?:-\d{3,4}){1,2}\b")
_GROUPED = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_INTEGER = re.compile(r"\d+")


class TextRejected(ValueError):
    """Input that must not be synthesized (code is sent to the client)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _words(n: int) -> str:
    return _p.number_to_words(n, andword="").replace(",", "")


def _year(y: int) -> str:
    if 2000 <= y <= 2009 or y % 1000 == 0:
        return _words(y)
    hi, lo = divmod(y, 100)
    if lo == 0:
        return f"{_words(hi)} hundred"
    return f"{_words(hi)} {'oh ' + _words(lo) if lo < 10 else _words(lo)}"


def _money(m: re.Match) -> str:
    one, many, sub_one, sub_many = _CURRENCIES[m.group(1)]
    major = int(m.group(2).replace(",", ""))
    minor = int((m.group(3) or "0").ljust(2, "0"))
    parts = []
    if major or not minor:
        parts.append(f"{_words(major)} {one if major == 1 else many}")
    if minor:
        parts.append(f"{_words(minor)} {sub_one if minor == 1 else sub_many}")
    return " and ".join(parts)


def _clock(m: re.Match, hour: int, minute: int, ampm: str | None, dot: str | None) -> str:
    if minute == 0:
        spoken = f"{_words(hour)} o'clock" if not ampm else _words(hour)
    elif minute < 10:
        spoken = f"{_words(hour)} oh {_words(minute)}"
    else:
        spoken = f"{_words(hour)} {_words(minute)}"
    if not ampm:
        return spoken
    # "a.m." swallows the sentence period; keep one if the sentence really ends here.
    ends_sentence = dot and re.match(r"\s*($|[A-Z\"'])", m.string[m.end():])
    return f"{spoken} {ampm.upper()} M" + ("." if ends_sentence else "")


def _decade(m: re.Match) -> str:
    head, _, last = _year(int(m.group(1))).rpartition(" ")
    return f"{head} {_p.plural(last)}".strip()


def _date(m: re.Match) -> str:
    day = _p.number_to_words(_p.ordinal(int(m.group(2))))
    out = f"{m.group(1)} {day}"
    if m.group(3):
        out += f", {_year(int(m.group(3)))}"
    return out


def normalize(text: str) -> str:
    """Return speakable English text or raise TextRejected."""
    if _HANGUL.search(text):
        raise TextRejected("TEXT_NOT_ENGLISH")
    if len(text) > MAX_CHARS:
        raise TextRejected("TEXT_TOO_LONG")
    t = _MARKUP.sub(" ", text)
    t = t.replace("|>", " ").replace("<|", " ")
    t = _MONEY.sub(_money, t)
    t = _PERCENT.sub(lambda m: f"{m.group(1)} percent", t)
    t = _TIME.sub(lambda m: _clock(m, int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)), t)
    t = _HOUR_AMPM.sub(lambda m: _clock(m, int(m.group(1)), 0, m.group(2), m.group(3)), t)
    t = _DECADE.sub(_decade, t)
    t = _DATE.sub(_date, t)
    t = _YEAR_CONTEXT.sub(lambda m: f"{m.group(1)} {_year(int(m.group(2)))}", t)
    t = _ORDINAL.sub(lambda m: _p.number_to_words(_p.ordinal(int(m.group(1)))), t)
    t = t.replace("24/7", "twenty-four seven")
    t = _PHONE.sub(lambda m: ", ".join(" ".join(_words(int(d)) for d in g) for g in m.group(0).split("-")), t)
    t = _GROUPED.sub(lambda m: m.group(0).replace(",", ""), t)
    t = _DECIMAL.sub(lambda m: f"{_words(int(m.group(1)))} point {' '.join(_words(int(d)) for d in m.group(2))}", t)
    t = _INTEGER.sub(lambda m: _words(int(m.group(0))), t)
    t = re.sub(r"\s+", " ", t).strip()
    if not re.search(r"[A-Za-z]", t):
        raise TextRejected("TEXT_EMPTY")
    return t
