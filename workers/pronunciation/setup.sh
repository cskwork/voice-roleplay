#!/usr/bin/env bash
# Dev setup for the pronunciation worker: the uv environment from uv.lock.
# pyworld 0.3.5 has no macOS arm64 wheel and is compiled from source. uv's managed Python points the compiler at a
# fixed SDK path (e.g. CommandLineTools/SDKs/MacOSX15.sdk) that may not exist, so pass the installed SDK explicitly.
# Model files are downloaded separately (README.md: Qwen/Qwen3-ForcedAligner-0.6B, and for /assess
# facebook/wav2vec2-lv-60-espeak-cv-ft + CMUdict, all at pinned revisions).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
if [ "$(uname -s)" = Darwin ]; then
  sdk="$(xcrun --sdk macosx --show-sdk-path)" || { echo "Xcode Command Line Tools are required: xcode-select --install" >&2; exit 1; }
  export CFLAGS="-isysroot $sdk" CXXFLAGS="-isysroot $sdk" LDFLAGS="-isysroot $sdk"
fi
uv sync --frozen
echo "Pronunciation worker ready: VR_WORKER_TOKEN=... .venv/bin/python -m pron_worker"
