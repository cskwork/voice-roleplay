"""Real-model integration: gateway + real Silero VAD + real llama-server LLM (+ real ASR/TTS workers when available).

Run with:  VR_INTEGRATION=1 .venv/bin/python -m pytest tests/test_integration_real.py -s
Workers are started through the Supervisor with a fresh token. A worker whose port is already taken, or whose
environment is missing, is replaced by the FAKE from conftest.py and reported as such in the printed results.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time

import numpy as np
import pytest
from conftest import REPO, FakeAsr, FakeTts, connect_rt, silence

from vr_gateway.app import build_services, create_app
from vr_gateway.audio import parse_wav
from vr_gateway.config import load_config
from vr_gateway.supervisor import Supervisor

pytestmark = pytest.mark.skipif(os.environ.get("VR_INTEGRATION") != "1", reason="set VR_INTEGRATION=1")

FIX = REPO / "tests" / "fixtures" / "audio"
PORTS = {"asr": 8711, "tts": 8712, "llm": 8713}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="module")
def real_stack(tmp_path_factory):
    import uvicorn

    tmp = tmp_path_factory.mktemp("integration")
    config = load_config()
    config.worker_token = secrets.token_urlsafe(32)
    config.data_dir, config.cache_dir, config.log_dir, config.run_dir = tmp / "data", tmp / "cache", tmp / "log", tmp / "run"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        config.port = s.getsockname()[1]
    wanted = {"llm"}
    for name in ("asr", "tts"):
        spec = next((p for p in config.processes if p.name == name), None)
        if spec and os.path.exists(spec.cmd[0]):
            wanted.add(name)
    config.processes = [p for p in config.processes if p.name in wanted and port_free(PORTS[p.name])]
    sup = Supervisor(config)
    started_at = time.perf_counter()
    for name in list(sup.specs):
        try:
            sup.start(name)
        except FileNotFoundError:
            del sup.specs[name]
    ready = asyncio.run(sup.wait_ready(timeout_s=600))
    load_s = time.perf_counter() - started_at
    real = {n for n, ok in ready.items() if ok}
    external = set()
    if "llm" not in sup.specs and not port_free(PORTS["llm"]):
        import httpx  # a llama-server someone else started without an API key can be shared

        if httpx.get("http://127.0.0.1:8713/v1/models", timeout=2).status_code == 200:
            real.add("llm")
            external.add("llm")
    if "llm" not in real:
        sup.stop_all()
        pytest.skip(f"llama-server did not become ready: {ready}")

    kwargs = {}
    if "asr" not in real:
        kwargs["asr"] = FakeAsr()
    if "tts" not in real:
        kwargs["tts"] = FakeTts()
    svc = build_services(config, **kwargs)
    app = create_app(config, services=svc, warm_cache=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.port, log_level="warning"))
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    info = {"real": sorted(real), "fake": sorted({"asr", "tts", "llm"} - real), "external": sorted(external),
            "supervisor_ready_s": round(load_s, 1)}
    yield config, svc, info
    server.should_exit = True
    thread.join(10)
    sup.stop_all()


class Client:
    def __init__(self, config):
        import httpx

        self.base = f"http://127.0.0.1:{config.port}"
        self.http = httpx.Client(base_url=self.base, timeout=120)
        self.http.get("/")
        self.sid = self.http.cookies.get("vr_sid")
        csrf = self.http.get("/api/bootstrap").json()["csrf_token"]
        self.headers = {"Origin": self.base, "X-VR-CSRF": csrf}
        self.origin = self.base
        self.port = config.port

    def call(self, method, path, **kw):
        return self.http.request(method, path, headers={**self.headers, **kw.pop("headers", {})}, **kw)


def test_realtime_turn_real_models(real_stack):
    config, svc, info = real_stack
    c = Client(config)
    health = c.http.get("/api/health").json()
    session = c.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order",
                                                     "difficulty": "normal", "history_opt_in": False})
    assert session.status_code == 201, session.text
    sid = session.json()["session_id"]
    speech = parse_wav((FIX / "hello_16k.wav").read_bytes(), 120).pcm16k

    async def run():
        rt = await connect_rt(c, sid)
        await rt.event("session.start")
        opening = await rt.until("response.started", timeout=120)
        await rt.until("response.done", timeout=120)
        await rt.event("playback.completed", response_id=opening["response_id"], segment_id=0)
        await asyncio.sleep(1.0)  # let the prefix-cache warmup finish
        rt.mark_seen()
        audio = np.concatenate([silence(300), speech])
        t0 = time.perf_counter()
        marks: dict[str, float] = {}

        async def sender():
            for i in range(0, len(audio), 320):  # real-time pace, 20 ms frames
                await rt.audio(audio[i : i + 320], frame=320)
                await asyncio.sleep(0.02)
            marks["speech_end_sent"] = time.perf_counter() - t0
            for _ in range(100):  # 2 s trailing silence
                await rt.audio(silence(20), frame=320)
                await asyncio.sleep(0.02)

        send_task = asyncio.create_task(sender())
        texts = []
        while True:
            kind, item = await rt.recv(timeout=120)
            now = time.perf_counter() - t0
            if kind == "event":
                t = item["type"]
                if t == "asr.partial" and "first_partial" not in marks:
                    marks["first_partial"] = now
                elif t in ("speech.ended", "asr.final", "response.started") and t not in marks:
                    marks[t] = now
                    if t == "asr.final":
                        final_text = item["text"]
                elif t == "response.text":
                    marks.setdefault("first_response_text", now)
                    texts.append(item["text"])
                elif t == "response.done":
                    marks["response.done"] = now
                    break
                elif t == "error":
                    raise AssertionError(item)
            elif kind == "frame":
                marks.setdefault("first_output_audio", now)
        await send_task
        goal = await rt.until("goal.update", timeout=120)
        marks["goal.update"] = time.perf_counter() - t0
        await rt.ws.close()
        return marks, final_text, texts, goal

    marks, final_text, texts, goal = asyncio.run(run())
    t_end = time.perf_counter()
    summary = c.call("POST", f"/api/sessions/{sid}/end").json()
    summary_ms = round((time.perf_counter() - t_end) * 1000)
    reply = " ".join(texts)
    words = len(reply.split())
    base = marks["speech_end_sent"]
    report = {
        "stack": info,
        "device": "Apple M3 Pro 36GB, macOS arm64",
        "session_end_summary_ms": summary_ms,
        "summary_status": summary["summary"]["status"],
        "summary_items": len(summary["summary"]["items"]),
        "asr_final_words": len(final_text.split()),
        "reply_words": words,
        "reply_segments": len(texts),
        "ms_after_last_speech_sample": {k: round((v - base) * 1000) for k, v in marks.items() if k != "speech_end_sent"},
        "health_modes": {k: v["available"] for k, v in health["modes"].items()},
    }
    print("\nREALTIME " + json.dumps(report, indent=1))
    assert final_text.strip()
    assert 3 <= words <= 60
    assert goal["goals"]


def test_recorded_job_real_models(real_stack):
    config, svc, info = real_stack
    c = Client(config)
    aid = c.call("POST", "/api/attempts", json={"exercise_type": "free_answer", "scenario_id": "cafe_order",
                                                 "exercise_id": "cafe_order_free_1"}).json()
    if "attempt_id" not in aid:  # exercise ids come from content; take the first one
        sc = c.http.get("/api/scenarios/cafe_order").json()
        ex = sc["exercises"]["free_answer"][0]["exercise_id"]
        aid = c.call("POST", "/api/attempts", json={"exercise_type": "free_answer", "scenario_id": "cafe_order",
                                                     "exercise_id": ex}).json()
    attempt_id = aid["attempt_id"]
    wav = (FIX / "passage_30s.wav") if (FIX / "passage_30s.wav").exists() else FIX / "hello_16k.wav"
    assert c.call("PUT", f"/api/attempts/{attempt_id}/audio", content=wav.read_bytes()).status_code == 200
    t0 = time.perf_counter()
    job = c.call("POST", f"/api/attempts/{attempt_id}/submit", headers={"Idempotency-Key": "int-1"}).json()
    states = {}
    while True:
        state = c.http.get(f"/api/jobs/{job['job_id']}").json()["state"]
        states.setdefault(state, round((time.perf_counter() - t0) * 1000))
        if state in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(0.05)
    result = c.http.get(f"/api/attempts/{attempt_id}/result").json()
    report = {"stack": info, "input": wav.name, "audio_s": result["audio"]["duration_ms"] / 1000,
              "state_first_seen_ms": states, "feedback_status": result["feedback_status"],
              "feedback_items": len(result["feedback"]), "transcript_words": len((result["transcript"] or "").split()),
              "metrics": {k: result["metrics"][k] for k in ("wpm", "pause_count", "speech_span_s")} if result["metrics"] else None,
              "model_audio": [m["kind"] for m in result["model_audio"]]}
    print("\nRECORDED " + json.dumps(report, indent=1))
    assert states.get("completed") is not None, states
    assert result["transcript"]
    for item in result["feedback"]:
        assert item["evidence_quote"] in result["transcript"]
