# dev_voice_a — 개발용 — 출시 전 권리 확인된 음성으로 교체 필요

- Source: `asset/zero_shot_prompt.wav` in https://github.com/QwenAudio/CosyVoice (formerly FunAudioLLM/CosyVoice;
  the old URL redirects) at commit `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`. The file was last changed upstream in
  commit `b02d7e6` (2025-12-12, "update prompt wav").
- `prompt.wav` is a byte-identical copy (sha256 `c7b31d6dbe7cc6a716dded00550db5b50940bf209e424e4ad207b12e657c8ff6`,
  3.48 s, 24 kHz mono float32).
- Language: Mandarin Chinese (no English prompt audio is bundled in the repo).
- `prompt.txt` is the transcript the upstream `example.py` pairs with this file (`cosyvoice3_example`):
  `希望你以后能够做的比我还好呦。` Qwen3-ASR-0.6B heard `希望你以后能够做得比我还好哟。` (homophones only).
- Mode: `zero_shot` (prompt transcript + prompt speech tokens are given to the model; the worker prefixes the
  CosyVoice3 system prompt `You are a helpful assistant.<|endofprompt|>`).
- Rights: the repository code is Apache-2.0, but the speaker and the usage rights of this recording are not
  documented upstream. Development only; replace with a rights-cleared English voice before release.
