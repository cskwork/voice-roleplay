/**
 * Streaming WSOLA time stretch for speech. Raising a voice without changing its tempo = stretch by `factor` here, then
 * play the result `factor` times faster (the caller labels the audio with `rate * factor`, the playback resampler does the rest).
 */
export class Wsola {
  private readonly half: number // synthesis hop = half a frame
  private readonly search: number
  private readonly hop: number // analysis hop
  private readonly fade: Float32Array
  private buf = new Float32Array(16384)
  private len = 0
  private base = 0 // absolute index of buf[0]
  private k = 0
  private prev = -1 // absolute start of the previous frame, -1 before the first
  private tail: Float32Array

  constructor(rate: number, factor: number) {
    this.half = Math.round(rate * 0.015)
    this.search = Math.round(rate * 0.006)
    this.hop = this.half / factor
    this.tail = new Float32Array(this.half)
    this.fade = Float32Array.from({ length: this.half }, (_, i) => 0.5 - 0.5 * Math.cos((Math.PI * (i + 0.5)) / this.half))
  }

  push(x: Float32Array): Float32Array {
    this.append(x)
    const out: Float32Array[] = []
    for (;;) {
      const target = Math.round(this.k * this.hop)
      if (target + this.search + 2 * this.half > this.base + this.len) break
      const start = this.prev < 0 ? 0 : this.best(target)
      out.push(this.emit(start))
      this.prev = start
      this.k += 1
      this.trim()
    }
    return concat(out)
  }

  /** What is still held back at the end of the sound. */
  flush(): Float32Array {
    const rest = this.push(new Float32Array(this.search + 2 * this.half))
    return concat([rest, this.tail])
  }

  /** Cross-fades the previous frame's second half into this frame's first half. */
  private emit(start: number): Float32Array {
    const o = start - this.base
    const out = new Float32Array(this.half)
    for (let i = 0; i < this.half; i++) {
      const w = this.fade[i]!
      out[i] = this.prev < 0 ? this.buf[o + i]! : this.tail[i]! * (1 - w) + this.buf[o + i]! * w
    }
    this.tail.set(this.buf.subarray(o + this.half, o + 2 * this.half))
    return out
  }

  /** The frame near `target` that continues the previous one best (normalised cross-correlation). */
  private best(target: number): number {
    let at = target
    let top = -Infinity
    for (let s = Math.max(this.base, target - this.search); s <= target + this.search; s += 2) {
      const o = s - this.base
      let dot = 0
      let energy = 1e-6
      for (let i = 0; i < this.half; i += 2) {
        const v = this.buf[o + i]!
        dot += v * this.tail[i]!
        energy += v * v
      }
      const score = dot / Math.sqrt(energy)
      if (score > top) {
        top = score
        at = s
      }
    }
    return at
  }

  private append(x: Float32Array): void {
    if (this.len + x.length > this.buf.length) {
      const next = new Float32Array(Math.max(this.buf.length * 2, this.len + x.length))
      next.set(this.buf.subarray(0, this.len))
      this.buf = next
    }
    this.buf.set(x, this.len)
    this.len += x.length
  }

  private trim(): void {
    const keep = Math.min(this.prev, Math.round(this.k * this.hop) - this.search) - this.base
    if (keep < 4096) return
    this.buf.copyWithin(0, keep, this.len)
    this.len -= keep
    this.base += keep
  }
}

export function concat(parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0))
  let o = 0
  for (const p of parts) {
    out.set(p, o)
    o += p.length
  }
  return out
}
