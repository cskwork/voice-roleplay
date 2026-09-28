# ljspeech_f — 후보 (기본 목소리 아님, 제품 책임자 승인 전 사용 금지)

- Dataset: The LJ Speech Dataset 1.1, https://keithito.com/LJ-Speech-Dataset/ (archive
  `https://data.keithito.com/data/speech/LJSpeech-1.1.tar.bz2`, 2,748,572,632 bytes, last-modified 2018-02-19).
  Only this clip and `metadata.csv` were extracted from a streamed read of the archive; the full archive was not kept.
- Clip: `LJSpeech-1.1/wavs/LJ029-0150.wav`. `prompt.wav` is a byte-identical copy
  (sha256 `a3f076441f8e7edec640d9b274e42c82cce70b2eea403e27030cb13f19c9a9cf`, 6.71 s, 22.05 kHz mono PCM16).
- `prompt.txt` is the `metadata.csv` transcript of LJ029-0150 (raw and normalized columns are identical).
  Qwen3-ASR-0.6B on `prompt.wav`: 0 word errors of 18.
- Speaker: single female speaker, US English. The dataset page credits "Recordings by Linda Johnson from LibriVox".
  Source text: Report of the President's Commission on the Assassination of President Kennedy (1964).
- Licence (quoted from the dataset page, retrieved 2026-09-28): "This dataset is in the public domain in the US (and
  most likely other countries as well). There are no restrictions on its use." and "All text, audio, and annotations
  are in the public domain. [...] As this work is in the public domain, you may use it without attribution."
  LibriVox (https://librivox.org/pages/public-domain/, retrieved 2026-09-28): "all our recordings are public domain in
  the USA, but not necessarily in other countries".
- Attribution (not required; suggested for the credits page): "Voice prompt from the LJ Speech Dataset (Keith Ito,
  2017), recordings by Linda Johnson for LibriVox, public domain."
- Mode: `zero_shot`.
- Open questions for the product owner:
  - Copyright status outside the US (Korea) is stated by the sources as "most likely" / "not necessarily"; not verified.
  - Voice likeness / personality rights are separate from copyright: the reader donated recordings to the public
    domain, but did not agree to having an AI speak in her voice. Whether that is acceptable is a product decision.
  - This voice is one of the most widely used TTS training voices, so some listeners may recognise it.
