# Credits and licences

## Voice

The two spoken lines in the soundtrack ("Hi there, welcome to Maple Street Coffee!" and "I'd like a large
cappuccino with oat milk, please.") are the app's own output: Fun-CosyVoice3-0.5B-2512 (Apache-2.0, MLX
conversion), speaking with the app's female voice prompt `libritts_r_4992_f`. They were copied from the app's
TTS cache (`var/cache/tts/`) and trimmed (`capture/pack.py`).

> Voice prompt from LibriTTS-R (Y. Koizumi, H. Zen, S. Karita et al., 2023), https://www.openslr.org/141/,
> speaker 4992, licensed CC BY 4.0. Derived from LibriTTS and LibriVox recordings.

The CC BY 4.0 licence text is at https://creativecommons.org/licenses/by/4.0/. Likeness caveat: see
`content/voices/libritts_r_4992_f/SOURCE.md`. The reader did not agree to voice cloning; the product owner accepted
this when adopting the voice on 2026-09-29.

## Music and sound effects

Original. Everything was synthesized from scratch with numpy/scipy in `audio/make_audio.py`. No samples, loops or
third-party music were used.

## Font

"VR Launch Sans" (`scene/fonts/vr-launch-sans.woff2`) is a glyph subset of Pretendard Variable 1.3.9 by Kil
Hyung-jin (https://github.com/orioncactus/pretendard), SIL Open Font License 1.1 (`scene/fonts/OFL.txt`). A subset
is a Modified Version under the OFL and "Pretendard" is a Reserved Font Name, so the subset was renamed. It is
still licensed under the OFL.

## Screens

All UI in the video comes from real screenshots of this app (`apps/web`), running on the real local stack with
real models (`capture/capture.mjs`). The learner's microphone input during capture was macOS `say` speech fed
through the E2E suite's fake-mic source (`tests/e2e/lib/fakeMic.js`). That speech is not part of the soundtrack.
The phone and laptop frames are drawn in CSS and are generic. They contain no Apple artwork or trademarks.

## Tools (used to build the video; none are shipped in it)

| Tool | Licence |
|---|---|
| Playwright 1.63.0 + Chromium | Apache-2.0 / BSD-3-Clause |
| FFmpeg (libx264, AAC encoder, loudnorm) | GPL-2.0+ build (used as a command-line tool only) |
| numpy, scipy | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |
| fontTools (+ brotli) | MIT |

No animation library was used. The motion system (easing, keyframes, camera, masks) is plain JavaScript in
`scene/scene.js`, and no GSAP or Remotion code is involved.
