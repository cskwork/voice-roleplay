import type { OutputAudioHeader } from './envelope'
import { pcm16ToFloat } from './pcm'
import { StreamingResampler } from './resample'

/** Where accepted audio goes (the playback AudioWorklet in the app, a fake in tests). */
export interface PlaybackSink {
  push(responseId: string, segmentId: string, samples: Float32Array): void
  /** No more audio will arrive for this segment. */
  end(responseId: string, segmentId: string): void
  /** Stop immediately and drop everything queued. */
  flush(): void
}

interface OpenSegment {
  responseId: string
  segmentId: string
  resampler: StreamingResampler | null
}

/**
 * Decides which output audio may reach the speaker. Frames from an older epoch or from a
 * cancelled response are dropped (PROTOCOL §6.3 "Stale output"); TTS audio is resampled
 * to the playback context rate and split into segments for playback reporting.
 */
export class PlaybackController {
  private epoch = 0
  private readonly cancelled = new Set<string>()
  private open: OpenSegment | null = null
  private lastPushed: { responseId: string; epoch: number } | null = null

  constructor(
    private readonly sink: PlaybackSink,
    private readonly outRate: number,
  ) {}

  get currentEpoch(): number {
    return this.epoch
  }

  isCancelled(responseId: string): boolean {
    return this.cancelled.has(responseId)
  }

  isStale(responseId: string, epoch: number): boolean {
    return epoch < this.epoch || this.cancelled.has(responseId)
  }

  /** Record an epoch seen on any server event. Audio from an older generation is stopped. */
  observeEpoch(epoch: number): void {
    if (epoch <= this.epoch) return
    this.epoch = epoch
    if (this.lastPushed && this.lastPushed.epoch < epoch) {
      this.open = null
      this.lastPushed = null
      this.sink.flush()
    }
  }

  /** Returns false when the frame was dropped as stale. */
  handleAudio(h: OutputAudioHeader, pcm: Int16Array): boolean {
    if (this.isStale(h.response_id, h.epoch)) return false
    this.observeEpoch(h.epoch)
    const segmentId = String(h.segment_id ?? '0')
    if (this.open && (this.open.responseId !== h.response_id || this.open.segmentId !== segmentId)) this.closeOpen()
    if (!this.open) {
      this.open = {
        responseId: h.response_id,
        segmentId,
        resampler: h.sample_rate === this.outRate ? null : new StreamingResampler(h.sample_rate, this.outRate),
      }
    }
    let samples = pcm16ToFloat(pcm)
    if (this.open.resampler) samples = this.open.resampler.process(samples)
    if (samples.length) this.sink.push(h.response_id, segmentId, samples)
    this.lastPushed = { responseId: h.response_id, epoch: h.epoch }
    return true
  }

  /** `response.done`: every frame of the response has arrived. */
  endResponse(responseId: string): void {
    if (this.open?.responseId === responseId) this.closeOpen()
  }

  /**
   * Cancel a response (server `response.cancelled`). Idempotent, so the server confirming a
   * cancel we already applied locally cannot cut off newer audio.
   */
  cancel(responseId: string): boolean {
    if (this.cancelled.has(responseId)) return false
    this.cancelled.add(responseId)
    if (this.open?.responseId === responseId) this.open = null
    if (this.lastPushed?.responseId === responseId) {
      this.lastPushed = null
      this.sink.flush()
    }
    return true
  }

  /** Local stop (stop button, local barge-in): silence now and cancel the given responses. */
  stopNow(...responseIds: (string | null | undefined)[]): void {
    for (const id of responseIds) if (id) this.cancelled.add(id)
    if (this.lastPushed) this.cancelled.add(this.lastPushed.responseId)
    this.open = null
    this.lastPushed = null
    this.sink.flush()
  }

  private closeOpen(): void {
    const o = this.open
    if (!o) return
    this.open = null
    const tail = o.resampler?.flush()
    if (tail?.length) this.sink.push(o.responseId, o.segmentId, tail)
    this.sink.end(o.responseId, o.segmentId)
  }
}
