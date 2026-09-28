"""Pre-synthesized audio for reviewed scenario texts (opening lines, model expressions, exercises).

Service asset cache under var/cache/tts/ (not user data): keyed by voice + text_id + speed + text hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ApiError
from .scenarios import text_entries

if TYPE_CHECKING:
    from .app import Services

log = logging.getLogger("vr_gateway.tts_cache")
VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class TtsCache:
    def __init__(self, services: Services, directory: Path):
        self.svc = services
        self.dir = directory
        self._locks: dict[str, asyncio.Lock] = {}
        # Warm-up progress for /api/health; `./app start` waits for the opening lines (PRD §14.3 예열).
        self.status = "idle"  # idle | warming | paused | done
        self.openings_total = 0
        self.openings_ready = 0
        self.openings_failed = 0

    def progress(self) -> dict:
        return {"status": self.status, "openings_total": self.openings_total,
                "openings_ready": self.openings_ready, "openings_failed": self.openings_failed}

    def _path(self, voice_id: str, text_id: str, speed: float, text: str) -> Path:
        digest = hashlib.sha256(text.encode()).hexdigest()[:12]
        return self.dir / voice_id / f"{text_id}__{speed:.2f}__{digest}.wav"

    def _entry(self, text_id: str) -> dict:
        entry = self.svc.scenarios.text(text_id)
        if entry is None:
            raise ApiError("NOT_FOUND")
        return entry

    def get(self, voice_id: str, text_id: str, speed: float = 1.0) -> bytes | None:
        if not VOICE_RE.match(voice_id):
            raise ApiError("NOT_FOUND")
        path = self._path(voice_id, text_id, speed, self._entry(text_id)["en"])
        return path.read_bytes() if path.exists() else None

    async def ensure(self, voice_id: str, text_id: str, speed: float = 1.0) -> bytes:
        cached = self.get(voice_id, text_id, speed)
        if cached is not None:
            return cached
        text = self._entry(text_id)["en"]
        path = self._path(voice_id, text_id, speed, text)
        lock = self._locks.setdefault(str(path), asyncio.Lock())
        async with lock:
            if path.exists():
                return path.read_bytes()
            started = time.perf_counter()
            wav = await self.svc.tts.synthesize_wav(voice_id, self.svc.brain.normalize_for_tts(text), speed)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(wav)
            tmp.replace(path)
            log.info("tts_cached text_id=%s voice=%s bytes=%d ms=%d", text_id, voice_id, len(wav),
                     int((time.perf_counter() - started) * 1000))
            return wav

    async def warm(self) -> None:
        """Opening lines first (instant first AI turn), then the rest of the reviewed texts.

        The TTS worker is far from real time on this machine and shares the GPU with ASR and the LLM, so warming
        pauses while a realtime session is active (PRD §14.2: no background work competing with a live
        conversation). A session's own opening line is synthesized on demand if it is not cached yet."""
        jobs: list[tuple[str, str]] = []
        scenarios = self.svc.scenarios.summaries()
        for sc in scenarios:
            if sc.get("opening_line"):
                jobs.append((sc["default_voice_id"], sc["opening_line"]["text_id"]))
        for sc in scenarios:
            for entry in text_entries(sc):
                key = (sc["default_voice_id"], entry["text_id"])
                if key not in jobs:
                    jobs.append(key)
        self.openings_total = openings = sum(1 for sc in scenarios if sc.get("opening_line"))
        done = failed = 0
        for i, (voice_id, text_id) in enumerate(jobs):
            while self.svc.sessions.active_realtime() is not None:
                self.status = "paused"
                await asyncio.sleep(2)
            self.status = "warming"
            try:
                await self.ensure(voice_id, text_id)
                done += 1
                self.openings_ready += i < openings
            except Exception as exc:
                failed += 1
                self.openings_failed += i < openings
                log.warning("tts_cache_warm_failed text_id=%s error=%s", text_id, type(exc).__name__)
        self.status = "done"
        log.info("tts_cache_warm done=%d failed=%d", done, failed)
