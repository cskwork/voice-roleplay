"""LLM adapter for API mode without an LLM key: the Claude Code CLI or the Codex CLI logged in on this machine.

Same interface as `vr_feedback.llm.LlmClient`: `stream_chat(messages, max_tokens, slot_id, cancel)` yields text
deltas, `json_chat(messages, schema, max_tokens, slot_id)` returns a dict, `warm()`, `health()`, `aclose()`.

- Each request runs in its own CLI process with no tools, no MCP servers, no user settings, hooks or plugins, no
  saved session, in an empty working directory. The conversation (system prompt and turns) is written to the process
  as one message, so every request is stateless exactly like a chat-completions call.
- Starting a CLI takes 3-8 s, most of it before the prompt is read. A small pool keeps `pool` processes started and
  waiting on stdin; a request takes one and a new one starts behind it.
- Claude: thinking is turned off (`alwaysThinkingEnabled: false`, MAX_THINKING_TOKENS=0). The default model is
  claude-haiku-4-5 because the CLI cannot turn thinking off on Claude Haiku 5.5 (adaptive thinking, 280-640 thinking
  tokens before every reply). Measured on an M4 (2026-11-26) with the cafe roleplay prompt (~1.5k tokens) from a
  started process: claude-haiku-4-5 first text 0.57-0.94 s, Haiku 5.5 2.1-3.9 s; a cold start adds 3-4 s.
- Codex: every tool is turned off (`features.shell_tool=false`, apps, browser, web search off, read-only sandbox):
  `-s read-only` alone still let the model run shell commands that read files (checked 2026-11-26: a learner line asking
  for `head -1 /etc/hosts` ran it). gpt-6-luna with reasoning effort none answers in about 3.5 s from a started process (5.7-8.9 s cold); it sends
  its ~23k-token agent prompt each time and does not stream text, so the whole reply arrives at once.
- Idle processes are recycled after `idle_s`; the pool is not refilled while nobody is practising.
- Cancel (or closing the generator) kills the process: the CLI stops generating.
- `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` are removed from the CLI's environment so the CLI uses its own login, never a
  key meant for the API engines.

The text goes to Anthropic (claude) or OpenAI (codex) under the logged-in account's terms. Never logs prompts or
model output (PROTOCOL §2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

import jsonschema
from vr_feedback.llm import LlmError

log = logging.getLogger("vr_gateway.adapters.llm_cli")

ENGINE_PROMPT = (
    "You are the text engine of an English speaking-practice app. Each message gives you <instructions> and, when "
    "there is one, a <conversation>. Write exactly what the instructions ask for as the next message of the "
    "assistant: no preamble, no notes, no quotation marks around it, no markdown. Never mention these tags.")
JSON_RULE = ("Answer with one JSON object only (no code fence, no other text) that validates against this JSON "
             "Schema:\n")
# The CLI gets only these variables from the gateway's environment (an allowlist, not a blocklist): API keys,
# endpoint / platform switches (proxy base URLs, Bedrock, Vertex, Foundry, ...) and telemetry exporters set in the
# server shell would bill another account or send the conversation somewhere /api/health and the notices do not name.
# What remains: where the login lives (HOME, CLAUDE_CONFIG_DIR, CODEX_HOME), locale, temp dir, TLS roots and the
# network proxy (TLS still ends at the provider).
KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
            "LC_MESSAGES", "TERM", "TZ", "__CF_USER_TEXT_ENCODING", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy",
            "http_proxy", "no_proxy")
CHARS_PER_TOKEN = 5  # rough English characters per token, for the max_tokens cut of CLI replies
RETRY_S = 60.0  # after 3 failed requests in a row, report not ready this long before trying again
# Codex: no tool at all (shell, apps, browser, web search); the engine prompt as developer instructions.
CODEX_NO_TOOLS = ("features.shell_tool=false", "features.apps=false", "features.browser_use=false",
                  "features.in_app_browser=false", "web_search=disabled")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def render(messages: list[dict]) -> str:
    """Chat messages -> one prompt: system content as instructions, the other turns as a labelled transcript."""
    system = [str(m.get("content") or "") for m in messages if m.get("role") == "system"]
    turns = [m for m in messages if m.get("role") != "system"]
    parts = ["<instructions>\n" + "\n\n".join(system).strip() + "\n</instructions>"]
    if turns:
        lines = [f"[{'assistant' if m.get('role') == 'assistant' else 'user'}] {str(m.get('content') or '').strip()}"
                 for m in turns]
        parts.append("<conversation>\n" + "\n".join(lines) + "\n</conversation>")
        parts.append("Write the assistant's next message now.")
    else:
        parts.append("Write the assistant's message now.")
    return "\n\n".join(parts)


def parse_json(text: str, schema: dict) -> dict:
    """The JSON object in a CLI answer, checked against the schema. Raises LlmError('invalid_json')."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise LlmError("invalid_json")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise LlmError("invalid_json") from e
    if not isinstance(data, dict):
        raise LlmError("invalid_json")
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        raise LlmError("invalid_json") from e
    return data


