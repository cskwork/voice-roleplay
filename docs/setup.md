# Setup and running

## Requirements

- An Apple Silicon Mac. Validated on an M3 Pro with 36 GB; smaller machines are untested.
- About 30 GB of free disk: the files pinned in [`models.lock.json`](../models.lock.json) add up to about 19 GiB, and the Python environments take about 5.5 GB.
- [uv](https://docs.astral.sh/uv/), git, Node.js 22 or newer, and `brew install llama.cpp`.
- Optional: Xcode Command Line Tools, so setup can build pyworld for the word-timing worker. Without them setup warns and finishes without that feature.
- A headset is recommended. Echo from speakers can trigger barge-in.

## Commands

```sh
./app setup    # lists what it will install and asks y/N (--yes skips the prompt)
./app doctor   # machine, runtimes, model hashes, ports, voices and offline readiness (--full re-hashes everything)
./app start    # starts the gateway and workers, waits for /health, prints http://127.0.0.1:8710
./app stop     # cancels running work and shuts every worker down
./app benchmark [...]   # benchmarks/run.sh
```

On the first start, each scenario's opening line is synthesised once and cached in `var/cache/tts/`.

## What setup does

It runs `uv sync --frozen` for each Python component, clones `vendor/CosyVoice` at a pinned commit, runs
`npm ci && npm run build` in `apps/web`, and downloads only the files listed in `models.lock.json` at pinned
revisions, checking each SHA-256. Setup is the only step that uses the network. `./app start` downloads
nothing and refuses to start if a model file or environment is missing.

## Per-component tests

| Suite | Command | Needs |
|---|---|---|
| Gateway (fake workers + real Silero VAD) | `cd services/gateway && .venv/bin/python -m pytest` | gateway env; `test_vad_real.py` also needs macOS `say` + ffmpeg; `test_integration_real.py` needs `VR_INTEGRATION=1` and the real stack |
| Feedback library | `cd workers/feedback && ../../services/gateway/.venv/bin/python -m pytest` | `test_llm_integration.py` needs llama-server on :8713 |
| Pronunciation benchmark harness | `cd benchmarks/pronunciation && ../../services/gateway/.venv/bin/python -m pytest tests` | `test_metrics.py` partly needs scipy; `test_fit_calibration.py` needs the pronunciation env |
| Scenario content | `uv run --no-project --with jsonschema --with pytest python -m pytest tests/content` | nothing |
| Web app | `cd apps/web && npm ci && npm run build && npx vitest run` | Node.js 22 |
| ASR worker | `cd workers/asr && uv run pytest -m "not model"` (add the real model: `uv run pytest`) | torch env; `model` tests need Qwen3-ASR-0.6B |
| TTS worker | `cd workers/tts && .venv/bin/python -m pytest` | `./setup.sh` (vendored CosyVoice + torch/MLX env); `test_real_model.py` needs the weights |
| Pronunciation worker | `cd workers/pronunciation && .venv/bin/python -m pytest -m "not model"` | torch env; `model` tests need Qwen3-ForcedAligner-0.6B |
| Full-stack smoke | `services/gateway/.venv/bin/python tests/integration/stack_smoke.py` | `./app start` with real models |
| Browser E2E (Playwright) | see [`tests/e2e/README.md`](../tests/e2e/README.md) | `./app start` with real models |

## Linux

The Linux + NVIDIA path (`ASR_BACKEND=vllm`, PyTorch TTS) exists in code but has never been run.
