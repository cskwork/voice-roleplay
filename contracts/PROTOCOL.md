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
| pronunciation worker (Qwen3-ForcedAligner-0.6B, wav2vec2-lv-60-espeak-cv-ft, pyworld) — optional, §12 | 8714 | `workers/pronunciation/.venv` (py3.12, uv), package `pron_worker` | `workers/pronunciation/` |

Model files live in `models/` (gitignored), pinned in `models.lock.json`:
- `models/Qwen3-ASR-0.6B` (Qwen/Qwen3-ASR-0.6B @ 5eb144179a02acc5e5ba31e748d22b0cf3e303b0)
- `models/Fun-CosyVoice3-0.5B-2512` (FunAudioLLM/Fun-CosyVoice3-0.5B-2512 @ 29e01c4e8d000f4bcd70751be16fa94bf3d85a18)
- `models/Qwen3-4B-Instruct-2507-GGUF/Qwen3-4B-Instruct-2507-Q4_K_M.gguf` (unsloth/Qwen3-4B-Instruct-2507-GGUF @ a06e946bb6b655725eafa393f4a9745d460374c9 — third-party conversion, record as such)
- Optional (§12): `models/Qwen3-ForcedAligner-0.6B` (Qwen/Qwen3-ForcedAligner-0.6B) and `models/wav2vec2-lv-60-espeak-cv-ft`
  (facebook/wav2vec2-lv-60-espeak-cv-ft), both Apache-2.0; revisions and hashes pinned in `models.lock.json` like the others.

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
- Voices: `content/voices/<voice_id>/{prompt.wav,prompt.txt,SOURCE.md,voice.json}`; `voice.json` = `{"label","license_note","mode"}`. Default voices (since 2026-09-29):
  `libritts_r_4992_f` and `libritts_r_1188_m` (LibriTTS-R, CC BY 4.0; the attribution text is in SOURCE.md and `license_note`).
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

