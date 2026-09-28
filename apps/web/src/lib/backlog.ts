/** PRD §10.2: warn when more than 2 s of microphone audio is waiting in the WebSocket send buffer. */
export const BACKLOG_WARN_MS = 2000

/**
 * Milliseconds of audio queued in `bufferedAmount`, given the size and duration of the frames being sent.
 * Frames carry a JSON header, so bytes are converted per frame rather than per PCM sample.
 */
export function queuedAudioMs(bufferedAmount: number, frameBytes: number, frameMs: number): number {
  return frameBytes > 0 ? (bufferedAmount / frameBytes) * frameMs : 0
}
