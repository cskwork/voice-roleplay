# Pronunciation worker (word timing, prosody, experimental GOP)

Local worker on `127.0.0.1:8714` (contract: `contracts/PROTOCOL.md` §12) for TRIAGE PA-1 (word timings for compare playback),
PA-5 (pitch contours as reference data) and PA-2/PA-3 (phone-level goodness of pronunciation behind an experimental flag).
`/align` and `/prosody` give **timings and measurements, not judgments** (PRD §8.2). Forced alignment always succeeds for
any text, so a successful alignment says nothing about whether a word was pronounced correctly (PRD §8.2, §4).

`POST /assess` returns raw, uncalibrated GOP values per expected phone (for the gateway and offline benchmarks, never for
display) and a word `band` **only** when `VR_PRON_EXPERIMENTAL=1` and a calibration file is loaded. The only calibration
is fitted on speechocean762 (Mandarin-L1 speakers). Nothing has been measured on Korean learners (PA-0 data does not
exist), so bands are off by default and no accuracy for Korean learners may be claimed.

## Run

```sh
cd workers/pronunciation
./setup.sh                                  # uv sync --frozen; compiles pyworld (needs Xcode Command Line Tools)
VR_WORKER_TOKEN=$(openssl rand -hex 16) .venv/bin/python -m pron_worker
```

Every request, `/health` included, needs `X-Worker-Token` (401 `AUTH_REQUIRED` otherwise). The aligner (and, when installed, the phone model) loads in the
background after the port opens; until then `/health` says `"ready": false` and every POST answers 503
`MODEL_NOT_READY` (`/prosody` too, although pitch analysis does not use the model). Exit codes: 2 = configuration
(missing token, model files, unknown device; the message says how to fix it), 3 = model load failure.
The worker sets `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` before any Hugging Face import and never downloads.

| Env | Default | |
|---|---|---|
| `VR_WORKER_TOKEN` | — (required) | internal auth token |
| `VR_PRON_PORT` | `8714` | host is always 127.0.0.1 |
| `PRON_ALIGNER_DIR` | `<repo>/models/Qwen3-ForcedAligner-0.6B` | |
| `PRON_DEVICE` | `auto` | `auto` = `mps` if available, else `cpu` |
| `PRON_PHONES_DIR` | `<repo>/models/wav2vec2-lv-60-espeak-cv-ft` | missing files → `/assess` answers 501, the rest works |
| `PRON_LEXICON` | `<repo>/models/cmudict/cmudict.dict` | missing → same as above |
| `PRON_CALIBRATION` | `workers/pronunciation/calibration/so762-ctcgop-2026-09-28.json` | missing → `calibration_version: null`, no bands |
| `VR_PRON_EXPERIMENTAL` | unset | `1` lets `/assess` return bands, and only with a calibration file |

## Model and licences

| Component | Pin | Licence |
|---|---|---|
| `Qwen/Qwen3-ForcedAligner-0.6B` | revision `c7cbfc2048c462b0d63a45797104fc9db3ad62b7` | Apache-2.0 (model card front matter `license: apache-2.0`) |
| `qwen-asr` (loader, `qwen_asr.Qwen3ForcedAligner`) | `0.0.6` (uv.lock) | Apache-2.0 (source header) |
| `facebook/wav2vec2-lv-60-espeak-cv-ft` (phone posteriors, `transformers.Wav2Vec2ForCTC`) | revision `ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4` | Apache-2.0 (model card front matter `license: apache-2.0`) |
| CMUdict (`cmudict.dict`, expected phones) | `cmusphinx/cmudict` commit `74790861f652b15e4ac49015a90074ad62a27690` (2025-10-24) | BSD-2-Clause-style (CMU; `models/cmudict/LICENSE`) |
| `pyworld` (Harvest F0) | `0.3.5`, built from the sdist (sha256 `1b93e53c…396aa3`); no macOS arm64 wheel exists | MIT; bundles WORLD by M. Morise, BSD-3-Clause (`lib/World/LICENSE.txt`) |

