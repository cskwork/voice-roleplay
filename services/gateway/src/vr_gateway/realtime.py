"""Realtime roleplay engine: one instance per WebSocket connection (PROTOCOL §6.3).

Audio in -> Silero VAD -> turn taking -> ASR stream -> final -> LLM stream -> segmenter -> TTS -> audio out.
All sends go through one lock that also guards epoch/response cancellation, so once `response.cancelled`
is sent no frame or text of that response can follow it.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import numpy as np

from .audio import wav_pcm
from .errors import MESSAGES_KO
from .protocol import FrameError, pack_frame, unpack_input_frame
from .sessions import Session, merge_goals
from .vad import WINDOW, WINDOW_MS
from .workers import WorkerError

if TYPE_CHECKING:
    from .app import Services

log = logging.getLogger("vr_gateway.realtime")

SPEECH_ON = 0.5
SPEECH_OFF = 0.35
ANNOUNCE_MS = 200  # voiced audio needed before speech.started / barge-in
MIN_SPEECH_MS = 250  # shorter utterances are discarded without an LLM call
PREROLL_SAMPLES = 3200  # 200 ms
FILLER_EXTENSION_MS = 400
WARN_MS = 40_000
MAX_UTTERANCE_MS = 45_000
ECHO_OVERLAP = 0.6
ECHO_LIMIT = 2
OUTPUT_CHUNK_MS = 100
PLAYBACK_GRACE_S = 2.0
CLIENT_STOP_WINDOW_S = 2.0  # speech this soon after the browser stopped AI audio still counts as a barge-in
COMMIT_WAIT_S = 0.5
FILLERS = ("um", "uh", "and", "but", "because", "so", "or", "the", "a", "to", "like", "i mean")
HISTORY_WINDOW = 12  # history entries the roleplay prompt keeps verbatim (vr_feedback.prompts: 6 turns)
MAX_LEARNER_FACTS = 8

COMMIT = object()
CANCEL = object()


class Transport(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...


def ends_with_question(text: str) -> bool:
    return text.rstrip().rstrip("\"'”’)]").endswith("?")


def ends_with_filler(text: str) -> bool:
    words = text.lower().strip().rstrip(".,!?;:-— ").split()
    if not words:
        return False
    return words[-1] in FILLERS or " ".join(words[-2:]) == "i mean"


@dataclass
class Utterance:
    turn_id: str | None
    manual: bool = False
    windows: int = 0
    voiced_windows: int = 0
    silence_windows: int = 0
    extended: bool = False
    announced: bool = False
    warned: bool = False
    in_speech: bool = True
    barge_in: bool = False
    echo_ref: str = ""
    last_partial: str = ""
    buffer: list[bytes] = field(default_factory=list)
    spans: list[list[int]] = field(default_factory=list)  # voiced [start_w, end_w)
    queue: asyncio.Queue | None = None
    task: asyncio.Task | None = None

    @property
    def voiced_ms(self) -> int:
        return self.voiced_windows * WINDOW_MS

    @property
    def length_ms(self) -> int:
        return self.windows * WINDOW_MS


@dataclass
class Segment:
    text: str
    status: str = "pending"  # pending | sent | played | interrupted | text_only
    audio_ms: int = 0


@dataclass
class Response:
    response_id: str
    epoch: int
    turn_id: str | None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    segments: dict[int, Segment] = field(default_factory=dict)
    cancelled: bool = False
    done: bool = False
    done_at: float = 0.0
    audio_ms: int = 0
    folded: bool = False
    failed: bool = False
    asked: bool = False  # a segment ended in "?": the rest of the reply is not generated or spoken
    tts_request: str | None = None
    tts_error_sent: bool = False
    task: asyncio.Task | None = None
    stopped_at: float = 0.0  # the client reported playback.stopped (its own barge-in trigger)
    stopped_text: str = ""

    def playback_finished(self) -> bool:
        if self.cancelled:
            return True
        if not self.done:
            return False
        if all(s.status in ("played", "interrupted", "text_only") for s in self.segments.values()):
            return True
        return time.monotonic() > self.done_at + self.audio_ms / 1000 + PLAYBACK_GRACE_S


class RealtimeEngine:
    def __init__(self, session: Session, services: Services, transport: Transport):
        self.s = session
        self.svc = services
        self.t = transport
        self.brain = services.brain
        self.vad = services.vad_model.stream()
        self.tts = services.tts.stream_session()
        self._send_lock = asyncio.Lock()
        self.event_seq = 0
        self.out_seq = 0
        self.state = "READY"
        self.input_state = "idle"
        self.output_state = "idle"
        self.started = False
        self.muted = False
        self.paused = False
        self.closed = False
        self.auto_barge_in = True
        self.speed = 1.0
        self.silence_ms = session.silence_ms
        self.utt: Utterance | None = None
        self.preroll: collections.deque[np.ndarray] = collections.deque()
        self.preroll_samples = 0
        self.turn_status: dict[str, str] = {}  # turn_id -> finalizing|final|discarded|merged
        self.finals: dict[str, str] = {}
        self.aliases: dict[str, str] = {}  # client turn id -> server turn id
        self.client_turn_id: str | None = None
        self.last_seq = -1
        self.pending_commit: tuple[str, int] | None = None
        self.response: Response | None = None
        self.responses: list[Response] = []
        self.playing: tuple[str, int] | None = None
        self.echo_count = 0
        self.carry_text = ""
        self.carry_turn: str | None = None
        self.tasks: set[asyncio.Task] = set()
        self.goals_task: asyncio.Task | None = None
        self.goals_due: Response | None = None  # latest reply whose turn still needs a goal check
        self.summary_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ sending

    @property
    def epoch(self) -> int:
        return self.s.epoch

    def _event(self, type_: str, **fields) -> str:
        self.event_seq += 1
        return json.dumps(
            {"type": type_, "event_id": uuid.uuid4().hex[:12], "session_id": self.s.session_id,
             "epoch": self.epoch, "event_seq": self.event_seq, **fields},
            ensure_ascii=False,
        )

    async def send(self, type_: str, resp: Response | None = None, **fields) -> bool:
        async with self._send_lock:
            return await self._send_locked(type_, resp, **fields)

    async def _send_locked(self, type_: str, resp: Response | None = None, **fields) -> bool:
        if self.closed:
            return False
        if resp is not None:
            if resp.cancelled or resp.epoch != self.epoch:
                return False
            fields.setdefault("response_id", resp.response_id)
        try:
            await self.t.send_text(self._event(type_, **fields))
        except Exception:
            self.closed = True
            return False
        return True

    async def send_error(self, code: str, recoverable: bool = True, **fields) -> None:
        await self.send("error", code=code, message_ko=MESSAGES_KO.get(code, ""), recoverable=recoverable, **fields)

    async def _send_audio(self, resp: Response, segment_id: int, sample_rate: int, pcm: bytes) -> bool:
        async with self._send_lock:
            if self.closed or resp.cancelled or resp.epoch != self.epoch:
                return False
            self.out_seq += 1
            header = {"v": 1, "kind": "output_audio", "session_id": self.s.session_id,
                      "response_id": resp.response_id, "epoch": resp.epoch, "seq": self.out_seq,
                      "sample_rate": sample_rate, "sample_count": len(pcm) // 2, "segment_id": segment_id}
            try:
                await self.t.send_bytes(pack_frame(header, pcm))
            except Exception:
                self.closed = True
                return False
            return True

    async def _set_state(self, state: str | None = None, input_state: str | None = None,
                         output_state: str | None = None) -> None:
        changed = False
        for name, value in (("state", state), ("input_state", input_state), ("output_state", output_state)):
            if value is not None and getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            await self.send("session.state", state=self.state, input_state=self.input_state,
                            output_state=self.output_state)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            log.error("engine_task_failed session=%s error=%s", self.s.session_id, type(exc).__name__)

    # ------------------------------------------------------------------ inbound

    async def on_text(self, raw: str) -> None:
        try:
            ev = json.loads(raw)
        except ValueError:
            await self.send_error("EVENT_INVALID")
            return
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str) or ev.get("session_id") != self.s.session_id:
            await self.send_error("EVENT_INVALID")
            return
        handler = self.HANDLERS.get(ev["type"])
        if handler is None:
            await self.send_error("EVENT_INVALID", event_type=ev["type"][:40])
            return
        if self.s.state == "ended" and ev["type"] != "session.end":
            await self.send_error("INVALID_STATE", recoverable=False)
            return
        try:
            await handler(self, ev)
        except (KeyError, TypeError, ValueError):
            await self.send_error("EVENT_INVALID", event_type=ev["type"])

    async def on_binary(self, data: bytes) -> None:
        try:
            header, payload = unpack_input_frame(data, self.s.session_id)
        except FrameError as exc:
            log.info("frame_rejected session=%s reason=%s bytes=%d", self.s.session_id, exc, len(data))
            await self.send_error("FRAME_INVALID")
            return
        seq = header["seq"]
        if seq <= self.last_seq:
            return  # duplicate / replayed frame
        self.last_seq = seq
        if header.get("turn_id"):
            self.client_turn_id = header["turn_id"]
        if not self.started or self.muted or self.paused or self.s.state == "ended":
            return
        pcm = np.frombuffer(payload, dtype="<i2")
        for prob, window in self.vad.process(pcm):
            await self._on_window(prob, window)
        if self.pending_commit and seq >= self.pending_commit[1]:
            turn_id, _ = self.pending_commit
            self.pending_commit = None
            await self._commit(turn_id)

    # ------------------------------------------------------------------ turn taking

    def _push_preroll(self, window: np.ndarray) -> None:
        self.preroll.append(window)
        self.preroll_samples += window.shape[0]
        while self.preroll and self.preroll_samples - self.preroll[0].shape[0] >= PREROLL_SAMPLES:
            self.preroll_samples -= self.preroll.popleft().shape[0]

    def _new_turn_id(self, preferred: str | None) -> str:
        if preferred and preferred not in self.turn_status and (self.utt is None or self.utt.turn_id != preferred):
            return preferred
        return f"turn_{self.s.session_id[-6:]}_{self.s.turn_counter + len(self.turn_status) + 1}_{uuid.uuid4().hex[:4]}"

    def _open_utterance(self, manual: bool, turn_id: str | None = None) -> Utterance:
        utt = Utterance(turn_id=turn_id, manual=manual)
        utt.buffer = [w.tobytes() for w in self.preroll]
        self.preroll.clear()
        self.preroll_samples = 0
        self.utt = utt
        return utt

    async def _on_window(self, prob: float, window: np.ndarray) -> None:
        utt = self.utt
        if utt is None:
            if prob >= SPEECH_ON:
                utt = self._open_utterance(manual=False)
            else:
                self._push_preroll(window)
                return
        speech = prob >= (SPEECH_OFF if utt.in_speech else SPEECH_ON)
        idx = utt.windows
        utt.windows += 1
        self._feed(utt, window.tobytes())
        if speech:
            if not utt.in_speech or not utt.spans:
                utt.spans.append([idx, idx + 1])
            else:
                utt.spans[-1][1] = idx + 1
            utt.in_speech = True
            utt.voiced_windows += 1
            utt.silence_windows = 0
            utt.extended = False
        else:
            utt.in_speech = False
            utt.silence_windows += 1

        if not utt.announced and utt.voiced_ms >= ANNOUNCE_MS:
            if not await self._announce(utt):
                return
        if not utt.warned and utt.length_ms >= WARN_MS and utt.announced:
            utt.warned = True
            await self.send("turn.warning", turn_id=utt.turn_id, code="UTTERANCE_40S")
        if utt.length_ms >= MAX_UTTERANCE_MS:
            await self._end_utterance(utt, "max_length")
            return
        if utt.manual:
            return
        silence = utt.silence_windows * WINDOW_MS
        if not utt.announced:
            # A blip that never reached the announce threshold.
            if silence >= self.silence_ms:
                self.utt = None
            return
        if silence >= self.silence_ms and not utt.extended and ends_with_filler(utt.last_partial):
            utt.extended = True
        limit = self.silence_ms + (FILLER_EXTENSION_MS if utt.extended else 0)
        if silence >= limit:
            await self._end_utterance(utt, "silence")

    def _feed(self, utt: Utterance, chunk: bytes) -> None:
        if utt.queue is not None:
            utt.queue.put_nowait(chunk)
        else:
            utt.buffer.append(chunk)

    async def _announce(self, utt: Utterance) -> bool:
        """Utterance passed the voiced threshold: barge-in if needed, open the ASR stream."""
        resp = self.response
        if resp is not None and not resp.playback_finished():
            if not self.auto_barge_in and not utt.manual:
                self.utt = None  # push-to-talk mode: ignore speech while the AI talks
                return False
            utt.barge_in = True
            utt.echo_ref = self._playing_text(resp)
            await self._cancel_response(resp, "barge_in")
        elif resp is not None and time.monotonic() - resp.stopped_at < CLIENT_STOP_WINDOW_S:
            # The browser's level detector stopped the AI audio before our VAD announced the speech (usual
            # when the reply was already fully generated): nothing left to cancel, but it is still a barge-in,
            # so the echo check below must see what was playing.
            utt.barge_in = True
            utt.echo_ref = resp.stopped_text
        utt.turn_id = utt.turn_id or self._new_turn_id(self.client_turn_id)
        utt.announced = True
        utt.queue = asyncio.Queue()
        for chunk in utt.buffer:
            utt.queue.put_nowait(chunk)
        utt.buffer = []
        utt.task = self._spawn(self._asr_pipeline(utt))
        await self.send("speech.started", turn_id=utt.turn_id)
        await self._set_state(state="LISTENING", input_state="speaking")
        return True

    async def _end_utterance(self, utt: Utterance, reason: str) -> None:
        if self.utt is utt:
            self.utt = None
        tid = utt.turn_id
        if not utt.announced or utt.voiced_ms < MIN_SPEECH_MS:
            if utt.queue is not None:
                utt.queue.put_nowait(CANCEL)
            if tid:
                self.turn_status[tid] = "discarded"
                await self.send("speech.ended", turn_id=tid, discarded=True)
            await self._set_state(input_state="listening")
            log.info("turn_discarded session=%s voiced_ms=%d", self.s.session_id, utt.voiced_ms)
            await self._flush_carry()
            return
        self.turn_status[tid] = "finalizing"
        utt.queue.put_nowait(COMMIT)
        await self.send("speech.ended", turn_id=tid, reason=reason)
        busy = self.response is not None and not self.response.playback_finished()
        await self._set_state(state="RESPONDING" if busy else "FINALIZING", input_state="listening")
        log.info("turn_ended session=%s reason=%s length_ms=%d voiced_ms=%d", self.s.session_id, reason,
                 utt.length_ms, utt.voiced_ms)

    async def _asr_pipeline(self, utt: Utterance) -> None:
        stream = None

        def on_partial(text: str) -> None:
            utt.last_partial = text
            if self.turn_status.get(utt.turn_id) is None:
                self._spawn(self.send("asr.partial", turn_id=utt.turn_id, text=text))

        started = time.perf_counter()
        try:
            stream = await self.svc.asr.open_stream(on_partial)
            while True:
                item = await utt.queue.get()
                if item is CANCEL:
                    await stream.cancel()
                    return
                if item is COMMIT:
                    final = await stream.commit()
                    break
                await stream.send_audio(item)
        except WorkerError:
            if stream is not None:
                await stream.close()
            if self.turn_status.get(utt.turn_id) == "finalizing":
                self.turn_status[utt.turn_id] = "discarded"
                await self.send_error("ASR_FAILED", turn_id=utt.turn_id)
                await self._set_state(state="LISTENING")
            else:
                # Stream died mid-utterance: drain the queue so the turn still ends cleanly.
                self.turn_status[utt.turn_id] = "discarded"
                if self.utt is utt:
                    self.utt = None
                await self.send_error("ASR_FAILED", turn_id=utt.turn_id)
            return
        except asyncio.CancelledError:
            if stream is not None:
                await stream.cancel()
            raise
        log.info("asr_final session=%s audio_ms=%s elapsed_ms=%d", self.s.session_id, final.get("audio_ms"),
                 int((time.perf_counter() - started) * 1000))
        await self._on_final(utt, (final.get("text") or "").strip())

    async def _on_final(self, utt: Utterance, text: str) -> None:
        tid = utt.turn_id
        self.turn_status[tid] = "final"
        self.finals[tid] = text
        await self.send("asr.final", turn_id=tid, text=text, transcript_revision=1)
        if not text:
            await self._idle_state()
            return
        if utt.barge_in and utt.echo_ref and self.brain.echo_overlap(text, utt.echo_ref) >= ECHO_OVERLAP:
            self.turn_status[tid] = "discarded"
            self.echo_count += 1
            suggest = self.echo_count >= ECHO_LIMIT
            if suggest:
                self.auto_barge_in = False
            await self.send("echo.suspected", turn_id=tid, count=self.echo_count, auto_barge_in=self.auto_barge_in,
                            push_to_talk_suggested=suggest)
            log.info("echo_suspected session=%s count=%d", self.s.session_id, self.echo_count)
            await self._idle_state()
            return
        voiced = [(a * WINDOW / 16000, b * WINDOW / 16000) for a, b in utt.spans]
        metrics = self.brain.compute_metrics(voiced, text)
        self._fold_all()  # the AI line this turn answers is recorded before it (playback end unconfirmed)
        self.s.record_turn(self.svc, {
            "turn_id": tid, "turn_index": self.s.next_turn_index(), "role": "user", "text": text,
            "transcript_revision": 1, "metrics": metrics, "created_at": time.time(),
        })
        if self.utt is not None and self.utt.announced:
            # The learner kept talking after the pause: answer both parts together.
            self.turn_status[tid] = "merged"
            self.carry_text = f"{self.carry_text} {text}".strip()
            self.carry_turn = tid
            return
        user_text = f"{self.carry_text} {text}".strip()
        self.carry_text, self.carry_turn = "", None
        await self._start_response(tid, user_text)

    async def _flush_carry(self) -> None:
        """A merged turn is waiting but the follow-up produced no usable text: answer what we have."""
        if self.carry_text and (self.utt is None or not self.utt.announced):
            text, turn_id = self.carry_text, self.carry_turn
            self.carry_text, self.carry_turn = "", None
            await self._start_response(turn_id, text)

    async def _idle_state(self) -> None:
        await self._flush_carry()
        busy = self.response is not None and not self.response.playback_finished()
        if self.utt is None or not self.utt.announced:
            await self._set_state(state="RESPONDING" if busy else "LISTENING")

    # ------------------------------------------------------------------ responses

    def _fold(self, resp: Response) -> None:
        """Move what the learner actually heard (or saw, if TTS failed) into history once. Called as soon as
        the reply's playback has ended (completed, stopped or cancelled), so goal checks, the rolling summary
        and hints see the line without waiting for the next reply, and the transcript keeps the spoken order."""
        if resp.folded:
            return
        resp.folded = True
        ordered = [resp.segments[k] for k in sorted(resp.segments)]
        heard = " ".join(seg.text for seg in ordered if seg.status in ("played", "text_only"))
        if heard:
            self.s.history.append({"role": "assistant", "text": heard})
            self._maybe_summarize()
        if ordered:
            status = "completed" if all(s.status in ("played", "text_only") for s in ordered) else "interrupted"
            self.s.record_turn(self.svc, {
                "turn_id": resp.response_id, "turn_index": self.s.next_turn_index(), "role": "assistant",
                "text": heard, "playback_status": status, "created_at": time.time(),
                "spoken_segments": [{"segment_id": k, "text": resp.segments[k].text, "status": resp.segments[k].status}
                                    for k in sorted(resp.segments)],
            })

    def _fold_all(self) -> None:
        for r in self.responses:
            if r.cancelled or r.playback_finished():
                self._fold(r)

    def _fold_if_finished(self, resp: Response) -> None:
        # A failed reply stays out until the next turn: response.retry expects the learner's line last in history.
        if not resp.failed and resp.playback_finished():
            self._fold(resp)

    async def _start_response(self, turn_id: str, user_text: str, retry: bool = False) -> None:
        self._fold_all()
        history = list(self.s.history)
        if retry and history and history[-1]["role"] == "user":
            history.pop()
        messages = self.brain.build_roleplay_messages(
            self.s.scenario, self.s.difficulty, self.s.goals, history, user_text,
            summary=self.s.context_summary, learner_facts=list(self.s.learner_facts.values()),
        )
        if not retry:
            self.s.history.append({"role": "user", "text": user_text})
        resp = Response(response_id="r_" + uuid.uuid4().hex[:12], epoch=self.epoch, turn_id=turn_id)
        self.response = resp
        self.responses.append(resp)
        await self._set_state(state="RESPONDING", output_state="generating")
        await self.send("response.started", resp, epoch=resp.epoch, turn_id=turn_id)
        resp.task = self._spawn(self._run_response(resp, messages))

    async def _run_response(self, resp: Response, messages: list[dict]) -> None:
        started = time.perf_counter()
        queue: asyncio.Queue = asyncio.Queue()
        producer = asyncio.create_task(self._produce(resp, messages, queue))
        seg_id = 0
        first_audio_ms = None
        try:
            while True:
                text = await queue.get()
                if text is None:
                    break
                if resp.cancelled:
                    continue
                sent = await self._speak(resp, seg_id, text)
                if sent and first_audio_ms is None:
                    first_audio_ms = int((time.perf_counter() - started) * 1000)
                seg_id += 1
            await producer
            if not resp.segments and not resp.cancelled:
                raise WorkerError("LLM_FAILED")
        except Exception as exc:
            producer.cancel()
            if not resp.cancelled:
                log.error("response_failed session=%s error=%s", self.s.session_id, type(exc).__name__)
                await self.send_error("LLM_FAILED", response_id=resp.response_id, turn_id=resp.turn_id)
                resp.failed = True
                resp.done = True
                resp.done_at = time.monotonic()
                await self._set_state(state="LISTENING", output_state="idle")
            return
        except asyncio.CancelledError:
            producer.cancel()
            raise
        resp.done = True
        resp.done_at = time.monotonic()
        if resp.cancelled:
            return
        await self.send("response.done", resp)
        self._fold_if_finished(resp)  # e.g. every segment text_only after a TTS failure
        await self._set_state(output_state="playing" if not resp.playback_finished() else "idle")
        log.info("response_done session=%s segments=%d audio_ms=%d first_audio_ms=%s total_ms=%d question_stop=%s",
                 self.s.session_id, len(resp.segments), resp.audio_ms, first_audio_ms,
                 int((time.perf_counter() - started) * 1000), resp.asked)
        await self._maybe_listening(resp)
        self._after_response(resp)

    async def _produce(self, resp: Response, messages: list[dict], queue: asyncio.Queue) -> None:
        """Stream the reply into segments. At most one question per reply (the system prompt asks for it):
        once a segment ends in "?", the LLM stream is closed and nothing after it is spoken."""
        segmenter = self.brain.segmenter_factory()
        stream = self.svc.llm.stream_chat(messages, max_tokens=128, slot_id=0, cancel=resp.cancel_event)
        try:
            async with aclosing(stream):
                async for delta in stream:
                    if resp.cancelled:
                        break
                    for seg in segmenter.feed(delta):
                        queue.put_nowait(seg)
                        if ends_with_question(seg):
                            resp.asked = True
                            break
                    if resp.asked:
                        break  # closing the stream makes llama-server stop generating
            if not resp.cancelled and not resp.asked:
                for seg in segmenter.flush():
                    queue.put_nowait(seg)
                    if ends_with_question(seg):
                        break
        finally:
            queue.put_nowait(None)

    async def _speak(self, resp: Response, seg_id: int, text: str) -> bool:
        """Show the segment text and stream its audio. Returns True if any audio frame was sent."""
        seg = Segment(text=text)
        resp.segments[seg_id] = seg
        if not await self.send("response.text", resp, segment_id=seg_id, text=text):
            return False
        spoken = self.brain.normalize_for_tts(text)
        if not spoken.strip():
            seg.status = "text_only"
            return False
        seg.status = "sent"
        request_id = f"{resp.response_id}-{seg_id}"
        resp.tts_request = request_id
        sent_any = False
        rate = 0
        pending = b""
        try:
            async with asyncio.timeout(60):
                async with aclosing(self.tts.synthesize(request_id, self.s.voice_id, spoken, self.speed)) as gen:
                    async for kind, value in gen:
                        if kind == "start":
                            rate = value
                        elif kind == "audio":
                            if resp.cancelled:
                                continue  # drain until the worker confirms `cancelled`
                            pending += value
                            chunk = rate * OUTPUT_CHUNK_MS // 1000 * 2
                            while len(pending) >= chunk:
                                sent_any |= await self._send_audio(resp, seg_id, rate, pending[:chunk])
                                seg.audio_ms += OUTPUT_CHUNK_MS
                                pending = pending[chunk:]
                        else:
                            break
            if pending and not resp.cancelled:
                sent_any |= await self._send_audio(resp, seg_id, rate, pending)
                seg.audio_ms += len(pending) * 1000 // (2 * rate)
        except (WorkerError, TimeoutError):
            if resp.cancelled:
                return sent_any
            seg.status = "text_only"
            if not resp.tts_error_sent:
                resp.tts_error_sent = True
                await self.send_error("TTS_FAILED", response_id=resp.response_id, segment_id=seg_id)
            return sent_any
        resp.audio_ms += seg.audio_ms
        if not sent_any and not resp.cancelled:
            seg.status = "text_only"
        return sent_any

    async def _cancel_response(self, resp: Response, reason: str) -> None:
        async with self._send_lock:
            if resp.cancelled or resp.playback_finished():
                return
            resp.cancelled = True
            self.s.epoch += 1
            await self._send_locked("response.cancelled", None, response_id=resp.response_id, reason=reason)
        resp.cancel_event.set()
        for seg in resp.segments.values():
            if seg.status in ("pending", "sent"):
                seg.status = "interrupted"
        self._fold(resp)
        if resp.tts_request:
            self._spawn(self.tts.cancel(resp.tts_request))
        await self._set_state(state="INTERRUPTING", output_state="idle")
        await self._set_state(state="LISTENING")
        log.info("response_cancelled session=%s reason=%s epoch=%d", self.s.session_id, reason, self.epoch)

    async def _maybe_listening(self, resp: Response) -> None:
        if resp is self.response and resp.playback_finished() and self.state == "RESPONDING":
            await self._set_state(state="LISTENING", output_state="idle")

    def _playing_text(self, resp: Response) -> str:
        if self.playing and self.playing[0] == resp.response_id and self.playing[1] in resp.segments:
            return resp.segments[self.playing[1]].text
        sent = [s.text for s in resp.segments.values() if s.status in ("sent", "played")]
        return sent[-1] if sent else ""

    def _after_response(self, resp: Response) -> None:
        """Background LLM work (llama-server slot 1) starts once the reply is generated, while it plays:
        goal tracking (it reads the learner's turns only) and optional per-turn feedback. The rolling summary
        starts when a reply enters history (_fold). None of it blocks a reply."""
        if resp.turn_id is None or resp.turn_id == "opening":
            return
        if any(g["status"] != "done" for g in self.s.goals):
            self.goals_due = resp
            if self.goals_task is None or self.goals_task.done():
                self.goals_task = self._spawn(self._update_goals())
        if self.s.feedback_policy == "per_turn":
            self._spawn(self._turn_feedback(resp.turn_id))

    def _newer_turn_started(self, resp: Response) -> bool:
        return self.response is not resp or (self.utt is not None and self.utt.announced)

    async def _update_goals(self) -> None:
        """Evaluate only the goals still pending, one evaluation at a time. A check is skipped when the
        learner has already started a newer turn: that turn's reply schedules one that includes it."""
        while self.goals_due is not None:
            resp, self.goals_due = self.goals_due, None
            pending = [g for g in self.s.scenario.get("goals", [])
                       if any(x["goal_id"] == g["goal_id"] and x["status"] != "done" for x in self.s.goals)]
            if not pending or self._newer_turn_started(resp):
                log.info("goals_skipped session=%s pending=%d", self.s.session_id, len(pending))
                continue
            started = time.perf_counter()
            goals = await self.brain.evaluate_goals(self.svc.llm_bg, {**self.s.scenario, "goals": pending},
                                                    self.s.user_turns())
            merged = merge_goals(self.s.goals, goals)
            changed = merged != self.s.goals
            self.s.goals = merged
            await self.send("goal.update", goals=[
                {k: g[k] for k in ("goal_id", "status", "evidence_turn_id") if g.get(k)} for g in merged
            ], changed=changed)
            log.info("goals_evaluated session=%s pending=%d ms=%d", self.s.session_id, len(pending),
                     int((time.perf_counter() - started) * 1000))

    def _maybe_summarize(self) -> None:
        """PRD §11: once history is longer than the prompt window, fold the older entries into a bounded
        summary. The reply always uses whatever summary exists; this never waits on the LLM. Runs when a reply
        enters history, so the cutoff matches the verbatim window of the next prompt. Entries older than the
        window with no learner line (only the opening, which the prompt prefix already has) are not summarized."""
        if self.closed:
            return
        cutoff = len(self.s.history) - HISTORY_WINDOW
        older = self.s.history[self.s.summarized_upto:max(cutoff, 0)]
        if (any(h["role"] == "user" for h in older)
                and (self.summary_task is None or self.summary_task.done())):
            self.summary_task = self._spawn(self._summarize(cutoff))

    async def _summarize(self, cutoff: int) -> None:
        started = time.perf_counter()
        older = self.s.history[self.s.summarized_upto:cutoff]
        try:
            result = await self.brain.update_summary(self.svc.llm_bg, self.s.scenario, self.s.context_summary, older)
        except Exception as exc:  # keep the previous summary; the next reply retries
            log.warning("summary_failed session=%s error=%s", self.s.session_id, type(exc).__name__)
            return
        self.s.context_summary = result["summary"]
        facts = self.s.learner_facts
        for fact in result["facts"]:
            facts.pop(fact["name"], None)  # re-insert: newest statement wins and moves to the end
            facts[fact["name"]] = fact
        while len(facts) > MAX_LEARNER_FACTS:
            facts.pop(next(iter(facts)))
        self.s.summarized_upto = cutoff
        log.info("summary_updated session=%s entries=%d facts=%d ms=%d", self.s.session_id, len(older), len(facts),
                 int((time.perf_counter() - started) * 1000))

    async def _turn_feedback(self, turn_id: str) -> None:
        text = self.finals.get(turn_id, "")
        result = await self.brain.generate_feedback(
            self.svc.llm_bg, transcript=text, transcript_revision=1, source={"source_turn_id": turn_id},
            context={"exercise_type": "roleplay", "scenario_title_en": self.s.scenario.get("title_en")},
            max_items=3, model_revision=self.svc.config.llm_model_revision,
        )
        await self.send("feedback.ready", turn_id=turn_id, transcript_revision=1, **result)

    # ------------------------------------------------------------------ opening line

    async def _play_opening(self) -> None:
        line = self.s.scenario["opening_line"]
        resp = Response(response_id="r_open_" + uuid.uuid4().hex[:8], epoch=self.epoch, turn_id="opening")
        self.response = resp
        self.responses.append(resp)
        await self._set_state(state="RESPONDING", output_state="generating")
        await self.send("response.started", resp, epoch=resp.epoch, turn_id=None, opening=True)
        seg = Segment(text=line["en"])
        resp.segments[0] = seg
        await self.send("response.text", resp, segment_id=0, text=line["en"], text_ko=line.get("ko"))
        wav = None
        try:
            wav = await self.svc.tts_cache.ensure(self.s.voice_id, line["text_id"], self.speed)
        except Exception as exc:
            log.error("opening_audio_failed session=%s error=%s", self.s.session_id, type(exc).__name__)
        if wav is not None:
            pcm, rate = wav_pcm(wav)
            chunk = rate * OUTPUT_CHUNK_MS // 1000 * 2
            seg.status = "sent"
            for i in range(0, len(pcm), chunk):
                if not await self._send_audio(resp, 0, rate, pcm[i:i + chunk]):
                    break
            seg.audio_ms = len(pcm) * 1000 // (2 * rate)
            resp.audio_ms = seg.audio_ms
        else:
            seg.status = "text_only"
            await self.send_error("TTS_FAILED", response_id=resp.response_id, segment_id=0)
        resp.done = True
        resp.done_at = time.monotonic()
        await self.send("response.done", resp)
        await self._set_state(output_state="playing" if not resp.playback_finished() else "idle")
        await self._maybe_listening(resp)

    async def _warm_llm(self) -> None:
        started = time.perf_counter()
        try:
            await self.svc.llm.warm(self.brain.build_opening_warmup(self.s.scenario, self.s.difficulty), slot_id=0)
            log.info("llm_warm session=%s ms=%d", self.s.session_id, int((time.perf_counter() - started) * 1000))
        except Exception as exc:
            log.warning("llm_warm_failed session=%s error=%s", self.s.session_id, type(exc).__name__)

    # ------------------------------------------------------------------ event handlers

    async def h_session_start(self, ev: dict) -> None:
        if self.started:
            await self.send("session.state", state=self.state, input_state=self.input_state,
                            output_state=self.output_state)
            return
        self.started = True
        self.s.state = "active"
        self._spawn(self._warm_llm())
        await self._set_state(state="LISTENING", input_state="listening", output_state="idle")
        if not self.s.turns and not self.s.history:
            self._spawn(self._play_opening())

    async def h_input_start(self, ev: dict) -> None:
        turn_id = ev.get("turn_id")
        if turn_id is not None and (not isinstance(turn_id, str) or len(turn_id) > 64):
            raise ValueError("turn_id")
        if self.utt is not None and self.utt.announced:
            return
        if self.response is not None and not self.response.playback_finished():
            await self._cancel_response(self.response, "input_start")
        utt = self.utt or self._open_utterance(manual=True)
        utt.manual = True
        utt.turn_id = self._new_turn_id(turn_id)
        await self._announce(utt)

    async def h_input_commit(self, ev: dict) -> None:
        turn_id = ev["turn_id"]
        last_seq = int(ev.get("last_seq", -1))
        if last_seq > self.last_seq:
            self.pending_commit = (turn_id, last_seq)
            self._spawn(self._commit_after_wait(turn_id))
            return
        await self._commit(turn_id)

    async def _commit_after_wait(self, turn_id: str) -> None:
        await asyncio.sleep(COMMIT_WAIT_S)
        if self.pending_commit and self.pending_commit[0] == turn_id:
            self.pending_commit = None
            await self._commit(turn_id)

    async def _commit(self, turn_id: str) -> None:
        turn_id = self.aliases.get(turn_id, turn_id)
        if self.turn_status.get(turn_id) is not None:
            if turn_id in self.finals:  # idempotent: repeat the same result
                await self.send("asr.final", turn_id=turn_id, text=self.finals[turn_id], transcript_revision=1)
            return
        utt = self.utt
        if utt is not None:
            # "말하기 완료" finishes whatever is being said now, whichever id the client used.
            if utt.turn_id is None:
                utt.turn_id = self._new_turn_id(turn_id)
            if utt.turn_id != turn_id:
                self.aliases[turn_id] = utt.turn_id
            await self._end_utterance(utt, "commit")
            return
        self.turn_status[turn_id] = "discarded"
        await self.send("speech.ended", turn_id=turn_id, discarded=True)

    async def h_response_cancel(self, ev: dict) -> None:
        resp = self.response
        if resp is not None and resp.response_id == ev.get("response_id"):
            await self._cancel_response(resp, "client")

    async def h_response_retry(self, ev: dict) -> None:
        """Retry the reply to the last user turn after LLM_FAILED (PRD §17)."""
        resp = self.response
        last = self.s.history[-1] if self.s.history else None
        if resp is None or not resp.failed or last is None or last["role"] != "user":
            await self.send_error("INVALID_STATE")
            return
        await self._start_response(resp.turn_id, last["text"], retry=True)

    async def h_session_pause(self, ev: dict) -> None:
        self.paused = True
        await self._drop_activity("pause")
        await self._set_state(state="PAUSED", input_state="paused", output_state="idle")

    async def h_session_resume(self, ev: dict) -> None:
        if not self.paused:
            return
        self.paused = False
        self.vad.reset()
        await self._set_state(state="LISTENING", input_state="muted" if self.muted else "listening")

    async def h_session_end(self, ev: dict) -> None:
        self.svc.sessions.end_in_background(self.s)

    async def h_playback_started(self, ev: dict) -> None:
        resp = self._find_response(ev.get("response_id"))
        if resp is not None:
            self.playing = (resp.response_id, int(ev.get("segment_id", 0)))
            await self._set_state(output_state="playing")

    async def h_playback_completed(self, ev: dict) -> None:
        await self._playback_end(ev, "played")

    async def h_playback_stopped(self, ev: dict) -> None:
        await self._playback_end(ev, "interrupted")

    async def _playback_end(self, ev: dict, status: str) -> None:
        resp = self._find_response(ev.get("response_id"))
        if resp is None:
            return
        seg_id = int(ev.get("segment_id", -1))
        seg = resp.segments.get(seg_id)
        if seg is not None and seg.status in ("sent", "pending", "text_only"):
            seg.status = status
            if status == "interrupted":
                resp.stopped_at = time.monotonic()
                if self.playing == (resp.response_id, seg_id) or not resp.stopped_text:
                    resp.stopped_text = seg.text  # the segment that was audible
        if resp.playback_finished():
            self._fold_if_finished(resp)
            if resp is self.response and self.state == "RESPONDING":
                await self._set_state(state="LISTENING", output_state="idle")
            elif resp is self.response:
                await self._set_state(output_state="idle")

    def _find_response(self, response_id) -> Response | None:
        for r in reversed(self.responses):
            if r.response_id == response_id:
                return r
        return None

    async def h_hint_request(self, ev: dict) -> None:
        level = int(ev["level"])
        if level not in (1, 2, 3):
            raise ValueError("level")
        request_id = ev.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 64):
            raise ValueError("request_id")
        # The line on the learner's screen is the newest response; history only gains it once its playback ends.
        last_ai, about = "", None
        resp = next((r for r in reversed(self.responses) if r.segments), None)
        if resp is not None:
            ordered = [resp.segments[k] for k in sorted(resp.segments)]
            shown = [s for s in ordered if s.status in ("played", "interrupted", "text_only")] if resp.cancelled else ordered
            last_ai = " ".join(s.text for s in shown).strip()
            about = resp.response_id if last_ai else None
        if not last_ai:
            last_ai = next((h["text"] for h in reversed(self.s.history) if h["role"] == "assistant"), "")
        self._spawn(self._hint(level, last_ai, about, request_id))

    async def _hint(self, level: int, last_ai: str, about: str | None, request_id: str | None) -> None:
        hint = await self.brain.build_hint(self.s.scenario, self.s.difficulty, level, self.s.goals, last_ai,
                                           llm=self.svc.llm_bg if level == 1 else None)
        if about is not None:
            hint["response_id"] = about  # which AI line the hint (and its level-1 translation) belongs to
        if request_id is not None:
            hint["request_id"] = request_id  # replies can overtake each other (level 1 may wait on the LLM)
        await self.send("hint", **hint)

    async def h_settings_update(self, ev: dict) -> None:
        if ev.get("silence_ms") is not None:
            self.silence_ms = max(700, min(1400, int(ev["silence_ms"])))
        if ev.get("auto_barge_in") is not None:
            self.auto_barge_in = bool(ev["auto_barge_in"])
        if ev.get("slow") is not None:
            self.speed = 0.85 if ev["slow"] else 1.0
        await self.send("settings.applied", silence_ms=self.silence_ms, auto_barge_in=self.auto_barge_in,
                        slow=self.speed < 1.0)

    async def h_mic_state(self, ev: dict) -> None:
        self.muted = bool(ev["muted"])
        if self.muted and self.utt is not None:
            await self._discard_utterance()
        if not self.paused:
            await self._set_state(input_state="muted" if self.muted else "listening")

    HANDLERS = {
        "session.start": h_session_start,
        "input.start": h_input_start,
        "input.commit": h_input_commit,
        "response.cancel": h_response_cancel,
        "response.retry": h_response_retry,
        "session.pause": h_session_pause,
        "session.resume": h_session_resume,
        "session.end": h_session_end,
        "playback.started": h_playback_started,
        "playback.completed": h_playback_completed,
        "playback.stopped": h_playback_stopped,
        "hint.request": h_hint_request,
        "settings.update": h_settings_update,
        "mic.state": h_mic_state,
    }

    # ------------------------------------------------------------------ teardown

    async def _discard_utterance(self) -> None:
        utt, self.utt = self.utt, None
        if utt is None:
            return
        if utt.queue is not None:
            utt.queue.put_nowait(CANCEL)
        if utt.turn_id and utt.announced:
            self.turn_status[utt.turn_id] = "discarded"
            await self.send("speech.ended", turn_id=utt.turn_id, discarded=True)

    async def _drop_activity(self, reason: str) -> None:
        await self._discard_utterance()
        if self.response is not None and not self.response.playback_finished():
            await self._cancel_response(self.response, reason)
        self.vad.reset()
        self.preroll.clear()
        self.preroll_samples = 0

    async def shutdown(self, reason: str) -> None:
        """Stop mic processing, cancel generation and worker streams, release audio buffers."""
        if self.closed and not self.tasks:
            return
        if self.response is not None and not self.response.playback_finished():
            await self._cancel_response(self.response, reason)
        if reason == "session_end":
            await self._set_state(state="CLOSED", input_state="closed", output_state="idle")
        self.closed = True
        if self.utt is not None and self.utt.queue is not None:
            self.utt.queue.put_nowait(CANCEL)
        self.utt = None
        self._fold_all()
        for r in self.responses:
            if not r.folded:
                r.cancelled = True
                r.cancel_event.set()
                self._fold(r)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.tts.close()
        self.preroll.clear()
        if self.s.engine is self:
            self.s.engine = None
            self.s.last_seen = time.monotonic()
        log.info("engine_closed session=%s reason=%s", self.s.session_id, reason)
