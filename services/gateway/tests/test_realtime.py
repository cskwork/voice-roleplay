"""Realtime WebSocket engine against FAKE ASR/TTS/LLM/VAD (see conftest.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import struct
import time

import numpy as np
import pytest
from conftest import connect_rt, create_session, silence, tone
from test_jobs import new_attempt, speech_wav, upload_and_submit

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
    # After a reply, the hint belongs to that newest AI line (history only folds it when the next reply starts).
    await speak_turn(rt)
    reply = await rt.until("response.started")
    await rt.until("response.done")
    await rt.event("hint.request", level=1)
    assert (await rt.until("hint"))["response_id"] == reply["response_id"]
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
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant"]  # spoken order (CO-7)
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
    # The summary starts once the 7th reply is in history and covers exactly what the next prompt's verbatim
    # window (the last 12 entries) no longer has: opening, Turn0 and its reply (CO-7: no entry falls in between).
    assert [t["role"] for t in first["turns"]] == ["assistant", "user", "assistant"] and first["previous"] == ""
    assert first["turns"][1]["text"] == "Turn0 words here please"
    await full_turn(rt, "Turn7 words here please")
    state = gw.llm.calls[-1][-1]["content"]
    assert "Earlier in this conversation: Summary 1 of 3 entries." in state
    verbatim = [m["content"] for m in gw.llm.calls[-1][:-1] if m["role"] == "user" and "Turn" in m["content"]]
    assert "Turn1 words" in verbatim[0]
    assert "- drink_order: order v1" in state
    engine_session = gw.svc.sessions.sessions[session["session_id"]]
    assert engine_session.summarized_upto == 5 and len(engine_session.learner_facts) == 1
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


async def test_hint_reply_echoes_request_id(gw):
    """CO-6: a slow level-1 reply (it waits on the LLM translation) can arrive after the reply to a newer
    request; the echoed request_id lets the client drop it. The slow translation is a FAKE delay."""
    fast = gw.svc.brain.build_hint

    async def slow_level_1(scenario, difficulty, level, goals_state, last_ai_text, llm=None):
        if level == 1:
            await asyncio.sleep(0.4)
        return await fast(scenario, difficulty, level, goals_state, last_ai_text, llm)

    gw.svc.brain = dataclasses.replace(gw.svc.brain, build_hint=slow_level_1)
    _, rt = await start(gw)
    await rt.event("hint.request", level=1, request_id="h1")
    await rt.event("hint.request", level=2, request_id="h2")
    first = await rt.until("hint")
    second = await rt.until("hint")
    assert (first["request_id"], first["level"]) == ("h2", 2)
    assert (second["request_id"], second["level"]) == ("h1", 1)
    await rt.event("hint.request", level=3)  # the id is optional
    assert "request_id" not in await rt.until("hint")
    for bad in (7, "x" * 65):
        await rt.event("hint.request", level=1, request_id=bad)
        assert (await rt.until("error"))["code"] == "EVENT_INVALID"
    await rt.ws.close()


async def test_ai_line_enters_history_when_playback_completes(gw):
    """CO-7: the reply joins the LLM history and the transcript as soon as its playback is confirmed, before the
    learner's next turn, so the recorded transcript keeps the spoken order."""
    session, rt = await start(gw)
    s = gw.svc.sessions.sessions[session["session_id"]]
    opening = s.scenario["opening_line"]["en"]
    await rt.drain(0.1)  # start() may have matched the LISTENING state sent before the opening line
    assert s.history == [{"role": "assistant", "text": opening}]
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.until("response.done")
    assert s.history[-1]["role"] == "user"  # not heard yet
    rt.mark_seen()
    await confirm_playback(rt, resp["response_id"])
    await rt.until("session.state", state="LISTENING")
    assert s.history[-1] == {"role": "assistant", "text": gw.llm.reply}
    await speak_turn(rt)
    await rt.until("response.started")
    assert [(t["role"], t["turn_index"]) for t in s.turns] == [("assistant", 1), ("user", 2), ("assistant", 3),
                                                                ("user", 4)]
    assert s.turns[2]["playback_status"] == "completed"
    await rt.ws.close()


async def test_interrupted_reply_keeps_only_heard_segments_in_order(gw):
    gw.tts.chunks = 30
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    s = gw.svc.sessions.sessions[session["session_id"]]
    await speak_turn(rt, ms=800)
    resp = await rt.until("response.started")
    first = await rt.until("response.text", response_id=resp["response_id"])
    await rt.until("response.text", response_id=resp["response_id"], segment_id=1)  # still being synthesized
    await rt.event("playback.completed", response_id=resp["response_id"], segment_id=first["segment_id"])
    await rt.event("response.cancel", response_id=resp["response_id"])
    await rt.until("response.cancelled", response_id=resp["response_id"])
    assert s.history[-1] == {"role": "assistant", "text": first["text"]}
    ai = s.turns[-1]
    assert ai["turn_id"] == resp["response_id"] and ai["playback_status"] == "interrupted"
    assert ai["spoken_segments"][0]["status"] == "played"
    assert all(x["status"] == "interrupted" for x in ai["spoken_segments"][1:])
    await speak_turn(rt)
    await rt.until("response.started", timeout=10)
    assert [t["role"] for t in s.turns] == ["assistant", "user", "assistant", "user"]
    await rt.ws.close()