`LOCAL_BUSY` (409) is no longer the answer to "a realtime session is active" (PRD §7, v0.2.1): starting new work ends that
session instead (§7). It is returned only when the previous realtime session could not be stopped within 5 s, so two
workloads never overlap; the request can simply be retried. As a pronunciation `reason` (§12.4) it still means a realtime
session started while the job was being analysed.

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
    "benchmark": {"status"}, "pronunciation_assessment": "<pronunciation_status>"}` (§12.4; `workers` also has
    `"pron": {reachable, ready, models, bands_enabled, calibration_version}` when the pronunciation worker is configured). Realtime needs ASR+TTS+LLM+VAD,
    recorded needs ASR+VAD. `blocked_by_realtime` is always `false` since v0.2.1 (kept for compatibility; recorded work
    ends the realtime session, §7). The TTS cache warm-up pauses while a realtime session is active; `./app start` waits until
    every opening line is cached.
  - `POST /api/sessions/{id}/end` → `{"session_id", "state": "ended", "summary": {"items": [≤3 feedback], "goals", "status": "ok|held|unavailable",
    "reason", "turns": [{turn_id, turn_index, role, text, transcript_revision, spoken_segments, playback_status, metrics}],
    "pronunciation_score": null, "pronunciation_status": "assessment_unavailable"}}` (realtime turns are never sent to the
    pronunciation worker, §12).
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
Server-side end: when the gateway ends a connected session it sends `session.state {state: "CLOSED"}` (an unfinished
reply gets `response.cancelled`), then closes the socket with `4000` reason `session_ended` (`POST /api/sessions/{id}/end`
or `session.end`, possibly from another tab) or `4001` reason `session_superseded` (the learner started something new, §7).
Clients treat both as a normal end, not a connection error. The summary stays available through `POST .../end` or
`GET /api/sessions/{id}` for 15 min (longer only with history opt-in).
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
`response.done {response_id}`, `response.cancelled {response_id}`, `turn.warning {turn_id, code:"UTTERANCE_40S"}`, `hint {level, text_ko?, keywords?, example_en?, response_id?}`,
`goal.update {goals:[{goal_id, status:"done|pending", evidence_turn_id?}]}`, `feedback.ready {...}`, `echo.suspected {}`, `error {code, message_ko, recoverable}`,
`settings.applied {silence_ms, auto_barge_in, slow}` (reply to every `settings.update`).

Extra fields: `response.started.{turn_id, opening?}`, `response.text.text_ko` (opening line only), `response.cancelled.reason`
(`barge_in|client|input_start|pause|...`), `speech.ended.{reason: silence|commit|max_length, discarded?}`,
`echo.suspected.{turn_id, count, auto_barge_in, push_to_talk_suggested}`, `goal.update.changed`, `asr.final` repeats for a
repeated commit. `feedback.ready {turn_id, transcript_revision, items, status}` is sent only with `feedback_policy: "per_turn"`.
Realtime error codes: `FRAME_INVALID, EVENT_INVALID, ASR_FAILED, LLM_FAILED, TTS_FAILED, INVALID_STATE`.

Hint levels map to the scenario hint fields: 1 = `ko` (plus a Korean rendering of the AI's last line when the LLM is up),
2 = + `keywords`, 3 = + `example_en`. Hints are built asynchronously (level 1 may call the LLM on slot 1); the reply is a `hint`
event whenever it is ready. `hint.response_id` names the AI line the hint was built for (the newest response with text); the
client shows a hint only while that line is still the newest one.

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
- One job slot, queue capacity 2, audio kept in memory only, TTL 5 min while queued.
- At most one realtime session is active, and it never runs next to new recorded work (PRD §7, v0.2.1). `POST /api/attempts`,
  `PUT /api/attempts/{id}/audio`, `POST /api/attempts/{id}/submit`, `PATCH /api/attempts/{id}/transcript` and
  `POST /api/sessions` (`mode: realtime`) first end every realtime session that has not ended, connected or waiting for a
  reconnect, through the same path as `POST /api/sessions/{id}/end` (cancel LLM/TTS/ASR work, close the socket with `4001`,
  release audio buffers; the summary is still built and kept as usual), wait until its engine has stopped, then proceed.
  A repeated `Idempotency-Key`, a request refused anyway (`INVALID_STATE`, `AUDIO_EXPIRED`, `QUEUE_FULL`) and a realtime
  start refused with `MODEL_NOT_READY` leave the running session alone. These switches are serialized in the gateway, so
  concurrent requests end up with exactly one realtime session. If the old engine does not stop within 5 s the request
  gets `LOCAL_BUSY` (409). The web app also ends the session itself when the learner leaves the realtime screen (route
  change or unmount: `POST .../end`; tab close or reload: the same request with `fetch(..., {keepalive: true})` carrying
  `X-VR-CSRF`).
- Job states: `queued, transcribing, analyzing, synthesizing, completed, failed, cancelled, expired`. On gateway start, any non-terminal job in
  SQLite becomes `expired`.
- WAV input: PCM16, 1–2 ch, 16/24/44.1/48 kHz, ≤ 120 s, ≤ 32 MiB; validated by actually parsing; request body read into memory with a hard cap (no temp spooling).
  Resample with a proper polyphase/sinc resampler (not by relabelling the rate); stereo → mono average.
- Reading, shadowing and drill exercises: transcribe **without** the target sentence as context; diff target vs transcript as
  "다르게 인식된 부분".
- Pronunciation analysis (§12) adds no job state: the learner side runs in `analyzing`, the model-audio side in `synthesizing`.
  A pronunciation failure or timeout never fails the job.

## 8. Feedback object (PRD §8.3, §13.3)
```json
{"feedback_id","category":"grammar|expression|vocabulary|goal|fluency_metric","severity":"required|optional",
 "status":"observed|suggested|needs_confirmation|unavailable","evidence_type":"asr_text|user_confirmed_text|vad_metric|target_diff|word_timing|prosody_contour|phone_gop",
 "source_turn_id"|"attempt_id","transcript_revision","evidence_quote","suggestion","explanation_ko","model_revision","prompt_revision"}
