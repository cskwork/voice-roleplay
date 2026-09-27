"""Aggregated readiness of workers and supported modes (GET /api/health)."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Services


class HealthMonitor:
    def __init__(self, services: Services):
        self.svc = services
        self._snapshot: dict | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()
        self._manifest_seen: list | None = None

    async def snapshot(self, force: bool = False) -> dict:
        async with self._lock:
            if not force and self._snapshot and time.monotonic() - self._at < self.svc.config.health_cache_s:
                return self._snapshot
            asr, tts, llm = await asyncio.gather(self.svc.asr.health(), self.svc.tts.health(), self._llm())
            self._snapshot = self._build(asr, tts, llm)
            self._at = time.monotonic()
            self._record_manifest(asr, tts, llm)
            return self._snapshot

    async def _llm(self) -> bool:
        try:
            return bool(await self.svc.llm.health())
        except Exception:
            return False

    def _build(self, asr: dict | None, tts: dict | None, llm_ok: bool) -> dict:
        asr_ready = bool(asr and asr.get("ready"))
        tts_ready = bool(tts and tts.get("ready"))
        vad_ready = self.svc.vad_model is not None
        workers = {
            "asr": {"reachable": asr is not None, "ready": asr_ready,
                    **{k: asr.get(k) for k in ("model_id", "revision", "device", "backend", "streaming_mode") if asr}},
            "tts": {"reachable": tts is not None, "ready": tts_ready,
                    **{k: tts.get(k) for k in ("model_id", "revision", "device", "sample_rate", "voices") if tts}},
            "llm": {"reachable": llm_ok, "ready": llm_ok, "model_revision": self.svc.config.llm_model_revision},
            "vad": {"ready": vad_ready, "model": "silero-vad 6.2.3 (onnx, cpu)"},
        }
        rt_missing = [n for n, ok in (("ASR", asr_ready), ("TTS", tts_ready), ("LLM", llm_ok), ("VAD", vad_ready)) if not ok]
        rec_missing = [n for n, ok in (("ASR", asr_ready), ("VAD", vad_ready)) if not ok]
        busy = self.svc.sessions.active_realtime() is not None
        modes = {
            "realtime": {
                "available": not rt_missing,
                "reason_ko": f"준비되지 않은 모델: {', '.join(rt_missing)}" if rt_missing else None,
                "session_active": busy,
            },
            "recorded": {
                "available": not rec_missing,
                "reason_ko": f"준비되지 않은 모델: {', '.join(rec_missing)}" if rec_missing else None,
                "feedback_available": llm_ok,
                "model_audio_available": tts_ready,
                "blocked_by_realtime": busy,
            },
        }
        return {
            "gateway": {"ready": True, "protocol_version": 1},
            "workers": workers,
            "modes": modes,
            "benchmark": {"status": "not_measured"},
            "pronunciation_assessment": "assessment_unavailable",
        }

    def _record_manifest(self, asr: dict | None, tts: dict | None, llm_ok: bool) -> None:
        """Keep model_manifest in step with what the workers report (diagnostics, PRD §13.4)."""
        seen = []
        for info, runtime in ((asr, "asr"), (tts, "tts")):
            if info and info.get("ready") and info.get("model_id"):
                seen.append((info["model_id"], info.get("revision"), f"{runtime}:{info.get('device', '')}"))
        if llm_ok:
            seen.append(("llm", self.svc.config.llm_model_revision, "llama.cpp"))
        if seen != self._manifest_seen:
            self._manifest_seen = seen
            for row in seen:
                self.svc.db.upsert_manifest(*row)

    def realtime_available(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["modes"]["realtime"]["available"])

    def recorded_available(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["modes"]["recorded"]["available"])

    def llm_ready(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["workers"]["llm"]["ready"])

    def tts_ready(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["workers"]["tts"]["ready"])
