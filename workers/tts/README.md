# TTS worker (Fun-CosyVoice3-0.5B-2512)

Implements `contracts/PROTOCOL.md` §4 on `127.0.0.1:8712`, with two backends behind the same API:

- `mlx` (default on Apple Silicon): the MLX conversion `mlx-community/Fun-CosyVoice3-0.5B-2512-fp16` of the same
  official weights, run with the CosyVoice3 model classes of `mlx-audio-plus` 0.1.8 (`tts_worker/engine_mlx.py`).
- `torch`: the official PyTorch code (vendored CosyVoice) on CPU + MPS (`tts_worker/engine.py`).

## Setup and run

```sh
cd workers/tts
./setup.sh                                   # vendor/CosyVoice @ 074ca6dc + uv env (Python 3.10, torch 2.3.1, mlx on arm64 macOS)
VR_WORKER_TOKEN=<token> .venv/bin/python -m tts_worker
```

Nothing is downloaded at runtime. Model files (revisions pinned, reported by `/health`):

| Backend | Directory | Repo @ revision |
|---|---|---|
| torch | `models/Fun-CosyVoice3-0.5B-2512` | `FunAudioLLM/Fun-CosyVoice3-0.5B-2512` @ `29e01c4e…` |
| mlx | `models/Fun-CosyVoice3-0.5B-2512-mlx-fp16` | `mlx-community/Fun-CosyVoice3-0.5B-2512-fp16` @ `18ccb7fb…` (third-party conversion) |
| mlx 8bit (optional) | `models/Fun-CosyVoice3-0.5B-2512-mlx-8bit` | `mlx-community/Fun-CosyVoice3-0.5B-2512-8bit` @ `177baf27…` |

The mlx backend also needs `speech_tokenizer_v3.onnx` from the official directory (prompt speech tokens).
`hf download <repo> --local-dir <dir>` without file names fetched nothing here, so name the files:

```sh
hf download mlx-community/Fun-CosyVoice3-0.5B-2512-fp16 .gitattributes README.md config.json model.safetensors \
  tokenizer.json tokenizer_config.json --revision 18ccb7fbd7246e8cd3420d02f5dd28595cc0fcd9 \
  --local-dir models/Fun-CosyVoice3-0.5B-2512-mlx-fp16
```

| Env | Default | |
|---|---|---|
| `VR_WORKER_TOKEN` | required | checked on every HTTP request and on the WS upgrade (401 otherwise) |
| `VR_TTS_PORT` | `8712` | |
| `VR_TTS_BACKEND` | `auto` | `auto` = `mlx` on Apple Silicon when mlx is installed and the MLX files are present, else `torch` |
| `VR_TTS_MLX_VARIANT` | `fp16` | `fp16` or `8bit` (8bit quantizes only the Qwen2 LLM layers) |
| `VR_TTS_FLOW_STEPS` | `5` | mlx: flow-matching ODE steps (upstream uses 10; see measurements) |
| `VR_TTS_TOKEN_HOP` | `50` | mlx: first streaming chunk in speech tokens (25 tokens = 1 s); later chunks double up to 4x (upstream: 25) |
| `VR_TTS_DEVICE` | `auto` | torch: `auto` = `hybrid` when MPS is available, else `cpu` |
| `VR_TTS_CPU_THREADS` | `4` | torch: CPU threads (4 was fastest for the CPU-side LLM on M3 Pro; 6/11 were slower) |

Dependencies: one venv. `mlx-audio-plus` ships its own `mlx_audio` package but declares `mlx-audio[all]` (a
different project that installs into the same directory and needs transformers ≥ 5), so `pyproject.toml`
overrides that dependency away and keeps CosyVoice's librosa/soundfile pins. Upstream `mlx-audio` (checked 0.2.9 and
0.5.6) has no CosyVoice3; the model card names `mlx-audio-plus`.

## API notes

- `GET /health` adds `placement` (per-module device) and `load_seconds`. With mlx: `model_id` is the MLX repo,
  `revision` its commit, `device` = `"mlx"`.
- `WS /synthesize`: requests on one connection are queued and run in order (max 8 queued). `cancel` works for the
  active or a queued request; `cancelled` is sent immediately and no frames for that request follow it. Binary frames
  carry at most 100 ms of PCM16 at 24 kHz. Error codes: `MODEL_NOT_READY, VOICE_NOT_FOUND, TEXT_NOT_ENGLISH,
  TEXT_TOO_LONG, TEXT_EMPTY, INVALID_SPEED, BAD_REQUEST, DUPLICATE_REQUEST, QUEUE_FULL, WORKER_FAILED`.
- `POST /synthesize` returns a 24 kHz mono PCM16 WAV. It uses CosyVoice's non-stream mode (faster for whole WAVs).
- `speed` (0.5–2.0): CosyVoice only supports speed in non-stream mode (mel time-stretch, pitch kept), so a WS
  request with `speed != 1.0` sends its audio as one chunk per sentence instead of streaming. Both backends.
