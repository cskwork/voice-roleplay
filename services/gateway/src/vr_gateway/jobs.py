"""Recorded-practice attempts and the non-realtime job queue (PROTOCOL §7, PRD §7).

One running job, at most 2 queued, audio in memory only, 5-minute TTL while queued.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import vad as vadmod
from .audio import DecodedAudio
from .errors import ApiError
from .scenarios import asr_context
from .workers import WorkerError

if TYPE_CHECKING:
    from .app import Services

log = logging.getLogger("vr_gateway.jobs")

EXERCISE_TYPES = ("reading", "shadowing", "free_answer", "roleplay_turn", "drill")
TARGET_TYPES = ("reading", "shadowing", "drill")  # the learner says a given sentence; never sent to ASR
TERMINAL = ("completed", "failed", "cancelled", "expired")
MIN_SPEECH_S = 0.25
DIFF_LABEL_KO = "다르게 인식된 부분"


@dataclass
class Attempt:
    attempt_id: str
    exercise_type: str
    scenario_id: str | None
    exercise_ref: str | None
    session_id: str | None
    history_opt_in: bool
    attempt_index: int
    target_en: str | None = None
    question_en: str | None = None
    sample_answer_en: str | None = None
    created_at: float = field(default_factory=time.time)
    audio: DecodedAudio | None = None
    audio_released: bool = False
    audio_info: dict | None = None
    revisions: list[dict] = field(default_factory=list)  # [{revision, text, source}]
    feedback: dict[int, dict] = field(default_factory=dict)  # revision -> {status, items, reason?}
    metrics: dict | None = None
    target_diff: list[dict] | None = None
    model_audio: dict[str, dict] = field(default_factory=dict)  # audio_id -> {kind, text, wav}
    next_ai: dict | None = None
    no_speech: bool = False
    finished_at: float | None = None
    last_job_id: str | None = None

    def latest_revision(self) -> int:
        return self.revisions[-1]["revision"] if self.revisions else 0

    def text(self, revision: int | None = None) -> str:
        revision = revision or self.latest_revision()
        return next((r["text"] for r in self.revisions if r["revision"] == revision), "")


@dataclass
class Job:
    job_id: str
    attempt_id: str
    kind: str  # analyze | reanalyze
    idempotency_key: str
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    transcript_revision: int | None = None
    error_code: str | None = None
    audio: DecodedAudio | None = None
    task: asyncio.Task | None = None

    def public(self) -> dict:
        return {
            "job_id": self.job_id, "attempt_id": self.attempt_id, "kind": self.kind, "state": self.state,
            "error_code": self.error_code, "transcript_revision": self.transcript_revision,
            "created_at": self.created_at,
        }


class JobManager:
    def __init__(self, services: Services):
        self.svc = services
        self.attempts: dict[str, Attempt] = {}
        self.jobs: dict[str, Job] = {}
        self.by_key: dict[str, str] = {}
        self.queue: collections.deque[Job] = collections.deque()
        self.running: Job | None = None
        self._wake = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._index: dict[tuple, int] = {}

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        expired = self.svc.db.expire_unfinished_jobs()
        if expired:
            log.info("jobs_expired_on_start count=%d", expired)
        self._runner = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
        if self.running and self.running.task:
            self.running.task.cancel()
        for job in list(self.queue):
            self._finish(job, "cancelled")
        self.queue.clear()

    # ------------------------------------------------------------ attempts

    def create_attempt(self, body: dict) -> Attempt:
        etype = body.get("exercise_type")
        if etype not in EXERCISE_TYPES:
            raise ApiError("UNSUPPORTED_MODE")
        if etype == "drill" and not body.get("scenario_id"):
            scenario = None  # a saved review expression may not belong to any scenario
        else:
            scenario = self.svc.scenarios.get(body.get("scenario_id") or "")
            if scenario is None:
                raise ApiError("NOT_FOUND")
        if (etype == "drill") != ("target_text" in body):
            raise ApiError("INVALID_REQUEST")  # target_text is required for drill and only allowed there
        ref = body.get("text_id") or body.get("exercise_id")
        target = question = sample = None
        session_id = None
        if etype == "drill":
            # "다시 말하기": a suggestion or saved expression, handled like reading (PROTOCOL §7).
            target = " ".join(body["target_text"].split())
            if body.get("session_id"):
                session_id = self.svc.sessions.get(body["session_id"]).session_id
            ref = None
        elif etype in ("reading", "shadowing"):
            entry = self.svc.scenarios.text(ref or "")
            if entry is None or entry["scenario_id"] != scenario["scenario_id"]:
                raise ApiError("NOT_FOUND")
            target = entry["en"]
        elif etype == "free_answer":
            ex = self.svc.scenarios.free_answer(scenario["scenario_id"], ref or "")
            if ex is None:
                raise ApiError("NOT_FOUND")
            question, sample = ex["question_en"], ex.get("sample_answer_en")
        else:
            session = self.svc.sessions.get(body.get("session_id", ""))
            if session.mode != "turn_based" or session.state == "ended":
                raise ApiError("INVALID_STATE")
            session_id = session.session_id
        settings = self.svc.settings()
        scenario_id = scenario["scenario_id"] if scenario else None
        key = (scenario_id, etype, target if etype == "drill" else ref or session_id)
        self._index[key] = self._index.get(key, 0) + 1
        attempt = Attempt(
            attempt_id="a_" + uuid.uuid4().hex[:16], exercise_type=etype, scenario_id=scenario_id,
            exercise_ref=ref, session_id=session_id,
            history_opt_in=bool(body.get("history_opt_in", settings.get("history_opt_in", False))),
            attempt_index=self._index[key], target_en=target, question_en=question, sample_answer_en=sample,
        )
        self.attempts[attempt.attempt_id] = attempt
        return attempt

    def get_attempt(self, attempt_id: str) -> Attempt:
        self.purge()
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            raise ApiError("NOT_FOUND")
        return attempt

    def set_audio(self, attempt: Attempt, audio: DecodedAudio) -> dict:
        if attempt.last_job_id is not None:
            raise ApiError("INVALID_STATE")  # a submitted take is final; record a new attempt instead
        self._ensure_not_busy()
        attempt.audio = audio
        attempt.audio_info = {
            "duration_ms": int(audio.duration_s * 1000), "source_rate": audio.source_rate,
            "source_channels": audio.source_channels, "samples_16k": audio.num_samples,
        }
        return attempt.audio_info

    # ------------------------------------------------------------ jobs

    def _ensure_not_busy(self) -> None:
        if self.svc.sessions.active_realtime() is not None:
            raise ApiError("LOCAL_BUSY")

    def _idempotent(self, key: str, attempt_id: str, kind: str) -> Job | dict | None:
        job_id = self.by_key.get(key)
        if job_id:
            job = self.jobs[job_id]
            if job.attempt_id != attempt_id or job.kind != kind:
                raise ApiError("IDEMPOTENCY_CONFLICT")
            return job
        row = self.svc.db.job_by_key(key)
        if row:  # from a previous gateway run: audio is gone, report the stored state
            if row["attempt_id"] != attempt_id or row["kind"] != kind:
                raise ApiError("IDEMPOTENCY_CONFLICT")
            return {k: row[k] for k in ("job_id", "attempt_id", "kind", "state", "error_code", "transcript_revision",
                                        "created_at")}
        return None

    def _enqueue(self, job: Job) -> None:
        if len(self.queue) >= self.svc.config.job_queue_capacity:
            raise ApiError("QUEUE_FULL")
        job.expires_at = job.created_at + self.svc.config.job_ttl_s
        self.jobs[job.job_id] = job
        self.by_key[job.idempotency_key] = job.job_id
        self.svc.db.insert_job({
            "job_id": job.job_id, "attempt_id": job.attempt_id, "kind": job.kind, "state": job.state,
            "idempotency_key": job.idempotency_key, "transcript_revision": job.transcript_revision,
            "created_at": job.created_at, "expires_at": job.expires_at,
        })
        self.queue.append(job)
        self._wake.set()
        log.info("job_queued job=%s kind=%s queue=%d", job.job_id, job.kind, len(self.queue))

    def submit(self, attempt: Attempt, key: str) -> tuple[dict, bool]:
        """Returns (job, created)."""
        existing = self._idempotent(key, attempt.attempt_id, "analyze")
        if existing is not None:
            return (existing.public() if isinstance(existing, Job) else existing), False
        self._ensure_not_busy()
        if attempt.last_job_id is not None:
            raise ApiError("INVALID_STATE")
        if attempt.audio is None:
            raise ApiError("AUDIO_EXPIRED" if attempt.audio_released else "INVALID_STATE")
        job = Job(job_id="j_" + uuid.uuid4().hex[:16], attempt_id=attempt.attempt_id, kind="analyze",
                  idempotency_key=key, audio=attempt.audio)
        self._enqueue(job)
        attempt.audio = None  # the job owns the only copy now
        attempt.last_job_id = job.job_id
        return job.public(), True

    def resubmit_transcript(self, attempt: Attempt, text: str, key: str) -> tuple[dict, bool]:
        existing = self._idempotent(key, attempt.attempt_id, "reanalyze")
        if existing is not None:
            return (existing.public() if isinstance(existing, Job) else existing), False
        self._ensure_not_busy()
        if not attempt.revisions:
            raise ApiError("INVALID_STATE")
        text = " ".join(text.split())
        if not text or len(text) > 2000:
            raise ApiError("INVALID_REQUEST")
        revision = attempt.latest_revision() + 1
        job = Job(job_id="j_" + uuid.uuid4().hex[:16], attempt_id=attempt.attempt_id, kind="reanalyze",
                  idempotency_key=key, transcript_revision=revision)
        self._enqueue(job)
        attempt.revisions.append({"revision": revision, "text": text, "source": "user", "created_at": time.time()})
        attempt.last_job_id = job.job_id
        return job.public(), True

    def get_job(self, job_id: str) -> Job:
        self._sweep()
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError("NOT_FOUND")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if job.state in TERMINAL:
            return job
        if job in self.queue:
            self.queue.remove(job)
            self._finish(job, "cancelled")
        elif job.task is not None:
            job.task.cancel()  # the runner records the state
            job.state = "cancelled"
        return job

    def _finish(self, job: Job, state: str, error_code: str | None = None, conn=None) -> None:
        job.state = state
        job.error_code = error_code
        job.audio = None
        attempt = self.attempts.get(job.attempt_id)
        if attempt is not None:
            attempt.finished_at = time.time()
            if state != "completed" and job.kind == "analyze":
                attempt.audio_released = True
        self.svc.db.set_job_state(job.job_id, state, error_code, conn=conn)
        log.info("job_%s job=%s code=%s", state, job.job_id, error_code)

    def _sweep(self) -> None:
        now = time.time()
        for job in [j for j in self.queue if j.expires_at and now > j.expires_at]:
            self.queue.remove(job)
            self._finish(job, "expired", "AUDIO_EXPIRED")

    def purge(self) -> None:
        ttl = self.svc.config.summary_ttl_s
        now = time.time()
        for aid in [a.attempt_id for a in self.attempts.values() if a.finished_at and now - a.finished_at > ttl]:
            del self.attempts[aid]

    async def _run(self) -> None:
        while True:
            self._sweep()
            if not self.queue:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            job = self.queue.popleft()
            self.running = job
            job.task = asyncio.create_task(self._process(job))
            try:
                await job.task
            except asyncio.CancelledError:
                self._finish(job, "cancelled")
                if asyncio.current_task().cancelling():
                    raise  # the runner itself is stopping
            except Exception as exc:
                code = exc.code if isinstance(exc, (ApiError, WorkerError)) else "WORKER_FAILED"
                if code not in ("OUT_OF_MEMORY", "MODEL_NOT_READY", "AUDIO_TOO_LONG"):
                    code = "WORKER_FAILED"
                log.error("job_error job=%s error=%s", job.job_id, type(exc).__name__)
                self._finish(job, "failed", code)
            finally:
                self.running = None

    def _set(self, job: Job, state: str) -> None:
        job.state = state
        self.svc.db.set_job_state(job.job_id, state)

    # ------------------------------------------------------------ processing

    async def _process(self, job: Job) -> None:
        attempt = self.attempts[job.attempt_id]
        started = time.perf_counter()
        if job.kind == "analyze":
            await self._analyze(job, attempt)
        else:
            await self._reanalyze(job, attempt)
        with self.svc.db.tx() as conn:
            if attempt.history_opt_in:
                self._persist(attempt, conn)
            self._finish(job, "completed", conn=conn)
        log.info("job_done job=%s kind=%s ms=%d", job.job_id, job.kind, int((time.perf_counter() - started) * 1000))

    async def _analyze(self, job: Job, attempt: Attempt) -> None:
        svc = self.svc
        audio = job.audio
        self._set(job, "transcribing")
        voiced = await asyncio.to_thread(vadmod.analyze, svc.vad_model, audio.pcm16k)
        if sum(e - s for s, e in voiced) < MIN_SPEECH_S:
            # Silence or too little speech: no ASR/LLM call, no failure record (PRD §17).
            attempt.no_speech = True
            attempt.metrics = None
            job.audio = None
            return
        scenario = svc.scenarios.get(attempt.scenario_id)
        # Reading/shadowing/drill targets are never given to ASR as context (PROTOCOL §7).
        context = None if attempt.exercise_type in TARGET_TYPES else asr_context(scenario)
        pcm = audio.pcm16k.tobytes()
        try:
            result = await svc.asr.transcribe(pcm, context)
        except WorkerError as exc:
            if exc.code != "WORKER_FAILED":
                raise
            result = await svc.asr.transcribe(pcm, context)  # one retry for transient failures
        job.audio = None
        del pcm
        text = (result.get("text") or "").strip()
        attempt.revisions = [{"revision": 1, "text": text, "source": "asr", "created_at": time.time()}]
        job.transcript_revision = 1

        self._set(job, "analyzing")
        attempt.metrics = svc.brain.compute_metrics(voiced, text)
        if attempt.target_en is not None:
            attempt.target_diff = svc.brain.diff_target(attempt.target_en, text)
        if attempt.exercise_type in ("free_answer", "roleplay_turn"):
            attempt.feedback[1] = await self._feedback(attempt, 1, "asr_text")
        if attempt.exercise_type == "roleplay_turn" and text:
            await self._next_ai(attempt, text)

        self._set(job, "synthesizing")
        await self._model_audio(attempt, scenario)

    async def _reanalyze(self, job: Job, attempt: Attempt) -> None:
        self._set(job, "analyzing")
        revision = job.transcript_revision
        if attempt.exercise_type in ("free_answer", "roleplay_turn"):
            attempt.feedback[revision] = await self._feedback(attempt, revision, "user_confirmed_text")
        else:
            attempt.feedback[revision] = {"status": "ok", "items": []}

    async def _feedback(self, attempt: Attempt, revision: int, evidence_type: str) -> dict:
        svc = self.svc
        await svc.health.snapshot()
        if not svc.health.llm_ready():
            return {"status": "unavailable", "items": [], "reason": "llm_not_ready"}
        scenario = svc.scenarios.get(attempt.scenario_id) or {}
        return await svc.brain.generate_feedback(
            svc.llm, transcript=attempt.text(revision), transcript_revision=revision,
            source={"attempt_id": attempt.attempt_id},
            context={"exercise_type": attempt.exercise_type, "scenario_title_en": scenario.get("title_en"),
                     "question_en": attempt.question_en, "evidence_type": evidence_type},
            max_items=3, model_revision=svc.config.llm_model_revision,
        )

    async def _next_ai(self, attempt: Attempt, text: str) -> None:
        """Turn-based roleplay: the AI's next line, kept separate from the feedback."""
        svc = self.svc
        session = svc.sessions.get(attempt.session_id)
        await svc.health.snapshot()
        if not svc.health.llm_ready():
            attempt.next_ai = {"status": "unavailable"}
            return
        messages = svc.brain.build_roleplay_messages(session.scenario, session.difficulty, session.goals,
                                                     list(session.history), text)
        parts = [delta async for delta in svc.llm.stream_chat(messages, max_tokens=128)]
        segmenter = svc.brain.segmenter_factory()
        segments = segmenter.feed("".join(parts)) + segmenter.flush()
        reply = " ".join(segments).strip()
        turn_index = session.next_turn_index()
        session.history += [{"role": "user", "text": text}, {"role": "assistant", "text": reply}]
        session.record_turn(svc, {"turn_id": attempt.attempt_id, "turn_index": turn_index, "role": "user",
                                  "text": text, "transcript_revision": 1, "metrics": attempt.metrics})
        session.record_turn(svc, {"turn_id": attempt.attempt_id + "_ai", "turn_index": session.next_turn_index(),
                                  "role": "assistant", "text": reply, "playback_status": "text_only"})
        attempt.next_ai = {"status": "ok", "text": reply}

    async def _model_audio(self, attempt: Attempt, scenario: dict | None) -> None:
        svc = self.svc
        await svc.health.snapshot()
        if not svc.health.tts_ready():
            return
        voice = svc.settings().get("voice_id") or (scenario or {}).get("default_voice_id", "")
        wanted: list[tuple[str, str | None, str]] = []  # (kind, text_id, text)
        if attempt.exercise_type in ("reading", "shadowing"):
            wanted.append(("target", attempt.exercise_ref, attempt.target_en))
        items = (attempt.feedback.get(1) or {}).get("items", [])
        suggestion = next((i["suggestion"] for i in items if i.get("suggestion")), None)
        if suggestion:
            wanted.append(("suggestion", None, suggestion))
        elif attempt.exercise_type == "free_answer" and attempt.sample_answer_en:
            wanted.append(("sample_answer", None, attempt.sample_answer_en))
        if attempt.next_ai and attempt.next_ai.get("text"):
            wanted.append(("next_ai", None, attempt.next_ai["text"]))
        for kind, text_id, text in wanted:
            try:
                if text_id:
                    wav = await svc.tts_cache.ensure(voice, text_id)
                else:
                    wav = await svc.tts.synthesize_wav(voice, svc.brain.normalize_for_tts(text)[:400])
            except Exception as exc:  # text results stay valid when only audio fails (PRD §17)
                log.warning("model_audio_failed attempt=%s kind=%s error=%s", attempt.attempt_id, kind,
                            type(exc).__name__)
                continue
            audio_id = kind if kind != "next_ai" else "next_ai"
            attempt.model_audio[audio_id] = {"kind": kind, "text": text, "wav": wav}

    def _persist(self, attempt: Attempt, conn) -> None:
        db = self.svc.db
        db.upsert_attempt({
            "attempt_id": attempt.attempt_id, "session_id": attempt.session_id,
            "exercise_type": attempt.exercise_type, "scenario_id": attempt.scenario_id,
            "exercise_ref": attempt.exercise_ref, "attempt_index": attempt.attempt_index,
            "result_version": attempt.latest_revision(), "metrics": attempt.metrics,
            "target_diff": attempt.target_diff, "created_at": attempt.created_at,
            "extra": {"no_speech": attempt.no_speech, "next_ai": attempt.next_ai,
                      **({"target_en": attempt.target_en} if attempt.exercise_type == "drill" else {})},
        }, conn=conn)
        for rev in attempt.revisions:
            db.insert_revision("attempt", attempt.attempt_id, rev["revision"], rev["text"], rev["source"], conn)
        for fb in attempt.feedback.values():
            db.insert_feedback("attempt", attempt.attempt_id, fb.get("items", []), conn)
            for item in fb.get("items", []):
                if item.get("suggestion") and item.get("status") in ("observed", "suggested"):
                    db.add_review_item({
                        "item_id": "r_" + uuid.uuid4().hex[:16], "text_en": item["suggestion"],
                        "text_ko": item.get("explanation_ko"), "source_type": "attempt",
                        "source_id": attempt.attempt_id,
                    }, conn=conn)

    # ------------------------------------------------------------ result

    def result(self, attempt: Attempt) -> dict:
        job = self.jobs.get(attempt.last_job_id) if attempt.last_job_id else None
        latest = attempt.latest_revision()
        fb_rev = max((r for r in attempt.feedback if r <= latest), default=None)
        feedback = attempt.feedback.get(fb_rev) if fb_rev else None
        return {
            "attempt_id": attempt.attempt_id,
            "exercise_type": attempt.exercise_type,
            "scenario_id": attempt.scenario_id,
            "exercise_ref": attempt.exercise_ref,
            "attempt_index": attempt.attempt_index,
            "job": job.public() if job else None,
            "no_speech": attempt.no_speech,
            "transcript_revision": latest or None,
            "transcript": attempt.text() if latest else None,
            "original_transcript": attempt.text(1) if latest else None,
            "revisions": [{k: r[k] for k in ("revision", "text", "source")} for r in attempt.revisions],
            "feedback": feedback["items"] if feedback else [],
            "feedback_status": feedback["status"] if feedback else None,
            "feedback_revision": fb_rev,
            "feedback_by_revision": {str(k): v for k, v in sorted(attempt.feedback.items())},
            # Metrics and the target diff describe the recorded audio (revision 1) and are never recomputed.
            "metrics": attempt.metrics,
            "metrics_revision": 1 if attempt.metrics else None,
            "target_en": attempt.target_en,
            "target_diff": attempt.target_diff,
            "target_diff_label_ko": DIFF_LABEL_KO if attempt.target_diff is not None else None,
            "question_en": attempt.question_en,
            "next_ai": attempt.next_ai,
            "model_audio": [
                {"audio_id": aid, "kind": m["kind"], "text": m["text"],
                 "url": f"/api/attempts/{attempt.attempt_id}/model-audio/{aid}"}
                for aid, m in attempt.model_audio.items()
            ],
            "audio": attempt.audio_info,
            "pronunciation_score": None,
            "pronunciation_status": "assessment_unavailable",
        }
