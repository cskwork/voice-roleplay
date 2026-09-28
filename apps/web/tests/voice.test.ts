import { describe, expect, it } from 'vitest'
import { pickVoiceId, savedVoiceChoice } from '../src/lib/voice'

const voices = [
  { voice_id: 'libritts_r_4992_f', label: '여성 · 미국 영어' },
  { voice_id: 'libritts_r_1188_m', label: '남성 · 미국 영어' },
]

describe('voice choice', () => {
  it('uses the saved voice when the worker offers it', () => {
    expect(pickVoiceId('libritts_r_1188_m', 'libritts_r_4992_f', voices)).toBe('libritts_r_1188_m')
  })

  it('falls back to the scenario default when the saved voice was removed', () => {
    expect(pickVoiceId('dev_voice_a', 'libritts_r_1188_m', voices)).toBe('libritts_r_1188_m')
    expect(pickVoiceId('dev_voice_a', undefined, voices)).toBe('libritts_r_4992_f')
    expect(savedVoiceChoice('dev_voice_a', voices)).toBe('')
    expect(savedVoiceChoice('libritts_r_4992_f', voices)).toBe('libritts_r_4992_f')
  })

  it('uses the scenario default without a saved voice, and trusts the saved voice while the list is unknown', () => {
    expect(pickVoiceId(null, 'libritts_r_4992_f', voices)).toBe('libritts_r_4992_f')
    expect(pickVoiceId('dev_voice_a', 'libritts_r_4992_f', undefined)).toBe('dev_voice_a')
    expect(pickVoiceId(null, undefined, [])).toBe('')
  })
})
