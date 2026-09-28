// The browser's own copy of a submitted take, for "내 발음" word playback (PROTOCOL §12.4: the server released
// the audio). Memory only, never stored; dropped 5 minutes after recording like the preview (PRD §16) or when
// the page closes.
import { decodeWav } from './wav'

export const TAKE_TTL_MS = 5 * 60 * 1000

interface Kept {
  samples: Int16Array
  rate: number
  expiresAt: number
  timer: ReturnType<typeof setTimeout>
}

const takes = new Map<string, Kept>()

export function rememberTake(attemptId: string, wav: ArrayBuffer, recordedAt: number, now = Date.now()): void {
  forgetTake(attemptId)
  const expiresAt = recordedAt + TAKE_TTL_MS
  if (expiresAt <= now) return
  const { samples, sampleRate } = decodeWav(wav)
  takes.set(attemptId, { samples, rate: sampleRate, expiresAt, timer: setTimeout(() => forgetTake(attemptId), expiresAt - now) })
}

export function learnerTake(attemptId: string, now = Date.now()): { samples: Int16Array; rate: number; expiresAt: number } | null {
  const t = takes.get(attemptId)
  if (!t) return null
  if (t.expiresAt <= now) {
    forgetTake(attemptId)
    return null
  }
  return { samples: t.samples, rate: t.rate, expiresAt: t.expiresAt }
}

export function forgetTake(attemptId: string): void {
  const t = takes.get(attemptId)
  if (t) clearTimeout(t.timer)
  takes.delete(attemptId)
}
