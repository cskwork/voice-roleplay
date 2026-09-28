"""Unit tests with a FAKE aligner and a FAKE GOP scorer (no model): request handling, limits, auth, prosody maths.

The fakes return canned timings/scores; they do not pretend to be models. pyworld (real) runs on synthetic tones.
"""
import base64
import logging
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pron_worker.aligner import AlignedWord, align_words, has_alignable_text
from pron_worker.app import create_app
from pron_worker.config import MAX_AUDIO_BYTES, Settings
from pron_worker.prosody import f0_contour, word_stats

from .helpers import b64, buzz, tone

SECRET_PHRASE = "periwinkle octopus sandwich"


class FakeAligner:
    """FAKE: splits the text on spaces and gives each word an equal share of the audio; `overshoot_s` pushes the
    last end past the audio to exercise clamping."""

    device = "cpu"

    def __init__(self, overshoot_s: float = 0.0, delay: float = 0.0):
        self.overshoot_s = overshoot_s
        self.delay = delay
        self.calls = 0

    def align(self, audio, text):
        self.calls += 1
        time.sleep(self.delay)
        words = [w for w in text.split() if any(c.isalnum() for c in w)]
        step = len(audio) / 16000 / len(words)
        out = [(w.strip(".,!?"), k * step, (k + 1) * step) for k, w in enumerate(words)]
        if out and self.overshoot_s:
            w, s, e = out[-1]
            out[-1] = (w, s, e + self.overshoot_s)
        return out


class FakeScorer:
    """FAKE GOP scorer: fixed numbers, only to test the /assess plumbing."""

    phones_model = {"model_id": "fake/phones", "revision": "0" * 40}

    def __init__(self, calibration_version=None):
        self.calibration_version = calibration_version

    def assess(self, audio, reference_text, words, mode):
        return [{**w.to_json(), "phones": [], "word_gop": -1.0, "band": "check"} for w in words]


def settings(**kw) -> Settings:
    base = dict(token="secret", port=0, model_dir=None, device="cpu", experimental=False, exit_on_load_failure=False)
    return Settings(**{**base, **kw})


H = {"X-Worker-Token": "secret"}


