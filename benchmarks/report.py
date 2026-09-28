"""Turn a raw browser benchmark run (tests/e2e/bench/fullstack.spec.ts) into benchmarks/results/<name>.{json,md}.

Adds the machine, OS, browser, model revisions + file hashes (models.lock.json, verified by `./app doctor`), runtime
versions and the TTS worker's per-request log numbers (RTF, first chunk). Standard library only.

    python benchmarks/report.py RAW_JSON OUT_BASENAME
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import app_tool  # noqa: E402  (stdlib helpers of ./app)

# PRD §15.1 targets. (key, label, statistic, limit, unit)
TARGETS = [
    ("last_voiced_to_first_played_ms", "실시간 응답 시작 (마지막 음성 샘플 → AI 첫 음성 재생)", "p50", 2000, "ms"),
    ("last_voiced_to_first_played_ms", "실시간 응답 시작", "p95", 4000, "ms"),
    ("speech_start_to_first_partial_ms", "첫 임시 전사 (발화 시작 → 첫 임시 자막)", "p95", 3000, "ms"),
    ("speech_start_to_local_stop_ms", "자동 끼어들기 (발화 시작 → AI 음성 정지)", "p95", 350, "ms"),
    ("tts_rtf_per_response", "연속 TTS 생성 RTF (응답별)", "p95", 0.8, ""),
    ("rec30_submit_to_transcript_ms", "녹음형 전사 30 s (제출 → 전사, 서버 기준)", "p95", 15000, "ms"),
    ("rec30_submit_to_full_result_ms", "녹음형 전체 결과 30 s", "p95", 25000, "ms"),
    ("rec120_submit_to_full_result_ms", "녹음형 전체 결과 120 s", "p95", 60000, "ms"),
]


def pct(values: list[float], q: float) -> float | None:
    """Percentile with linear interpolation between closest ranks (numpy's default)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(values: list) -> dict:
    xs = [v for v in values if v is not None]
    return {"n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "min": min(xs) if xs else None,
            "max": max(xs) if xs else None, "missing": len(values) - len(xs)}


def tts_requests(response_ids: set[str]) -> dict[str, list[dict]]:
    """synthesize_ws log lines of the TTS worker, grouped by response id (rid = <response_id>-<segment>)."""
    out: dict[str, list[dict]] = {}
    log = ROOT / "var/log/tts.log"
    pat = re.compile(r"synthesize_ws rid=(\S+)-(\d+) .*audio_ms=(\d+) first_chunk_ms=(\d+) elapsed_ms=(\d+)")
    for line in log.read_text(errors="ignore").splitlines() if log.exists() else []:
        m = pat.search(line)
        if m and m.group(1) in response_ids:
            out.setdefault(m.group(1), []).append({"segment": int(m.group(2)), "audio_ms": int(m.group(3)),
                                                   "first_chunk_ms": int(m.group(4)), "elapsed_ms": int(m.group(5))})
    return out


def tts_config() -> str:
    log = ROOT / "var/log/tts.log"
    lines = [ln for ln in log.read_text(errors="ignore").splitlines() if "model_loaded" in ln] if log.exists() else []
    return lines[-1].split("model_loaded", 1)[1].strip() if lines else "unknown"


def environment(raw: dict) -> dict:
    lock = json.loads((ROOT / "models.lock.json").read_text())
    models = []
    for m in lock["models"]:
        if not m.get("files") or not m.get("source"):
            continue
        big = max([f for f in m["files"] if f.get("required", True)] or m["files"], key=lambda f: f["bytes"])
        models.append({"id": m["id"], "repo": m["source"]["repo"], "revision": m["source"]["revision"],
                       "largest_file": big["path"], "sha256": big["sha256"]})
    try:
        with urllib.request.urlopen("http://127.0.0.1:8710/api/health", timeout=5) as r:
            health = json.load(r)
    except OSError:
        health = {}
    workers = health.get("workers", {})
    llama = app_tool.run(["llama-server", "--version"])
    return {
        "machine": app_tool.run(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "memory_gb": round(int(app_tool.run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30),
        "os": f"macOS {platform.mac_ver()[0]} {platform.machine()}",
        "gpu": "Apple GPU (unified memory), Metal; no NVIDIA",
        "browser": raw.get("browser"),
        "active_workers": {k: {x: workers.get(k, {}).get(x) for x in ("model_id", "revision", "device", "backend")}
                           for k in ("asr", "tts")},
        "llm": workers.get("llm", {}).get("model_revision"),
        "tts_engine": tts_config(),
        "models_lock": models,
        "runtimes": {
            "llama-server": next((ln for ln in llama.splitlines() if ln.startswith("version")), ""),
            "gateway": app_tool.pkg_versions("gateway", ["onnxruntime", "fastapi", "uvicorn"]),
            "asr": app_tool.pkg_versions("asr", ["torch", "transformers", "qwen-asr"]),
            "tts": app_tool.pkg_versions("tts", ["mlx", "mlx-audio-plus", "torch", "onnxruntime"]),
        },
        "voice": "dev_voice_a (development voice, see content/voices)",
        "audio_devices": "input: FAKE-MIC-SOURCE (fixture WAVs via MediaStream, 48 kHz context); output: Chromium --mute-audio",
        "llm_context": "llama-server -c 8192 -np 2 (4096 tokens per slot), Q4_K_M, all layers on Metal",
    }


def fmt(v, unit: str) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}" if unit == "" else f"{v:,.0f} {unit}"


def main(raw_path: str, out_base: str) -> None:
    raw = json.loads(Path(raw_path).read_text())
    turns = raw["turns"]
    rids = {t["response_id"] for t in turns}
    tts = tts_requests(rids)
    per_response_rtf, per_request_rtf, first_chunk = [], [], []
    for reqs in tts.values():
        audio = sum(r["audio_ms"] for r in reqs)
        if audio:
            per_response_rtf.append(sum(r["elapsed_ms"] for r in reqs) / audio)
        for r in reqs:
            if r["audio_ms"]:
                per_request_rtf.append(r["elapsed_ms"] / r["audio_ms"])
            first_chunk.append(r["first_chunk_ms"])
    rec = raw["recorded"]
    metrics = {
        "last_voiced_to_first_played_ms": stats([t["last_voiced_to_first_played_ms"] for t in turns]),
        "speech_start_to_first_partial_ms": stats([t["speech_start_to_first_partial_ms"] for t in turns]),
        "last_voiced_to_final_ms": stats([t["last_voiced_to_final_ms"] for t in turns]),
        "response_started_to_first_frame_ms": stats([t["response_started_to_first_frame_ms"] for t in turns]),
        "first_frame_to_played_ms": stats([t["first_frame_to_played_ms"] for t in turns]),
        "reply_words": stats([t["words"] for t in turns]),
        "stall_ms_per_reply": stats([t["stall_ms"] for t in turns if not t.get("interrupted")]),
        "segment_gap_ms": stats([g for t in turns for g in t["segment_gaps_ms"]]),
        "speech_start_to_local_stop_ms": stats([b["speech_start_to_local_stop_ms"] for b in raw["barge_ins"]]),
        "speech_start_to_server_cancel_ms": stats([b["speech_start_to_server_cancel_ms"] for b in raw["barge_ins"]]),
        "post_barge_last_voiced_to_first_played_ms": stats([b["last_voiced_to_first_played_ms"] for b in raw["post_barge_turns"]]),
        "tts_rtf_per_response": stats(per_response_rtf),
        "tts_rtf_per_request": stats(per_request_rtf),
        "tts_first_chunk_ms": stats(first_chunk),
        "rec30_submit_to_transcript_ms": stats([r["submit_to_transcript_ms"] for r in rec if r["input_s"] == 30]),
        "rec30_submit_to_full_result_ms": stats([r["submit_to_full_result_ms"] for r in rec if r["input_s"] == 30]),
        "rec120_submit_to_transcript_ms": stats([r["submit_to_transcript_ms"] for r in rec if r["input_s"] == 120]),
        "rec120_submit_to_full_result_ms": stats([r["submit_to_full_result_ms"] for r in rec if r["input_s"] == 120]),
    }
    stale = sum(b["stale_playback_after_stop"] for b in raw["barge_ins"])
    stalls = [t["stall_ms"] for t in turns if t.get("stall_ms") is not None]
    replies_with_stall = sum(1 for s in stalls if s > 50)
    no_partial = sum(1 for t in turns if t["speech_start_to_first_partial_ms"] is None)
    mem = {}
    first, last = raw["memory"][0]["rss"], raw["memory"][-1]["rss"]
    for pid, name in raw["pids"].items():
        peak = max((m["rss"].get(pid, 0) for m in raw["memory"]), default=0)
        mem[name] = {"start_mb": first.get(pid), "end_mb": last.get(pid), "peak_mb": peak}

    verdicts = []
    for key, label, stat, limit, unit in TARGETS:
        value = metrics[key][stat]
        verdicts.append({"metric": key, "label": label, "stat": stat, "target": limit, "unit": unit, "value": value,
                         "n": metrics[key]["n"], "met": None if value is None else value <= limit})
    env = environment(raw)
    result = {"summary": verdicts, "metrics": metrics, "stale_playback_after_barge_in": stale,
              "replies_with_stall_over_50ms": replies_with_stall, "turns_without_partial": no_partial,
              "memory_rss_mb": mem, "environment": env, "raw": raw}
    out_json, out_md = Path(out_base + ".json"), Path(out_base + ".md")
    out_json.write_text(json.dumps(result, indent=1, ensure_ascii=False))

    cfg = raw["config"]
    lines = [
        f"# Full-stack benchmark — {env['machine']} ({raw['started_at'][:10]})",
        "",
        f"Run `{raw['started_at']}` → `{raw['finished_at']}` with `benchmarks/run.sh`. Raw data and every per-turn number: "
        f"`{out_json.name}`.",
        "",
        f"- Realtime: {cfg['turns']} warm turns through Chromium (headless, audio muted) in sessions of {cfg['session_turns']}, "
        f"cafe_order scenario, normal difficulty (end-of-turn silence 900 ms), plus one unmeasured warm-up turn. "
        f"**PRD §15.3 asks for ≥ 200 turns; this run has {cfg['turns']}** (`benchmarks/run.sh --turns 200` for the full count).",
        f"- One reply in every {cfg['barge_every']} is interrupted 600 ms into its playback by another utterance "
        f"({len(raw['barge_ins'])} barge-ins). Interrupted turns still count for response start; the reply to the "
        "interrupting utterance is reported separately (`post_barge_*`).",
        f"- Recorded practice: free answer (ASR + LLM feedback + model audio), 30 s × {cfg['rec30']}, 120 s × {cfg['rec120']}, one job at a time.",
        "- Learner audio: FAKE-MIC-SOURCE — macOS `say` (Samantha) fixtures scheduled into a 48 kHz AudioContext and handed to the app "
        "as its microphone MediaStream, so the app's capture worklet, resampler, WebSocket envelope, gateway VAD, ASR, LLM, TTS and "
        "playback worklet are all real. Clean synthetic speech, not learner speech: ASR/turn-taking numbers are best case.",
        "- Clock: browser `performance.now()` for all realtime numbers. \"Played\" = the app's playback AudioWorklet reports the first "
        f"rendered sample; the context's reported base+output latency ({fmt(raw['browser'].get('output_latency_ms'), 'ms')}) comes on top "
        "at the speaker and is **not** included. No loopback recording was made.",
        "- The machine was not otherwise idle (desktop apps, other agents); see the TTS README for how much that moves RTF.",
        "",
        "## PRD §15.1 targets",
        "",
        "| 지표 | 목표 | 측정 | n | 판정 |",
        "|---|---|---|---|---|",
    ]
    for v in verdicts:
        verdict = "no data" if v["met"] is None else ("met" if v["met"] else "**UNMET**")
        lines.append(f"| {v['label']} | {v['stat']} ≤ {fmt(v['target'], v['unit'])} | {v['stat']} {fmt(v['value'], v['unit'])} | {v['n']} | {verdict} |")
    lines += [
        "",
        "Not measured here: stop-button latency (PRD p95 ≤ 150 ms), 60-minute stability, memory growth over 10 sessions, WER on a learner set.",
        "",
        "## Details (p50 / p95, n)",
        "",
        "| Metric | p50 | p95 | min | max | n |",
        "|---|---|---|---|---|---|",
    ]
    for key, s in metrics.items():
        unit = "" if "rtf" in key or key == "reply_words" else "ms"
        lines.append(f"| `{key}` | {fmt(s['p50'], unit)} | {fmt(s['p95'], unit)} | {fmt(s['min'], unit)} | {fmt(s['max'], unit)} | {s['n']} |")
    lines += [
        "",
        f"- Stale audio played after a barge-in stop: {stale} (over {len(raw['barge_ins'])} barge-ins).",
        f"- Replies with more than 50 ms of silence inside their playback (TTS underrun): {replies_with_stall} of {len(stalls)}.",
        f"- Turns whose final transcript arrived before any partial caption: {no_partial} of {len(turns)} (short utterances; partials are "
        "re-decoded every 700 ms).",
        "- `response_started_to_first_frame_ms` = LLM time to the first segment + TTS first chunk (+ WebSocket); "
        "`last_voiced_to_final_ms` includes the 900 ms end-of-turn silence.",
        "",
        "## Memory (RSS)",
        "",
        "| Process | start | end | peak (5 s samples) |",
        "|---|---|---|---|",
    ]
    for name, m in mem.items():
        lines.append(f"| {name} | {fmt(m['start_mb'], 'MB')} | {fmt(m['end_mb'], 'MB')} | {fmt(m['peak_mb'], 'MB')} |")
    lines += ["", "RSS only; MLX/Metal buffers of the TTS and LLM processes are partly outside RSS.", "", "## Environment", ""]
    lines += [f"- Machine: {env['machine']}, {env['memory_gb']} GB, {env['os']}, {env['gpu']}",
              f"- Browser: Chromium {raw['browser'].get('version')} (Playwright, headless), AudioContext {raw['browser'].get('context_sample_rate')} Hz",
              f"- Audio devices: {env['audio_devices']}",
              f"- Voice: {env['voice']}",
              f"- ASR worker: {env['active_workers']['asr']}",
              f"- TTS worker: {env['active_workers']['tts']}; engine `{env['tts_engine']}`",
              f"- LLM: {env['llm']}; {env['llm_context']}",
              f"- Runtimes: llama-server `{env['runtimes']['llama-server']}`; gateway `{env['runtimes']['gateway']}`; "
              f"asr `{env['runtimes']['asr']}`; tts `{env['runtimes']['tts']}`",
              "- Model files (models.lock.json; `./app doctor` verifies every file's SHA-256):", ""]
    lines += [f"  - `{m['id']}` {m['repo']}@{m['revision'][:12]} — `{m['largest_file']}` sha256 `{m['sha256'][:16]}…`" for m in env["models_lock"]]
    out_md.write_text("\n".join(lines) + "\n")
    print(out_md)
    for v in verdicts:
        print(f"  {v['label']}: {v['stat']} {fmt(v['value'], v['unit'])} (target {fmt(v['target'], v['unit'])}) "
              f"-> {'no data' if v['met'] is None else 'met' if v['met'] else 'UNMET'}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