Download (setup time only; `hf download` without file names fetches nothing here):

```sh
hf download Qwen/Qwen3-ForcedAligner-0.6B README.md chat_template.json config.json generation_config.json \
  merges.txt model.safetensors preprocessor_config.json tokenizer_config.json vocab.json \
  --revision c7cbfc2048c462b0d63a45797104fc9db3ad62b7 --local-dir models/Qwen3-ForcedAligner-0.6B
```

SHA-256 on the dev machine (`model.safetensors` equals the Hugging Face LFS oid):

| File | Bytes | SHA-256 |
|---|---|---|
| README.md | 57456 | `5058416891bc47a2051557765997e8c42f8eb78a0e33c3e775bd17d4b0ba4d50` |
| chat_template.json | 1161 | `75a8cfca24f00de72d796fbfed6858fc9614ef3dabd8696684cc3bc03a9c58ff` |
| config.json | 5982 | `d616c65d46c4b90bdc651b0a0963ea932732241140f337f9bb6b0335a9c8ef09` |
| generation_config.json | 115 | `948d089b23bca1d214e768d59c4438365665f52ec6d33678f4062206b3fbbb8c` |
| merges.txt | 1671853 | `8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5` |
| model.safetensors | 1835544544 | `47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7` |
| preprocessor_config.json | 330 | `45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d` |
| tokenizer_config.json | 12666 | `3ab80063f8511deb9566e6ad438d17b7a6277fcffd52d92854112f19d36bd81c` |
| vocab.json | 2776833 | `ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910` |

Phone model and lexicon (only needed for `/assess`):

```sh
hf download facebook/wav2vec2-lv-60-espeak-cv-ft README.md config.json preprocessor_config.json pytorch_model.bin \
  special_tokens_map.json tokenizer_config.json vocab.json \
  --revision ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4 --local-dir models/wav2vec2-lv-60-espeak-cv-ft
mkdir -p models/cmudict && for f in cmudict.dict LICENSE; do curl -fL -o models/cmudict/$f \
  https://raw.githubusercontent.com/cmusphinx/cmudict/74790861f652b15e4ac49015a90074ad62a27690/$f; done
```

| File | Bytes | SHA-256 |
|---|---|---|
| wav2vec2 `pytorch_model.bin` (= Hugging Face LFS oid) | 1263535127 | `3173bde9e9ce490fa0f989e413c42f25bc1820c020adc1e6b9b87025b3cfcc5e` |
| wav2vec2 `config.json` | 1856 | `4609fb49b7e1d28aecb2840da1926c40bd915bc6f1120a940afacf7159bbfb13` |
| wav2vec2 `vocab.json` | 4637 | `d732ab2456c0c017930001dc9af0b41b3b93d25b2eb9740bf9d925508d7d87d0` |
| wav2vec2 `preprocessor_config.json` | 212 | `a2254a5b58f72cd4de3632f8eee64f3f098b7c1402128d2f419e7d00ae13e335` |
| `cmudict.dict` | 3.5 MB | `81917843c7f44ce2b094ac63873c2c7a4cf802040792c455ba3ca406891c3d22` |
| CMUdict `LICENSE` | 1.7 KB | `bd4ce8e44170a5f9f481310ca85c51de3c4f851a65e679b40e603b143bd3542a` |

The model repository only has `pytorch_model.bin` (no safetensors); transformers loads it with `torch.load(weights_only=True)`.
None of these (nor the aligner) are in `models.lock.json` yet, so `./app setup` / `./app doctor` do not know about them.

## API

Audio in every request: `audio_b64` = base64 of PCM signed 16-bit little-endian, mono, 16 000 Hz, at most 120 s
(3 840 000 bytes; more → 413 `AUDIO_TOO_LONG`). Body `Content-Type: application/json`.

