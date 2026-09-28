"""Expected phones for a reference text: CMUdict (ARPAbet) -> units of the phone recogniser.

facebook/wav2vec2-lv-60-espeak-cv-ft was trained on espeak-ng IPA transcriptions, so its English units follow espeak's
en-us conventions (e.g. a vowel + R inside a word is one unit such as "ɑːɹ"). This module maps ARPAbet to those units
without running espeak-ng (GPL-3.0, PROTOCOL §12.1): a fixed table written for this worker plus the merges below.

Each unit has a primary IPA token (`ipa`, what the learner is expected to say) and `accepted` variants that are normal
in American English and must not count against the learner (flapped t/d, reduced vowels, happy-tensing, the cot-caught
merger, and rhotic vs non-rhotic vowels: an r-less "car" is an accent, not an error, and speechocean762's own lexicon
is partly non-rhotic, e.g. "for" = F AO0). `src` lists the ARPAbet positions (within the word) the unit covers; merged units cover two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ARPAbet (stress digit removed; AH/ER split by stress) -> (primary IPA token, accepted variants).
ARPA = {
    "P": ("p", ()), "B": ("b", ()), "T": ("t", ("ɾ", "ʔ")), "D": ("d", ("ɾ",)), "K": ("k", ()), "G": ("ɡ", ()),
    "F": ("f", ()), "V": ("v", ()), "TH": ("θ", ()), "DH": ("ð", ()), "S": ("s", ()), "Z": ("z", ()),
    "SH": ("ʃ", ()), "ZH": ("ʒ", ()), "HH": ("h", ()), "CH": ("tʃ", ()), "JH": ("dʒ", ()),
    "M": ("m", ()), "N": ("n", ()), "NG": ("ŋ", ()), "L": ("l", ()), "R": ("ɹ", ()), "W": ("w", ()), "Y": ("j", ()),
    "IY": ("iː", ("i",)), "IH": ("ɪ", ("ᵻ",)), "EH": ("ɛ", ()), "AE": ("æ", ()),
    "AA": ("ɑː", ("ɑ", "ɑːɹ")), "AO": ("ɔː", ("ɑː", "ɔːɹ")), "UH": ("ʊ", ()), "UW": ("uː", ("u",)),
    "AH1": ("ʌ", ("ə",)), "AH0": ("ə", ("ɐ", "ʌ")),
    "ER1": ("ɜː", ("ɚ",)), "ER0": ("ɚ", ("ə",)),
    "EY": ("eɪ", ()), "AY": ("aɪ", ()), "OY": ("ɔɪ", ()), "AW": ("aʊ", ()), "OW": ("oʊ", ()),
}
# Vowel + R (same word) is one espeak unit; AY + ER is the "fire" triphthong unit.
MERGE_WITH_R = {"AA": ("ɑːɹ", ("ɑː",)), "AO": ("ɔːɹ", ("oːɹ", "ɔː")), "EH": ("ɛɹ", ("ɛ", "eə")),
                "IH": ("ɪɹ", ("ɪ", "iə")), "IY": ("ɪɹ", ("ɪ", "iə")), "UH": ("ʊɹ", ("ʊ", "ʊə")), "UW": ("ʊɹ", ("ʊ", "ʊə"))}
MERGE_AY_ER = ("aɪɚ", ("aɪə",))
# Version of the tables above; part of every GOP result's provenance (benchmark caches refuse other versions).
UNIT_TABLE_VERSION = "units-2"


@dataclass(frozen=True)
class Unit:
    ipa: str
    accepted: tuple[str, ...]
    src: tuple[int, ...]  # ARPAbet positions in the word covered by this unit


def _key(phone: str) -> str:
    base = phone.rstrip("012")
    if base in ("AH", "ER"):
        return base + ("0" if phone.endswith("0") else "1")
    return base


def arpabet_units(phones: list[str] | tuple[str, ...]) -> list[Unit]:
    """ARPAbet of one word (stress digits allowed) -> model units. Raises KeyError for an unknown phone."""
    bases = [p.rstrip("012") for p in phones]
    units, k = [], 0
    while k < len(phones):
        nxt = bases[k + 1] if k + 1 < len(phones) else None
        if nxt == "R" and bases[k] in MERGE_WITH_R:
            ipa, acc = MERGE_WITH_R[bases[k]]
            units.append(Unit(ipa, acc, (k, k + 1)))
            k += 2
        elif nxt == "ER" and bases[k] == "AY":
            units.append(Unit(*MERGE_AY_ER, (k, k + 1)))
            k += 2
        else:
            ipa, acc = ARPA[_key(phones[k])]
            units.append(Unit(ipa, acc, (k,)))
            k += 1
    return units


def inventory() -> tuple[str, ...]:
    """Every unit the GOP compares against (English units of the model plus accepted variants), fixed order."""
    seen: dict[str, None] = {}
    for ipa, acc in [*ARPA.values(), *MERGE_WITH_R.values(), MERGE_AY_ER]:
        for t in (ipa, *acc):
            seen[t] = None
    return tuple(seen)


_ENTRY = re.compile(r"^(\S+?)(?:\(\d+\))? (.+?)\s*(?:#.*)?$")
_EDGE_PUNCT = re.compile(r"^[^\w']+|[^\w']+$")


class Lexicon:
    """CMUdict (cmusphinx/cmudict `cmudict.dict`): lowercase word -> ARPAbet pronunciations in file order."""

    def __init__(self, path: Path):
        self.prons: dict[str, list[tuple[str, ...]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _ENTRY.match(line)
            if m:
                self.prons.setdefault(m[1], []).append(tuple(m[2].split()))

    def variants(self, token: str) -> list[list[Unit]]:
        """Unit sequences for a text token (all CMUdict pronunciations), [] if the word is not in the lexicon.
        Surrounding punctuation is dropped; a hyphenated token is looked up whole, else part by part."""
        word = _EDGE_PUNCT.sub("", token.replace("’", "'").lower())
        if word not in self.prons:
            word = word.strip("'")  # quoted words: 'small' (CMUdict also has words like 'bout, so try as-is first)
        if word in self.prons:
            return [arpabet_units(p) for p in self.prons[word]]
        parts = [p for p in word.split("-") if p]
        if len(parts) > 1 and all(p in self.prons for p in parts):
            # First pronunciation of each part; units keep their own positions per part (src is informational only).
            return [[u for p in parts for u in arpabet_units(self.prons[p][0])]]
        return []
