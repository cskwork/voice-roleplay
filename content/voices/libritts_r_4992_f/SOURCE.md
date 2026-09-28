# libritts_r_4992_f — 기본 여성 목소리 (채택됨)

Adopted default voice, chosen by the product owner on 2026-09-29 after a listening check (CO-3, `docs/TRIAGE.md`).
Copied from `content/voices/candidates/libritts_r_4992_f/` (prompt.wav, prompt.txt, this file); measurements are in
`content/voices/candidates/README.md`.

- Dataset: LibriTTS-R (OpenSLR SLR141), https://www.openslr.org/141/, subset `test_clean.tar.gz`
  (md5 `4d373d453eb96c0691e598061bbafab7` per the official `md5sum.txt`; mirror https://openslr.elda.org/resources/141/).
  Only the first speakers of a streamed read of the archive were extracted; the full archive was not kept.
- Clip: `LibriTTS_R/test-clean/4992/41797/4992_41797_000025_000000.wav`. `prompt.wav` is a byte-identical copy
  (sha256 `f010b8d9cba71ee133ae1ba65de152b482f357b5281423d6e425ae4aa7f00607`, 6.68 s, 24 kHz mono PCM16).
  Not listed in the dataset's `test-clean_bad_sample_list.txt` (another clip of this chapter, `..._000010_000000`, is).
- `prompt.txt` is the normalized transcript from `4992_41797.trans.tsv` (identical to the original transcript).
  Qwen3-ASR-0.6B on `prompt.wav`: 0 word errors of 19.
- Speaker: LibriTTS reader 4992, gender F (`speakers.tsv`), LibriVox reader listed as "Joyce Martin".
  Source: LibriVox project 4566 / Project Gutenberg 10540, Kate Douglas Wiggin, "Mother Carey's Chickens", ch. 18.
- Licence: CC BY 4.0 (stated on https://www.openslr.org/141/, retrieved 2026-09-28). The underlying LibriVox
  recordings are public domain in the USA (https://librivox.org/pages/public-domain/).
- Attribution text (required by CC BY 4.0): "Voice prompt from LibriTTS-R (Y. Koizumi, H. Zen, S. Karita et al.,
  2023), https://www.openslr.org/141/, speaker 4992, licensed CC BY 4.0. Derived from LibriTTS and LibriVox
  recordings." LibriTTS-R audio is the LibriTTS audio processed by the Miipher speech-restoration model.
- Mode: `zero_shot`.
- Recorded decision (likeness caveat): voice likeness / personality rights are separate from copyright. The reader
  donated recordings to the public domain but did not agree to voice cloning. The product owner accepted this when
  adopting the voice on 2026-09-29. Whether synthesized speech counts as an adaptation needing the CC BY notice is
  not settled; the attribution above is shown either way (`voice.json` `license_note`, root `README.md`).
