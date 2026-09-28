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

Per-file SHA-256, licenses, runtime versions and the `vendor/CosyVoice` commit are in `models.lock.json`
(`./app setup` downloads exactly those files and verifies the hashes; `./app doctor` re-checks them).
Workers never download anything at runtime (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`). Missing files → fail with a setup hint.

The TTS worker may offer more than one backend (env `VR_TTS_BACKEND`, owned by `workers/tts`); its CLI stays
`workers/tts/.venv/bin/python -m tts_worker` and its HTTP/WS contract (§4) does not depend on the backend.

### Worker auth
Gateway generates a random internal token at start and passes it to workers via env `VR_WORKER_TOKEN`.
Every worker request, **including `GET /health`**, must carry header `X-Worker-Token: <token>` (WS: same header on
the upgrade request). Reject otherwise with 401 `AUTH_REQUIRED`. Workers must not log request text/audio.
llama-server gets the same token as `LLAMA_API_KEY` (§5): `/v1/*` needs `Authorization: Bearer <token>`,
its `/health` stays open (llama-server behaviour).

### Worker process exit codes
`0` normal stop, `2` bad configuration (missing `VR_WORKER_TOKEN`, model dir, unsupported backend; the message
is a setup hint), `3` model load failure (ASR: the process exits so the supervisor sees it). The TTS worker keeps
serving `/health` with `"ready": false, "error": "<ExceptionType>"` after a load failure instead of exiting.

### Error bodies
Worker HTTP errors are `{"error": {"code"}}`, WS errors `{"type": "error", "code"}` (plus `request_id` on TTS).

## 2. Logging policy (all processes)
Log only: event names, ids, durations, byte/sample counts, status, error codes. **Never** transcripts, TTS text,
prompts, LLM output, audio, tokens. Third-party loggers that print text (CosyVoice logs synthesized text) must be
silenced/patched in the worker adapter. A test greps logs for a unique phrase after a run (PRD AT-16).

## 3. ASR worker — `http://127.0.0.1:8711`

Audio in: PCM signed 16-bit little-endian, mono, 16 000 Hz (gateway resamples before sending).

- `GET /health` → `{"ready": bool, "model_id", "revision", "device": "mps|cpu|cuda", "backend": "transformers|vllm", "streaming_mode": "incremental_redecode|native"}`.
  The model loads after the port opens; until then `ready: false` and other requests get 503 `MODEL_NOT_READY`.
- `POST /transcribe` — body raw PCM16 bytes (≤ 120 s = 3 840 000 bytes, else 413), `Content-Type: application/octet-stream`.
  Query: `language=English` (default), optional `context` (scenario hint text, ≤ 300 chars; **never the target reading sentence**).
  → `{"text", "language", "audio_ms", "elapsed_ms"}`. Empty/near-silent audio → `{"text": ""...}` without a model call
  (with the language forced to English the model hallucinates a word on silence).
- Error codes: `AUTH_REQUIRED` 401, `AUDIO_TOO_LONG` 413, `AUDIO_INVALID` (odd byte count 400, wrong content type 415),
  `UNSUPPORTED_LANGUAGE` / `CONTEXT_TOO_LONG` 400, `MODEL_NOT_READY` 503, `WORKER_FAILED` / `OUT_OF_MEMORY` 500,
  `INVALID_MESSAGE` (WS only).
- `WS /stream` — one utterance per connection. Same optional query parameters as `/transcribe` (`language`, `context`;
  the gateway's realtime path sends neither). Access logs are off because the query can carry `context`.
  - client → server: binary frames of PCM16 (any size); JSON `{"type":"commit"}` = end of utterance; JSON `{"type":"cancel"}`.
  - server → client: `{"type":"partial","text","audio_ms"}` (replaces previous partial), `{"type":"final","text","audio_ms","elapsed_ms"}` once after commit, `{"type":"error","code"}`.
    The server closes the socket after the final, after a cancel, and after an error.
  - A stream longer than 45.5 s (45 s plus slack for the gateway's VAD pre-roll) gets `{"type":"error","code":"AUDIO_TOO_LONG"}`.
  - Partial policy on macOS: re-decode the utterance buffer at most every `ASR_PARTIAL_INTERVAL_MS` (default 700), only if ≥ 300 ms new audio arrived, the new audio is not silent, and no decode is running (never queue up redundant decodes; never every 20 ms). For utterances > 12 s decode only the last 12 s window for partials, and prefix with the stable text of earlier windows. Final = one full decode of the whole utterance (≤ 45 s).
  - Cancel/close frees the buffer immediately.
- Concurrency: one model instance; decodes are serialized with an asyncio lock; final decodes take priority over partials.

## 4. TTS worker — `http://127.0.0.1:8712`

- `GET /health` → `{"ready", "model_id", "revision", "device", "sample_rate": int, "voices": [{"voice_id","label","license_note"}]}`
  plus `placement` (per-module device), `load_seconds`, and `error` (exception type) after a failed load. Backends may add fields.
- `WS /synthesize` — sequential requests on one connection (served with the `wsproto` implementation). Requests are
  queued per connection and run in order, at most 8 waiting (`QUEUE_FULL` beyond that); `cancel` works for the active
  or a queued request.
  - client → server: `{"type":"synthesize","request_id","voice_id","text","speed":1.0}` (text is English `spoken_text` only, ≤ 400 chars);
    `{"type":"cancel","request_id"}`.
  - server → client: `{"type":"start","request_id","sample_rate"}`, then binary frames = raw PCM16LE mono at `sample_rate`
    (each frame belongs to the most recent `start`), then `{"type":"done","request_id","audio_ms","elapsed_ms","first_chunk_ms"}`
    or `{"type":"cancelled","request_id"}` or `{"type":"error","request_id","code"}`.
  - `speed` 0.5–2.0. CosyVoice only supports speed in non-stream mode, so a request with `speed != 1.0` sends its audio
    as one chunk per sentence instead of streaming. Binary frames carry at most 100 ms of audio.
  - Error codes: `MODEL_NOT_READY, VOICE_NOT_FOUND, TEXT_NOT_ENGLISH, TEXT_TOO_LONG, TEXT_EMPTY, INVALID_SPEED, BAD_REQUEST,
    DUPLICATE_REQUEST, QUEUE_FULL, WORKER_FAILED` (plus `AUTH_REQUIRED` 401 on HTTP/upgrade).
  - Cancel must stop generation at the next model chunk boundary (check a flag between streaming yields), and no binary frames
    for that request may be sent after `cancelled`.
- `POST /synthesize` JSON `{"voice_id","text","speed"}` → `audio/wav` (PCM16 mono) — used for model sentences and cache warmup.
- Voices: `content/voices/<voice_id>/{prompt.wav,prompt.txt,SOURCE.md,voice.json}`; `voice.json` = `{"label","license_note","mode"}`. Initial 2 voices built from assets bundled in the pinned
  CosyVoice repo, marked **"개발용 — 출시 전 권리 확인된 음성으로 교체 필요"** in SOURCE.md and `license_note`.
- Text normalization before synthesis: expand prices/numbers/dates/times only where needed for correct reading; never pass markup, JSON, or Korean.

## 5. LLM — llama-server `http://127.0.0.1:8713`

Start (the gateway supervisor reads `config/llm/server.json`, verified with Homebrew llama-server 0.5.0 build 11146):
`LLAMA_API_KEY=$VR_WORKER_TOKEN llama-server -m <gguf> --host 127.0.0.1 --port 8713 -c 8192 -np 2 --no-kv-unified --jinja -ngl 99 --no-webui --cache-prompt`.
Two slots with a fixed 4096-token context each. The API key matters: llama-server answers CORS for any Origin.

Gateway uses OpenAI-compatible `POST /v1/chat/completions` with `stream: true` for roleplay (`max_tokens` 128, `cache_prompt: true`)
and `response_format: {"type":"json_schema", ...}` for feedback (`max_tokens` 768). Slots (`id_slot`):
- **slot 0** — realtime roleplay replies and the prefix warm-up at `session.start`;
- **slot 1** — everything else that runs *during* a realtime session: goal checks, hints, per-turn feedback, rolling summary.
  Pinning keeps this work from evicting the roleplay prefix cached in slot 0.
- Recorded-practice jobs and the session-end summary are not pinned (no realtime session runs at the same time).

The LLM never gets tools. Prompt prefix (system + scenario facts + opening line) is kept byte-stable per session so the KV prefix
cache hits; per-turn state (goals, rolling summary, learner facts) goes into the last user message.

**At most one question per reply.** The system prompt asks for it, and the gateway enforces it: once a streamed segment ends in `?`
the LLM stream is closed (llama-server stops generating) and nothing after it is spoken or added to history.

**Rolling summary (PRD §11).** The prompt keeps the last 6 turns verbatim. When the history grows past that window, the gateway
folds the older entries into a summary (≤ 400 chars) with a background call on slot 1 after a reply is generated; a reply never waits
for it and uses whatever summary exists. The same call returns details the learner stated (`learner_facts`: name, value, and an
exact quote of the learner's words; items without a matching quote are dropped). These are kept as explicit state (≤ 8, newest
statement per name wins) and rendered separately from the summary, which can never overwrite them. Scenario `facts` stay in the
system prompt and are authoritative over both.

## 6. Browser ⇄ gateway

Origin: `http://127.0.0.1:8710` or `http://localhost:8710` (allowlist). Web build (`apps/web/dist`) served by the gateway at `/`.

### 6.1 Local auth
- `GET /` sets cookie `vr_sid` (random, HttpOnly, SameSite=Strict, Path=/) if absent.
- `GET /api/bootstrap` (requires `vr_sid`) → `{"csrf_token", "protocol_version": 1}`.
- All non-GET HTTP requests require `vr_sid` cookie + header `X-VR-CSRF: <csrf_token>` + allowed `Origin`. WS upgrade requires `vr_sid` + allowed `Origin`.
- Tokens never in URLs or logs. Missing/invalid → 401/403 with `{"error":{"code","message_ko"}}`.

### 6.2 HTTP API
As PRD §13.1 (route list with request fields: `services/gateway/README.md`). Error body always
`{"error": {"code": "<CODE>", "message_ko": "..."}}`. Codes and HTTP status:

| Code | Status | | Code | Status |
|---|---|---|---|---|
| `UNSUPPORTED_MODE` | 400 | | `NOT_FOUND` | 404 |
| `AUDIO_TOO_LONG`, `AUDIO_TOO_LARGE`, `BODY_TOO_LARGE` | 413 | | `AUTH_REQUIRED` | 401 |
| `AUDIO_INVALID`, `INVALID_REQUEST` | 422 | | `ORIGIN_DENIED`, `CSRF_INVALID` | 403 |
| `AUDIO_EXPIRED` | 410 | | `IDEMPOTENCY_CONFLICT`, `INVALID_STATE`, `LOCAL_BUSY` | 409 |
| `QUEUE_FULL` | 429 | | `MODEL_NOT_READY`, `OUT_OF_MEMORY` | 503 |
| `WORKER_FAILED` | 502 | | | |

- **Idempotency.** `POST /api/attempts/{id}/submit` and `PATCH /api/attempts/{id}/transcript` take header `Idempotency-Key`
  (≤ 128 chars). A new key → 202 + new job; the same key for the same attempt/kind → 200 + the same job (also across restarts,
  from SQLite); the same key for something else → 409 `IDEMPOTENCY_CONFLICT`. Without the header, submit uses one implicit key
  per attempt.
- **Attempts.** `POST /api/attempts` `{exercise_type, scenario_id?, text_id? | exercise_id? | session_id?, target_text?, history_opt_in?}`.
  `exercise_type`: `reading | shadowing` (`text_id`), `free_answer` (`exercise_id`), `roleplay_turn` (`session_id` of a
  `turn_based` session), `drill`. `scenario_id` is required except for `drill`.
  **`drill`** ("다시 말하기" of a suggestion or saved expression): `target_text` (English, ≤ 400 chars) is required and only allowed
  here; optional `scenario_id`, `session_id`. Processed like reading: the target never goes to ASR as context, the result has
  `target_diff` ("다르게 인식된 부분"), no LLM feedback, no model audio (the client plays the sentence via `POST /api/tts`).
- **Response shapes.**
  - `GET /api/health` → `{"gateway": {"ready", "protocol_version"}, "workers": {"asr": {reachable, ready, model_id, revision, device, backend, streaming_mode},
    "tts": {reachable, ready, model_id, revision, device, sample_rate, voices}, "llm": {reachable, ready, model_revision}, "vad": {ready, model}},
    "modes": {"realtime": {available, reason_ko, session_active}, "recorded": {available, reason_ko, feedback_available, model_audio_available,
    blocked_by_realtime}}, "tts_cache": {"status": "idle|warming|paused|done", "openings_total", "openings_ready", "openings_failed"},
    "benchmark": {"status"}, "pronunciation_assessment": "assessment_unavailable"}`. Realtime needs ASR+TTS+LLM+VAD,
    recorded needs ASR+VAD. The TTS cache warm-up pauses while a realtime session is active; `./app start` waits until
    every opening line is cached.
  - `POST /api/sessions/{id}/end` → `{"session_id", "state": "ended", "summary": {"items": [≤3 feedback], "goals", "status": "ok|held|unavailable",
    "reason", "turns": [{turn_id, turn_index, role, text, transcript_revision, spoken_segments, playback_status, metrics}],
    "pronunciation_score": null, "pronunciation_status": "assessment_unavailable"}}`.
  - Jobs (`GET/DELETE /api/jobs/{id}`, submit, transcript PATCH) → `{"job_id", "attempt_id", "kind": "analyze|reanalyze", "state",
    "error_code", "transcript_revision", "created_at"}`.

Additional endpoints (Speak-style learning loop):
- `GET /api/review/due` → saved expressions due for spaced review (Leitner boxes 1–5; intervals 0,1,3,7,14 days), only if history opt-in.
- `POST /api/review/{item_id}/grade` `{"result":"again|good"}`.
- `GET /api/tts/cached?voice_id=&text_id=` → pre-synthesized WAV for scenario opening lines / model expressions (`text_id` from scenario file; no free text).
- `POST /api/tts` `{"voice_id","text","speed"}` → WAV (model sentence playback; text length ≤ 400; English only).

### 6.3 Realtime WebSocket `WS /api/sessions/{id}/realtime`

Upgrade refusals: the gateway closes the socket before accepting it with `4401` no/invalid `vr_sid`, `4403` Origin
not allowed, `4404` unknown, ended or non-realtime session, `4409` the session already has a connection (one per session).
A close before accept becomes an HTTP 403 handshake response in uvicorn, so a real client (browser, raw upgrade) sees
a refused handshake without the code; only in-process ASGI test clients see the codes (checked in `tests/e2e`, AT-20).

**Binary frame** = `u32 little-endian header_len` + UTF-8 JSON header + PCM16LE payload.
Header: `{"v":1,"kind":"input_audio"|"output_audio","session_id","turn_id"|"response_id","epoch","seq","sample_rate","sample_count","segment_id"?}`.
`segment_id` (output only) is an integer, 0-based per response, matching `response.text.segment_id`. `seq` is monotonic per connection;
duplicate or older input frames are dropped.
Reject (error event `FRAME_INVALID`, drop frame) if `len(payload) != sample_count*2`, header > 1 KiB, payload > 64 KiB, unknown kind, or wrong session.
Input audio: 16 000 Hz mono, 20 ms frames (320 samples). Output audio: TTS sample_rate, chunked ≤ 100 ms.

**Control events** (JSON text frames), every event has `type, event_id, session_id, epoch, event_seq` (+ `turn_id` / `response_id` where relevant).

Client → server: `session.start`, `input.start`, `input.commit {turn_id, last_seq}`, `response.cancel {response_id}`, `session.pause`, `session.resume`,
`session.end`, `playback.started {response_id, segment_id}`, `playback.completed {response_id, segment_id}`, `playback.stopped {response_id, segment_id, played_ms}`,
`hint.request {level: 1|2|3}`, `settings.update {silence_ms?, auto_barge_in?, slow?}`, `mic.state {muted}`,
`response.retry {}` (re-asks the LLM for the last user turn after `LLM_FAILED`, PRD §17; otherwise `INVALID_STATE`).

Server → client: `session.state {input_state, output_state, state}`, `speech.started {turn_id}`, `speech.ended {turn_id}`, `asr.partial {turn_id, text}`,
`asr.final {turn_id, text, transcript_revision}`, `response.started {response_id, epoch}`, `response.text {response_id, segment_id, text}`,
`response.done {response_id}`, `response.cancelled {response_id}`, `turn.warning {turn_id, code:"UTTERANCE_40S"}`, `hint {level, text_ko?, keywords?, example_en?}`,
`goal.update {goals:[{goal_id, status:"done|pending", evidence_turn_id?}]}`, `feedback.ready {...}`, `echo.suspected {}`, `error {code, message_ko, recoverable}`,
`settings.applied {silence_ms, auto_barge_in, slow}` (reply to every `settings.update`).

Extra fields: `response.started.{turn_id, opening?}`, `response.text.text_ko` (opening line only), `response.cancelled.reason`
(`barge_in|client|input_start|pause|...`), `speech.ended.{reason: silence|commit|max_length, discarded?}`,
`echo.suspected.{turn_id, count, auto_barge_in, push_to_talk_suggested}`, `goal.update.changed`, `asr.final` repeats for a
repeated commit. `feedback.ready {turn_id, transcript_revision, items, status}` is sent only with `feedback_policy: "per_turn"`.
Realtime error codes: `FRAME_INVALID, EVENT_INVALID, ASR_FAILED, LLM_FAILED, TTS_FAILED, INVALID_STATE`.

Hint levels map to the scenario hint fields: 1 = `ko` (plus a Korean rendering of the AI's last line when the LLM is up),
2 = + `keywords`, 3 = + `example_en`. Hints are built asynchronously (level 1 may call the LLM on slot 1); the reply is a `hint`
event whenever it is ready.

**Turn ids.** The server owns turn ids and announces them in `speech.started`. It reuses the `turn_id` of the client's audio
frames when that id is new, otherwise it makes one. `input.start {turn_id}` opens a push-to-talk turn with that id (silence does
not end it). `input.commit` finishes whatever speech is in progress, whichever id it carries (the client id is aliased to the
server id); a repeated commit for the same id is a no-op that re-sends the same `asr.final`.

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
  The client's level trigger (160 ms) usually fires first. If it stopped the only unfinished segment of a reply that was
  already fully generated, the reply counts as finished and nothing is cancelled (no `response.cancelled`, no epoch bump),
  but speech announced within 2 s of that `playback.stopped` is still a barge-in for the echo check below.
- History: only segments confirmed by `playback.completed` enter conversation history; stopped ones are stored as `interrupted` with the played prefix unknown.
  A client that never sends `playback.completed` keeps no AI lines in the LLM history; after `audio length + 2 s` the reply counts as
  finished for barge-in and state purposes.
- Background LLM work (goal checks, rolling summary, per-turn feedback) starts once a reply is generated and never blocks the next reply.
  Goal checks cover only goals still `pending`, run one at a time, and a queued check is skipped if the learner has already started a
  newer turn (that turn's reply schedules its own check).
- Echo heuristic: if a barge-in's final transcript has `echo_overlap(user_text, ai_segment) ≥ 0.6` (fraction of the user's words that
  appear in order in the AI segment that was playing, `vr_feedback.echo`), discard it and emit `echo.suspected`;
  after 2 in a session, disable auto barge-in and suggest push-to-talk (`눌러 말하기`).

**Response pipeline (latency optimizations)**
- Roleplay prompt prefix is byte-stable per session; gateway warms the llama.cpp prefix cache at `session.start` (`n_predict: 0` / `max_tokens: 1`).
- Scenario opening line audio is pre-synthesized at gateway startup (service asset cache in `var/cache/tts/`, not user data) → first AI turn plays instantly.
- Segmenter: first segment may be a clause (≥ 4 words ending in `,`/`;`/`—`), later segments are full sentences; never split inside numbers
  (`$4.50`, `3.5`), abbreviations (`Mr.`, `Dr.`, `a.m.`, `p.m.`, `e.g.`, `U.S.`), or quotes. TTS for segment n+1 is requested while n plays.
  Generation stops after the first segment ending in `?` (§5).
- Client send backlog: if the socket's `bufferedAmount` holds more than 2 s of input frames (headers included), the web app warns and
  offers pause; it never drops audio to hide the delay (PRD §10.2).
- Stale output: client drops any output audio/text whose `epoch` ≠ current epoch or whose `response_id` was cancelled.

## 7. Non-realtime jobs
- One job slot, queue capacity 2, audio kept in memory only, TTL 5 min while queued. Realtime session active → `LOCAL_BUSY` (409).
- Job states: `queued, transcribing, analyzing, synthesizing, completed, failed, cancelled, expired`. On gateway start, any non-terminal job in
  SQLite becomes `expired`.
- WAV input: PCM16, 1–2 ch, 16/24/44.1/48 kHz, ≤ 120 s, ≤ 32 MiB; validated by actually parsing; request body read into memory with a hard cap (no temp spooling).
  Resample with a proper polyphase/sinc resampler (not by relabelling the rate); stereo → mono average.
- Reading, shadowing and drill exercises: transcribe **without** the target sentence as context; diff target vs transcript as
  "다르게 인식된 부분".

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

## 10. Scenario content
`contracts/scenario.schema.json` (JSON Schema draft 2020-12) validates every `content/scenarios/*.json`. The gateway skips (and logs
by file name) any file that fails the schema or does not have exactly 3 goals. Field meanings and review rules: `content/README.md`.

## 11. Launcher (`./app`, PRD §14.3)
- `setup` — asks for consent (`--yes` skips the prompt), then: `uv sync --frozen` per Python component, `workers/tts/setup.sh`
  (vendor/CosyVoice at the pinned commit), `npm ci && npm run build` for the web app, model downloads of the exact files in
  `models.lock.json` at the pinned revisions, and SHA-256 verification of every file.
- `doctor` — hardware, runtime versions, model files + hashes, ports, voices, offline readiness; prints a table, exits non-zero on failure.
- `start` — refuses when model files or environments are missing (never downloads), runs `python -m vr_gateway --manage-workers`
  in the background (pid in `var/run/gateway.pid`, workers in `var/run/<name>.pid`), waits for `/api/health` and prints
  `http://127.0.0.1:8710`.
- `stop` — SIGTERM to the gateway (cancels jobs, closes sessions, stops workers), then kills leftover workers from their pidfiles.
- `benchmark` — runs `benchmarks/run.sh`.
