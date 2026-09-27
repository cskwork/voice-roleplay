import captureUrl from './worklets/capture.worklet.ts?worker&url'
import playbackUrl from './worklets/playback.worklet.ts?worker&url'
import type { PlaybackSink } from '../lib/playbackQueue'
import { resample } from '../lib/resample'

let ctxPromise: Promise<AudioContext> | null = null

/** Shared AudioContext with both worklets loaded. Call from a user gesture the first time. */
export function audioContext(): Promise<AudioContext> {
  ctxPromise ??= (async () => {
    const ctx = new AudioContext({ latencyHint: 'interactive' })
    await Promise.all([ctx.audioWorklet.addModule(captureUrl), ctx.audioWorklet.addModule(playbackUrl)])
    return ctx
  })().catch((e: unknown) => {
    ctxPromise = null
    throw e
  })
  return ctxPromise.then(async (ctx) => {
    if (ctx.state === 'suspended') await ctx.resume()
    return ctx
  })
}

export async function listMicrophones(): Promise<MediaDeviceInfo[]> {
  const devices = await navigator.mediaDevices.enumerateDevices()
  return devices.filter((d) => d.kind === 'audioinput')
}

export type FrameHandler = (pcm: Int16Array, rms: number) => void

/** Microphone capture producing 16 kHz mono PCM16 frames of 20 ms. */
export class MicCapture {
  level = 0
  private muted = false

  private constructor(
    private readonly stream: MediaStream,
    private readonly nodes: AudioNode[],
    readonly deviceRate: number,
  ) {}

  static async open(deviceId: string | undefined, onFrame: FrameHandler): Promise<MicCapture> {
    const ctx = await audioContext()
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    })
    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, 'vr-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] })
    // Keep the node pulled by the graph without making it audible.
    const sink = ctx.createGain()
    sink.gain.value = 0
    source.connect(node)
    node.connect(sink)
    sink.connect(ctx.destination)
    const mic = new MicCapture(stream, [source, node, sink], ctx.sampleRate)
    node.port.onmessage = (e: MessageEvent<{ pcm: Int16Array; rms: number }>) => {
      mic.level = mic.muted ? 0 : e.data.rms
      if (!mic.muted) onFrame(e.data.pcm, e.data.rms)
    }
    return mic
  }

  get trackLabel(): string {
    return this.stream.getAudioTracks()[0]?.label ?? ''
  }

  setMuted(muted: boolean): void {
    this.muted = muted
    for (const t of this.stream.getAudioTracks()) t.enabled = !muted
  }

  close(): void {
    for (const n of this.nodes) n.disconnect()
    for (const t of this.stream.getTracks()) t.stop()
    this.level = 0
  }
}

export type PlaybackEvent =
  | { type: 'started'; key: string; seg: string; playedMs: number }
  | { type: 'completed'; key: string; seg: string; playedMs: number }
  | { type: 'stopped'; key: string; seg: string; playedMs: number }
  | { type: 'flushed' }

/** AudioWorklet playback queue; audio pushed here must already be at `rate`. */
export class Player implements PlaybackSink {
  private constructor(
    private readonly node: AudioWorkletNode,
    readonly rate: number,
  ) {}

  onEvent: (e: PlaybackEvent) => void = () => {}

  static async create(): Promise<Player> {
    const ctx = await audioContext()
    const node = new AudioWorkletNode(ctx, 'vr-playback', { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2] })
    node.connect(ctx.destination)
    const p = new Player(node, ctx.sampleRate)
    node.port.onmessage = (e: MessageEvent<PlaybackEvent>) => p.onEvent(e.data)
    return p
  }

  push(key: string, seg: string, samples: Float32Array): void {
    const data = samples.slice() // transferred to the worklet; callers keep their copy
    this.node.port.postMessage({ type: 'push', key, seg, data }, [data.buffer])
  }

  end(key: string, seg: string): void {
    this.node.port.postMessage({ type: 'end', key, seg })
  }

  flush(): void {
    this.node.port.postMessage({ type: 'flush' })
  }

  /** Plays a whole clip (e.g. test sound, replay) under a local key. */
  playClip(key: string, samples: Float32Array, rate: number): void {
    this.push(key, '0', rate === this.rate ? samples : resample(samples, rate, this.rate))
    this.end(key, '0')
  }

  close(): void {
    this.flush()
    this.node.disconnect()
  }
}
