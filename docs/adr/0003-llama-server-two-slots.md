# 0003. One llama-server with two fixed slots: conversation on slot 0, background work on slot 1

Status: accepted (commits ecd86a2 and a16fae9; `config/llm/server.json`)

## Context

During a conversation the same 4B model has to answer the learner with low latency. It also has to check
goal progress, keep a rolling summary and optionally give per-turn feedback. Running two model servers would
double memory on a machine that already uses about 22.7 GB with every model loaded (benchmark, "Memory").
Sharing one context lets background prompts evict the conversation's cached prefix, so the next reply has to
re-process the whole prompt.

## Decision

Start `llama-server` with `-c 8192 -np 2 --no-kv-unified` (`config/llm/server.json`). That gives two slots,
each with its own 4096-token KV cache. Every request sets `cache_prompt: true`, and `id_slot` is set where a
slot is given (`workers/feedback/src/vr_feedback/llm.py`). The realtime engine always uses slot 0 and
pre-warms the scenario prompt into it at `session.start` (`_warm_llm` in `realtime.py`). The gateway's
background client is `LlmClient(..., default_slot=1)` (`services/gateway/src/vr_gateway/app.py`). Background
work starts after a reply is generated, while it plays (`_after_response`).

## Consequences

- Goal checks and summaries never cost the conversation its prefix cache.
- Each slot is capped at 4096 tokens. The roleplay prompt keeps 12 history entries word for word
  (`HISTORY_WINDOW`) and folds older turns into a rolling summary.
- Background requests queue behind each other on slot 1. The session summary runs feedback and goals in
  parallel on both slots, about 5.5 s (`config/llm/README.md`).
- llama-server answers CORS for any origin, so it gets the per-start worker token as `LLAMA_API_KEY`.

## Alternatives considered

- **One slot for everything:** background prompts would evict the roleplay prefix.
- **A second, smaller model for background work:** more memory and another pinned artifact. Not tried.
- **A unified KV cache (llama-server's default):** the 8192 tokens would be shared between slots, so the fixed
  4096-token budget per slot (PRD §11) would not be guaranteed. `config/llm/README.md` records the chosen
  setting as confirmed in the server log (`n_ctx_slot = 4096, kv_unified = 'false'`).
