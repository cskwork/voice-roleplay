/**
 * Conversation captions. A turn's partial caption is replaced (never appended) by later
 * partials and by the final transcript. AI captions are kept per segment so that only
 * segments actually heard are shown as spoken.
 */
export type SegmentStatus = 'queued' | 'playing' | 'played' | 'interrupted' | 'dropped'

export interface UserCaption {
  kind: 'user'
  turn_id: string
  text: string
  final: boolean
  revision?: number
}

export interface AiSegment {
  segment_id: string
  text: string
  status: SegmentStatus
}

export interface AiCaption {
  kind: 'ai'
  response_id: string
  segments: AiSegment[]
  state: 'streaming' | 'done' | 'cancelled'
}

export type Caption = UserCaption | AiCaption

export type CaptionAction =
  | { type: 'speech.started'; turn_id: string }
  | { type: 'asr.partial'; turn_id: string; text: string }
  | { type: 'asr.final'; turn_id: string; text: string; revision?: number }
  | { type: 'echo.suspected' }
  | { type: 'response.text'; response_id: string; segment_id: string; text: string }
  | { type: 'segment.status'; response_id: string; segment_id: string; status: SegmentStatus }
  | { type: 'response.done'; response_id: string }
  | { type: 'response.cancelled'; response_id: string }
  | { type: 'reset' }

function updateUser(items: Caption[], turnId: string, fn: (c: UserCaption | undefined) => UserCaption | null): Caption[] {
  const i = items.findIndex((c) => c.kind === 'user' && c.turn_id === turnId)
  const next = fn(i >= 0 ? (items[i] as UserCaption) : undefined)
  if (i < 0) return next ? [...items, next] : items
  const copy = items.slice()
  if (next) copy[i] = next
  else copy.splice(i, 1)
  return copy
}

function updateAi(items: Caption[], responseId: string, fn: (c: AiCaption) => AiCaption, create: boolean): Caption[] {
  const i = items.findIndex((c) => c.kind === 'ai' && c.response_id === responseId)
  if (i < 0) {
    if (!create) return items
    return [...items, fn({ kind: 'ai', response_id: responseId, segments: [], state: 'streaming' })]
  }
  const copy = items.slice()
  copy[i] = fn(items[i] as AiCaption)
  return copy
}

export function captionsReducer(items: Caption[], a: CaptionAction): Caption[] {
  switch (a.type) {
    case 'reset':
      return []
    case 'speech.started':
      return updateUser(items, a.turn_id, (c) => c ?? { kind: 'user', turn_id: a.turn_id, text: '', final: false })
    case 'asr.partial':
      // A late partial must not overwrite the final transcript.
      return updateUser(items, a.turn_id, (c) => (c?.final ? c : { kind: 'user', turn_id: a.turn_id, text: a.text, final: false }))
    case 'asr.final':
      // Empty final = silence/noise that was discarded; drop the placeholder.
      return updateUser(items, a.turn_id, () =>
        a.text.trim() ? { kind: 'user', turn_id: a.turn_id, text: a.text, final: true, revision: a.revision } : null,
      )
    case 'echo.suspected':
      return items.filter((c) => c.kind !== 'user' || c.final)
    case 'response.text':
      return updateAi(
        items,
        a.response_id,
        (c) => {
          if (c.state === 'cancelled') return c
          const j = c.segments.findIndex((s) => s.segment_id === a.segment_id)
          const segments = c.segments.slice()
          if (j >= 0) segments[j] = { ...segments[j]!, text: a.text }
          else segments.push({ segment_id: a.segment_id, text: a.text, status: 'queued' })
          return { ...c, segments }
        },
        true,
      )
    case 'segment.status':
      return updateAi(
        items,
        a.response_id,
        (c) => ({ ...c, segments: c.segments.map((s) => (s.segment_id === a.segment_id ? { ...s, status: a.status } : s)) }),
        false,
      )
    case 'response.done':
      return updateAi(items, a.response_id, (c) => (c.state === 'streaming' ? { ...c, state: 'done' } : c), false)
    case 'response.cancelled':
      return updateAi(
        items,
        a.response_id,
        (c) => ({
          ...c,
          state: 'cancelled',
          segments: c.segments.map((s) =>
            s.status === 'playing' ? { ...s, status: 'interrupted' } : s.status === 'queued' ? { ...s, status: 'dropped' } : s,
          ),
        }),
        false,
      )
  }
}

/** Text of the most recent AI response that the learner actually heard (for 다시 듣기). */
export function lastHeardAiText(items: Caption[]): string {
  for (let i = items.length - 1; i >= 0; i--) {
    const c = items[i]!
    if (c.kind !== 'ai') continue
    const heard = c.segments.filter((s) => s.status === 'played' || s.status === 'playing' || s.status === 'interrupted')
    if (heard.length) return heard.map((s) => s.text).join(' ')
  }
  return ''
}
