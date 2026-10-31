"""API mode, shared mode and reverse-proxy support (FAKE workers, see conftest.py; no network, no keys).

- config: VR_MODE / VR_SHARED / VR_PUBLIC_ORIGINS / VR_PATH_PREFIX parsing, keys never in repr, no worker processes;
- /api/health keeps its shape and adds mode, shared, api_keys (booleans), sessions;
- shared mode: per-learner sessions (vr_sid), the concurrency cap, the session time limit, history and review off;
- proxy: "/voice/..." routed like "/...", the public Host/Origin accepted over HTTP and WebSocket, cookie path;
- the ./app tool's API-mode doctor/preflight (keys reported as set/missing, never printed).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import httpx
import pytest
from conftest import REPO, Gateway, connect_rt, create_session, silence
from test_jobs import new_attempt
from test_realtime import speak_turn

from vr_gateway.app import build_services
from vr_gateway.config import Config, load_config, normalize_origin, normalize_prefix

PUBLIC = "https://content-forge.example"


class Learner:
    """A second browser on the same gateway: its own vr_sid cookie and CSRF token."""

    def __init__(self, gw: Gateway, origin: str | None = None, host: str | None = None, prefix: str = ""):
        self.gw, self.port, self.prefix = gw, gw.port, prefix
        self.origin = origin or gw.origin
        extra = {"Host": host} if host else {}
        self.http = httpx.Client(base_url=gw.base, timeout=10, headers=extra)
        self.http.get(f"{prefix}/")
        self.sid = self.http.cookies.get("vr_sid")
        self.csrf = self.http.get(f"{prefix}/api/bootstrap").json()["csrf_token"]
        self.headers = {"Origin": self.origin, "X-VR-CSRF": self.csrf}

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.http.request(method, self.prefix + path, headers=headers, **kw)

    def close(self):
        self.http.close()


@pytest.fixture
def shared(tmp_path):
    g = Gateway(tmp_path, shared=True, max_sessions=2, public_origins=[PUBLIC], path_prefix="/voice")
    yield g
    g.stop()


# ---------------------------------------------------------------- config


def test_config_env_overrides(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text((REPO / "services/gateway/config.toml").read_text())
    monkeypatch.setenv("VR_MODE", "api")
    monkeypatch.setenv("VR_SHARED", "1")
    monkeypatch.setenv("VR_MAX_SESSIONS", "30")
    monkeypatch.setenv("VR_PUBLIC_ORIGINS", "https://Content-Forge.example/, http://localhost:5173")
    monkeypatch.setenv("VR_PATH_PREFIX", "/voice/")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret-value")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret-value")
    cfg = load_config(cfg_file)
    assert cfg.mode == "api" and cfg.shared and cfg.max_sessions == 30 and cfg.max_session_s == 1800
    assert cfg.processes == [] and cfg.pron_url == ""  # no local workers; pronunciation off by default
    assert cfg.llm_model_revision == "openrouter:openai/gpt-4.1-mini"
    assert cfg.api.asr_model == "gpt-live-transcribe" and cfg.api.llm_providers == ["openai", "azure"]
    assert cfg.public_origins == ["https://content-forge.example", "http://localhost:5173"]
    assert cfg.path_prefix == "/voice"
    assert {"content-forge.example", "localhost:5173"} <= cfg.allowed_hosts
    assert "https://content-forge.example" in cfg.allowed_origins
    assert cfg.api.key_presence() == {"openrouter": True, "openai": False, "deepgram": True}
    assert "secret" not in repr(cfg) and cfg.history_enabled is False
    assert cfg.job_concurrency == 4 and cfg.job_queue_capacity >= 30


def test_config_defaults_stay_local(monkeypatch):
    for var in ("VR_MODE", "VR_SHARED", "VR_PUBLIC_ORIGINS", "VR_PATH_PREFIX"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg.mode == "local" and not cfg.shared and cfg.path_prefix == "" and cfg.public_origins == []
    assert {p.name for p in cfg.processes} >= {"asr", "tts", "llm"} or not (REPO / "config/llm/server.json").exists()
    assert cfg.allowed_hosts == {"127.0.0.1:8710", "localhost:8710"} or os.environ.get("VR_GATEWAY_PORT")


def test_config_rejects_bad_values():
    assert normalize_prefix("") == "" and normalize_prefix("/") == "" and normalize_prefix("voice") == "/voice"
    for bad in ("/voice/../x", "/vo ice", "/a?b"):
        with pytest.raises(ValueError):
            normalize_prefix(bad)
    for bad in ("content-forge.example", "ftp://x", "https://x/path"):
        with pytest.raises(ValueError):
            normalize_origin(bad)


def test_api_mode_builds_adapters_without_network(tmp_path, monkeypatch):
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config(mode="api", data_dir=tmp_path / "data", cache_dir=tmp_path / "cache", scenarios_dir=tmp_path,
                 scenario_schema=tmp_path / "none.json")
    svc = build_services(cfg)
    from vr_feedback.llm import LlmClient

    from vr_gateway.adapters.asr_openai import OpenAiAsr
    from vr_gateway.adapters.tts_deepgram import DeepgramTts

    assert isinstance(svc.asr, OpenAiAsr) and isinstance(svc.tts, DeepgramTts)
    assert isinstance(svc.llm, LlmClient) and svc.llm.remote and svc.llm_bg is svc.llm
    assert svc.pron is None and str(svc.tts_cache.dir).endswith("tts-api")
    svc.db.close()


# ---------------------------------------------------------------- health


def test_health_api_mode_shape_and_key_presence(tmp_path):
    g = Gateway(tmp_path, mode="api")
    try:
        g.svc.config.api.keys = {"openrouter": "x", "openai": "", "deepgram": "y"}
        body = g.http.get("/api/health").json()
        assert {"gateway", "workers", "modes", "benchmark", "pronunciation_assessment", "tts_cache"} <= set(body)
        assert body["mode"] == "api" and body["shared"] is False
        assert body["api_keys"] == {"openrouter": True, "openai": False, "deepgram": True}
        assert body["workers"]["asr"]["ready"] and body["modes"]["realtime"]["available"]  # fakes injected
    finally:
        g.stop()


def test_health_local_mode_has_no_api_fields(gw):
    body = gw.http.get("/api/health").json()
    assert body["mode"] == "local" and body["shared"] is False and "api_keys" not in body and "sessions" not in body


def test_health_names_missing_keys_in_api_mode(tmp_path):
    g = Gateway(tmp_path, mode="api")
    try:
        g.asr.ready = False
        body = g.http.get("/api/health").json()
        assert body["modes"]["realtime"]["available"] is False
        assert body["modes"]["realtime"]["reason_ko"] == "API 키가 설정되지 않음: OPENAI_API_KEY"
    finally:
        g.stop()


# ---------------------------------------------------------------- reverse proxy


def test_prefix_routes_and_public_origin(shared):
    fresh = httpx.Client(base_url=shared.base, headers={"Host": "content-forge.example"})
    try:
        page = fresh.get("/voice/")
        assert page.status_code == 200
        cookie = page.headers["set-cookie"]
        assert "Path=/voice/" in cookie and "SameSite=Strict" in cookie and "HttpOnly" in cookie
        assert "wss://content-forge.example" in page.headers["content-security-policy"]
        redirect = fresh.get("/voice", follow_redirects=False)
        assert redirect.status_code == 308 and redirect.headers["location"] == "/voice/"
        health = fresh.get("/voice/api/health")
        assert health.status_code == 200 and health.json()["mode"] == "local" and health.json()["shared"] is True
        assert fresh.get("/voice/api/bootstrap").status_code == 200
        # Unknown hosts and origins are still refused.
        assert httpx.get(f"{shared.base}/voice/api/health", headers={"Host": "evil.example"}).status_code == 403
    finally:
        fresh.close()
    learner = Learner(shared, origin=PUBLIC, host="content-forge.example", prefix="/voice")
    try:
        assert learner.call("PUT", "/api/settings", json={"slow": True}).json()["slow"] is True
        denied = learner.call("PUT", "/api/settings", json={"slow": True}, headers={"Origin": "https://evil.example"})
        assert denied.status_code == 403
    finally:
        learner.close()


async def test_realtime_websocket_under_prefix_with_public_origin(shared):
    learner = await asyncio.to_thread(Learner, shared, PUBLIC, "content-forge.example", "/voice")
    try:
        session = await asyncio.to_thread(create_session, learner)
        assert session["realtime_url"] == f"/api/sessions/{session['session_id']}/realtime"
        from websockets.asyncio.client import connect

        ws = await connect(f"ws://127.0.0.1:{shared.port}/voice/api/sessions/{session['session_id']}/realtime",
                           additional_headers={"Origin": PUBLIC, "Cookie": f"vr_sid={learner.sid}",
                                               "Host": "content-forge.example"})
        from conftest import RtClient

        rt = RtClient(ws, session["session_id"])
        await rt.event("session.start")
        await rt.until("response.done")
        await ws.close()
    finally:
        learner.close()


# ---------------------------------------------------------------- shared mode


async def test_two_learners_do_not_end_each_other(shared):
    a = await asyncio.to_thread(Learner, shared)
    b = await asyncio.to_thread(Learner, shared)
    try:
        sa = await asyncio.to_thread(create_session, a)
        rta = await connect_rt(a, sa["session_id"])
        await rta.event("session.start")
        await rta.until("response.done")
        sb = await asyncio.to_thread(create_session, b)
        rtb = await connect_rt(b, sb["session_id"])
        await rtb.event("session.start")
        await rtb.until("response.done")
        # B starting did not supersede A: A still talks.
        assert (await asyncio.to_thread(a.call, "GET", f"/api/sessions/{sa['session_id']}")).json()["state"] != "ended"
        await speak_turn(rta)
        await rta.until("asr.final", timeout=5)
        # Recorded practice by B ends B's own realtime session (PRD §7) and leaves A's alone.
        await asyncio.to_thread(new_attempt, b)
        assert (await asyncio.to_thread(b.call, "GET", f"/api/sessions/{sb['session_id']}")).json()["state"] == "ended"
        assert (await asyncio.to_thread(a.call, "GET", f"/api/sessions/{sa['session_id']}")).json()["state"] != "ended"
        # Learners cannot see or end each other's sessions, attempts or jobs.
        assert (await asyncio.to_thread(b.call, "GET", f"/api/sessions/{sa['session_id']}")).status_code == 404
        assert (await asyncio.to_thread(b.call, "POST", f"/api/sessions/{sa['session_id']}/end")).status_code == 404
        aid = await asyncio.to_thread(new_attempt, a)
        assert (await asyncio.to_thread(b.call, "GET", f"/api/attempts/{aid}/result")).status_code == 404
        # B's socket cannot attach to A's session (refused like an unknown session).
        from websockets.exceptions import InvalidStatus

        with pytest.raises(InvalidStatus):
            await connect_rt(b, sa["session_id"])
        # A new session by A supersedes only A's own previous one.
        sb2 = await asyncio.to_thread(create_session, b)
        sa2 = await asyncio.to_thread(create_session, a)
        assert (await asyncio.to_thread(a.call, "GET", f"/api/sessions/{sa['session_id']}")).json()["state"] == "ended"
        assert (await asyncio.to_thread(b.call, "GET", f"/api/sessions/{sb2['session_id']}")).json()["state"] != "ended"
        assert sa2["session_id"] != sa["session_id"]
        await rtb.ws.close()
    finally:
        a.close()
        b.close()


def test_capacity_cap_is_enforced(shared):
    a, b, c = Learner(shared), Learner(shared), Learner(shared)
    try:
        create_session(a)
        create_session(b)
        refused = c.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"})
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "CAPACITY_FULL"
        assert shared.http.get("/api/health").json()["sessions"] == {"live": 2, "max": 2, "max_session_s": 1800.0}
        # A learner at the cap can still restart their own session (theirs is ended first).
        assert a.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"}).status_code == 201
        # Turn-based practice is not a live realtime session and is not capped.
        assert c.call("POST", "/api/sessions", json={"mode": "turn_based", "scenario_id": "cafe_order"}).status_code == 201
    finally:
        for learner in (a, b, c):
            learner.close()


def test_history_and_review_are_off(shared):
    a, b = Learner(shared), Learner(shared)
    try:
        assert a.call("PUT", "/api/settings", json={"history_opt_in": True, "voice_id": "voice_a"}).json()[
            "history_opt_in"] is False
        assert a.call("GET", "/api/settings").json()["voice_id"] == "voice_a"
        assert b.call("GET", "/api/settings").json()["voice_id"] is None  # settings are per learner
        session = create_session(a, history_opt_in=True)
        assert session["history_opt_in"] is False
        aid = new_attempt(a, history_opt_in=True)
        assert shared.svc.jobs.attempts[aid].history_opt_in is False
        a.call("POST", f"/api/sessions/{session['session_id']}/end")
        assert a.call("GET", "/api/history").json() == {"sessions": [], "attempts": []}
        assert a.call("GET", "/api/review/due").json() == {"items": []}
        review = a.call("POST", "/api/review", json={"text_en": "Could I get a latte?"})
        assert review.status_code == 409
        assert shared.svc.db.get_settings() == {}  # nothing about learners in SQLite
    finally:
        a.close()
        b.close()


async def test_session_time_limit(tmp_path):
    g = await asyncio.to_thread(Gateway, tmp_path, shared=True, max_session_s=0.5)
    try:
        session = await asyncio.to_thread(create_session, g)
        rt = await connect_rt(g, session["session_id"])
        await rt.event("session.start")
        await rt.until("response.done")
        await rt.audio(silence(100))
        deadline = time.monotonic() + 5
        while (await asyncio.to_thread(g.call, "GET", f"/api/sessions/{session['session_id']}")).json()["state"] != "ended":
            assert time.monotonic() < deadline, "session did not end at the time limit"
            await asyncio.sleep(0.05)
        with pytest.raises(Exception):
            for _ in range(50):
                await rt.recv(1)
        assert rt.ws.close_code == 4000 and rt.ws.close_reason == "session_time_limit"
    finally:
        await asyncio.to_thread(g.stop)


def test_local_mode_still_supersedes_across_browsers(gw):
    """Single-learner mode is unchanged: a second browser's session ends the first (PRD §7)."""
    other = Learner(gw)
    try:
        first = create_session(gw)
        create_session(other)
        assert gw.call("GET", f"/api/sessions/{first['session_id']}").json()["state"] == "ended"
    finally:
        other.close()


