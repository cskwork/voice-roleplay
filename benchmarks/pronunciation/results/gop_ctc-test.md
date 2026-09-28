# gop_ctc on speechocean762 test

Generated 2026-09-28T14:42:27+00:00 on arm64 Darwin 25.6.0. Human scores: speechocean762 (Mandarin L1 speakers only; says nothing about Korean learners).

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
    "scorer_elapsed_s": 667.3,
    "real_time_factor": 0.0689
  },
  "scorer": {
    "method": "segmentation-free CTC GOP over aligned word windows (prev+word+next, pad 200 ms); expected phones = corpus ARPAbet -> pron_worker.lexicon units",
    "phones_model": "facebook/wav2vec2-lv-60-espeak-cv-ft",
    "phones_revision": "ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4",
    "aligner_model": "Qwen/Qwen3-ForcedAligner-0.6B",
    "aligner_revision": "c7cbfc2048c462b0d63a45797104fc9db3ad62b7",
    "unit_table": "units-2",
    "device": "mps"
  }
}
```

## Correlation with human scores

| Level | n | Pearson | Spearman |
|---|---|---|---|
| phone | 47369 | 0.4676 | 0.3591 |
| word | 15967 | 0.4219 | 0.3584 |
