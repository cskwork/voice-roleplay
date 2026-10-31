"""Async client for an OpenAI-compatible chat endpoint (PROTOCOL §5).

Local mode: llama-server (`/v1/chat/completions`, slot pinning and prompt cache fields).
API mode: OpenRouter (`LlmClient.openrouter`), which drops the llama-only fields (`id_slot`, `cache_prompt`, `top_k`,
`min_p`) and pins provider routing (`provider`: listed upstreams only, no fallbacks, zero data retention, no data
collection, every parameter supported).

Never logs prompts or model output (PROTOCOL §2).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

# Qwen3-Instruct-2507 recommended sampling.
_ROLEPLAY_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0}
_JSON_SAMPLING = {"temperature": 0.2, "top_p": 0.8, "top_k": 20, "min_p": 0.0}
# Fields only llama-server understands; never sent to a hosted provider.
LLAMA_ONLY = ("top_k", "min_p", "cache_prompt", "id_slot")
OPENROUTER_URL = "https://openrouter.ai/api/v1"


class LlmError(Exception):
    """code: unreachable | timeout | http_<status> | invalid_json | truncated | provider_error"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LlmClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8713", timeout_s: float = 60, api_key: str | None = None,
                 default_slot: int | None = None, *, model: str | None = None, provider: dict | None = None,
                 headers: dict[str, str] | None = None, chat_path: str = "/v1/chat/completions"):
        """`default_slot` pins requests that pass no `slot_id` to one llama-server slot (id_slot), so
        background work (feedback, goals, summaries) never evicts the roleplay prefix cached in another slot.

        `model` set = a hosted endpoint (see `openrouter`): llama-only fields are left out and `provider` routing
        is added to every body. Slots do not exist there; `slot_id`/`default_slot` are accepted and ignored."""
        all_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        all_headers.update(headers or {})
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s, connect=5.0),
                                       headers=all_headers)
        self.remote = model is not None
        self.model = model
        self.provider = provider
        self.chat_path = chat_path
        self._has_key = bool(api_key)
        self.default_slot = default_slot
        # llama-server "timings" of the last completed request (token counts and ms only, no text).
        self.last_timings: dict | None = None

    @classmethod
    def openrouter(cls, api_key: str, model: str, *, providers: list[str] | tuple[str, ...] = ("openai", "azure"),
                   base_url: str = OPENROUTER_URL, timeout_s: float = 60) -> "LlmClient":
        """OpenRouter chat completions (API mode). Routing is pinned: only `providers`, in order, no fallbacks,
        zero-data-retention endpoints only, no providers that collect data, and only endpoints that support every
        request parameter (json_schema output included)."""
        provider = {"order": list(providers), "allow_fallbacks": False, "zdr": True, "data_collection": "deny",
                    "require_parameters": True}
        return cls(base_url, timeout_s=timeout_s, api_key=api_key or None, model=model, provider=provider,
                   headers={"X-OpenRouter-Title": "voice-roleplay"}, chat_path="/chat/completions")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "LlmClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _body(self, messages: list[dict], max_tokens: int, slot_id: int | None, **extra) -> dict:
        body = {"messages": messages, "max_tokens": max_tokens, "cache_prompt": True, **extra}
        slot_id = self.default_slot if slot_id is None else slot_id
        if slot_id is not None:
            body["id_slot"] = slot_id
        if self.remote:
            body = {k: v for k, v in body.items() if k not in LLAMA_ONLY}
            body = {"model": self.model, **body, "provider": self.provider}
        return body

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 128,
        slot_id: int | None = None,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas. Setting `cancel` (or closing this generator) closes the HTTP stream, which makes
        llama-server stop the slot and a hosted provider stop generating (and billing, for OpenAI/Azure)."""
        body = self._body(messages, max_tokens, slot_id, stream=True, **_ROLEPLAY_SAMPLING)
        cancel = cancel or asyncio.Event()
        self.last_timings = None
        try:
            async with self._http.stream("POST", self.chat_path, json=body) as resp:
                if resp.status_code != 200:
                    raise LlmError(f"http_{resp.status_code}")
                lines = resp.aiter_lines()
                cancel_wait = asyncio.ensure_future(cancel.wait())
                try:
                    while not cancel.is_set():
                        next_line = asyncio.ensure_future(anext(lines))
                        await asyncio.wait({next_line, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
                        if not next_line.done():
                            next_line.cancel()
                            break
                        try:
                            line = next_line.result()
                        except StopAsyncIteration:
                            break
                        delta = self._parse_sse(line)
                        if delta is None:
                            break
                        if delta:
                            yield delta
                finally:
                    cancel_wait.cancel()
        except httpx.TimeoutException as e:
            raise LlmError("timeout") from e
        except httpx.TransportError as e:
            raise LlmError("unreachable") from e

    def _parse_sse(self, line: str) -> str | None:
        """Return delta text ('' for none), or None at end of stream. Comment lines (": OPENROUTER PROCESSING")
        are keep-alives. A chunk with a top-level `error` (OpenRouter mid-stream failure) raises."""
        if not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            return None
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as e:
            raise LlmError("provider_error") from e
        if chunk.get("error"):
            raise LlmError("provider_error")
        if "timings" in chunk:
            self.last_timings = chunk["timings"]
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("delta") or {}).get("content") or ""

    async def warm(self, messages: list[dict], *, slot_id: int | None = None) -> None:
        """Process the prompt prefix into the slot's KV cache (generates at most one token). Hosted endpoints have
        no slots to warm (their prompt cache is automatic), so this does nothing there and costs nothing."""
        if self.remote:
            return
        await self._complete(self._body(messages, 1, slot_id, **_ROLEPLAY_SAMPLING))

    async def json_chat(self, messages: list[dict], schema: dict, *, max_tokens: int = 768, slot_id: int | None = None) -> dict:
        """Grammar-constrained JSON reply. Raises LlmError('invalid_json'|'truncated') if unusable."""
        body = self._body(
            messages,
            max_tokens,
            slot_id,
            response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}},
            **_JSON_SAMPLING,
        )
        data = await self._complete(body)
        if self.remote and (data.get("error") or not data.get("choices")):
            raise LlmError("provider_error")  # OpenRouter can answer 200 with only an error object
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise LlmError("truncated" if choice.get("finish_reason") == "length" else "invalid_json") from e
        if not isinstance(parsed, dict):
            raise LlmError("invalid_json")
        return parsed

    async def health(self) -> bool:
        """Local: llama-server /health. Hosted: whether a key is configured (no network call, no cost)."""
        if self.remote:
            return self._has_key
        try:
            resp = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("status") == "ok"

    async def _complete(self, body: dict) -> dict:
        try:
            resp = await self._http.post(self.chat_path, json=body)
        except httpx.TimeoutException as e:
            raise LlmError("timeout") from e
        except httpx.TransportError as e:
            raise LlmError("unreachable") from e
        if resp.status_code != 200:
            raise LlmError(f"http_{resp.status_code}")
        data = resp.json()
        self.last_timings = data.get("timings")
        return data
