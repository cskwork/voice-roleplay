# Pronunciation benchmarks (PA-0, PA-2, PA-3)

Offline evaluation of pronunciation scorers against human ratings. Nothing here runs in the app.

| Path | Purpose |
|---|---|
| `fetch_speechocean762.py` | Downloads speechocean762 into `data/speechocean762` and verifies every file (setup time only, stdlib) |
| `so762.py` | Loader: published train/test splits, speaker metadata, human phone/word/sentence scores, 16 kHz audio |
| `metrics.py` | Pearson, Spearman (average ranks for ties), per-group distributions, binary detection (miss rate, precision) |
| `run_eval.py` | `--system <name>`: runs a scorer over a split, caches its outputs, writes a numbers-only report |
| `scorers/` | Scorer protocol (`scorers/__init__.py`), the `asr_diff` baseline and the `gop_ctc*` systems (PA-2/PA-3) |
| `fit_calibration.py` | PA-3: fits the GOP calibration on train, evaluates on test, writes `workers/pronunciation/calibration/<version>.json` |
| `results/` | Reports (`<system>-<split>.{json,md}`) |
| `korean_set/` | Collection protocol, Korean consent form, rating guide, agreement method and manifest schema for the Korean-learner set. **No data exists yet.** |

`data/` is gitignored. Scorer output caches (which may contain transcripts of the public corpus) go to `data/pron_runs/`; reports contain numbers only.

## Dataset: speechocean762

OpenSLR 101, CC BY 4.0. 5,000 read English sentences by 250 Mandarin-L1 speakers (about half children, ages 6–43), each item scored independently by five experts; `resource/scores.json` stores their average/median.

| Split | Utterances | Speakers (under 18) | Words | Phones | Audio |
|---|---|---|---|---|---|
| train | 2,500 | 125 (58) | 15,849 | 47,076 | 2.88 h |
| test | 2,500 | 125 (64) | 15,967 | 47,369 | 2.69 h |

The splits are speaker-disjoint (checked on every run). All speakers are Mandarin L1, so **no result here says anything about Korean learners** (TRIAGE §2).

```sh
python3.12 benchmarks/pronunciation/fetch_speechocean762.py     # ~500 MB download, ~10 s extract + verify
```

Source and verification: the corpus GitHub repository at pinned commit `613968e` (2025-10-26), which OpenSLR 101 itself recommends as the latest version. It includes the 2024-07-16 fix of illegal phoneme symbols in the `mispronunciations` blocks of `scores.json`. OpenSLR publishes no checksum, so the script checks all 5,263 files against the git blob SHA-1s of the pinned commit's tree and writes `data/speechocean762/VERIFIED.json` (blob-list SHA-256 `0b53410f…62ed`). The archive fetched on 2026-09-28 had SHA-256 `823e9aa5…d1b5` (GitHub generates it on the fly, so this may change; the per-file check is the real guarantee). The OpenSLR download ran at about 17 KB/s from every mirror tried, so it was stopped; the 60 WAV files it had delivered were byte-identical to the GitHub copy (only `README.md` and `.gitignore` differ).

## Running a scorer

```sh
workers/asr/.venv/bin/python benchmarks/pronunciation/run_eval.py --system asr_diff            # test split
workers/asr/.venv/bin/python benchmarks/pronunciation/run_eval.py --system asr_diff --limit 20 # smoke run
```

Use the python of the venv that has the scorer's dependencies. The harness itself needs only numpy. Interrupted runs resume from the cache; `--fresh` discards it. The cache header stores `scorer.info()`, and a run with different settings refuses to reuse it.

### Plugging in a scorer (PA-2/PA-3)

The protocol is documented in `scorers/__init__.py`. Briefly, a class with `name`, `info()` (model ids, pinned revisions, method) and

```python
score(wav: np.ndarray, reference_text: str, ref_words) -> {
    "sentence": {"accuracy": ..., "fluency": ..., "prosodic": ..., "total": ...},   # optional
    "words": [{"accuracy": float | None, "flagged": bool | None, "phones": [float | None, ...] | None}, ...],
}
```

`wav` is 16 kHz mono float32. `ref_words` carry `.text` and canonical ARPAbet `.phones`. `words` must have one entry per ref word; `phones` must match the ref word's phone count, otherwise that word counts as not covered (coverage is reported). Scores are "higher = better" on any scale. Register it in `scorers.SYSTEMS`, or pass `--system package.module:ClassName` with `PYTHONPATH` set (for example a GOP scorer that imports `pron_worker` from `workers/pronunciation`, run with that venv).

## Metrics