```
- `evidence_quote` must be an exact substring of the referenced transcript revision; otherwise drop the item (validator).
- Validator rejects text mentioning pronunciation/accent/stress/intonation/tone-of-voice/emotion claims (EN + KO keywords) unless status `unavailable`.
- `pronunciation_score: null` always. `pronunciation_status` is `assessment_unavailable` unless the pronunciation worker produced a
  result for a recorded attempt (§12.4). `word_timing`, `prosody_contour` and `phone_gop` evidence comes only from the pronunciation
  worker, never from the LLM; the LLM validator rule above is unchanged.
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

## 12. Pronunciation (PRD §8.4, v0.2)

Plain-language explanation for learners and reviewers: `docs/pronunciation.md`. Work items: `docs/TRIAGE.md` PA-0…PA-8.

### 12.1 Rules
- `pronunciation_score` stays `null` in every result. No numeric pronunciation value (GOP, probability, percentage, "accuracy")
  is ever shown to the learner. The only judgement the UI may show is a word band, and only in `experimental_banded` (§12.4).
- Default is `timing_only`: word timings, compare playback and pitch contours, **no judgements**.
- There is no Korean-learner evaluation data yet (PA-0), so the adoption criteria (§12.3) are not met and bands are **off by
  default**. Nothing may claim pronunciation accuracy for Korean learners.
- Alignment success is not a correct pronunciation, alignment failure is not an error (PRD §8.2).
- Recorded practice only. The gateway never calls the pronunciation worker while a realtime session is active, and realtime
  turns / session summaries keep `pronunciation_status: "assessment_unavailable"`.
- Local only: no cloud pronunciation API, no automatic fallback. The worker is optional; without it recorded practice works as
  before with `pronunciation.status: "unavailable"` (AT-18).
- Licences: Qwen3-ForcedAligner-0.6B Apache-2.0, wav2vec2-lv-60-espeak-cv-ft Apache-2.0, pyworld MIT. GPL-3.0 packages
  (Parselmouth, espeak-ng, phonemizer) are not added without a recorded licence decision; this applies to the text → expected-phone
  (G2P) step as well.

### 12.2 Worker — `http://127.0.0.1:8714`
Package `pron_worker` in `workers/pronunciation/` (own uv venv, py3.12), FastAPI, started as
`workers/pronunciation/.venv/bin/python -m pron_worker`. Same conventions as the other workers: `X-Worker-Token` on every request
including `/health` (401 `AUTH_REQUIRED`), `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`, no downloads at runtime, logging policy §2
(additionally: never log reference text, words, IPA strings or f0 values), exit codes `2` bad configuration / `3` model load failure (§1).
Env: `VR_WORKER_TOKEN`, `VR_PRON_PORT` (default 8714), `VR_PRON_EXPERIMENTAL` (`1` = allow bands, §12.3). Requests are served one
at a time (asyncio lock), like the ASR worker. Model placement and load timing are documented in the worker README and must respect
PRD §14.2 (the worker must not compete with a realtime session).

- `GET /health` → `{"ready", "device", "models": {"aligner": {"model_id", "revision"}, "phones": {"model_id", "revision"}},
  "prosody_method": "pyworld-harvest", "calibration_version": str|null, "bands_enabled": bool}`. Until models are loaded `ready: false`
  and other requests get 503 `MODEL_NOT_READY`.
- Request body for every POST: JSON (`Content-Type: application/json`) with `audio_b64` = base64 of PCM16LE mono 16 000 Hz,
  ≤ 120 s decoded (3 840 000 bytes). The gateway resamples before sending (§7).
- `POST /align` `{audio_b64, text}` → `{"words": [{"i", "word", "start_ms", "end_ms"}], "model_revision", "elapsed_ms"}`
  (Qwen/Qwen3-ForcedAligner-0.6B, English). `i` is the 0-based index of the word in `text` as tokenized by the worker; every word of
  `text` gets an entry.
- `POST /prosody` `{audio_b64, words?}` → `{"f0_hz": [...], "hop_ms": 10, "per_word": [{"i", "mean_f0", "f0_range_st", "duration_ms"}],
  "method": "pyworld-harvest"}`. `f0_hz` has one value per 10 ms frame, `0` = unvoiced. `per_word` is empty without `words`
  (the `/align` output); `mean_f0` is over voiced frames of the word (`0` if none), `f0_range_st` = `12·log2(max/min)` over voiced frames.
- `POST /assess` `{audio_b64, reference_text, mode: "scripted"|"unscripted"}` → `{"words": [{"i", "word", "start_ms", "end_ms",
  "phones": [{"expected_ipa", "heard_candidates": [{"ipa", "p"}], "gop"}], "word_gop", "band": null|"good"|"check"|"practice"}],
  "calibration_version": str|null, "bands_enabled": bool, "model_revisions": {"aligner", "phones"}}`.
  `gop` is a CTC-based goodness-of-pronunciation value from facebook/wav2vec2-lv-60-espeak-cv-ft phone posteriors; `p` is the model's
  posterior, **not calibrated**. Both are for the gateway and offline benchmarks only, never for display. `band` is `null` unless
  `bands_enabled`. `mode: "unscripted"` means `reference_text` is an ASR transcript.
- Error codes: `AUTH_REQUIRED` 401, `AUDIO_TOO_LONG` 413, `AUDIO_INVALID` 400 (bad base64, odd byte count), `TEXT_EMPTY` /
  `BAD_REQUEST` 400, `MODEL_NOT_READY` 503, `WORKER_FAILED` / `OUT_OF_MEMORY` 500. Body `{"error": {"code"}}` (§1).

### 12.3 Calibration, flag and adoption criteria
- `bands_enabled` = a calibration file was loaded **and** `VR_PRON_EXPERIMENTAL=1`. Either one alone → `band: null` everywhere.
- The calibration file (produced offline by PA-2/PA-3; path and format documented in the worker README) holds
  `calibration_version`, the dataset(s) it was fitted on with speaker L1s, the band thresholds, and the measured correlations.
  A file fitted only on speechocean762 (Mandarin-L1 speakers) may be used behind the flag; it is not evidence of accuracy for
  Korean learners and the UI note says so.
