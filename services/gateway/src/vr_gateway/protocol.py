"""Binary audio envelope (PROTOCOL §6.3): u32 LE header_len + UTF-8 JSON header + PCM16LE payload."""

from __future__ import annotations

import json
import struct

MAX_HEADER = 1024
MAX_PAYLOAD = 64 * 1024
INPUT_RATE = 16000


class FrameError(ValueError):
    pass


def pack_frame(header: dict, payload: bytes) -> bytes:
    raw = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(raw)) + raw + payload


def unpack_input_frame(data: bytes, session_id: str) -> tuple[dict, bytes]:
    """Validate an input_audio frame; raise FrameError with a short reason otherwise."""
    if len(data) < 4:
        raise FrameError("short")
    header_len = struct.unpack_from("<I", data)[0]
    if header_len == 0 or header_len > MAX_HEADER or 4 + header_len > len(data):
        raise FrameError("header_len")
    try:
        header = json.loads(data[4 : 4 + header_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("header_json") from exc
    if not isinstance(header, dict):
        raise FrameError("header_json")
    payload = data[4 + header_len :]
    if header.get("v") != 1:
        raise FrameError("version")
    if header.get("kind") != "input_audio":
        raise FrameError("kind")
    if header.get("session_id") != session_id:
        raise FrameError("session")
    count = header.get("sample_count")
    seq = header.get("seq")
    if not isinstance(count, int) or not isinstance(seq, int) or isinstance(count, bool) or count < 0 or seq < 0:
        raise FrameError("fields")
    if header.get("sample_rate") != INPUT_RATE:
        raise FrameError("sample_rate")
    if len(payload) > MAX_PAYLOAD:
        raise FrameError("payload_size")
    if len(payload) != count * 2:
        raise FrameError("sample_count")
    turn_id = header.get("turn_id")
    if turn_id is not None and (not isinstance(turn_id, str) or len(turn_id) > 64):
        raise FrameError("turn_id")
    return header, payload
