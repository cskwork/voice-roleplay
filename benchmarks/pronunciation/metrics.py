"""Correlation and detection metrics for pronunciation benchmarks (numpy only)."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Pearson r, or None when fewer than 3 pairs or either side is constant."""
    a, b = np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    if len(a) != len(b):
        raise ValueError("length mismatch")
    if len(a) < 3:
        return None
    a, b = a - a.mean(), b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    if denom == 0:
        return None
    return float((a * b).sum() / denom)


def rankdata(x: Sequence[float]) -> np.ndarray:
    """1-based ranks; ties get the average rank (same as scipy.stats.rankdata(method="average"))."""
    a = np.asarray(x, dtype=np.float64)
    order = np.argsort(a, kind="mergesort")
    sorted_a = a[order]
    ranks = np.empty(len(a), dtype=np.float64)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y):
        raise ValueError("length mismatch")
    if len(x) < 3:
        return None
    return pearson(rankdata(x), rankdata(y))


def correlation(pred: Sequence[float], human: Sequence[float]) -> dict:
    return {"n": len(pred), "pearson": _r(pearson(pred, human)), "spearman": _r(spearman(pred, human))}


def distribution(values: Sequence[float]) -> dict:
    """min / quartiles / max of per-group values (e.g. one correlation per speaker)."""
    v = np.asarray([x for x in values if x is not None], dtype=np.float64)
    if len(v) == 0:
        return {"n": 0}
    q = np.percentile(v, [0, 25, 50, 75, 100])
    return {"n": len(v), "min": _r(q[0]), "p25": _r(q[1]), "median": _r(q[2]), "p75": _r(q[3]), "max": _r(q[4])}


def detection(flagged: Sequence[bool], positive: Sequence[bool]) -> dict:
    """How well binary flags find the positive (e.g. mispronounced) items.

    miss_rate = positives that were not flagged / positives. precision = flagged positives / flagged.
    """
    f = np.asarray(flagged, dtype=bool)
    p = np.asarray(positive, dtype=bool)
    tp = int((f & p).sum())
    n_pos, n_flag = int(p.sum()), int(f.sum())
    return {
        "n": len(f),
        "positives": n_pos,
        "flagged": n_flag,
        "flagged_positives": tp,
        "missed_positives": n_pos - tp,
        "miss_rate": _r((n_pos - tp) / n_pos) if n_pos else None,
        "recall": _r(tp / n_pos) if n_pos else None,
        "precision": _r(tp / n_flag) if n_flag else None,
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)
