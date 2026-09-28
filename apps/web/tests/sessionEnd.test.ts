import { describe, expect, it } from 'vitest'
import { CLOSE_ENDED, CLOSE_SUPERSEDED, ENDED_NOTICE, endedBy, guardSessionEnd, type SessionEnder } from '../src/lib/sessionEnd'

function fakeEnder() {
  const calls: string[] = []
  const ender: SessionEnder = {
    end: async (id) => {
      calls.push(`end:${id}`)
    },
    endOnUnload: (id) => {
      calls.push(`unload:${id}`)
    },
  }
  return { calls, ender }
}

describe('leaving the Talk screen ends the session', () => {
  it('unmount without pressing 종료 sends end once', () => {
    const { calls, ender } = fakeEnder()
    const target = new EventTarget()
    const guard = guardSessionEnd('s_1', ender, target)
    guard.release()
    guard.release()
    target.dispatchEvent(new Event('pagehide'))
    expect(calls).toEqual(['end:s_1'])
  })

  it('tab close sends the keepalive request, and the later unmount sends nothing more', () => {
    const { calls, ender } = fakeEnder()
    const target = new EventTarget()
    const guard = guardSessionEnd('s_2', ender, target)
    target.dispatchEvent(new Event('pagehide'))
    guard.release()
    expect(calls).toEqual(['unload:s_2'])
  })

  it('after 종료 nothing is sent on unmount or unload', () => {
    const { calls, ender } = fakeEnder()
    const target = new EventTarget()
    const guard = guardSessionEnd('s_3', ender, target)
    guard.markEnded()
    target.dispatchEvent(new Event('pagehide'))
    guard.release()
    expect(calls).toEqual([])
  })

  it('a failed end request is swallowed', async () => {
    const guard = guardSessionEnd('s_4', { end: () => Promise.reject(new Error('offline')), endOnUnload: () => {} }, new EventTarget())
    expect(() => guard.release()).not.toThrow()
    await new Promise((r) => setTimeout(r, 0))
  })

  it('maps the gateway close codes to a calm notice', () => {
    expect(endedBy(CLOSE_SUPERSEDED)).toBe('superseded')
    expect(endedBy(CLOSE_ENDED)).toBe('ended')
    expect(endedBy(1006)).toBeNull()
    expect(ENDED_NOTICE.superseded).toBe('새 연습을 시작해 이전 회화를 종료했어요.')
  })
})
