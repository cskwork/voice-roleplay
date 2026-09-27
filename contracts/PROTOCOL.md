# Internal contracts (source of truth for all components)

Product spec: `docs/PRD.md`. This file fixes the concrete interfaces so components can be built in parallel.
If a component must deviate, update this file in the same change and say why.

## 0. Target machine and deviations from the PRD reference profile

- Dev/validation machine: Apple M3 Pro, 36 GB unified memory, macOS arm64. No NVIDIA GPU.
- The PRD's realtime ASR reference path (`qwen-asr` vLLM streaming) is Linux/NVIDIA-only. On macOS the ASR
  worker uses the `qwen-asr` transformers backend on MPS (fallback CPU) and produces partial captions by
  **bounded incremental re-decoding** (see §3). A `vllm` backend adapter is kept behind the same worker
  contract for NVIDIA hosts but is untested here — it must be labelled as such in docs.
- All measured numbers go to `benchmarks/results/`. Never state a PRD target as met unless measured.

## 1. Processes and ports (all bind 127.0.0.1 only)

| Process | Port | Env / runtime | Dir |
|---|---|---|---|
| gateway (FastAPI + static web + VAD + feedback + SQLite) | 8710 | `services/gateway/.venv` (py3.12, uv) | `services/gateway/` |
| ASR worker (Qwen3-ASR-0.6B) | 8711 | `workers/asr/.venv` (py3.12, uv) | `workers/asr/` |
| TTS worker (Fun-CosyVoice3-0.5B-2512) | 8712 | `workers/tts/.venv` (py3.10, uv) + `vendor/CosyVoice` at pinned commit | `workers/tts/` |
| LLM (llama.cpp `llama-server`, Qwen3-4B-Instruct-2507 Q4_K_M GGUF) | 8713 | Homebrew `llama-server` | `config/llm/` |

Model files live in `models/` (gitignored), pinned in `models.lock.json`:
- `models/Qwen3-ASR-0.6B` (Qwen/Qwen3-ASR-0.6B @ 5eb144179a02acc5e5ba31e748d22b0cf3e303b0)
- `models/Fun-CosyVoice3-0.5B-2512` (FunAudioLLM/Fun-CosyVoice3-0.5B-2512 @ 29e01c4e8d000f4bcd70751be16fa94bf3d85a18)
- `models/Qwen3-4B-Instruct-2507-GGUF/Qwen3-4B-Instruct-2507-Q4_K_M.gguf` (unsloth/Qwen3-4B-Instruct-2507-GGUF @ a06e946bb6b655725eafa393f4a9745d460374c9 — third-party conversion, record as such)

