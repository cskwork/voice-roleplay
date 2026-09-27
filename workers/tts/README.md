# TTS worker (Fun-CosyVoice3-0.5B-2512)

Implements `contracts/PROTOCOL.md` §4 on `127.0.0.1:8712`.

## Setup and run

```sh
cd workers/tts
./setup.sh                                   # vendor/CosyVoice @ 074ca6dc + uv env (Python 3.10, torch 2.3.1)
VR_WORKER_TOKEN=<token> .venv/bin/python -m tts_worker
```

The model must already be in `models/Fun-CosyVoice3-0.5B-2512` (revision `29e01c4e…`, read from the HF download
metadata and reported by `/health`). Nothing is downloaded at runtime.

| Env | Default | |
|---|---|---|
| `VR_WORKER_TOKEN` | required | checked on every HTTP request and on the WS upgrade (401 otherwise) |
| `VR_TTS_PORT` | `8712` | |
| `VR_TTS_DEVICE` | `auto` | `auto` = `hybrid` when MPS is available, else `cpu` |
| `VR_TTS_CPU_THREADS` | `4` | torch CPU threads (4 was fastest for the CPU-side LLM on M3 Pro; 6/11 were slower) |

## API notes

- `GET /health` adds `placement` (per-module device) and `load_seconds`; `device` is where the flow model runs.
- `WS /synthesize`: requests on one connection are queued and run in order (max 8 queued). `cancel` works for the
  active or a queued request; `cancelled` is sent immediately and no frames for that request follow it. Binary frames
  carry at most 100 ms of PCM16 at 24 kHz. Error codes: `MODEL_NOT_READY, VOICE_NOT_FOUND, TEXT_NOT_ENGLISH,
  TEXT_TOO_LONG, TEXT_EMPTY, INVALID_SPEED, BAD_REQUEST, DUPLICATE_REQUEST, QUEUE_FULL, WORKER_FAILED`.
- `POST /synthesize` returns a 24 kHz mono PCM16 WAV. It uses CosyVoice's non-stream mode (faster for whole WAVs).
- `speed` (0.5–2.0): CosyVoice only supports speed in non-stream mode (mel time-stretch, pitch kept), so a WS
  request with `speed != 1.0` sends its audio as one chunk per sentence instead of streaming.
- Text is normalized in `tts_worker/textnorm.py` (prices, percentages, times, dates, years, ordinals, decimals, phone
  numbers, plain numbers). Hangul is rejected, CosyVoice control markup (`<|…|>`, `[breath]`, tags) is stripped.
  Upstream's `wetext` normalizer is not installed: it downloads its FST files from ModelScope when constructed.

## Voices

`content/voices/dev_voice_a` (zero-shot, upstream transcript) and `dev_voice_b` (cross-lingual) come from the two
Mandarin prompt clips bundled in the CosyVoice repo; there is no English prompt audio upstream. Both are marked
**개발용 — 출시 전 권리 확인된 음성으로 교체 필요**; provenance is in each `SOURCE.md`. `voice.json` holds label,
license note and mode.

## Why these device choices (measured, Apple M3 Pro 36 GB, other agents running, load avg 4–7)

- MPS for everything is not possible: HiFT's F0 predictor needs float64. With LLM and flow both on MPS, the LLM
  thread and the flow thread crash Metal (`failed assertion ... commit call`), on torch 2.3.1 and 2.7.1.
- `hybrid` = LLM on CPU (fp32, 4 threads, ~30–35 tokens/s; the model needs 25 tokens/s for real time), flow (DiT)
  on MPS, HiFT on CPU. `bench.py` (5 sentences × 2 voices, engine level):

| placement | stream first chunk p50 | stream RTF p50 / p95 | non-stream RTF p50 / p95 |
|---|---|---|---|
| hybrid | 2.97 s | 1.91 / 2.83 | 1.54 / 2.71 |
| cpu | 5.79 s | 3.82 / 4.44 | 2.30 / 4.12 |

  Results JSON: `benchmarks/`. Short sentences have the worst RTF (fixed per-request cost). **The PRD target
  (RTF p95 ≤ 0.8) is not met on this machine.** The CPU LLM is near memory-bandwidth bound in fp32; bf16/fp16 on CPU
  were far slower (11–15 s prefill), and fp16 autocast on MPS (tried on torch 2.7.1) was slower than fp32.
- Load: ~11–14 s including a warm-up synthesis; process RSS ~5.6–7.5 GB plus ~2.1 GB MPS driver memory.

## Upstream workarounds (see `tts_worker/engine.py` docstring)

Module placement, sampling on a CPU copy of the scores, `token_hop_len` reset per request, LLM thread stopped on
cancel, the final flow pass skipped after cancel, and CosyVoice's text-bearing log lines dropped (`logsafe.py`).

## Tests

```sh
.venv/bin/python -m pytest            # textnorm + FAKE-engine protocol tests + real-model tests (~1.5 min)
.venv/bin/python bench.py --device hybrid --runs 3
```

The real-model tests start `python -m tts_worker` as a subprocess and check audio duration/non-silence, streaming,
cancel, auth, that a unique phrase never reaches the worker log, and that the process has no non-loopback sockets.
Sample WAVs are written to `tests/fixtures/audio/tts_*.wav` (gitignored).