def wait_ready(client, timeout=10):
    deadline = time.monotonic() + timeout
    while not client.get("/health", headers=H).json()["ready"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.fixture
def client():
    aligner = FakeAligner(overshoot_s=0.5)
    with TestClient(create_app(settings(), aligner_factory=lambda: aligner)) as c:
        wait_ready(c)
        c.aligner = aligner
        yield c


def test_auth_required_everywhere(client):
    for method, path in [("get", "/health"), ("post", "/align"), ("post", "/prosody"), ("post", "/assess")]:
        r = getattr(client, method)(path, headers={"X-Worker-Token": "wrong"})
        assert r.status_code == 401 and r.json() == {"error": {"code": "AUTH_REQUIRED"}}


def test_health(client):
    h = client.get("/health", headers=H).json()
    assert h == {
        "ready": True, "device": "cpu",
        "models": {"aligner": {"model_id": "Qwen/Qwen3-ForcedAligner-0.6B",
                               "revision": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"}, "phones": None},
        "prosody_method": "pyworld-harvest", "calibration_version": None, "bands_enabled": False,
    }


def test_align_returns_integer_ms_clamped_to_audio(client):
    r = client.post("/align", headers=H, json={"audio_b64": b64(tone(2.0)), "text": "Hello there, my friend!"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_revision"] == "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"
    assert [w["i"] for w in body["words"]] == [0, 1, 2, 3]
    assert [w["word"] for w in body["words"]] == ["Hello", "there", "my", "friend"]
    assert body["words"][0] == {"i": 0, "word": "Hello", "start_ms": 0, "end_ms": 500}
    assert body["words"][-1]["end_ms"] == 2000  # 2.5 s from the fake, clamped to the 2.0 s audio


def test_align_request_errors(client):
    ok_audio = b64(tone(1.0))
    cases = [
        ({"audio_b64": "not base64!", "text": "hi"}, 400, "AUDIO_INVALID"),
        ({"audio_b64": base64.b64encode(b"\x00\x01\x02").decode(), "text": "hi"}, 400, "AUDIO_INVALID"),
        ({"audio_b64": "", "text": "hi"}, 400, "AUDIO_INVALID"),
        ({"text": "hi"}, 400, "AUDIO_INVALID"),
        ({"audio_b64": ok_audio}, 400, "BAD_REQUEST"),
        ({"audio_b64": ok_audio, "text": 5}, 400, "BAD_REQUEST"),
        ({"audio_b64": ok_audio, "text": " ... !? "}, 400, "TEXT_EMPTY"),
        ({"audio_b64": ok_audio, "text": "word " * 801}, 400, "BAD_REQUEST"),
        ({"audio_b64": b64(bytes(32000)), "text": "hello"}, 422, "NO_SPEECH"),
    ]
    for body, status, code in cases:
        r = client.post("/align", headers=H, json=body)
        assert (r.status_code, r.json()) == (status, {"error": {"code": code}}), body.get("text")
    r = client.post("/align", headers={**H, "Content-Type": "text/plain"}, content=b"{}")
    assert (r.status_code, r.json()["error"]["code"]) == (415, "BAD_REQUEST")
    r = client.post("/align", headers={**H, "Content-Type": "application/json"}, content=b"{not json")
    assert (r.status_code, r.json()["error"]["code"]) == (400, "BAD_REQUEST")
    assert client.aligner.calls == 1  # warmup only: invalid requests never reach the model


def test_align_audio_limit(client):
    at_limit = b64(tone(MAX_AUDIO_BYTES / 32000))
    assert client.post("/align", headers=H, json={"audio_b64": at_limit, "text": "hello"}).status_code == 200
    over = b64(tone(MAX_AUDIO_BYTES / 32000 + 0.01))
    r = client.post("/align", headers=H, json={"audio_b64": over, "text": "hello"})
    assert (r.status_code, r.json()) == (413, {"error": {"code": "AUDIO_TOO_LONG"}})


def test_model_not_ready_then_ready():
    release = threading.Event()

    def slow_factory():
        release.wait(5)
        return FakeAligner()

    with TestClient(create_app(settings(), aligner_factory=slow_factory)) as c:
        assert c.get("/health", headers=H).json()["ready"] is False
        r = c.post("/align", headers=H, json={"audio_b64": b64(tone(1.0)), "text": "hello"})
        assert (r.status_code, r.json()) == (503, {"error": {"code": "MODEL_NOT_READY"}})
        r = c.post("/prosody", headers=H, json={"audio_b64": b64(tone(0.5))})
        assert (r.status_code, r.json()) == (503, {"error": {"code": "MODEL_NOT_READY"}})
        release.set()
        wait_ready(c)
        assert c.post("/align", headers=H, json={"audio_b64": b64(tone(1.0)), "text": "hello"}).status_code == 200


def test_load_failure_is_reported_without_exit():
    def broken():
        raise RuntimeError("boom")

    with TestClient(create_app(settings(), aligner_factory=broken)) as c:
        time.sleep(0.2)
        assert c.get("/health", headers=H).json()["ready"] is False
        assert c.app.state.pron["load_error"] == "RuntimeError"


def test_align_words_keeps_order_and_bounds():
    class Canned:
        device = "cpu"

        def align(self, audio, text):
            return [("a", -0.01, 0.08), ("b", 0.08, 0.08), ("c", 0.16, 9.0)]

    words = align_words(Canned(), np.zeros(16000, np.float32), "a b c")
    assert words == [AlignedWord(0, "a", 0, 80), AlignedWord(1, "b", 80, 80), AlignedWord(2, "c", 160, 1000)]


def test_has_alignable_text():
    assert has_alignable_text("I'd like it.") and has_alignable_text("42")
    assert not has_alignable_text("  -- ... !?") and not has_alignable_text("")


def test_f0_contour_on_tone():
    x = np.frombuffer(buzz(1.0, freq=200.0), "<i2").astype(np.float32) / 32768
    f0 = f0_contour(x)
    assert len(f0) == 101  # 10 ms hop, frames at 0..1000 ms
    voiced = f0[f0 > 0]
    assert len(voiced) > 80 and abs(np.median(voiced) - 200) < 2


def test_word_stats():
    f0 = np.zeros(100)
    f0[10:20] = 200.0
    f0[20:30] = np.linspace(100, 400, 10)
    stats = word_stats(f0, [
        {"i": 0, "start_ms": 100, "end_ms": 200},
        {"i": 1, "start_ms": 200, "end_ms": 300},
        {"i": 2, "start_ms": 500, "end_ms": 650},
    ])
    assert stats[0] == {"i": 0, "mean_f0": 200.0, "f0_range_st": 0.0, "duration_ms": 100}
    assert stats[1] == {"i": 1, "mean_f0": 250.0, "f0_range_st": 24.0, "duration_ms": 100}  # 100 -> 400 Hz = 2 octaves
    assert stats[2] == {"i": 2, "mean_f0": 0.0, "f0_range_st": 0.0, "duration_ms": 150}  # no voiced frame


def test_prosody_endpoint(client):
    pcm = buzz(0.5, freq=180.0) + bytes(16000)  # 0.5 s voiced, 0.5 s silence
    words = [{"i": 0, "word": "x", "start_ms": 0, "end_ms": 500}, {"i": 1, "start_ms": 600, "end_ms": 1000}]
    r = client.post("/prosody", headers=H, json={"audio_b64": b64(pcm), "words": words})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hop_ms"] == 10 and body["method"] == "pyworld-harvest" and len(body["f0_hz"]) == 101
    assert abs(body["per_word"][0]["mean_f0"] - 180) < 3
    assert body["per_word"][1] == {"i": 1, "mean_f0": 0.0, "f0_range_st": 0.0, "duration_ms": 400}
    assert client.post("/prosody", headers=H, json={"audio_b64": b64(pcm)}).json()["per_word"] == []
    for bad in ([{"i": 0, "start_ms": 0, "end_ms": 1001}], [{"i": 0, "start_ms": 50, "end_ms": 10}],
                [{"i": "0", "start_ms": 0, "end_ms": 10}], {"i": 0}):
        r = client.post("/prosody", headers=H, json={"audio_b64": b64(pcm), "words": bad})
        assert (r.status_code, r.json()) == (400, {"error": {"code": "BAD_REQUEST"}})


def test_assess_not_implemented_by_default(client):
    r = client.post("/assess", headers=H, json={"audio_b64": b64(tone(1.0)), "reference_text": "hi", "mode": "scripted"})
    assert (r.status_code, r.json()) == (501, {"error": {"code": "NOT_IMPLEMENTED"}})


@pytest.mark.parametrize(
    "experimental,calibration,band,enabled",
    [(False, None, None, False), (False, "cal-1", None, False), (True, None, None, False), (True, "cal-1", "check", True)],
)
def test_assess_bands_need_flag_and_calibration(experimental, calibration, band, enabled):
    app = create_app(
        settings(experimental=experimental), aligner_factory=FakeAligner, scorer_factory=lambda: FakeScorer(calibration)
    )
    with TestClient(app) as c:
        wait_ready(c)
        r = c.post("/assess", headers=H, json={"audio_b64": b64(tone(1.0)), "reference_text": "hi there", "mode": "scripted"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert [w["band"] for w in body["words"]] == [band, band]
        assert body["bands_enabled"] is enabled and body["calibration_version"] == calibration
        assert body["model_revisions"] == {"aligner": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7", "phones": "0" * 40}
        h = c.get("/health", headers=H).json()
        assert (h["bands_enabled"], h["calibration_version"], h["models"]["phones"]["model_id"]) == (enabled, calibration, "fake/phones")
        r = c.post("/assess", headers=H, json={"audio_b64": b64(tone(1.0)), "reference_text": "hi", "mode": "free"})
        assert r.status_code == 400


def test_logs_carry_no_text(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.post("/align", headers=H, json={"audio_b64": b64(tone(1.0)), "text": f"My cousin ordered a {SECRET_PHRASE}."})
    client.post("/align", headers=H, json={"audio_b64": b64(tone(1.0)), "text": SECRET_PHRASE * 200})
    assert any("align.done" in rec.getMessage() for rec in caplog.records)
    assert not any(word in caplog.text for word in SECRET_PHRASE.split())
