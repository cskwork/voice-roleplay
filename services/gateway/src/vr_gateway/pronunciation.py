"""Pronunciation analysis of recorded attempts (PROTOCOL §12.4, PRD §8.4, TRIAGE PA-1/PA-4/PA-5/PA-6).

Default `timing_only`: word timings (`/align`) and pitch contours (`/prosody`) of the learner's take and of the model
audio, no judgements. `experimental_banded` only when the worker reports `bands_enabled` (VR_PRON_EXPERIMENTAL=1 and
a calibration file): `/assess` replaces `/align`, and word bands plus "heard" phone candidates are passed on. GOP
values and posteriors never leave this module. Any failure gives `status: "unavailable"`; it never fails the job.
Nothing here is logged except status, reason codes, counts and durations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .audio import to_mono_16k, wav_pcm
from .workers import WorkerError

if TYPE_CHECKING:
    from .app import Services
    from .jobs import Attempt

log = logging.getLogger("vr_gateway.pronunciation")

SCRIPTED_TYPES = ("reading", "shadowing", "drill")  # reference = the target sentence; others use ASR revision 1
TOP_STATUS = {"unavailable": "assessment_unavailable", "timing_only": "timing_only",
              "experimental_banded": "experimental_banded"}
NOTE_KO = {
    "timing_only": "단어 위치와 억양 곡선만 보여 줍니다. 발음을 채점하지 않습니다.",
    "experimental_banded": "실험 기능입니다. 보정 데이터가 한국인 학습자로 검증되지 않았으므로 결과가 틀릴 수 있어요.",
    "unavailable": "이번 녹음은 발음 분석을 하지 못했어요.",
}
# A phone is shown as "heard differently" when the phone model gives the accepted pronunciations of the expected
# unit less than half of its probability mass (worker GOP = ln(accepted share)). Only in experimental_banded, only
# for words banded check/practice; the value itself never leaves the gateway.
WEAK_GOP = math.log(0.5)

# CMUdict ARPAbet -> (primary unit, accepted variants) of the phone model's espeak-style units. Kept in step with
# workers/pronunciation/pron_worker/lexicon.py (units-2); used to link words to guide entries in timing_only mode
# and to skip accepted variants when picking a "heard" candidate.
ARPA = {
    "P": ("p", ()), "B": ("b", ()), "T": ("t", ("ɾ", "ʔ")), "D": ("d", ("ɾ",)), "K": ("k", ()), "G": ("ɡ", ()),
    "F": ("f", ()), "V": ("v", ()), "TH": ("θ", ()), "DH": ("ð", ()), "S": ("s", ()), "Z": ("z", ()),
    "SH": ("ʃ", ()), "ZH": ("ʒ", ()), "HH": ("h", ()), "CH": ("tʃ", ()), "JH": ("dʒ", ()),
    "M": ("m", ()), "N": ("n", ()), "NG": ("ŋ", ()), "L": ("l", ()), "R": ("ɹ", ()), "W": ("w", ()), "Y": ("j", ()),
    "IY": ("iː", ("i",)), "IH": ("ɪ", ("ᵻ",)), "EH": ("ɛ", ()), "AE": ("æ", ()),
    "AA": ("ɑː", ("ɑ", "ɑːɹ")), "AO": ("ɔː", ("ɑː", "ɔːɹ")), "UH": ("ʊ", ()), "UW": ("uː", ("u",)),
    "AH1": ("ʌ", ("ə",)), "AH0": ("ə", ("ɐ", "ʌ")), "ER1": ("ɜː", ("ɚ",)), "ER0": ("ɚ", ("ə",)),
    "EY": ("eɪ", ()), "AY": ("aɪ", ()), "OY": ("ɔɪ", ()), "AW": ("aʊ", ()), "OW": ("oʊ", ()),
}
MERGE_WITH_R = {"AA": ("ɑːɹ", ("ɑː",)), "AO": ("ɔːɹ", ("oːɹ", "ɔː")), "EH": ("ɛɹ", ("ɛ", "eə")),
                "IH": ("ɪɹ", ("ɪ", "iə")), "IY": ("ɪɹ", ("ɪ", "iə")), "UH": ("ʊɹ", ("ʊ", "ʊə")),
                "UW": ("ʊɹ", ("ʊ", "ʊə"))}
MERGE_AY_ER = ("aɪɚ", ("aɪə",))
ACCEPTED = {ipa: set(acc) for ipa, acc in [*ARPA.values(), *MERGE_WITH_R.values(), MERGE_AY_ER]}

_ENTRY = re.compile(r"^(\S+?)(?:\(\d+\))? (.+?)\s*(?:#.*)?$")
_EDGE_PUNCT = re.compile(r"^[^\w']+|[^\w']+$")
_KEEP = re.compile(r"[^0-9a-z']")


def arpabet_units(phones: list[str]) -> list[str]:
    """Primary units of one CMUdict pronunciation (vowel + R and AY + ER merged as in the worker)."""
    bases = [p.rstrip("012") for p in phones]
    out, k = [], 0
    while k < len(phones):
        nxt = bases[k + 1] if k + 1 < len(phones) else None
        if nxt == "R" and bases[k] in MERGE_WITH_R:
            out.append(MERGE_WITH_R[bases[k]][0])
            k += 2
        elif nxt == "ER" and bases[k] == "AY":
            out.append(MERGE_AY_ER[0])
            k += 2
        else:
            base = bases[k]
            key = base + ("0" if phones[k].endswith("0") else "1") if base in ("AH", "ER") else base
            if key in ARPA:
                out.append(ARPA[key][0])
            k += 1
    return out


class GuideIndex:
    """Which guide entries (content/pronunciation/guide.json, PA-8) describe the sounds of a word.

    This only means "this sound is in the word" (content/pronunciation/README.md); it is never a judgement.
    Both files are optional and loaded on first use: without them words simply get no guide links."""

    def __init__(self, guide_path: Path, lexicon_path: Path):
        self.guide_path = guide_path
        self.lexicon_path = lexicon_path
        self._expected: dict[str, list[str]] | None = None
        self._lexicon: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    def _load(self) -> None:
        expected: dict[str, list[str]] = {}
        try:
            guide = json.loads(self.guide_path.read_text(encoding="utf-8"))
            for row in guide.get("espeak_map", []):
                if row.get("context") == "expected":
                    expected[row["espeak"]] = list(row["entry_ids"])
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("pron_guide_unavailable")
        lexicon: dict[str, str] = {}
        try:
            with self.lexicon_path.open(encoding="utf-8") as f:
                for line in f:
                    m = _ENTRY.match(line)
                    if m and m[1] not in lexicon:  # first pronunciation only
                        lexicon[m[1]] = m[2]
        except OSError:
            log.warning("pron_lexicon_unavailable")
        self._expected, self._lexicon = expected, lexicon

    async def ensure_loaded(self) -> None:
        async with self._lock:
            if self._expected is None:
                await asyncio.to_thread(self._load)

    def for_units(self, units: list[str]) -> list[str]:
        out: list[str] = []
        for unit in units:
            for entry in (self._expected or {}).get(unit, []):
                if entry not in out:
                    out.append(entry)
        return out

    def units_for_word(self, word: str) -> list[str]:
        token = _EDGE_PUNCT.sub("", word.replace("’", "'").lower())
        pron = (self._lexicon or {}).get(token) or (self._lexicon or {}).get(token.strip("'"))
        return arpabet_units(pron.split()) if pron else []


def call_timeout(duration_s: float) -> float:
    """Per worker call (PROTOCOL §12.4 initial values; replace with measured ones)."""
    return 30.0 if duration_s <= 30.0 else 90.0


def unavailable(reason: str, mode: str | None) -> dict:
    return {"status": "unavailable", "reason": reason, "mode": mode, "transcript_revision": 1, "words": [],
            "prosody": {"learner": None, "model": None}, "calibration_version": None, "model_revisions": {},
            "evidence_types": [], "note_ko": NOTE_KO["unavailable"]}


def top_status(pron: dict | None) -> str:
    return TOP_STATUS[pron["status"]] if pron else "assessment_unavailable"


def mode_for(exercise_type: str) -> str:
    return "scripted" if exercise_type in SCRIPTED_TYPES else "unscripted"


def _norm(word: str | None) -> str:
    return _KEEP.sub("", (word or "").lower().replace("’", "'"))


def in_transcript_flags(words: list[dict], target_diff: list[dict] | None) -> list[bool | None]:
    """Scripted only: was each aligned word found in the transcript by the target diff (op `equal`)?

    The aligner and the diff tokenize differently (e.g. "$4.50"), so words are matched by normalized spelling; a word
    that cannot be matched gets None (unknown), never False."""
    if target_diff is None:
        return [None] * len(words)
    ops = [op for op in target_diff if op.get("target")]
    a = [_norm(w["word"]) for w in words]
    b = [_norm(op["target"]) for op in ops]
    flags: list[bool | None] = [None] * len(words)
    for block in SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            flags[block.a + k] = ops[block.b + k]["op"] == "equal"
    return flags


def _weak_sounds(phones: list[dict], guide: GuideIndex) -> list[dict]:
    out = []
    for ph in phones:
        gop, expected = ph.get("gop"), ph.get("expected_ipa")
        if gop is None or expected is None or gop >= WEAK_GOP:
            continue
        accepted = {expected, *ACCEPTED.get(expected, ())}
        heard = next((c.get("ipa") for c in ph.get("heard_candidates") or [] if c.get("ipa") not in accepted), None)
        out.append({"expected_ipa": expected, "heard_ipa": heard, "guide_ids": guide.for_units([expected])})
    return out


def build_words(raw: list[dict], *, banded: bool, flags: list[bool | None], guide: GuideIndex) -> list[dict]:
    """Result words (PROTOCOL §12.4). Drops gop / word_gop / posteriors; bands only when `banded`."""
    words, prev_end = [], None
    for k, w in enumerate(raw):
        start, end = int(w["start_ms"]), int(w["end_ms"])
        phones = w.get("phones") or []
        units = [p["expected_ipa"] for p in phones if p.get("expected_ipa")] or guide.units_for_word(w["word"])
        band = w.get("band") if banded else None
        weak = _weak_sounds(phones, guide) if banded and band in ("check", "practice") else []
        words.append({
            "i": int(w["i"]), "word": w["word"], "start_ms": start, "end_ms": end,
            "duration_ms": end - start, "gap_before_ms": 0 if prev_end is None else max(0, start - prev_end),
            "in_transcript": flags[k] if k < len(flags) else None,
            "band": band,
            "heard_ipa": [s["heard_ipa"] for s in weak] if banded else None,
            "weak_sounds": weak if banded else None,
            "guide_ids": guide.for_units(units),
        })
        prev_end = end
    return words


def _realtime_active(svc: Services) -> bool:
    return svc.sessions.active_realtime() is not None


async def analyze_learner(svc: Services, attempt: Attempt, pcm: bytes, duration_s: float) -> dict:
    """Learner side, run while the job is `analyzing`. Returns the `pronunciation` result object."""
    mode = mode_for(attempt.exercise_type)
    if svc.pron is None:
        return unavailable("NOT_CONFIGURED", mode)
    state = svc.health.pron_state()
    if not state or not state.get("ready"):
        return unavailable("MODEL_NOT_READY", mode)
    reference = (attempt.target_en if mode == "scripted" else attempt.text(1)) or ""
    if not reference.strip():
        return unavailable("TEXT_EMPTY", mode)
    # Never compete with a realtime session (PRD §14.2, PROTOCOL §12.1).
    if _realtime_active(svc):
        return unavailable("LOCAL_BUSY", mode)
    started = time.perf_counter()
    timeout = call_timeout(duration_s)
    try:
        await svc.pron_guide.ensure_loaded()
        assessed = None
        if state.get("bands_enabled"):
            try:
                assessed = await svc.pron.assess(pcm, reference, mode, timeout)
            except WorkerError as exc:
                if exc.code != "NOT_IMPLEMENTED":
                    raise
        if assessed is not None:
            raw, revisions = assessed["words"], dict(assessed.get("model_revisions") or {})
        else:
            aligned = await svc.pron.align(pcm, reference, timeout)
            raw, revisions = aligned["words"], {"aligner": aligned.get("model_revision")}
        if _realtime_active(svc):
            return unavailable("LOCAL_BUSY", mode)
        prosody = await svc.pron.prosody(pcm, raw, timeout)
    except WorkerError as exc:
        log.info("pron_unavailable attempt=%s reason=%s ms=%d", attempt.attempt_id, exc.code,
                 int((time.perf_counter() - started) * 1000))
        return unavailable(exc.code, mode)
    except (KeyError, TypeError, ValueError) as exc:
        log.error("pron_bad_response attempt=%s error=%s", attempt.attempt_id, type(exc).__name__)
        return unavailable("WORKER_FAILED", mode)
    banded = bool(assessed and assessed.get("bands_enabled"))
    status = "experimental_banded" if banded else "timing_only"
    flags = in_transcript_flags(raw, attempt.target_diff) if mode == "scripted" else [None] * len(raw)
    words = build_words(raw, banded=banded, flags=flags, guide=svc.pron_guide)
    log.info("pron_done attempt=%s status=%s words=%d ms=%d", attempt.attempt_id, status, len(words),
             int((time.perf_counter() - started) * 1000))
    return {
        "status": status, "reason": None, "mode": mode, "transcript_revision": 1, "words": words,
        "prosody": {"learner": {"hop_ms": prosody["hop_ms"], "f0_hz": prosody["f0_hz"],
                                "per_word": prosody["per_word"]}, "model": None},
        "calibration_version": assessed.get("calibration_version") if banded else None,
        "model_revisions": revisions,
        "evidence_types": ["word_timing", "prosody_contour", *(["phone_gop"] if banded else [])],
        "note_ko": NOTE_KO[status],
    }


async def analyze_model(svc: Services, attempt: Attempt) -> None:
    """Model side, run while the job is `synthesizing`: timings and pitch of the model audio of the same sentence,
    for "모범 음성" word playback and the contour chart. Leaves `prosody.model` null when anything is missing."""
    pron = attempt.pronunciation
    if not pron or pron["status"] == "unavailable" or pron["mode"] != "scripted" or svc.pron is None:
        return
    entry = attempt.model_audio.get("target")
    if entry is None or " ".join((entry["text"] or "").lower().split()) != " ".join((attempt.target_en or "").lower().split()):
        return
    if _realtime_active(svc):
        return
    started = time.perf_counter()
    try:
        pcm_bytes, rate = wav_pcm(entry["wav"])
        pcm16k = await asyncio.to_thread(to_mono_16k, np.frombuffer(pcm_bytes, dtype="<i2"), rate)
        pcm = pcm16k.tobytes()
        timeout = call_timeout(len(pcm16k) / 16000)
        aligned = await svc.pron.align(pcm, attempt.target_en, timeout)
        prosody = await svc.pron.prosody(pcm, aligned["words"], timeout)
    except WorkerError as exc:
        log.info("pron_model_unavailable attempt=%s reason=%s", attempt.attempt_id, exc.code)
        return
    except (KeyError, TypeError, ValueError) as exc:
        log.error("pron_model_failed attempt=%s error=%s", attempt.attempt_id, type(exc).__name__)
        return
    pron["prosody"]["model"] = {
        "audio_id": "target", "hop_ms": prosody["hop_ms"], "f0_hz": prosody["f0_hz"], "per_word": prosody["per_word"],
        "words": [{k: w[k] for k in ("i", "word", "start_ms", "end_ms")} for w in aligned["words"]],
    }
    log.info("pron_model_done attempt=%s words=%d ms=%d", attempt.attempt_id, len(aligned["words"]),
             int((time.perf_counter() - started) * 1000))


def stored(pron: dict | None) -> dict | None:
    """What may be kept with an opted-in attempt (PROTOCOL §12.4): word intervals and bands, no contours/candidates."""
    if not pron:
        return None
    return {"status": pron["status"], "mode": pron["mode"], "calibration_version": pron["calibration_version"],
            "words": [{k: w[k] for k in ("i", "word", "start_ms", "end_ms", "band")} for w in pron["words"]]}
