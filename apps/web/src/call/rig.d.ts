export interface RigDef {
  svg?: string
  [key: string]: unknown
}

export class Rig {
  constructor(host: HTMLElement, prefix: string, opts: { rig: { id: string; def: RigDef; svg: string }; seed?: number; size?: number })
  readonly svg: SVGSVGElement
  pose: Record<string, unknown>
  blink: { t0: number; twice: boolean } | null
  nextBlink: number
  hush?: boolean
  setReduced(on: boolean, now: number): void
  hold(name: string, now: number, blendMs?: number): void
  play(name: string, now: number, onEnd?: () => void): void
  still(name: string, now: number): void
  /** Mouth and loudness of the audio being heard right now; null ends the talk layer. */
  live(fn: ((now: number) => { shape: string; level: number } | null) | null): void
  tick(now: number): void
}
