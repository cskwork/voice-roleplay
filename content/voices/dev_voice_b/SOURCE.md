# dev_voice_b — 개발용 — 출시 전 권리 확인된 음성으로 교체 필요

- Source: `asset/cross_lingual_prompt.wav` in https://github.com/QwenAudio/CosyVoice (formerly FunAudioLLM/CosyVoice)
  at commit `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc` (original sha256
  `353a7715c2e4811f4045658b29d1ce67ecad5120e09de10ce890f1763aab486c`, 13.75 s, 22.05 kHz mono s16).
- `prompt.wav` = first 5.75 s of that file, cut inside a pause (silence 5.53–5.99 s) with
  `ffmpeg -i cross_lingual_prompt.wav -t 5.75 -c:a pcm_s16le prompt.wav`
  (sha256 `ae7221973eecc61fd8efc1e0886fd8eaa92d97692b9dc3889d03c94cf69c9336`). The full 13.75 s prompt made synthesis
  about 2x slower because the flow model re-processes the prompt on every streamed chunk.
- Language: Mandarin Chinese.
- `prompt.txt` is **ASR-derived** (Qwen3-ASR-0.6B on the trimmed file), not an upstream transcript; upstream ships no
  transcript for this asset. It is not used for synthesis: this voice runs in `cross_lingual` mode, where CosyVoice
  drops the prompt text and prompt speech tokens from the LLM input and uses only the speaker embedding and the
  flow prompt (see `frontend_cross_lingual` in `cosyvoice/cli/frontend.py`).
- Rights: the repository code is Apache-2.0, but the speaker and the usage rights of this recording are not
  documented upstream. Development only; replace with a rights-cleared English voice before release.
