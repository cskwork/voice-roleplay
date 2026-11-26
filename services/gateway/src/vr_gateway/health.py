"""Aggregated readiness of workers and supported modes (GET /api/health).

In API mode the "workers" are the engine adapters: a hosted one reports ready when its key is configured and a CLI when
it is installed, without a network call (PROTOCOL §6.2); Kokoro and the local ASR worker report their model state.
`mode_info()` adds `mode`, `shared` and, in API mode, `api_keys` (presence only, for the keys the chosen engines need)
and `engines` (llm / asr / tts).
"""

from __future__ import annotations

import asyncio
import time

from .config import API_KEY_ENV
from .engines import NEEDS_KEY
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
            asr, tts, llm, pron = await asyncio.gather(self.svc.asr.health(), self.svc.tts.health(), self._llm(),
                                                       self._pron())
            self._snapshot = self._build(asr, tts, llm, pron)
            self._at = time.monotonic()
            self._record_manifest(asr, tts, llm, pron)
            return self._snapshot

    async def _llm(self) -> bool:
        try:
            return bool(await self.svc.llm.health())
        except Exception:
            return False

    async def _pron(self) -> dict | None:
        return await self.svc.pron.health() if self.svc.pron is not None else None

    def _build(self, asr: dict | None, tts: dict | None, llm_ok: bool, pron: dict | None = None) -> dict:
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
        pron_status = "assessment_unavailable"
        if self.svc.pron is not None:  # optional worker (PROTOCOL §12): listed only when configured
            pron_ready = bool(pron and pron.get("ready"))
            workers["pron"] = {"reachable": pron is not None, "ready": pron_ready,
                               **{k: pron.get(k) for k in ("device", "models", "bands_enabled", "calibration_version")
                                  if pron}}
            if pron_ready:
                pron_status = "experimental_banded" if pron.get("bands_enabled") else "timing_only"
        rt_missing = [n for n, ok in (("ASR", asr_ready), ("TTS", tts_ready), ("LLM", llm_ok), ("VAD", vad_ready)) if not ok]
        rec_missing = [n for n, ok in (("ASR", asr_ready), ("VAD", vad_ready)) if not ok]
        if self.svc.config.mode == "api":
            # Per part: a hosted engine names its missing key variable (never the value); a CLI or a model on this
            # machine says what is not ready.
            eng = self.svc.config.api.engines()
            local = {"claude-cli": "Claude CLI(이 PC 로그인)", "codex-cli": "Codex CLI(이 PC 로그인)",
                     "local": "이 PC 음성 인식 모델(Qwen3-ASR)", "kokoro": "이 PC 목소리 모델(Kokoro)"}
            part = {"ASR": eng["asr"], "TTS": eng["tts"], "LLM": eng["llm"]}

            def why(missing: list[str]) -> str | None:
                if not missing:
                    return None
                keys = [API_KEY_ENV[NEEDS_KEY[part[n]]] for n in missing if n in part and part[n] in NEEDS_KEY]
                here = [local[part[n]] for n in missing if n in part and part[n] in local]
                bits = ([f"API 키가 설정되지 않음: {', '.join(dict.fromkeys(keys))}"] if keys else []) + \
                       ([f"준비되지 않음: {', '.join(dict.fromkeys(here))}"] if here else []) + \
                       (["준비되지 않은 모델: VAD"] if "VAD" in missing else [])
                return " · ".join(bits)

            rt_reason = why(rt_missing)
            rec_reason = why(rec_missing)
        else:
            rt_reason = f"준비되지 않은 모델: {', '.join(rt_missing)}" if rt_missing else None
            rec_reason = f"준비되지 않은 모델: {', '.join(rec_missing)}" if rec_missing else None
        # Shared mode: other learners' sessions never block anyone, so "session_active" stays false there.
        busy = self.svc.sessions.active_realtime() is not None and not self.svc.config.shared
        modes = {
            "realtime": {
                "available": not rt_missing,
                "reason_ko": rt_reason,
                "session_active": busy,
            },
            "recorded": {
                "available": not rec_missing,
                "reason_ko": rec_reason,
                "feedback_available": llm_ok,
                "model_audio_available": tts_ready,
                # Kept for compatibility, always false: starting recorded work ends the realtime session (PRD §7).
                "blocked_by_realtime": False,
            },
        }
        return {
            "gateway": {"ready": True, "protocol_version": 1},
            "workers": workers,
            "modes": modes,
            "benchmark": {"status": "not_measured"},
            "pronunciation_assessment": pron_status,
        }

    def _record_manifest(self, asr: dict | None, tts: dict | None, llm_ok: bool, pron: dict | None = None) -> None:
        """Keep model_manifest in step with what the workers report (diagnostics, PRD §13.4)."""
        seen = []
        for info, runtime in ((asr, "asr"), (tts, "tts")):
            if info and info.get("ready") and info.get("model_id"):
                seen.append((info["model_id"], info.get("revision"), f"{runtime}:{info.get('device', '')}"))
        if pron and pron.get("ready"):
            for model in (pron.get("models") or {}).values():
                if model and model.get("model_id"):
                    seen.append((model["model_id"], model.get("revision"), f"pron:{pron.get('device', '')}"))
        if llm_ok:
            cfg = self.svc.config
            seen.append(("llm", cfg.llm_model_revision, cfg.api.llm_provider if cfg.mode == "api" else "llama.cpp"))
        if seen != self._manifest_seen:
            self._manifest_seen = seen
            for row in seen:
                self.svc.db.upsert_manifest(*row)

    def mode_info(self) -> dict:
        cfg = self.svc.config
        info: dict = {"mode": cfg.mode, "shared": cfg.shared}
        if cfg.mode == "api":
            info["api_keys"] = cfg.api.key_presence()  # booleans only, for the keys the chosen engines need
            info["engines"] = cfg.api.engines()
        if cfg.shared:
            info["sessions"] = {"live": len(self.svc.sessions.live_realtime()), "max": cfg.max_sessions,
                                "max_session_s": cfg.max_session_s}
        return info

    def realtime_available(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["modes"]["realtime"]["available"])

    def recorded_available(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["modes"]["recorded"]["available"])

    def llm_ready(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["workers"]["llm"]["ready"])

    def pron_state(self) -> dict | None:
        """The pronunciation worker's entry of the last snapshot (None when not configured)."""
        snap = self._snapshot
        return snap["workers"].get("pron") if snap else None

    def tts_voice_ids(self) -> set[str] | None:
        """Voice ids the TTS worker listed in the last snapshot; None when not known (no snapshot, no voices yet)."""
        snap = self._snapshot
        voices = snap["workers"]["tts"].get("voices") if snap else None
        return {v["voice_id"] for v in voices} if voices else None

    def tts_ready(self) -> bool:
        snap = self._snapshot
        return bool(snap and snap["workers"]["tts"]["ready"])
