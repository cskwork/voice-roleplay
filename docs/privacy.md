# Privacy

- Every server binds to `127.0.0.1`. There is no LAN or cloud mode.
- Microphone audio is never written to disk or the database. Recorded-practice audio is freed from memory when the job ends, fails or is cancelled. The per-word "my take" playback uses the recording kept in browser memory, which is dropped after 5 minutes or when you close the page.
- Transcripts and feedback live in session memory unless you turn on history; unsaved summaries disappear after 15 minutes. With history on they go to `var/data/app.sqlite3`, and you can delete everything from settings.
- Logs hold event names, ids, durations, states and error codes. Transcripts, TTS text, prompts, LLM output and audio are never logged.
- No telemetry, CDN or model downloads at runtime. Workers start with `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.
- macOS can still leave traces in swap or crash dumps even though the app writes nothing. Use disk encryption if that matters to you.

The Playwright spec `tests/e2e/specs/at15_16_21_privacy.spec.ts` checks these on the real stack: no
non-loopback sockets in any process, and a unique spoken phrase never appears under `var/` or in new temp files.
