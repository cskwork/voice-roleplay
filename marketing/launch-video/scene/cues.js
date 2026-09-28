// Shared timeline (seconds). The scene animates from it and audio/make_audio.py places music and SFX from it,
// so picture and sound stay in sync. 120 BPM: one beat = 0.5 s; the groove's bar 1 starts at 3.0 s.
// The object literal below is plain JSON (audio/make_audio.py parses it).
window.CUES = {
  "duration": 30.0,
  "bpm": 120,
  "grooveStart": 3.0,
  "acts": {
    "hook": [0.0, 3.0],
    "intro": [3.0, 7.0],
    "live": [7.0, 10.0],
    "barge": [10.0, 12.5],
    "hints": [12.5, 15.5],
    "goals": [15.5, 18.0],
    "summary": [18.0, 21.0],
    "recorded": [21.0, 24.0],
    "private": [24.0, 27.0],
    "brand": [27.0, 30.0]
  },
  "words": [0.28, 0.40, 0.52, 0.64, 1.68, 1.82, 2.2],
  "hits": [3.0, 27.0],
  "whooshes": [2.6, 6.6, 9.95, 12.45, 15.4, 17.95, 20.95, 23.85],
  "callouts": { "voice": 4.55, "live": 7.55, "reply": 8.75, "barge": 11.25, "drill": 19.75, "compare": 21.25, "contour": 22.45 },
  "cut": 11.25,
  "hintCards": [12.8, 13.3, 13.8],
  "dings": [16.25, 16.75, 17.25],
  "strike": 18.9,
  "fix": 19.25,
  "priv": [24.3, 24.8, 25.3],
  "chips": [25.85, 26.0, 26.15],
  "riser": [25.4, 27.0],
  "cta": 28.2,
  "voice": { "opening": 3.35, "model_read": 21.3 },
  "flip": { "start": 7.05, "step": 0.17 }
}
