# Gateway (`vr_gateway`)

FastAPI on `127.0.0.1:8710`: local auth, the static web build, the realtime WebSocket engine (Silero VAD, turn
taking, ASR stream → LLM stream → segmenter → TTS), the recorded-practice job queue, SQLite, and a small
process supervisor for the ASR/TTS/LLM workers. Contracts: `contracts/PROTOCOL.md` §6–§9.

## Run

```sh
cd services/gateway
uv sync
.venv/bin/python -m vr_gateway                    # workers started elsewhere; VR_WORKER_TOKEN must match theirs
.venv/bin/python -m vr_gateway --manage-workers   # generates a token, starts asr/tts/llm, stops them on exit
```

`config.toml` holds ports, paths and the worker commands (`[supervisor.*]`; the LLM command is read from
`config/llm/server.json`). Env overrides: `VR_GATEWAY_PORT`, `VR_DATA_DIR`, `VR_CACHE_DIR`, `VR_WORKER_TOKEN`.
Workers get `VR_WORKER_TOKEN` plus `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`; llama-server gets the token as
`LLAMA_API_KEY`. Worker output goes to `var/log/<name>.log`.

Silero VAD: `assets/silero_vad.onnx` from PyPI `silero-vad` 6.2.3 (MIT, see `assets/SILERO_VAD_SOURCE.md`).

## HTTP API

All non-GET requests need the `vr_sid` cookie (set by `GET /`), `X-VR-CSRF` from `GET /api/bootstrap`, and an
allowed `Origin`. Every `/api` route except `/api/health` needs the cookie. Errors: `{"error":{"code","message_ko"}}`.

| Route | Notes |
|---|---|
| `GET /api/health` | per-worker readiness, `modes.realtime.available` (ASR+TTS+LLM+VAD), `modes.recorded.available` (ASR+VAD) |
| `GET /api/bootstrap` | `{csrf_token, protocol_version: 1}` |
| `GET /api/scenarios`, `GET /api/scenarios/{id}` | full scenario files |
| `GET/PUT /api/settings` | `difficulty, silence_ms (700–1400 or null), history_opt_in, voice_id, slow, auto_barge_in, feedback_policy, input_device_id, output_device_id, profile`; PUT is a partial update |
| `POST /api/sessions` | `{mode: "realtime"\|"turn_based", scenario_id, difficulty?, history_opt_in?, feedback_policy?, voice_id?}` → 201; realtime needs models ready (503 `MODEL_NOT_READY`) and no other active realtime session (409 `LOCAL_BUSY`) |
| `GET /api/sessions/{id}` | state, goals, turns (in memory), summary |
| `POST /api/sessions/{id}/end` | stops the realtime engine, returns `{summary: {items ≤3, goals, turns, status, pronunciation_score: null}}`; unsaved summaries expire after 15 min |
| `WS /api/sessions/{id}/realtime` | see below |
| `POST /api/attempts` | `{exercise_type: reading\|shadowing\|free_answer\|roleplay_turn, scenario_id, text_id \| exercise_id \| session_id, history_opt_in?}` |
| `PUT /api/attempts/{id}/audio` | raw WAV body (PCM16, 1–2 ch, 16/24/44.1/48 kHz, ≤120 s, ≤32 MiB), read into memory; one take per attempt once submitted |
| `POST /api/attempts/{id}/submit` | header `Idempotency-Key`; 202 new job, 200 same job again; 409 `IDEMPOTENCY_CONFLICT`, 429 `QUEUE_FULL`, 409 `LOCAL_BUSY` |
| `GET /api/jobs/{id}`, `DELETE /api/jobs/{id}` | `queued, transcribing, analyzing, synthesizing, completed, failed, cancelled, expired` |
| `GET /api/attempts/{id}/result` | transcript + revisions, feedback (latest revision) and `feedback_by_revision`, metrics (revision 1 only), `target_diff` (label `다르게 인식된 부분`), `model_audio[{audio_id, kind, text, url}]`, `next_ai` (roleplay_turn), `no_speech` |
| `PATCH /api/attempts/{id}/transcript` | `{text}` → new revision, text-only re-analysis job (202) |
| `GET /api/attempts/{id}/model-audio/{audio_id}` | WAV (memory only) |
| `GET/DELETE /api/history`, `POST /api/history/export` | opt-in data only; export `{format: json\|markdown}` as an attachment |
| `GET /api/review/due`, `POST /api/review/{item_id}/grade`, `POST /api/review` | Leitner boxes 1–5 (0/1/3/7/14 days); `POST /api/review` saves an expression, only with `history_opt_in` |
| `GET /api/tts/cached?voice_id=&text_id=&slow=` | pre-synthesized reviewed texts (`var/cache/tts/`) |
| `POST /api/tts` | `{voice_id, text ≤400 English, speed 0.5–1.5}` → WAV |

## Realtime WebSocket

As PROTOCOL §6.3, plus:

- **Turn ids.** The server announces the id in `speech.started`. It reuses the `turn_id` of the client's audio
  frames when that id is new, otherwise it makes one. `input.start {turn_id}` opens a push-to-talk turn with that
  id (silence does not end it). `input.commit` finishes the current speech whatever id it carries; repeats of the
  same id are no-ops that resend the same `asr.final`.
- **`seq`** is monotonic per connection; duplicated or older frames are dropped.
- Extra server events/fields: `response.text.text_ko` (opening line), `response.started.opening`,
  `speech.ended.{reason, discarded}`, `echo.suspected.{count, auto_barge_in, push_to_talk_suggested}`,
  `goal.update.changed`, `settings.applied` (reply to `settings.update`), `feedback.ready` only with
  `feedback_policy: "per_turn"`.
- Extra client event: `response.retry` re-asks the LLM after `LLM_FAILED` (PRD §17).
- Error codes on the socket: `FRAME_INVALID, EVENT_INVALID, ASR_FAILED, LLM_FAILED, TTS_FAILED, INVALID_STATE`.
- A client that never sends `playback.completed` keeps no AI lines in the LLM history; after
  `audio length + 2 s` the reply counts as finished for barge-in and state purposes.

## Tests

```sh
.venv/bin/python -m pytest                                # FAKE ASR/TTS/LLM (labelled in tests/conftest.py); real Silero in test_vad_real.py
VR_INTEGRATION=1 .venv/bin/python -m pytest tests/test_integration_real.py -s   # real workers via the supervisor
```