- `GET /health` → `{"ready", "device": "mps|cpu", "models": {"aligner": {"model_id", "revision"}, "phones": null},
  "prosody_method": "pyworld-harvest", "calibration_version": null, "bands_enabled": false}` (`phones` = `{"model_id", "revision"}`
  of the phone model, or `null` when it is not installed)
- `POST /align {audio_b64, text}` → `{"words": [{"i", "word", "start_ms", "end_ms"}], "model_revision", "elapsed_ms"}`
  - Words are the aligner's own tokens: `text` split on whitespace, keeping only letters, digits and `'`
    (qwen-asr `Qwen3ForceAlignProcessor`); tokens with none of those are dropped. `i` counts these tokens from 0.
    Example: `"I'd like it, please!"` → `I'd`, `like`, `it`, `please`.
  - Times are multiples of 80 ms (model resolution); a word can have `start_ms == end_ms`. Starts and ends are
    non-decreasing (qwen-asr repairs out-of-order predictions); times past the end of the audio are clamped to it.
  - Language is fixed to English. `text` ≤ 4000 characters.
- `POST /prosody {audio_b64, words?}` → `{"f0_hz": [...], "hop_ms": 10, "per_word": [...], "method": "pyworld-harvest", "elapsed_ms"}`
  - `f0_hz[k]` is F0 (Hz, 0.1 Hz steps) of the frame centred at `k*10` ms, `0` = unvoiced. Search range 60–500 Hz.
  - `words` = the `words` of `/align` (only `i`, `start_ms`, `end_ms` are read). `per_word[j]` =
    `{"i", "mean_f0", "f0_range_st", "duration_ms"}` over frames centred in `[start_ms, end_ms)`: mean F0 of voiced
    frames, `12·log2(max/min)` semitones over voiced frames, and `end_ms - start_ms`. `mean_f0` and `f0_range_st`
    are `0` when the word has no voiced frame. A single octave error of the tracker inflates `f0_range_st`.
    Without `words`, `per_word` is `[]`.
  - Harvest treats a pure sine tone as unvoiced (it looks for harmonics); speech and harmonic signals are fine.
- `POST /assess {audio_b64, reference_text, mode: "scripted"|"unscripted"}` → `{"words": [{"i", "word", "start_ms", "end_ms",
  "phones": [{"expected_ipa", "heard_candidates": [{"ipa", "p"}], "gop"}], "word_gop", "band"}], "calibration_version",
  "bands_enabled", "model_revisions": {"aligner", "phones"}}` (see below). Same audio/text limits and errors as `/align`
  (`reference_text` instead of `text`); 501 `NOT_IMPLEMENTED` when the phone model or lexicon is not installed.

Errors (`{"error": {"code"}}`): `AUTH_REQUIRED` 401, `AUDIO_INVALID` 400 (missing, not base64, empty, odd byte count),
`AUDIO_TOO_LONG` 413, `BAD_REQUEST` 400 (bad JSON or fields, text over 4000 characters; 415 for a non-JSON content
type), `TEXT_EMPTY` 400 (nothing alignable in the text), `NO_SPEECH` 422 (`/align`: fewer than ~90 ms above
-40 dBFS — the aligner would still return timings for silence, and they would mean nothing), `MODEL_NOT_READY` 503,
`WORKER_FAILED` / `OUT_OF_MEMORY` 500, `NOT_IMPLEMENTED` 501.

Requests are served one at a time on one thread (model calls and pitch analysis alike), so a long `/prosody` delays
a following `/align`. Logs carry event names, `audio_ms`, word/frame counts and durations only — never text, words, timings of
individual words or audio. Uvicorn access logs are off.

## `/assess`: CTC-based GOP (experimental)

