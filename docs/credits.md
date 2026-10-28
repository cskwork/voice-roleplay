# Credits and licences

| Component | Source | Licence |
|---|---|---|
| Speech recognition | [Qwen/Qwen3-ASR-0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B) | Apache-2.0 |
| Conversation | [Qwen3-4B-Instruct-2507](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF), GGUF Q4_K_M by unsloth, served by [llama.cpp](https://github.com/ggml-org/llama.cpp) | Apache-2.0 / MIT |
| Voice | [FunAudioLLM/Fun-CosyVoice3-0.5B-2512](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512), MLX conversion by [mlx-community](https://huggingface.co/mlx-community/Fun-CosyVoice3-0.5B-2512-fp16) | Apache-2.0 |
| Turn-taking | [Silero VAD](https://github.com/snakers4/silero-vad) (ONNX) | MIT |
| Word timing (optional) | [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B), [pyworld](https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder) | Apache-2.0 / MIT |
| Phones (experimental) | [facebook/wav2vec2-lv-60-espeak-cv-ft](https://huggingface.co/facebook/wav2vec2-lv-60-espeak-cv-ft), [CMUdict](https://github.com/cmusphinx/cmudict) | Apache-2.0 / BSD-style |

Every model file, revision, hash and licence note is pinned in [`models.lock.json`](../models.lock.json).

**Voices.** The two AI voices are LibriTTS-R speakers, used as voice prompts under CC BY 4.0:

> Voice prompt from LibriTTS-R (Y. Koizumi, H. Zen, S. Karita et al., 2023), https://www.openslr.org/141/, speaker 4992, licensed CC BY 4.0. Derived from LibriTTS and LibriVox recordings.
>
> Voice prompt from LibriTTS-R (Y. Koizumi, H. Zen, S. Karita et al., 2023), https://www.openslr.org/141/, speaker 1188, licensed CC BY 4.0. Derived from LibriTTS and LibriVox recordings.

The original readers did not specifically agree to voice cloning; the product owner chose these voices knowing that (`content/voices/*/SOURCE.md`).

**Fonts and video.** The website and launch video use subsets of [Pretendard](https://github.com/orioncactus/pretendard) by Kil Hyung-jin (SIL Open Font License 1.1), renamed as the licence requires. The launch video's music and sound effects were synthesised from scratch; see [`marketing/launch-video/CREDITS.md`](../marketing/launch-video/CREDITS.md).

**This repository.** Code and content are licensed under the [Apache License 2.0](../LICENSE); third-party material is listed in [`NOTICE`](../NOTICE). Model weights are not included: `./app setup` downloads them from their publishers, and each keeps its own licence.
