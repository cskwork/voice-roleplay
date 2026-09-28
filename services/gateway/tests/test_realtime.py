"""Realtime WebSocket engine against FAKE ASR/TTS/LLM/VAD (see conftest.py)."""

from __future__ import annotations

import asyncio
import json
import struct

import numpy as np
import pytest
from conftest import connect_rt, create_session, silence, tone

from vr_gateway.protocol import pack_frame

pytestmark = pytest.mark.asyncio


async def start(gw, **session_kw):
    session = await asyncio.to_thread(create_session, gw, **session_kw)
    rt = await connect_rt(gw, session["session_id"])
    await rt.event("session.start")
    opening = await rt.until("response.started")  # opening line
    await rt.until("response.done")
    await rt.event("playback.completed", response_id=opening["response_id"], segment_id=0)
    await rt.until("session.state", state="LISTENING", output_state="idle")
    rt.mark_seen()
    return session, rt


async def speak_turn(rt, ms=1000, pause_ms=1100, turn_id=None):
    await rt.audio(np.concatenate([silence(320), tone(ms), silence(pause_ms)]), turn_id=turn_id)


async def confirm_playback(rt, response_id):
    segs = sorted({h["segment_id"] for h, _ in rt.frames if h["response_id"] == response_id})
    for seg in segs:
        await rt.event("playback.completed", response_id=response_id, segment_id=seg)


async def test_opening_line_and_warmup(gw):
    session = await asyncio.to_thread(create_session, gw)
    rt = await connect_rt(gw, session["session_id"])
    await rt.event("session.start")
    started = await rt.until("response.started")
    text = await rt.until("response.text")
    assert text["text"] == "Hi there! What can I get for you today?" and text["text_ko"] == "안녕하세요!"
    await rt.until("response.done")
    frames = [h for h, _ in rt.frames if h["response_id"] == started["response_id"]]
    assert frames and all(h["kind"] == "output_audio" and h["epoch"] == 0 and h["sample_rate"] == 24000 for h in frames)
    assert all(h["sample_count"] <= 2400 for h in frames)  # <= 100 ms chunks
    for h, payload in rt.frames:
        assert len(payload) == h["sample_count"] * 2
    assert gw.llm.warm_calls == 1
    assert gw.tts.wav_calls  # opening line synthesized into the cache on first use
    await rt.ws.close()


async def test_frame_validation(gw):
    session, rt = await start(gw)
    sid = session["session_id"]
    good = {"v": 1, "kind": "input_audio", "session_id": sid, "turn_id": None, "epoch": 0, "seq": 1,
            "sample_rate": 16000, "sample_count": 320}
    bad_frames = [
        b"\x01\x00",
        struct.pack("<I", 5000) + b"{}" * 10,
        pack_frame({**good, "session_id": "other"}, b"\0" * 640),
        pack_frame({**good, "sample_count": 321}, b"\0" * 640),
        pack_frame({**good, "kind": "output_audio"}, b"\0" * 640),
        pack_frame({**good, "sample_rate": 48000}, b"\0" * 640),
        pack_frame({**good, "sample_count": 33000}, b"\0" * 66000),
        pack_frame({**good, "v": 2}, b"\0" * 640),
        struct.pack("<I", 4) + b"null",
        pack_frame({**good, "pad": "x" * 1100}, b"\0" * 640),
    ]
    for frame in bad_frames:
        await rt.raw(frame)
    for _ in bad_frames:
        err = await rt.until("error", code="FRAME_INVALID")
        assert err["recoverable"] is True
    await rt.raw(pack_frame(good, b"\0" * 640))  # connection still usable
    await rt.event("bogus.event")
    assert (await rt.until("error"))["code"] == "EVENT_INVALID"
    await rt.ws.send(json.dumps({"type": "session.pause", "session_id": "someone-else"}))
    assert (await rt.until("error"))["code"] == "EVENT_INVALID"
    await rt.ws.close()


