import { floatToPcm16 } from '../../lib/pcm'
import { StreamingResampler } from '../../lib/resample'

const TARGET_RATE = 16000
const FRAME_SAMPLES = 320 // 20 ms at 16 kHz

/** Mic -> mono -> 16 kHz (windowed-sinc low-pass resampler) -> 20 ms PCM16 frames + RMS level. */
class CaptureProcessor extends AudioWorkletProcessor {
  private readonly resampler = new StreamingResampler(sampleRate, TARGET_RATE)
  private frame = new Float32Array(FRAME_SAMPLES)
  private fill = 0
  private mono = new Float32Array(128)

  process(inputs: Float32Array[][]): boolean {
    const channels = inputs[0]
    if (!channels || channels.length === 0) return true
    const n = channels[0]!.length
    if (this.mono.length !== n) this.mono = new Float32Array(n)
    this.mono.fill(0)
    for (const ch of channels) for (let i = 0; i < n; i++) this.mono[i] = this.mono[i]! + ch[i]! / channels.length

    const out = this.resampler.process(this.mono)
    for (let i = 0; i < out.length; i++) {
      this.frame[this.fill++] = out[i]!
      if (this.fill === FRAME_SAMPLES) {
        let sum = 0
        for (let j = 0; j < FRAME_SAMPLES; j++) sum += this.frame[j]! * this.frame[j]!
        const pcm = floatToPcm16(this.frame)
        this.port.postMessage({ pcm, rms: Math.sqrt(sum / FRAME_SAMPLES) }, [pcm.buffer])
        this.fill = 0
      }
    }
    return true
  }
}

registerProcessor('vr-capture', CaptureProcessor)
