"""Thin facade over the `vr_feedback` library so the engine and job runner can be tested with fakes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Brain:
    segmenter_factory: Callable[[], Any]
    normalize_for_tts: Callable[[str], str]
    compute_metrics: Callable[..., dict]
    diff_target: Callable[[str, str], list[dict]]
    echo_overlap: Callable[[str, str], float]
    build_roleplay_messages: Callable[..., list[dict]]
    build_opening_warmup: Callable[..., list[dict]]
    generate_feedback: Callable[..., Any]
    session_summary: Callable[..., Any]
    evaluate_goals: Callable[..., Any]
    build_hint: Callable[..., Any]
    update_summary: Callable[..., Any]


def default_brain() -> Brain:
    from vr_feedback import echo, feedback, goals, hints, metrics, prompts, reading, segmenter, summary, tts_text

    return Brain(
        segmenter_factory=segmenter.SpeechSegmenter,
        normalize_for_tts=tts_text.normalize_for_tts,
        compute_metrics=metrics.compute_metrics,
        diff_target=reading.diff_target,
        echo_overlap=echo.echo_overlap,
        build_roleplay_messages=prompts.build_roleplay_messages,
        build_opening_warmup=prompts.build_opening_warmup,
        generate_feedback=feedback.generate_feedback,
        session_summary=feedback.session_summary,
        evaluate_goals=goals.evaluate_goals,
        build_hint=hints.build_hint,
        update_summary=summary.update_summary,
    )
