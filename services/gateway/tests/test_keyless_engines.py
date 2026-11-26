"""API mode without keys (2026-11-26): engine choice, the CLI text engines, Kokoro speech and the Claude API mapping.

The CLIs are replaced by small fake executables on PATH and Kokoro by a fake engine: nothing here calls a provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import REPO
from vr_feedback.llm import LlmError

from vr_gateway import engines
from vr_gateway.adapters.llm_anthropic import split
from vr_gateway.adapters.llm_cli import ClaudeCliLlm, CodexCliLlm, parse_json, render
from vr_gateway.adapters.tts_kokoro import KokoroTts, clean_text

# ---------------------------------------------------------------- engine choice


def test_resolve_defaults_and_auto():
    assert engines.resolve({}, {}) == {"llm": "claude-cli", "asr": "local", "tts": "kokoro"}
    keys = {"OPENAI_API_KEY": "k", "OPENROUTER_API_KEY": "k"}
    assert engines.resolve(keys, {}) == {"llm": "claude-cli", "asr": "openai", "tts": "openrouter"}
    # Settings win over auto; env wins over config.toml.
    assert engines.resolve(keys, {"tts_provider": "kokoro", "asr_provider": "local"})["tts"] == "kokoro"
    assert engines.resolve({"VR_API_LLM_PROVIDER": "codex-cli"}, {"llm_provider": "anthropic"})["llm"] == "codex-cli"
    assert engines.required_keys({"llm": "anthropic", "asr": "local", "tts": "kokoro"}) == ["anthropic"]
    assert engines.required_keys(engines.resolve({}, {})) == []
    with pytest.raises(ValueError):
        engines.resolve({"VR_API_ASR_PROVIDER": "whisper"}, {})


# ---------------------------------------------------------------- prompts


def test_render_and_parse_json():
    text = render([{"role": "system", "content": "Be a barista."}, {"role": "assistant", "content": "Hi!"},
                   {"role": "user", "content": "A latte, please."}])
    assert text.startswith("<instructions>\nBe a barista.\n</instructions>")
    assert "[assistant] Hi!\n[user] A latte, please." in text and text.endswith("next message now.")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    assert parse_json('```json\n{"ok": true}\n```', schema) == {"ok": True}
    for bad in ("no json", '{"ok": "yes"}', "[1]"):
        with pytest.raises(LlmError):
            parse_json(bad, schema)


def test_anthropic_split_keeps_user_first_and_last():
    system, turns = split([{"role": "system", "content": "S"}, {"role": "assistant", "content": "Hello."}])
    assert system == "S" and [t["role"] for t in turns] == ["user", "assistant", "user"]
    _, turns = split([{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert turns == [{"role": "user", "content": "a\nb"}]


# ---------------------------------------------------------------- fake CLIs

FAKE_CLAUDE = r'''
import json, sys, time
line = sys.stdin.readline()
if not line:
    sys.exit(0)
prompt = json.loads(line)["message"]["content"]
open(sys.argv[0] + ".seen", "a").write(prompt.replace("\n", " ") + "\n")
if "FAIL" in prompt:
    sys.exit(1)
if "SLOW" in prompt:
    time.sleep(30)
out = '{"ok": true}' if "JSON Schema" in prompt else "Sure! What size?"
for part in (out[:5], out[5:]):
    print(json.dumps({"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": part}}}), flush=True)
print(json.dumps({"type": "result", "is_error": False}), flush=True)
'''

FAKE_CODEX = r'''
import json, sys
prompt = sys.stdin.read()
print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Hello from codex."}}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
'''


def fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-the-cli")
    return tmp_path


async def test_claude_cli_streams_from_a_started_process(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("haiku", pool=1, workdir=fake_path / "work", timeout_s=10)
    try:
        assert await llm.health()
        await llm.warm()
        for _ in range(50):  # the spare process starts in the background
            if llm._idle:
                break
            await asyncio.sleep(0.05)
        assert len(llm._idle) == 1
        deltas = [d async for d in llm.stream_chat([{"role": "system", "content": "S"},
                                                    {"role": "user", "content": "hi"}])]
        assert "".join(deltas) == "Sure! What size?" and len(deltas) == 2
        assert llm.last_timings["engine"] == "claude-cli" and llm.last_timings["first_text_ms"] is not None
        assert await llm.json_chat([{"role": "user", "content": "x"}],
                                   {"type": "object", "properties": {"ok": {"type": "boolean"}}}) == {"ok": True}
        seen = (fake_path / "bin" / "claude.seen").read_text()
        assert "<instructions>" in seen and "JSON Schema" in seen
    finally:
        await llm.aclose()


async def test_claude_cli_errors_and_cancel(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("haiku", pool=0, workdir=fake_path / "work", timeout_s=10)
    try:
        with pytest.raises(LlmError) as err:
            _ = [d async for d in llm.stream_chat([{"role": "user", "content": "FAIL"}])]
        assert err.value.code == "unreachable"
        cancel = asyncio.Event()
        stream = llm.stream_chat([{"role": "user", "content": "SLOW"}], cancel=cancel)
        task = asyncio.ensure_future(anext(stream, None))
        await asyncio.sleep(0.5)
        cancel.set()
        assert await asyncio.wait_for(task, 5) is None  # ends quietly, the process is killed
        llm.timeout_s = 0.5
        with pytest.raises(LlmError) as err:
            _ = [d async for d in llm.stream_chat([{"role": "user", "content": "SLOW"}])]
        assert err.value.code == "timeout"
    finally:
        await llm.aclose()


async def test_cli_without_binary_is_not_ready(fake_path):
    llm = ClaudeCliLlm("haiku", pool=0, workdir=fake_path / "work")
    assert await llm.health() is False
    with pytest.raises(LlmError):
        _ = [d async for d in llm.stream_chat([{"role": "user", "content": "hi"}])]


async def test_codex_cli_answer(fake_path):
    fake_bin(fake_path, "codex", FAKE_CODEX)
    llm = CodexCliLlm("gpt-6-luna", pool=0, workdir=fake_path / "work", timeout_s=10)
    try:
        assert "".join([d async for d in llm.stream_chat([{"role": "user", "content": "hi"}])]) == "Hello from codex."
        cmd = " ".join(llm.command())
        # No tool at all: -s read-only alone still lets Codex run shell commands that read files.
        assert "features.shell_tool=false" in cmd and "web_search=disabled" in cmd and "features.apps=false" in cmd
        assert "developer_instructions=" in cmd and "--ignore-user-config" in cmd and "base_instructions" not in cmd
    finally:
        await llm.aclose()


def test_cli_environment_drops_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    env = ClaudeCliLlm("haiku")._env()
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env and "PATH" in env
    assert env["MAX_THINKING_TOKENS"] == "0" and "alwaysThinkingEnabled" in " ".join(ClaudeCliLlm("m").command())


# ---------------------------------------------------------------- Kokoro


class FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, voice, speed, lang):
        self.calls.append((text, voice, speed, lang))
        return [0.0, 0.5, -0.5] * 4000, 24000  # 0.5 s


def kokoro(tmp_path: Path, engine: FakeKokoro) -> KokoroTts:
    model, voices = tmp_path / "m.onnx", tmp_path / "v.bin"
    model.write_bytes(b"x")
    voices.write_bytes(b"x")
    return KokoroTts(model, voices, REPO / "config/voices.kokoro.json", engine_factory=lambda: engine)


async def test_kokoro_loads_then_speaks(tmp_path):
    engine = FakeKokoro()
    tts = kokoro(tmp_path, engine)
    first = await tts.health()
    assert first["provider"] == "kokoro" and {v["voice_id"] for v in first["voices"]} == {"kokoro_heart_f",
                                                                                          "kokoro_michael_m"}
    assert await tts.ready() and (await tts.health())["ready"]
    assert engine.calls[0][0] == "Hello."  # warm-up
    wav = await tts.synthesize_wav("libritts_r_1188_m", "Hi <|endofprompt|> there.", speed=3.0)
    assert wav[:4] == b"RIFF" and engine.calls[-1] == ("Hi there.", "am_michael", 1.5, "en-us")
    stream = tts.stream_session()
    events = [e async for e in stream.synthesize("r1", "kokoro_heart_f", "Hello there.")]
    assert events[0] == ("start", 24000) and events[-1][0] == "done"
    assert sum(len(b) for k, b in events if k == "audio") == 12000 * 2 and len(events) == 2 + 5  # 100 ms frames
    await stream.cancel("r2")
    assert [e async for e in stream.synthesize("r2", "kokoro_heart_f", "Bye.")] == [("cancelled", None)]


async def test_kokoro_without_files_reports_why(tmp_path):
    tts = KokoroTts(tmp_path / "none.onnx", tmp_path / "none.bin", REPO / "config/voices.kokoro.json")
    info = await tts.health()
    assert info["ready"] is False and "missing" in info["error"]
    assert clean_text("  a [laugh]  b <b>c</b> ") == "a b c"


def test_kokoro_lock_entry_matches_adapter_paths():
    lock = json.loads((REPO / "models.lock.json").read_text())
    entry = next(m for m in lock["models"] if m["id"] == "tts_kokoro")
    names = {f["path"] for f in entry["files"]}
    from vr_gateway.config import ApiConfig

    api = ApiConfig()
    assert entry["local_dir"] == "models/kokoro" and {api.kokoro_model.name, api.kokoro_voices.name} == names


async def test_cli_not_ready_after_failures_then_retries(fake_path, monkeypatch):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    for _ in range(3):
        with pytest.raises(LlmError):
            _ = [d async for d in llm.stream_chat([{"role": "user", "content": "FAIL"}])]
    assert await llm.health() is False
    import vr_gateway.adapters.llm_cli as mod

    monkeypatch.setattr(mod, "RETRY_S", 0.0)
    assert await llm.health() is True  # after the cool-down requests may try again
    assert "".join([d async for d in llm.stream_chat([{"role": "user", "content": "hi"}])]) == "Sure! What size?"
    assert llm._failures == 0


async def test_kokoro_skips_a_sentence_cancelled_while_waiting(tmp_path):
    engine = FakeKokoro()
    tts = kokoro(tmp_path, engine)
    assert await tts.ready()
    calls = len(engine.calls)
    stream = tts.stream_session()
    async with tts._lock:  # another learner's sentence holds the model
        task = asyncio.ensure_future(anext(stream.synthesize("r9", "kokoro_heart_f", "Wait for me.").__aiter__()))
        await asyncio.sleep(0.05)
        await stream.cancel("r9")
    assert await asyncio.wait_for(task, 5) == ("cancelled", None)
    assert len(engine.calls) == calls  # nothing was synthesized


class FakeMessages:
    def __init__(self, text):
        self.text, self.params = text, None

    async def create(self, **params):
        self.params = params
        block = type("B", (), {"type": "text", "text": self.text})()
        return type("R", (), {"stop_reason": "end_turn", "content": [block]})()


class FakeAnthropic:
    def __init__(self, text):
        self.messages = FakeMessages(text)


async def test_anthropic_json_sends_a_supported_schema_and_checks_the_original():
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    schema = {"type": "object", "properties": {"ko": {"type": "string", "minLength": 1, "maxLength": 5}},
              "required": ["ko"], "additionalProperties": False}
    client = FakeAnthropic('{"ko": "안녕"}')
    llm = AnthropicLlm("k" * 30, client=client)
    assert await llm.json_chat([{"role": "user", "content": "x"}], schema) == {"ko": "안녕"}
    sent = client.messages.params["output_config"]["format"]["schema"]
    assert "maxLength" not in sent["properties"]["ko"] and "minLength" not in sent["properties"]["ko"]
    assert client.messages.params["thinking"] == {"type": "disabled"}
    with pytest.raises(LlmError):  # the original constraint still applies to the answer
        await AnthropicLlm("k" * 30, client=FakeAnthropic('{"ko": "너무 긴 글입니다"}')).json_chat([], schema)


async def test_cli_cancel_is_not_a_failure_and_long_replies_are_cut(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    for _ in range(3):  # a task cancelled before any text (a recorded-practice job stopped by the learner)
        task = asyncio.ensure_future(anext(llm.stream_chat([{"role": "user", "content": "SLOW"}]).__aiter__()))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert llm._failures == 0 and await llm.health()
    # max_tokens=1 -> about 5 characters, then the stream ends (the process is killed)
    assert "".join([d async for d in llm.stream_chat([{"role": "user", "content": "hi"}], max_tokens=1)]) == "Sure!"


class SlowKokoro(FakeKokoro):
    def __init__(self):
        super().__init__()
        import threading

        self.busy = threading.Event()
        self.release = threading.Event()

    def create(self, text, voice, speed, lang):
        if text != "Hello.":
            self.busy.set()
            self.release.wait(5)
        return super().create(text, voice, speed, lang)


async def test_kokoro_keeps_the_model_locked_until_a_cancelled_sentence_finishes(tmp_path):
    engine = SlowKokoro()
    tts = kokoro(tmp_path, engine)
    assert await tts.ready()
    task = asyncio.ensure_future(tts.synthesize_wav("kokoro_heart_f", "A long sentence."))
    await asyncio.to_thread(engine.busy.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    assert tts._lock.locked()  # the thread is still synthesizing: nobody else may start
    engine.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not tts._lock.locked()


class SlowStream:
    """messages.stream() whose response headers never arrive."""

    def __init__(self):
        self.closed = asyncio.Event()

    async def __aenter__(self):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.closed.set()
            raise

    async def __aexit__(self, *exc):
        return False


async def test_anthropic_cancel_while_waiting_for_headers():
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    slow = SlowStream()
    client = type("C", (), {"messages": type("M", (), {"stream": lambda self, **kw: slow})()})()
    llm = AnthropicLlm("k" * 30, client=client)
    cancel = asyncio.Event()
    task = asyncio.ensure_future(anext(llm.stream_chat([{"role": "user", "content": "hi"}], cancel=cancel).__aiter__(),
                                       None))
    await asyncio.sleep(0.1)
    cancel.set()
    assert await asyncio.wait_for(task, 5) is None
    assert slow.closed.is_set()  # the pending request was stopped


async def test_cli_outcomes_and_cooling(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    llm._failures = 2
    cancel = asyncio.Event()
    stream = llm.stream_chat([{"role": "user", "content": "SLOW"}], cancel=cancel)
    task = asyncio.ensure_future(anext(stream.__aiter__(), None))
    await asyncio.sleep(0.3)
    cancel.set()
    await asyncio.wait_for(task, 5)
    assert llm._failures == 2  # a barge-in cancel is neither success nor failure
    with pytest.raises(LlmError):
        _ = [d async for d in llm.stream_chat([{"role": "user", "content": "FAIL"}])]
    assert llm._failures == 3 and llm.cooling()
    # while cooling, requests of sessions already open do not start a CLI either
    with pytest.raises(LlmError) as err:
        _ = [d async for d in llm.stream_chat([{"role": "user", "content": "hi"}])]
    assert err.value.code == "unreachable"


def test_cli_environment_drops_codex_key(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "x")
    assert "CODEX_API_KEY" not in CodexCliLlm("m")._env()


async def test_cli_json_over_budget_is_truncated(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    with pytest.raises(LlmError) as err:  # '{"ok": true}' is 12 characters, the budget 5
        await llm.json_chat([{"role": "user", "content": "x"}], {"type": "object"}, max_tokens=1)
    assert err.value.code == "truncated"


def test_anthropic_thinking_off_only_on_haiku():
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    msgs = [{"role": "user", "content": "hi"}]
    haiku = AnthropicLlm("k" * 30, "claude-haiku-5-5", client=object())._params(msgs, 128)
    assert haiku["thinking"] == {"type": "disabled"}
    assert "thinking" not in AnthropicLlm("k" * 30, "claude-sonnet-5-5", client=object())._params(msgs, 128)


async def test_codex_exit_without_turn_completed_fails(fake_path):
    fake_bin(fake_path, "codex", FAKE_CODEX.replace('print(json.dumps({"type": "turn.completed"}), flush=True)\n', ""))
    llm = CodexCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    with pytest.raises(LlmError):
        _ = [d async for d in llm.stream_chat([{"role": "user", "content": "hi"}])]
    assert llm._failures == 1


def test_cli_environment_drops_endpoint_overrides(monkeypatch):
    for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "OPENAI_BASE_URL"):
        monkeypatch.setenv(name, "1")
    for env in (ClaudeCliLlm("m")._env(), CodexCliLlm("m")._env()):
        assert not {"ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "OPENAI_BASE_URL"} & env.keys()


def test_cli_environment_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_USE_ANTHROPIC_AWS", "1")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
    monkeypatch.setenv("OTEL_LOG_USER_PROMPTS", "1")
    monkeypatch.setenv("HOME", "/home/x")
    env = ClaudeCliLlm("m")._env()
    assert env["HOME"] == "/home/x" and "PATH" in env
    assert not {"CLAUDE_CODE_USE_ANTHROPIC_AWS", "OTEL_LOGS_EXPORTER", "OTEL_LOG_USER_PROMPTS"} & env.keys()


async def test_cli_cancelled_before_send_never_writes(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    cancel = asyncio.Event()
    cancel.set()
    assert [d async for d in llm.stream_chat([{"role": "user", "content": "hi"}], cancel=cancel)] == []
    assert llm._failures == 0


def test_anthropic_client_pins_the_official_endpoint(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    assert str(AnthropicLlm("k" * 30)._client.base_url).startswith("https://api.anthropic.com")


def test_anthropic_effort_only_where_accepted():
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    msgs = [{"role": "user", "content": "hi"}]
    assert AnthropicLlm("k" * 30, "claude-haiku-5-5", client=object())._params(msgs, 128)["output_config"] == {"effort": "low"}
    old = AnthropicLlm("k" * 30, "claude-haiku-4-5", client=object())._params(msgs, 128)
    assert "output_config" not in old and old["thinking"] == {"type": "disabled"}


async def test_anthropic_client_ignores_env_tracing_and_headers(monkeypatch):
    import httpx2

    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer other\nX-Extra: 1")
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    from vr_gateway.adapters.llm_anthropic import AnthropicLlm

    seen = {}

    def handler(req):
        seen.update(req.headers)
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "x"}})

    key = "sk-ant-" + "k" * 30
    llm = AnthropicLlm(key)
    llm._client._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    with pytest.raises(LlmError):
        await llm.json_chat([{"role": "user", "content": "hi"}], {"type": "object"})
    assert seen["authorization"] == f"Bearer {key}" and "x-extra" not in seen
    assert llm._client._open_telemetry["traces"]["enabled"] is False
    assert os.environ["ANTHROPIC_CUSTOM_HEADERS"].startswith("Authorization")  # put back after the client is built


async def test_cli_spares_are_let_go_without_another_request(fake_path):
    fake_bin(fake_path, "claude", FAKE_CLAUDE)
    llm = ClaudeCliLlm("m", pool=1, workdir=fake_path / "work", timeout_s=10, idle_s=0.5)
    try:
        await llm.warm()
        for _ in range(100):
            if llm._idle:
                break
            await asyncio.sleep(0.05)
        assert llm._idle
        await asyncio.sleep(1.0)
        assert llm._idle == []
    finally:
        await llm.aclose()


async def test_cli_cancel_while_writing_the_prompt(fake_path):
    fake_bin(fake_path, "claude", "import time\ntime.sleep(30)\n")  # never reads stdin
    llm = ClaudeCliLlm("m", pool=0, workdir=fake_path / "work", timeout_s=10)
    cancel = asyncio.Event()
    big = [{"role": "user", "content": "x" * 2_000_000}]
    task = asyncio.ensure_future(anext(llm.stream_chat(big, cancel=cancel).__aiter__(), None))
    await asyncio.sleep(0.5)
    cancel.set()
    assert await asyncio.wait_for(task, 5) is None
    assert llm._failures == 0