- Text is normalized in `tts_worker/textnorm.py` (prices, percentages, times, dates, years, ordinals, decimals, phone
  numbers, plain numbers). Hangul is rejected, CosyVoice control markup (`<|…|>`, `[breath]`, tags) is stripped.
  Upstream's `wetext` normalizer is not installed: it downloads its FST files from ModelScope when constructed.
- Cancel: torch stops the LLM at the next token; mlx checks after every LLM token and every chunk, so it stops
  within one token or one flow pass (a flow pass cannot be interrupted).

## Voices

`content/voices/libritts_r_4992_f` (female) and `libritts_r_1188_m` (male) are English LibriTTS-R clips (CC BY 4.0,
attribution required; the text is in each `SOURCE.md` and `license_note`), both `zero_shot` with the dataset
transcript. The product owner chose them on 2026-09-29 from the candidates in `content/voices/candidates/`. `voice.json`
holds label, license note and mode. Both backends use the same files and modes. Until 2026-09-29 the defaults were
`dev_voice_a` / `dev_voice_b`, two Mandarin prompt clips bundled in the CosyVoice repo with undocumented rights
(removed); the checks and measurements below that mention "dev voices" or "voice a / b" were made on those.

## MLX backend: what it does and what was checked

`engine_mlx.py` uses the port's model classes but not its `Model.generate` wrapper (no streaming, ignores speed,
trims and resamples the prompt differently, and computes the prompt mel with fmax 8 kHz where `cosyvoice3.yaml`
says `fmax: null`, i.e. 12 kHz). It follows the upstream PyTorch path instead; the module docstring lists each
step. Checked against the upstream code on the two former dev voices (Mandarin prompts, see Voices):

- Text token ids: identical to upstream `CosyVoice3Tokenizer` for the prompt prefix + English text and for the
  Mandarin prompt transcript.
- Prompt speech tokens: the MLX S3 tokenizer (built from the official ONNX) matches onnxruntime exactly on the
  same mel. The full prompt path matches upstream on 95.4% / 94.4% of tokens (voice a / b); the difference comes
  from the resampler (scipy `resample_poly` here, torchaudio upstream).