Pipeline (`pron_worker/gop.py`): align `reference_text` (same words and timings as `/align`) → expected phones per word
from CMUdict (`lexicon.py`) → phone posteriors of wav2vec2-lv-60-espeak-cv-ft over the whole take (`phones.py`; one
pass up to 30 s, longer audio in 20 s chunks with 1 s context each side) → GOP per expected phone → optional band.

- **Expected phones.** CMUdict ARPAbet mapped to the model's espeak-style units by a fixed table (`lexicon.py`), not by
  espeak-ng/phonemizer (GPL-3.0, PROTOCOL §12.1). Vowel + R inside a word is one unit (`ɑːɹ`, `ɔːɹ`, `ɛɹ`, `ɪɹ`, `ʊɹ`) as
  in the model's training labels. Each unit has **accepted variants** that are normal accent variation and must not count
  against the learner: flapped/glottal t, flapped d, reduced vowels (`ɐ`, `ᵻ`), happy-tensing (`i`), the cot–caught
  merger, rhotic vs non-rhotic vowels. `sit`/`seat` (`ɪ`/`iː`), `pull`/`pool` (`ʊ`/`uː`), r/l and similar contrasts are
  **not** merged. Words with several CMUdict pronunciations use the one with the highest CTC likelihood. Words not in
  CMUdict (names, numbers written as digits) get `phones: []`, `word_gop: null`, `band: null`; digits are not expanded.
- **GOP** (segmentation-free CTC GOP after Cao et al., arXiv:2507.16838, implemented from the paper's description, no
  code copied; the frank613/CTC-based-GOP repository has no licence and was not used): for expected unit *i*, over the
  frames of the previous word, the word and the next word (aligner timings ± 200 ms), `P_q` = CTC likelihood of the
  expected unit sequence with unit *i* replaced by *q*, for every unit *q* of the English inventory (59 units incl.
  variants) and for deletion. `gop = ln(Σ_accepted P_q / Σ_all P_q)` ≤ 0 (floor −50). `heard_candidates` = up to 3 *q*
  with the largest `P_q / Σ P_q` (`ipa: ""` = nothing heard). `word_gop` = mean of the word's unit GOPs. All values are raw
  model posteriors, **not calibrated, never for display** (PROTOCOL §12.2).
- **Bands** (`calibration.py`): a calibration file maps unit GOPs → predicted human phone accuracy (0–2) → predicted
  word accuracy (0–10) → `practice` / `check` / `good` by two thresholds. `band` is `null` unless the file is loaded **and**
  `VR_PRON_EXPERIMENTAL=1`; `/health` `bands_enabled` says which. No sentence or overall score exists in the worker.
- **`mode`** is validated but does not change the computation; `unscripted` means `reference_text` is an ASR transcript,
  which the gateway must treat as needing confirmation (PRD §8.3).
- Logs: `assess.done audio_ms words elapsed_ms` only — never text, IPA, GOP values or candidates.

### Calibration file

`calibration/so762-ctcgop-2026-09-28.json`, produced by `benchmarks/pronunciation/fit_calibration.py`: `calibration_version`,
`status`, `fitted_on` (dataset, split, speaker count, **speaker L1: Mandarin**), `korean_learner_validation: null`, model
revisions, `phone` / `word` coefficients (features fixed in `calibration.py`; a file with other features is rejected at
load, exit 3), `bands` (thresholds, how they were chosen), `eval` (train and test correlations, band confusion) and the
published baselines. Everything was fitted on speechocean762 train; test was used once, for the report
(`benchmarks/pronunciation/results/2026-09-28-speechocean762.md`). **It is not evidence of accuracy for Korean learners.**

## Measurements

Apple M3 Pro 36 GB, macOS 26.6.2, 2026-09-28, while the rest of the app stack was running on the same machine —
indicative only. Inputs are the `say` fixtures `price` (6.2 s) and `passage_30s` (28.5 s), not learner speech.
Raw data: `benchmarks/2026-09-28T134633+0000-{mps,cpu}.json` (`uv run python bench.py --device mps|cpu`).

