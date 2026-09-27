#!/usr/bin/env bash
# Generate English speech fixtures (macOS `say` + ffmpeg) into tests/fixtures/audio/.
# Reference texts go to audio/<name>.txt; audio/ is gitignored and can be regenerated any time.
set -euo pipefail

command -v say >/dev/null || { echo "macOS 'say' is required" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is required (brew install ffmpeg)" >&2; exit 1; }

here="$(cd "$(dirname "$0")" && pwd)"
out="$here/audio"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$out"

voice="${FIXTURE_VOICE:-Samantha}"

# name|text  ("[[slnc 1500]]" is a say(1) embedded command: 1.5 s pause, not spoken)
fixtures=(
  "short_answer|I would like a medium latte, please."
  "mid_pause|I think the best part of my weekend was visiting my grandmother. [[slnc 2000]] We cooked dinner together and talked about her garden and her old photos for hours."
  "price|The total comes to twelve dollars and fifty cents, and the sandwich costs four dollars and seventy five cents."
  "passage_30s|Last summer I traveled to Seattle with two of my friends. We stayed in a small hotel near the waterfront, and every morning we walked to a different coffee shop. On the second day we took a ferry to an island, where we rented bicycles and rode along the coast. The weather was cool and cloudy, but nobody complained. In the evening we ate fresh seafood and talked about our plans for the next year. It was one of the most relaxing trips I have ever taken, and I hope we can go back there again next summer with a few more friends."
)

for entry in "${fixtures[@]}"; do
  name="${entry%%|*}"
  text="${entry#*|}"
  say -v "$voice" -o "$tmp/$name.aiff" "$text"
  ffmpeg -loglevel error -y -i "$tmp/$name.aiff" -ar 16000 -ac 1 -sample_fmt s16 "$out/$name.wav"
  # Reference text without say(1) embedded commands.
  printf '%s\n' "$text" | sed -E 's/\[\[[^]]*\]\] ?//g' > "$out/$name.txt"
done

# 1 s of digital silence.
ffmpeg -loglevel error -y -f lavfi -i anullsrc=r=16000:cl=mono -t 1 -sample_fmt s16 "$out/silence_1s.wav"
printf '\n' > "$out/silence_1s.txt"

# Resampling variants of the short answer (44.1 kHz stereo, 48 kHz mono).
ffmpeg -loglevel error -y -i "$tmp/short_answer.aiff" -ar 44100 -ac 2 -sample_fmt s16 "$out/short_answer_44k_stereo.wav"
ffmpeg -loglevel error -y -i "$tmp/short_answer.aiff" -ar 48000 -ac 1 -sample_fmt s16 "$out/short_answer_48k.wav"
cp "$out/short_answer.txt" "$out/short_answer_44k_stereo.txt"
cp "$out/short_answer.txt" "$out/short_answer_48k.txt"

for f in "$out"/*.wav; do
  dur="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")"
  printf '%-28s %6.2fs\n' "$(basename "$f")" "$dur"
done
