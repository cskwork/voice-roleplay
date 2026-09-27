/**
 * Plays queued Float32 chunks (already at the context rate) and reports per-segment
 * started / completed / stopped with the number of samples actually rendered.
 */
type Item =
  | { kind: 'audio'; key: string; seg: string; data: Float32Array; pos: number }
  | { kind: 'end'; key: string; seg: string }

type Msg =
  | { type: 'push'; key: string; seg: string; data: Float32Array }
  | { type: 'end'; key: string; seg: string }
  | { type: 'flush' }

class PlaybackProcessor extends AudioWorkletProcessor {
  private queue: Item[] = []
  private current: { key: string; seg: string; played: number } | null = null

  constructor() {
    super()
    this.port.onmessage = (e: MessageEvent<Msg>) => {
      const m = e.data
      if (m.type === 'push') this.queue.push({ kind: 'audio', key: m.key, seg: m.seg, data: m.data, pos: 0 })
      else if (m.type === 'end') this.queue.push({ kind: 'end', key: m.key, seg: m.seg })
      else this.flush()
    }
  }

  private report(type: 'started' | 'completed' | 'stopped', c: { key: string; seg: string; played: number }): void {
    this.port.postMessage({ type, key: c.key, seg: c.seg, playedMs: (c.played / sampleRate) * 1000 })
  }

  private flush(): void {
    if (this.current) this.report('stopped', this.current)
    this.current = null
    this.queue = []
    this.port.postMessage({ type: 'flushed' })
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const out = outputs[0]
    const left = out?.[0]
    if (!out || !left) return true
    let i = 0
    while (i < left.length && this.queue.length) {
      const item = this.queue[0]!
      if (item.kind === 'end') {
        if (this.current && this.current.key === item.key && this.current.seg === item.seg) {
          this.report('completed', this.current)
          this.current = null
        }
        this.queue.shift()
        continue
      }
      if (!this.current || this.current.key !== item.key || this.current.seg !== item.seg) {
        this.current = { key: item.key, seg: item.seg, played: 0 }
        this.report('started', this.current)
      }
      const n = Math.min(left.length - i, item.data.length - item.pos)
      left.set(item.data.subarray(item.pos, item.pos + n), i)
      item.pos += n
      i += n
      this.current.played += n
      if (item.pos >= item.data.length) this.queue.shift()
    }
    left.fill(0, i)
    for (let c = 1; c < out.length; c++) out[c]!.set(left)
    return true
  }
}

registerProcessor('vr-playback', PlaybackProcessor)
