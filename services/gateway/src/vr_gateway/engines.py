"""Which engine runs each part of API mode (PROTOCOL §1): one pure function shared by the gateway and `./app`.

API mode used to mean three hosted providers. Since 2026-11-26 it means "no big local LLM/TTS workers", and each part
picks its engine on its own:

- llm: `claude-cli` (default: the Claude Code CLI logged in on this machine, no API key), `codex-cli` (the Codex CLI
  logged in on this machine), `anthropic` (Claude API, ANTHROPIC_API_KEY) or `openrouter` (OPENROUTER_API_KEY).
- asr: `auto` (default) = `openai` when OPENAI_API_KEY is set, else `local` (the Qwen3-ASR worker on this machine).
- tts: `auto` (default) = `openrouter` when OPENROUTER_API_KEY is set, else `kokoro` (Kokoro-82M on this machine);
  `deepgram` stays an explicit opt-in.

Settings come from VR_API_LLM_PROVIDER / VR_API_ASR_PROVIDER / VR_API_TTS_PROVIDER, then the [api] table of
config.toml (`llm_provider`, `asr_provider`, `tts_provider`). Only key presence is ever read here, never a value.
No third-party imports: `scripts/app_tool.py` loads this file with the launcher's plain Python.
"""

from __future__ import annotations

from collections.abc import Mapping

LLM_PROVIDERS = ("claude-cli", "codex-cli", "anthropic", "openrouter")
ASR_PROVIDERS = ("auto", "openai", "local")
TTS_PROVIDERS = ("auto", "openrouter", "deepgram", "kokoro")
DEFAULTS = {"llm": "claude-cli", "asr": "auto", "tts": "auto"}
CHOICES = {"llm": LLM_PROVIDERS, "asr": ASR_PROVIDERS, "tts": TTS_PROVIDERS}
ENV = {"llm": "VR_API_LLM_PROVIDER", "asr": "VR_API_ASR_PROVIDER", "tts": "VR_API_TTS_PROVIDER"}
# Environment variable holding each hosted provider's key.
KEY_ENV = {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "deepgram": "DEEPGRAM_API_KEY",
           "anthropic": "ANTHROPIC_API_KEY"}
# Engine -> the key it needs (engines missing here run on this machine or through a logged-in CLI).
NEEDS_KEY = {"openrouter": "openrouter", "anthropic": "anthropic", "openai": "openai", "deepgram": "deepgram"}


def _has(environ: Mapping[str, str], provider: str) -> bool:
    return bool(environ.get(KEY_ENV[provider], "").strip())


def configured(environ: Mapping[str, str], table: Mapping[str, object]) -> dict[str, str]:
    """The configured setting of each part (before `auto` is resolved). Raises ValueError for an unknown value."""
    out = {}
    for part, default in DEFAULTS.items():
        value = (environ.get(ENV[part], "").strip() or str(table.get(f"{part}_provider", "") or "").strip()
                 or default).lower()
        if value not in CHOICES[part]:
            raise ValueError(f"{part}_provider must be one of {CHOICES[part]}, got {value!r}")
        out[part] = value
    return out


def resolve(environ: Mapping[str, str], table: Mapping[str, object]) -> dict[str, str]:
    """{"llm", "asr", "tts"} -> the engine that will run, with `auto` decided by which keys are present."""
    eng = configured(environ, table)
    if eng["asr"] == "auto":
        eng["asr"] = "openai" if _has(environ, "openai") else "local"
    if eng["tts"] == "auto":
        eng["tts"] = "openrouter" if _has(environ, "openrouter") else "kokoro"
    return eng


def required_keys(engines: Mapping[str, str]) -> list[str]:
    """Hosted providers whose key the resolved engines need, in a stable order."""
    need = {NEEDS_KEY[e] for e in engines.values() if e in NEEDS_KEY}
    return [p for p in KEY_ENV if p in need]


def summary(engines: Mapping[str, str]) -> str:
    """One line for `./app doctor` and the gateway start log."""
    llm = {"claude-cli": "Claude CLI (this machine's login)", "codex-cli": "Codex CLI (this machine's login)",
           "anthropic": "Claude API", "openrouter": "OpenRouter"}
    asr = {"openai": "OpenAI realtime transcription", "local": "local Qwen3-ASR"}
    tts = {"openrouter": "Deepgram Aura-2 via OpenRouter", "deepgram": "Deepgram Aura-2 direct",
           "kokoro": "local Kokoro-82M"}
    return (f"api (LLM {llm[engines['llm']]}; ASR {asr[engines['asr']]}; TTS {tts[engines['tts']]}; "
            f"Silero VAD local)")
