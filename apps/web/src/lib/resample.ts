/**
 * Streaming windowed-sinc resampler (Kaiser window). The kernel's cutoff sits below the
 * lower of the two Nyquist frequencies, so it low-passes before decimation (no aliasing)
 * and suppresses images when upsampling. Arbitrary rate ratios (e.g. 44.1 kHz -> 16 kHz).
 * Pure TS so it runs in AudioWorklets and in unit tests.
 */
const ZERO_CROSSINGS = 16
const TABLE_RES = 128 // kernel samples per input sample
const KAISER_BETA = 8.6

function besselI0(x: number): number {
  let sum = 1
  let term = 1
  const q = (x * x) / 4
  for (let k = 1; k < 50; k++) {
    term *= q / (k * k)
    sum += term
    if (term < sum * 1e-12) break
  }
  return sum
}

export class StreamingResampler {
  readonly ratio: number // output samples per input sample
  private readonly halfWidth: number // kernel half width, in input samples
  private readonly table: Float32Array
  private buf: Float32Array
  private len: number
  private t: number // input position (relative to buf[0]) of the next output sample

  constructor(readonly inRate: number, readonly outRate: number) {
    this.ratio = outRate / inRate
    const cutoff = 0.92 * Math.min(1, this.ratio) // fraction of the input Nyquist
    this.halfWidth = Math.ceil(ZERO_CROSSINGS / cutoff)
    const n = this.halfWidth * TABLE_RES + 2
    this.table = new Float32Array(n)
    const i0b = besselI0(KAISER_BETA)
    for (let i = 0; i < n; i++) {
      const x = i / TABLE_RES
      const r = x / this.halfWidth
      if (r >= 1) continue
      const sinc = x === 0 ? 1 : Math.sin(Math.PI * cutoff * x) / (Math.PI * cutoff * x)
      this.table[i] = cutoff * sinc * (besselI0(KAISER_BETA * Math.sqrt(1 - r * r)) / i0b)
    }
    // Leading zeros stand in for the signal before t=0.
    this.buf = new Float32Array(4096)
    this.len = this.halfWidth
    this.t = this.halfWidth
  }

  /** Number of input samples of delay the filter introduces at the end of the stream. */
  get tailSamples(): number {
    return this.halfWidth + 1
  }

  process(input: Float32Array): Float32Array {
    this.append(input)
    const step = 1 / this.ratio
    const hw = this.halfWidth
    const out = new Float32Array(Math.max(0, Math.ceil((this.len - this.t) * this.ratio) + 1))
    let n = 0
    // The kernel reads buf[floor(t) - hw + 1 .. floor(t) + hw].
    while (Math.floor(this.t) + hw <= this.len - 1) {
      out[n++] = this.sampleAt(this.t)
      this.t += step
    }
    // Drop consumed history, keeping what the kernel still needs.
    const keepFrom = Math.max(0, Math.floor(this.t) - hw)
    if (keepFrom > 0) {
      this.buf.copyWithin(0, keepFrom, this.len)
      this.len -= keepFrom
      this.t -= keepFrom
    }
    return out.subarray(0, n)
  }

  /** Emits the remaining samples held back by the filter delay. */
  flush(): Float32Array {
    return this.process(new Float32Array(this.tailSamples))
  }

  private append(input: Float32Array): void {
    if (this.len + input.length > this.buf.length) {
      const next = new Float32Array(Math.max(this.buf.length * 2, this.len + input.length))
      next.set(this.buf.subarray(0, this.len))
      this.buf = next
    }
    this.buf.set(input, this.len)
    this.len += input.length
  }

  private sampleAt(t: number): number {
    const hw = this.halfWidth
    const base = Math.floor(t)
    let acc = 0
    for (let k = base - hw + 1; k <= base + hw; k++) {
      const d = Math.abs(t - k)
      if (d >= hw) continue
      const pos = d * TABLE_RES
      const i = pos | 0
      const w = pos - i
      const h = this.table[i]! + (this.table[i + 1]! - this.table[i]!) * w
      acc += this.buf[k]! * h
    }
    return acc
  }
}

/** One-shot convenience for whole buffers. */
export function resample(input: Float32Array, inRate: number, outRate: number): Float32Array {
  if (inRate === outRate) return input.slice()
  const r = new StreamingResampler(inRate, outRate)
  const a = r.process(input)
  const b = r.flush()
  const expected = Math.round(input.length * r.ratio)
  const out = new Float32Array(expected)
  out.set(a.subarray(0, Math.min(a.length, expected)))
  if (a.length < expected) out.set(b.subarray(0, expected - a.length), a.length)
  return out
}
