"""Pick the captures the video uses and pack them small for the scene (and for git).

Input:  assets/captures/ (raw PNGs + manifest.json from capture/capture.mjs; gitignored, large)
        var/cache/tts/libritts_r_4992_f/ (the app's own cached TTS lines, reviewed text, CC BY 4.0 voice)
Output: scene/media/*.webp   lossy WebP q90 (UI text stays sharp at the sizes the video shows)
        scene/media/rects.json  element rects in image CSS px (scroll offset removed)
        audio/voice/*.flac   trimmed voice lines

Run: python3 capture/pack.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from PIL import Image

PKG = Path(__file__).resolve().parents[1]
ROOT = PKG.parents[1]
CAP = PKG / "assets/captures"
BURST = PKG / ".cache/burst/mobile-talk"
MEDIA = PKG / "scene/media"
VOICE = PKG / "audio/voice"
Q = 90

# name in scene -> (capture file, crop box in CSS px or None)
STILLS = {
    "m-home": ("mobile/home.png", None),
    "m-ai-speaking": ("mobile/talk-ai-speaking.png", None),
    "m-barge": ("mobile/barge-cancelled.png", None),
    "m-barge-reply": ("mobile/barge-reply.png", None),
    "m-hint1": ("mobile/hint-1.png", None),
    "m-t4": ("mobile/t4-done.png", None),
    "m-summary": ("mobile/summary.png", None),
    "m-feedback": ("mobile/summary-feedback.png", None),
    "m-result-pron": ("mobile/result-pron.png", None),
    "m-result": ("mobile/result.png", None),
    "d-home": ("desktop/home.png", None),
    "d-t1": ("desktop/t1-reply.png", None),
    "d-t2": ("desktop/t2-reply.png", None),
    "d-hint1": ("desktop/hint-1.png", None),
    "d-t4": ("desktop/t4-done.png", None),
    "d-talk-ready": ("desktop/talk-ready.png", None),
    # Hint ladder pieces from the real hint panel (desktop, where the panel is not collapsed):
    # each level's header, the level-1 Korean tip, the level-2 key words, the level-3 example sentence.
    "c-hdr1": ("desktop/hint-1.png", (494, 430, 240, 38)),
    "c-hdr2": ("desktop/hint-2.png", (494, 389, 240, 38)),
    "c-hdr3": ("desktop/hint-3.png", (494, 365, 240, 38)),
    "c-tip": ("desktop/hint-1.png", (491, 596, 400, 64)),
    "c-kw": ("desktop/hint-2-detail.png", (491, 616, 380, 43)),
    "c-ex": ("desktop/hint-3-detail.png", (491, 619, 330, 39)),
    # Scenario cards from the home screen.
    "c-card0": ("desktop/home.png", (220, 395, 238, 334)),
    "c-card1": ("desktop/home.png", (474, 395, 238, 334)),
    "c-card2": ("desktop/home.png", (728, 395, 238, 334)),
    "c-card3": ("desktop/home.png", (982, 395, 238, 334)),
}
# Live-caption flip-book: burst frames of turn 1 (listening -> partial captions -> final -> AI reply streaming).
BURST_FRAMES = [32, 33, 35, 37, 39, 41, 43, 45, 47, 49, 51, 53, 55, 57]

# The app's cached TTS lines (female voice, libritts_r_4992_f): (source prefix, start s, end s)
VOICES = {
    "opening": ("cafe_order_opening__1.00__", 0.80, 4.45),  # "Hi there, welcome to Maple Street Coffee!"
    "model_read": ("cafe_order_read_01__1.00__", 1.10, 4.50),  # "I'd like a large cappuccino with oat milk, please."
}


def dpr(img: Image.Image, vp_w: int) -> float:
    return img.width / vp_w


def save(img: Image.Image, name: str) -> None:
    img.convert("RGB").save(MEDIA / f"{name}.webp", "WEBP", quality=Q, method=6)


def main() -> None:
    MEDIA.mkdir(parents=True, exist_ok=True)
    VOICE.mkdir(parents=True, exist_ok=True)
    for old in MEDIA.glob("*.webp"):  # this script owns scene/media: drop images no longer picked
        old.unlink()
    manifest = json.loads((CAP / "manifest.json").read_text())
    rects: dict[str, dict] = {}

    for name, (file, crop) in STILLS.items():
        meta = manifest[file]
        img = Image.open(CAP / file)
        k = dpr(img, meta["viewport"]["width"])
        sy = meta.get("scrollY", 0)
        if crop:
            x, y, w, h = crop
            img = img.crop((round(x * k), round(y * k), round((x + w) * k), round((y + h) * k)))
            rects[name] = {"w": w, "h": h, "dpr": k}
        else:
            r = {key: [{**a, "y": a["y"] - sy} for a in v] for key, v in meta["rects"].items()}
            rects[name] = {"w": meta["viewport"]["width"], "h": meta["viewport"]["height"], "dpr": k, "rects": r}
        save(img, name)

    for i, n in enumerate(BURST_FRAMES):
        img = Image.open(BURST / f"{n:04d}.jpg")
        save(img, f"m-live-{i:02d}")
    rects["m-live"] = {"count": len(BURST_FRAMES), "w": 390, "h": 844}

    (MEDIA / "rects.json").write_text(json.dumps(rects, indent=1, ensure_ascii=False))

    src_dir = ROOT / "var/cache/tts/libritts_r_4992_f"
    for name, (prefix, a, b) in VOICES.items():
        src = next(src_dir.glob(f"{prefix}*.wav"))
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ss", str(a), "-to", str(b),
             "-af", "afade=t=in:d=0.02,areverse,afade=t=in:d=0.08,areverse", "-c:a", "flac", str(VOICE / f"{name}.flac")],
            check=True,
        )
    total = sum(p.stat().st_size for p in MEDIA.iterdir())
    print(f"packed {len(list(MEDIA.glob('*.webp')))} images, {total / 1e6:.1f} MB; voices: {', '.join(VOICES)}")


if __name__ == "__main__":
    main()
