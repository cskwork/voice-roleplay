# asr_diff on speechocean762 test

Generated 2026-09-28T13:59:36+00:00 on arm64 Darwin 25.6.0. Human scores: speechocean762 (Mandarin L1 speakers only; says nothing about Korean learners).

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
    "scorer_elapsed_s": 922.3,
    "real_time_factor": 0.0952
  },
  "scorer": {
    "method": "Qwen3-ASR transcript (no context) + vr_feedback.reading.diff_target; flags only",
    "asr_model": "Qwen/Qwen3-ASR-0.6B",
    "asr_revision": "5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
    "device": "mps",
    "silence_dbfs": -40.0
  }
}
```

## Correlation with human scores

| Level | n | Pearson | Spearman |
|---|---|---|---|
| phone | — | no output | — |
| word | — | no output | — |

## Binary word flags

| Human definition of a mispronounced word | words | flagged | missed | miss rate | precision |
|---|---|---|---|---|---|
| word accuracy ≤ 6 (primary) | 1297 | 781 | 516 | 0.3978 | 0.4286 |
| word accuracy ≤ 3 | 858 | 609 | 249 | 0.2902 | 0.3342 |
| any phone score < 0.5 | 783 | 552 | 231 | 0.295 | 0.303 |

Flag rate on words humans scored 10: 0.0659. Point-biserial r (not flagged vs word accuracy): 0.4853.

Per-speaker miss rate (accuracy ≤ 6): {"n": 56, "min": 0.0286, "p25": 0.2857, "median": 0.446, "p75": 0.6845, "max": 1.0, "min_positives_per_speaker": 5}

By age group (accuracy ≤ 6): adult miss rate 0.4218 (n=901), child miss rate 0.3434 (n=396)

| Human word accuracy | words | flagged rate |
|---|---|---|
| 0 | 27 | 1.0 |
| 1 | 18 | 1.0 |
| 2 | 26 | 1.0 |
| 3 | 787 | 0.6836 |
| 4 | 6 | 0.3333 |
| 5 | 356 | 0.3933 |
| 6 | 77 | 0.3896 |
| 7 | 26 | 0.3077 |
| 8 | 321 | 0.271 |
| 9 | 4 | 0.5 |
| 10 | 14319 | 0.0659 |