- **Adoption criteria** (TRIAGE PA-2) for turning bands on by default (future status `assessed_banded`): on the consented
  Korean-learner set (PA-0), Pearson correlation with human ratings ≥ 0.55 at phone level and ≥ 0.5 at word level, with at least two
  raters and inter-rater agreement reported, and speechocean762 results reported separately. **Status on 2026-09-28: not met
  (PA-0 data does not exist).** Changing the default requires a PRD update, not only a config change.
- Bands are displayed as bands only (colour **plus** text label and icon, PRD §12), never with numbers.

### 12.4 Gateway: attempt result `pronunciation`
`GET /api/attempts/{id}/result` gains
```json
"pronunciation": {
  "status": "unavailable|timing_only|experimental_banded",
  "reason": "<CODE>|null",
  "mode": "scripted|unscripted",
  "transcript_revision": 1,
  "words": [{"i", "word", "start_ms", "end_ms", "gap_before_ms", "in_transcript": bool|null,
             "band": null|"good"|"check"|"practice", "heard_ipa": ["<ipa>", ...]|null}],
  "prosody": {"learner": {"hop_ms": 10, "f0_hz": [...], "per_word": [...]},
              "model": null|{"audio_id", "hop_ms": 10, "f0_hz": [...], "per_word": [...], "words": [{"i", "word", "start_ms", "end_ms"}]}},
  "calibration_version": "<version>|null",
  "model_revisions": {"aligner", "phones"?},
  "note_ko": "<Korean note shown with the result>"
}
```
- **Status.** `unavailable` (worker not configured/not ready, request failed or timed out, no speech, empty reference; `reason` holds
  the code, e.g. `MODEL_NOT_READY`, `WORKER_FAILED`, `TIMEOUT`, `NO_SPEECH`), `timing_only` (default), `experimental_banded` (worker
  reported `bands_enabled`). Top-level `pronunciation_status` mirrors it: `assessment_unavailable` / `timing_only` /
  `experimental_banded`; `assessed_banded` is reserved and not produced in v0.2. `GET /api/health` `pronunciation_assessment` reports
  what a new attempt would get (`assessment_unavailable` / `timing_only` / `experimental_banded`).
- **Reference text.** `reading`, `shadowing`, `drill`: scripted, reference = target text. `free_answer`, `roleplay_turn`: unscripted,
  reference = ASR transcript revision 1. A transcript edit (§8, PATCH) does not re-align the old audio; the result stays bound to
  `transcript_revision` 1 (PRD §13.3).
- **Calls.** `timing_only`: `/align` then `/prosody` with the aligned words. `experimental_banded`: `/assess` (its word timings replace
  `/align`) then `/prosody`. Model side (scripted only, when the attempt has model audio whose text equals the reference text after
  normalization): the same `/align` + `/prosody` on that model audio (resampled to 16 kHz), linked by `audio_id` from `model_audio`.
  Otherwise `prosody.model` is `null`.
- **Fields.** `gap_before_ms` = start of the word − end of the previous word (0 for the first). `in_transcript` (scripted only): whether
  the target diff found the word in the transcript; `false` means the aligner placed a word the ASR did not hear and the UI marks the
  interval as uncertain. `band` and `heard_ipa` (top candidates of the word's phones, no probabilities) are non-null only in
  `experimental_banded`. `gop`, `word_gop` and `p` never leave the gateway. In `unscripted` mode a band is shown as
  `needs_confirmation` until the learner confirms the transcript (PRD §8.3).
- **Evidence types** (§8): `word_timing` (word intervals), `prosody_contour` (f0 and per-word duration), `phone_gop`
  (`experimental_banded` only). Every item carries the word index, interval in ms and model revision.
- **Compare playback.** "내 발음" plays the browser's own copy of the take (the server released the audio, §7) sliced by
  `start_ms`/`end_ms`; "모범 음성" plays `GET /api/attempts/{id}/model-audio/{audio_id}` sliced by `prosody.model.words`.
- **Timeouts.** Per worker call 30 s for audio ≤ 30 s, 90 s up to 120 s (initial values; replace with measured ones). On timeout the
  request is abandoned and the status is `unavailable`.
- **`note_ko`.** `timing_only`: "단어 위치와 억양 곡선만 보여 줍니다. 발음을 채점하지 않습니다." `experimental_banded`: "실험 기능입니다.
  보정 데이터가 한국인 학습자로 검증되지 않았으므로 결과가 틀릴 수 있어요." `unavailable`: "이번 녹음은 발음 분석을 하지 못했어요."
  The UI may shorten these but not remove the meaning.
- **Storage.** With `history_opt_in`, only `words` (without `heard_ipa`) and `calibration_version` may be stored with the attempt;
  pitch contours, phone candidates and audio are never stored.
