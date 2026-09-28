"""Smoke test against a running stack (`./app start`): real ASR, TTS, LLM and VAD, no fakes.

  services/gateway/.venv/bin/python tests/integration/stack_smoke.py [--base http://127.0.0.1:8710]

Plays synthetic speech (macOS `say`, 16 kHz PCM16) into the realtime WebSocket as 20 ms frames in real time,
with silence frames in between like an open microphone, and checks:
  1. opening line (text + audio), one realtime turn: speech.started -> asr.partial -> asr.final -> response text -> audio
  2. barge-in: talking over the AI reply cancels it and no audio of the cancelled reply arrives afterwards
  3. a recorded reading attempt and a drill attempt: upload WAV -> job -> result with target_diff
Timings printed here come from a single run on this machine and are not benchmarks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

import httpx
import numpy as np
from websockets.asyncio.client import connect

FRAME = 320  # 20 ms at 16 kHz


def say_wav(text: str, path: Path) -> np.ndarray:
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1",
                    "-sample_fmt", "s16", str(path)], check=True)
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def pack(header: dict, pcm: bytes) -> bytes:
    h = json.dumps(header).encode()
    return struct.pack("<I", len(h)) + h + pcm


def unpack(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack_from("<I", data)[0]
    return json.loads(data[4:4 + n]), data[4 + n:]


class Stack:
    def __init__(self, base: str):
        self.base = base
        self.http = httpx.Client(base_url=base, timeout=60)
        self.http.get("/")
        self.sid_cookie = self.http.cookies.get("vr_sid")
        self.csrf = self.http.get("/api/bootstrap").json()["csrf_token"]
        self.headers = {"Origin": base, "X-VR-CSRF": self.csrf}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, headers={**self.headers, **kw.pop("headers", {})}, **kw)


class Rt:
    """Realtime client: an always-on 'microphone' task plus an event/frame reader, all timestamped."""

    def __init__(self, ws, session_id: str):
        self.ws, self.sid = ws, session_id
        self.t0 = time.monotonic()
        self.events: list[tuple[float, dict]] = []
        self.frames: list[tuple[float, dict, int]] = []
        self.speech: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.seq = 0
        self.speech_sent: list[tuple[float, float]] = []  # (start, end) of each utterance played into the mic
        self.cancelled: dict[str, float] = {}

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def send(self, type_: str, **fields) -> None:
        await self.ws.send(json.dumps({"type": type_, "event_id": uuid.uuid4().hex, "session_id": self.sid,
                                       "epoch": 0, "event_seq": 0, **fields}))

    async def mic(self) -> None:
        silence = np.zeros(FRAME, dtype="<i2")
        pending: np.ndarray | None = None
        pos = 0
        next_at = time.monotonic()
        while True:
            if pending is None and not self.speech.empty():
                pending, pos = self.speech.get_nowait(), 0
                start = self.now()
            if pending is not None:
                chunk = pending[pos:pos + FRAME]
                if len(chunk) < FRAME:
                    chunk = np.concatenate([chunk, silence[: FRAME - len(chunk)]])
                pos += FRAME
                if pos >= len(pending):
                    self.speech_sent.append((start, self.now()))
                    pending = None
            else:
                chunk = silence
            self.seq += 1
            header = {"v": 1, "kind": "input_audio", "session_id": self.sid, "turn_id": None, "epoch": 0,
                      "seq": self.seq, "sample_rate": 16000, "sample_count": FRAME}
            await self.ws.send(pack(header, chunk.astype("<i2").tobytes()))
            next_at += 0.02
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    async def reader(self) -> None:
        async for msg in self.ws:
            t = self.now()
            if isinstance(msg, bytes):
                header, payload = unpack(msg)
                assert len(payload) == header["sample_count"] * 2, "frame length mismatch"
                self.frames.append((t, header, len(payload)))
            else:
                ev = json.loads(msg)
                self.events.append((t, ev))
                if ev["type"] == "response.cancelled":
                    self.cancelled[ev["response_id"]] = t

    async def wait(self, type_: str, after: float = 0.0, timeout: float = 60.0, **match) -> tuple[float, dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for t, ev in self.events:
                if t >= after and ev["type"] == type_ and all(ev.get(k) == v for k, v in match.items()):
                    return t, ev
            await asyncio.sleep(0.01)
        raise AssertionError(f"timeout waiting for {type_} {match}; seen: {[e['type'] for _, e in self.events][-15:]}")

    def audio_for(self, response_id: str) -> list[tuple[float, dict, int]]:
        return [f for f in self.frames if f[1]["response_id"] == response_id]

    async def confirm_playback(self, response_id: str) -> None:
        for seg in sorted({h["segment_id"] for _, h, _ in self.audio_for(response_id)}):
            await self.send("playback.started", response_id=response_id, segment_id=seg)
            await self.send("playback.completed", response_id=response_id, segment_id=seg)


def check(cond: bool, what: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + what)
    if not cond:
        raise SystemExit(1)


async def realtime(stack: Stack, audio: dict[str, np.ndarray]) -> str:
    resp = stack.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order",
                                                      "difficulty": "normal", "history_opt_in": False})
    check(resp.status_code == 201, f"create realtime session ({resp.status_code})")
    sid = resp.json()["session_id"]
    ws_url = stack.base.replace("http://", "ws://") + f"/api/sessions/{sid}/realtime"
    async with connect(ws_url, additional_headers={"Origin": stack.base, "Cookie": f"vr_sid={stack.sid_cookie}"},
                       max_size=2**22) as ws:
        rt = Rt(ws, sid)
        reader = asyncio.create_task(rt.reader())
        mic = asyncio.create_task(rt.mic())
        await rt.send("session.start")

        print("\n[1] opening line")
        _, opening = await rt.wait("response.started", opening=True)
        t_done, _ = await rt.wait("response.done", response_id=opening["response_id"])
        frames = rt.audio_for(opening["response_id"])
        check(bool(frames), f"opening audio frames: {len(frames)} ({sum(n for *_, n in frames) / 48:.0f} ms at 24 kHz)")
        await rt.confirm_playback(opening["response_id"])
        await rt.wait("session.state", after=t_done, state="LISTENING")

        print("\n[2] one realtime turn")
        t_mark = rt.now()
        await rt.speech.put(audio["turn1"])
        t_start, started = await rt.wait("speech.started", after=t_mark)
        turn_id = started["turn_id"]
        t_partial, partial = await rt.wait("asr.partial", after=t_mark, turn_id=turn_id)
        t_ended, ended = await rt.wait("speech.ended", after=t_mark, turn_id=turn_id)
        t_final, final = await rt.wait("asr.final", after=t_mark, turn_id=turn_id)
        t_rs, rs = await rt.wait("response.started", after=t_mark, turn_id=turn_id)
        t_text, _ = await rt.wait("response.text", after=t_mark, response_id=rs["response_id"])
        t_rd, _ = await rt.wait("response.done", after=t_mark, response_id=rs["response_id"])
        texts = [e["text"] for _, e in rt.events if e["type"] == "response.text" and e["response_id"] == rs["response_id"]]
        frames = rt.audio_for(rs["response_id"])
        speech_start, speech_end = rt.speech_sent[-1]
        check(bool(partial["text"]), f"asr.partial after {t_partial - speech_start:.2f} s of speech")
        check(bool(final["text"]), f"asr.final: {final['text']!r}")
        check(ended.get("reason") == "silence", f"end of turn by VAD silence ({t_ended - speech_end:.2f} s after the last speech sample)")
        check(bool(texts), f"AI reply ({len(texts)} segment(s)): {' '.join(texts)!r}")
        check(sum("?" in t for t in texts) <= 1 and (not any("?" in t for t in texts) or texts[-1].rstrip('"\'').endswith("?")),
              "at most one question, nothing spoken after it")
        check(bool(frames), f"reply audio frames: {len(frames)}, sample_rate {frames[0][1]['sample_rate'] if frames else '-'}")
        first_audio = frames[0][0] if frames else float("nan")
        print(f"  single-run timings: last speech sample -> asr.final {t_final - speech_end:.2f} s, "
              f"-> first response.text {t_text - speech_end:.2f} s, -> first audio frame received {first_audio - speech_end:.2f} s, "
              f"-> response.done {t_rd - speech_end:.2f} s (server send times, not playback)")
        await rt.confirm_playback(rs["response_id"])
        await rt.wait("session.state", after=t_rd, state="LISTENING")

        print("\n[3] barge-in")
        t_mark = rt.now()
        await rt.speech.put(audio["turn2"])
        _, s2 = await rt.wait("speech.started", after=t_mark)
        _, rs2 = await rt.wait("response.started", after=t_mark, turn_id=s2["turn_id"], timeout=90)
        rid = rs2["response_id"]
        # Wait for the reply to be audibly streaming, report its first segment as playing, then talk over it.
        while not rt.audio_for(rid):
            if any(e["type"] == "response.done" and e.get("response_id") == rid for _, e in rt.events):
                break
            await asyncio.sleep(0.02)
        check(bool(rt.audio_for(rid)), "reply 2 audio started")
        await rt.send("playback.started", response_id=rid, segment_id=0)
        t_barge = rt.now()
        await rt.speech.put(audio["barge"])
        t_cancel, cancel = await rt.wait("response.cancelled", after=t_barge, response_id=rid, timeout=30)
        _, s3 = await rt.wait("speech.started", after=t_barge)
        await rt.send("playback.stopped", response_id=rid, segment_id=0, played_ms=300)
        _, rs3 = await rt.wait("response.started", after=t_barge, turn_id=s3["turn_id"], timeout=90)
        t_rd3, _ = await rt.wait("response.done", after=t_barge, response_id=rs3["response_id"], timeout=90)
        late = [f for f in rt.frames if f[1]["response_id"] == rid and f[0] > t_cancel]
        late_text = [e for t, e in rt.events if t > t_cancel and e.get("response_id") == rid and e["type"] == "response.text"]
        speech_start = rt.speech_sent[-1][0] if len(rt.speech_sent) >= 3 else t_barge
        check(cancel.get("reason") == "barge_in", f"response.cancelled reason={cancel.get('reason')} "
              f"{t_cancel - t_barge:.2f} s after barge-in audio started (includes 200 ms voiced threshold)")
        check(not late and not late_text, f"no audio/text of the cancelled reply after response.cancelled (late frames: {len(late)})")
        new_epoch = [f[1]["epoch"] for f in rt.audio_for(rs3["response_id"])]
        check(bool(new_epoch) and min(new_epoch) > rs2["epoch"], f"new reply streams with a newer epoch ({rs2['epoch']} -> {new_epoch[0] if new_epoch else '-'})")
        await rt.confirm_playback(rs3["response_id"])
        await rt.wait("session.state", after=t_rd3, state="LISTENING")

        goals = [e for _, e in rt.events if e["type"] == "goal.update"]
        print(f"  goal.update events: {len(goals)}; last: {goals[-1]['goals'] if goals else '-'}")
        mic.cancel()
        await rt.send("session.end")
        await asyncio.sleep(0.3)
        reader.cancel()
    summary = stack.call("POST", f"/api/sessions/{sid}/end")
    body = summary.json()
    check(summary.status_code == 200 and body["summary"]["pronunciation_score"] is None,
          f"session end summary: status={body['summary']['status']}, items={len(body['summary']['items'])}")
    return sid


def recorded(stack: Stack, wav_path: Path, body: dict, label: str) -> dict:
    resp = stack.call("POST", "/api/attempts", json=body)
    check(resp.status_code == 201, f"{label}: create attempt ({resp.status_code} {resp.text[:120] if resp.status_code != 201 else ''})")
    aid = resp.json()["attempt_id"]
    up = stack.call("PUT", f"/api/attempts/{aid}/audio", content=wav_path.read_bytes())
    check(up.status_code == 200, f"{label}: upload WAV ({up.json().get('duration_ms')} ms)")
    t0 = time.monotonic()
    job = stack.call("POST", f"/api/attempts/{aid}/submit", headers={"Idempotency-Key": f"smoke-{aid}"}).json()
    again = stack.call("POST", f"/api/attempts/{aid}/submit", headers={"Idempotency-Key": f"smoke-{aid}"})
    check(again.status_code == 200 and again.json()["job_id"] == job["job_id"], f"{label}: repeated submit returns the same job")
    while True:
        state = stack.call("GET", f"/api/jobs/{job['job_id']}").json()
        if state["state"] in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(0.2)
    check(state["state"] == "completed", f"{label}: job {state['state']} in {time.monotonic() - t0:.1f} s (single run)")
    result = stack.call("GET", f"/api/attempts/{aid}/result").json()
    diff = [d for d in result["target_diff"] or [] if d["op"] != "equal"]
    check(result["target_diff"] is not None, f"{label}: transcript {result['transcript']!r}; "
          f"{len(diff)} word difference(s) ({result['target_diff_label_ko']})")
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8710")
    args = ap.parse_args()
    stack = Stack(args.base)
    health = stack.http.get("/api/health").json()
    print("health:", {k: v["ready"] for k, v in health["workers"].items()}, "realtime:", health["modes"]["realtime"]["available"])
    check(health["modes"]["realtime"]["available"], "realtime mode available")
    scenario = stack.http.get("/api/scenarios/cafe_order").json()
    reading = scenario["exercises"]["reading"][0]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        audio = {
            "turn1": say_wav("Hi. Can I get a medium latte with oat milk, please?", tmp / "turn1.wav"),
            "turn2": say_wav("Actually, I would also like a blueberry muffin. How much is that in total?", tmp / "turn2.wav"),
            "barge": say_wav("Sorry, wait. Can I pay by card?", tmp / "barge.wav"),
        }
        say_wav(reading["en"], tmp / "reading.wav")
        say_wav("Could I get a large latte with oat milk?", tmp / "drill.wav")
        await realtime(stack, audio)
        print("\n[4] recorded practice")
        recorded(stack, tmp / "reading.wav", {"exercise_type": "reading", "scenario_id": "cafe_order",
                                              "text_id": reading["text_id"], "history_opt_in": False}, "reading")
        recorded(stack, tmp / "drill.wav", {"exercise_type": "drill", "scenario_id": "cafe_order",
                                            "target_text": "Could I get a large latte with oat milk?",
                                            "history_opt_in": False}, "drill")
    print("\nall smoke checks passed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
