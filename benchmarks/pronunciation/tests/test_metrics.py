import numpy as np
import pytest

import metrics


def test_pearson_and_spearman_match_scipy():
    stats = pytest.importorskip("scipy.stats")
    rng = np.random.default_rng(0)
    x = rng.integers(0, 11, 200).astype(float)  # many ties, like human word scores
    y = x + rng.normal(0, 3, 200)
    assert metrics.pearson(x, y) == pytest.approx(stats.pearsonr(x, y)[0])
    assert metrics.spearman(x, y) == pytest.approx(stats.spearmanr(x, y)[0])
    assert list(metrics.rankdata([3, 1, 3, 2])) == list(stats.rankdata([3, 1, 3, 2]))


def test_undefined_correlations_are_none():
    assert metrics.pearson([1, 2], [1, 2]) is None
    assert metrics.pearson([1, 1, 1], [1, 2, 3]) is None
    assert metrics.spearman([5, 5, 5], [1, 2, 3]) is None
    with pytest.raises(ValueError):
        metrics.pearson([1, 2, 3], [1, 2])


def test_detection_counts():
    d = metrics.detection([True, False, False, True], [True, True, False, False])
    assert d["positives"] == 2 and d["flagged_positives"] == 1 and d["missed_positives"] == 1
    assert d["miss_rate"] == 0.5 and d["precision"] == 0.5
    assert metrics.detection([False], [False])["miss_rate"] is None


def test_distribution_skips_none():
    d = metrics.distribution([0.1, None, 0.3, 0.2])
    assert d["n"] == 3 and d["median"] == 0.2
    assert metrics.distribution([None]) == {"n": 0}
