"""Echo heuristic: does a barge-in transcript just repeat the AI audio that was playing?"""

from __future__ import annotations

from .textutil import norm_word, words


def echo_overlap(user_text: str, ai_text: str) -> float:
    """Fraction of user words that appear, in order, in the AI text (longest common subsequence)."""
    u = [norm_word(w) for w in words(user_text)]
    a = [norm_word(w) for w in words(ai_text)]
    if not u or not a:
        return 0.0
    prev = [0] * (len(a) + 1)
    for uw in u:
        cur = [0] * (len(a) + 1)
        for j, aw in enumerate(a, 1):
            cur[j] = prev[j - 1] + 1 if uw == aw else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1] / len(u)
