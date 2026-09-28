# Launch video

A 30-second launch video for 말하기 연습 (voice-roleplay), built from real screens of the app. There are two cuts:
1920x1080 and 1080x1920, both 60 fps H.264 with AAC audio at -14 LUFS.

The video is a web page (`scene/`). Every frame is a pure function of time `t`. `render/render.mjs` opens the page
in headless Chromium, draws each frame, takes a screenshot and pipes it to ffmpeg. There are no CSS transitions,
no timers and no randomness, so re-rendering gives the same frames. See `CREDITS.md` for licences.

## Re-render

Rendering needs only what is committed: packed screens, voice lines and the font.

```sh
cd marketing/launch-video
npm ci                                   # Playwright 1.63.0 (uses the Chromium already installed for tests/e2e)
python3 audio/make_audio.py              # -> audio/build/mix.wav (+ scene/voice_env.js)
node render/render.mjs                   # -> out/voice-roleplay-launch-1080p.mp4 + out/poster.png   (~11 min)
node render/render.mjs --format vertical # -> out/voice-roleplay-launch-vertical.mp4 + out/poster-vertical.png
pngquant --quality 80-95 --force --ext .png out/poster*.png   # keep the committed posters small (~330 KB)
```

For a quick look without a full render:

```sh
node render/render.mjs --stills 3.5,8.9,16.9,28.9      # PNGs in .cache/stills/
node render/render.mjs --from 7 --to 10                # partial MP4 in .cache/
python3 -m http.server -d scene 8000                    # open http://127.0.0.1:8000/?t=12.9 (&format=vertical)
```

Needs Node 22, Python 3 with numpy, scipy, Pillow and fontTools, and ffmpeg with libx264.

## Re-capture the app (only when the UI changes)

Capture needs the real stack running (`./app start`, every worker ready). The script waits until no other
realtime session is active and never ends someone else's session. It holds one realtime conversation per device
size (about 2 minutes each) and does one reading attempt.

```sh
node capture/capture.mjs            # raw PNGs + element rects -> assets/captures/ (gitignored); bursts -> .cache/burst/
python3 capture/pack.py             # picks the screens the video uses -> scene/media/*.webp + rects.json, voices -> audio/voice/
python3 scene/fonts/subset.py       # after any change to on-screen copy (needs the Pretendard source, see the script)
```

If a new capture changes the UI layout, check the hard-coded anchor points in `scene/scene.js` (callout pins,
word-chip boxes, goal check marks, `GOAL_PTS`, `RW`) against the new `scene/media/rects.json`.

## Layout

| Path | What |
|---|---|
| `scene/cues.js` | Shared timeline: act boundaries and every sync point (callouts, dings, whooshes, voice lines). Both the picture and the sound read it. |
| `scene/scene.js` | Motion system (easing, keyframe camera tracks, masked kinetic type, callouts, drawn phone and laptop) and all shots |
| `scene/media/` | Packed real screens (WebP q90) and element rects |
| `audio/make_audio.py` | Procedural score and SFX, voice ducking, two-pass loudnorm |
| `audio/voice/` | The app's own TTS lines, trimmed (CC BY 4.0 voice, see CREDITS) |
| `capture/` | Real-app capture (Playwright + the E2E fake-mic source) and packing |
| `render/render.mjs` | Frame-by-frame renderer |
| `out/` | Posters and MP4s (committed; the MP4s are also attached to the GitHub release) |

## Shot list

120 BPM (one beat is 0.5 s). The groove starts on the hit at 3.0 s.

| Time | Act | Shot |
|---|---|---|
| 0.0 to 3.0 | Hook | Dark stage and a live waveform. Kinetic type: "Speak English out **loud.**" (the brand box wipes in behind "loud."), then "Nobody's listening." and "Not even the cloud." The waveform collapses into a point, and an iris opens from it on the first hit. |
| 3.0 to 7.0 | Intro | The phone rises with a 3D tilt while the real scenario cards float at different depths. The app's AI voice says "Hi there, welcome to Maple Street Coffee!" and rings pulse on the AI-speaking indicator in time with it. "Introducing 말하기 연습". Callout: "Natural AI voice". |
| 7.0 to 10.0 | 01 Real-time roleplay | The camera pushes into the captions. A flip-book of real burst screenshots shows the partial caption growing ("Hi." → "Hi, can I get a large latte?") into the final caption, then the AI reply streams in. Callouts: "Live caption", "AI replies out loud". |
| 10.0 to 12.5 | 02 Barge-in | A whip pan to the real barge-in state ("AI · 말하다 멈춤 / 여기서 멈춤"). At 11.25 s the music stops like tape and the phone jolts. Callout: "AI stopped here". |
| 12.5 to 15.5 | 03 Hint ladder | Warm tint. Three real hint-panel pieces climb in on beats: Korean tip, then key words, then the example sentence "How much is that altogether?". |
| 15.5 to 18.0 | 04 Goals | The laptop flies in and the camera zooms to the real goal list. Goals check off on three bell dings (1/3, 2/3, 3/3) with ring bursts and a counter. |
| 18.0 to 21.0 | 05 Session summary | The phone shows the real summary suggestion. "I want pay with my phone." is struck through and "I'd like to pay with my phone." wipes in. Callout on 다시 말하기. |
| 21.0 to 24.0 | 06 Recorded practice | The real result screen. A focus ring moves across the word chips in time with the app's model voice reading the sentence, then the camera pans down and the pitch contour draws on. Callouts: "My take ↔ model voice", "Pitch contour (reference only)". |
| 24.0 to 27.0 | Private | A dark stage wipes up. Laptop (home) and phone (summary) sit in parallax. "Runs on your Mac." / "Works offline." / "Audio never saved." land on beats, followed by the model chips (Qwen3-ASR, CosyVoice3, Qwen3-4B · llama.cpp). A riser builds and the devices collapse to the centre. |
| 27.0 to 30.0 | Brand | Hit. An indigo iris opens, the app's mic mark springs in, then "말하기 연습", "voice-roleplay", "Private English speaking practice.", the CTA `$ ./app start` with a blinking caret, and a Korean footer. |

## Honesty notes

- Everything on screen is real app output captured on 2026-09-29. The LLM's Korean gloss in the level-1 hint
  had translation errors that day, so the video shows only the hint's authored tip, key words and example.
- There is no pronunciation score, and the video says so: "No scores" and "reference only".
- The app is a local web app that runs on a Mac (validated on Apple Silicon). The phone frame shows its mobile
  layout. The app binds to 127.0.0.1 and is not a phone app, so none of the copy claims it runs on a phone.