- Prompt mel (80 bins, fmax 12 kHz, voice a): max abs difference 0.03 vs upstream `matcha` mel (5.8 with the wrapper's fmax 8 kHz).
- Speaker embedding (CAM++ weights from the MLX checkpoint): cosine 0.993 / 0.998 vs the official `campplus.onnx`.
- Flow noise: a fixed buffer like upstream; the port's fallback re-seeds the global RNG on every flow pass.
- Running the LLM in a separate thread as upstream does was tried: the LLM is starved while a flow pass holds the
  GPU, RTF improved ~5% and first-chunk latency got ~0.2 s worse, so it runs in the synthesis thread.

## Measurements (Apple M3 Pro 36 GB, macOS 26.6)

`bench.py`, engine level, 12 sentences of 10–26 words × 2 voices, one request at a time. RTF = wall time ÷ audio
duration per request (PRD §15.1 target: p95 ≤ 0.8). "Stall" = total time a player starting at the first chunk
would wait for audio. The machine was not idle: desktop apps (WindowServer, Chrome) share the GPU, load average
1.4–8.5, and the same setting varied by up to ~40% between windows. Results JSON: `benchmarks/`.

Chosen default, `mlx` fp16, 5 flow steps, first chunk 50 tokens (`…-mlx-default-fp16-s5-h50-runs2.json`, 48 requests):

| | stream RTF p50 / p95 | first chunk p50 / p95 | stalls | non-stream RTF p50 / p95 | ASR WER |
|---|---|---|---|---|---|
| mlx fp16, 5 steps, hop 50 | **0.55 / 0.72** | **1.15 / 1.33 s** | 0 of 48 | 0.37 / 0.46 | 0.6% (5 / 844 words) |
| torch hybrid (same sentences, `…-torch-m-torch-hybrid.json`) | 1.60 / 2.13 | 2.76 / 2.98 s | 24 of 24, 63 s | 1.18 / 1.49 | 0% |

Settings compared (24 requests each; WER from the ASR round trip, 422 words per setting):

| mlx setting | run | stream RTF p50 / p95 | first chunk p50 | stalls (runs, total) | WER |
|---|---|---|---|---|---|
| fp16, 10 steps, hop 25 (upstream) | sequential | 1.03 / 1.25 | 1.45 s | 24, 23.1 s | 0% |
| fp16, 6 steps, hop 25 | sequential | 0.68 / 0.83 | 0.99 s | 12, 3.0 s | 0% |
| fp16, 5 steps, hop 25 | sequential | 0.61 / 0.70 | 0.87 s | 10, 0.6 s | 0.7% |
| fp16, 5 steps, hop 50 | sequential | 0.55 / 0.64 | 1.16 s | 0 | 0.5% |
| fp16, 10 steps, hop 50 | sequential | 0.89 / 1.17 | 1.80 s | 12, 8.1 s | 0% |
| 8bit, 10 steps, hop 25 | sequential | 1.03 / 1.20 | 1.40 s | 24, 17.7 s | 0.2% |
| 8bit, 6 steps, hop 25 | sequential | 0.63 / 0.77 | 0.89 s | 10, 1.0 s | 0.7% |
| fp16, 10 / 6 / 5 steps, hop 25 | interleaved, busier | 1.17 / 1.50, 0.80 / 0.99, 0.74 / 0.84 | 1.57, 1.18, 1.01 s | 24, 12, 11 | 0%, 0.2%, 0% |
| fp16, 10 / 6 / 5 steps, hop 50 | interleaved, busier | 0.98 / 1.22, 0.68 / 0.85, 0.64 / 0.77 | 2.03, 1.46, 1.33 s | 13, 7, 6 | 0.5%, 0%, 0% |

"Interleaved" = one process, the six settings take turns on every sentence (`--flow-steps 10,6,5 --token-hop 25,50`),
so they share the same machine conditions. A separate 5-step/hop-25 run during heavy desktop GPU use measured
0.89 / 1.14; hop 50 is the default because it keeps RTF p95 under 0.8 in the busier windows and removes nearly
all stalls, for ~0.3 s later first audio.

Trade-offs:
- Flow steps dominate: each streamed chunk re-runs the DiT over prompt + all tokens so far (10 steps, batch 2 for
  CFG); the LLM runs at ~125 tokens/s (5x real time). With the same LLM tokens, 5 steps change the mel by
  0.15 mean abs (6 steps 0.11, 4 steps 0.32; mel std 2.65) and leave speaker similarity unchanged (CAM++ cosine to
  the prompt 0.78 for 10, 6, 5 and 4 steps). WER stays ≤ 0.7% for every setting (differences are 0–3 words, within
  sampling noise); speaker similarity of the streamed bench audio is 0.78–0.79 for every mlx setting and torch.
  Nobody has listened to the outputs yet; do a listening check before release.
- 8bit only speeds up the LLM, which is not the bottleneck: ~7% at 6 steps, none at 10 steps.
- Chunk size: hop 50 vs 25 costs ~0.3 s first audio and saves one flow pass per sentence.
- Memory (mlx fp16 default): process RSS ~2.2 GB, macOS phys_footprint 2.1–3.1 GB, MLX peak 3.5 GB; load 3–5 s
  including warm-up. torch hybrid: RSS 9.0 GB, phys_footprint 12.3 GB (MPS driver 3.2 GB), load 10.6 s.

ASR round trip: `../asr/.venv/bin/python asr_roundtrip.py benchmarks/<run>.json` transcribes the streamed WAVs
(`var/tts-bench/`, gitignored) with Qwen3-ASR-0.6B and adds per-setting WER to the JSON; the sentences contain no
digits or number words so number formatting cannot count as errors. Transcripts are never printed or stored.

### torch backend device choices (measured earlier, 5 sentences × 2 voices, other agents running, load avg 4–7)

- MPS for everything is not possible: HiFT's F0 predictor needs float64. With LLM and flow both on MPS, the LLM
  thread and the flow thread crash Metal (`failed assertion ... commit call`), on torch 2.3.1 and 2.7.1.
- `hybrid` = LLM on CPU (fp32, 4 threads, ~30–35 tokens/s; the model needs 25 tokens/s for real time), flow (DiT)
  on MPS, HiFT on CPU. hybrid: stream first chunk p50 2.97 s, stream RTF p50/p95 1.91/2.83; cpu: 5.79 s,
  3.82/4.44. The CPU LLM is near memory-bandwidth bound in fp32; bf16/fp16 on CPU were far slower (11–15 s
  prefill), and fp16 autocast on MPS (tried on torch 2.7.1) was slower than fp32.

## Upstream workarounds (torch, see `tts_worker/engine.py` docstring)

Module placement, sampling on a CPU copy of the scores, `token_hop_len` reset per request, LLM thread stopped on
cancel, the final flow pass skipped after cancel, and CosyVoice's text-bearing log lines dropped (`logsafe.py`).

## Tests

```sh
.venv/bin/python -m pytest            # textnorm, backend selection, FAKE-engine protocol tests,
                                      # real-model tests for torch and mlx (~2 min)
.venv/bin/python bench.py                                   # default backend and settings
.venv/bin/python bench.py --backend mlx --flow-steps 10,6,5 --token-hop 25,50 --runs 1
.venv/bin/python bench.py --backend torch --device hybrid
```

The real-model tests run once per backend (a backend without its model files is skipped). They start
`python -m tts_worker` as a subprocess and check `/health` (model id + pinned revision), audio duration/non-silence,
speed, streaming, cancel (no frames after `cancelled`, next request served), auth, that a unique phrase never
reaches the worker log, and that the process has no non-loopback sockets. Sample WAVs are written to
`tests/fixtures/audio/tts_<backend>_<name>_<voice_id>.wav` (gitignored); the pronunciation worker's prosody test reads
`tts_mlx_price_libritts_r_4992_f.wav`. `bench.py` writes its streamed outputs to `var/tts-bench/<run>/`.