| | MPS bf16 (default) | CPU fp32 |
|---|---|---|
| Aligner load / warm-up | 6.0 s / 0.26 s | 5.0 s / 0.35 s |
| `/align` 6.2 s audio, median of 5 | 217 ms | 607 ms |
| `/align` 28.5 s audio, median of 5 | 473 ms | 2861 ms |
| Memory after load (phys_footprint / RSS) | 2.5 GB / 0.9 GB | 4.2 GB / 4.3 GB |
| Memory after the runs (phys_footprint) | 3.5 GB | 4.2 GB |
| Harvest F0 6.2 s / 28.5 s audio, median of 3 | 2.0 s / 9.7 s | (CPU either way) |

Other single observations: worker process start → ready 10.1 s and 6.2 s in two integration-test runs (includes Python/torch import);
one ~114 s input (passage repeated 4×) aligned in 2.1 s on MPS with phys_footprint 3.7 GB. MPS bf16 and CPU fp32 gave the same
word timings on 155 of 156 words of four fixtures (one start differed by one 80 ms step). Harvest is the slow part (about 0.34× real time, so a
120 s recording needs roughly 40 s of pitch analysis); a faster tracker was not evaluated.

`/assess` (GOP scoring part, after alignment; `bench.py`, second run 2026-09-28T14:53Z,
`benchmarks/2026-09-28T145305+0000-{mps,cpu}.json`, other workloads running on the machine):

| | MPS fp32 (default) | CPU fp32 |
|---|---|---|
| Phone model + lexicon load | 1.0 s | 0.3 s |
| phys_footprint after aligner load → after phone model load → after the `/assess` runs | 2.5 → 4.6 → 5.9 GB | 4.2 → 4.3 → 6.1 GB |
| Scoring 6.2 s audio (78 units), median of 5 | 108 ms | 298 ms |
| Scoring 28.5 s audio (344 units), median of 5 | 504 ms | 1477 ms |
| Scoring 114 s audio (passage 4×, chunked, 1,376 units), 1 run | 2.1 s | 6.0 s |

MPS and CPU gave identical unit GOPs (to 3 decimals) on the 78 units of `price`. Over HTTP, `/assess` on a 3.0 s fixture
took 183–213 ms warm and a 57 s input 2.6 s (integration tests, MPS). Worker start → ready with both models: 8.3 s in one run
(the warm-up `/assess` took 0.9–2.9 s). On speechocean762 the whole pipeline ran at RTF 0.055–0.069 on MPS
(`benchmarks/pronunciation/results/2026-09-28-speechocean762.md`, which also has the accuracy numbers: calibrated
Pearson on test phone 0.480, word 0.463; the published GOPT baseline is 0.612 / 0.549; Mandarin-L1 speakers only).

Checked by the integration tests (not an accuracy measurement): the aligner returns exactly the expected tokens,
boundaries are ordered and inside the audio, speech covers most of the audio, and the inserted pauses of the
fixtures are found (2000 ms pause → 2160 ms gap; 600 ms pause → 640 ms gap). **Not measured:** timing error against
hand-labelled boundaries, and anything on Korean-learner speech (PA-0 data does not exist). For `/assess` the tests
check that a deliberately wrong reference word lowers the GOP of its first unit by more than 2 nats and that the spoken unit
appears among the heard candidates (4 cases), and that bands appear only with the flag and the calibration file.

## Tests

```sh
../../tests/fixtures/make_fixtures.sh       # generate speech fixtures (macOS say + ffmpeg)
.venv/bin/python -m pytest -m "not model"   # unit tests: FAKE aligner / FAKE scorer / FAKE posteriors (labelled), real pyworld
.venv/bin/python -m pytest                  # + real-model integration tests (~50 s; two worker processes one after the other)
```

The prosody integration test uses a CosyVoice fixture (`tts_mlx_price_dev_voice_a.wav`, 24 kHz, resampled in the test).