async def test_full_turn_with_partials_and_goal_update(gw):
    session, rt = await start(gw)
    await speak_turn(rt, ms=1600)
    started = await rt.until("speech.started")
    partials = [e for e in rt.events if e["type"] == "asr.partial"]
    await rt.until("speech.ended", turn_id=started["turn_id"])
    final = await rt.until("asr.final")
    assert final["text"] == gw.asr.default_final and final["transcript_revision"] == 1
    partials = [e for e in rt.events if e["type"] == "asr.partial"]
    # Replace semantics: each partial is the full current text, growing, never a delta.
    assert len(partials) >= 2
    assert partials[1]["text"].startswith(partials[0]["text"]) and len(partials[1]["text"]) > len(partials[0]["text"])
    resp = await rt.until("response.started")
    assert resp["epoch"] == 0 and resp["turn_id"] == started["turn_id"]
    texts = []
    while True:
        kind, item = await rt.recv()
        if kind == "event" and item["type"] == "response.text":
            texts.append(item["text"])
        if kind == "event" and item["type"] == "response.done":
            break
    assert " ".join(texts) == gw.llm.reply
    assert len(texts) >= 2  # segmented, not one blob
    audio = [h for h, _ in rt.frames if h["response_id"] == resp["response_id"]]
    assert {h["segment_id"] for h in audio} == set(range(len(texts)))
    assert len(gw.llm.calls) == 1
    messages = gw.llm.calls[0]
    assert gw.asr.default_final in messages[-1]["content"]
    goals = await rt.until("goal.update")
    assert goals["goals"][0] == {"goal_id": "order", "status": "done", "evidence_turn_id": started["turn_id"]}
    # History only contains AI text whose playback was confirmed.
    await confirm_playback(rt, resp["response_id"])
    await rt.until("session.state", state="LISTENING")
    await rt.drain(0.1)
    rt.mark_seen()
    await speak_turn(rt, ms=800)
    await rt.until("response.done")
    second_call = gw.llm.calls[1]
    assert any(m["role"] == "assistant" and m["content"] == gw.llm.reply for m in second_call)
    await rt.ws.close()


async def test_silence_never_calls_llm(gw):
    session, rt = await start(gw)
    await rt.audio(silence(5000))
    await rt.drain(0.3)
    assert "speech.started" not in rt.types() and gw.llm.calls == [] and gw.asr.streams == []
    await rt.ws.close()


async def test_short_blip_discarded_without_llm(gw):
    session, rt = await start(gw)
    # 7 windows (224 ms) of speech: announced (>= 200 ms) but below the 250 ms minimum.
    await rt.audio(np.concatenate([silence(320), tone(224), silence(1500)]))
    started = await rt.until("speech.started")
    ended = await rt.until("speech.ended", turn_id=started["turn_id"])
    assert ended["discarded"] is True
    await rt.drain(0.2)
    assert gw.llm.calls == [] and "asr.final" not in rt.types()
    assert gw.asr.streams[0].cancelled
    await rt.ws.close()


async def test_manual_commit_is_idempotent(gw):
    session, rt = await start(gw)
    await rt.event("input.start", turn_id="c1")
    started = await rt.until("speech.started")
    assert started["turn_id"] == "c1"
    await rt.audio(np.concatenate([tone(800), silence(3000)]), turn_id="c1")  # manual turns ignore silence
    await rt.drain(0.2)
    assert "speech.ended" not in rt.types()
    await rt.event("input.commit", turn_id="c1", last_seq=rt.seq)
    await rt.event("input.commit", turn_id="c1", last_seq=rt.seq)
    await rt.until("response.done")
    await rt.drain(0.3)
    await rt.event("input.commit", turn_id="c1", last_seq=rt.seq)
    await rt.drain(0.3)
    finals = [e for e in rt.events if e["type"] == "asr.final"]
    assert {e["turn_id"] for e in finals} == {"c1"} and len({e["text"] for e in finals}) == 1
    assert rt.types().count("response.started") == 2  # opening + exactly one reply
    assert rt.types().count("speech.ended") == 1
    assert len(gw.llm.calls) == 1 and gw.asr.streams[0].committed == 1
    await rt.ws.close()


async def test_commit_with_client_turn_id_finishes_current_speech(gw):
    session, rt = await start(gw)
    await rt.audio(tone(600))
    started = await rt.until("speech.started")
    await rt.event("input.commit", turn_id="client-7", last_seq=rt.seq)
    await rt.until("asr.final", turn_id=started["turn_id"])
    await rt.until("response.done")
    await rt.event("input.commit", turn_id="client-7", last_seq=rt.seq)
    await rt.drain(0.2)
    assert len(gw.llm.calls) == 1
    await rt.ws.close()


