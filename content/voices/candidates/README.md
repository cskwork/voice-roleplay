# Voice candidates (CO-3) — 후보, 기본 목소리 아님

Candidate English prompt voices with documented reuse rights, to replace the development voices `dev_voice_a` /
`dev_voice_b`. Nothing here is used by the app: the default voices are unchanged. Adopting a candidate needs the
product owner's decision on voice likeness (see below) and a listening check.

| id | gender, accent | source | licence | attribution | prompt |
|---|---|---|---|---|---|
| `ljspeech_f` | F, US | LJ Speech 1.1 `LJ029-0150` | public domain (US; "most likely" elsewhere) | not required | 6.71 s, 22.05 kHz |
| `libritts_r_4992_f` | F, US (LibriVox) | LibriTTS-R test-clean, speaker 4992 | CC BY 4.0 | required | 6.68 s, 24 kHz |
| `libritts_r_1188_m` | M, US (LibriVox) | LibriTTS-R test-clean, speaker 1188 | CC BY 4.0 | required | 7.08 s, 24 kHz |
| `vctk_p311_m` | M, American (Iowa) | CSTR VCTK 0.92, `p311_006_mic1` | CC BY 4.0 | required | 6.77 s, 48 kHz |

Each folder has `prompt.wav` (the dataset's own clip, unedited), `prompt.txt` (the dataset's transcript),
`voice.json` (`mode: zero_shot`), `SOURCE.md` (URL, licence quote, attribution text, speaker id, hashes, retrieval
date 2026-09-28, likeness caveat) and `samples/NN_<kind>.mp3` (64 kbps listening copies of the 5 test sentences).

## Measurements

`evaluate.py` (run 2026-09-28 14:02 UTC, Apple M3 Pro, with the app stack and other agents running): MLX TTS engine
in-process with the worker's defaults (`mlx-community/Fun-CosyVoice3-0.5B-2512-fp16` @ `18ccb7fb`, 5 flow steps,
first chunk 50 tokens, streamed), then Qwen3-ASR-0.6B on the lossless output.

Test sentences (`evaluate.py`): price "That comes to $24.99, and you can pay by card or in cash."; date "The meeting
has been moved to Tuesday, October 14th, 2026."; contraction "I'm sorry, but we don't have that size, and it won't
arrive until next week."; question "Would you like to sit by the window, or would you rather sit near the door?";
long "When you get off the train, walk two blocks north, turn left at the bakery on the corner, and you'll see the
museum entrance right across from the small park." (92 reference words after number expansion).

| id | prompt ASR check | round-trip WER (5 sentences) | speaker similarity mean (min–max) | earlier run mean | RTF median |
|---|---|---|---|---|---|
| `ljspeech_f` | 0 / 18 words wrong | 0% (0 / 92) | 0.898 (0.836–0.944) | 0.908 | 0.57 |
| `libritts_r_4992_f` | 0 / 19 | 0% (0 / 92) | 0.833 (0.688–0.914) | 0.816 | 0.55 |
| `libritts_r_1188_m` | 0 / 25 | 0% (0 / 92) | 0.764 (0.717–0.827) | 0.759 | 0.55 |
| `vctk_p311_m` | 0 / 17 | 0% (0 / 92) | 0.900 (0.850–0.929) | 0.882 | 0.56 |

- WER: reference and ASR output both go through the worker's `textnorm.normalize` (so "$24.99" and "twenty-four
  dollars and ninety-nine cents" match), then lowercase words. Negative control: the ASR text of one prompt scored
  against another prompt's transcript gives 24 errors of 25 words, so the scoring does count errors. With 92 words
  per voice, 0 errors only says all four are intelligible to this ASR; it does not separate them.
- Speaker similarity: cosine of CAM++ embeddings (weights from the MLX checkpoint, as in the worker) between
  `prompt.wav` and each output; higher = output sounds more like the prompt speaker to that model. It is not a
  quality or naturalness score. For reference, the dev voices scored 0.78–0.79 on the bench sentences
  (`workers/tts/README.md`), a different sentence set. "Earlier run" is a first run of the same script 5 minutes
  before; sampling makes each run differ.
- RTF: wall time / audio length per sentence on a shared machine; indicative only.
- Prompt ASR check: Qwen3-ASR-0.6B on `prompt.wav` against `prompt.txt`, to confirm the transcript matches the audio.
- Signal levels (20 ms frames; the clips contain little silence, so the "floor" is mostly quiet speech): LJ peak
  −5.1 dBFS, quietest-10% −54.6 dBFS; LibriTTS-R 4992 −0.9 / −46.3; LibriTTS-R 1188 −0.9 / −45.0 (no samples at
  full scale); VCTK p311 −3.9 / −52.0.
- Not done: nobody has listened to the prompts or the samples. No naturalness/MOS rating, no Korean-learner
  listening test.

## Recommendation (subject to a listening check and the product owner's likeness decision)

- Female: `ljspeech_f`. Public domain, no attribution needed, highest speaker similarity of the two female
  candidates in both runs. Downsides: 22.05 kHz source (no content above 11 kHz), and it is a very widely used TTS
  voice that some listeners may recognise. Alternative: `libritts_r_4992_f` (CC BY 4.0, 24 kHz, lower similarity).
- Male: `vctk_p311_m`. CC BY 4.0, recorded for speech-synthesis research in a hemi-anechoic room, similarity 0.88–0.90
  vs 0.76 for `libritts_r_1188_m`. Needs the attribution in `SOURCE.md`.

## Rights: what is and is not settled

- Copyright/licence: verified from the official pages on 2026-09-28 (quotes in each `SOURCE.md`). CC BY 4.0
  candidates need the attribution text shown somewhere in the app (e.g. a credits screen).
- Voice likeness / personality rights are a separate question from copyright and are **not** settled by any of these
  licences. LibriVox readers donated recordings to the public domain but did not agree to voice cloning; VCTK
  participants recorded for a synthesis corpus, but their consent terms are not published and were not checked.
  The product owner must decide whether the app may speak in these real people's voices.
- Public-domain status of LJ Speech / LibriVox outside the US (Korea) is described by the sources themselves as
  "most likely" / "not necessarily"; not verified.

## Adopting a candidate

Copy the folder to `content/voices/<id>/` (the TTS worker reads `content/voices/*/`), change the `voice.json` label
and `license_note`, and point the scenarios' `default_voice_id` at it.

**Warning:** `load_voices()` in `workers/tts/tts_worker/engine.py` reads `voice.json` in *every* subdirectory of
`content/voices/`, including this `candidates/` folder, which has none. Until that function skips folders without
`voice.json`, a TTS worker (re)start while this folder exists fails to load the model (`FileNotFoundError` for
`content/voices/candidates/voice.json`, checked 2026-09-28): the worker process stays up but cannot speak. The
already-running worker is not affected. Proposed one-line fix in `load_voices()`: iterate only
`p.is_dir() and (p / "voice.json").exists()`.

## Re-running

```sh
cd workers/tts && .venv/bin/python ../../content/voices/candidates/evaluate.py [voice_id ...]
```

Needs `workers/tts` (MLX backend files) and `workers/asr/.venv` + `models/Qwen3-ASR-0.6B`. Writes `results.json` and
the `samples/`; transcripts are compared in memory and never printed or stored. The raw dataset files are not kept
in the repo; `SOURCE.md` lists the exact archive members.
