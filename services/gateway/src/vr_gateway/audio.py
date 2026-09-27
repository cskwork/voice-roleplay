"""WAV parsing/validation, resampling to 16 kHz mono PCM16, WAV encoding."""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np
import soxr

from .errors import ApiError

TARGET_RATE = 16000
ALLOWED_RATES = {16000, 24000, 44100, 48000}
PCM_FORMAT = 1
EXTENSIBLE_FORMAT = 0xFFFE
PCM_SUBFORMAT = bytes.fromhex("0100000000001000800000aa00389b71")


@dataclass
class DecodedAudio:
    pcm16k: np.ndarray  # int16 mono at 16 kHz
    source_rate: int
    source_channels: int
    duration_s: float

    @property
    def num_samples(self) -> int:
        return int(self.pcm16k.shape[0])


def parse_wav(data: bytes, max_duration_s: float) -> DecodedAudio:
    """Parse a RIFF/WAVE PCM16 file by walking its chunks; reject anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ApiError("AUDIO_INVALID")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body_start = pos + 8
        if cid == b"data":
            available = len(data) - body_start
            # 0 / 0xFFFFFFFF are placeholders written by streaming encoders.
            if size in (0, 0xFFFFFFFF):
                size = available
            elif size > available:
                raise ApiError("AUDIO_INVALID")
            pcm = data[body_start : body_start + size]
            break
        if body_start + size > len(data):
            raise ApiError("AUDIO_INVALID")
        if cid == b"fmt ":
            if size < 16:
                raise ApiError("AUDIO_INVALID")
            tag, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", data, body_start)
            if tag == EXTENSIBLE_FORMAT:
                if size < 40 or data[body_start + 24 : body_start + 40] != PCM_SUBFORMAT:
                    raise ApiError("AUDIO_INVALID")
            elif tag != PCM_FORMAT:
                raise ApiError("AUDIO_INVALID")
            fmt = (channels, rate, byte_rate, block_align, bits)
        pos = body_start + size + (size & 1)
    if fmt is None or pcm is None:
        raise ApiError("AUDIO_INVALID")
    channels, rate, byte_rate, block_align, bits = fmt
    if bits != 16 or channels not in (1, 2) or rate not in ALLOWED_RATES:
        raise ApiError("AUDIO_INVALID")
    if block_align != channels * 2 or byte_rate != rate * block_align:
        raise ApiError("AUDIO_INVALID")
    frames = len(pcm) // block_align
    if frames == 0:
        raise ApiError("AUDIO_INVALID")
    duration = frames / rate
    if duration > max_duration_s + 1e-6:
        raise ApiError("AUDIO_TOO_LONG")
    samples = np.frombuffer(pcm[: frames * block_align], dtype="<i2").reshape(frames, channels)
    return DecodedAudio(to_mono_16k(samples, rate), rate, channels, duration)


def to_mono_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    """int16 [frames, channels] -> int16 mono 16 kHz using a sinc resampler (soxr HQ)."""
    x = samples.astype(np.float32) / 32768.0
    if x.ndim == 2:
        x = x.mean(axis=1)
    if rate != TARGET_RATE:
        x = soxr.resample(x, rate, TARGET_RATE, quality="HQ")
    return float_to_pcm16(x)


def float_to_pcm16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767.0).round().astype(np.int16)


def encode_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, PCM_FORMAT, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16,
        b"data", len(pcm),
    )
    return header + pcm


def wav_pcm(data: bytes) -> tuple[bytes, int]:
    """Return (PCM16 mono bytes, sample_rate) of a mono PCM16 WAV produced by the TTS worker."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, rate = 12, None
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            _, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, pos + 8)
            if channels != 1 or bits != 16:
                raise ValueError("expected mono PCM16")
        elif cid == b"data":
            if rate is None:
                raise ValueError("data before fmt")
            end = len(data) if size in (0, 0xFFFFFFFF) else pos + 8 + size
            return data[pos + 8 : end], rate
        pos += 8 + size + (size & 1)
    raise ValueError("no data chunk")
