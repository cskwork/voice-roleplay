"""API-mode adapters (PROTOCOL §1): hosted providers behind the same interfaces as the local worker clients.

`build_api_clients(config)` returns (asr, tts, llm, llm_bg) for `build_services`: OpenAI realtime transcription,
Deepgram Aura-2 speech through OpenRouter (or Deepgram directly with `tts_provider = "deepgram"`), and the OpenRouter
LLM. Keys come from
`config.api.keys` (read from the environment at start); a missing key leaves that component "not ready" in
/api/health instead of failing the gateway start.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


def build_api_clients(config: Config):
    from vr_feedback.llm import LlmClient

    from .asr_openai import OpenAiAsr

    api = config.api
    asr = OpenAiAsr(api.key("openai"), url=api.asr_url, model=api.asr_model)
    if api.tts_provider == "deepgram":  # opt-in fallback: Deepgram directly, with its own key
        from .tts_deepgram import DeepgramTts

        tts = DeepgramTts(api.key("deepgram"), api.voices_path(), ws_url=api.tts_url, rest_url=api.tts_rest_url)
    else:  # default: Deepgram Aura-2 through OpenRouter, same key as the LLM
        from .tts_openrouter import OpenRouterTts

        tts = OpenRouterTts(api.key("openrouter"), api.voices_path(), base_url=api.tts_base_url, model=api.tts_model)
    # One client serves the conversation and background JSON work: hosted endpoints have no slots to keep apart.
    llm = LlmClient.openrouter(api.key("openrouter"), api.llm_model, providers=api.llm_providers,
                               base_url=api.llm_base_url, timeout_s=api.llm_timeout_s)
    return asr, tts, llm, llm
