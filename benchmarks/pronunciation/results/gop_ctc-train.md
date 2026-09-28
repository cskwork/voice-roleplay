# gop_ctc on speechocean762 train

Generated 2026-09-28T14:31:10+00:00 on arm64 Darwin 25.6.0. Human scores: speechocean762 (Mandarin L1 speakers only; says nothing about Korean learners).

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
    "child_speakers": 58,
    "words": 15849,
    "phones": 47076,
    "audio_hours": 2.876,
    "scorer_elapsed_s": 623.8,
    "real_time_factor": 0.0602
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
| phone | 47076 | 0.5123 | 0.3907 |
| word | 15849 | 0.4957 | 0.4129 |
