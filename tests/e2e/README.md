# Browser E2E tests and full-stack benchmark

Real Chromium (Playwright) against the real stack (`./app start`: gateway, Qwen3-ASR, CosyVoice3, llama-server). The app
runs unmodified: its capture AudioWorklet (downmix, sinc resampling to 16 kHz, 20 ms frames), WebSocket envelope,
gateway VAD/turn taking, ASR, LLM, TTS and playback AudioWorklet are all exercised.

**FAKE-MIC-SOURCE.** The only substitute is the microphone *source*: `lib/fakeMic.js` (a page init script) makes
`getUserMedia({audio})` return a MediaStream from a separate 48 kHz AudioContext, and the test schedules fixture WAVs
into it (`tests/fixtures/audio/e2e_*.wav`, macOS `say`, regenerate with `tests/fixtures/make_fixtures.sh`). The same
script records, on the browser's `performance.now()` clock, the fixture timing, the app's playback worklet messages
(`started/completed/stopped/flushed`), capture onsets and every WebSocket event and audio-frame header.
`smoke_device_capture.spec.ts` skips the override and uses Chromium's `--use-file-for-fake-audio-capture` device instead.
Chromium runs headless with `--mute-audio` (rendering continues, nothing reaches the speakers).

## Run

```sh
./app start                                   # real models; wait for "Ready"
cd tests/e2e && npm ci && npx playwright install chromium   # once (setup-time network)
npx playwright test                           # all specs, serially (one realtime session at a time)
npx playwright test -c docs.config.ts         # regenerate docs/screenshots/*.png
../../benchmarks/run.sh --turns 30            # full-stack benchmark (./app benchmark), report in benchmarks/results/
```

| Spec | PRD case | What is checked |
|---|---|---|
| `at01_realtime` | AT-01 | cafe scenario via 학습 홈: opening line plays, 3 turns, partial then final captions, replies ≤ 60 words that actually play, `goal.update` with a done goal shown in the UI |
| `at02_03_bargein` | AT-02, AT-03 | barge-in into the opening line and into a streamed reply: local stop (worklet `flushed`), first syllable kept, new reply plays, nothing of the old reply plays again; stale frames/text re-injected after a cancel (via `routeWebSocket`) never reach the worklet or the captions, while a control frame does |
| `at05_06_turns` | AT-05, AT-06 | 0.6 s pause inside a sentence stays one turn at normal difficulty; 말하기 완료 ends the turn before the silence rule; 30 s of -60 dBFS noise: no turn, no LLM request (llama-server log), no gateway turn/goal/summary log lines |
| `at07_09_recorded` | AT-07, AT-08, AT-09, AT-18 | reading: record → preview → re-record → only the second take is analysed; double-clicked submit → one job, same `Idempotency-Key` → 200 same job, other key → 409; transcript edit → revision 2, original kept, feedback on revision 2; no pronunciation score in API or page; resent `input.commit` → same `asr.final`, one reply |
| `at11_12_20_api` | AT-11, AT-12, AT-20 | garbage / 8-bit / 22.05 kHz / truncated / 120.5 s / 33 MiB rejected with the documented codes, exactly 120 s accepted, no temp file open in any stack process, gateway RSS bounded; 16 kHz, 44.1 kHz stereo and 48 kHz give the same 16 kHz length, transcript and voiced span; missing cookie, foreign Origin/Host, missing CSRF, oversize JSON, bad WebSocket upgrades, oversize/malformed frames rejected, no paths in error bodies, conversation continues |
| `at15_16_21_privacy` | AT-15, AT-16, AT-21 | `lsof` every 0.5 s on gateway + workers (incl. the pronunciation worker when running) + children during realtime, hint, recorded, re-analysis, TTS and review calls: no non-loopback socket; page requests stay on 127.0.0.1; a unique phrase spoken (realtime, recorded), typed (edit) and synthesized never appears in `var/` (logs, SQLite, caches) or new `/tmp`/`$TMPDIR` files, and no audio file is written except reviewed-text cache assets; a spoken injection attempt gets an in-role English reply without prompt disclosure, settings and voices unchanged |
| `smoke_device_capture` | — | the browser's own capture device path end to end |
| `co5_summary_drill` | CO-5 | realtime cafe turn with grammar errors ("Yesterday I go to the cafe and I buyed a coffee.") → session summary has an improvement with a suggestion → 다시 말하기: the suggestion is the drill target, a retry take is recorded through the app's recorder and submitted (one `drill` attempt with that target) → the inline result shows the first sentence, the recognised sentence and the word comparison; no score in API or page. Its two fixtures are made on demand with `say` into `.out/fixtures/` (`makeFixture` in `lib/harness.ts`); screenshot `.out/co5-summary-drill.png` |
| `pronunciation` | AT-23 (PA-1/PA-4/PA-5) | reading take → `pronunciation.status: "timing_only"` with the target's words, all bands null, no GOP/probability fields in the result; the result page's word row plays my word slice (browser copy of the take) and, when TTS is ready, the model voice's same word (both slice lengths checked); contour labelled `참고 지표 · 점수 아님` with legend and table view; no band markup, experimental label or score-like numbers on the page. Skipped when the optional pronunciation worker is not running; with TTS down it checks that the model half is disabled with a reason (annotated `degraded`). |

Not covered here: AT-04 (speaker echo needs a real speaker→mic loop), AT-10 restart/expiry, AT-13 (TTS listening
check), AT-14 is the benchmark, AT-17, AT-19 fault injection, AT-22 long sessions, stop-button latency.

Notes on what the tests observed:
- With `routeWebSocket`, the page's socket object belongs to Playwright, so injected messages are only visible to the
  app (worklet, DOM), not to the init script's WebSocket log — AT-03 therefore asserts on the worklet and DOM and
  uses a control frame to prove delivery.
- Refused WebSocket upgrades arrive as HTTP 403 handshakes (uvicorn), not as 4401/4403/4404 close codes (PROTOCOL §6.3).
- onnxruntime opens an empty `$TMPDIR/mat-debug-<pid>.log` when imported on macOS (gateway, TTS worker); AT-11 allows it
  only while empty.
