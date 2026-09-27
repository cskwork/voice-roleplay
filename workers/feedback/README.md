# vr_feedback

This package handles text for voice-roleplay. It is used by the gateway, needs no model weights, and takes only httpx, pydantic and numpy as dependencies.

| Module | Purpose |
|---|---|
| `segmenter` | Splits streamed LLM text into speakable TTS segments (PROTOCOL §6.3) |
| `tts_text` | Spells out prices, times, dates, ordinals and symbols for TTS |
| `metrics` | WPM and pauses from VAD intervals (`metrics_version` m1) |
| `reading` | Word diff of target vs. transcript, labelled "다르게 인식된 부분" |
| `echo` | Measures overlap between a barge-in transcript and the AI line that was playing |
| `llm` | Async llama-server client: streaming with cancel, prefix warm-up, JSON schema output |
| `prompts` | Roleplay messages with a byte-stable scenario prefix |
| `feedback`, `goals`, `hints` | Evidence-checked feedback, goal status and 3-level hints |

```sh
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e . pytest pytest-asyncio
.venv/bin/python -m pytest -m "not integration"      # pure unit tests
.venv/bin/python -m pytest -m integration            # needs llama-server on 127.0.0.1:8713 (see config/llm)
.venv/bin/python bench/bench_llm.py --trials 5 --out bench/results/llm_bench.json
```

Set `VR_LLM_URL` and `VR_LLM_API_KEY` when the server runs somewhere else or has `LLAMA_API_KEY` set.
If the server is unreachable, the integration tests skip.
