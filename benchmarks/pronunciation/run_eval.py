"""Evaluate a pronunciation scorer on speechocean762 against the human scores.

    <python> benchmarks/pronunciation/run_eval.py --system asr_diff [--split test] [--limit N] [--fresh]

<python> is the interpreter of the venv that has the scorer's dependencies (asr_diff: workers/asr/.venv/bin/python).
Scorer outputs are cached per utterance in data/pron_runs/<system>-<split>.jsonl (gitignored; may hold
transcripts of the public corpus), so an interrupted run resumes. The report holds numbers only:
benchmarks/pronunciation/results/<system>-<split>[-limitN].{json,md}.

Scorer protocol: see scorers/__init__.py. Metric definitions: see README.md.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import metrics  # noqa: E402
import so762  # noqa: E402
from scorers import load_scorer  # noqa: E402

ROOT = HERE.parents[1]
# Word accuracy rubric (corpus README): 10 perfect, 7-9 correct but accented, 4-6 "less than 30% of phones wrong",
# 2-3 "more than 30% wrong or another word", 1 hard to distinguish, 0 no voice.
# Primary definition of "mispronounced word": accuracy <= 6, i.e. at least one phone judged wrong.
MISPRONOUNCED_MAX = 6
SEVERE_MAX = 3
PHONE_WRONG_BELOW = 0.5  # the corpus adds a "mispronunciations" block for phones scored below 0.5
MINOR_AGE = 18  # speakers under 18 reported as "child" (the corpus is about half children)
MIN_SPEAKER_POSITIVES = 5  # per-speaker miss rates only for speakers with at least this many mispronounced words


def run_scorer(scorer, utts, cache: Path, fresh: bool) -> dict[str, dict]:
    info = scorer.info()
    done: dict[str, dict] = {}
    if cache.is_file() and not fresh:
        lines = cache.read_text().splitlines()
        if not lines or json.loads(lines[0]).get("meta") != info:
            raise SystemExit(f"{cache} was made with different scorer settings; rerun with --fresh")
        for line in lines[1:]:
            rec = json.loads(line)
            done[rec["utt_id"]] = rec
    else:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"meta": info}) + "\n")
    todo = [u for u in utts if u.utt_id not in done]
    with cache.open("a") as f:
        for n, u in enumerate(todo, 1):
            wav = u.load_audio()
            started = time.perf_counter()
            out = scorer.score(wav, u.text, u.words)
            rec = {
                "utt_id": u.utt_id,
                "audio_ms": round(len(wav) / 16),
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "output": out,
            }
            f.write(json.dumps(rec) + "\n")
            f.flush()
            done[u.utt_id] = rec
            if n % 100 == 0 or n == len(todo):
                print(f"{n}/{len(todo)} utterances scored", file=sys.stderr)
    return done


def evaluate(utts, records: dict[str, dict]) -> dict:
    word_pred, word_human = [], []
    phone_pred, phone_human = [], []
    phones_total = phones_covered = 0
    sent = defaultdict(lambda: ([], []))
    flag_rows = []  # (flagged, human word accuracy, any phone < PHONE_WRONG_BELOW, speaker, is_child)
    per_spk_words = defaultdict(lambda: ([], []))
    by_group = defaultdict(lambda: defaultdict(lambda: ([], [])))

    for u in utts:
        out = records[u.utt_id]["output"]
        words = out["words"]
        if len(words) != len(u.words):
            raise ValueError(f"{u.utt_id}: scorer returned {len(words)} words, expected {len(u.words)}")
        group = "child" if u.age < MINOR_AGE else "adult"
        for key, value in (out.get("sentence") or {}).items():
            if value is not None and key in u.sentence:
                sent[key][0].append(value)
                sent[key][1].append(u.sentence[key])
        for ref, pred in zip(u.words, words):
            if pred.get("accuracy") is not None:
                word_pred.append(pred["accuracy"])
                word_human.append(ref.accuracy)
                per_spk_words[u.speaker][0].append(pred["accuracy"])
                per_spk_words[u.speaker][1].append(ref.accuracy)
                by_group[group]["word"][0].append(pred["accuracy"])
                by_group[group]["word"][1].append(ref.accuracy)
            phones_total += len(ref.phones)
            pp = pred.get("phones")
            if pp is not None and len(pp) == len(ref.phones):
                for p, h in zip(pp, ref.phone_accuracy):
                    if p is not None:
                        phones_covered += 1
                        phone_pred.append(p)
                        phone_human.append(h)
                        by_group[group]["phone"][0].append(p)
                        by_group[group]["phone"][1].append(h)
            if pred.get("flagged") is not None:
                phone_wrong = any(a < PHONE_WRONG_BELOW for a in ref.phone_accuracy)
                flag_rows.append((bool(pred["flagged"]), ref.accuracy, phone_wrong, u.speaker, group))

    report: dict = {
        "phone": {**metrics.correlation(phone_pred, phone_human), "coverage": _ratio(phones_covered, phones_total)}
        if phone_pred else None,
        "word": metrics.correlation(word_pred, word_human) if word_pred else None,
        "sentence": {k: metrics.correlation(p, h) for k, (p, h) in sorted(sent.items())} or None,
    }
    if per_spk_words:
        report["word_per_speaker_pearson"] = metrics.distribution(
            [metrics.pearson(p, h) for p, h in per_spk_words.values()]
        )
    if by_group:
        report["by_age_group"] = {
            g: {lvl: metrics.correlation(p, h) for lvl, (p, h) in levels.items()} for g, levels in sorted(by_group.items())
        }
    if flag_rows:
        report["flags"] = flag_report(flag_rows)
    return report


def flag_report(rows) -> dict:
    flagged = [r[0] for r in rows]
    acc = [r[1] for r in rows]
    out = {
        "mispronounced_word_accuracy_le_6": metrics.detection(flagged, [a <= MISPRONOUNCED_MAX for a in acc]),
        "severe_word_accuracy_le_3": metrics.detection(flagged, [a <= SEVERE_MAX for a in acc]),
        "any_phone_below_0_5": metrics.detection(flagged, [r[2] for r in rows]),
        "flag_rate_on_words_scored_10": _ratio(sum(f for f, a in zip(flagged, acc) if a == 10), sum(a == 10 for a in acc)),
        "not_flagged_vs_word_accuracy": metrics.correlation([0.0 if f else 1.0 for f in flagged], acc),
        "flag_rate_by_human_word_accuracy": {
            str(int(a) if a == int(a) else a): {
                "words": sum(x == a for x in acc),
                "flagged_rate": _ratio(sum(f for f, x in zip(flagged, acc) if x == a), sum(x == a for x in acc)),
            }
            for a in sorted(set(acc))
        },
    }
    per_spk = defaultdict(list)
    per_group = defaultdict(list)
    for f, a, _, spk, group in rows:
        per_spk[spk].append((f, a))
        per_group[group].append((f, a))
    miss = []
    for items in per_spk.values():
        pos = [f for f, a in items if a <= MISPRONOUNCED_MAX]
        if len(pos) >= MIN_SPEAKER_POSITIVES:
            miss.append(1 - sum(pos) / len(pos))
    out["per_speaker_miss_rate_le_6"] = {**metrics.distribution(miss), "min_positives_per_speaker": MIN_SPEAKER_POSITIVES}
    out["by_age_group_le_6"] = {
        g: metrics.detection([f for f, _ in items], [a <= MISPRONOUNCED_MAX for _, a in items])
        for g, items in sorted(per_group.items())
    }
    return out


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def dataset_summary(utts, records) -> dict:
    audio_ms = sum(records[u.utt_id]["audio_ms"] for u in utts)
    elapsed_ms = sum(records[u.utt_id]["elapsed_ms"] for u in utts)
    return {
        "utterances": len(utts),
        "speakers": len({u.speaker for u in utts}),
        "child_speakers": len({u.speaker for u in utts if u.age < MINOR_AGE}),
        "words": sum(len(u.words) for u in utts),
        "phones": sum(len(w.phones) for u in utts for w in u.words),
        "audio_hours": round(audio_ms / 3_600_000, 3),
        "scorer_elapsed_s": round(elapsed_ms / 1000, 1),
        "real_time_factor": round(elapsed_ms / audio_ms, 4) if audio_ms else None,
    }


def to_markdown(rep: dict) -> str:
    lines = [
        f"# {rep['system']} on speechocean762 {rep['split']}",
        "",
        (
            f"Generated {rep['generated_at']} on {rep['machine']}. Human scores: speechocean762 "
            "(Mandarin L1 speakers only; says nothing about Korean learners)."
        ),
        "",
        "```json",
        json.dumps({"dataset": rep["dataset"], "scorer": rep["scorer"]}, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Correlation with human scores",
        "",
        "| Level | n | Pearson | Spearman |",
        "|---|---|---|---|",
    ]
    res = rep["results"]
    rows = [("phone", res["phone"]), ("word", res["word"])]
    rows += [(f"sentence {k}", v) for k, v in (res["sentence"] or {}).items()]
    for name, c in rows:
        lines.append(f"| {name} | {c['n']} | {c['pearson']} | {c['spearman']} |" if c else f"| {name} | — | no output | — |")
    if "flags" in res:
        fl = res["flags"]
        lines += [
            "",
            "## Binary word flags",
            "",
            "| Human definition of a mispronounced word | words | flagged | missed | miss rate | precision |",
            "|---|---|---|---|---|---|",
        ]
        for key, label in (
            ("mispronounced_word_accuracy_le_6", "word accuracy ≤ 6 (primary)"),
            ("severe_word_accuracy_le_3", "word accuracy ≤ 3"),
            ("any_phone_below_0_5", "any phone score < 0.5"),
        ):
            d = fl[key]
            lines.append(
                f"| {label} | {d['positives']} | {d['flagged_positives']} | {d['missed_positives']} | "
                f"{d['miss_rate']} | {d['precision']} |"
            )
        lines += [
            "",
            (
                f"Flag rate on words humans scored 10: {fl['flag_rate_on_words_scored_10']}. "
                f"Point-biserial r (not flagged vs word accuracy): {fl['not_flagged_vs_word_accuracy']['pearson']}."
            ),
            "",
            "Per-speaker miss rate (accuracy ≤ 6): " + json.dumps(fl["per_speaker_miss_rate_le_6"]),
            "",
            "By age group (accuracy ≤ 6): "
            + ", ".join(f"{g} miss rate {d['miss_rate']} (n={d['positives']})" for g, d in fl["by_age_group_le_6"].items()),
            "",
            "| Human word accuracy | words | flagged rate |",
            "|---|---|---|",
        ]
        for a, d in fl["flag_rate_by_human_word_accuracy"].items():
            lines.append(f"| {a} | {d['words']} | {d['flagged_rate']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", required=True, help="built-in name (see scorers.SYSTEMS) or package.module:ClassName")
    ap.add_argument("--split", default="test", choices=so762.SPLITS)
    ap.add_argument("--root", type=Path, default=so762.DEFAULT_ROOT)
    ap.add_argument("--limit", type=int, help="first N utterances only (smoke runs)")
    ap.add_argument("--fresh", action="store_true", help="ignore cached scorer outputs")
    ap.add_argument("--cache-dir", type=Path, default=ROOT / "data" / "pron_runs")
    ap.add_argument("--out-dir", type=Path, default=HERE / "results")
    args = ap.parse_args()

    so762.check_speaker_disjoint(args.root)
    utts = so762.load(args.split, args.root)
    if args.limit:
        utts = utts[: args.limit]
    scorer = load_scorer(args.system)
    records = run_scorer(scorer, utts, args.cache_dir / f"{scorer.name}-{args.split}.jsonl", args.fresh)
    rep = {
        "system": scorer.name,
        "split": args.split,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": f"{platform.machine()} {platform.system()} {platform.release()}",
        "scorer": scorer.info(),
        "dataset": {"name": "speechocean762", "source": _verified(args.root), **dataset_summary(utts, records)},
        "definitions": {
            "mispronounced_word": f"human word accuracy <= {MISPRONOUNCED_MAX}",
            "severe": f"human word accuracy <= {SEVERE_MAX}",
            "phone_wrong": f"human phone accuracy < {PHONE_WRONG_BELOW}",
            "child": f"age < {MINOR_AGE}",
        },
        "results": evaluate(utts, records),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{scorer.name}-{args.split}" + (f"-limit{args.limit}" if args.limit else "")
    (args.out_dir / f"{stem}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n")
    (args.out_dir / f"{stem}.md").write_text(to_markdown(rep))
    print(args.out_dir / f"{stem}.md")
    return 0


def _verified(root: Path) -> dict | None:
    p = root / "VERIFIED.json"
    return json.loads(p.read_text()) if p.is_file() else None


if __name__ == "__main__":
    sys.exit(main())
