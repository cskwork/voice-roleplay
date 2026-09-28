# vctk_p311_m — 후보 (기본 목소리 아님, 제품 책임자 승인 전 사용 금지)

- Dataset: CSTR VCTK Corpus 0.92, University of Edinburgh, https://doi.org/10.7488/ds/2645
  (https://datashare.ed.ac.uk/handle/10283/3443, `VCTK-Corpus-0.92.zip`, 11,747,302,977 bytes). Only the needed
  members were fetched from the zip with HTTP range requests; the full archive was not downloaded.
- Clip: `wav48_silence_trimmed/p311/p311_006_mic1.flac` (DPA 4035 omni microphone, 48 kHz, 16 bit, end-pointed by
  the corpus authors; sha256 of the FLAC `c55d819ed3b5af77c26c6977456e5e774fee33c176206749cde31049daa6a995`).
  `prompt.wav` is a lossless FLAC-to-PCM16 conversion with identical samples
  (sha256 `7b4b989c1ba9209f46c6e8c21c2bbcb477d03aea3576d93623ab35962de23dd2`, 6.77 s, 48 kHz mono).
- `prompt.txt` is `txt/p311/p311_006.txt` (a sentence of the Rainbow Passage, read by all VCTK speakers).
  Qwen3-ASR-0.6B on `prompt.wav`: 0 word errors of 17.
- Speaker: p311, age 21, M, American, region Iowa (`speaker-info.txt`). No name is published.
- Licence: CC BY 4.0. The corpus README: "This corpus is licensed under the Creative Commons License: Attribution
  4.0 International"; the DataShare record `dc.rights`: "Creative Commons Attribution 4.0 International Public
  License" (both retrieved 2026-09-28).
- Attribution text (required by CC BY 4.0), using the corpus's own citation: "Voice prompt from the CSTR VCTK Corpus
  (version 0.92), Yamagishi, Veaux, MacDonald, University of Edinburgh, https://doi.org/10.7488/ds/2645, speaker
  p311, licensed CC BY 4.0."
- Mode: `zero_shot`.
- Open questions for the product owner: voice likeness / personality rights are separate from copyright. The corpus
  was recorded for speech-synthesis research ("English Multi-speaker Corpus for CSTR Voice Cloning Toolkit"), but the
  participants' consent terms are not published with it and were not checked. Whether an app may speak in this
  speaker's voice is a product decision. Accent: American English but recorded in Edinburgh; the rest of the corpus
  is mostly British.