async def test_barge_in_cancels_and_no_stale_frames(gw):
    gw.tts.chunks = 30
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started")
    # Wait until the reply is audibly streaming, then talk over it.
    while not any(h["response_id"] == resp["response_id"] for h, _ in rt.frames):
        await rt.recv()
    await rt.event("playback.started", response_id=resp["response_id"], segment_id=0)
    await rt.audio(np.concatenate([silence(64), tone(700), silence(1100)]))
    cancelled = await rt.until("response.cancelled", response_id=resp["response_id"])
    assert cancelled["epoch"] == 1
    idx = next(i for i, (k, x) in enumerate(rt.log) if k == "event" and x["type"] == "response.cancelled")
    await rt.until("asr.final")
    new = await rt.until("response.started")
    assert new["epoch"] == 1 and new["response_id"] != resp["response_id"]
    await rt.until("response.done", response_id=new["response_id"], timeout=10)
    after = rt.log[idx + 1 :]
    stale = [x for k, x in after if (x.get("response_id") == resp["response_id"] and x.get("type") != "error")]
    assert stale == []
    assert all(x["epoch"] == 1 for k, x in after if k == "frame")
    assert gw.tts.cancels and gw.tts.cancels[0].startswith(resp["response_id"])
    types_after = [x["type"] for k, x in after if k == "event"]
    assert "response.done" in types_after
    await rt.ws.close()


async def test_response_cancel_event(gw):
    gw.tts.chunks = 30
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.event("response.cancel", response_id=resp["response_id"])
    await rt.until("response.cancelled", response_id=resp["response_id"])
    await rt.event("response.cancel", response_id=resp["response_id"])  # idempotent
    await rt.drain(0.5)
    assert rt.types().count("response.cancelled") == 1
    assert "response.done" not in [e["type"] for e in rt.events if e.get("response_id") == resp["response_id"]]
    await rt.ws.close()


async def test_45s_auto_commit_with_warning(gw):
    session, rt = await start(gw)
    await rt.audio(tone(46_000), frame=4096)
    started = await rt.until("speech.started")
    warning = await rt.until("turn.warning", timeout=10)
    assert warning["code"] == "UTTERANCE_40S" and warning["turn_id"] == started["turn_id"]
    ended = await rt.until("speech.ended", timeout=10)
    assert ended["reason"] == "max_length"
    await rt.until("asr.final")
    await rt.ws.close()


async def test_filler_extends_silence_once(gw):
    session, rt = await start(gw)
    gw.asr.partial_words = ["I", "want", "um"]
    await rt.audio(np.concatenate([silence(320), tone(1500)]))
    await rt.until("speech.started")
    await rt.until("asr.partial", text="I want um")
    await rt.audio(silence(1000))  # > 900 ms but the partial ends with a filler
    await rt.drain(0.2)
    assert "speech.ended" not in rt.types()
    await rt.audio(silence(400))
    await rt.until("speech.ended")
    await rt.ws.close()


async def test_echo_detection_switches_to_push_to_talk(gw):
    gw.tts.chunks = 40
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    for round_ in range(2):
        gw.asr.finals = ["I would like a large latte please"]
        await speak_turn(rt, ms=800)
        resp = await rt.until("response.started")
        seg = await rt.until("response.text", response_id=resp["response_id"])
        await rt.event("playback.started", response_id=resp["response_id"], segment_id=seg["segment_id"])
        gw.asr.finals = [seg["text"]]  # the mic heard the AI's own voice
        await rt.audio(np.concatenate([tone(700), silence(1100)]))
        await rt.until("response.cancelled")
        echo = await rt.until("echo.suspected")
        assert echo["count"] == round_ + 1
        await rt.drain(0.2)
    assert echo["push_to_talk_suggested"] is True and echo["auto_barge_in"] is False
    assert len(gw.llm.calls) == 2  # echo turns never reached the LLM
    # With auto barge-in off, speech during AI playback no longer cancels it.
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started")
    await rt.until("response.text", response_id=resp["response_id"])
    await rt.audio(tone(700))
    await rt.drain(0.3)
    assert resp["response_id"] not in [e.get("response_id") for e in rt.events if e["type"] == "response.cancelled"]
    await rt.ws.close()


async def test_echo_detected_after_client_side_stop(gw):
    """The browser stops AI audio on its own level detector (playback.stopped) before the gateway's VAD
    announces the speech; the reply was already fully generated, so there is nothing to cancel. The utterance
    is still a barge-in and must go through the echo check (found by the browser E2E run)."""
    session, rt = await start(gw)
    gw.asr.finals = ["I would like a large latte please"]
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started")
    seg = await rt.until("response.text", response_id=resp["response_id"])
    await rt.until("response.done", response_id=resp["response_id"])
    await rt.event("playback.started", response_id=resp["response_id"], segment_id=seg["segment_id"])
    await rt.event("playback.stopped", response_id=resp["response_id"], segment_id=seg["segment_id"], played_ms=300)
    for later in sorted({h["segment_id"] for h, _ in rt.frames if h["response_id"] == resp["response_id"]} - {0}):
        await rt.event("playback.stopped", response_id=resp["response_id"], segment_id=later, played_ms=0)
    gw.asr.finals = [seg["text"]]  # the mic heard the AI's own voice
    await rt.audio(np.concatenate([tone(700), silence(1100)]))
    echo = await rt.until("echo.suspected")
    assert echo["count"] == 1
    await rt.drain(0.3)
    assert len(gw.llm.calls) == 1  # the echo never reached the LLM
    assert "response.cancelled" not in rt.types()  # nothing was left to cancel
    await rt.ws.close()


