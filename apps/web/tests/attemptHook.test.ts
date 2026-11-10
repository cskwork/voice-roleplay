import { afterEach, describe, expect, it, vi } from 'vitest'

// Deterministic hook host: run the real hook's effects and cleanup without a browser/audio device.
const host = vi.hoisted(() => ({ cells: [] as unknown[], cursor: 0, effects: [] as (() => void)[], cleanups: [] as (() => void)[] }))
vi.mock('react', () => ({
  useState: (value: unknown) => {
    const i = host.cursor++
    if (!(i in host.cells)) host.cells[i] = typeof value === 'function' ? (value as () => unknown)() : value
    return [host.cells[i], (v: unknown) => { host.cells[i] = typeof v === 'function' ? (v as (old: unknown) => unknown)(host.cells[i]) : v }]
  },
  useRef: (value: unknown) => { const i = host.cursor++; if (!(i in host.cells)) host.cells[i] = { current: value }; return host.cells[i] },
  useCallback: (fn: unknown) => fn,
  useEffect: (fn: () => void | (() => void), deps: unknown[]) => {
    const i = host.cursor++, prev = host.cells[i] as unknown[] | undefined
    if (!prev || deps.some((d, n) => d !== prev[n])) {
      host.cells[i] = deps
      host.effects.push(() => { host.cleanups[i]?.(); const cleanup = fn(); host.cleanups[i] = cleanup || (() => {}) })
    }
  },
}))
vi.mock('../src/lib/takes', () => ({ rememberTake: vi.fn() }))
import { api } from '../src/lib/api'
import { useAttemptRunner } from '../src/hooks/attempt'
import type { Take } from '../src/hooks/audio'
import type { AttemptResult, Job } from '../src/lib/types'

const result = { attempt_id: 'a', transcript: 'Hi.' } as AttemptResult
const job = { job_id: 'j', attempt_id: 'a', state: 'completed' } as Job
const take = { id: 'take', wav: new ArrayBuffer(2), recordedAt: 1 } as Take
const body = { exercise_type: 'shadowing', scenario_id: 'cafe_order', history_opt_in: false } as const
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
function setup() {
  const completed = vi.fn()
  vi.spyOn(api, 'createAttempt').mockResolvedValue({ attempt_id: 'a' })
  vi.spyOn(api, 'uploadAudio').mockResolvedValue({})
  vi.spyOn(api, 'submitAttempt').mockResolvedValue(job)
  vi.spyOn(api, 'job').mockResolvedValue(job)
  vi.spyOn(api, 'cancelJob').mockResolvedValue({ ...job, state: 'cancelled' })
  vi.spyOn(api, 'result').mockResolvedValue(result)
  const render = () => { host.cursor = 0; const runner = useAttemptRunner(completed); const effects = host.effects.splice(0); effects.forEach((fn) => fn()); return runner }
  const unmount = () => { const cleanups = host.cleanups.splice(0); cleanups.forEach((fn) => fn?.()) }
  return { render, completed, unmount }
}
afterEach(() => { host.cleanups.forEach((fn) => fn?.()); host.cells = []; host.effects = []; host.cleanups = []; vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('attempt hook regression against the original lifecycle', () => {
  it('does not deliver a result that returns after unmount', async () => {
    const { render, completed, unmount } = setup()
    let release!: (r: AttemptResult) => void
    vi.mocked(api.result).mockReturnValueOnce(new Promise((yes) => { release = yes }))
    await render().submit(take, body); render(); await settle()
    expect(api.result).toHaveBeenCalledOnce()
    unmount(); release(result); await settle()
    expect(completed).not.toHaveBeenCalled()
  })
  it('reuploads the same take after upload failure', async () => {
    const { render } = setup()
    vi.mocked(api.uploadAudio).mockRejectedValueOnce(new TypeError('offline'))
    await render().submit(take, body)
    await render().submit(take, body); render(); await settle()
    expect(api.uploadAudio).toHaveBeenCalledTimes(2)
  })
  it('surfaces a polling error and releases the busy state', async () => {
    const { render, completed } = setup()
    vi.mocked(api.job).mockRejectedValueOnce(new TypeError('offline'))
    await render().submit(take, body); render(); await settle()
    const state = render()
    expect(state.error).toBeTruthy()
    expect(state.busy).toBe(false)
    expect(completed).not.toHaveBeenCalled()
  })
})

import { MicCapture } from '../src/audio/engine'
import { stopModelAudio, useModelAudio, useRecorder } from '../src/hooks/audio'

describe('audio request lifetime', () => {
  const mount = <T,>(hook: () => T) => {
    host.cursor = 0
    const value = hook(); host.effects.splice(0).forEach((fn) => fn())
    return value
  }
  const unmount = () => { host.cleanups.splice(0).forEach((fn) => fn?.()) }
  it('closes a microphone whose permission resolves after leaving', async () => {
    const close = vi.fn(); let opened!: (m: MicCapture) => void
    vi.spyOn(MicCapture, 'open').mockReturnValueOnce(new Promise((yes) => { opened = yes }))
    const recorder = mount(() => useRecorder('', 30000))
    const starting = recorder.start(); unmount()
    opened({ close } as unknown as MicCapture); await starting
    expect(close).toHaveBeenCalledOnce()
  })
  for (const action of ['leave', 'record'] as const) it(`does not play late TTS after ${action === 'leave' ? 'leaving' : 'starting recording'}`, async () => {
    let resolve!: (wav: ArrayBuffer) => void
    const makeAudio = vi.fn(function (src: string) { return { src, play: async () => {}, pause: () => {} } })
    vi.stubGlobal('Audio', makeAudio)
    vi.spyOn(api, 'cachedTts').mockReturnValueOnce(new Promise((yes) => { resolve = yes }))
    const audio = mount(() => useModelAudio('voice'))
    const pending = audio.play({ text: 'Hi.', textId: 'hi' }, false)
    if (action === 'leave') unmount(); else stopModelAudio()
    resolve(new ArrayBuffer(44)); await pending
    expect(makeAudio).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })
})
