"""Recorded practice: WAV parsing, job lifecycle, idempotency, cancel, expiry, LOCAL_BUSY, revisions (FAKE workers)."""

from __future__ import annotations

import sqlite3
import threading
import time

import numpy as np
import pytest
from conftest import Gateway, create_session, silence, tone, wav_bytes

from vr_gateway.audio import parse_wav
from vr_gateway.errors import ApiError


def speech_wav(ms=3000) -> bytes:
    return wav_bytes(np.concatenate([silence(300), tone(ms), silence(300)]))


def new_attempt(gw, exercise_type="free_answer", **body) -> str:
    ref = {"free_answer": {"exercise_id": "cafe_order.free1"}, "reading": {"text_id": "cafe_order.read1"},
           "shadowing": {"text_id": "cafe_order.shadow1"}}.get(exercise_type, {})
    resp = gw.call("POST", "/api/attempts", json={"exercise_type": exercise_type, "scenario_id": "cafe_order",
                                                  **ref, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["attempt_id"]


def upload_and_submit(gw, aid, key=None, wav=None):
    assert gw.call("PUT", f"/api/attempts/{aid}/audio", content=wav or speech_wav()).status_code == 200
    headers = {"Idempotency-Key": key} if key else {}
    return gw.call("POST", f"/api/attempts/{aid}/submit", headers=headers)


# ---------------------------------------------------------------- WAV parsing (unit)


@pytest.mark.parametrize("rate", [16000, 24000, 44100, 48000])
@pytest.mark.parametrize("channels", [1, 2])
def test_parse_wav_resamples_to_16k_mono(rate, channels):
    mono = tone(1500, rate=rate)
    pcm = np.repeat(mono, channels) if channels == 2 else mono
    decoded = parse_wav(wav_bytes(pcm, rate=rate, channels=channels), 120)
    assert decoded.source_rate == rate and decoded.source_channels == channels
    assert abs(decoded.num_samples - 24000) <= 1
    # Pitch preserved: dominant frequency stays at 220 Hz after resampling.
    spectrum = np.abs(np.fft.rfft(decoded.pcm16k.astype(np.float32)))
    peak_hz = np.argmax(spectrum) * 16000 / decoded.num_samples
    assert abs(peak_hz - 220) < 2


def test_parse_wav_stereo_is_channel_average():
    left = tone(500, amp=8000)
    right = np.zeros_like(left)
    stereo = np.stack([left, right], axis=1).reshape(-1)
    decoded = parse_wav(wav_bytes(stereo, channels=2), 120)
    assert np.abs(decoded.pcm16k.astype(int) - left.astype(int) // 2).max() <= 1


@pytest.mark.parametrize("data,code", [
    (b"", "AUDIO_INVALID"),
    (b"RIFF\x00\x00\x00\x00WAVE", "AUDIO_INVALID"),
    (wav_bytes(tone(100), rate=8000), "AUDIO_INVALID"),
    (wav_bytes(tone(100), rate=22050), "AUDIO_INVALID"),
    (wav_bytes(np.zeros(100, dtype=np.int16), channels=3), "AUDIO_INVALID"),
    (wav_bytes(np.zeros(100, dtype=np.int16), fmt=3), "AUDIO_INVALID"),
    (wav_bytes(np.zeros(0, dtype=np.int16)), "AUDIO_INVALID"),
    (wav_bytes(tone(1000))[:-1000] + b"", "AUDIO_INVALID"),  # data chunk shorter than declared
    (wav_bytes(silence(120_100)), "AUDIO_TOO_LONG"),
])
def test_parse_wav_rejects(data, code):
    with pytest.raises(ApiError) as err:
        parse_wav(data, 120)
    assert err.value.code == code


def test_parse_wav_accepts_exactly_120s():
    assert parse_wav(wav_bytes(silence(120_000)), 120).num_samples == 120 * 16000


def test_parse_wav_8bit_rejected():
    raw = bytearray(wav_bytes(np.zeros(100, dtype=np.int16)))
    raw[34:36] = (8).to_bytes(2, "little")
    with pytest.raises(ApiError):
        parse_wav(bytes(raw), 120)


# ---------------------------------------------------------------- job lifecycle


def test_free_answer_lifecycle(gw):
    aid = new_attempt(gw)
    resp = upload_and_submit(gw, aid, key="k1")
    assert resp.status_code == 202
    job = resp.json()
    assert job["state"] in ("queued", "transcribing", "analyzing", "synthesizing", "completed")
    assert gw.wait_job(job["job_id"])["state"] == "completed"
    result = gw.http.get(f"/api/attempts/{aid}/result").json()
    assert result["transcript"] == gw.asr.transcribe_text and result["transcript_revision"] == 1
    assert result["feedback"][0]["evidence_quote"] in result["transcript"]
    assert result["feedback_status"] == "ok"
    assert result["metrics"]["metrics_version"] == "m1" and result["metrics"]["word_count"] == 9
    assert result["pronunciation_score"] is None and result["pronunciation_status"] == "assessment_unavailable"
    kinds = {m["kind"] for m in result["model_audio"]}
    assert "suggestion" in kinds
    audio = gw.http.get(result["model_audio"][0]["url"])
    assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
    assert gw.asr.transcribe_calls[-1]["context"]  # scenario hint (not a target sentence)
    # The submitted take is final for this attempt.
    assert gw.call("PUT", f"/api/attempts/{aid}/audio", content=speech_wav()).status_code == 409


def test_submit_is_idempotent(gw):
    aid = new_attempt(gw)
    first = upload_and_submit(gw, aid, key="same-key")
    second = gw.call("POST", f"/api/attempts/{aid}/submit", headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    gw.wait_job(first.json()["job_id"])
    assert len(gw.asr.transcribe_calls) == 1
    other = new_attempt(gw)
    gw.call("PUT", f"/api/attempts/{other}/audio", content=speech_wav())
    conflict = gw.call("POST", f"/api/attempts/{other}/submit", headers={"Idempotency-Key": "same-key"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_submit_without_audio_is_invalid_state(gw):
    aid = new_attempt(gw)
    resp = gw.call("POST", f"/api/attempts/{aid}/submit")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "INVALID_STATE"


def test_queue_capacity_and_cancel(gw):
    gw.asr.transcribe_gate = threading.Event()
    jobs = []
    for i in range(3):
        aid = new_attempt(gw)
        resp = upload_and_submit(gw, aid, key=f"q{i}")
        assert resp.status_code == 202
        jobs.append(resp.json()["job_id"])
    time.sleep(0.1)
    assert gw.call("GET", f"/api/jobs/{jobs[0]}").json()["state"] == "transcribing"
    aid = new_attempt(gw)
    full = upload_and_submit(gw, aid, key="q3")
    assert full.status_code == 429 and full.json()["error"]["code"] == "QUEUE_FULL"

    cancelled = gw.call("DELETE", f"/api/jobs/{jobs[2]}").json()
    assert cancelled["state"] == "cancelled"
    assert gw.svc.jobs.jobs[jobs[2]].audio is None  # audio released
    running = gw.call("DELETE", f"/api/jobs/{jobs[0]}").json()
    assert running["state"] == "cancelled"
    gw.asr.transcribe_gate.set()
    assert gw.wait_job(jobs[1])["state"] == "completed"
    assert gw.call("GET", f"/api/jobs/{jobs[0]}").json()["state"] == "cancelled"
    # Cancelled attempts cannot be resubmitted with their released audio.
    again = gw.call("POST", f"/api/attempts/{gw.svc.jobs.jobs[jobs[2]].attempt_id}/submit",
                    headers={"Idempotency-Key": "fresh"})
    assert again.status_code == 409


def test_queued_job_expires_after_ttl(gw):
    gw.config.job_ttl_s = 0.3
    gw.asr.transcribe_gate = threading.Event()
    first = upload_and_submit(gw, new_attempt(gw), key="t0").json()
    queued = upload_and_submit(gw, new_attempt(gw), key="t1").json()
    time.sleep(0.5)
    state = gw.call("GET", f"/api/jobs/{queued['job_id']}").json()
    assert state["state"] == "expired" and state["error_code"] == "AUDIO_EXPIRED"
    gw.asr.transcribe_gate.set()
    gw.wait_job(first["job_id"])
    assert len(gw.asr.transcribe_calls) == 1


def test_unfinished_jobs_expire_on_restart(tmp_path):
    g = Gateway(tmp_path)
    g.asr.transcribe_gate = threading.Event()
    job = upload_and_submit(g, new_attempt(g), key="restart").json()
    time.sleep(0.1)
    g.stop()
    g.asr.transcribe_gate.set()
    # Simulate a crash: the stop above cancelled it, so force a non-terminal row like an abrupt exit would leave.
    conn = sqlite3.connect(tmp_path / "data" / "app.sqlite3")
    conn.execute("UPDATE jobs SET state = 'transcribing' WHERE job_id = ?", (job["job_id"],))
    conn.commit()
    conn.close()
    g2 = Gateway(tmp_path, data_dir=tmp_path / "data")
    try:
        row = g2.svc.db.query("SELECT state, error_code FROM jobs WHERE job_id = ?", (job["job_id"],))[0]
        assert row == {"state": "expired", "error_code": "AUDIO_EXPIRED"}
        # The same idempotency key reports the stored (expired) job instead of creating a new one.
        aid = new_attempt(g2)
        g2.call("PUT", f"/api/attempts/{aid}/audio", content=speech_wav())
        conflict = g2.call("POST", f"/api/attempts/{aid}/submit", headers={"Idempotency-Key": "restart"})
        assert conflict.status_code == 409
    finally:
        g2.stop()


def test_local_busy_during_realtime(gw):
    aid = new_attempt(gw)
    session = create_session(gw)
    resp = gw.call("PUT", f"/api/attempts/{aid}/audio", content=speech_wav())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "LOCAL_BUSY"
    gw.call("POST", f"/api/sessions/{session['session_id']}/end")
    resp = upload_and_submit(gw, aid)
    assert resp.status_code == 202


def test_transcript_revision_semantics(gw):
    aid = new_attempt(gw)
    job = upload_and_submit(gw, aid).json()
    gw.wait_job(job["job_id"])
    before = gw.http.get(f"/api/attempts/{aid}/result").json()

    resp = gw.call("PATCH", f"/api/attempts/{aid}/transcript", json={"text": "Yesterday I went to a cafe"},
                   headers={"Idempotency-Key": "rev2"})
    assert resp.status_code == 202 and resp.json()["transcript_revision"] == 2
    dup = gw.call("PATCH", f"/api/attempts/{aid}/transcript", json={"text": "Yesterday I went to a cafe"},
                  headers={"Idempotency-Key": "rev2"})
    assert dup.status_code == 200 and dup.json()["job_id"] == resp.json()["job_id"]
    gw.wait_job(resp.json()["job_id"])
    after = gw.http.get(f"/api/attempts/{aid}/result").json()

    assert after["original_transcript"] == gw.asr.transcribe_text
    assert after["transcript"] == "Yesterday I went to a cafe" and after["transcript_revision"] == 2
    assert [r["source"] for r in after["revisions"]] == ["asr", "user"]
    assert after["feedback_revision"] == 2
    assert all(i["transcript_revision"] == 2 for i in after["feedback"])
    assert after["feedback_by_revision"]["1"] == before["feedback_by_revision"]["1"]  # old feedback stays on rev 1
    assert gw.record["feedback_calls"][-1]["evidence_type"] == "user_confirmed_text"
    assert after["metrics"] == before["metrics"] and after["metrics_revision"] == 1  # not recomputed
    assert len(gw.asr.transcribe_calls) == 1


def test_reading_never_sends_target_to_asr(gw):
    gw.asr.transcribe_text = "I would like a small latte with milk"
    aid = new_attempt(gw, "reading")
    gw.wait_job(upload_and_submit(gw, aid).json()["job_id"])
    assert gw.asr.transcribe_calls[-1]["context"] is None
    result = gw.http.get(f"/api/attempts/{aid}/result").json()
    assert result["target_diff_label_ko"] == "다르게 인식된 부분"
    ops = [(d["op"], d["target"]) for d in result["target_diff"] if d["op"] != "equal"]
    assert ("missing", "oat") in ops
    assert result["feedback"] == [] and "feedback_calls" not in gw.record
    assert any(m["kind"] == "target" for m in result["model_audio"])


def test_silence_makes_no_model_calls(gw):
    aid = new_attempt(gw)
    job = upload_and_submit(gw, aid, wav=wav_bytes(silence(5000))).json()
    assert gw.wait_job(job["job_id"])["state"] == "completed"
    result = gw.http.get(f"/api/attempts/{aid}/result").json()
    assert result["no_speech"] is True and result["transcript"] is None and result["feedback"] == []
    assert gw.asr.transcribe_calls == [] and "feedback_calls" not in gw.record


def test_worker_failure_marks_job_failed(gw):
    async def broken(pcm, context=None):
        from vr_gateway.workers import WorkerError

        raise WorkerError("WORKER_FAILED")

    gw.asr.transcribe = broken
    job = upload_and_submit(gw, new_attempt(gw)).json()
    done = gw.wait_job(job["job_id"])
    assert done["state"] == "failed" and done["error_code"] == "WORKER_FAILED"


def _db_text(tmp_path) -> bytes:
    data = b""
    for p in (tmp_path / "data").glob("app.sqlite3*"):
        data += p.read_bytes()
    return data


def test_history_only_with_opt_in(gw, tmp_path):
    gw.asr.transcribe_text = "Zanzibar marmalade opt out phrase"
    out = new_attempt(gw, history_opt_in=False)
    gw.wait_job(upload_and_submit(gw, out).json()["job_id"])
    assert gw.http.get("/api/history").json()["attempts"] == []
    assert b"Zanzibar" not in _db_text(tmp_path)

    gw.asr.transcribe_text = "Quixotic pelican opt in phrase"
    keep = new_attempt(gw, history_opt_in=True)
    gw.wait_job(upload_and_submit(gw, keep).json()["job_id"])
    hist = gw.http.get("/api/history").json()
    assert [a["attempt_id"] for a in hist["attempts"]] == [keep]
    assert hist["attempts"][0]["revisions"][0]["text"] == "Quixotic pelican opt in phrase"
    assert hist["attempts"][0]["feedback"][0]["attempt_id"] == keep

    due = gw.http.get("/api/review/due").json()["items"]
    assert due and due[0]["text_en"] == "Yesterday I went"
    graded = gw.call("POST", f"/api/review/{due[0]['item_id']}/grade", json={"result": "good"}).json()
    assert graded["box"] == 2 and graded["due_at"] > time.time() + 86000
    assert gw.http.get("/api/review/due").json()["items"] == []
    again = gw.call("POST", f"/api/review/{due[0]['item_id']}/grade", json={"result": "again"}).json()
    assert again["box"] == 1

    md = gw.call("POST", "/api/history/export", json={"format": "markdown"})
    assert md.status_code == 200 and "Quixotic pelican" in md.text
    assert "attachment" in md.headers["content-disposition"]
    assert gw.call("POST", "/api/history/export").json()["attempts"][0]["attempt_id"] == keep

    assert gw.call("DELETE", "/api/history").status_code == 200
    assert gw.http.get("/api/history").json() == {"sessions": [], "attempts": []}
    assert gw.http.get("/api/review/due").json()["items"] == []


def test_review_save_requires_opt_in(gw):
    body = {"text_en": "Can I get a latte, please?", "source_type": "model_expression"}
    assert gw.call("POST", "/api/review", json=body).status_code == 409
    gw.call("PUT", "/api/settings", json={"history_opt_in": True})
    assert gw.call("POST", "/api/review", json=body).status_code == 201
    assert len(gw.http.get("/api/review/due").json()["items"]) == 1


def test_turn_based_roleplay_attempt(gw):
    session = gw.call("POST", "/api/sessions", json={"mode": "turn_based", "scenario_id": "cafe_order"}).json()
    aid = new_attempt(gw, "roleplay_turn", session_id=session["session_id"])
    gw.wait_job(upload_and_submit(gw, aid).json()["job_id"])
    result = gw.http.get(f"/api/attempts/{aid}/result").json()
    assert result["next_ai"]["status"] == "ok" and result["next_ai"]["text"].startswith("Sure")
    assert result["feedback"]  # feedback is separate from the AI reply
    assert {m["kind"] for m in result["model_audio"]} >= {"next_ai"}
    summary = gw.call("POST", f"/api/sessions/{session['session_id']}/end").json()
    assert summary["summary"]["status"] == "ok"
