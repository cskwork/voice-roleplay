import type { CallLine } from './callSession'

export interface PastCall {
  title: { ko: string; en: string }
  at: Date
  lines: CallLine[]
}

/** Calls of this visit, newest first. Memory only: gone when the page closes, nothing is stored. */
let calls: PastCall[] = []
const listeners = new Set<() => void>()

export const pastCalls = {
  get: () => calls,
  add(call: PastCall): void {
    calls = [call, ...calls].slice(0, 20)
    for (const l of listeners) l()
  },
  subscribe(fn: () => void): () => void {
    listeners.add(fn)
    return () => listeners.delete(fn)
  },
}

let nextTopic = 0
/** A different chat topic each call, without asking. */
export function topicOfTheCall<T>(topics: T[]): T | undefined {
  return topics[nextTopic % Math.max(1, topics.length)]
}
export function rotateTopic(): void {
  nextTopic += 1
}
