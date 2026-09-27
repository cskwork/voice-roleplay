"""Async client for llama-server's OpenAI-compatible chat endpoint (PROTOCOL §5).

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


class LlmError(Exception):
    """code: unreachable | timeout | http_<status> | invalid_json | truncated"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class LlmClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8713", timeout_s: float = 60, api_key: str | None = None):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout_s, connect=5.0), headers=headers)
        # llama-server "timings" of the last completed request (token counts and ms only, no text).
        self.last_timings: dict | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "LlmClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    @staticmethod
    def _body(messages: list[dict], max_tokens: int, slot_id: int | None, **extra) -> dict:
        body = {"messages": messages, "max_tokens": max_tokens, "cache_prompt": True, **extra}
        if slot_id is not None:
            body["id_slot"] = slot_id
        return body

    async def stream_chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 128,
        slot_id: int | None = None,
        cancel: asyncio.Event | None = None,
    ) -> AsyncIterator[str]:
        """Yield content deltas. Setting `cancel` closes the HTTP stream, which makes llama-server stop the slot."""
        body = self._body(messages, max_tokens, slot_id, stream=True, **_ROLEPLAY_SAMPLING)
        cancel = cancel or asyncio.Event()
        self.last_timings = None
        try:
            async with self._http.stream("POST", "/v1/chat/completions", json=body) as resp:
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
        """Return delta text ('' for none), or None at end of stream."""
        if not line.startswith("data:"):
            return ""
        data = line[5:].strip()
        if data == "[DONE]":
            return None
        chunk = json.loads(data)
        if "timings" in chunk:
            self.last_timings = chunk["timings"]
        choices = chunk.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("delta") or {}).get("content") or ""

    async def warm(self, messages: list[dict], *, slot_id: int | None = None) -> None:
        """Process the prompt prefix into the slot's KV cache (generates at most one token)."""
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
        try:
            resp = await self._http.get("/health")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("status") == "ok"

    async def _complete(self, body: dict) -> dict:
        try:
            resp = await self._http.post("/v1/chat/completions", json=body)
        except httpx.TimeoutException as e:
            raise LlmError("timeout") from e
        except httpx.TransportError as e:
            raise LlmError("unreachable") from e
        if resp.status_code != 200:
            raise LlmError(f"http_{resp.status_code}")
        data = resp.json()
        self.last_timings = data.get("timings")
        return data
