# 0001. Run CosyVoice3 on MLX by default, keep PyTorch as the fallback

Status: accepted (commit a16fae9, 2026-09-28)

## Context

TTS sits on the critical path of every reply. The learner hears nothing until the first audio chunk is ready,
and playback stalls whenever synthesis runs slower than real time. The PRD target is a per-reply real-time
factor (RTF) of p95 ≤ 0.8. The official Fun-CosyVoice3-0.5B-2512 code is PyTorch. On the M3 Pro it ran on
CPU + MPS ("hybrid", `workers/tts/tts_worker/engine.py`) and measured stream RTF p50/p95 1.60/2.13, a first
chunk at 2.76 s p50, and stalls on 24 of 24 requests (`workers/tts/README.md`, "Measurements").

## Decision

Add an MLX backend (`workers/tts/tts_worker/engine_mlx.py`) that loads the mlx-community conversion of the
same weights through `mlx-audio-plus`. It follows the upstream PyTorch inference path step by step instead of
the port's `generate` wrapper. `tts_worker/backend.py` chooses it under `VR_TTS_BACKEND=auto` when running on
Apple Silicon, when `mlx` is importable, and when the MLX files are present. Defaults are fp16, 5 flow-matching
steps (upstream uses 10) and a first streaming chunk of 50 speech tokens. The PyTorch engine stays as the
fallback.

## Consequences

- Engine-level benchmark, 48 requests: stream RTF p50/p95 0.55/0.72, first chunk 1.15/1.33 s, 0 stalls,
  ASR round-trip WER 0.6% (`workers/tts/README.md`).
- In the full-stack run with every model loaded, per-reply RTF p95 was 0.87, which misses the 0.8 target.
  50 of 134 uninterrupted replies still stalled for more than 50 ms
  (`benchmarks/results/2026-09-29-m3pro.md`).
- A first chunk of 50 tokens instead of 25 costs about 0.3 s of first audio but removes nearly all stalls.
- There are two engines to keep in step. The MLX path was checked against upstream: text token ids are
  identical, prompt speech tokens match 95.4% / 94.4% (the resampler differs), and speaker-embedding cosine
  is 0.993 / 0.998.
- Two packages that would otherwise conflict share one venv (`pyproject.toml` overrides `mlx-audio` away).

## Alternatives considered

- **PyTorch only (hybrid CPU + MPS):** too slow, numbers above.
- **MLX with the upstream 10 flow steps:** stream RTF p50 1.03, with stalls on 24 of 24 requests.
- **8-bit MLX weights:** similar speed at 6 steps and no clear gain over fp16. Kept as an option
  (`VR_TTS_MLX_VARIANT=8bit`).
- **Running the TTS LLM in its own thread, as upstream does:** RTF improved about 5%, but the first chunk got
  about 0.2 s later, because the flow pass holds the GPU.
