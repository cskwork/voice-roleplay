/**
 * Leaving the realtime Talk screen ends the conversation (PRD §7, v0.2.1): a route change or unmount sends
 * `POST /api/sessions/{id}/end`; closing or reloading the tab sends the same request with `keepalive`.
 * The gateway also ends a running conversation by itself when something new starts, so this is best effort.
 */

/** WebSocket close codes the gateway uses for a session it ended (PROTOCOL §6.3). */
export const CLOSE_ENDED = 4000
export const CLOSE_SUPERSEDED = 4001

export type EndedBy = 'ended' | 'superseded'

export function endedBy(code: number): EndedBy | null {
  return code === CLOSE_SUPERSEDED ? 'superseded' : code === CLOSE_ENDED ? 'ended' : null
}

export const ENDED_NOTICE: Record<EndedBy, string> = {
  superseded: '새 연습을 시작해 이전 회화를 종료했어요.',
  ended: '회화가 종료되었어요.',
}

export interface SessionEnder {
  /** Normal request (route change, unmount). */
  end(sessionId: string): Promise<unknown>
  /** The page is going away: the request must outlive it (fetch `keepalive`). */
  endOnUnload(sessionId: string): void
}

export interface LeaveGuard {
  /** The session was ended another way (종료 button): nothing more to send. */
  markEnded(): void
  /** Screen unmount: ends the session unless already ended, and stops watching for page unload. */
  release(): void
}

type Target = Pick<EventTarget, 'addEventListener' | 'removeEventListener'>

export function guardSessionEnd(sessionId: string, ender: SessionEnder, target: Target = window): LeaveGuard {
  let ended = false
  const onPageHide = () => {
    if (ended) return
    ended = true
    ender.endOnUnload(sessionId)
  }
  target.addEventListener('pagehide', onPageHide)
  return {
    markEnded() {
      ended = true
    },
    release() {
      target.removeEventListener('pagehide', onPageHide)
      if (ended) return
      ended = true
      ender.end(sessionId).catch(() => {})
    },
  }
}
