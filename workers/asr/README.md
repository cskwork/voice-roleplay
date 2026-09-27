# ASR worker (Qwen3-ASR-0.6B)

Implements `contracts/PROTOCOL.md` §3 on `127.0.0.1:8711`: `GET /health`, `POST /transcribe`, `WS /stream`.
Uses the official `qwen-asr` package (transformers backend), language fixed to English, model loaded from
`models/Qwen3-ASR-0.6B` with `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` (nothing is downloaded at runtime).

## Run

```sh
cd workers/asr
uv sync
VR_WORKER_TOKEN=$(openssl rand -hex 16) uv run python -m asr_worker
```

`VR_WORKER_TOKEN` is always required, in development too: the worker refuses to start without it, and every
request (HTTP and the WS upgrade) must send `X-Worker-Token`, otherwise 401. The gateway generates the token
and passes it when it spawns the worker; for manual runs pick any random value and send it yourself.

The model loads in the background after the port opens; `/health` reports `"ready": false` until the model
is loaded and warmed (about 5–10 s on the M3 Pro), and requests before that get `503 MODEL_NOT_READY`.
A load failure exits the process (code 3). Bad configuration exits with code 2 and a setup hint.

| Env | Default | |
|---|---|---|
| `VR_WORKER_TOKEN` | — (required) | internal auth token |
| `ASR_PORT` | `8711` | host is always 127.0.0.1 |
| `ASR_MODEL_DIR` | `<repo>/models/Qwen3-ASR-0.6B` | |
| `ASR_BACKEND` | `transformers` | `vllm` = untested NVIDIA path, see below |
| `ASR_DEVICE` | `auto` | `auto` picks `mps` if available, else `cpu` |
| `ASR_PARTIAL_INTERVAL_MS` | `700` | minimum time between partial decode starts |
| `ASR_PARTIAL_MIN_NEW_MS` | `300` | minimum new audio before another partial |
| `ASR_PARTIAL_WINDOW_S` | `12` | partial decode window for long utterances |
| `ASR_SILENCE_DBFS` | `-40` | audio with < 90 ms of 30 ms frames above this RMS is treated as silence |

Device and dtype: MPS uses bfloat16, CPU uses float32. On the M3 Pro, MPS was about 2x faster than CPU fp32
(bf16 and fp16 on MPS were equal, fp32 on MPS about 1.4x slower, bf16 on CPU about 3x slower than CPU fp32).
Run `uv run python bench.py [--device mps|cpu]` to measure again.

## Behaviour notes

- **Silence:** with the language forced to English the model hallucinates a word ("Okay.") on silent input,
  so near-silent audio is never sent to the model and returns `""`.
- **Partials** (incremental re-decode; no native streaming on macOS): a partial decode starts only when
  ≥ 700 ms passed since the previous one started, ≥ 300 ms of new audio arrived, the new audio is not silent,
  and no decode is running or waiting anywhere in the worker, so partials never queue up. After 12 s,
  older audio is frozen at the quietest 100 ms point (6–12 s into the window) and decoded in the same model
  call as the live tail. The frozen text is used as the stable prefix of later partials.
- **Final** = one full decode of the whole utterance, identical to `/transcribe` on the same audio. Finals
  waiting for the model go ahead of waiting partials. A partial that is still decoding at commit finishes
  first (a running model call cannot be interrupted), and its result is dropped. After the final the
  server closes the socket.
- **Cancel/close** drops pending partials and frees the buffer immediately; the server then closes the socket.
- **Limits:** `/transcribe` accepts at most 120 s (3 840 000 bytes, checked while reading) and returns
  413 `AUDIO_TOO_LONG` above that. A stream above 45.5 s (45 s plus slack for VAD pre-roll) gets
  `{"type":"error","code":"AUDIO_TOO_LONG"}` and is closed.
- **Error codes** (`{"error":{"code"}}` for HTTP, `{"type":"error","code"}` for WS): `AUTH_REQUIRED` (401),
  `AUDIO_TOO_LONG` (413), `AUDIO_INVALID` (odd byte count 400, wrong content type 415),
  `UNSUPPORTED_LANGUAGE` / `CONTEXT_TOO_LONG` (400), `MODEL_NOT_READY` (503),
  `WORKER_FAILED` / `OUT_OF_MEMORY` (500), `INVALID_MESSAGE` (WS).
- **Logging:** event names, sample counts and durations only. Uvicorn access logs are off, because query
  strings carry the `context` text. Transformers logging is set to errors only.

## vLLM backend: untested

`ASR_BACKEND=vllm` selects a small adapter around `Qwen3ASRModel.LLM(...)` for Linux/NVIDIA hosts. It has
**never been run**: the development machine has no NVIDIA GPU, and on macOS the worker refuses to start with
this setting. It would still produce partials by incremental re-decoding; qwen-asr's native vLLM streaming
API is not wired in. It needs `qwen-asr[vllm]` installed in the venv.

## Tests

```sh
../../tests/fixtures/make_fixtures.sh      # generate speech fixtures (macOS say + ffmpeg)
uv run pytest -m "not model"               # fast unit tests, fake backend
uv run pytest                              # + real-model integration tests (~1.5 min), prints measurements
```
