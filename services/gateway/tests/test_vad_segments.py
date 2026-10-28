"""voiced_segments: the hysteresis that turns Silero window probabilities into voiced spans (no model needed).

One window is 512 samples at 16 kHz = 32 ms (0.032 s). Defaults: on at >= 0.5, off below 0.35,
a span ends after 3 quiet windows (100 ms // 32 ms), spans shorter than 250 ms are dropped.
"""

import pytest

from vr_gateway.vad import WINDOW_MS, voiced_segments

W = 0.032


def test_window_is_32_ms():
    assert WINDOW_MS == 32


def test_no_speech_gives_no_spans():
    assert voiced_segments([]) == []
    assert voiced_segments([0.1] * 50) == []


def test_span_ends_at_first_quiet_window():
    probs = [0.1] * 5 + [0.9] * 10 + [0.1] * 5
    assert voiced_segments(probs) == [pytest.approx((5 * W, 15 * W))]


def test_hysteresis_keeps_speech_between_off_and_on_thresholds():
    # 0.4 would not start speech (< 0.5) but does not end it either (>= 0.35).
    probs = [0.9] * 10 + [0.4] * 5 + [0.1] * 5
    assert voiced_segments(probs) == [pytest.approx((0.0, 15 * W))]


def test_gap_shorter_than_min_silence_is_bridged():
    probs = [0.9] * 5 + [0.1] * 2 + [0.9] * 5 + [0.1] * 3
    assert voiced_segments(probs) == [pytest.approx((0.0, 12 * W))]


def test_gap_of_min_silence_splits_spans():
    probs = [0.9] * 10 + [0.1] * 3 + [0.9] * 10
    assert voiced_segments(probs) == [pytest.approx((0.0, 10 * W)), pytest.approx((13 * W, 23 * W))]


def test_spans_shorter_than_min_speech_are_dropped():
    assert voiced_segments([0.9] * 7 + [0.1] * 3) == []  # 7 x 32 ms = 224 ms < 250 ms
    assert voiced_segments([0.9] * 8 + [0.1] * 3) == [pytest.approx((0.0, 8 * W))]  # 256 ms


def test_span_still_open_at_the_end_is_kept():
    probs = [0.1] * 2 + [0.9] * 8
    assert voiced_segments(probs) == [pytest.approx((2 * W, 10 * W))]
