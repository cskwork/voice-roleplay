import { describe, expect, it } from 'vitest'
import type { OutputAudioHeader } from '../src/lib/envelope'
import { PlaybackController, type PlaybackSink } from '../src/lib/playbackQueue'

/** FAKE sink (test double for the playback AudioWorklet): records what would be played. */
class FakeSink implements PlaybackSink {
  calls: string[] = []
  samples = 0
  push(r: string, s: string, x: Float32Array) {
    this.calls.push(`push ${r}/${s}`)
    this.samples += x.length
  }
  end(r: string, s: string) {
    this.calls.push(`end ${r}/${s}`)
  }
  flush() {
    this.calls.push('flush')
  }
}

const frame = (response_id: string, epoch: number, segment_id: number, n = 480, sample_rate = 24000): [OutputAudioHeader, Int16Array] => [
  { v: 1, kind: 'output_audio', session_id: 's', response_id, epoch, seq: 0, sample_rate, sample_count: n, segment_id },
  new Int16Array(n).fill(1000),
]

describe('PlaybackController', () => {
  it('plays frames of the current epoch and closes segments in order', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    expect(pc.handleAudio(...frame('r1', 1, 0))).toBe(true)
    expect(pc.handleAudio(...frame('r1', 1, 0))).toBe(true)
    expect(pc.handleAudio(...frame('r1', 1, 1))).toBe(true)
    pc.endResponse('r1')
    expect(sink.calls).toEqual(['push r1/0', 'push r1/0', 'end r1/0', 'push r1/1', 'end r1/1'])
    expect(sink.samples).toBe(3 * 480)
  })

  it('drops frames from an older epoch', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    pc.observeEpoch(5)
    expect(pc.handleAudio(...frame('old', 4, 0))).toBe(false)
    expect(sink.calls).toEqual([])
  })

  it('drops late frames and text of a cancelled response (AT-03)', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    pc.handleAudio(...frame('r1', 1, 0))
    expect(pc.cancel('r1')).toBe(true)
    expect(sink.calls).toEqual(['push r1/0', 'flush'])
    expect(pc.handleAudio(...frame('r1', 1, 0))).toBe(false)
    expect(pc.handleAudio(...frame('r1', 1, 1))).toBe(false)
    expect(pc.isStale('r1', 1)).toBe(true)
    expect(sink.calls).toEqual(['push r1/0', 'flush'])
  })

  it('local stop flushes immediately and a later server confirmation does not cut newer audio', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    pc.handleAudio(...frame('r1', 1, 0))
    pc.stopNow('r1')
    expect(sink.calls.at(-1)).toBe('flush')
    pc.observeEpoch(2) // server bumped epoch on cancel
    pc.handleAudio(...frame('r2', 2, 0))
    const before = sink.calls.length
    expect(pc.cancel('r1')).toBe(false) // response.cancelled for r1 arrives late
    expect(sink.calls.length).toBe(before) // r2 keeps playing
    expect(pc.handleAudio(...frame('r1', 1, 1))).toBe(false)
  })

  it('stopNow cancels the response even before any audio arrived', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    pc.stopNow('r9')
    expect(pc.handleAudio(...frame('r9', 0, 0))).toBe(false)
  })

  it('an epoch bump stops audio still queued from the previous generation', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 24000)
    pc.handleAudio(...frame('r1', 1, 0))
    pc.observeEpoch(2)
    expect(sink.calls).toEqual(['push r1/0', 'flush'])
    expect(pc.handleAudio(...frame('r1', 1, 0))).toBe(false)
  })

  it('resamples TTS audio to the output rate', () => {
    const sink = new FakeSink()
    const pc = new PlaybackController(sink, 48000)
    for (let i = 0; i < 10; i++) pc.handleAudio(...frame('r1', 0, 0, 2400)) // 10 x 100 ms at 24 kHz
    pc.endResponse('r1')
    expect(Math.abs(sink.samples - 48000)).toBeLessThanOrEqual(2)
  })
})
