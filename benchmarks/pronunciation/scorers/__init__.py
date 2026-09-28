"""Scorer protocol for run_eval.py (PA-0). PA-2/PA-3 scorers plug in here.

A scorer is any object with:

    name: str
    def info(self) -> dict                       # model ids + pinned revisions, method, settings (no data)
    def score(self, wav, reference_text, ref_words) -> dict

Arguments:
    wav             numpy float32, 16 kHz mono, [-1, 1)
    reference_text  the sentence the learner read, as displayed (may contain punctuation)
    ref_words       sequence of objects with .text and .phones (canonical ARPAbet with stress digits, from the
                    corpus lexicon). This is the index space of the output; a scorer may ignore .phones.

Return value (every field optional except "words"; use None for "no output", never a made-up value):
    {
      "sentence": {"accuracy": float, "fluency": float, "prosodic": float, "total": float, ...},
      "words": [                                   # exactly len(ref_words) entries, same order
        {"accuracy": float | None,                 # higher = better, any scale (correlations are scale-free)
         "flagged": bool | None,                   # binary "this word needs attention" decision
         "phones": [float | None, ...] | None},    # higher = better, aligned to ref_words[i].phones;
      ],                                           # a list of a different length counts as not covered
      "extra": {...}                               # anything else worth caching (kept out of reports)
    }

Register a built-in scorer in SYSTEMS below, or pass `--system package.module:ClassName` to run_eval.py for a
scorer living elsewhere (e.g. in workers/pronunciation, run with that venv's python and PYTHONPATH set).
The class is constructed with no arguments; read configuration from environment variables.
"""

from __future__ import annotations

import importlib
from typing import Any, Protocol


class Scorer(Protocol):
    name: str

    def info(self) -> dict: ...

    def score(self, wav: Any, reference_text: str, ref_words: Any) -> dict: ...


SYSTEMS = {
    "asr_diff": "scorers.asr_diff:AsrDiffScorer",
    "gop_ctc": "scorers.gop_ctc:GopCtcScorer",
    "gop_ctc_cal": "scorers.gop_ctc:GopCtcCalibratedScorer",
    "gop_ctc_cal_cmudict": "scorers.gop_ctc:GopCtcCmudictScorer",
}


def load_scorer(system: str) -> Scorer:
    target = SYSTEMS.get(system, system)
    if ":" not in target:
        raise SystemExit(f"unknown system {system!r}; known: {', '.join(SYSTEMS)} or package.module:ClassName")
    module, cls = target.split(":", 1)
    return getattr(importlib.import_module(module), cls)()
