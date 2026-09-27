#!/usr/bin/env bash
# Dev setup for the TTS worker: vendored CosyVoice at the pinned commit + the uv environment.
# Model files are downloaded separately into models/ (see models.lock.json / contracts/PROTOCOL.md §1).
set -euo pipefail

COSYVOICE_REPO=https://github.com/QwenAudio/CosyVoice.git   # formerly FunAudioLLM/CosyVoice (redirects)
COSYVOICE_COMMIT=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
MATCHA_COMMIT=dd9105b34bf2be2230f4aa1e4769fb586a3c824e       # third_party/Matcha-TTS submodule at that commit

here="$(cd "$(dirname "$0")" && pwd)"
vendor="$here/../../vendor/CosyVoice"

if [ ! -d "$vendor/.git" ]; then
  git clone "$COSYVOICE_REPO" "$vendor"
fi
git -C "$vendor" fetch --quiet origin
git -C "$vendor" checkout --quiet "$COSYVOICE_COMMIT"
git -C "$vendor" submodule update --init --recursive
test "$(git -C "$vendor/third_party/Matcha-TTS" rev-parse HEAD)" = "$MATCHA_COMMIT"

cd "$here"
uv sync --frozen
echo "TTS worker ready: VR_WORKER_TOKEN=... .venv/bin/python -m tts_worker"
