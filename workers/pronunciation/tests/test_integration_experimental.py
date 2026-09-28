"""Integration test of the experimental flag: a REAL worker with VR_PRON_EXPERIMENTAL=1 and the checked-in calibration
file returns bands. Separate module so that only one worker process (about 4 GB with both models) runs at a time.

The fixtures are macOS `say` speech with a deliberately wrong reference word, not learner speech: this checks that bands
are produced and move in the right direction, not that they are accurate (the calibration is fitted on Mandarin-L1
speech only and was never validated on Korean learners).
"""
import re

import pytest
from pron_worker.config import CALIBRATION_VERSION

from .helpers import fixture_text
from .test_integration import CAL_FILE, METRICS, _worker, assess, word

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not CAL_FILE.is_file():
        pytest.skip("no calibration file (run benchmarks/pronunciation/fit_calibration.py)")
    with _worker(tmp_path_factory, "experimental_", VR_PRON_EXPERIMENTAL="1") as s:
        yield s


def test_bands_only_with_flag_and_calibration(server):
    import httpx

    h = httpx.get(f"{server['base']}/health", headers=server["headers"]).json()
    assert h["bands_enabled"] is True and h["calibration_version"] == CALIBRATION_VERSION

    name = "e2e_read_01"
    body = assess(server, name)
    assert body["bands_enabled"] is True and body["calibration_version"] == CALIBRATION_VERSION
    bands = [w["band"] for w in body["words"]]
    assert all(b in ("good", "check", "practice") for b in bands)
    METRICS["experimental_bands_e2e_read_01"] = {b: bands.count(b) for b in set(bands)}

    wrong = assess(server, name, re.sub(r"\bmilk\b", "silk", fixture_text(name)))
    assert word(wrong, "silk")["band"] != "good"
    assert word(body, "milk")["band"] == "good"
