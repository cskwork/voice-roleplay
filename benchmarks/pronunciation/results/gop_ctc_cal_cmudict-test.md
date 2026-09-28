# gop_ctc_cal_cmudict on speechocean762 test

Generated 2026-09-28T14:52:47+00:00 on arm64 Darwin 25.6.0. Human scores: speechocean762 (Mandarin L1 speakers only; says nothing about Korean learners).

```json
{
  "dataset": {
    "name": "speechocean762",
    "source": {
      "source": "https://github.com/jimbozhang/speechocean762",
      "commit": "613968e3b0b789fc33936fb5eba1973176ba7d11",
      "license": "CC BY 4.0 (OpenSLR 101)",
      "files_verified": 5263,
      "blob_list_sha256": "0b53410fba4780114a7beba086dcf5978d092ac1db236a302e66531dc78d62ed"
    },
    "utterances": 2500,
    "speakers": 125,
    "child_speakers": 64,
    "words": 15967,
    "phones": 47369,
    "audio_hours": 2.691,
    "scorer_elapsed_s": 512.1,
    "real_time_factor": 0.0529
  },
  "scorer": {
    "method": "segmentation-free CTC GOP over aligned word windows (prev+word+next, pad 200 ms); expected phones = corpus ARPAbet -> pron_worker.lexicon units",
    "phones_model": "facebook/wav2vec2-lv-60-espeak-cv-ft",
    "phones_revision": "ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4",
    "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
    "aligner_revision": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7",
    "unit_table": "units-2",
    "device": "mps",
    "calibration_version": "so762-ctcgop-2026-09-28",
    "sentence_model": [
      -9.54522,
      1.785852,
      0.071659
    ],
    "expected_phones": "CMUdict cmusphinx/cmudict@74790861 (worker path)"
  }
}
```

## Correlation with human scores

| Level | n | Pearson | Spearman |
|---|---|---|---|
| phone | — | no output | — |
| word | 15950 | 0.4723 | 0.3923 |
| sentence accuracy | 2500 | 0.6231 | 0.6008 |

## Binary word flags

| Human definition of a mispronounced word | words | flagged | missed | miss rate | precision |
|---|---|---|---|---|---|
| word accuracy ≤ 6 (primary) | 1294 | 827 | 467 | 0.3609 | 0.363 |
| word accuracy ≤ 3 | 857 | 628 | 229 | 0.2672 | 0.2757 |
| any phone score < 0.5 | 781 | 598 | 183 | 0.2343 | 0.2625 |

Flag rate on words humans scored 10: 0.0933. Point-biserial r (not flagged vs word accuracy): 0.4471.

Per-speaker miss rate (accuracy ≤ 6): {"n": 56, "min": 0.0, "p25": 0.2905, "median": 0.4286, "p75": 0.6, "max": 1.0, "min_positives_per_speaker": 5}

By age group (accuracy ≤ 6): adult miss rate 0.3219 (n=901), child miss rate 0.4504 (n=393)

| Human word accuracy | words | flagged rate |
|---|---|---|
| 0 | 27 | 1.0 |
| 1 | 18 | 1.0 |
| 2 | 26 | 0.9615 |
| 3 | 786 | 0.7099 |
| 4 | 6 | 0.5 |
| 5 | 354 | 0.4774 |
| 6 | 77 | 0.3506 |
| 7 | 26 | 0.3077 |
| 8 | 319 | 0.3354 |
| 9 | 4 | 0.25 |
| 10 | 14307 | 0.0933 |