# ---------------------------------------------------------------- starting something new ends the conversation


async def closed_by_server(rt, timeout: float = 3.0) -> tuple[int | None, str | None]:
    """Reads until the gateway closes the socket; returns (code, reason)."""
    from websockets.exceptions import ConnectionClosed

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            await rt.recv(deadline - time.monotonic())
        except ConnectionClosed:
            break
    else:
        raise AssertionError("socket still open")
    return rt.ws.close_code, rt.ws.close_reason


async def test_submit_during_active_session_ends_it(gw):
    """User flow: realtime conversation, then 녹음형 연습 without pressing 종료 → the submit is accepted."""
    gw.asr.default_final = "Can I have an oat latte"
    session, rt = await start(gw)
    await speak_turn(rt)
    resp = await rt.until("response.started")
    await rt.until("response.done")
    await confirm_playback(rt, resp["response_id"])
    aid = await asyncio.to_thread(new_attempt, gw, "reading")
    sub = await asyncio.to_thread(upload_and_submit, gw, aid)
    assert sub.status_code == 202
    await rt.until("session.state", state="CLOSED")
    assert await closed_by_server(rt) == (4001, "session_superseded")
    s = gw.svc.sessions.sessions[session["session_id"]]
    assert s.state == "ended" and s.end_reason == "superseded" and s.engine is None
    assert gw.svc.sessions.active_realtime() is None
    summary = (await asyncio.to_thread(gw.call, "POST", f"/api/sessions/{session['session_id']}/end")).json()
    assert summary["state"] == "ended" and summary["summary"]["status"] == "ok"
    assert summary["summary"]["items"][0]["suggestion"] == "Could I get a latte?"
    job = await asyncio.to_thread(gw.wait_job, sub.json()["job_id"])
    assert job["state"] == "completed"


async def test_new_session_ends_connected_previous_one(gw):
    gw.tts.chunks = 50
    gw.tts.chunk_delay = 0.03
    session, rt = await start(gw)
    await speak_turn(rt)
    await rt.until("response.started")  # a reply is still being synthesized: it must be cancelled
    second = await asyncio.to_thread(create_session, gw)
    assert (await rt.until("response.cancelled"))["reason"] == "superseded"
    assert await closed_by_server(rt) == (4001, "session_superseded")
    assert [s.session_id for s in gw.svc.sessions.live_realtime()] == [second["session_id"]]
    rt2 = await connect_rt(gw, second["session_id"])
    await rt2.event("session.start")
    await rt2.until("response.started")
    # The ended session cannot be reconnected.
    from websockets.exceptions import InvalidStatus

    with pytest.raises(InvalidStatus):
        await connect_rt(gw, session["session_id"])
    await rt2.ws.close()


async def test_end_request_closes_socket_with_ended_code(gw):
    session, rt = await start(gw)
    await asyncio.to_thread(gw.call, "POST", f"/api/sessions/{session['session_id']}/end")
    assert await closed_by_server(rt) == (4000, "session_ended")


async def test_concurrent_switch_requests_leave_one_session(gw):
    """Two new sessions and a submit at the same moment: no deadlock, no second live session, every request served."""
    import httpx

    aid = await asyncio.to_thread(new_attempt, gw, "reading")
    upload = await asyncio.to_thread(gw.call, "PUT", f"/api/attempts/{aid}/audio", content=speech_wav())
    assert upload.status_code == 200
    session, rt = await start(gw)
    gw.tts.close_delay = 0.3  # the old engine is still stopping while the other requests arrive
    body = {"mode": "realtime", "scenario_id": "cafe_order", "difficulty": "normal", "history_opt_in": False}
    async with httpx.AsyncClient(base_url=gw.base, headers=gw.headers, cookies={"vr_sid": gw.sid}, timeout=10) as c:
        results = await asyncio.wait_for(asyncio.gather(
            c.post("/api/sessions", json=body),
            c.post(f"/api/attempts/{aid}/submit"),
            c.post("/api/sessions", json=body),
            c.post("/api/sessions", json=body),
        ), 10)
        assert [r.status_code for r in results] == [201, 202, 201, 201]
        created = [r.json()["session_id"] for r in results if r.status_code == 201]
        states = [(await c.get(f"/api/sessions/{sid}")).json()["state"] for sid in [session["session_id"], *created]]
    assert states.count("ended") == 3 and states.count("created") == 1
    assert len(gw.svc.sessions.live_realtime()) == 1
    assert await closed_by_server(rt) == (4001, "session_superseded")
    assert (await asyncio.to_thread(gw.wait_job, results[1].json()["job_id"]))["state"] == "completed"


async def test_local_busy_only_when_old_engine_does_not_stop(gw, monkeypatch):
    import vr_gateway.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "STOP_TIMEOUT_S", 0.1)
    session, rt = await start(gw)
    gw.tts.close_delay = 0.6  # the old engine's shutdown hangs past the timeout
    body = {"mode": "realtime", "scenario_id": "cafe_order"}
    busy = await asyncio.to_thread(gw.call, "POST", "/api/sessions", json=body)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "LOCAL_BUSY"
    assert gw.svc.sessions.sessions[session["session_id"]].state == "ended"  # still being stopped, never revived
    await closed_by_server(rt)
    retry = await asyncio.to_thread(create_session, gw)
    assert [s.session_id for s in gw.svc.sessions.live_realtime()] == [retry["session_id"]]
