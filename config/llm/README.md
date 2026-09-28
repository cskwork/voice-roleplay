# LLM server (llama.cpp `llama-server`)

Roleplay replies and text feedback both use one local `llama-server` running
`Qwen3-4B-Instruct-2507-Q4_K_M.gguf` (unsloth third-party GGUF conversion, see `models.lock.json`).
`server.json` holds the launch command. The gateway reads it and starts the process.

## Command

```sh
LLAMA_API_KEY="$VR_WORKER_TOKEN" llama-server \
  -m models/Qwen3-4B-Instruct-2507-GGUF/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8713 -c 8192 -np 2 --no-kv-unified \
  --jinja -ngl 99 --no-webui --cache-prompt
```

The flags were checked against `llama-server --help` of the installed Homebrew build
(`version: 0.5.0 (build 11146, commit 7fe450e19)`). `llama-server --list-devices` shows `MTL0: Apple M3 Pro`.

| Flag | Why |
|---|---|
| `--host 127.0.0.1 --port 8713` | Local only (PROTOCOL §1). |
| `-c 8192 -np 2 --no-kv-unified` | Two slots, each with its own 4096-token context (PRD §11 budget). The server log confirms `n_slots = 2, n_ctx_slot = 4096, kv_unified = 'false'`. The gateway pins slot 0 to live roleplay replies and slot 1 to background work during a session (goals, hints, per-turn feedback, rolling summary). |
| `--jinja` | Uses the GGUF's embedded chat template. This is the default, but it is listed so the setting is explicit. |
| `-ngl 99` | Puts all 36 layers on Metal. |
| `--no-webui` | No bundled web UI. |
| `--cache-prompt` | Prompt/KV prefix reuse. This is the default, but it is listed because roleplay latency depends on it. |
| `LLAMA_API_KEY` | `llama-server` answers CORS preflights for **any** `Origin` (checked with `curl -H "Origin: http://evil.example"`), so without a key any web page in the user's browser could call the model. With the key set, `/v1/*` and `/slots` return 401 without `Authorization: Bearer <key>`, and `/health` stays open. Pass the same key to `vr_feedback.llm.LlmClient(api_key=...)`. |

Logging: at the default verbosity (3) the server logs slot ids, token counts and timings only.
After the test suite and the benchmark, `grep -c -i -E "maple|yesterday|oat milk|latte|ignore previous|barista"`
on the server log returned 0. Do not add `-v`, `-lv 4+` or `--log-prompts-dir`, because those would log prompts (PROTOCOL §2).

## How clients call it

`workers/feedback` (`vr_feedback.llm.LlmClient`) sends `POST /v1/chat/completions` with `cache_prompt: true`
and `id_slot` when given. Roleplay streams (`max_tokens` 128). Feedback uses
`response_format: {"type": "json_schema", ...}` (grammar-constrained, `max_tokens` 768). The roleplay prompt is
built so that the system prompt and scenario opening line form a byte-stable prefix. `LlmClient.warm()` loads
that prefix into the slot's KV cache at `session.start`.

## Measured (Apple M3 Pro 36 GB, macOS 26.6.2, 2026-09-27, 5 trials, medians)

Other agents' workers may have been loaded on the same GPU at the time, so treat these as indicative.
Raw data: `workers/feedback/bench/results/llm_bench_2026-09-27.json` (run `workers/feedback/bench/bench_llm.py`).

| Measure | Result |
|---|---|
| Roleplay TTFT, cold prefix (~697 prompt tokens processed) | 1584 ms |
| Prefix warm-up call (`warm()`) | 1407 ms (done at session start, off the critical path) |
| Roleplay TTFT, warmed prefix, first turn (82 new tokens, 619 cached) | 264 ms |
| Roleplay TTFT, second turn (102 new tokens, 641 cached) | 316 ms |
| Full roleplay reply (median 19 tokens) | 815 ms (warm, first turn) / 983 ms (second turn) |
| Decode speed | ~35 tok/s (roleplay and 128-token generation) |
| Cold prompt processing | ~443 tok/s |
| `generate_feedback` JSON (one transcript) | 3.0 s |
| `evaluate_goals` | 4.2 s |
| `session_summary` (feedback + goals in parallel on 2 slots) | 5.5 s |
