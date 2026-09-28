"""Test harness: the real gateway app served by uvicorn on a free port, with FAKE workers.

Everything named Fake* below is a test double, not a model: the ASR/TTS/LLM/VAD fakes return scripted
text/tones so the gateway logic (auth, queueing, turn taking, cancellation) can be tested deterministically.
Real-model checks live in test_vad_real.py (Silero) and are reported separately.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import struct
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import uvicorn

from vr_gateway.app import build_services, create_app
from vr_gateway.brain import default_brain
from vr_gateway.config import Config
from vr_gateway.protocol import pack_frame
from vr_gateway.workers import WorkerError

REPO = Path(__file__).resolve().parents[3]

SCENARIO = {
    "scenario_id": "cafe_order",
    "version": "1.0.0",
    "title_ko": "카페 주문",
    "title_en": "Ordering at a cafe",
    "ai_role": "barista",
    "ai_role_ko": "바리스타",
    "user_role_ko": "손님",
    "setting_ko": "동네 카페",
    "default_voice_id": "voice_a",
    "facts": {"latte_price": "$4.50"},
    "opening_line": {"text_id": "cafe_order.opening", "en": "Hi there! What can I get for you today?", "ko": "안녕하세요!"},
    "goals": [
        {"goal_id": "order", "en": "Order a drink", "ko": "음료 주문"},
        {"goal_id": "option", "en": "Change an option", "ko": "옵션 변경"},
        {"goal_id": "price", "en": "Ask the price", "ko": "가격 확인"},
    ],
    "allowed_flow": ["greet", "order", "pay"],
    "difficulty": {
        "easy": {"guidance_en": "simple", "silence_ms": 1200},
        "normal": {"guidance_en": "normal", "silence_ms": 900},
        "hard": {"guidance_en": "hard", "silence_ms": 700},
    },
    "hints": [{"hint_id": "h1", "goal_id": "order", "ko": "음료를 주문해 보세요", "keywords": ["latte"],
               "example_en": "Can I get a latte, please?"}],
    "model_expressions": [{"text_id": "cafe_order.expr1", "goal_id": "order", "en": "Can I get a latte, please?", "ko": "라테 주세요"}],
    "exercises": {
        "reading": [{"text_id": "cafe_order.read1", "en": "I would like a small latte with oat milk.", "ko": "..."}],
        "shadowing": [{"text_id": "cafe_order.shadow1", "en": "Could I have it to go?", "ko": "..."}],
        "free_answer": [{"exercise_id": "cafe_order.free1", "question_en": "What is your favorite drink?",
                         "question_ko": "좋아하는 음료는?", "sample_answer_en": "My favorite drink is a latte."}],
    },
}

TTS_RATE = 24000


# ---------------------------------------------------------------- audio helpers


def tone(ms: int, amp: int = 6000, rate: int = 16000) -> np.ndarray:
    n = rate * ms // 1000
    t = np.arange(n) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.int16)


def silence(ms: int, rate: int = 16000) -> np.ndarray:
    return np.zeros(rate * ms // 1000, dtype=np.int16)


def wav_bytes(pcm: np.ndarray, rate: int = 16000, channels: int = 1, bits: int = 16, fmt: int = 1) -> bytes:
    data = pcm.astype("<i2").tobytes()
    block = channels * bits // 8
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, fmt, channels, rate,
                       rate * block, block, bits, b"data", len(data)) + data


# ---------------------------------------------------------------- FAKE workers


class FakeAsrStream:
    """FAKE ASR stream: emits a growing partial every 0.5 s of audio, a scripted final on commit."""

    def __init__(self, owner: FakeAsr, on_partial):
        self.owner = owner
        self.on_partial = on_partial
        self.bytes = 0
        self.cancelled = False
        self.committed = 0
        self._words = 0

    async def send_audio(self, pcm: bytes) -> None:
        self.bytes += len(pcm)
        while self.bytes >= (self._words + 1) * 16000:
            self._words += 1
            self.on_partial(" ".join(self.owner.partial_words[: self._words]))

    async def commit(self, timeout_s: float = 30.0) -> dict:
        self.committed += 1
        if self.owner.final_delay:
            await asyncio.sleep(self.owner.final_delay)
        if self.owner.fail_final:
            raise WorkerError("ASR_FAILED")
        text = self.owner.finals.pop(0) if self.owner.finals else self.owner.default_final
        return {"type": "final", "text": text, "audio_ms": self.bytes // 32, "elapsed_ms": 1}

    async def cancel(self) -> None:
        self.cancelled = True

    async def close(self) -> None:
        pass


class FakeAsr:
    def __init__(self):
        self.ready = True
        self.streams: list[FakeAsrStream] = []
        self.finals: list[str] = []
        self.default_final = "I would like a large latte please"
        self.partial_words = ["Can", "I", "get", "one", "large", "latte", "please", "now"]
        self.transcribe_text = "Yesterday I go to a cafe with my friend"
        self.transcribe_calls: list[dict] = []
        self.transcribe_gate: threading.Event | None = None
        self.fail_final = False
        self.final_delay = 0.0

    async def health(self):
        return {"ready": self.ready, "model_id": "FAKE-asr", "device": "cpu"} if self.ready is not None else None

    async def open_stream(self, on_partial):
        stream = FakeAsrStream(self, on_partial)
        self.streams.append(stream)
        return stream

    async def transcribe(self, pcm: bytes, context: str | None = None) -> dict:
        self.transcribe_calls.append({"bytes": len(pcm), "context": context})
        while self.transcribe_gate is not None and not self.transcribe_gate.is_set():
            await asyncio.sleep(0.01)
        return {"text": self.transcribe_text, "language": "English", "audio_ms": len(pcm) // 32, "elapsed_ms": 1}


class FakeTtsStream:
    """FAKE TTS: `chunks` 100 ms tone chunks per request with `chunk_delay` between them; honours cancel."""

    def __init__(self, owner: FakeTts):
        self.owner = owner
        self.cancelled: set[str] = set()

    async def synthesize(self, request_id, voice_id, text, speed=1.0):
        self.owner.requests.append({"request_id": request_id, "text": text, "speed": speed})
        yield "start", TTS_RATE
        chunk = tone(100, rate=TTS_RATE).tobytes()
        for _ in range(self.owner.chunks):
            if request_id in self.cancelled:
                self.owner.cancel_confirmed.append(request_id)
                yield "cancelled", None
                return
            await asyncio.sleep(self.owner.chunk_delay)
            if request_id in self.cancelled:
                continue
            yield "audio", chunk
        yield "done", {"request_id": request_id, "audio_ms": 100 * self.owner.chunks}

    async def cancel(self, request_id):
        self.cancelled.add(request_id)
        self.owner.cancels.append(request_id)

    async def close(self):
        await asyncio.sleep(self.owner.close_delay)


class FakeTts:
    def __init__(self):
        self.ready = True
        self.chunks = 3
        self.chunk_delay = 0.0
        self.close_delay = 0.0  # slow stream close: keeps an engine shutdown in flight (session switch races)
        self.requests: list[dict] = []
        self.cancels: list[str] = []
        self.cancel_confirmed: list[str] = []
        self.wav_calls: list[dict] = []

    async def health(self):
        return {"ready": self.ready, "model_id": "FAKE-tts", "sample_rate": TTS_RATE,
                "voices": [{"voice_id": "voice_a", "label": "A", "license_note": "fake"}]}

    async def synthesize_wav(self, voice_id, text, speed=1.0) -> bytes:
        self.wav_calls.append({"voice_id": voice_id, "text": text, "speed": speed})
        return wav_bytes(tone(300, rate=TTS_RATE), rate=TTS_RATE)

    def stream_session(self):
        return FakeTtsStream(self)


class FakeLlm:
    """FAKE LLM: streams a scripted reply word by word; stops when the cancel event is set."""

    def __init__(self):
        self.ready = True
        self.reply = "Sure, one large latte coming right up. Would you like anything else with that today?"
        self.token_delay = 0.0
        self.calls: list[list[dict]] = []
        self.warm_calls = 0
        self.cancelled = 0
        self.yielded = 0  # deltas actually handed to the gateway
        self.closed = 0  # streams closed by the gateway before the reply ended
        self.slots: list[int | None] = []
        self.fail = False

    async def health(self):
        return self.ready

    async def warm(self, messages, *, slot_id=None):
        self.warm_calls += 1

    async def stream_chat(self, messages, *, max_tokens=128, slot_id=None, cancel=None):
        from vr_feedback.llm import LlmError

        self.calls.append(messages)
        self.slots.append(slot_id)
        if self.fail:
            raise LlmError("unreachable")
        words = self.reply.split(" ")
        finished = False
        try:
            for i, w in enumerate(words):
                if cancel is not None and cancel.is_set():
                    self.cancelled += 1
                    return
                await asyncio.sleep(self.token_delay)
                self.yielded += 1
                yield (w if i == 0 else " " + w)
            finished = True
        finally:
            if not finished and not (cancel is not None and cancel.is_set()):
                self.closed += 1

    async def json_chat(self, messages, schema, *, max_tokens=768):
        raise AssertionError("FakeLlm.json_chat should not be reached; brain LLM functions are faked")

    async def aclose(self):
        pass


class FakePron:
    """FAKE pronunciation worker (PROTOCOL §12.2): scripted word timings (300 ms per word, 100 ms gaps), a flat
    150 Hz contour, and for /assess scripted phones/GOP/bands. Not a model; records calls (sizes, texts)."""

    def __init__(self):
        self.ready = True
        self.bands_enabled = False
        self.calibration_version = "FAKE-cal-1"
        self.calls: list[dict] = []
        self.fail: dict[str, str] = {}  # endpoint -> WorkerError code
        self.delay = 0.0
        # /assess per word (by lowercase word): band + phones; others get band "good" and no phones.
        self.assessed: dict[str, dict] = {}

    async def health(self):
        if self.ready is None:
            return None
        return {"ready": self.ready, "device": "cpu",
                "models": {"aligner": {"model_id": "FAKE-aligner", "revision": "fake-a"},
                           "phones": {"model_id": "FAKE-phones", "revision": "fake-p"}},
                "prosody_method": "pyworld-harvest", "calibration_version": self.calibration_version,
                "bands_enabled": self.bands_enabled}

    async def _enter(self, endpoint: str, **info):
        self.calls.append({"endpoint": endpoint, **info})
        if self.delay:
            await asyncio.sleep(self.delay)
        if endpoint in self.fail:
            raise WorkerError(self.fail[endpoint])

    @staticmethod
    def _words(text: str) -> list[dict]:
        tokens = [t for t in ("".join(c for c in w if c.isalnum() or c == "'") for w in text.split()) if t]
        return [{"i": i, "word": w, "start_ms": 100 + i * 400, "end_ms": 400 + i * 400} for i, w in enumerate(tokens)]

    async def align(self, pcm16: bytes, text: str, timeout_s: float) -> dict:
        await self._enter("align", bytes=len(pcm16), text=text, timeout_s=timeout_s)
        return {"words": self._words(text), "model_revision": "fake-a", "elapsed_ms": 1}

    async def prosody(self, pcm16: bytes, words, timeout_s: float) -> dict:
        await self._enter("prosody", bytes=len(pcm16), words=len(words or []), timeout_s=timeout_s)
        frames = len(pcm16) // 320
        return {"f0_hz": [150.0] * frames, "hop_ms": 10, "method": "pyworld-harvest", "elapsed_ms": 1,
                "per_word": [{"i": w["i"], "mean_f0": 150.0, "f0_range_st": 0.0,
                              "duration_ms": w["end_ms"] - w["start_ms"]} for w in words or []]}

    async def assess(self, pcm16: bytes, reference_text: str, mode: str, timeout_s: float) -> dict:
        await self._enter("assess", bytes=len(pcm16), text=reference_text, mode=mode, timeout_s=timeout_s)
        words = []
        for w in self._words(reference_text):
            spec = self.assessed.get(w["word"].lower(), {})
            words.append({**w, "phones": spec.get("phones", []), "word_gop": spec.get("word_gop"),
                          "band": spec.get("band", "good") if self.bands_enabled else None})
        return {"words": words, "calibration_version": self.calibration_version, "bands_enabled": self.bands_enabled,
                "model_revisions": {"aligner": "fake-a", "phones": "fake-p"}}


class FakeVadStream:
    """FAKE VAD: probability 1.0 for loud 512-sample windows, 0.0 for quiet ones."""

    def __init__(self):
        self.pending = np.zeros(0, dtype=np.int16)

    def reset(self):
        self.pending = np.zeros(0, dtype=np.int16)

    def process(self, pcm):
        buf = np.concatenate([self.pending, pcm])
        n = buf.shape[0] // 512
        out = []
        for i in range(n):
            w = buf[i * 512 : (i + 1) * 512]
            out.append((1.0 if np.abs(w.astype(np.int32)).mean() > 500 else 0.0, w))
        self.pending = buf[n * 512 :].copy()
        return out


class FakeVadModel:
    def stream(self):
        return FakeVadStream()


def fake_brain(record: dict):
    """Real vr_feedback text utilities + FAKE LLM-backed functions (feedback, goals, summary, hints)."""

    async def generate_feedback(llm, *, transcript, transcript_revision, source, context, max_items=3,
                                model_revision, prompt_revision="fb-v1"):
        record.setdefault("feedback_calls", []).append(
            {"transcript": transcript, "revision": transcript_revision, "evidence_type": context.get("evidence_type")})
        quote = " ".join(transcript.split()[:3])
        return {"status": "ok", "items": [{
            "feedback_id": f"fb_{transcript_revision}", "category": "grammar", "severity": "required",
            "status": "suggested", "evidence_type": context.get("evidence_type", "asr_text"), **source,
            "transcript_revision": transcript_revision, "evidence_quote": quote, "suggestion": "Yesterday I went",
            "explanation_ko": "과거형", "model_revision": model_revision, "prompt_revision": prompt_revision}]}

    async def session_summary(llm, *, turns, scenario, model_revision):
        record.setdefault("summary_calls", []).append(turns)
        t = turns[0]
        return {"status": "ok", "goals": [{"goal_id": "order", "status": "done", "evidence_turn_id": t["turn_id"]},
                                          {"goal_id": "option", "status": "pending"},
                                          {"goal_id": "price", "status": "pending"}],
                "items": [{"feedback_id": "fb_s1", "category": "expression", "severity": "optional",
                           "status": "suggested", "evidence_type": "asr_text", "source_turn_id": t["turn_id"],
                           "transcript_revision": 1, "evidence_quote": t["text"][:10], "suggestion": "Could I get a latte?",
                           "explanation_ko": "더 공손한 표현", "model_revision": model_revision, "prompt_revision": "fb-v1"}]}

    async def evaluate_goals(llm, scenario, turns):
        record.setdefault("goal_calls", []).append(len(turns))
        record.setdefault("goal_ids", []).append([g["goal_id"] for g in scenario["goals"]])
        await asyncio.sleep(record.get("goal_delay", 0))
        out = {"order": {"goal_id": "order", "status": "done", "evidence_turn_id": turns[0]["turn_id"]},
               "option": {"goal_id": "option", "status": "pending"}, "price": {"goal_id": "price", "status": "pending"}}
        return [out[g["goal_id"]] for g in scenario["goals"]]

    async def update_summary(llm, scenario, previous, turns):
        record.setdefault("summary_updates", []).append({"previous": previous, "turns": list(turns)})
        await asyncio.sleep(record.get("summary_delay", 0))
        n = len(record["summary_updates"])
        user = next(t["text"] for t in turns if t["role"] == "user")
        return {"summary": f"Summary {n} of {len(turns)} entries.",
                "facts": [{"name": "drink_order", "value": f"order v{n}", "evidence_quote": user.split()[0]}]}

    async def build_hint(scenario, difficulty, level, goals_state, last_ai_text, llm=None):
        return {"level": level, "text_ko": "음료를 주문해 보세요"}

    return dataclasses.replace(default_brain(), generate_feedback=generate_feedback, session_summary=session_summary,
                               evaluate_goals=evaluate_goals, build_hint=build_hint, update_summary=update_summary)


# ---------------------------------------------------------------- live server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Gateway:
    def __init__(self, tmp: Path, *, web_dist: Path | None = None, data_dir: Path | None = None, real_vad=False,
                 pron: FakePron | None = None, pron_lexicon: Path | None = None):
        self.port = free_port()
        scen_dir = tmp / "scenarios"
        scen_dir.mkdir(exist_ok=True)
        (scen_dir / "cafe_order.json").write_text(json.dumps(SCENARIO))
        self.config = Config(
            port=self.port, web_dist=web_dist or tmp / "no-dist", scenarios_dir=scen_dir,
            scenario_schema=tmp / "no-schema.json", data_dir=data_dir or tmp / "data", cache_dir=tmp / "cache",
            log_dir=tmp / "log", worker_token="test-token", pron_lexicon=pron_lexicon or tmp / "no-cmudict.dict",
        )
        self.asr, self.tts, self.llm, self.pron = FakeAsr(), FakeTts(), FakeLlm(), pron
        self.record: dict = {}
        vad_model = None if real_vad else FakeVadModel()
        self.svc = build_services(self.config, asr=self.asr, tts=self.tts, llm=self.llm,
                                  brain=fake_brain(self.record), vad_model=vad_model, pron=pron)
        self.app = create_app(self.config, services=self.svc, warm_cache=False)
        self.base = f"http://127.0.0.1:{self.port}"
        self.origin = self.base
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                                    ws_max_size=128 * 1024, access_log=False))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        self.http = httpx.Client(base_url=self.base, timeout=10)
        self.http.get("/")
        self.sid = self.http.cookies.get("vr_sid")
        self.csrf = self.http.get("/api/bootstrap").json()["csrf_token"]
        self.headers = {"Origin": self.origin, "X-VR-CSRF": self.csrf}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.http.request(method, path, headers=headers, **kw)

    def run(self, coro):
        """Run a coroutine on the server loop (for poking fakes/state safely)."""
        return asyncio.run_coroutine_threadsafe(coro, self.server_loop()).result(5)

    def server_loop(self):
        return self.svc.jobs._runner.get_loop()

    def wait_job(self, job_id: str, states=("completed", "failed", "cancelled", "expired"), timeout=10) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.call("GET", f"/api/jobs/{job_id}").json()
            if job["state"] in states:
                return job
            time.sleep(0.02)
        raise AssertionError(f"job stuck in {job['state']}")

    def stop(self):
        self.http.close()
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def gw(tmp_path):
    g = Gateway(tmp_path)
    yield g
    g.stop()


# ---------------------------------------------------------------- realtime client


class RtClient:
    """websockets client speaking the PROTOCOL §6.3 envelope."""

    def __init__(self, ws, session_id: str):
        self.ws = ws
        self.sid = session_id
        self.seq = 0
        self.events: list[dict] = []
        self.frames: list[tuple[dict, bytes]] = []
        self.log: list = []  # interleaved order of ("event", dict) / ("frame", header)

    async def event(self, type_: str, **fields):
        await self.ws.send(json.dumps({"type": type_, "event_id": f"c{time.time_ns()}", "session_id": self.sid,
                                       "epoch": 0, "event_seq": 0, **fields}))

    async def audio(self, pcm: np.ndarray, frame: int = 512, turn_id: str | None = None):
        for i in range(0, len(pcm), frame):
            chunk = pcm[i : i + frame]
            self.seq += 1
            header = {"v": 1, "kind": "input_audio", "session_id": self.sid, "turn_id": turn_id, "epoch": 0,
                      "seq": self.seq, "sample_rate": 16000, "sample_count": len(chunk)}
            await self.ws.send(pack_frame(header, chunk.tobytes()))
        await asyncio.sleep(0)

    async def raw(self, data: bytes):
        await self.ws.send(data)

    async def recv(self, timeout: float = 2.0):
        msg = await asyncio.wait_for(self.ws.recv(), timeout)
        if isinstance(msg, bytes):
            hlen = struct.unpack_from("<I", msg)[0]
            header = json.loads(msg[4 : 4 + hlen])
            self.frames.append((header, msg[4 + hlen :]))
            self.log.append(("frame", header))
            return ("frame", header)
        ev = json.loads(msg)
        self.events.append(ev)
        self.log.append(("event", ev))
        return ("event", ev)

    async def until(self, type_: str, timeout: float = 3.0, **match) -> dict:
        deadline = time.monotonic() + timeout
        for ev in self.events:
            if ev["type"] == type_ and all(ev.get(k) == v for k, v in match.items()) and not ev.get("_seen"):
                ev["_seen"] = True
                return ev
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no {type_} {match}; got {[e['type'] for e in self.events]}")
            kind, item = await self.recv(left)
            if kind == "event" and item["type"] == type_ and all(item.get(k) == v for k, v in match.items()):
                item["_seen"] = True
                return item

    async def drain(self, seconds: float):
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            try:
                await self.recv(left)
            except TimeoutError:
                return

    def mark_seen(self):
        for ev in self.events:
            ev["_seen"] = True

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


async def connect_rt(gw: Gateway, session_id: str, **kw) -> RtClient:
    from websockets.asyncio.client import connect

    headers = {"Origin": gw.origin, "Cookie": f"vr_sid={gw.sid}", **kw.pop("headers", {})}
    ws = await connect(f"ws://127.0.0.1:{gw.port}/api/sessions/{session_id}/realtime", additional_headers=headers,
                       max_size=2**22)
    return RtClient(ws, session_id)


def create_session(gw: Gateway, **body) -> dict:
    resp = gw.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order",
                                                   "difficulty": "normal", "history_opt_in": False, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()
