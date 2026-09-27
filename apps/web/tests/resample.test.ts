import { describe, expect, it } from 'vitest'
import { resample, StreamingResampler } from '../src/lib/resample'

const sine = (freq: number, rate: number, seconds: number, amp = 0.5) =>
  Float32Array.from({ length: Math.round(rate * seconds) }, (_, i) => amp * Math.sin((2 * Math.PI * freq * i) / rate))

/** Amplitude of one frequency component (Goertzel), normalised so a pure sine of amplitude A gives ~A. */
function toneAmplitude(x: Float32Array, freq: number, rate: number): number {
  const w = (2 * Math.PI * freq) / rate
  let re = 0
  let im = 0
  for (let i = 0; i < x.length; i++) {
    re += x[i]! * Math.cos(w * i)
    im -= x[i]! * Math.sin(w * i)
  }
  return (2 * Math.hypot(re, im)) / x.length
}

/** Dominant frequency from zero crossings (skipping filter warm-up edges). */
function zeroCrossingFreq(x: Float32Array, rate: number): number {
  const s = x.subarray(200, x.length - 200)
  let crossings = 0
  for (let i = 1; i < s.length; i++) if (s[i - 1]! < 0 !== s[i]! < 0) crossings++
  return (crossings / 2) * (rate / s.length)
}

describe('StreamingResampler', () => {
  it.each([
    [48000, 16000],
    [44100, 16000],
    [22050, 16000],
    [24000, 48000],
    [24000, 44100],
  ])('produces the expected length for %i -> %i Hz', (inRate, outRate) => {
    const x = sine(440, inRate, 1.0)
    expect(resample(x, inRate, outRate).length).toBe(outRate)
    // Streaming in 128-sample render quanta yields the same count (within the filter delay).
    const r = new StreamingResampler(inRate, outRate)
    let n = 0
    for (let i = 0; i < x.length; i += 128) n += r.process(x.subarray(i, i + 128)).length
    n += r.flush().length
    expect(Math.abs(n - outRate)).toBeLessThanOrEqual(2)
  })

  it('keeps pitch when downsampling 48k/44.1k -> 16k and upsampling 24k -> 48k', () => {
    for (const [inRate, outRate] of [
      [48000, 16000],
      [44100, 16000],
      [24000, 48000],
    ] as const) {
      const y = resample(sine(1000, inRate, 0.5), inRate, outRate)
      expect(zeroCrossingFreq(y, outRate)).toBeCloseTo(1000, -1) // ±5 Hz
      expect(toneAmplitude(y.subarray(400, y.length - 400), 1000, outRate)).toBeCloseTo(0.5, 1)
    }
  })

  it('low-passes before decimation: a 10 kHz tone does not alias into 16 kHz output', () => {
    const y = resample(sine(10000, 48000, 0.5), 48000, 16000)
    // Without filtering it would fold to 6 kHz at full amplitude.
    const aliased = toneAmplitude(y.subarray(400, y.length - 400), 6000, 16000)
    expect(20 * Math.log10(aliased / 0.5)).toBeLessThan(-60)
  })

  it('streaming in chunks matches whole-buffer processing sample for sample', () => {
    const x = sine(700, 44100, 0.3)
    const whole = new StreamingResampler(44100, 16000)
    const a = [...whole.process(x), ...whole.flush()]
    const chunked = new StreamingResampler(44100, 16000)
    const b: number[] = []
    for (let i = 0; i < x.length; i += 97) b.push(...chunked.process(x.subarray(i, i + 97)))
    b.push(...chunked.flush())
    expect(b.length).toBe(a.length)
    for (let i = 0; i < a.length; i++) expect(b[i]).toBeCloseTo(a[i]!, 5)
  })
})
