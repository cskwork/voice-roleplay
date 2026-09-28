# libritts_r_1188_m — 후보 (기본 목소리 아님, 제품 책임자 승인 전 사용 금지)

- Dataset: LibriTTS-R (OpenSLR SLR141), https://www.openslr.org/141/, subset `test_clean.tar.gz`
  (md5 `4d373d453eb96c0691e598061bbafab7` per the official `md5sum.txt`; mirror https://openslr.elda.org/resources/141/).
  Only the first speakers of a streamed read of the archive were extracted; the full archive was not kept.
- Clip: `LibriTTS_R/test-clean/1188/133604/1188_133604_000045_000001.wav`. `prompt.wav` is a byte-identical copy
  (sha256 `11ee126ea8a637bf14c41f219630f8ebde332ca1c8a92ebfcd5e8e215d4d3fea`, 7.08 s, 24 kHz mono PCM16).
  Not listed in the dataset's `test-clean_bad_sample_list.txt` (failed speech restoration).
- `prompt.txt` is the normalized transcript from `1188_133604.trans.tsv` (identical to the original transcript).
  Qwen3-ASR-0.6B on `prompt.wav`: 0 word errors of 25.
- Speaker: LibriTTS reader 1188, gender M (`speakers.tsv`), LibriVox reader listed as "Duncan Murrell".
  Source: LibriVox project 825 / Project Gutenberg 20019, John Ruskin, "Lectures on Landscape", Lecture III: Color.
- Licence: CC BY 4.0 (stated on https://www.openslr.org/141/, retrieved 2026-09-28). The underlying LibriVox
  recordings are public domain in the USA (https://librivox.org/pages/public-domain/).
- Attribution text (required by CC BY 4.0): "Voice prompt from LibriTTS-R (Y. Koizumi, H. Zen, S. Karita et al.,
  2023), https://www.openslr.org/141/, speaker 1188, licensed CC BY 4.0. Derived from LibriTTS and LibriVox
  recordings." LibriTTS-R audio is the LibriTTS audio processed by the Miipher speech-restoration model.
- Mode: `zero_shot`.
- Open questions for the product owner: voice likeness / personality rights are separate from copyright. The reader
  donated recordings to the public domain but did not agree to voice cloning. Whether that is acceptable is a product
  decision; whether synthesized speech counts as an adaptation needing the CC BY notice is also not settled here
  (the attribution above is suggested either way).
