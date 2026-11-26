/**
 * Speech audio -> mouth shapes over time. The shapes come from the sound itself (loudness, three frequency bands, short
 * gaps), never from the text, and are computed before the audio is heard, so the mouth moves with the voice.
 */
export type MouthShape = 'rest' | 'closed' | 'small' | 'wide' | 'round' | 'oo' | 'ee' | 'fv'
export interface Mouth {
  shape: MouthShape
  level: number
}

const HOP = 0.0125
const WIN = 0.03
const MIN_HOLD = 0.06

interface Raw {
  rms: number
  low: number
  mid: number
  high: number
  air: number
}

const span = (a: number, b: number, n: number) => Array.from({ length: n }, (_, i) => a + ((b - a) * i) / (n - 1))

/** Band energy with a few Goertzel filters (cheaper than an FFT for this few bands). */
function bandEnergy(x: Float32Array, from: number, n: number, sr: number, freqs: number[], window: Float32Array): number {
  let e = 0
  for (const f of freqs) {
    const c = 2 * Math.cos((2 * Math.PI * f) / sr)
    let s1 = 0
    let s2 = 0
    for (let i = 0; i < n; i++) {
      const s0 = x[from + i]! * window[i]! + c * s1 - s2
      s2 = s1
      s1 = s0
    }
    e += s1 * s1 + s2 * s2 - c * s1 * s2
  }
  return e / freqs.length / (n * n)
}

/** Grows with the audio of one spoken line; `at(t)` answers for any time already received. */
export class MouthTrack {
  private readonly hop: number
  private readonly win: number
  private readonly window: Float32Array
  private readonly bands: Record<'low' | 'mid' | 'high' | 'air', number[]>
  private raw: Raw[] = []
  private frames: Mouth[] = []
  private pend = new Float32Array(0) // pre-emphasised samples not yet analysed
  private pendDry = new Float32Array(0)
  private lastSample = 0
  private samples = 0
  private peak: number

  /** `formant` = how much the voice was raised (the bands move with it); `peak` = loudest level heard so far in the call. */
  constructor(
    private readonly rate: number,
    formant: number,
    peak = 0,
  ) {
    this.hop = Math.round(HOP * rate)
    this.win = Math.round(WIN * rate)
    this.window = Float32Array.from({ length: this.win }, (_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (this.win - 1)))
    const band = (a: number, b: number, n: number) => span(a * formant, Math.min(b * formant, rate * 0.47), n)
    this.bands = { low: band(240, 480, 5), mid: band(620, 1100, 6), high: band(1900, 3000, 8), air: band(4200, 8000, 6) }
    this.peak = peak
  }

  get duration(): number {
    return this.samples / this.rate
  }
  get loudest(): number {
    return this.peak
  }

  push(x: Float32Array): void {
    this.samples += x.length
    const wet = new Float32Array(x.length)
    for (let i = 0; i < x.length; i++) {
      // Voices fall off towards high bands: lift them, or every vowel looks like "oo".
      wet[i] = x[i]! - 0.94 * this.lastSample
      this.lastSample = x[i]!
    }
    const dry = join(this.pendDry, x)
    const pre = join(this.pend, wet)
    let o = 0
    for (; o + this.win <= dry.length; o += this.hop) {
      let s = 0
      for (let i = o; i < o + this.win; i++) s += dry[i]! * dry[i]!
      const rms = Math.sqrt(s / this.win)
      this.peak = Math.max(this.peak, rms)
      const e = (k: keyof typeof this.bands) => bandEnergy(pre, o, this.win, this.rate, this.bands[k], this.window)
      this.raw.push({ rms, low: e('low'), mid: e('mid'), high: e('high'), air: e('air') })
    }
    this.pendDry = dry.slice(o)
    this.pend = pre.slice(o)
    this.shape()
  }

  /** Mouth at `t` seconds into the line; null once everything received has been passed. */
  at(t: number): Mouth | null {
    if (t < 0) return { shape: 'rest', level: 0 }
    if (t >= this.duration + 0.05) return null
    return this.frames[Math.floor(t / HOP)] ?? { shape: 'rest', level: 0 }
  }

  /** When each word of `text` is heard (seconds): voiced time shared out by syllables. `expected` fills in while audio is still arriving. */
  wordTimes(text: string, complete: boolean): number[] {
    const words = text.split(' ')
    const syl = words.map((w) => Math.max(1, (w.toLowerCase().match(/[aeiouy]+/g) ?? []).length) + (/[.,!?]$/.test(w) ? 0.6 : 0))
    const total = syl.reduce((a, b) => a + b, 0)
    const voiced: number[] = []
    this.frames.forEach((f, i) => {
      if (f.shape !== 'rest') voiced.push(i * HOP)
    })
    const first = voiced[0] ?? 0
    const guess = first + total * 0.21
    const span = complete || this.duration > guess ? voiced : null
    let acc = 0
    return syl.map((s) => {
      const t = span?.length ? span[Math.min(span.length - 1, Math.floor((acc / total) * span.length))]! : first + (acc / total) * (guess - first)
      acc += s
      return t
    })
  }

  private shape(): void {
    const ref = Math.max(this.peak, 0.02)
    const toks = this.raw.map((r): Mouth => {
      const level = Math.min(1, Math.pow(r.rms / ref, 0.7))
      if (r.rms < ref * 0.07) return { shape: 'rest', level: 0 }
      const sum = r.low + r.mid + r.high + 1e-12
      const [l, m, h] = [r.low / sum, r.mid / sum, r.high / sum]
      const air = r.air / (sum + r.air)
      if (air > 0.5) return { shape: level < 0.3 ? 'fv' : 'ee', level }
      if (m > 0.58) return { shape: level > 0.45 ? 'wide' : 'small', level }
      if (h > 0.2 && h > m * 0.55) return { shape: 'ee', level }
      if (m > 0.3) return { shape: level > 0.6 ? 'round' : 'small', level }
      return { shape: l > 0.8 && level < 0.75 ? 'oo' : 'round', level }
    })
    // A short dip inside speech is a closed-lips sound (m, b, p); a long one is a pause.
    for (let i = 0; i < toks.length; ) {
      if (toks[i]!.shape !== 'rest') {
        i++
        continue
      }
      let j = i
      while (j < toks.length && toks[j]!.shape === 'rest') j++
      const len = (j - i) * HOP
      if (i > 0 && j < toks.length && len >= 0.02 && len <= 0.11) for (let k = i; k < j; k++) toks[k]!.shape = 'closed'
      i = j
    }
    // Hold each shape at least 60 ms, except closed lips and pauses, which must stay readable.
    let start = 0
    this.frames = []
    toks.forEach((tok, i) => {
      const last = this.frames[i - 1]
      const short = last && last.shape !== tok.shape && (i - start) * HOP < MIN_HOLD && tok.shape !== 'closed' && tok.shape !== 'rest'
      const shape = short ? last.shape : tok.shape
      if (!last || last.shape !== shape) start = i
      this.frames.push({ shape, level: tok.level })
    })
  }
}

function join(a: Float32Array, b: Float32Array): Float32Array {
  if (!a.length) return b
  const out = new Float32Array(a.length + b.length)
  out.set(a)
  out.set(b, a.length)
  return out
}