async def test_pause_resume_and_mute(gw):
    gw.tts.chunks = 30
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.event("session.pause")
    await rt.until("response.cancelled", response_id=resp["response_id"])
    await rt.until("session.state", state="PAUSED")
    await rt.audio(np.concatenate([tone(1000), silence(1200)]))
    await rt.drain(0.2)
    assert rt.types().count("speech.started") == 1
    await rt.event("session.resume")
    await rt.until("session.state", state="LISTENING")
    await rt.event("mic.state", muted=True)
    await rt.until("session.state", input_state="muted")
    await rt.audio(np.concatenate([tone(1000), silence(1200)]))
    await rt.drain(0.2)
    assert rt.types().count("speech.started") == 1
    await rt.ws.close()


async def test_hint_and_settings(gw):
    session, rt = await start(gw)
    await rt.event("hint.request", level=1)
    hint = await rt.until("hint")
    assert hint["level"] == 1 and hint["text_ko"]
    await rt.event("hint.request", level=7)
    assert (await rt.until("error"))["code"] == "EVENT_INVALID"
    await rt.event("settings.update", silence_ms=5000, slow=True)
    applied = await rt.until("settings.applied")
    assert applied["silence_ms"] == 1400 and applied["slow"] is True
    await rt.ws.close()


async def test_llm_failure_is_recoverable_and_retry(gw):
    gw.llm.fail = True
    session, rt = await start(gw)
    await speak_turn(rt)
    err = await rt.until("error", code="LLM_FAILED")
    assert err["recoverable"] is True
    await rt.until("session.state", state="LISTENING")
    gw.llm.fail = False
    await rt.event("response.retry")
    await rt.until("response.done")
    assert sum(1 for m in gw.llm.calls[1] if m["role"] == "user" and gw.asr.default_final in m["content"]) == 1
    await rt.ws.close()


async def test_disconnect_cleans_up(gw):
    gw.tts.chunks = 50
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    await speak_turn(rt)
    await rt.until("response.started")
    await rt.ws.close()
    await asyncio.sleep(0.3)
    s = gw.svc.sessions.sessions[session["session_id"]]
    assert s.engine is None
    assert gw.svc.sessions.active_realtime() is s  # reconnect grace keeps the slot
    rt2 = await connect_rt(gw, session["session_id"])
    await rt2.event("session.start")
    await rt2.until("session.state")
    await asyncio.sleep(0.1)
    assert "response.started" not in rt2.types()  # opening is not replayed, nothing regenerated
    await rt2.ws.close()


async def test_session_end_summary_without_opt_in(gw, tmp_path):
    gw.asr.default_final = "Flibbertigibbet espresso order"
    session, rt = await start(gw, history_opt_in=False)
    await speak_turn(rt)
    await rt.until("response.done")
    await rt.event("session.end")
    await rt.until("session.state", state="CLOSED")
    summary = (await asyncio.to_thread(gw.call, "POST", f"/api/sessions/{session['session_id']}/end")).json()
    assert summary["summary"]["status"] == "ok" and len(summary["summary"]["items"]) == 1
    assert summary["summary"]["goals"][0]["status"] == "done"
    assert summary["summary"]["pronunciation_score"] is None
    assert gw.svc.db.query("SELECT * FROM sessions") == [] and gw.svc.db.query("SELECT * FROM turns") == []
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("app.sqlite3*"))
    assert b"Flibbertigibbet" not in raw


async def test_session_end_summary_with_opt_in(gw):
    gw.asr.default_final = "Can I have an oat latte"
    session, rt = await start(gw, history_opt_in=True)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.until("response.done")
    await confirm_playback(rt, resp["response_id"])
    await rt.drain(0.1)
    summary = (await asyncio.to_thread(gw.call, "POST", f"/api/sessions/{session['session_id']}/end")).json()
    assert summary["state"] == "ended"
    hist = (await asyncio.to_thread(gw.http.get, "/api/history")).json()
    turns = hist["sessions"][0]["turns"]
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant"] or [t["role"] for t in turns][:2] == ["user", "assistant"]
    user = next(t for t in turns if t["role"] == "user")
    assert user["text"] == "Can I have an oat latte" and user["metrics"]["metrics_version"] == "m1"
    ai = [t for t in turns if t["role"] == "assistant" and t["turn_id"] == resp["response_id"]][0]
    assert ai["playback_status"] == "completed"
    assert hist["sessions"][0]["summary"]["items"][0]["suggestion"] == "Could I get a latte?"
    due = (await asyncio.to_thread(gw.http.get, "/api/review/due")).json()["items"]
    assert due[0]["text_en"] == "Could I get a latte?"


