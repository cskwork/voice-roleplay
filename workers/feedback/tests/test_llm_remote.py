"""LlmClient in API mode (OpenRouter) against a FAKE HTTP server on a real socket (no network, no key).

FakeChatServer is a labelled test double: a tiny HTTP/1.1 server that records each request and answers with a
scripted SSE stream or JSON body, and notes when the client hangs up mid-stream (cancellation must close the HTTP
request so the provider stops generating).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest

from vr_feedback import feedback, goals, hints, summary
from vr_feedback.llm import LLAMA_ONLY, LlmClient, LlmError


class FakeChatServer:
    def __init__(self):
        self.requests: list[dict] = []
        self.deltas = ["Sure", ",", " one", " latte", "."]
        self.delta_delay = 0.0
        self.mode = "sse"  # sse | sse_error | json | json_error | status_500
        self.json_content = '{"ko": "라테 한 잔 주세요"}'
        self.disconnected = asyncio.Event()
        self.chunks_sent = 0
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = {k.lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
            body = json.loads(await reader.readexactly(int(headers.get("content-length", "0"))) or b"{}")
            self.requests.append({"method": method, "path": path, "headers": headers, "body": body})
            if self.mode == "status_500":
                await self._send(writer, 500, b'{"error":{"code":500,"message":"x"}}', "application/json")
            elif self.mode in ("json", "json_error"):
                data = {"error": {"code": 502, "message": "upstream"}} if self.mode == "json_error" else {
                    "choices": [{"message": {"content": self.json_content}, "finish_reason": "stop"}]}
                await self._send(writer, 200, json.dumps(data).encode(), "application/json")
            else:
                await self._stream(writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            self.disconnected.set()
        finally:
            writer.close()

    async def _send(self, writer, status: int, payload: bytes, ctype: str):
        writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(payload)}\r\n"
                     "Connection: close\r\n\r\n".encode() + payload)
        await writer.drain()

    async def _stream(self, writer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")

        async def chunk(text: str):
            data = text.encode()
            writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            await writer.drain()

        await chunk(": OPENROUTER PROCESSING\n\n")
        for i, delta in enumerate(self.deltas):
            if self.delta_delay:
                await asyncio.sleep(self.delta_delay)
            if writer.is_closing() or writer.transport.get_extra_info("socket") is None:
                raise ConnectionError
            if self.mode == "sse_error" and i == 2:
                await chunk("data: " + json.dumps({"error": {"code": 502, "message": "upstream"},
                                                   "choices": [{"index": 0, "delta": {"content": ""},
                                                                "finish_reason": "error"}]}) + "\n\n")
                break
            await chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": delta}}]}) + "\n\n")
            self.chunks_sent += 1
        else:
            await chunk("data: [DONE]\n\n")
        await chunk("")


@pytest.fixture
async def server():
    s = await FakeChatServer().start()
    yield s
    await s.stop()


def client(server: FakeChatServer, key: str = "or-test") -> LlmClient:
    return LlmClient.openrouter(key, "openai/gpt-4.1-mini", base_url=f"http://127.0.0.1:{server.port}/api/v1")


MESSAGES = [{"role": "system", "content": "You are a barista."}, {"role": "user", "content": "A latte, please."}]
PROVIDER = {"order": ["openai", "azure"], "allow_fallbacks": False, "zdr": True, "data_collection": "deny",
            "require_parameters": True}


async def test_stream_request_body_and_deltas(server):
    async with client(server) as llm:
        out = [d async for d in llm.stream_chat(MESSAGES, max_tokens=128, slot_id=0)]
    assert "".join(out) == "Sure, one latte."
    req = server.requests[0]
    assert req["method"] == "POST" and req["path"] == "/api/v1/chat/completions"
    assert req["headers"]["authorization"] == "Bearer or-test"
    body = req["body"]
    assert body["model"] == "openai/gpt-4.1-mini" and body["stream"] is True and body["max_tokens"] == 128
    assert body["provider"] == PROVIDER
    assert body["messages"] == MESSAGES and body["temperature"] == 0.7
    assert not set(LLAMA_ONLY) & set(body)  # no id_slot / cache_prompt / top_k / min_p


async def test_cancel_event_closes_the_http_request(server):
    server.deltas = [f" w{i}" for i in range(200)]
    server.delta_delay = 0.01
    cancel = asyncio.Event()
    got = []
    async with client(server) as llm:
        async for delta in llm.stream_chat(MESSAGES, cancel=cancel):
            got.append(delta)
            if len(got) == 3:
                cancel.set()  # barge-in
        await asyncio.wait_for(server.disconnected.wait(), 2)
    assert len(got) == 3 and server.chunks_sent < 200


async def test_closing_after_first_question_closes_the_http_request(server):
    """The gateway closes the stream once a segment ends in "?" (one question per reply)."""
    server.deltas = ["Anything", " else?", " We", " also"] + [" more"] * 200
    server.delta_delay = 0.01
    async with client(server) as llm:
        text = ""
        async with aclosing(llm.stream_chat(MESSAGES)) as stream:
            async for delta in stream:
                text += delta
                if text.endswith("?"):
                    break
        await asyncio.wait_for(server.disconnected.wait(), 2)
    assert text == "Anything else?" and server.chunks_sent < 200


async def test_mid_stream_error_and_http_errors(server):
    server.mode = "sse_error"
    async with client(server) as llm:
        with pytest.raises(LlmError) as err:
            [d async for d in llm.stream_chat(MESSAGES)]
        assert err.value.code == "provider_error"
        server.mode = "status_500"
        with pytest.raises(LlmError) as err:
            [d async for d in llm.stream_chat(MESSAGES)]
        assert err.value.code == "http_500"


async def test_json_chat_body_and_errors(server):
    server.mode = "json"
    schema = {"type": "object", "properties": {"ko": {"type": "string"}}, "required": ["ko"],
              "additionalProperties": False}
    async with client(server) as llm:
        assert await llm.json_chat(MESSAGES, schema, max_tokens=128) == {"ko": "라테 한 잔 주세요"}
        body = server.requests[-1]["body"]
        assert body["response_format"] == {"type": "json_schema",
                                           "json_schema": {"name": "result", "schema": schema, "strict": True}}
        assert body["provider"] == PROVIDER and body["temperature"] == 0.2 and "stream" not in body
        assert not set(LLAMA_ONLY) & set(body)
        server.mode = "json_error"  # OpenRouter may answer 200 with only an error object
        with pytest.raises(LlmError) as err:
            await llm.json_chat(MESSAGES, schema)
        assert err.value.code == "provider_error"


async def test_warm_and_health_make_no_request(server):
    async with client(server) as llm:
        await llm.warm(MESSAGES, slot_id=0)
        assert await llm.health() is True
    async with client(server, key="") as keyless:
        assert await keyless.health() is False
    assert server.requests == []


async def test_local_body_keeps_llama_fields():
    llm = LlmClient("http://127.0.0.1:9", default_slot=1)
    body = llm._body(MESSAGES, 64, None, temperature=0.7, top_k=20, min_p=0.0)
    assert body["id_slot"] == 1 and body["cache_prompt"] is True and body["top_k"] == 20 and "model" not in body
    assert llm.chat_path == "/v1/chat/completions" and not llm.remote
    await llm.aclose()


def _strict_ok(schema: dict) -> bool:
    """OpenAI strict structured outputs: every object lists all properties as required, no extra properties."""
    if schema.get("type") == "object":
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is not False or set(schema.get("required", [])) != set(props):
            return False
        return all(_strict_ok(p) for p in props.values())
    if schema.get("type") == "array":
        return _strict_ok(schema["items"])
    return True


def test_every_json_schema_is_strict_compatible():
    """API mode sends `strict: true`; a schema OpenAI rejects would silently turn off feedback, goals and hints."""
    schemas = [feedback._items_schema(3), feedback._items_schema(3, ["t1", "t2"]), goals._schema(["g1"], ["t1"]),
               hints._TRANSLATE_SCHEMA, summary._SCHEMA]
    assert all(_strict_ok(s) for s in schemas)