# ---------------------------------------------------------------- ./app tool


def run_tool(*args, env_extra: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY")}
    env.update(env_extra)
    return subprocess.run([sys.executable, str(REPO / "scripts/app_tool.py"), *args], capture_output=True, text=True,
                          env=env, timeout=60)


def test_doctor_api_reports_keys_without_printing_them():
    out = run_tool("doctor", "--api", env_extra={"OPENROUTER_API_KEY": "or-SECRET-123", "OPENAI_API_KEY": "sk-SECRET-456"})
    text = out.stdout + out.stderr
    assert "SECRET" not in text
    assert "key OPENROUTER_API_KEY" in text and "set (value not shown)" in text
    assert "key DEEPGRAM_API_KEY" in text and "missing" in text
    assert "model silero-vad" in text and "sha256 ok" in text
    assert out.returncode == 1  # a key is missing
    pre = run_tool("preflight", "--api", env_extra={})
    assert pre.returncode == 1 and "OPENROUTER_API_KEY, OPENAI_API_KEY, DEEPGRAM_API_KEY" in pre.stdout
    assert run_tool("mode", env_extra={"VR_MODE": "api"}).stdout.strip() == "api"


def test_tts_audio_for_shared_learners_uses_their_voice(shared):
    a = Learner(shared)
    try:
        a.call("PUT", "/api/settings", json={"voice_id": "voice_a"})
        session = create_session(a)
        assert session["voice_id"] == "voice_a"
    finally:
        a.close()




def test_benchmark_exits_cleanly_without_keys():
    env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY")}
    out = subprocess.run([sys.executable, str(REPO / "benchmarks/api/bench_api.py")], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 2 and "No API keys set" in out.stderr and "no network calls made" in out.stderr
    sys.path.insert(0, str(REPO / "benchmarks/api"))
    from bench_api import wer

    assert wer("Can I get a latte, please?", "can I get a latte please") == 0.0
    assert wer("one two three four", "one three four five") == 0.5


def test_abandoned_sessions_free_capacity(shared):
    a, b, c = Learner(shared), Learner(shared), Learner(shared)
    try:
        create_session(a)
        create_session(b)
        # Neither ever connected: once past the idle grace they no longer hold a place.
        shared.svc.config.realtime_idle_grace_s = 0.0
        assert c.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"}).status_code == 201
    finally:
        for learner in (a, b, c):
            learner.close()