- **Correlation** (phone, word, sentence): Pearson and Spearman between scorer output and the human score over all items of the split. Phone: human phone accuracy 0–2. Word: human word accuracy 0–10. Sentence: each key the scorer returns (`accuracy`, `fluency`, `prosodic`, `completeness`, `total`) against the same human key. A correlation is `null` when fewer than 3 pairs exist or one side is constant.
- **Per speaker:** distribution (min, quartiles, max) of word-level Pearson per test speaker. **By age group:** child (< 18) vs adult.
- **Binary flags** (for scorers that return `flagged`), against three human definitions of a mispronounced word:
  - word accuracy ≤ 6 (**primary**). Per the corpus rubric, 7–9 means "correct but accented", 4–6 "less than 30 % of phones wrong", 2–3 "more than 30 % wrong or another word", so ≤ 6 means at least one phone was judged wrong;
  - word accuracy ≤ 3 (severe);
  - any phone scored < 0.5 (the corpus's own threshold for adding a `mispronunciations` block).

  Reported: miss rate (mispronounced words not flagged / mispronounced words), precision, flag rate on words humans scored 10, the point-biserial r of "not flagged" vs word accuracy, per-speaker miss rate (speakers with ≥ 5 mispronounced words), and flag rate per human score.

## Baseline: how much the v0.1 reading diff misses

`asr_diff` = the product's reading path: Qwen3-ASR-0.6B (revision `5eb1441`, MPS, bf16) transcribes the audio **without** the target as context (PROTOCOL §7), near-silent audio is skipped like in the worker, and `vr_feedback.reading.diff_target` marks target words `missing`/`different`. The ASR runs in-process from `workers/asr/.venv`; the live worker was not used. The gateway's VAD minimum-speech gate is not applied (only 1 of 2,500 test transcripts was empty).

speechocean762 test, 2026-09-28 (`results/asr_diff-test.md`):

| Human definition of mispronounced | Words | Not flagged by the diff | Miss rate | Precision of flags |
|---|---|---|---|---|
| word accuracy ≤ 6 (primary) | 1,297 | 516 | **39.8 %** | 42.9 % |
| word accuracy ≤ 3 | 858 | 249 | 29.0 % | 33.4 % |
| any phone < 0.5 | 783 | 231 | 29.5 % | 30.3 % |

- Flag rate by human word accuracy: 0–2 → 100 %, 3 → 68 %, 5 → 39 %, 6 → 39 %, 8 → 27 %, 10 → 6.6 %. Words with one or a few wrong phones mostly pass: the ASR restores the intended word.
- The diff flagged 1,822 of 15,967 words. 944 of them (52 %) were words the experts scored 10, i.e. 6.6 % of the 14,319 words scored 10 were flagged. Only 781 flags (42.9 %) fall on words scored ≤ 6.
- Per speaker (56 speakers with ≥ 5 mispronounced words): miss rate median 44.6 %, quartiles 28.6–68.5 %, range 2.9–100 %.
- Adults 42.2 % (n = 901), children 34.3 % (n = 396).
- 1,478 of 2,500 utterances (59 %) got no flag at all.
- Scorer time 922 s for 2.69 h of audio (RTF 0.095) on the M3 Pro, with other workloads running.

This measures Mandarin-L1 speech only. It supports the TRIAGE claim that the diff is not a pronunciation check, but it is not a measurement for Korean learners.

## PA-2/PA-3: CTC GOP of the pronunciation worker

Full report with numbers: **`results/2026-09-28-speechocean762.md`**. Method: `workers/pronunciation/README.md` (`/assess`).

| System | What it is |
|---|---|
| `gop_ctc` | raw worker GOP; expected phones = the corpus's own ARPAbet (so phone scores line up with the human ones) |
| `gop_ctc_cal` | + calibration file (phone → word model, bands; `flagged` = band is not `good`) + a benchmark-only sentence model |
| `gop_ctc_cal_cmudict` | as `gop_ctc_cal`, but expected phones from CMUdict like the worker; word/sentence/flags only |

```sh
py=workers/pronunciation/.venv/bin/python
$py benchmarks/pronunciation/run_eval.py --system gop_ctc --split train     # ~12 min each on the M3 Pro (MPS)
$py benchmarks/pronunciation/run_eval.py --system gop_ctc --split test
$py benchmarks/pronunciation/fit_calibration.py                             # fit on train, evaluate on test (seconds)
$py benchmarks/pronunciation/run_eval.py --system gop_ctc_cal --split test  # end-to-end check of the calibration file
$py benchmarks/pronunciation/run_eval.py --system gop_ctc_cal_cmudict --split test
```

Coefficients and band thresholds are chosen on train only; test is read once, for the report. Feature choices were
compared by a speaker-disjoint 2-fold split inside train, never on test.

## Tests

```sh
cd benchmarks/pronunciation && ../../workers/asr/.venv/bin/python -m pytest -q tests
```

Tests use a FAKE miniature corpus (synthetic tones, invented scores) and a FAKE scorer; `test_asr_diff.py` checks the word mapping of the real `diff_target` without loading a model.
