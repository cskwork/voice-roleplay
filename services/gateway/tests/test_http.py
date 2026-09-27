"""Local auth, body caps, static serving, health/modes, settings and TTS endpoints (FAKE workers)."""

from __future__ import annotations

import asyncio
import tempfile

import httpx
from conftest import Gateway, create_session, silence, tone, wav_bytes
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus


def test_root_sets_cookie_and_reports_missing_build(gw):
    fresh = httpx.Client(base_url=gw.base)
    resp = fresh.get("/")
    assert resp.status_code == 200
    assert "웹 빌드가 없습니다" in resp.text
    cookie = resp.headers["set-cookie"]
    assert "vr_sid=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "default-src 'self'" in resp.headers["content-security-policy"]
    fresh.close()


def test_serves_web_dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    g = Gateway(tmp_path, web_dist=dist)
    try:
        assert g.http.get("/").text == "<html>app</html>"
        assert g.http.get("/assets/app.js").text == "console.log(1)"
        assert g.http.get("/practice/123").text == "<html>app</html>"  # SPA fallback
        assert g.http.get("/../../etc/passwd").text == "<html>app</html>"
    finally:
        g.stop()


def test_bootstrap_requires_cookie(gw):
    anon = httpx.Client(base_url=gw.base)
    resp = anon.get("/api/bootstrap")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_REQUIRED"
    assert anon.get("/api/settings").status_code == 401
    assert anon.get("/api/health").status_code == 200  # launcher probes health without a cookie
    anon.close()
    body = gw.http.get("/api/bootstrap").json()
    assert body == {"csrf_token": gw.csrf, "protocol_version": 1}


def test_mutations_need_csrf_and_origin(gw):
    url = "/api/settings"
    ok = gw.call("PUT", url, json={"difficulty": "easy"})
    assert ok.status_code == 200 and ok.json()["difficulty"] == "easy"

    no_csrf = gw.http.put(url, json={}, headers={"Origin": gw.origin})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "CSRF_INVALID"
    bad_csrf = gw.http.put(url, json={}, headers={"Origin": gw.origin, "X-VR-CSRF": "x" * 64})
    assert bad_csrf.json()["error"]["code"] == "CSRF_INVALID"
    no_origin = gw.http.put(url, json={}, headers={"X-VR-CSRF": gw.csrf})
    assert no_origin.status_code == 403 and no_origin.json()["error"]["code"] == "ORIGIN_DENIED"
    evil = gw.http.put(url, json={}, headers={"Origin": "http://evil.example", "X-VR-CSRF": gw.csrf})
    assert evil.json()["error"]["code"] == "ORIGIN_DENIED"
    # Cross-origin reads are refused too, and a foreign Host (DNS rebinding) is refused outright.
    assert gw.http.get("/api/history", headers={"Origin": "http://localhost:3000"}).status_code == 403
    assert gw.http.get("/api/health", headers={"Host": "attacker.example"}).status_code == 403
    assert gw.http.get("/api/health", headers={"Host": f"localhost:{gw.port}"}).status_code == 200


def test_websocket_requires_cookie_and_origin(gw):
    session = create_session(gw)

    async def attempt(headers):
        try:
            ws = await connect(f"ws://127.0.0.1:{gw.port}/api/sessions/{session['session_id']}/realtime",
                               additional_headers=headers)
        except InvalidStatus as exc:
            return exc.response.status_code
        await ws.close()
        return 101

    assert asyncio.run(attempt({"Origin": gw.origin})) == 403
    assert asyncio.run(attempt({"Origin": "http://evil.example", "Cookie": f"vr_sid={gw.sid}"})) == 403
    assert asyncio.run(attempt({"Cookie": f"vr_sid={gw.sid}"})) == 403
    assert asyncio.run(attempt({"Origin": gw.origin, "Cookie": f"vr_sid={gw.sid}"})) == 101


def test_oversize_json_body_rejected(gw):
    resp = gw.call("PUT", "/api/settings", content=b"{" + b" " * 300_000 + b"}",
                   headers={"Content-Type": "application/json"})
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "BODY_TOO_LARGE"

    def chunks():
        yield b"{"
        for _ in range(40):
            yield b" " * 10_000
        yield b"}"

    resp = gw.call("PUT", "/api/settings", content=chunks(), headers={"Content-Type": "application/json"})
    assert resp.status_code == 413


def _attempt(gw, **body) -> str:
    resp = gw.call("POST", "/api/attempts", json={"exercise_type": "free_answer", "scenario_id": "cafe_order",
                                                  "exercise_id": "cafe_order.free1", **body})
    assert resp.status_code == 201, resp.text
    return resp.json()["attempt_id"]


def test_oversize_wav_rejected_by_length_and_while_streaming(gw):
    aid = _attempt(gw)
    big = 32 * 1024 * 1024 + 1
    resp = gw.call("PUT", f"/api/attempts/{aid}/audio", content=b"\0" * big)
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "AUDIO_TOO_LARGE"

    def chunks():  # no Content-Length: the in-memory reader must stop at the cap
        for _ in range(33):
            yield b"\0" * (1024 * 1024)

    resp = gw.call("PUT", f"/api/attempts/{aid}/audio", content=chunks())
    assert resp.status_code == 413 and resp.json()["error"]["code"] == "AUDIO_TOO_LARGE"


