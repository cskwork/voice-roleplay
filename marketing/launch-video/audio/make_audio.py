"""Original soundtrack + SFX, synthesized from scratch with numpy (no samples, no loops, no copyrighted music).

Reads the shared timeline (scene/cues.js), writes:
  audio/build/mix.wav    48 kHz stereo, loudness-normalized to -14 LUFS (ffmpeg loudnorm, two passes), TP <= -1.5 dB
  scene/voice_env.js     60 fps loudness envelopes of the voice lines (the scene's orb rings follow the real voice)

Voice lines (audio/voice/*.flac) are the app's own TTS output with the CC BY 4.0 LibriTTS-R voice 4992
(see CREDITS.md). Run: python3 audio/make_audio.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

PKG = Path(__file__).resolve().parents[1]
SR = 48000
CUES = json.loads(re.sub(r"^.*?window\.CUES\s*=\s*", "", (PKG / "scene/cues.js").read_text(), flags=re.S))
DUR = float(CUES["duration"])
N = int(round(DUR * SR))
BEAT = 60.0 / CUES["bpm"]
G0 = CUES["grooveStart"]
rng = np.random.default_rng(20260929)
T = np.arange(N) / SR


def buf() -> np.ndarray:
    return np.zeros((N, 2))


def add(bus: np.ndarray, x: np.ndarray, at: float, gain: float = 1.0, pan: float = 0.0) -> None:
    """Mix mono x into stereo bus at time `at` (s) with constant-power pan (-1 left .. 1 right)."""
    i = int(round(at * SR))
    if i >= N or i + len(x) <= 0:
        return
    j0 = max(0, -i)
    x = x[j0:]
    i = max(0, i)
    n = min(len(x), N - i)
    a = (pan + 1) * np.pi / 4
    bus[i : i + n, 0] += x[:n] * gain * np.cos(a)
    bus[i : i + n, 1] += x[:n] * gain * np.sin(a)


def midi(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def env_ad(n: int, a: float, d: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-np.maximum(0, t - a) / d)


def bandpass(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    sos = signal.butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def highpass(x: np.ndarray, f: float, order: int = 2) -> np.ndarray:
    return signal.sosfilt(signal.butter(order, f, btype="high", fs=SR, output="sos"), x)


def lowpass(x: np.ndarray, f: float, order: int = 2) -> np.ndarray:
    return signal.sosfilt(signal.butter(order, f, btype="low", fs=SR, output="sos"), x)


def sweep_filter(x: np.ndarray, f0: float, f1: float, q: float = 1.2, block: int = 256) -> np.ndarray:
    """Band-pass whose centre moves exponentially from f0 to f1 over the clip (block-wise, state carried)."""
    out = np.zeros_like(x)
    zi = None
    nb = int(np.ceil(len(x) / block))
    for b in range(nb):
        u = b / max(1, nb - 1)
        fc = f0 * (f1 / f0) ** u
        lo, hi = fc / (1 + 1 / q), min(fc * (1 + 1 / q), SR / 2 - 100)
        sos = signal.butter(2, [lo, hi], btype="band", fs=SR, output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        seg = x[b * block : (b + 1) * block]
        y, zi = signal.sosfilt(sos, seg, zi=zi)
        out[b * block : (b + 1) * block] = y
    return out


# ------------------------------------------------------------------ instruments

def kick(level: float = 1.0) -> np.ndarray:
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    f = 44 + 110 * np.exp(-t / 0.035)
    ph = 2 * np.pi * np.cumsum(f) / SR
    body = np.sin(ph) * np.exp(-t / 0.32)
    click = highpass(rng.standard_normal(n), 2000) * np.exp(-t / 0.004) * 0.25
    return np.tanh((body + click) * 1.4) * level


def clap() -> np.ndarray:
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    nz = bandpass(rng.standard_normal(n), 900, 6000)
    e = np.zeros(n)
    for k, d in enumerate([0, 0.011, 0.022]):
        e += (t >= d) * np.exp(-np.maximum(0, t - d) / (0.012 if k < 2 else 0.14))
    body = np.sin(2 * np.pi * 185 * t) * np.exp(-t / 0.05) * 0.4
    return (nz * e * 0.55 + body) * 0.8


def hat(open_: bool = False) -> np.ndarray:
    n = int((0.25 if open_ else 0.06) * SR)
    t = np.arange(n) / SR
    nz = highpass(rng.standard_normal(n), 7000, 4)
    return nz * np.exp(-t / (0.08 if open_ else 0.014)) * 0.35


def pluck(freq: float, dur: float = 0.35, bright: float = 1.0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for h, a in [(1, 1), (2, 0.35 * bright), (3, 0.18 * bright), (4, 0.08 * bright)]:
        x += a * np.sin(2 * np.pi * freq * h * t) * np.exp(-t * (6 + 5 * h))
    return x * np.minimum(1, t / 0.003)


def bell(freq: float, dur: float = 1.2) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    mod = np.sin(2 * np.pi * freq * 3.5 * t) * 2.2 * np.exp(-t / 0.18)
    x = np.sin(2 * np.pi * freq * t + mod) * np.exp(-t / 0.45)
    x += 0.3 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t / 0.25)
    return x * np.minimum(1, t / 0.002)


def tick(freq: float = 2600) -> np.ndarray:
    n = int(0.05 * SR)
    t = np.arange(n) / SR
    return (np.sin(2 * np.pi * freq * t) + 0.4 * np.sin(2 * np.pi * freq * 2.01 * t)) * np.exp(-t / 0.008)


def pop() -> np.ndarray:
    n = int(0.12 * SR)
    t = np.arange(n) / SR
    f = 380 + 900 * np.exp(-t / 0.018)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.035)


def thump(freq: float = 70) -> np.ndarray:
    n = int(0.4 * SR)
    t = np.arange(n) / SR
    f = freq + 60 * np.exp(-t / 0.02)
    return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.16)


def whoosh(dur: float = 0.55, up: bool = True) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    nz = rng.standard_normal(n)
    f0, f1 = (300, 5000) if up else (5000, 300)
    x = sweep_filter(nz, f0, f1, q=1.4)
    e = np.sin(np.pi * np.clip(t / dur, 0, 1)) ** 2
    return x * e * 0.9


def tape_stop() -> np.ndarray:
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    f = 330 * (1 - t / 0.3) ** 2
    ph = 2 * np.pi * np.cumsum(f) / SR
    x = signal.sawtooth(ph) * 0.35 + np.sin(ph * 0.5) * 0.4
    return lowpass(x, 2500) * (1 - t / 0.3)


def riser(dur: float) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    u = t / dur
    nz = sweep_filter(rng.standard_normal(n), 400, 9000, q=1.0)
    f = 110 * 2 ** (u * 2.2)
    tone = signal.sawtooth(2 * np.pi * np.cumsum(f) / SR) * 0.2 + signal.sawtooth(2 * np.pi * np.cumsum(f * 1.005) / SR) * 0.2
    tone = lowpass(tone, 3000)
    return (nz * 0.8 + tone) * u**2.2


def crash(dur: float = 2.6) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    return highpass(rng.standard_normal(n), 4500, 2) * np.exp(-t / 0.8) * 0.35


def pad_chord(notes: list[int], t0: float, t1: float, bright) -> np.ndarray:
    """Additive, slightly detuned saw-like pad with time-varying brightness bright(t_abs) in 0..1."""
    i0, i1 = int(t0 * SR), int(t1 * SR)
    t = np.arange(i1 - i0) / SR
    ta = t + t0
    b = bright(ta)
    x = np.zeros(len(t))
    for m in notes:
        f = midi(m)
        for det in (-0.07, 0.0, 0.07):
            fd = f * 2 ** (det / 12)
            ph0 = rng.uniform(0, 2 * np.pi)
            for h in range(1, 12):
                if fd * h > 9000:
                    break
                w = (1 / h) * np.exp(-h / (1.2 + 7 * b))
                x += w * np.sin(2 * np.pi * fd * h * t + ph0 * h)
    fade = np.minimum(1, t / 0.08) * np.minimum(1, (t1 - t0 - t) / 0.08)
    return x * fade / (len(notes) * 3)


# ------------------------------------------------------------------ arrangement

CHORDS = [  # (pad notes, bass root, arp tones) in D major, one chord per bar from grooveStart
    ([62, 66, 69, 73, 76], 38, [74, 78, 81, 85]),  # Dmaj9
    ([59, 62, 66, 69, 73], 35, [71, 74, 78, 81]),  # Bm9
    ([55, 59, 62, 66, 69], 31, [67, 71, 74, 78]),  # Gmaj9
    ([57, 61, 64, 66, 69], 33, [69, 73, 76, 78]),  # A6/9
]
BAR = 4 * BEAT


def chord_at(t: float):
    k = int(np.floor((t - G0) / BAR)) % 4 if t >= G0 else 0
    return CHORDS[k]


def build() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    music, sfx, voice = buf(), buf(), buf()
    acts = CUES["acts"]
    groove = (acts["live"][0], acts["private"][0])  # full groove with hats/claps/arp

    # Pad: hook drone (Dmaj9) opening its filter, then chords per bar, then the final chord ringing out.
    def bright_fn(ta):
        b = 0.15 + 0.55 * np.clip(ta / 3.0, 0, 1)
        b = np.where(ta > 3.0, 0.6, b)
        b = np.where((ta > 24.0) & (ta < 27.0), 0.35 + 0.6 * np.clip((ta - 25.4) / 1.6, 0, 1), b)
        b = np.where(ta >= 27.0, 0.75 * np.exp(-(ta - 27.0) / 2.5) + 0.2, b)
        return b

    add(music, pad_chord(CHORDS[0][0], 0.0, G0 + 0.05, bright_fn) * np.minimum(1, np.arange(int((G0 + 0.05) * SR)) / (1.2 * SR)), 0.0, 0.55)
    t = G0
    while t < 27.0 - 1e-6:
        notes = chord_at(t)[0]
        end = min(t + BAR, 27.0)
        add(music, pad_chord(notes, t, end + 0.05, bright_fn), t, 0.5)
        t += BAR
    tail = pad_chord([50, 62, 66, 69, 73, 76], 27.0, DUR, bright_fn)
    tail *= np.exp(-np.arange(len(tail)) / SR / 4.0)
    add(music, tail, 27.0, 0.75)

    # Drums
    kicks = []
    b = G0
    while b < 24.0 - 1e-6:
        kicks.append(b)
        b += BEAT
    b = 24.0
    while b < 26.9:
        kicks.append(b)
        b += 2 * BEAT  # half time in the private act
    for k in kicks:
        add(music, kick(0.95), k, 0.9)
    for hit in CUES["hits"]:
        add(music, kick(1.0), hit, 1.1)
        add(music, thump(42), hit, 0.9)
        add(music, crash(3.0 if hit > 20 else 1.6), hit, 0.55)
    bt = groove[0]
    i = 0
    while bt < groove[1] - 0.01:
        if i % 2 == 1:
            add(music, clap(), bt, 0.55, 0.05)
        add(music, hat(open_=(i % 4 == 3)), bt + BEAT / 2, 0.5 if i % 4 == 3 else 0.42, 0.3)
        add(music, hat(), bt, 0.18, -0.3)
        bt += BEAT
        i += 1

    # Bass: 8th-note pulses on the chord root (groove), sustained sub in the private act.
    t8 = G0
    while t8 < 24.0 - 1e-6:
        root = chord_at(t8)[1]
        n = int(0.22 * SR)
        tt = np.arange(n) / SR
        f = midi(root + 12)
        x = (np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(4 * np.pi * f * tt) + 0.12 * np.sin(6 * np.pi * f * tt)) * env_ad(n, 0.004, 0.09)
        add(music, x, t8, 0.42)
        t8 += BEAT / 2
    for s0, root in [(24.0, 35), (26.0, 33)]:
        n = int((1.0 if s0 > 25 else 2.0) * SR)
        tt = np.arange(n) / SR
        x = np.sin(2 * np.pi * midi(root + 12) * tt) * np.minimum(1, tt / 0.02) * np.minimum(1, (n / SR - tt) / 0.05)
        add(music, x, s0, 0.45)

    # Arp: 16ths, ping-pong, from the live act to the private act.
    t16 = groove[0]
    j = 0
    while t16 < groove[1] - 0.01:
        tones = chord_at(t16)[2]
        m = tones[[0, 1, 2, 3, 2, 1, 3, 2][j % 8]]
        vel = 0.9 if j % 4 == 0 else 0.6
        add(music, pluck(midi(m), 0.3, bright=0.8), t16, 0.16 * vel, pan=(-0.55 if j % 2 else 0.55))
        t16 += BEAT / 4
        j += 1
    # Brand: a short rising bell figure on the logo, then one last high note for the CTA.
    for k, m in enumerate([74, 78, 81, 86]):
        add(music, bell(midi(m), 1.6), 27.05 + k * 0.125, 0.2, pan=[-0.4, 0.4, -0.2, 0.2][k])
    add(music, bell(midi(90), 2.0), CUES["cta"], 0.12)

    # Sidechain: duck pad/bass/arp under every kick (not the kicks themselves: applied before adding kicks is
    # simpler, but a gentle envelope on the whole music bus reads the same at this level).
    duck = np.ones(N)
    for k in kicks + CUES["hits"]:
        i0 = int(k * SR)
        n = int(0.28 * SR)
        e = 1 - 0.35 * np.exp(-np.arange(n) / SR / 0.09)
        seg = duck[i0 : i0 + n]
        duck[i0 : i0 + n] = np.minimum(seg, e[: len(seg)])
    # Keep transients: only duck below 200 Hz content and the pads (approximation: whole bus, then re-add kicks).
    music *= duck[:, None]

    # Barge-in: the music "stops" for a moment when the AI is cut off.
    c = CUES["cut"]
    gate = np.ones(N)
    i0, i1 = int(c * SR), int((c + 0.28) * SR)
    gate[i0:i1] = 0.15
    ramp = int(0.01 * SR)
    gate[i0 - ramp : i0] = np.linspace(1, 0.15, ramp)
    gate[i1 : i1 + int(0.12 * SR)] = np.linspace(0.15, 1, int(0.12 * SR))
    music *= gate[:, None]
    add(sfx, tape_stop(), c, 0.8)
    add(sfx, pop(), c, 0.5)

    # Hook: soft thumps on each kinetic word, riser into the first hit.
    for w in CUES["words"]:
        add(sfx, thump(95), w, 0.5)
        add(sfx, tick(3200), w, 0.08)
    add(sfx, riser(0.9), 2.1, 0.5)
    add(sfx, riser(CUES["riser"][1] - CUES["riser"][0]), CUES["riser"][0], 0.55)

    for k, w in enumerate(CUES["whooshes"]):
        add(sfx, whoosh(0.6, up=(k % 2 == 0)), w - 0.3, 0.45, pan=(-0.3 if k % 2 else 0.3))
    for name, t0 in CUES["callouts"].items():
        if name == "barge":
            continue
        add(sfx, pop(), t0, 0.35)
        add(sfx, tick(2400), t0 + 0.12, 0.12)
    for k, t0 in enumerate(CUES["hintCards"]):
        add(sfx, pluck(midi([81, 85, 88][k]), 0.5, 0.6), t0, 0.22)
        add(sfx, whoosh(0.35), t0 - 0.15, 0.18)
    for k, t0 in enumerate(CUES["dings"]):
        add(sfx, bell(midi([81, 85, 88][k]), 1.2), t0, 0.28)
    # Strike (a short scratch) and the fix (a bright pluck).
    n = int(0.3 * SR)
    add(sfx, bandpass(rng.standard_normal(n), 1500, 5000) * np.exp(-np.arange(n) / SR / 0.1), CUES["strike"], 0.25)
    add(sfx, bell(midi(86), 1.0), CUES["fix"], 0.18)
    for k, t0 in enumerate(CUES["priv"]):
        add(sfx, thump(60), t0, 0.6)
        add(sfx, tick(1800), t0, 0.08)
    for t0 in CUES["chips"]:
        add(sfx, tick(3000), t0, 0.12)
    add(sfx, tick(2200), CUES["cta"], 0.15)

    # Voices
    for name, t0 in CUES["voice"].items():
        sr, x = wavfile.read(PKG / "audio/build" / f"{name}.wav")
        x = x.astype(np.float64) / 32768.0
        if sr != SR:
            x = signal.resample_poly(x, SR, sr)
        add(voice, x, t0, 0.95)
    return music, sfx, voice


def decode_voices() -> dict[str, np.ndarray]:
    out = {}
    (PKG / "audio/build").mkdir(parents=True, exist_ok=True)
    for name in CUES["voice"]:
        src = PKG / "audio/voice" / f"{name}.flac"
        dst = PKG / "audio/build" / f"{name}.wav"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(dst)], check=True)
        sr, x = wavfile.read(dst)
        out[name] = x.astype(np.float64) / 32768.0
    return out


def voice_env(voices: dict[str, np.ndarray]) -> None:
    fps = 60
    env = {"fps": fps}
    for name, x in voices.items():
        hop = SR // fps
        frames = [float(np.sqrt(np.mean(x[i : i + hop] ** 2))) for i in range(0, len(x), hop)]
        pk = max(frames) or 1.0
        env[name] = [round(min(1.0, f / pk), 3) for f in frames]
    (PKG / "scene/voice_env.js").write_text(
        "// Generated by audio/make_audio.py: RMS envelopes (0..1) of the voice lines at 60 fps.\nwindow.VOICE_ENV = " + json.dumps(env) + "\n"
    )


def main() -> None:
    voices = decode_voices()
    voice_env(voices)
    music, sfx, voice = build()
    # Duck music under the voice lines (-9 dB, smooth).
    v = np.abs(voice).max(axis=1)
    win = int(0.15 * SR)
    venv = np.convolve(v, np.ones(win) / win, mode="same")
    duckv = 1 - 0.65 * np.clip(venv / (venv.max() or 1) * 4, 0, 1)
    mix = music * 0.8 * duckv[:, None] + sfx * 0.8 + voice * 1.25
    # Final fade and a gentle bus saturation.
    fo = int(0.35 * SR)
    mix[-fo:] *= np.linspace(1, 0, fo)[:, None]
    mix = np.tanh(mix * 0.9) / 0.9
    mix /= np.abs(mix).max() / 0.7
    raw = PKG / "audio/build/raw.wav"
    wavfile.write(raw, SR, (mix * 32767).astype(np.int16))

    # Two-pass loudnorm to -14 LUFS integrated, true peak -1.5 dBTP.
    out = PKG / "audio/build/mix.wav"
    p1 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(raw), "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    m = json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p1.stderr).group(0))
    af = (
        f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
        f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-af", af, "-ar", str(SR), "-c:a", "pcm_s16le", str(out)], check=True)
    print(f"mix -> {out} (input {m['input_i']} LUFS, {m['input_tp']} dBTP)")


if __name__ == "__main__":
    main()
