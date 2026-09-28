"""Integration tests against the REAL models (Qwen3-ForcedAligner-0.6B, wav2vec2-lv-60-espeak-cv-ft + CMUdict) and real
pyworld, served by `python -m pron_worker` in a subprocess.

Needs the model files (README) and generated fixtures (tests/fixtures/make_fixtures.sh). The speech fixtures
are macOS `say` voices and CosyVoice output, not learner speech: these tests check that the plumbing, the word
boundaries and the direction of GOP are sane, not how accurate anything is on Korean learners (unmeasured).
The "mispronunciation" cases are correct speech paired with a deliberately wrong reference word.
Measured numbers are printed in the pytest summary ("Pronunciation worker measurements").
"""
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import psutil
import pytest
from pron_worker.config import ALIGNER_REVISION, CALIBRATION_VERSION, PHONES_ID, PHONES_REVISION

from .helpers import b64, fixture_pcm, fixture_text

pytestmark = pytest.mark.model

WORKER_DIR = Path(__file__).resolve().parents[1]
METRICS: dict = {}
CAL_FILE = WORKER_DIR / "calibration" / f"{CALIBRATION_VERSION}.json"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rss_mb(proc: subprocess.Popen) -> float:
    return round(psutil.Process(proc.pid).memory_info().rss / 2**20)


@contextmanager
def _worker(tmp_path_factory, label: str, **env_extra):
    """`python -m pron_worker` in a subprocess with the real models; yields once /health says ready."""
    token = secrets.token_hex(16)
    port = _free_port()
    log_path = tmp_path_factory.mktemp("pron") / "worker.log"
    env = {k: v for k, v in os.environ.items() if k != "VR_PRON_EXPERIMENTAL"}
    env.update(VR_WORKER_TOKEN=token, VR_PRON_PORT=str(port), **env_extra)
    started = time.monotonic()
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "pron_worker"], cwd=WORKER_DIR, env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Worker-Token": token}
    try:
        deadline = time.monotonic() + 180
        while True:
            assert proc.poll() is None, log_path.read_text()
            try:
                if httpx.get(f"{base}/health", headers=headers).json()["ready"]:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "worker not ready in 180 s"
            time.sleep(0.2)
        METRICS[f"{label}process_start_to_ready_s"] = round(time.monotonic() - started, 2)
        METRICS[f"{label}rss_mb_after_ready"] = _rss_mb(proc)
        yield {"base": base, "headers": headers, "proc": proc, "log": log_path}
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    METRICS[f"{label}model_ready_log"] = [l.split(" pron ", 1)[1] for l in log_path.read_text().splitlines() if "model.ready" in l]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with _worker(tmp_path_factory, "") as s:
        yield s


def post(server, path: str, body: dict) -> httpx.Response:
    return httpx.post(f"{server['base']}{path}", json=body, headers=server["headers"], timeout=180)


def align(server, name: str) -> tuple[list[dict], int]:
    pcm = fixture_pcm(name)
    r = post(server, "/align", {"audio_b64": b64(pcm), "text": fixture_text(name)})
    assert r.status_code == 200, r.text
    return r.json()["words"], len(pcm) // 32


def expected_words(text: str) -> list[str]:
    # Same token rule as qwen-asr's aligner: whitespace split, keep letters/digits/apostrophes.
    return [t for t in (re.sub(r"[^\w']|_", "", w) for w in text.split()) if t]


def check_boundaries(words: list[dict], total_ms: int) -> None:
    for w in words:
        assert 0 <= w["start_ms"] <= w["end_ms"] <= total_ms, w
    for a, b in zip(words, words[1:]):
        assert a["start_ms"] <= b["start_ms"] and a["end_ms"] <= b["start_ms"], (a, b)


