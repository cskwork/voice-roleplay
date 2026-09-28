"""Phone posteriors (facebook/wav2vec2-lv-60-espeak-cv-ft) and CTC-based goodness of pronunciation (GOP).

GOP, segmentation-free CTC variant (Cao, Fan, Svendsen, Salvi, arXiv:2507.16838; implemented here from the paper's
description, no code copied). For expected unit i of a word, with S = the expected units of the previous word, the word
itself and the next word, and X = the frames of that stretch of audio (word timings from the forced aligner +- 200 ms):

    P_q   = P_CTC(S with unit i replaced by q | X)    for every q of the inventory, and q = deletion (unit removed)
    gop_i = log( sum over q accepted for unit i of P_q  /  sum over all q of P_q )      (natural log, <= 0)

i.e. the log posterior that the expected unit (or an accepted variant, lexicon.py) was said at that position, against
every other English unit or nothing, without cutting the audio into phone segments. Heard candidates are the q with
the largest P_q / sum P_q. These are raw model posteriors, NOT calibrated, and never shown to learners (PROTOCOL §12.2).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .aligner import AlignedWord, resolve_device
from .config import SAMPLE_RATE
from .lexicon import Unit, inventory

INVENTORY = inventory()
DELETION = ""  # heard candidate "nothing": the expected unit was not heard
LABELS = (*INVENTORY, DELETION)
FRAME_MS = 20  # wav2vec2 frame hop (320 samples)
WINDOW_PAD_MS = 200  # around the aligned words; the aligner works in 80 ms steps
GOP_MIN = -50.0  # stands in for log(0) when the expected sequence cannot fit the window at all
TOP_CANDIDATES = 3
SINGLE_PASS_MAX_S = 30  # longer audio runs through the model in 20 s chunks with 1 s context on each side
CHUNK_S, CHUNK_CONTEXT_S = 20, 1


@dataclass(frozen=True)
class PhoneResult:
    expected_ipa: str
    heard: tuple[tuple[str, float], ...]  # (ipa, posterior) best first; ipa "" = deletion
    gop: float | None  # None = no usable frames

    def to_json(self) -> dict:
        return {
            "expected_ipa": self.expected_ipa,
            "heard_candidates": [{"ipa": ipa, "p": p} for ipa, p in self.heard],
            "gop": self.gop,
        }


class PhoneModel:
    """Wav2Vec2ForCTC phone recogniser; `log_probs` returns columns [CTC blank, *INVENTORY]."""

    def __init__(self, model_dir: Path, device: str):
        import torch
        from transformers import Wav2Vec2ForCTC
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        self.device = resolve_device(device)
        self._torch = torch
        # fp32 on both devices (the model is 0.3 B parameters and fast enough; bf16 was not evaluated).
        self._model = Wav2Vec2ForCTC.from_pretrained(str(model_dir)).eval().to(self.device)
        vocab = json.loads((model_dir / "vocab.json").read_text(encoding="utf-8"))
        missing = [u for u in INVENTORY if u not in vocab]
        if missing:
            raise ValueError(f"phone model vocabulary lacks {len(missing)} inventory units")
        self._cols = torch.tensor([vocab["<pad>"], *(vocab[u] for u in INVENTORY)], device=self.device)

    def _run(self, x: np.ndarray) -> np.ndarray:
        torch = self._torch
        with torch.inference_mode():
            logits = self._model(torch.from_numpy(x)[None].to(self.device)).logits[0]
            return torch.log_softmax(logits.float(), dim=-1)[:, self._cols].cpu().numpy()

    def log_probs(self, audio: np.ndarray) -> np.ndarray:
        """(frames, 1 + len(INVENTORY)) log posteriors (softmax over the full vocabulary); frame k starts at k*20 ms."""
        x = ((audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)).astype(np.float32)
        if len(x) <= SINGLE_PASS_MAX_S * SAMPLE_RATE:
            return self._run(x)
        core, ctx, hop = CHUNK_S * SAMPLE_RATE, CHUNK_CONTEXT_S * SAMPLE_RATE, SAMPLE_RATE * FRAME_MS // 1000
        parts = []
        for s in range(0, len(x), core):
            a = max(0, s - ctx)
            lp = self._run(x[a : min(len(x), s + core + ctx)])
            off = (s - a) // hop
            parts.append(lp[off : off + core // hop])
        return np.concatenate(parts)


def _ctc_loglik(lp, seqs: list[list[int]]) -> np.ndarray:
    """log P_CTC(seq | frames) for each seq; lp is a (T, C) torch tensor of log posteriors, blank = column 0."""
    import torch

    T, C = lp.shape
    lens = [len(s) for s in seqs]
    targets = torch.zeros(len(seqs), max(1, max(lens)), dtype=torch.long)
    for b, s in enumerate(seqs):
        targets[b, : len(s)] = torch.tensor(s, dtype=torch.long)
    nll = torch.nn.functional.ctc_loss(
        lp[:, None, :].expand(T, len(seqs), C),
        targets,
        torch.full((len(seqs),), T, dtype=torch.long),
        torch.tensor(lens, dtype=torch.long),
        blank=0,
        reduction="none",
        zero_infinity=False,
    )
    return -nll.double().numpy()


def _logsumexp(v: np.ndarray) -> float:
    m = float(np.max(v))
    return m if not np.isfinite(m) else m + float(np.log(np.sum(np.exp(v - m))))


def _phone_result(unit: Unit, row: np.ndarray) -> PhoneResult:
    """row[k] = log P of the window sequence with this unit replaced by LABELS[k]."""
    if not np.isfinite(row).any():
        return PhoneResult(unit.ipa, (), None)
    total = _logsumexp(row)
    accepted = [LABELS.index(t) for t in (unit.ipa, *unit.accepted)]
    gop = max(_logsumexp(row[accepted]) - total, GOP_MIN)
    post = np.exp(row - total)
    top = np.argsort(-post, kind="stable")[:TOP_CANDIDATES]
    heard = tuple((LABELS[k], round(float(post[k]), 3)) for k in top if post[k] >= 0.001)
    return PhoneResult(unit.ipa, heard, round(gop, 3))


def score_words(
    lp: np.ndarray, words: list[AlignedWord], variants: list[list[list[Unit]]]
) -> list[tuple[list[Unit], list[PhoneResult]]]:
    """Per word: the pronunciation variant used and one PhoneResult per unit ([] for words without phones).

    `lp` from PhoneModel.log_probs, `variants[j]` = candidate unit sequences of word j (lexicon order). When a word has
    several pronunciations, the one with the highest CTC likelihood over its window is used (all are dictionary forms).
    """
    import torch

    lpt = torch.from_numpy(np.ascontiguousarray(lp, dtype=np.float32))
    col = {u: k + 1 for k, u in enumerate(INVENTORY)}

    def ids(units: list[Unit]) -> list[int]:
        return [col[u.ipa] for u in units]

    chosen = [v[0] if v else [] for v in variants]
    out = []
    for j, w in enumerate(words):
        if not chosen[j]:
            out.append(([], []))
            continue
        left = chosen[j - 1] if j > 0 else []
        right = chosen[j + 1] if j + 1 < len(words) else []
        start = words[j - 1].start_ms if left else w.start_ms
        end = words[j + 1].end_ms if right else w.end_ms
        lo = max(0, (start - WINDOW_PAD_MS) // FRAME_MS)
        hi = min(len(lpt), -(-(end + WINDOW_PAD_MS) // FRAME_MS))
        win = lpt[lo:hi]
        if len(win) == 0:
            out.append((chosen[j], [PhoneResult(u.ipa, (), None) for u in chosen[j]]))
            continue
        if len(variants[j]) > 1:
            ll = _ctc_loglik(win, [ids(left) + ids(v) + ids(right) for v in variants[j]])
            chosen[j] = variants[j][int(np.argmax(ll))]
        units = chosen[j]
        base = ids(left) + ids(units) + ids(right)
        seqs = []
        for k in range(len(units)):
            pos = len(left) + k
            seqs += [base[:pos] + [c] + base[pos + 1 :] for c in range(1, len(INVENTORY) + 1)]
            seqs.append(base[:pos] + base[pos + 1 :])  # deletion
        ll = _ctc_loglik(win, seqs).reshape(len(units), len(LABELS))
        out.append((units, [_phone_result(u, row) for u, row in zip(units, ll)]))
    return out
