import { describe, expect, it } from 'vitest'
import { BACKLOG_WARN_MS, queuedAudioMs } from '../src/lib/backlog'

describe('send backlog', () => {
  it('counts whole framed 20 ms chunks, header included', () => {
    const frameBytes = 4 + 180 + 640 // length prefix + JSON header + 320 samples
    expect(queuedAudioMs(frameBytes * 100, frameBytes, 20)).toBe(2000)
    expect(queuedAudioMs(frameBytes * 101, frameBytes, 20)).toBeGreaterThan(BACKLOG_WARN_MS)
    // 64 000 raw bytes is only ~1.55 s of framed audio: counting PCM bytes alone would warn too early.
    expect(queuedAudioMs(64000, frameBytes, 20)).toBeLessThan(BACKLOG_WARN_MS)
  })

  it('is zero before any frame was sent', () => {
    expect(queuedAudioMs(5000, 0, 20)).toBe(0)
  })
})
