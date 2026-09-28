"""Pronunciation analysis of recorded attempts (PROTOCOL §12.4, PRD §8.4, AT-18/23/24/25/26).

All pronunciation-worker responses come from FakePron (conftest.py, labelled FAKE): these tests check the gateway's
call sequence, status mapping, gating, what leaves the gateway, storage and logging, not any model output.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import httpx
import pytest
from conftest import FakePron, Gateway, create_session
from test_jobs import new_attempt, speech_wav, upload_and_submit

from vr_gateway import pronunciation
from vr_gateway.workers import PronClient, WorkerError

TARGET = "I would like a small latte with oat milk."
TARGET_WORDS = ["I", "would", "like", "a", "small", "latte", "with", "oat", "milk"]
LEXICON = "latte  L AE1 T EY0\nmilk  M IH1 L K\nthink  TH IH1 NG K\ncar  K AA1 R\n"


@pytest.fixture
def pgw(tmp_path):
    lex = tmp_path / "cmudict.dict"
    lex.write_text(LEXICON)
    g = Gateway(tmp_path, pron=FakePron(), pron_lexicon=lex)
    yield g
    g.stop()


def run_attempt(g: Gateway, exercise_type: str = "reading", **body) -> dict:
    aid = new_attempt(g, exercise_type, **body)
    job = upload_and_submit(g, aid).json()
    assert g.wait_job(job["job_id"])["state"] == "completed"
    return g.http.get(f"/api/attempts/{aid}/result").json()


def no_numbers_leak(result: dict) -> None:
    """GOP values and posteriors never leave the gateway; no score anywhere (PROTOCOL §12.1)."""
    text = json.dumps(result)
    for key in ('"gop"', '"word_gop"', '"p"', '"heard_candidates"', '"phones": [', "score\": 0", "score\": 1"):
        assert key not in text, key
    assert result["pronunciation_score"] is None


def test_timing_only_reading(pgw):
    pgw.asr.transcribe_text = "I would like a small latte with milk"
    r = run_attempt(pgw, "reading")
    p = r["pronunciation"]
    assert r["pronunciation_status"] == "timing_only" and p["status"] == "timing_only" and p["reason"] is None
    assert p["mode"] == "scripted" and p["transcript_revision"] == 1
    assert p["note_ko"] == pronunciation.NOTE_KO["timing_only"]
    assert [w["word"] for w in p["words"]] == TARGET_WORDS
    assert all(w["band"] is None and w["heard_ipa"] is None and w["weak_sounds"] is None for w in p["words"])
    assert p["words"][0]["gap_before_ms"] == 0 and p["words"][1]["gap_before_ms"] == 100
    assert p["words"][0]["duration_ms"] == 300
    # Target diff: "oat" was not heard -> the aligner's interval is marked uncertain; heard words are True.
    flags = {w["word"]: w["in_transcript"] for w in p["words"]}
    assert flags["oat"] is False and flags["latte"] is True and flags["milk"] is True
    # Guide links come from CMUdict + guide.json espeak_map ("this sound is in the word", never a judgement).
    guide = {w["word"]: w["guide_ids"] for w in p["words"]}
    assert guide["latte"] == ["r_l", "ae_e"] and guide["milk"] == ["ih_ee", "r_l"] and guide["would"] == []
    assert p["evidence_types"] == ["word_timing", "prosody_contour"] and p["calibration_version"] is None
    assert p["model_revisions"] == {"aligner": "fake-a"}
    # Calls: learner /align + /prosody on the 16 kHz take, then the same on the model audio (resampled to 16 kHz).
    calls = [(c["endpoint"], c["bytes"]) for c in pgw.pron.calls]
    learner_bytes = 3600 * 32
    assert calls[:2] == [("align", learner_bytes), ("prosody", learner_bytes)]
    assert [c[0] for c in calls[2:]] == ["align", "prosody"] and calls[2][1] == 300 * 32
    assert all(c["text"] == TARGET for c in pgw.pron.calls if c["endpoint"] == "align")
    assert "assess" not in [c["endpoint"] for c in pgw.pron.calls]
    assert all(c["timeout_s"] == 30.0 for c in pgw.pron.calls)
    learner = p["prosody"]["learner"]
    assert learner["hop_ms"] == 10 and len(learner["f0_hz"]) == 360 and len(learner["per_word"]) == 9
    model = p["prosody"]["model"]
    assert model["audio_id"] == "target" and [w["word"] for w in model["words"]] == TARGET_WORDS
    assert any(m["audio_id"] == "target" for m in r["model_audio"])
    no_numbers_leak(r)


def test_unscripted_uses_asr_transcript_and_no_model_side(pgw):
    r = run_attempt(pgw, "free_answer")
    p = r["pronunciation"]
    assert p["status"] == "timing_only" and p["mode"] == "unscripted"
    assert [c["endpoint"] for c in pgw.pron.calls] == ["align", "prosody"]
    assert pgw.pron.calls[0]["text"] == pgw.asr.transcribe_text
    assert all(w["in_transcript"] is None for w in p["words"])
    assert p["prosody"]["model"] is None


def test_transcript_edit_does_not_realign(pgw):
    r = run_attempt(pgw, "free_answer")
    calls = len(pgw.pron.calls)
    resp = pgw.call("PATCH", f"/api/attempts/{r['attempt_id']}/transcript", json={"text": "Yesterday I went to a cafe"})
    pgw.wait_job(resp.json()["job_id"])
    after = pgw.http.get(f"/api/attempts/{r['attempt_id']}/result").json()
    assert len(pgw.pron.calls) == calls
    assert after["pronunciation"] == r["pronunciation"] and after["pronunciation"]["transcript_revision"] == 1


def test_not_configured_is_unavailable(gw):
    health = gw.http.get("/api/health").json()
    assert "pron" not in health["workers"] and health["pronunciation_assessment"] == "assessment_unavailable"
    r = run_attempt(gw, "reading")
    assert r["pronunciation_status"] == "assessment_unavailable"
    assert r["pronunciation"]["status"] == "unavailable" and r["pronunciation"]["reason"] == "NOT_CONFIGURED"
    assert r["pronunciation"]["note_ko"] == pronunciation.NOTE_KO["unavailable"]
    assert r["transcript"] and r["target_diff"] is not None  # the rest of the result is unchanged (AT-18)


@pytest.mark.parametrize("endpoint,code", [("align", "TIMEOUT"), ("prosody", "WORKER_FAILED"), ("align", "NO_SPEECH")])
def test_worker_failure_never_fails_the_job(pgw, endpoint, code):
    pgw.pron.fail[endpoint] = code
    r = run_attempt(pgw, "free_answer")
    assert r["pronunciation"]["status"] == "unavailable" and r["pronunciation"]["reason"] == code
    assert r["pronunciation_status"] == "assessment_unavailable"
    assert r["transcript"] and r["feedback_status"] == "ok" and r["feedback"]


def test_worker_not_ready_and_unreachable(pgw):
    pgw.pron.ready = False
    health = pgw.http.get("/api/health").json()
    assert health["workers"]["pron"]["ready"] is False and health["pronunciation_assessment"] == "assessment_unavailable"
    r = run_attempt(pgw, "reading")
    assert r["pronunciation"]["reason"] == "MODEL_NOT_READY" and pgw.pron.calls == []
    pgw.pron.ready = None  # process gone
    pgw.run(pgw.svc.health.snapshot(force=True))
    health = pgw.http.get("/api/health").json()
    assert health["workers"]["pron"]["reachable"] is False


def test_crash_in_analysis_is_contained(pgw):
    async def broken(*a, **kw):
        raise RuntimeError("boom")

    pgw.pron.align = broken
    r = run_attempt(pgw, "reading")
    assert r["pronunciation"]["status"] == "unavailable" and r["pronunciation"]["reason"] == "WORKER_FAILED"
    assert r["job"]["state"] == "completed"


def test_silence_is_no_speech_without_worker_calls(pgw):
    from conftest import silence, wav_bytes

    aid = new_attempt(pgw, "reading")
    pgw.wait_job(upload_and_submit(pgw, aid, wav=wav_bytes(silence(3000))).json()["job_id"])
    r = pgw.http.get(f"/api/attempts/{aid}/result").json()
    assert r["pronunciation"]["reason"] == "NO_SPEECH" and pgw.pron.calls == []


def test_realtime_session_blocks_worker_calls(pgw):
    """A realtime session that starts while a job transcribes: the job finishes, the worker is never called (AT-25)."""
    pgw.asr.transcribe_gate = threading.Event()
    aid = new_attempt(pgw, "reading")
    job = upload_and_submit(pgw, aid).json()
    session = create_session(pgw)
    pgw.asr.transcribe_gate.set()
    assert pgw.wait_job(job["job_id"])["state"] == "completed"
    r = pgw.http.get(f"/api/attempts/{aid}/result").json()
    assert r["pronunciation"]["status"] == "unavailable" and r["pronunciation"]["reason"] == "LOCAL_BUSY"
    assert pgw.pron.calls == []
    pgw.call("POST", f"/api/sessions/{session['session_id']}/end")


def test_experimental_banded(pgw):
    pgw.pron.bands_enabled = True
    pgw.pron.assessed = {
        "latte": {"band": "practice", "phones": [
            {"expected_ipa": "l", "gop": -2.0, "heard_candidates": [{"ipa": "ɹ", "p": 0.8}, {"ipa": "l", "p": 0.1}]},
            {"expected_ipa": "æ", "gop": -0.1, "heard_candidates": [{"ipa": "æ", "p": 0.9}]},
            # Accepted variant (flap) first: skipped, the next candidate is reported.
            {"expected_ipa": "t", "gop": -1.2, "heard_candidates": [{"ipa": "ɾ", "p": 0.3}, {"ipa": "", "p": 0.6}]},
        ]},
        "milk": {"band": "check", "phones": [
            {"expected_ipa": "ɪ", "gop": -0.2, "heard_candidates": [{"ipa": "ɪ", "p": 0.8}]}]},
    }
    health = pgw.http.get("/api/health").json()
    assert health["pronunciation_assessment"] == "experimental_banded" and health["workers"]["pron"]["bands_enabled"]
    r = run_attempt(pgw, "reading")
    p = r["pronunciation"]
    assert r["pronunciation_status"] == "experimental_banded" and p["status"] == "experimental_banded"
    assert p["calibration_version"] == "FAKE-cal-1" and p["note_ko"] == pronunciation.NOTE_KO["experimental_banded"]
    assert p["evidence_types"] == ["word_timing", "prosody_contour", "phone_gop"]
    assert p["model_revisions"] == {"aligner": "fake-a", "phones": "fake-p"}
    # /assess replaces the learner /align; the model side still uses /align.
    assert [c["endpoint"] for c in pgw.pron.calls] == ["assess", "prosody", "align", "prosody"]
    words = {w["word"]: w for w in p["words"]}
    assert words["latte"]["band"] == "practice" and words["milk"]["band"] == "check" and words["I"]["band"] == "good"
    assert words["latte"]["weak_sounds"] == [
        {"expected_ipa": "l", "heard_ipa": "ɹ", "guide_ids": ["r_l"]},
        {"expected_ipa": "t", "heard_ipa": "", "guide_ids": []},
    ]
    assert words["latte"]["heard_ipa"] == ["ɹ", ""]
    assert words["milk"]["weak_sounds"] == [] and words["I"]["weak_sounds"] == []
    assert words["latte"]["guide_ids"] == ["r_l", "ae_e"]
    no_numbers_leak(r)


def test_flag_without_phone_model_falls_back_to_timing(pgw):
    pgw.pron.bands_enabled = True
    pgw.pron.fail["assess"] = "NOT_IMPLEMENTED"
    r = run_attempt(pgw, "reading")
    assert r["pronunciation"]["status"] == "timing_only"
    assert all(w["band"] is None for w in r["pronunciation"]["words"])


def test_unscripted_bands_mode(pgw):
    pgw.pron.bands_enabled = True
    r = run_attempt(pgw, "free_answer")
    assert r["pronunciation"]["mode"] == "unscripted" and r["pronunciation"]["status"] == "experimental_banded"
    assert pgw.pron.calls[0]["mode"] == "unscripted" and pgw.pron.calls[0]["text"] == pgw.asr.transcribe_text


def test_history_keeps_only_words_and_bands(pgw, tmp_path):
    pgw.pron.bands_enabled = True
    pgw.pron.assessed = {"latte": {"band": "practice", "phones": [
        {"expected_ipa": "l", "gop": -2.0, "heard_candidates": [{"ipa": "ɹ", "p": 0.8}]}]}}
    r = run_attempt(pgw, "reading", history_opt_in=True)
    hist = pgw.http.get("/api/history").json()["attempts"][0]
    stored = hist["extra"]["pronunciation"]
    assert stored["status"] == "experimental_banded" and stored["calibration_version"] == "FAKE-cal-1"
    assert set(stored["words"][0]) == {"i", "word", "start_ms", "end_ms", "band"}
    db = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("app.sqlite3*"))
    assert b"f0_hz" not in db and b"heard_ipa" not in db and "ɹ".encode() not in db
    assert r["pronunciation"]["prosody"]["learner"]["f0_hz"]


def test_logs_have_no_text(pgw, caplog):
    caplog.set_level(logging.INFO)
    pgw.asr.transcribe_text = "Quokka saxophone unique phrase"
    run_attempt(pgw, "free_answer")
    run_attempt(pgw, "reading")
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "pron_done" in logs
    for secret in ("Quokka", "saxophone", "latte", "oat milk"):
        assert secret not in logs


def test_guide_endpoint(pgw):
    resp = pgw.http.get("/api/pronunciation/guide")
    assert resp.status_code == 200
    guide = resp.json()
    assert {e["entry_id"] for e in guide["entries"]} >= {"r_l", "th_s"} and "review_status" in guide


def test_timeout_values():
    assert pronunciation.call_timeout(30.0) == 30.0 and pronunciation.call_timeout(30.01) == 90.0


def test_arpabet_units_merge_like_the_worker():
    assert pronunciation.arpabet_units(["K", "AA1", "R"]) == ["k", "ɑːɹ"]
    assert pronunciation.arpabet_units(["F", "AY1", "ER0"]) == ["f", "aɪɚ"]
    assert pronunciation.arpabet_units(["AH0", "B", "AH1", "V"]) == ["ə", "b", "ʌ", "v"]


def test_in_transcript_flags_unmatched_is_unknown():
    words = [{"word": w} for w in ["It's", "4", "50", "please"]]
    diff = [{"op": "equal", "target": "It's", "heard": "it's"}, {"op": "different", "target": "$4.50", "heard": "4"},
            {"op": "equal", "target": "please", "heard": "please"}]
    assert pronunciation.in_transcript_flags(words, diff) == [True, None, None, True]


# ---------------------------------------------------------------- real HTTP client against a FAKE transport


def _client(handler) -> PronClient:
    c = PronClient("http://127.0.0.1:8714", "tok")
    c.http = httpx.AsyncClient(base_url=c.base_url, transport=httpx.MockTransport(handler),
                               headers={"X-Worker-Token": "tok"})
    return c


def test_pron_client_codes_and_timeout():
    async def main():
        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(WorkerError) as err:
            await _client(timeout).align(b"\0\0", "hi", 1.0)
        assert err.value.code == "TIMEOUT"

        def no_speech(request):
            body = json.loads(request.content)
            assert request.headers["x-worker-token"] == "tok" and body["audio_b64"] == "AAA=" and body["text"] == "hi"
            return httpx.Response(422, json={"error": {"code": "NO_SPEECH"}})

        with pytest.raises(WorkerError) as err:
            await _client(no_speech).align(b"\0\0", "hi", 1.0)
        assert err.value.code == "NO_SPEECH"

        def weird(request):
            return httpx.Response(500, text="oops")

        with pytest.raises(WorkerError) as err:
            await _client(weird).prosody(b"\0\0", None, 1.0)
        assert err.value.code == "WORKER_FAILED"

    asyncio.run(main())
