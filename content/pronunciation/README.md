# Pronunciation guide

> **원어민·음성학 검수 필요** — `guide.json`(v1.0.0)의 조음 설명, 최소 대립쌍, 연습 문장, 한국어 번역은 초안입니다.
> 영어 원어민 검수, 한국어 검수, 음성학(조음 설명·IPA) 검수를 모두 마치기 전에는 "검수된 발음 안내"로 표시하지 않습니다.

## Review status

| file | version | English review | Korean review | Phonetics review |
|---|---|---|---|---|
| `guide.json` | 1.0.0 | 필요 (pending) | 필요 (pending) | 필요 (pending) |

When a review is done, record who reviewed it and when in this table, set the matching `review_status` field in
`guide.json` to `reviewed`, and bump `version` if any text changed. All material is original.

## What this is (and is not)

A static guide to English sounds that Korean speakers commonly find hard, with a short Korean articulation tip,
6–10 minimal pairs, and 2 practice sentences per entry. It is reference material only:

- Tips describe how to make a sound. They never say the app heard, detected, or scored anything (PRD §8.2).
  `tests/content/test_pronunciation_content.py` rejects wording such as 들렸/인식/점수.
- The guide is written for General American English (`accent: en-US`). Other accents are not wrong.
- Nothing here is evidence about a specific learner. Linking an entry to a word in an attempt (PA-4) only means
  "this sound is in the word"; any judgment must come from the pronunciation worker with its own status and
  calibration rules.

## Entries

| entry_id | Topic | target_ipa |
|---|---|---|
| `r_l` | r vs l | ɹ, l |
| `f_p` | f vs p | f, p |
| `v_b` | v vs b | v, b |
| `th_s` | θ (think) vs s | θ, s |
| `dh_d` | ð (they) vs d | ð, d |
| `z_dzh` | z vs dʒ | z, dʒ |
| `ih_ee` | ɪ vs iː | ɪ, iː |
| `uh_oo` | ʊ vs uː | ʊ, uː |
| `ae_e` | æ vs ɛ | æ, ɛ |
| `uh_ah` | ʌ vs ɑː | ʌ, ɑː |
| `s_sh` | s vs ʃ (see / she) | s, ʃ |
| `schwa` | schwa in unstressed syllables (CON-tract / con-TRACT) | ə |
| `word_stress` | noun/verb stress pairs (PRE-sent / pre-SENT) | – |
| `final_consonants` | no vowel after a final consonant (cook / cookie) | – |
| `clusters` | no vowel inside a consonant cluster (sport / support) | – |

## Format

Validated by `contracts/pronunciation_content.schema.json`.

| Field | Language | Meaning |
|---|---|---|
| `phone_set` | – | Phone inventory the IPA uses: tokens of `facebook/wav2vec2-lv-60-espeak-cv-ft` at the pinned revision, and how the IPA was produced. |
| `review_status` | – | `pending` / `reviewed` per review type. |
| `entries[].title_ko`, `why_ko`, `tip_ko` | KO | UI text. `why_ko` explains why the sound is hard for Korean speakers; `tip_ko` is the mouth/tongue tip (≤ 200 chars). |
| `entries[].title_en` | EN | English title. |
| `entries[].target_ipa` | IPA | Phones the entry contrasts. Empty for `syllable` and `stress` entries. |
| `entries[].minimal_pairs[]` | EN + IPA | `a` / `b` each have `word` and `ipa` (list of phone tokens). Stress and schwa pairs are one spelling with `label_en` (`noun`/`verb`) and `stress_en` (e.g. `PRE-sent`). |
| `entries[].practice_sentences[]` | EN + KO | `sentence_id`, `en`, `ko`. Not in the TTS text cache yet. |
| `espeak_map[]` | IPA | `{espeak, context, entry_ids}`: which entries to show for a phone. |

### espeak_map contexts

- `expected` — the reference phone (from `/assess` `phones[].expected_ipa`) at a position the UI wants to explain.
  Example: `θ` → `th_s`; `s` → `th_s`, `s_sh`; `ə` / `ɐ` / `ᵻ` → `schwa`, `word_stress`.
- `inserted_word_final` — a vowel candidate with no reference phone, after a word's last consonant → `final_consonants`.
- `inserted_in_cluster` — a vowel candidate with no reference phone, between consonants of a cluster → `clusters`.

Symbols not in the map (e.g. `t`, `k`, `eɪ`) have no guide entry. Stress is not a phone token, so `word_stress`
is only reachable through the reduced vowels; showing it from prosody is up to PA-4.

## How the IPA was produced

Every `ipa` list was generated, not typed: espeak-ng 1.52.0 (`en-us`, via `phonemizer-fork` and the
`espeakng-loader` wheel), phones separated by espeak itself, stress marks removed — the same phonemizer the
wav2vec2 espeak tokenizer uses. Noun/verb forms were phonemized as `a <word>` / `to <word>` and the article
dropped. Every symbol was checked against `vocab.json` of the pinned model revision; the schema's
`espeak_symbol` enum is that English subset plus `ɨ` / `ɯ` (vowels a Korean speaker may insert).

License note: espeak-ng and phonemizer are GPL-3.0 and are **not** a project dependency (see
`docs/pronunciation.md`). They were run once, outside the repo, as a dev-time tool; only their IPA output is
stored here. If PA-2 picks a different G2P (e.g. CMUdict-based), its expected symbols must be converted to this
phone set or `espeak_map` updated.

Known espeak limits for reviewers: `increase`, `digest`, `extract` have the same IPA for noun and verb (only
stress differs, and stress marks are removed); `below`, `derive` use `ᵻ` and `terrain`, `parade` use `ɚ`.

To regenerate one word at dev time (network needed once for the wheels; not a runtime dependency):

```sh
uv run --no-project --python 3.12 --with espeakng-loader --with phonemizer-fork python -c '
import espeakng_loader
from phonemizer.backend.espeak.wrapper import EspeakWrapper
EspeakWrapper.set_library(espeakng_loader.get_library_path()); EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
from phonemizer.backend import EspeakBackend; from phonemizer.separator import Separator
print(EspeakBackend("en-us").phonemize(["support"], strip=True, separator=Separator(phone=" ", word=" | ")))'
```

## Checks

```sh
uv run --no-project --with jsonschema --with pytest python -m pytest tests/content
```

`tests/content/test_pronunciation_content.py` checks schema validity, IPA symbols from the fixed list, Hangul in
`*_ko` fields and none elsewhere, unique prefixed ids, that each consonant/vowel pair differs in exactly one
target phone, that final-consonant and cluster pairs differ by one inserted vowel, that stress pairs are noun/verb
forms of one word, that every entry is reachable from `espeak_map`, and that this README keeps the review marker
while any review is pending.
