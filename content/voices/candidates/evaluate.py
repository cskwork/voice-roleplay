"""Synthesize the same 5 test sentences with every candidate voice and score them (CO-3).

    cd workers/tts && .venv/bin/python ../../content/voices/candidates/evaluate.py [voice_id ...]

Runs the MLX TTS engine in-process (the worker's defaults: fp16, 5 flow steps, first chunk 50 tokens, streamed),
not the live worker. Per voice it writes ``<id>/samples/NN_<kind>.mp3`` (listening copies) and adds to
``results.json``:
- prompt check: word errors of Qwen3-ASR-0.6B on ``prompt.wav`` against ``prompt.txt``;
- round trip: word errors of Qwen3-ASR-0.6B on each synthesized sentence (lossless audio, kept in a temp dir);
  reference and hypothesis both go through the worker's ``textnorm.normalize`` so "$24.99" and
  "twenty-four dollars and ninety-nine cents" count as the same words;
- speaker similarity: cosine of CAM++ embeddings (weights from the MLX checkpoint) between prompt and output;
- RTF (wall time / audio duration; the machine is shared, so indicative only).
Transcripts are compared in memory and never printed or stored.
"""

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parents[2]
sys.path.insert(0, str(REPO_DIR / "workers" / "tts"))

from tts_worker.engine_mlx import MlxEngine  # noqa: E402
from tts_worker.logsafe import configure_logging  # noqa: E402
from tts_worker.textnorm import normalize  # noqa: E402

ASR_PYTHON = REPO_DIR / "workers" / "asr" / ".venv" / "bin" / "python"
SENTENCES = [
    ("price", "That comes to $24.99, and you can pay by card or in cash."),
    ("date", "The meeting has been moved to Tuesday, October 14th, 2026."),
    ("contraction", "I'm sorry, but we don't have that size, and it won't arrive until next week."),
    ("question", "Would you like to sit by the window, or would you rather sit near the door?"),
    ("long", "When you get off the train, walk two blocks north, turn left at the bakery on the corner, "
             "and you'll see the museum entrance right across from the small park."),
]


def words(text: str) -> list[str]:
    text = normalize(text).lower().replace("-", " ").replace("’", "'")
    return re.sub(r"[^a-z' ]+", " ", text).split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1]


def transcribe(paths: list[Path]) -> list[str]:
    out = subprocess.run([str(ASR_PYTHON), str(HERE / "_asr_transcribe.py"), *map(str, paths)],
                         stdout=subprocess.PIPE, check=True, text=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def speaker_encoder(model_dir: Path):
    import mlx.core as mx
    from mlx_audio.tts.models.cosyvoice2.speaker_encoder import CAMPlusSpeakerEncoder

    speaker = CAMPlusSpeakerEncoder()
    weights = mx.load(str(model_dir / "model.safetensors"))
    speaker.model.load_weights([(k[len("campplus."):], v) for k, v in weights.items() if k.startswith("campplus.")])
    speaker.model.eval()
    speaker._loaded = True

    def embed(path: Path) -> np.ndarray:
        audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
        a16 = mx.array(resample_poly(audio.mean(axis=1), 16000, sr).astype(np.float32))
        e = np.array(speaker(a16, sample_rate=16000)).reshape(-1)
        return e / np.linalg.norm(e)
    return embed


def main():
    configure_logging()
    # load_voices() reads every subdirectory, so give it only the candidate folders (not __pycache__ etc.).
    voices_dir = Path(tempfile.mkdtemp(prefix="vr-co3-voices-"))
    for d in HERE.iterdir():
        if (d / "voice.json").exists():
            (voices_dir / d.name).symlink_to(d)
    engine = MlxEngine("fp16", voices_dir=voices_dir)
    ids = sys.argv[1:] or list(engine.voices)
    engine.load()
    embed = speaker_encoder(engine.model_dir)
    results_path = HERE / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else {"voices": {}}
    tmp = Path(tempfile.mkdtemp(prefix="vr-co3-"))
    jobs = []  # (voice_id, kind or None for the prompt, wav path)
    runs = {}
    for vid in ids:
        (HERE / vid / "samples").mkdir(exist_ok=True)
        jobs.append((vid, None, HERE / vid / "prompt.wav"))
        for n, (kind, text) in enumerate(SENTENCES, 1):
            t0 = time.perf_counter()
            pcm = b"".join(c.tobytes() for c in engine.synthesize(normalize(text), vid, 1.0, threading.Event()))
            elapsed = time.perf_counter() - t0
            wav = tmp / f"{vid}_{n:02d}_{kind}.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(engine.sample_rate), w.writeframes(pcm)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-ac", "1", "-b:a", "64k",
                            str(HERE / vid / "samples" / f"{n:02d}_{kind}.mp3")], check=True)
            audio_s = len(pcm) / 2 / engine.sample_rate
            runs[(vid, kind)] = {"audio_s": round(audio_s, 2), "rtf": round(elapsed / audio_s, 3)}
            jobs.append((vid, kind, wav))
            print(json.dumps({"voice": vid, "sentence": kind, **runs[(vid, kind)]}), flush=True)

    hyps = transcribe([p for _, _, p in jobs])
    for (vid, kind, path), hyp in zip(jobs, hyps):
        voice = engine.voices[vid]
        if kind is None:
            ref = words(voice.prompt_text)
            prompt_emb = embed(path)
            entry = results["voices"].setdefault(vid, {})
            entry.clear()
            entry["prompt"] = {"seconds": round(sf.info(str(path)).duration, 2),
                               "sample_rate": sf.info(str(path)).samplerate,
                               "asr_errors": edit_distance(ref, words(hyp)), "ref_words": len(ref)}
            entry["sentences"] = {}
            continue
        ref = words(dict(SENTENCES)[kind])
        entry["sentences"][kind] = {**runs[(vid, kind)], "errors": edit_distance(ref, words(hyp)),
                                    "ref_words": len(ref),
                                    "speaker_cos": round(float(prompt_emb @ embed(path)), 3)}
    for vid in ids:
        s = results["voices"][vid]["sentences"].values()
        results["voices"][vid]["summary"] = {
            "wer": round(sum(x["errors"] for x in s) / sum(x["ref_words"] for x in s), 4),
            "errors": sum(x["errors"] for x in s), "ref_words": sum(x["ref_words"] for x in s),
            "speaker_cos_mean": round(float(np.mean([x["speaker_cos"] for x in s])), 3),
            "rtf_median": round(float(np.median([x["rtf"] for x in s])), 3)}
    results.update({
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tts": {"model_id": engine.model_id, "revision": engine.revision, "flow_steps": engine.flow_steps,
                "token_hop": engine.token_hop, "mode": "stream"},
        "asr": "Qwen/Qwen3-ASR-0.6B (models/Qwen3-ASR-0.6B, transformers backend)",
        "sentences": {k: t for k, t in SENTENCES},
    })
    results_path.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
    for vid in ids:
        print(vid, json.dumps(results["voices"][vid]["summary"]), flush=True)


if __name__ == "__main__":
    main()