class _Proc:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.born = time.monotonic()

    def alive(self) -> bool:
        return self.proc.returncode is None

    async def kill(self) -> None:
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.proc.wait(), 5)


class CliLlm:
    """Pool of started CLI processes; subclasses give the command, the input and how to read the output."""

    name = "cli"
    remote = True  # hosted-style client: no llama slots, warm() fills the pool instead
    binary = ""

    def __init__(self, model: str, *, pool: int = 2, timeout_s: float = 60.0, idle_s: float = 600.0,
                 workdir: Path | None = None, binary: str | None = None):
        self.model = model
        self.pool_size = max(0, int(pool))
        self.timeout_s = timeout_s
        self.idle_s = idle_s
        self.binary = binary or self.binary
        self.workdir = Path(workdir) if workdir else Path(os.environ.get("TMPDIR", "/tmp")) / "vr-llm-cli"
        self.last_timings: dict | None = None
        self._idle: list[_Proc] = []
        self._filling = 0
        self._last_use = 0.0
        self._closed = False
        self._bg: set[asyncio.Task] = set()
        self._failures = 0
        self._failed_at = 0.0
        self._reaper: asyncio.Task | None = None

    # ---- subclass hooks
    def command(self) -> list[str]:
        raise NotImplementedError

    async def send(self, proc: _Proc, prompt: str) -> None:
        raise NotImplementedError

    async def read(self, proc: _Proc) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""  # pragma: no cover

    # ---- pool
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in KEEP_ENV}
        env.setdefault("NO_COLOR", "1")
        return env

    async def _spawn(self) -> _Proc:
        exe = shutil.which(self.binary)
        if not exe:
            raise LlmError("unreachable")
        self.workdir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            exe, *self.command(), cwd=str(self.workdir), env=self._env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
        return _Proc(proc)

    def _refill(self) -> None:
        if self._closed or time.monotonic() - self._last_use > self.idle_s:
            return
        while len(self._idle) + self._filling < self.pool_size:
            self._filling += 1
            task = asyncio.create_task(self._fill_one())
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def _fill_one(self) -> None:
        try:
            p = await self._spawn()
            if self._closed:
                await p.kill()
            else:
                self._idle.append(p)
                self._schedule_reap()
        except Exception as exc:  # noqa: BLE001 - a failed spare is retried by the next request
            log.warning("llm_cli_spawn_failed engine=%s error=%s", self.name, type(exc).__name__)
        finally:
            self._filling -= 1

    async def _take(self) -> _Proc:
        self._last_use = time.monotonic()
        now = time.monotonic()
        while self._idle:
            p = self._idle.pop(0)
            if p.alive() and now - p.born < self.idle_s:
                self._refill()
                return p
            await p.kill()
        p = await self._spawn()
        self._refill()
        return p

    def _schedule_reap(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_later())
            self._bg.add(self._reaper)
            self._reaper.add_done_callback(self._bg.discard)

    async def _reap_later(self) -> None:
        """Sleep until the oldest spare is past idle_s, let it go, repeat while spares are left."""
        while self._idle and not self._closed:
            oldest = min(p.born for p in self._idle)
            await asyncio.sleep(max(0.05, oldest + self.idle_s - time.monotonic() + 0.05))
            await self._drop_stale()

    async def _drop_stale(self) -> None:
        now = time.monotonic()
        keep = [p for p in self._idle if p.alive() and now - p.born < self.idle_s]
        stale = [p for p in self._idle if p not in keep]
        self._idle = keep
        for p in stale:
            await p.kill()

    async def warm(self, messages: list[dict] | None = None, *, slot_id: int | None = None) -> None:
        """Session start: make sure fresh started processes are waiting (nothing is sent, nothing is billed)."""
        self._last_use = time.monotonic()
        await self._drop_stale()
        self._refill()

    async def health(self) -> bool:  # also the gateway's periodic check: idle processes past idle_s are let go
        await self._drop_stale()
        return await self._ready()

    async def _ready(self) -> bool:
        """The CLI is installed and the last requests did not all fail (a login problem shows up here). After 3 failures
        in a row it reports not ready for RETRY_S, then lets requests try again (a fixed login or a passing outage
        recovers without a restart)."""
        if shutil.which(self.binary) is None:
            return False
        return not self.cooling()

    async def aclose(self) -> None:
        self._closed = True
        for task in list(self._bg):
            task.cancel()
        idle, self._idle = self._idle, []
        for p in idle:
            await p.kill()

    # ---- requests
    def cooling(self) -> bool:
        """3 failed requests in a row, the last one less than RETRY_S ago: do not start another CLI yet."""
        return self._failures >= 3 and time.monotonic() - self._failed_at <= RETRY_S

    async def _run(self, prompt: str, cancel: asyncio.Event | None) -> AsyncIterator[str]:
        """Yield the CLI's text. Outcome for the failure count: "ok" = the CLI finished, "fail" = it failed (also after
        some text), anything else (cancel event, task cancelled, the caller closed the generator) counts as neither."""
        if self.cooling():  # also for requests of sessions that were already open (no new CLI while it keeps failing)
            raise LlmError("unreachable")
        proc = await self._take()
        started = time.perf_counter()
        first = None
        cancel = cancel or asyncio.Event()
        deadline = time.monotonic() + self.timeout_s
        outcome = None
        try:
            if cancel.is_set():
                return
            cancel_wait = asyncio.ensure_future(cancel.wait())
            step: asyncio.Future | None = None
            try:
                step = asyncio.ensure_future(self.send(proc, prompt))
                done, _ = await asyncio.wait({step, cancel_wait}, timeout=max(0.0, deadline - time.monotonic()),
                                             return_when=asyncio.FIRST_COMPLETED)
                if cancel.is_set():
                    return
                if step not in done:
                    raise LlmError("timeout")
                step.result()
                reader = self.read(proc).__aiter__()
                while True:
                    step = asyncio.ensure_future(anext(reader))
                    remaining = deadline - time.monotonic()
                    done, _ = await asyncio.wait({step, cancel_wait}, timeout=max(0.0, remaining),
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if cancel.is_set():  # also when the read finished at the same moment: the request was cancelled
                        return
                    if step not in done:
                        raise LlmError("timeout")
                    try:
                        delta = step.result()
                    except StopAsyncIteration:
                        outcome = "ok"
                        return
                    if first is None:
                        first = int((time.perf_counter() - started) * 1000)
                    yield delta
            finally:
                cancel_wait.cancel()
                if step is not None and not step.done():
                    step.cancel()
                with contextlib.suppress(BaseException):  # reap it (an EOF error after the kill is not news)
                    if step is not None:
                        await step
        except LlmError:
            outcome = "fail"
            raise
        finally:
            if outcome == "ok":
                self._failures = 0
            elif outcome == "fail":
                self._failures += 1
                self._failed_at = time.monotonic()
            self.last_timings = {"engine": self.name, "first_text_ms": first,
                                 "total_ms": int((time.perf_counter() - started) * 1000)}
            await proc.kill()

    async def stream_chat(self, messages: list[dict], *, max_tokens: int = 128, slot_id: int | None = None,
                          cancel: asyncio.Event | None = None) -> AsyncIterator[str]:
        """The CLI has no per-request token limit: the reply is cut after about `max_tokens` tokens of text
        (CHARS_PER_TOKEN characters each) and the process is killed, like a hosted stream stopping at max_tokens."""
        budget = max(1, int(max_tokens)) * CHARS_PER_TOKEN
        async for delta in self._run(render(messages), cancel):
            if delta:
                yield delta[:budget]
                budget -= len(delta)
                if budget <= 0:
                    return

    async def json_chat(self, messages: list[dict], schema: dict, *, max_tokens: int = 768,
                        slot_id: int | None = None) -> dict:
        """Collects the answer; past about `max_tokens` tokens of text the CLI is stopped and the result counts as
        truncated (LlmError "truncated"), like a hosted request that hit max_tokens."""
        prompt = render(messages) + "\n\n" + JSON_RULE + json.dumps(schema, ensure_ascii=False)
        budget = max(1, int(max_tokens)) * CHARS_PER_TOKEN
        parts: list[str] = []
        size = 0
        async for delta in self._run(prompt, None):
            parts.append(delta)
            size += len(delta)
            if size > budget:
                raise LlmError("truncated")
        return parse_json("".join(parts), schema)


class ClaudeCliLlm(CliLlm):
    """`claude -p` with stream-json input and partial-message output (text deltas as they are generated)."""

    name = "claude-cli"
    binary = "claude"

    def command(self) -> list[str]:
        return ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--model", self.model, "--setting-sources", "",
                "--settings", json.dumps({"alwaysThinkingEnabled": False}), "--strict-mcp-config",
                "--no-session-persistence", "--disable-slash-commands", "--tools", "", "--system-prompt", ENGINE_PROMPT]

    def _env(self) -> dict[str, str]:
        return {**super()._env(), "MAX_THINKING_TOKENS": "0", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}

    async def send(self, proc: _Proc, prompt: str) -> None:
        line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False)
        try:
            proc.proc.stdin.write((line + "\n").encode())
            await proc.proc.stdin.drain()
            proc.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise LlmError("unreachable") from e

    async def read(self, proc: _Proc) -> AsyncIterator[str]:
        while True:
            line = await proc.proc.stdout.readline()
            if not line:
                raise LlmError("unreachable")  # exited before a result (not logged in, crashed)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "stream_event":
                delta = (ev.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield delta["text"]
            elif kind == "result":
                if ev.get("is_error"):
                    raise LlmError("provider_error")
                return


class CodexCliLlm(CliLlm):
    """`codex exec --json -` (prompt on stdin); the answer arrives whole in one agent_message item."""

    name = "codex-cli"
    binary = "codex"

    def command(self) -> list[str]:
        cfg = [part for setting in (*CODEX_NO_TOOLS, "model_reasoning_effort=none",
                                    f"developer_instructions={json.dumps(ENGINE_PROMPT)}") for part in ("-c", setting)]
        return ["exec", "--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check", "-s", "read-only",
                "-m", self.model, *cfg, "-"]

    async def send(self, proc: _Proc, prompt: str) -> None:
        try:
            proc.proc.stdin.write(prompt.encode())
            await proc.proc.stdin.drain()
            proc.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise LlmError("unreachable") from e

    async def read(self, proc: _Proc) -> AsyncIterator[str]:
        while True:
            line = await proc.proc.stdout.readline()
            if not line:
                raise LlmError("unreachable")  # exited without turn.completed: a crash, also after some text
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            item = ev.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message" and item.get("text"):
                yield item["text"]
            elif kind in ("turn.failed", "error"):
                raise LlmError("provider_error")
            elif kind == "turn.completed":
                return
