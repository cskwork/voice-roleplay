"""LLM adapter for API mode with llm_provider = "anthropic": the Claude API with ANTHROPIC_API_KEY (official SDK).

Same interface as `vr_feedback.llm.LlmClient` (`stream_chat`, `json_chat`, `warm`, `health`, `aclose`).

- Model `anthropic_model` (default claude-haiku-5-5). Sampling parameters are not sent (non-default values are
  rejected on current models). Effort is "low" on models that take it (not Haiku 4.5, Sonnet 4.5 and older); on
  Haiku thinking is also turned off (Opus 5.5 and Sonnet 5.5 reject that): short roleplay replies, first text as
  soon as possible.
- System messages go to `system`; the other turns are sent as they are, with a leading "(start)" user turn when the
  conversation begins with the assistant (the API needs a user turn first) and same-role neighbours merged.
- json_chat uses structured outputs (`output_config.format` json_schema). The app's schemas use constraints the API does
  not accept (minLength, maxLength, maxItems): the request sends `anthropic.transform_schema(schema)` and the answer is
  checked against the original schema.
- Cancel (or closing the generator) closes the HTTP stream at once, also while waiting for the next text.

Never logs prompts, model output or the key (PROTOCOL §2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator

import jsonschema
from vr_feedback.llm import LlmError

log = logging.getLogger("vr_gateway.adapters.llm_anthropic")
API_URL = "https://api.anthropic.com"

# Models without the effort parameter (it is rejected with a 400): Claude 3.x, Haiku 4.5, Sonnet 4 / 4.5, Opus 4 / 4.1.
NO_EFFORT = ("claude-3", "claude-haiku-4", "claude-sonnet-4-0", "claude-sonnet-4-2", "claude-sonnet-4-5",
             "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-2")


@contextlib.contextmanager
def _without_env_headers():
    """The SDK merges ANTHROPIC_CUSTOM_HEADERS into its headers when a client is built (2.14 sends the key itself as
    `Authorization: Bearer`, so another Authorization there would replace it): hide the variable for that moment only.
    The constructor does not await, so nothing else on the event loop sees the gap."""
    saved = os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_CUSTOM_HEADERS"] = saved


def takes_effort(model: str) -> bool:
    return not model.startswith(NO_EFFORT)


def split(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").strip()
    turns: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        role = "assistant" if role == "assistant" else "user"
        text = str(m.get("content") or "")
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "(start)"})
    if turns[-1]["role"] != "user":  # no assistant prefill on current models
        turns.append({"role": "user", "content": "(continue)"})
    return system, turns


class AnthropicLlm:
    name = "anthropic"
    remote = True

    def __init__(self, api_key: str, model: str = "claude-haiku-5-5", *, timeout_s: float = 60.0, client=None):
        self.model = model
        self._has_key = bool(api_key)
        self.last_timings: dict | None = None
        if client is not None:
            self._client = client
        elif api_key:
            from anthropic import AsyncAnthropic

            # The disclosed destination: an inherited ANTHROPIC_BASE_URL must not redirect learner text, and
            # ANTHROPIC_LOG=debug must not write request bodies to the gateway log (PROTOCOL §2).
            # ANTHROPIC_LOG=debug must not write request bodies to the gateway log, OpenTelemetry content tracing must not
            # export them, and ANTHROPIC_CUSTOM_HEADERS (e.g. another Authorization) must not replace this key (PROTOCOL §2).
            logging.getLogger("anthropic").setLevel(logging.WARNING)
            with _without_env_headers():
                self._client = AsyncAnthropic(api_key=api_key, base_url=API_URL, timeout=timeout_s, max_retries=1,
                                              open_telemetry=False)
        else:
            self._client = None

    def _params(self, messages: list[dict], max_tokens: int) -> dict:
        system, turns = split(messages)
        params = {"model": self.model, "max_tokens": max(64, int(max_tokens)), "messages": turns}
        if takes_effort(self.model):
            params["output_config"] = {"effort": "low"}
        if self.model.startswith("claude-haiku"):
            # Haiku accepts thinking off at effort low; Opus 5.5 / Sonnet 5.5 reject it (400), so for them effort low
            # alone keeps the reply short.
            params["thinking"] = {"type": "disabled"}
        if system:
            params["system"] = system
        return params

    def _error(self, exc: Exception) -> LlmError:
        import anthropic

        if isinstance(exc, anthropic.APITimeoutError):
            return LlmError("timeout")
        if isinstance(exc, anthropic.APIConnectionError):
            return LlmError("unreachable")
        if isinstance(exc, anthropic.APIStatusError):
            return LlmError(f"http_{exc.status_code}")
        return LlmError("provider_error")

    async def stream_chat(self, messages: list[dict], *, max_tokens: int = 128, slot_id: int | None = None,
                          cancel: asyncio.Event | None = None) -> AsyncIterator[str]:
        if self._client is None:
            raise LlmError("http_401")
        cancel = cancel or asyncio.Event()
        if cancel.is_set():
            return
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        async def produce() -> None:
            """Opens the stream and feeds the queue; cancelling this task closes the HTTP request at any point
            (also while waiting for the response headers or a retry)."""
            if cancel.is_set():  # checked in the task, right before the request opens
                queue.put_nowait(done)
                return
            try:
                async with self._client.messages.stream(**self._params(messages, max_tokens)) as stream:
                    async for text in stream.text_stream:
                        if text:
                            queue.put_nowait(text)
                queue.put_nowait(done)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - handed to the consumer
                queue.put_nowait(exc)

        producer = asyncio.ensure_future(produce())
        cancel_wait = asyncio.ensure_future(cancel.wait())
        item_wait: asyncio.Future | None = None
        try:
            while True:
                item_wait = asyncio.ensure_future(queue.get())
                await asyncio.wait({item_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
                if cancel.is_set() or not item_wait.done():
                    return  # cancelled (also when an item arrived at the same moment): the finally block stops it
                item = item_wait.result()
                if item is done:
                    return
                if isinstance(item, Exception):
                    if type(item).__module__.startswith("anthropic"):
                        log.warning("llm_anthropic_failed error=%s", type(item).__name__)
                        raise self._error(item) from item
                    raise item
                yield item
        finally:
            cancel_wait.cancel()
            if item_wait is not None and not item_wait.done():
                item_wait.cancel()
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(BaseException):
                    await producer

    async def json_chat(self, messages: list[dict], schema: dict, *, max_tokens: int = 768,
                        slot_id: int | None = None) -> dict:
        if self._client is None:
            raise LlmError("http_401")
        params = self._params(messages, max_tokens)
        from anthropic import transform_schema

        params["output_config"] = {**params.get("output_config", {}),
                                   "format": {"type": "json_schema", "schema": transform_schema(schema)}}
        try:
            resp = await self._client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__module__.startswith("anthropic"):
                log.warning("llm_anthropic_failed error=%s", type(exc).__name__)
                raise self._error(exc) from exc
            raise
        if resp.stop_reason == "max_tokens":
            raise LlmError("truncated")
        if resp.stop_reason == "refusal":
            raise LlmError("provider_error")
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LlmError("invalid_json") from e
        if not isinstance(data, dict):
            raise LlmError("invalid_json")
        try:
            jsonschema.validate(data, schema)  # the constraints transform_schema moved into descriptions
        except jsonschema.ValidationError as e:
            raise LlmError("invalid_json") from e
        return data

    async def warm(self, messages: list[dict] | None = None, *, slot_id: int | None = None) -> None:
        """Hosted prompt caching is automatic; nothing to warm and nothing billed."""

    async def health(self) -> bool:
        return self._has_key

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
