"""API-mode adapters (PROTOCOL §1): engines behind the same interfaces as the local worker clients.

`build_api_clients(config)` returns (asr, tts, llm, llm_bg) for `build_services`, one engine per part as resolved by
`vr_gateway.engines` (config.api.llm_provider / asr_provider / tts_provider):

- llm: Claude CLI or Codex CLI logged in on this machine (no key), the Claude API (ANTHROPIC_API_KEY) or OpenRouter.
- asr: OpenAI realtime transcription, or None for "local" (build_services then uses the local Qwen3-ASR worker, which
  the supervisor starts in API mode for that case).
- tts: Deepgram Aura-2 through OpenRouter, Deepgram directly, or Kokoro-82M on this machine.

Keys come from `config.api.keys` (read from the environment at start); a missing key leaves that component "not
ready" in /api/health instead of failing the gateway start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


def build_llm(config: Config):
    api = config.api
    if api.llm_provider == "claude-cli":
        from .llm_cli import ClaudeCliLlm

        return ClaudeCliLlm(api.claude_cli_model, pool=api.cli_pool, timeout_s=api.llm_timeout_s,
                            workdir=config.run_dir / "llm-cli")
    if api.llm_provider == "codex-cli":
        from .llm_cli import CodexCliLlm

        return CodexCliLlm(api.codex_cli_model, pool=api.cli_pool, timeout_s=api.llm_timeout_s,
                           workdir=config.run_dir / "llm-cli")
    if api.llm_provider == "anthropic":
        from .llm_anthropic import AnthropicLlm

        return AnthropicLlm(api.key("anthropic"), api.anthropic_model, timeout_s=api.llm_timeout_s)
    from vr_feedback.llm import LlmClient

    return LlmClient.openrouter(api.key("openrouter"), api.llm_model, providers=api.llm_providers,
                                base_url=api.llm_base_url, timeout_s=api.llm_timeout_s)


def build_tts(config: Config):
    api = config.api
    if api.tts_provider == "deepgram":  # opt-in: Deepgram directly, with its own key
        from .tts_deepgram import DeepgramTts

        return DeepgramTts(api.key("deepgram"), api.voices_path(), ws_url=api.tts_url, rest_url=api.tts_rest_url)
    if api.tts_provider == "kokoro":  # no speech key: Kokoro-82M on this machine
        from .tts_kokoro import KokoroTts

        return KokoroTts(api.kokoro_model, api.kokoro_voices, api.voices_path())
    from .tts_openrouter import OpenRouterTts

    return OpenRouterTts(api.key("openrouter"), api.voices_path(), base_url=api.tts_base_url, model=api.tts_model)


def build_api_clients(config: Config):
    api = config.api
    asr = None
    if api.asr_provider == "openai":
        from .asr_openai import OpenAiAsr

        asr = OpenAiAsr(api.key("openai"), url=api.asr_url, model=api.asr_model)
    # One client serves the conversation and background JSON work: hosted endpoints and CLIs have no slots to keep
    # apart.
    llm = build_llm(config)
    return asr, build_tts(config), llm, llm