def test_health(server):
    h = httpx.get(f"{server['base']}/health", headers=server["headers"]).json()
    assert h["ready"] and h["device"] in ("mps", "cpu")
    assert h["models"]["aligner"] == {"model_id": "Qwen/Qwen3-ForcedAligner-0.6B", "revision": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"}
    assert h["models"]["phones"] == {"model_id": PHONES_ID, "revision": PHONES_REVISION}
    assert h["bands_enabled"] is False  # VR_PRON_EXPERIMENTAL is not set
    assert h["calibration_version"] == (CALIBRATION_VERSION if CAL_FILE.is_file() else None)
    METRICS["device"] = h["device"]


@pytest.mark.parametrize("name", ["short_answer", "price", "mid_pause", "e2e_hesitation", "e2e_unique", "passage_30s"])
def test_align_boundaries_sane(server, name):
    words, total_ms = align(server, name)
    assert [w["word"] for w in words] == expected_words(fixture_text(name))
    assert [w["i"] for w in words] == list(range(len(words)))
    check_boundaries(words, total_ms)
    # `say` output has < 0.5 s of lead-in and trailing silence, and speech throughout in between.
    assert words[0]["start_ms"] < 500 and words[-1]["end_ms"] > total_ms - 800
    spoken = sum(w["end_ms"] - w["start_ms"] for w in words)
    assert spoken > 0.5 * total_ms, spoken


@pytest.mark.parametrize("name,before,after,pause_ms", [("mid_pause", "grandmother", "We", 2000), ("e2e_hesitation", "latte", "with", 600)])
def test_align_finds_known_pause(server, name, before, after, pause_ms):
    """The fixture has an inserted silence of `pause_ms` between two words; the gap between them must cover most of it."""
    words, _ = align(server, name)
    k = [w["word"] for w in words].index(before)
    assert words[k + 1]["word"] == after
    gap = words[k + 1]["start_ms"] - words[k]["end_ms"]
    METRICS.setdefault("pause_gap_ms", {})[name] = {"inserted": pause_ms, "aligned_gap": gap}
    assert gap >= 0.6 * pause_ms, gap


def test_align_latency(server):
    for name in ("price", "passage_30s"):
        pcm, text = fixture_pcm(name), fixture_text(name)
        runs = []
        for _ in range(3):
            started = time.monotonic()
            r = post(server, "/align", {"audio_b64": b64(pcm), "text": text})
            assert r.status_code == 200
            runs.append({"http_ms": round((time.monotonic() - started) * 1000), "worker_ms": r.json()["elapsed_ms"]})
        METRICS[f"align_{name}_{len(pcm) / 32000:.1f}s"] = runs
    METRICS["rss_mb_after_align"] = _rss_mb(server["proc"])


def test_prosody_on_tts_sample(server):
    # CosyVoice (MLX) output written by workers/tts/tests/test_real_model.py (tts_<backend>_<name>_<voice_id>.wav),
    # 24 kHz resampled to 16 kHz.
    pcm = fixture_pcm("tts_mlx_price_libritts_r_4992_f")
    started = time.monotonic()
    r = post(server, "/prosody", {"audio_b64": b64(pcm)})
    assert r.status_code == 200, r.text
    body = r.json()
    f0 = [v for v in body["f0_hz"] if v > 0]
    METRICS[f"prosody_tts_{len(pcm) / 32000:.1f}s"] = {"http_ms": round((time.monotonic() - started) * 1000), "worker_ms": body["elapsed_ms"],
                                   "voiced_fraction": round(len(f0) / len(body["f0_hz"]), 2)}
    assert body["hop_ms"] == 10 and body["method"] == "pyworld-harvest"
    assert abs(len(body["f0_hz"]) - len(pcm) // 320) <= 1
    assert len(f0) > 0.3 * len(body["f0_hz"])
    assert 70 < sorted(f0)[len(f0) // 2] < 400  # median in the range of adult speech


def test_prosody_per_word_from_align(server):
    words, _ = align(server, "passage_30s")
    pcm = fixture_pcm("passage_30s")
    started = time.monotonic()
    r = post(server, "/prosody", {"audio_b64": b64(pcm), "words": words})
    assert r.status_code == 200, r.text
    per_word = r.json()["per_word"]
    METRICS["prosody_passage_28.5s"] = {"http_ms": round((time.monotonic() - started) * 1000), "worker_ms": r.json()["elapsed_ms"]}
    assert [p["i"] for p in per_word] == [w["i"] for w in words]
    assert [p["duration_ms"] for p in per_word] == [w["end_ms"] - w["start_ms"] for w in words]
    voiced = [p for p in per_word if p["mean_f0"] > 0]
    assert len(voiced) > 0.8 * len(per_word)
    assert all(60 <= p["mean_f0"] <= 500 and p["f0_range_st"] >= 0 for p in voiced)


def assess(server, name: str, text: str | None = None, mode: str = "scripted") -> dict:
    pcm = fixture_pcm(name)
    started = time.monotonic()
    r = post(server, "/assess", {"audio_b64": b64(pcm), "reference_text": text or fixture_text(name), "mode": mode})
    assert r.status_code == 200, r.text
    METRICS.setdefault("assess_http_ms", {}).setdefault(f"{name}_{len(pcm) / 32000:.1f}s", []).append(
        round((time.monotonic() - started) * 1000))
    return r.json()


def word(body: dict, text: str) -> dict:
    return next(w for w in body["words"] if w["word"] == text)


def test_assess_shape_and_no_bands_without_flag(server):
    name = "e2e_read_01"  # "I'd like a large cappuccino with oat milk, please." (macOS `say`, not learner speech)
    body = assess(server, name)
    words = body["words"]
    assert [w["word"] for w in words] == expected_words(fixture_text(name))
    check_boundaries(words, len(fixture_pcm(name)) // 32)
    assert body["bands_enabled"] is False and all(w["band"] is None for w in words)
    assert body["calibration_version"] == (CALIBRATION_VERSION if CAL_FILE.is_file() else None)
    assert body["model_revisions"] == {"aligner": ALIGNER_REVISION, "phones": PHONES_REVISION}
    assert [p["expected_ipa"] for p in word(body, "milk")["phones"]] == ["m", "ɪ", "l", "k"]
    assert [p["expected_ipa"] for p in word(body, "large")["phones"]] == ["l", "ɑːɹ", "dʒ"]
    top1 = []
    for w in words:
        assert w["phones"], w["word"]  # every word of this sentence is in CMUdict
        gops = [p["gop"] for p in w["phones"]]
        assert all(g is not None and -50 <= g <= 0 for g in gops)
        assert w["word_gop"] == pytest.approx(sum(gops) / len(gops), abs=0.002)
        for p in w["phones"]:
            assert 1 <= len(p["heard_candidates"]) <= 3 and sum(c["p"] for c in p["heard_candidates"]) <= 1.002
            top1.append(p["heard_candidates"][0]["ipa"] == p["expected_ipa"])
    METRICS["assess_e2e_read_01_top1_is_expected"] = f"{sum(top1)}/{len(top1)}"
    assert sum(top1) >= 0.7 * len(top1)  # clear synthetic speech: the expected unit is mostly the top candidate


@pytest.mark.parametrize(
    "name,right,wrong",
    [("e2e_read_01", "milk", "silk"), ("e2e_read_01", "like", "bike"), ("e2e_read_01", "oat", "eat"),
     ("e2e_cafe_08", "pay", "bay")],
)
def test_wrong_reference_word_gets_lower_gop(server, name, right, wrong):
    """Mispronunciation sanity check: the audio says `right`; the reference claims `wrong` (first unit differs).
    The first unit's GOP must drop, and the unit actually spoken must be among the heard candidates."""
    text = fixture_text(name)
    good = word(assess(server, name, text), right)
    bad = word(assess(server, name, re.sub(rf"\b{right}\b", wrong, text, count=1)), wrong)
    g, b = good["phones"][0], bad["phones"][0]
    assert g["expected_ipa"] != b["expected_ipa"]
    METRICS.setdefault("wrong_reference_first_unit_gop", {})[f"{right}->{wrong}"] = [g["gop"], b["gop"]]
    assert b["gop"] < g["gop"] - 2.0  # natural log: posterior at least ~7x lower
    assert bad["word_gop"] < good["word_gop"]
    assert g["expected_ipa"] in [c["ipa"] for c in b["heard_candidates"]]


def test_assess_unscripted_and_unknown_words(server):
    body = assess(server, "e2e_unique", mode="unscripted")  # "My cousin Bartholomew ordered a periwinkle octopus sandwich."
    assert [w["word"] for w in body["words"]] == expected_words(fixture_text("e2e_unique"))
    assert all(w["band"] is None for w in body["words"])
    oov = assess(server, "e2e_read_02", text="Could you make it a little less zzyzxqq?")
    last = oov["words"][-1]
    assert (last["phones"], last["word_gop"], last["band"]) == ([], None, None)
    assert all(w["phones"] for w in oov["words"][:-1])


def test_assess_long_audio_is_chunked(server):
    body = assess(server, "passage_30s")  # 28.5 s: single pass; chunking starts above 30 s
    assert len(body["words"]) == len(expected_words(fixture_text("passage_30s")))
    pcm = fixture_pcm("passage_30s") * 2  # ~57 s: two 20 s chunks + remainder
    text = (fixture_text("passage_30s") + " ") * 2
    started = time.monotonic()
    r = post(server, "/assess", {"audio_b64": b64(pcm), "reference_text": text, "mode": "scripted"})
    METRICS["assess_http_ms"]["passage_x2_57s"] = [round((time.monotonic() - started) * 1000)]
    assert r.status_code == 200, r.text
    words = r.json()["words"]
    half = len(words) // 2
    first = [p["gop"] for w in words[:half] for p in w["phones"]]
    second = [p["gop"] for w in words[half:] for p in w["phones"]]
    # Same speech twice: chunked and unchunked parts must look alike (mean GOP within 0.5 nats).
    assert abs(sum(first) / len(first) - sum(second) / len(second)) < 0.5, (sum(first) / len(first), sum(second) / len(second))


def test_no_text_in_logs(server):
    words, _ = align(server, "e2e_unique")
    assert words
    log = server["log"].read_text()
    assert "align.done" in log
    for w in ("Bartholomew", "periwinkle", "octopus"):
        assert w not in log
    assess(server, "e2e_unique")
    log = server["log"].read_text()
    assert "assess.done" in log
    for w in ("Bartholomew", "periwinkle", "octopus", "ɹ", "gop"):
        assert w not in log
