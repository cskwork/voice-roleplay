import { describe, expect, it } from 'vitest'
import { LevelVad } from '../src/lib/levelVad'

describe('LevelVad (local barge-in trigger)', () => {
  it('fires once after 160 ms of speech-level input, not on steady noise', () => {
    const vad = new LevelVad()
    for (let i = 0; i < 100; i++) expect(vad.update(0.003)).toBe(false) // ~-50 dBFS background
    const fired = Array.from({ length: 20 }, () => vad.update(0.1)) // ~-20 dBFS voice
    expect(fired.indexOf(true)).toBe(7)
    expect(fired.filter(Boolean)).toHaveLength(1)
  })

  it('does not fire on short clicks', () => {
    const vad = new LevelVad()
    for (let i = 0; i < 50; i++) {
      expect(vad.update(i % 5 === 0 ? 0.2 : 0.001)).toBe(false)
    }
  })
})
