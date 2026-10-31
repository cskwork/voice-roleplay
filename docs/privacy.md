# Privacy

- Every server binds to `127.0.0.1`. The default (local) mode has no LAN or cloud path; API mode, below, does.
- Microphone audio is never written to disk or the database. Recorded-practice audio is freed from memory when the job ends, fails or is cancelled. The per-word "my take" playback uses the recording kept in browser memory, which is dropped after 5 minutes or when you close the page.
- Transcripts and feedback live in session memory unless you turn on history; unsaved summaries disappear after 15 minutes. With history on they go to `var/data/app.sqlite3`, and you can delete everything from settings.
- Logs hold event names, ids, durations, states and error codes. Transcripts, TTS text, prompts, LLM output and audio are never logged.
- No telemetry, CDN or model downloads at runtime. Workers start with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
- macOS can still leave traces in swap or crash dumps even though the app writes nothing. Use disk encryption if that matters to you.

The Playwright spec `tests/e2e/specs/at15_16_21_privacy.spec.ts` checks these on the real stack: no
non-loopback sockets in any process, and a unique spoken phrase never appears under `var/` or in new temp files.

## API mode

API mode (`./app start --api`, [`contracts/PROTOCOL.md`](../contracts/PROTOCOL.md) §1.1) replaces the local models with hosted
APIs, so the points above about nothing leaving the machine do not apply to it:

- Learners' microphone audio is streamed to OpenAI (`gpt-live-transcribe`) and the conversation text to OpenRouter, both in the
  US. OpenRouter passes the text on: the conversation to its OpenAI/Azure upstreams, and the AI's reply text to Deepgram
  (Aura-2, `deepgram/aura-2`) for the voice.
- Requests ask for the least retention each provider offers: every OpenRouter request (conversation and voice) carries
  `provider.zdr: true` and `data_collection: "deny"`. OpenAI's realtime endpoint keeps abuse-monitoring logs for 30 days unless
  the account has approved zero data retention.
- Deepgram directly is an opt-in fallback for the voice (`VR_API_TTS_PROVIDER=deepgram`, `DEEPGRAM_API_KEY`): then the reply text
  goes to Deepgram without OpenRouter in between, with `mip_opt_out=true` on every request.
- API keys are read from environment variables only; the gateway never writes them to disk, logs them or returns them
  (`/api/health` shows only whether each is set).
- The gateway itself still writes no audio to disk and logs no transcripts, prompts or replies. Shared mode (a class on one
  gateway) stores no history or review items and keeps settings only in memory per browser.
- Pronunciation analysis stays local-only and is off in API mode.