async def test_logs_never_contain_transcripts(gw, caplog):
    caplog.set_level("DEBUG")
    phrase = "Supercalifragilistic unique phrase"
    gw.asr.default_final = phrase
    gw.llm.reply = "Kaleidoscopic reply sentence here. Another one follows now."
    session, rt = await start(gw)
    await speak_turn(rt)
    await rt.until("response.done")
    await rt.ws.close()
    await asyncio.sleep(0.2)
    text = caplog.text
    assert "Supercalifragilistic" not in text and "Kaleidoscopic" not in text
    assert "turn_ended" in text and "response_done" in text


async def full_turn(rt, final: str | None = None) -> dict:
    """One learner turn whose reply is fully played (playback confirmed)."""
    if final is not None:
        rt.gw.asr.finals.append(final)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.until("response.done")
    await confirm_playback(rt, resp["response_id"])
    await rt.until("session.state", state="LISTENING")
    await rt.drain(0.05)
    rt.mark_seen()
    return resp


async def test_reply_stops_after_first_question(gw):
    gw.llm.reply = "Sure, one large latte. Would you like it hot? Or maybe iced instead? We also have muffins today."
    session, rt = await start(gw)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.until("response.done")
    texts = [e["text"] for e in rt.events if e["type"] == "response.text" and e["response_id"] == resp["response_id"]]
    assert texts == ["Sure, one large latte.", "Would you like it hot?"]
    assert not any("iced" in r["text"] or "muffins" in r["text"] for r in gw.tts.requests)
    # The LLM stream was closed right after the question instead of running to the end.
    assert gw.llm.closed == 1 and gw.llm.yielded < len(gw.llm.reply.split(" "))
    await rt.ws.close()


async def test_goals_check_only_pending_goals(gw):
    session, rt = await start(gw)
    rt.gw = gw
    await full_turn(rt)
    await full_turn(rt)
    await rt.drain(0.1)
    assert [e["goals"][0]["status"] for e in rt.events if e["type"] == "goal.update"] == ["done", "done"]
    assert gw.record["goal_ids"] == [["order", "option", "price"], ["option", "price"]]
    await rt.ws.close()


async def test_goal_check_skipped_when_newer_turn_started(gw):
    gw.record["goal_delay"] = 1.0
    session, rt = await start(gw)
    rt.gw = gw
    await full_turn(rt)  # first check starts and takes 1 s
    await full_turn(rt)  # its check waits behind the first one
    await rt.audio(tone(600))  # the learner is already talking again
    await rt.until("speech.started")
    await rt.drain(1.3)
    assert len([e for e in rt.events if e["type"] == "goal.update"]) == 1
    assert len(gw.record["goal_ids"]) == 1  # the queued check for turn 2 was skipped
    await rt.ws.close()


async def test_rolling_summary_after_six_turns(gw):
    session, rt = await start(gw)
    rt.gw = gw
    for i in range(6):
        await full_turn(rt, f"Turn{i} words here please")
    assert "summary_updates" not in gw.record  # still inside the 6-turn window
    await full_turn(rt, "Turn6 words here please")
    await rt.drain(0.1)
    first = gw.record["summary_updates"][0]
    assert [t["role"] for t in first["turns"]] == ["assistant", "user"] and first["previous"] == ""
    await full_turn(rt, "Turn7 words here please")
    state = gw.llm.calls[-1][-1]["content"]
    assert "Earlier in this conversation: Summary 1 of 2 entries." in state
    assert "- drink_order: order v1" in state
    engine_session = gw.svc.sessions.sessions[session["session_id"]]
    assert engine_session.summarized_upto >= 2 and len(engine_session.learner_facts) == 1
    await rt.ws.close()


async def test_rolling_summary_never_blocks_a_reply(gw):
    gw.record["summary_delay"] = 30
    session, rt = await start(gw)
    rt.gw = gw
    for i in range(7):
        await full_turn(rt, f"Turn{i} words here please")
    assert len(gw.record["summary_updates"]) == 1  # started, still running
    await full_turn(rt, "Turn7 words here please")  # full_turn waits at most 3 s per event
    assert "Earlier in this conversation" not in gw.llm.calls[-1][-1]["content"]
    await rt.ws.close()
