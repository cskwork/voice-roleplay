import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .audio import is_silent, pcm16_to_float, quietest_cut
from .config import MAX_STREAM_SAMPLES, SAMPLE_RATE
from .engine import AsrEngine

log = logging.getLogger("asr.stream")


class StreamTooLong(Exception):
    pass


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


class StreamSession:
    """One utterance: buffers PCM16, emits bounded re-decode partials, then one full-decode final (PROTOCOL §3)."""

    def __init__(
        self,
        engine: AsrEngine,
        send: Callable[[dict], Awaitable[None]],
        *,
        context: str = "",
        interval_ms: int = 700,
        min_new_ms: int = 300,
        window_s: float = 12.0,
    ):
        self._engine = engine
        self._send = send
        self._context = context
        self._interval = interval_ms / 1000
        self._min_new = min_new_ms * SAMPLE_RATE // 1000
        self._window = int(window_s * SAMPLE_RATE)
        self._buf = bytearray()
        self._partial_upto = 0  # samples covered by the latest partial decode
        self._last_partial_at = float("-inf")
        self._partial_task: asyncio.Task | None = None
        self._timer: asyncio.TimerHandle | None = None
        # Long utterances: audio before _window_start is frozen into _stable_text.
        self._window_start = 0
        self._stable_text = ""
        self.done = False
        self.partial_decodes = 0

    @property
    def samples(self) -> int:
        return len(self._buf) // 2

    def add_audio(self, data: bytes) -> None:
        if self.done:
            return
        if (len(self._buf) + len(data)) // 2 > MAX_STREAM_SAMPLES:
            raise StreamTooLong()
        self._buf += data
        self._maybe_partial()

    async def commit(self) -> None:
        """Run the final full decode and send it. Partials still waiting for the model are dropped."""
        if self.done:
            return
        self.done = True
        self._stop_partials()
        started = time.monotonic()
        audio = pcm16_to_float(self._buf[: self.samples * 2])
        text = await self._engine.transcribe(audio, self._context, final=True)
        audio_ms = len(audio) * 1000 // SAMPLE_RATE
        elapsed_ms = round((time.monotonic() - started) * 1000)
        self._buf = bytearray()
        log.info("stream.final audio_ms=%d elapsed_ms=%d partials=%d", audio_ms, elapsed_ms, self.partial_decodes)
        await self._send({"type": "final", "text": text, "audio_ms": audio_ms, "elapsed_ms": elapsed_ms})

    def close(self) -> None:
        """Cancel/disconnect: drop pending work and free the buffer now."""
        self.done = True
        self._stop_partials()
        self._buf = bytearray()

    def _stop_partials(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._partial_task and not self._partial_task.done():
            self._partial_task.cancel()

    def _maybe_partial(self) -> None:
        if self.done or (self._partial_task and not self._partial_task.done()):
            return
        if self.samples - self._partial_upto < self._min_new:
            return
        wait = self._last_partial_at + self._interval - time.monotonic()
        if wait <= 0 and self._engine.gate.busy:
            wait = 0.1  # never queue behind another decode; look again shortly
        elif wait <= 0 and self._new_audio_is_silent():
            # Nothing new to show; keeps the model idle during the end-of-turn silence so the final starts at once.
            wait = self._interval
        if wait > 0:
            if self._timer is None:
                self._timer = asyncio.get_running_loop().call_later(wait, self._on_timer)
            return
        self._partial_task = asyncio.create_task(self._run_partial())

    def _new_audio_is_silent(self) -> bool:
        return is_silent(pcm16_to_float(self._buf[self._partial_upto * 2 :]), self._engine.silence_dbfs)

    def _on_timer(self) -> None:
        self._timer = None
        self._maybe_partial()

    async def _run_partial(self) -> None:
        n = self.samples
        self._last_partial_at = time.monotonic()
        self._partial_upto = n
        audio = pcm16_to_float(self._buf[: n * 2])
        try:
            if n - self._window_start > self._window:
                # Freeze the older audio at a quiet point and decode it together with the live tail
                # in one model call, so every decoded segment stays within the window.
                lo = self._window_start + self._window // 2
                hi = min(n - SAMPLE_RATE * 3 // 10, self._window_start + self._window)
                cut = quietest_cut(audio, lo, hi)
                frozen, tail = await self._engine.transcribe_many(
                    [audio[self._window_start : cut], audio[cut:]], self._context, final=False
                )
                self._stable_text = _join(self._stable_text, frozen)
                self._window_start = cut
            else:
                tail = await self._engine.transcribe(audio[self._window_start :], self._context, final=False)
            self.partial_decodes += 1
            if not self.done:
                await self._send({"type": "partial", "text": _join(self._stable_text, tail), "audio_ms": n * 1000 // SAMPLE_RATE})
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("stream.partial_failed")  # partials are best-effort; the final reports errors
        finally:
            self._partial_task = None
            self._maybe_partial()
