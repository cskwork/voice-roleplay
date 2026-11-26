import { describe, expect, it } from 'vitest'
import { normalizeHealth } from '../src/lib/api'
import { transferNotice } from '../src/lib/privacy'

const health = (extra: object) => normalizeHealth({ mode: 'api', workers: {}, modes: {}, ...extra })

describe('transferNotice', () => {
  it('says where each part runs (default: text through the Claude CLI, voice on the server)', () => {
    const h = health({ engines: { llm: 'claude-cli', asr: 'local', tts: 'kokoro' } })
    expect(h.engines).toEqual({ llm: 'claude-cli', asr: 'local', tts: 'kokoro' })
    expect(transferNotice(h, '끝.')).toBe(
      '대화 글은 서버 PC에 로그인된 Claude 계정으로 Anthropic(미국)에 보내고, 그 계정 약관에 따라 Anthropic이 보관할 수 있어요. 말하는 목소리와 AI 목소리는 서버 PC 안에서 처리하고 밖으로 보내지 않아요. 끝.')
  })
  it('names hosted voice engines when keys are set', () => {
    const text = transferNotice(health({ engines: { llm: 'anthropic', asr: 'openai', tts: 'openrouter' } }), '끝.')
    expect(text).toContain('Claude API로 Anthropic(미국)')
    expect(text).toContain('OpenAI(미국)로 보내 글로 바꿔요')
    expect(text).toContain('OpenRouter를 거쳐 Deepgram(미국)')
  })
  it('keeps the older sentence for a gateway without engines, and the local sentence outside API mode', () => {
    expect(transferNotice(health({}), '끝.')).toContain('OpenAI·OpenRouter(미국)로 전송돼요')
    expect(health({ engines: { llm: 'gpt', asr: 'local', tts: 'kokoro' } }).engines).toBeNull()
    expect(transferNotice(normalizeHealth({ mode: 'local' }), '끝.')).toBe('모든 처리는 이 컴퓨터 안에서만 이뤄져요. 끝.')
  })
})
