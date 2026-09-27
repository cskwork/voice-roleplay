"""Integration tests against the REAL Qwen3-ASR-0.6B model, served by `python -m asr_worker` in a subprocess.

Needs models/Qwen3-ASR-0.6B and generated fixtures (tests/fixtures/make_fixtures.sh).
Measured numbers are printed in the pytest summary (run with -rA or see the "ASR measurements" section).
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .helpers import fixture_pcm, fixture_text, wer

pytestmark = pytest.mark.model

WORKER_DIR = Path(__file__).resolve().parents[1]
METRICS: dict = {}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    token = secrets.token_hex(16)
    port = _free_port()
    log_path = tmp_path_factory.mktemp("asr") / "worker.log"
    env = {**os.environ, "VR_WORKER_TOKEN": token, "ASR_PORT": str(port)}
    started = time.monotonic()
    with open(log_path, "w") as log:
        proc = subprocess.Popen([sys.executable, "-m", "asr_worker"], cwd=WORKER_DIR, env=env, stdout=log, stderr=log)
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Worker-Token": token}
    try:
        deadline = time.monotonic() + 180
        while True:
            assert proc.poll() is None, log_path.read_text()
            try:
                if httpx.get(f"{base}/health", headers=headers).json()["ready"]:
                    break
            except httpx.TransportError:
                pass
            assert time.monotonic() < deadline, "worker not ready in 180 s"
            time.sleep(0.2)
        METRICS["process_start_to_ready_s"] = round(time.monotonic() - started, 2)
        yield {"base": base, "ws": f"ws://127.0.0.1:{port}/stream", "headers": headers, "proc": proc, "log": log_path}
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    METRICS["model_ready_log"] = [l.split(" asr ", 1)[1] for l in log_path.read_text().splitlines() if "model.ready" in l]


def transcribe(server, pcm: bytes, **params) -> httpx.Response:
    return httpx.post(
        f"{server['base']}/transcribe",
        params=params,
        content=pcm,
        headers={**server["headers"], "Content-Type": "application/octet-stream"},
        timeout=120,
    )


def test_health(server):
    h = httpx.get(f"{server['base']}/health", headers=server["headers"]).json()
    assert h["ready"] and h["backend"] == "transformers" and h["streaming_mode"] == "incremental_redecode"
    assert h["model_id"] == "Qwen/Qwen3-ASR-0.6B" and h["device"] in ("mps", "cpu")
    METRICS["device"] = h["device"]


@pytest.mark.parametrize("name", ["short_answer", "mid_pause", "price", "passage_30s"])
def test_transcribe_fixture_wer(server, name):
    pcm = fixture_pcm(name)
    transcribe(server, pcm)  # warm this input shape (MPS compiles per shape)
    r = transcribe(server, pcm, context="Ordering at a cafe and chatting about the weekend.")
    assert r.status_code == 200
    body = r.json()
    score = wer(fixture_text(name), body["text"])
    METRICS.setdefault("wer", {})[name] = round(score, 3)
    METRICS.setdefault("warm_full_decode_ms", {})[f"{name} ({body['audio_ms'] / 1000:.1f}s)"] = body["elapsed_ms"]
    assert score <= 0.15, body["text"]


def test_silence_is_empty(server):
    r = transcribe(server, fixture_pcm("silence_1s")).json()
    assert r["text"] == "" and r["audio_ms"] == 1000


def test_rejects_without_token_and_oversize(server):
    assert httpx.get(f"{server['base']}/health").status_code == 401
    r = httpx.post(f"{server['base']}/transcribe", content=b"\0\0", headers={"X-Worker-Token": "nope"})
    assert r.status_code == 401
    with pytest.raises(InvalidStatus) as exc:
        connect(server["ws"], additional_headers={"X-Worker-Token": "nope"}).close()
    assert exc.value.response.status_code == 401
    assert transcribe(server, b"\0" * (3_840_000 + 2)).status_code == 413


def stream_realtime(server, pcm: bytes, frame_ms: int = 20, cancel_after_s: float | None = None):
    """Send PCM at real-time pace in 20 ms frames; return (messages with receive times, t0, commit time)."""
    msgs: list[tuple[float, dict]] = []
    with connect(server["ws"], additional_headers=server["headers"], max_size=None) as ws:

        def receive():
            try:
                for raw in ws:
                    msgs.append((time.monotonic(), json.loads(raw)))
            except ConnectionClosed:
                pass

        reader = threading.Thread(target=receive)
        reader.start()
        step = 16000 * frame_ms // 1000 * 2
        t0 = time.monotonic()
        for k, i in enumerate(range(0, len(pcm), step)):
            if cancel_after_s is not None and k * frame_ms / 1000 >= cancel_after_s:
                ws.send(json.dumps({"type": "cancel"}))
                break
            ws.send(pcm[i : i + step])
            time.sleep(max(0.0, t0 + (k + 1) * frame_ms / 1000 - time.monotonic()))
        else:
            ws.send(json.dumps({"type": "commit"}))
        committed = time.monotonic()
        reader.join(timeout=120)
    return msgs, t0, committed


@pytest.mark.parametrize("name", ["mid_pause", "passage_30s"])
def test_stream_partials_then_final_equals_full_decode(server, name):
    # 1 s of trailing silence, as the gateway commits only after its end-of-turn silence.
    pcm = fixture_pcm(name) + b"\0" * 32000
    msgs, t0, committed = stream_realtime(server, pcm)
    kinds = [m["type"] for _, m in msgs]
    assert kinds[-1] == "final" and kinds.count("final") == 1 and "error" not in kinds
    partials = [(t, m) for t, m in msgs if m["type"] == "partial"]
    assert len(partials) >= 3, kinds
    duration_s = len(pcm) / 32000
    assert len(partials) <= duration_s / 0.7 + 1  # interval bound
    assert any(m["text"] for _, m in partials)

    final_t, final = msgs[-1]
    full = transcribe(server, pcm).json()["text"]
    assert final["text"] == full
    assert final["audio_ms"] == len(pcm) * 1000 // 32000

    lags = sorted(round((t - (t0 + m["audio_ms"] / 1000)) * 1000) for t, m in partials)
    METRICS.setdefault("stream", {})[name] = {
        "audio_s": round(duration_s, 2),
        "partials": len(partials),
        "partial_lag_ms_median": lags[len(lags) // 2],
        "partial_lag_ms_max": lags[-1],
        "commit_to_final_ms": round((final_t - committed) * 1000),
        "final_wer": round(wer(fixture_text(name), final["text"]), 3),
        "last_partial_wer": round(wer(fixture_text(name), partials[-1][1]["text"]), 3),
    }
    if duration_s > 12:
        # Window rule: late partials still cover the start of the utterance via the frozen prefix.
        first_words = " ".join(fixture_text(name).lower().split()[:3]).replace(",", "")
        assert partials[-1][1]["text"].lower().replace(",", "").startswith(first_words)


def test_stream_cancel(server):
    msgs, _, _ = stream_realtime(server, fixture_pcm("passage_30s"), cancel_after_s=2.0)
    assert all(m["type"] == "partial" for _, m in msgs)
    started = time.monotonic()
    assert transcribe(server, fixture_pcm("short_answer")).status_code == 200
    assert time.monotonic() - started < 5  # nothing from the cancelled stream is holding the model


def test_logs_contain_no_transcript_text(server):
    log = server["log"].read_text().lower()
    for phrase in ("grandmother", "latte", "seattle", "sandwich", "chatting about the weekend"):
        assert phrase not in log


def test_memory(server):
    proc = psutil.Process(server["proc"].pid)
    METRICS["worker_rss_mb"] = round(proc.memory_info().rss / 2**20)
