import { describe, expect, it } from 'vitest'
import { captionsReducer, lastHeardAiText, type Caption, type CaptionAction } from '../src/lib/captions'

const run = (actions: CaptionAction[], start: Caption[] = []) => actions.reduce(captionsReducer, start)

describe('captions', () => {
  it('partials replace each other instead of appending, final replaces partial', () => {
    const c = run([
      { type: 'speech.started', turn_id: 't1' },
      { type: 'asr.partial', turn_id: 't1', text: 'I would' },
      { type: 'asr.partial', turn_id: 't1', text: 'I would like a' },
      { type: 'asr.final', turn_id: 't1', text: 'I would like a latte.', revision: 1 },
    ])
    expect(c).toEqual([{ kind: 'user', turn_id: 't1', text: 'I would like a latte.', final: true, revision: 1 }])
  })

  it('a late partial never overwrites the final transcript', () => {
    const c = run([
      { type: 'asr.final', turn_id: 't1', text: 'Hello there.' },
      { type: 'asr.partial', turn_id: 't1', text: 'Hello' },
    ])
    expect(c).toHaveLength(1)
    expect(c[0]).toMatchObject({ text: 'Hello there.', final: true })
  })

  it('empty final (silence) removes the placeholder; echo removes unconfirmed captions', () => {
    expect(run([{ type: 'speech.started', turn_id: 't1' }, { type: 'asr.final', turn_id: 't1', text: '  ' }])).toEqual([])
    const c = run([
      { type: 'asr.final', turn_id: 't1', text: 'Yes please.' },
      { type: 'asr.partial', turn_id: 't2', text: 'what size would you' },
      { type: 'echo.suspected' },
    ])
    expect(c.map((x) => (x.kind === 'user' ? x.turn_id : ''))).toEqual(['t1'])
  })

  it('AI segments: text per segment, interrupted segment kept, unplayed ones dropped on cancel', () => {
    const c = run([
      { type: 'response.text', response_id: 'r1', segment_id: '0', text: 'Sure,' },
      { type: 'response.text', response_id: 'r1', segment_id: '1', text: 'what size?' },
      { type: 'response.text', response_id: 'r1', segment_id: '2', text: 'We have three.' },
      { type: 'segment.status', response_id: 'r1', segment_id: '0', status: 'played' },
      { type: 'segment.status', response_id: 'r1', segment_id: '1', status: 'playing' },
      { type: 'response.cancelled', response_id: 'r1' },
      { type: 'response.text', response_id: 'r1', segment_id: '3', text: 'late text' },
    ])
    const ai = c[0]
    expect(ai?.kind).toBe('ai')
    if (ai?.kind !== 'ai') return
    expect(ai.state).toBe('cancelled')
    expect(ai.segments.map((s) => s.status)).toEqual(['played', 'interrupted', 'dropped'])
    expect(lastHeardAiText(c)).toBe('Sure, what size?')
  })
})
