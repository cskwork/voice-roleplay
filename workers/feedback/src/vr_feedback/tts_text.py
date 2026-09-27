"""Spell out prices, times, dates, ordinals and a few symbols so TTS reads them correctly.

Plain words and plain integers are left alone; the TTS front end reads those fine.
"""

from __future__ import annotations

import re

_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL_IRREGULAR = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_CURRENCY = {"$": ("dollar", "dollars", "cent", "cents"), "€": ("euro", "euros", "cent", "cents"), "£": ("pound", "pounds", "penny", "pence")}


def number_to_words(n: int) -> str:
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + (" " + number_to_words(rest) if rest else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            high, rest = divmod(n, size)
            return number_to_words(high) + " " + name + (" " + number_to_words(rest) if rest else "")
    raise AssertionError("unreachable")


def ordinal_words(n: int) -> str:
    words = number_to_words(n)
    head, sep, last = words.rpartition("-" if "-" in words.split(" ")[-1] else " ")
    if last in _ORDINAL_IRREGULAR:
        last = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + last


def year_words(y: int) -> str:
    if 2000 <= y <= 2009:
        return number_to_words(y)
    high, low = divmod(y, 100)
    if low == 0:
        return number_to_words(high) + " hundred"
    return number_to_words(high) + " " + (number_to_words(low) if low >= 10 else "oh " + number_to_words(low))


def _money(m: re.Match) -> str:
    one, many, sub_one, sub_many = _CURRENCY[m.group(1)]
    whole = int(m.group(2).replace(",", ""))
    frac = m.group(3)
    cents = int((frac + "0")[:2]) if frac else 0
    parts = []
    if whole or not cents:
        parts.append(f"{number_to_words(whole)} {one if whole == 1 else many}")
    if cents:
        parts.append(f"{number_to_words(cents)} {sub_one if cents == 1 else sub_many}")
    return " and ".join(parts)


def _clock(hour: int, minute: int) -> str:
    if minute == 0:
        return f"{number_to_words(hour)} o'clock"
    if minute < 10:
        return f"{number_to_words(hour)} oh {number_to_words(minute)}"
    return f"{number_to_words(hour)} {number_to_words(minute)}"


def _time(m: re.Match) -> str:
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if meridiem:
        suffix = "a.m." if meridiem.lower().startswith("a") else "p.m."
        spoken = number_to_words(hour) if minute == 0 else _clock(hour, minute)
        return f"{spoken} {suffix}"
    return _clock(hour, minute)


def _month_day(m: re.Match) -> str:
    out = f"{m.group(1)} {ordinal_words(int(m.group(2)))}"
    if m.group(3):
        out += f", {year_words(int(m.group(3)))}"
    return out


def _numeric_date(m: re.Match) -> str:
    month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return m.group(0)
    year_num = int(year)
    if len(year) == 2:
        year_num += 2000
    return f"{_MONTHS[month - 1]} {ordinal_words(day)}, {year_words(year_num)}"


_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"([$€£])\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)"), _money),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b"), _numeric_date),
    (re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?"), _month_day),
    (re.compile(r"\b(\d{1,2}):(\d{2})\s*([aApP]\.?[mM]\.?)?(?![\w:])"), _time),
    (re.compile(r"\b(\d{1,2})()\s*([aApP]\.?[mM]\.?)(?![\w])"), _time),
    (re.compile(r"\b(\d+)(?:st|nd|rd|th)\b"), lambda m: ordinal_words(int(m.group(1)))),
    (re.compile(r"(\d)\s?%"), r"\1 percent"),
    (re.compile(r"(\d)\s?°\s?([CF])\b"), lambda m: f"{m.group(1)} degrees " + ("Celsius" if m.group(2) == "C" else "Fahrenheit")),
    (re.compile(r"#\s?(\d)"), r"number \1"),
    (re.compile(r"\s&\s"), " and "),
    (re.compile(r"\s\+\s"), " plus "),
    (re.compile(r"\s=\s"), " equals "),
    (re.compile(r"(\w)@(\w)"), r"\1 at \2"),
]


def normalize_for_tts(text: str) -> str:
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()
