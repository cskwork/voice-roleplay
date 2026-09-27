import asyncio
import heapq
import itertools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import numpy as np

from .audio import is_silent
from .backends import Backend

T = TypeVar("T")


class DecodeGate:
    """Runs model calls one at a time on a single thread; waiting finals go ahead of waiting partials."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-decode")
        self._busy = False
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    @property
    def busy(self) -> bool:
        return self._busy or bool(self._waiters)

    async def run(self, fn: Callable[[], T], *, final: bool) -> T:
        if self.busy:
            slot = asyncio.get_running_loop().create_future()
            heapq.heappush(self._waiters, (0 if final else 1, next(self._seq), slot))
            try:
                await slot
            except asyncio.CancelledError:
                if slot.done() and not slot.cancelled():
                    self._release()  # the slot was handed to us just before cancellation; pass it on
                raise
        else:
            self._busy = True

        job = asyncio.get_running_loop().run_in_executor(self._executor, fn)
        job.add_done_callback(self._on_job_done)
        # A cancelled caller must not free the gate while the model is still running.
        return await asyncio.shield(job)

    def _on_job_done(self, job: asyncio.Future) -> None:
        if not job.cancelled():
            job.exception()  # mark retrieved when the caller has gone away
        self._release()

    def _release(self) -> None:
        while self._waiters:
            _, _, slot = heapq.heappop(self._waiters)
            if not slot.done():
                slot.set_result(None)  # hand over; stays busy
                return
        self._busy = False

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class AsrEngine:
    def __init__(self, backend: Backend, silence_dbfs: float):
        self.backend = backend
        self.silence_dbfs = silence_dbfs
        self.gate = DecodeGate()

    async def transcribe_many(self, audios: list[np.ndarray], context: str, *, final: bool) -> list[str]:
        """Decode several segments in one model call; near-silent segments are skipped and return ""."""
        voiced = [i for i, a in enumerate(audios) if not is_silent(a, self.silence_dbfs)]
        texts = [""] * len(audios)
        if voiced:
            outs = await self.gate.run(
                lambda: self.backend.transcribe([audios[i] for i in voiced], context), final=final
            )
            for i, text in zip(voiced, outs):
                texts[i] = text.strip()
        return texts

    async def transcribe(self, audio: np.ndarray, context: str, *, final: bool = True) -> str:
        return (await self.transcribe_many([audio], context, final=final))[0]

    def warmup(self) -> None:
        """First MPS decode pays kernel compilation; run it before reporting ready."""
        rng = np.random.default_rng(0)
        self.backend.transcribe([(rng.standard_normal(16_000) * 0.05).astype(np.float32)], "")