Workers never download anything at runtime (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`). Missing files → fail with a setup hint.

### Worker auth
Gateway generates a random internal token at start and passes it to workers via env `VR_WORKER_TOKEN`.
Every worker request must carry header `X-Worker-Token: <token>` (WS: same header on the upgrade request).
Reject otherwise with 401. Workers must not log request text/audio.

## 2. Logging policy (all processes)
Log only: event names, ids, durations, byte/sample counts, status, error codes. **Never** transcripts, TTS text,
prompts, LLM output, audio, tokens. Third-party loggers that print text (CosyVoice logs synthesized text) must be
silenced/patched in the worker adapter. A test greps logs for a unique phrase after a run (PRD AT-16).

## 3. ASR worker — `http://127.0.0.1:8711`

Audio in: PCM signed 16-bit little-endian, mono, 16 000 Hz (gateway resamples before sending).

- `GET /health` → `{"ready": bool, "model_id", "revision", "device": "mps|cpu|cuda", "backend": "transformers|vllm", "streaming_mode": "incremental_redecode|native"}`
- `POST /transcribe` — body raw PCM16 bytes (≤ 120 s = 3 840 000 bytes, else 413), `Content-Type: application/octet-stream`.
  Query: `language=English` (default), optional `context` (scenario hint text, ≤ 300 chars; **never the target reading sentence**).
  → `{"text", "language", "audio_ms", "elapsed_ms"}`. Empty/near-silent audio → `{"text": ""...}`.
- `WS /stream` — one utterance per connection.
  - client → server: binary frames of PCM16 (any size); JSON `{"type":"commit"}` = end of utterance; JSON `{"type":"cancel"}`.
  - server → client: `{"type":"partial","text","audio_ms"}` (replaces previous partial), `{"type":"final","text","audio_ms","elapsed_ms"}` once after commit, `{"type":"error","code"}`.
  - Partial policy on macOS: re-decode the utterance buffer at most every `ASR_PARTIAL_INTERVAL_MS` (default 700), only if ≥ 300 ms new audio arrived and no decode is running (never queue up redundant decodes; never every 20 ms). For utterances > 12 s decode only the last 12 s window for partials, and prefix with the stable text of earlier windows. Final = one full decode of the whole utterance (≤ 45 s).
  - Cancel/close frees the buffer immediately.
- Concurrency: one model instance; decodes are serialized with an asyncio lock; final decodes take priority over partials.

## 4. TTS worker — `http://127.0.0.1:8712`

- `GET /health` → `{"ready", "model_id", "revision", "device", "sample_rate": int, "voices": [{"voice_id","label","license_note"}]}`
- `WS /synthesize` — sequential requests on one connection.
  - client → server: `{"type":"synthesize","request_id","voice_id","text","speed":1.0}` (text is English `spoken_text` only, ≤ 400 chars);
    `{"type":"cancel","request_id"}`.
  - server → client: `{"type":"start","request_id","sample_rate"}`, then binary frames = raw PCM16LE mono at `sample_rate`
    (each frame belongs to the most recent `start`), then `{"type":"done","request_id","audio_ms","elapsed_ms","first_chunk_ms"}`
    or `{"type":"cancelled","request_id"}` or `{"type":"error","request_id","code"}`.
  - Cancel must stop generation at the next model chunk boundary (check a flag between streaming yields), and no binary frames
    for that request may be sent after `cancelled`.
- `POST /synthesize` JSON `{"voice_id","text","speed"}` → `audio/wav` (PCM16 mono) — used for model sentences and cache warmup.
- Voices: `content/voices/<voice_id>/{prompt.wav,prompt.txt,SOURCE.md}`. Initial 2 voices built from assets bundled in the pinned
  CosyVoice repo, marked **"개발용 — 출시 전 권리 확인된 음성으로 교체 필요"** in SOURCE.md and `license_note`.
- Text normalization before synthesis: expand prices/numbers/dates/times only where needed for correct reading; never pass markup, JSON, or Korean.

## 5. LLM — llama-server `http://127.0.0.1:8713`

Start: `llama-server -m <gguf> --host 127.0.0.1 --port 8713 -c 8192 -np 2 --jinja -ngl 99 --no-webui` (exact flags in `config/llm/server.json`; verify against installed version).
Gateway uses OpenAI-compatible `POST /v1/chat/completions` with `stream: true` for roleplay (`max_tokens` 128, `cache_prompt: true`,
slot pinned per session via `id_slot` when supported) and `response_format: {"type":"json_schema", ...}` for feedback (`max_tokens` 768).
The LLM never gets tools. Prompt prefix (system + scenario facts) is kept byte-stable per session so the KV prefix cache hits.

## 6. Browser ⇄ gateway

Origin: `http://127.0.0.1:8710` or `http://localhost:8710` (allowlist). Web build (`apps/web/dist`) served by the gateway at `/`.

### 6.1 Local auth
- `GET /` sets cookie `vr_sid` (random, HttpOnly, SameSite=Strict, Path=/) if absent.
- `GET /api/bootstrap` (requires `vr_sid`) → `{"csrf_token", "protocol_version": 1}`.
- All non-GET HTTP requests require `vr_sid` cookie + header `X-VR-CSRF: <csrf_token>` + allowed `Origin`. WS upgrade requires `vr_sid` + allowed `Origin`.
- Tokens never in URLs or logs. Missing/invalid → 401/403 with `{"error":{"code","message_ko"}}`.

### 6.2 HTTP API
As PRD §13.1. Error body always `{"error": {"code": "<CODE>", "message_ko": "..."}}`. Codes include:
`UNSUPPORTED_MODE, AUDIO_TOO_LONG, AUDIO_TOO_LARGE, AUDIO_INVALID, AUDIO_EXPIRED, QUEUE_FULL, LOCAL_BUSY, MODEL_NOT_READY,
OUT_OF_MEMORY, NOT_FOUND, AUTH_REQUIRED, ORIGIN_DENIED, CSRF_INVALID, IDEMPOTENCY_CONFLICT, INVALID_STATE, WORKER_FAILED`.

Additional endpoints (Speak-style learning loop):
- `GET /api/review/due` → saved expressions due for spaced review (Leitner boxes 1–5; intervals 0,1,3,7,14 days), only if history opt-in.
- `POST /api/review/{item_id}/grade` `{"result":"again|good"}`.
- `GET /api/tts/cached?voice_id=&text_id=` → pre-synthesized WAV for scenario opening lines / model expressions (`text_id` from scenario file; no free text).
- `POST /api/tts` `{"voice_id","text","speed"}` → WAV (model sentence playback; text length ≤ 400; English only).

### 6.3 Realtime WebSocket `WS /api/sessions/{id}/realtime`

**Binary frame** = `u32 little-endian header_len` + UTF-8 JSON header + PCM16LE payload.
Header: `{"v":1,"kind":"input_audio"|"output_audio","session_id","turn_id"|"response_id","epoch","seq","sample_rate","sample_count","segment_id"?}`.
Reject (error event `FRAME_INVALID`, drop frame) if `len(payload) != sample_count*2`, header > 1 KiB, payload > 64 KiB, unknown kind, or wrong session.
Input audio: 16 000 Hz mono, 20 ms frames (320 samples). Output audio: TTS sample_rate, chunked ≤ 100 ms.

**Control events** (JSON text frames), every event has `type, event_id, session_id, epoch, event_seq` (+ `turn_id` / `response_id` where relevant).

Client → server: `session.start`, `input.start`, `input.commit {turn_id, last_seq}`, `response.cancel {response_id}`, `session.pause`, `session.resume`,
`session.end`, `playback.started {response_id, segment_id}`, `playback.completed {response_id, segment_id}`, `playback.stopped {response_id, segment_id, played_ms}`,
`hint.request {level: 1|2|3}`, `settings.update {silence_ms?, auto_barge_in?, slow?}`, `mic.state {muted}`.

Server → client: `session.state {input_state, output_state, state}`, `speech.started {turn_id}`, `speech.ended {turn_id}`, `asr.partial {turn_id, text}`,
`asr.final {turn_id, text, transcript_revision}`, `response.started {response_id, epoch}`, `response.text {response_id, segment_id, text}`,
`response.done {response_id}`, `response.cancelled {response_id}`, `turn.warning {turn_id, code:"UTTERANCE_40S"}`, `hint {level, text_ko?, keywords?, example_en?}`,
`goal.update {goals:[{goal_id, status:"done|pending", evidence_turn_id?}]}`, `feedback.ready {...}`, `echo.suspected {}`, `error {code, message_ko, recoverable}`.

**State machine** (server): `READY → LISTENING → FINALIZING → RESPONDING → LISTENING`; barge-in `RESPONDING → INTERRUPTING → LISTENING`;
common `PAUSED, RECOVERABLE_ERROR, CLOSED`. Input and output sub-states tracked separately; VAD runs during RESPONDING.

**Turn taking (gateway)**
- VAD: Silero VAD ONNX (onnxruntime, CPU), 512-sample windows at 16 kHz, per-session state. Pre-roll 200 ms ring buffer.
- End of turn: silence ≥ `silence_ms` (easy 1200, normal 900, hard 700; clamp 700–1400). **Adaptive extension (optimization):** if the latest partial
  ends with a filler or continuation word (`um, uh, and, but, because, so, or, the, a, to, like, I mean`), extend by +400 ms once per pause.
- Max utterance 45 s: `turn.warning` at 40 s, auto-commit at 45 s. Utterances with < 250 ms total speech are discarded silently (no LLM call).
- `input.commit` (manual "말하기 완료") carries `last_seq`; gateway finalizes after that frame; idempotent per `turn_id`.
- Barge-in: speech start during RESPONDING (≥ 200 ms voiced, and not suspected echo) → cancel `response_id`, bump `epoch`, send `response.cancelled`,
  cancel LLM stream + TTS request. The client also stops playback locally on its own VAD/level trigger or on `speech.started`.
- History: only segments confirmed by `playback.completed` enter conversation history; stopped ones are stored as `interrupted` with the played prefix unknown.
- Echo heuristic: if a barge-in's final transcript matches ≥ 60 % of the words of the currently playing AI segment, discard it and emit `echo.suspected`;
  after 2 in a session, disable auto barge-in and suggest push-to-talk (`눌러 말하기`).

**Response pipeline (latency optimizations)**
- Roleplay prompt prefix is byte-stable per session; gateway warms the llama.cpp prefix cache at `session.start` (`n_predict: 0` / `max_tokens: 1`).
- Scenario opening line audio is pre-synthesized at gateway startup (service asset cache in `var/cache/tts/`, not user data) → first AI turn plays instantly.
- Segmenter: first segment may be a clause (≥ 4 words ending in `,`/`;`/`—`), later segments are full sentences; never split inside numbers
  (`$4.50`, `3.5`), abbreviations (`Mr.`, `Dr.`, `a.m.`, `p.m.`, `e.g.`, `U.S.`), or quotes. TTS for segment n+1 is requested while n plays.
- Stale output: client drops any output audio/text whose `epoch` ≠ current epoch or whose `response_id` was cancelled.

## 7. Non-realtime jobs
- One job slot, queue capacity 2, audio kept in memory only, TTL 5 min while queued. Realtime session active → `LOCAL_BUSY` (409).
- Job states: `queued, transcribing, analyzing, synthesizing, completed, failed, cancelled, expired`. On gateway start, any non-terminal job in
  SQLite becomes `expired`.
- WAV input: PCM16, 1–2 ch, 16/24/44.1/48 kHz, ≤ 120 s, ≤ 32 MiB; validated by actually parsing; request body read into memory with a hard cap (no temp spooling).
  Resample with a proper polyphase/sinc resampler (not by relabelling the rate); stereo → mono average.
- Reading exercise: transcribe **without** the target sentence as context; diff target vs transcript as "다르게 인식된 부분".

## 8. Feedback object (PRD §8.3, §13.3)
```json
{"feedback_id","category":"grammar|expression|vocabulary|goal|fluency_metric","severity":"required|optional",
 "status":"observed|suggested|needs_confirmation|unavailable","evidence_type":"asr_text|user_confirmed_text|vad_metric|target_diff",
 "source_turn_id"|"attempt_id","transcript_revision","evidence_quote","suggestion","explanation_ko","model_revision","prompt_revision"}
```
- `evidence_quote` must be an exact substring of the referenced transcript revision; otherwise drop the item (validator).
- Validator rejects text mentioning pronunciation/accent/stress/intonation/tone-of-voice/emotion claims (EN + KO keywords) unless status `unavailable`.
- `pronunciation_score: null`, `pronunciation_status: "assessment_unavailable"` always (no scoring module).
- Metrics: `wpm = words / (last_voiced_end - first_voiced_start) * 60`, pause threshold 500 ms, edges excluded; `metrics_version: "m1"`.
- Max 3 items for session-end summary; each can be sent to "다시 말하기" drill (Speak-style loop): show suggestion, play model audio, record, compare.

## 9. SQLite (gateway only) — `var/data/app.sqlite3`
Tables: `settings, sessions, turns, attempts, transcript_revisions, feedback, jobs, review_items, model_manifest`.
Unique: `(session_id, turn_index)`, `jobs.idempotency_key`. Result write + job state transition in one transaction.
Transcripts/feedback persisted **only** when the session/attempt has `history_opt_in = true`; otherwise in-memory only (15-min summary TTL).
