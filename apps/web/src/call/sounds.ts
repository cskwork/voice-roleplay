/** Phone sounds, synthesised here (no audio files). */
function tone(ctx: AudioContext, freq: number, when: number, dur: number, gain: number): OscillatorNode {
  const o = ctx.createOscillator()
  const g = ctx.createGain()
  o.frequency.value = freq
  g.gain.setValueAtTime(0, when)
  g.gain.linearRampToValueAtTime(gain, when + 0.012)
  g.gain.exponentialRampToValueAtTime(0.0001, when + dur)
  o.connect(g).connect(ctx.destination)
  o.start(when)
  o.stop(when + dur + 0.02)
  return o
}

/** Soft marimba-like ring every 1.5 s until the returned function is called. */
export function ring(ctx: AudioContext): () => void {
  const live = new Set<OscillatorNode>()
  const once = () => {
    const t = ctx.currentTime + 0.05
    for (const [f, d] of [[659.3, 0], [880, 0.16], [987.8, 0.32]] as const) {
      for (const o of [tone(ctx, f, t + d, 0.42, 0.07), tone(ctx, f * 4, t + d, 0.09, 0.014)]) {
        live.add(o)
        o.onended = () => live.delete(o)
      }
    }
  }
  once()
  const timer = setInterval(once, 1500)
  return () => {
    clearInterval(timer)
    for (const o of live) o.stop()
  }
}

export function connected(ctx: AudioContext): void {
  tone(ctx, 783.99, ctx.currentTime, 0.16, 0.08)
  tone(ctx, 1174.66, ctx.currentTime + 0.09, 0.26, 0.08)
}

export function hungUp(ctx: AudioContext): void {
  tone(ctx, 523.25, ctx.currentTime, 0.16, 0.07)
  tone(ctx, 392, ctx.currentTime + 0.12, 0.3, 0.07)
}
