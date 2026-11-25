// The character's acting: state names become poses, clips and a live mouth on the rig.
import rigDef from './ham/ham-m-rig.json'
import rigSvg from './ham/ham-m-rig.svg?raw'
import type { Mouth } from './mouth'
import { Rig, type RigDef } from './rig.js'

const LISTEN = ['listen1', 'listen2', 'listen3', 'listen4'] as const
const THINK = ['think1', 'think2'] as const
const WAIT = ['waitAside', 'waitTilt'] as const
const BOIL_MS = 170

/** Gesture that goes with a reply (the LLM picks one, vr_feedback.calls.ACTS) -> pose or clip of the rig. */
const ACT: Record<string, ['hold' | 'play', string]> = {
  calm: ['hold', 'idle'],
  happy: ['hold', 'happy'],
  nod: ['play', 'nod'],
  excited: ['play', 'excited'],
  surprised: ['play', 'surprised'],
  confused: ['play', 'confused'],
  bye: ['play', 'bye'],
}

let serial = 0

export class Stage {
  private readonly rig: Rig
  private readonly reduced = matchMedia('(prefers-reduced-motion: reduce)').matches
  private readonly lines: [Element, string[]][]
  private readonly last: Record<string, string> = {}
  private boil = 0
  private raf = 0
  /** Called once per frame while the stage runs. */
  onFrame: (() => void) | null = null

  constructor(host: HTMLElement, size: number, pose = 'idle') {
    this.rig = new Rig(host, `ham${serial++}`, { rig: { id: 'ham-m', def: rigDef as RigDef, svg: rigSvg }, seed: Date.now() & 0xffff, size })
    this.rig.svg.removeAttribute('width')
    this.rig.svg.removeAttribute('height')
    this.rig.hold(pose, performance.now(), 0)
    if (this.reduced) this.rig.setReduced(true, performance.now())
    // Hand-drawn line: three versions of every stroke, swapped a few times a second.
    this.lines = [...this.rig.svg.querySelectorAll('[data-b]')].map((el) => [el, (el as SVGElement).dataset.b!.split('|')])
    this.start()
  }

  start(): void {
    cancelAnimationFrame(this.raf)
    const frame = (t: number) => {
      this.raf = requestAnimationFrame(frame)
      this.rig.tick(t)
      const k = Math.floor(t / BOIL_MS) % 3
      if (!this.reduced && k !== this.boil) {
        this.boil = k
        for (const [el, d] of this.lines) el.setAttribute('d', d[k]!)
      }
      this.onFrame?.()
    }
    this.raf = requestAnimationFrame(frame)
  }

  stop(): void {
    cancelAnimationFrame(this.raf)
  }

  hold(name: string, ms = 380): void {
    const now = performance.now()
    if (this.reduced) this.rig.still(name.replace(/\d$/, ''), now)
    else this.rig.hold(name, now, ms)
  }

  play(name: string, onEnd?: () => void): void {
    const now = performance.now()
    if (!this.reduced) return this.rig.play(name, now, onEnd)
    this.rig.still(name === 'bye' || name === 'excited' ? 'happy' : 'idle', now)
    setTimeout(() => onEnd?.(), 400)
  }

  idle(): void {
    this.hold('idle', 420)
  }
  pickUp(): Promise<void> {
    return new Promise((resolve) => this.play('pickup', resolve))
  }
  listen(): void {
    this.blink()
    this.hold(this.pick('listen', LISTEN), 400)
  }
  think(): void {
    this.blink()
    this.hold(this.pick('think', THINK), 360)
  }
  wait(): void {
    this.hold(this.pick('wait', WAIT), 520)
  }
  /** A small "I'm listening" nod that keeps the listening pose. */
  nod(): void {
    if (this.reduced) return
    const held = this.rig.pose
    this.rig.play('nod', performance.now(), () => {
      this.rig.pose = held
    })
  }
  speak(mouth: () => Mouth | null, act: string | null): void {
    this.blink()
    const [cmd, name] = ACT[act ?? ''] ?? ['hold', 'idle']
    if (cmd === 'play') this.play(name)
    else this.hold(name, 260)
    if (!this.reduced) this.rig.live(mouth)
  }
  quiet(): void {
    this.rig.live(null)
  }

  /** A reaction starts under a blink, as the observed calls do. */
  private blink(): void {
    if (this.reduced) return
    this.rig.blink = { t0: performance.now(), twice: false }
    this.rig.nextBlink = performance.now() + 2600
  }

  /** Never the same pose twice in a row. */
  private pick(kind: string, list: readonly string[]): string {
    const rest = list.filter((x) => x !== this.last[kind])
    return (this.last[kind] = rest[Math.floor(Math.random() * rest.length)]!)
  }
}
