"""Speaking-rate and pause metrics from VAD voiced intervals (PRD §8.1, PROTOCOL §8)."""

from __future__ import annotations

from .textutil import words

METRICS_VERSION = "m1"


def compute_metrics(voiced: list[tuple[float, float]], transcript: str, pause_threshold_s: float = 0.5) -> dict:
    """wpm = words / (last voiced end - first voiced start) * 60.

    Pauses are gaps >= threshold between voiced intervals; leading/trailing silence is excluded.
    """
    spans = sorted((float(s), float(e)) for s, e in voiced if e > s)
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    word_count = len(words(transcript))
    span = merged[-1][1] - merged[0][0] if merged else 0.0
    gaps = [b[0] - a[1] for a, b in zip(merged, merged[1:])]
    pauses = [g for g in gaps if g >= pause_threshold_s]
    wpm = round(word_count / span * 60, 1) if span >= 1.0 and word_count else None

    return {
        "wpm": wpm,
        "speech_span_s": round(span, 3),
        "word_count": word_count,
        "pause_count": len(pauses),
        "total_pause_s": round(sum(pauses), 3),
        "metrics_version": METRICS_VERSION,
        "definition_ko": (
            "분당 단어 수 = 전사 단어 수 ÷ (첫 음성 시작부터 마지막 음성 종료까지의 시간) × 60. "
            f"{int(round(pause_threshold_s * 1000))} ms 이상 이어진 무음을 휴지로 세며, 앞뒤 무음은 제외합니다. "
            "참고 지표이며 발음 점수가 아닙니다."
        ),
    }