def test_upload_never_spools_to_temp_files(gw, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("temporary file created during upload")

    for name in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, boom)
    aid = _attempt(gw)
    data = wav_bytes(tone(60_000, rate=48000), rate=48000)  # 60 s, ~5.8 MB
    resp = gw.call("PUT", f"/api/attempts/{aid}/audio", content=data)
    assert resp.status_code == 200, resp.text
    assert resp.json()["samples_16k"] == 60 * 16000


def test_wav_validation_over_http(gw):
    aid = _attempt(gw)
    stereo = tone(2000, rate=44100)
    interleaved = __import__("numpy").repeat(stereo, 2)
    resp = gw.call("PUT", f"/api/attempts/{aid}/audio", content=wav_bytes(interleaved, rate=44100, channels=2))
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_rate"] == 44100 and body["source_channels"] == 2 and body["samples_16k"] == 32000

    bad = gw.call("PUT", f"/api/attempts/{aid}/audio", content=b"RIFF....WAVEjunk")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "AUDIO_INVALID"
    too_long = gw.call("PUT", f"/api/attempts/{aid}/audio", content=wav_bytes(silence(121_000)))
    assert too_long.status_code == 413 and too_long.json()["error"]["code"] == "AUDIO_TOO_LONG"


def test_health_modes_follow_worker_readiness(gw):
    health = gw.http.get("/api/health").json()
    assert health["modes"]["realtime"]["available"] is True
    assert health["workers"]["vad"]["ready"] is True
    gw.llm.ready = False
    import time

    time.sleep(gw.config.health_cache_s + 0.1)
    health = gw.http.get("/api/health").json()
    assert health["modes"]["realtime"]["available"] is False
    assert "LLM" in health["modes"]["realtime"]["reason_ko"]
    assert health["modes"]["recorded"]["available"] is True
    resp = gw.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "MODEL_NOT_READY"


def test_session_validation_and_single_realtime(gw):
    bad = gw.call("POST", "/api/sessions", json={"mode": "video", "scenario_id": "cafe_order"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "UNSUPPORTED_MODE"
    missing = gw.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "nope"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"
    first = create_session(gw)
    assert first["silence_ms"] == 900 and first["realtime_url"].endswith("/realtime")
    second = gw.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"})
    assert second.status_code == 409 and second.json()["error"]["code"] == "LOCAL_BUSY"
    gw.call("POST", f"/api/sessions/{first['session_id']}/end")
    assert gw.call("POST", "/api/sessions", json={"mode": "realtime", "scenario_id": "cafe_order"}).status_code == 201


def test_settings_validation(gw):
    assert gw.call("PUT", "/api/settings", json={"bogus": 1}).json()["error"]["code"] == "INVALID_REQUEST"
    assert gw.call("PUT", "/api/settings", json={"silence_ms": 300}).status_code == 422
    assert gw.call("PUT", "/api/settings", content=b"not json").status_code == 422
    body = gw.call("PUT", "/api/settings", json={"silence_ms": 1100, "history_opt_in": True}).json()
    assert body["silence_ms"] == 1100 and body["history_opt_in"] is True and body["difficulty"] == "normal"
    assert gw.http.get("/api/settings").json()["silence_ms"] == 1100


def test_scenarios_listed(gw):
    body = gw.http.get("/api/scenarios").json()
    assert [s["scenario_id"] for s in body["scenarios"]] == ["cafe_order"]
    assert gw.http.get("/api/scenarios/cafe_order").json()["ai_role"] == "barista"
    assert gw.http.get("/api/scenarios/zzz").status_code == 404


def test_tts_endpoints(gw):
    resp = gw.call("POST", "/api/tts", json={"voice_id": "voice_a", "text": "It costs $4.50.", "speed": 1.0})
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"
    assert gw.tts.wav_calls[-1]["text"] != "It costs $4.50."  # normalized before synthesis
    korean = gw.call("POST", "/api/tts", json={"voice_id": "voice_a", "text": "안녕하세요", "speed": 1.0})
    assert korean.status_code == 422
    long = gw.call("POST", "/api/tts", json={"voice_id": "voice_a", "text": "a" * 401})
    assert long.status_code == 422
    traversal = gw.http.get("/api/tts/cached", params={"voice_id": "../x", "text_id": "cafe_order.opening"})
    assert traversal.status_code == 404

    calls = len(gw.tts.wav_calls)
    first = gw.http.get("/api/tts/cached", params={"voice_id": "voice_a", "text_id": "cafe_order.opening"})
    assert first.status_code == 200 and first.content[:4] == b"RIFF"
    again = gw.http.get("/api/tts/cached", params={"voice_id": "voice_a", "text_id": "cafe_order.opening"})
    assert again.content == first.content and len(gw.tts.wav_calls) == calls + 1
    assert list((gw.config.cache_dir / "tts" / "voice_a").glob("cafe_order.opening__1.00__*.wav"))
    unknown = gw.http.get("/api/tts/cached", params={"voice_id": "voice_a", "text_id": "free text please"})
    assert unknown.status_code == 404


def test_unknown_api_route_uses_error_envelope(gw):
    resp = gw.http.get("/api/nope")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"
