import { describe, expect, it } from 'vitest'
import { wordDiff } from '../src/lib/diff'

describe('wordDiff', () => {
  it('ignores case and punctuation', () => {
    expect(wordDiff('Could I get a latte, please?', 'could i get a latte please').every((o) => o.op === 'same')).toBe(true)
  })

  it('pairs substitutions and reports missing/extra words', () => {
    const ops = wordDiff('I would like a small latte', 'I like a large latte too')
    expect(ops).toEqual([
      { op: 'same', target: 'I', heard: 'I' },
      { op: 'missing', target: 'would' },
      { op: 'same', target: 'like', heard: 'like' },
      { op: 'same', target: 'a', heard: 'a' },
      { op: 'changed', target: 'small', heard: 'large' },
      { op: 'same', target: 'latte', heard: 'latte' },
      { op: 'extra', heard: 'too' },
    ])
  })

  it('handles empty transcripts', () => {
    expect(wordDiff('Hello there', '')).toEqual([
      { op: 'missing', target: 'Hello' },
      { op: 'missing', target: 'there' },
    ])
  })
})
