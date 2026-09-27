"""Word-level diff between a reading target and what ASR heard (PROTOCOL §7).

Differences are "다르게 인식된 부분" (recognized differently), never pronunciation errors:
the ASR may simply have misheard.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .textutil import norm_word, words

DIFF_LABEL_KO = "다르게 인식된 부분"


def diff_target(target: str, transcript: str) -> list[dict]:
    t_words, h_words = words(target), words(transcript)
    matcher = SequenceMatcher(a=[norm_word(w) for w in t_words], b=[norm_word(w) for w in h_words], autojunk=False)
    out: list[dict] = []

    def add(op: str, t: str | None, h: str | None) -> None:
        item = {"op": op, "target": t, "heard": h}
        if op != "equal":
            item["label"] = DIFF_LABEL_KO
        out.append(item)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for t, h in zip(t_words[i1:i2], h_words[j1:j2]):
                add("equal", t, h)
        elif tag == "delete":
            for t in t_words[i1:i2]:
                add("missing", t, None)
        elif tag == "insert":
            for h in h_words[j1:j2]:
                add("extra", None, h)
        else:
            ts, hs = t_words[i1:i2], h_words[j1:j2]
            for k in range(max(len(ts), len(hs))):
                t = ts[k] if k < len(ts) else None
                h = hs[k] if k < len(hs) else None
                add("different" if t and h else ("missing" if t else "extra"), t, h)
    return out
