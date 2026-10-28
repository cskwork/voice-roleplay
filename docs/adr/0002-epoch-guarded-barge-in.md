# 0002. Barge-in through an epoch counter, a single send lock, and history of what was actually heard

Status: accepted (commits ecd86a2 and dffa0b9, 2026-09-27 to 2026-09-29)

## Context

The AI reply is produced by three concurrent streams: LLM tokens, TTS audio per segment, and browser
playback. When the learner talks over the AI, all three must stop. No old audio may play after the stop,
even if it was already in flight. The conversation history must also not contain sentences the learner never
heard; otherwise hints, goal checks and the next reply refer to things that were never said.

## Decision

- Every outgoing event and audio frame carries the session `epoch`. `_cancel_response` in
  `services/gateway/src/vr_gateway/realtime.py` takes the send lock, marks the response cancelled, increments
  the epoch and sends `response.cancelled`. After that it sets the cancel event, which closes the llama-server
  stream, and cancels the TTS request. `_send_locked` and `_send_audio` drop anything for a cancelled response
  or an older epoch while holding the same lock. So once `response.cancelled` is sent, nothing from that
  response can follow it.
- The browser stops playback by itself on its own energy detector (`apps/web/src/lib/levelVad.ts`), without
  waiting for the server. `PlaybackQueue` (`apps/web/src/lib/playbackQueue.ts`) discards frames from an older
  epoch or a cancelled response.
- Each reply is kept as numbered segments with a status: pending, sent, played, interrupted or text_only.
  `_fold` writes only played and text_only segments into history, once playback has ended (commit dffa0b9:
  "AI lines enter history on playback completion").

## Consequences

- In the 200-turn benchmark, the AI stopped within p95 196 ms of speech start across 66 barge-ins (target
  350 ms). Stale audio played after a stop: 0.
- Tests cover it without models: `test_barge_in_cancels_and_no_stale_frames` in `services/gateway/tests/test_realtime.py` checks that no frame of the cancelled reply follows `response.cancelled`.
  The Playwright spec `tests/e2e/specs/at02_03_bargein.spec.ts` replays stale frames through `routeWebSocket`
  on the real stack.
- Speaker echo can look like a barge-in. An overlap check (`ECHO_OVERLAP = 0.6`) discards such turns, and
  after 2 of them automatic barge-in turns off and push-to-talk is suggested. A headset is still recommended.
- The engine has to track per-segment state and the browser's playback reports, which makes it the largest
  module (`realtime.py`, 1,078 lines).

## Alternatives considered

The repository history does not record these as tried. They are the obvious simpler designs, listed for contrast.


- **Cancel without an epoch, filtering by response id only:** this does not cover events sent without a
  response (state changes) and leaves a race between a check and a send unless the same lock is held. The
  epoch plus the lock closes that gap.
- **Write the full generated reply to history:** simpler, but the model would "remember" lines the learner
  never heard.
- **Rely on the server VAD alone for stopping audio:** VAD needs 200 ms of voiced audio before it announces
  speech, so the local stop in the browser keeps the cut-off well under the 350 ms target.
