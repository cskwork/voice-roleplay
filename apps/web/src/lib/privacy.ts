/**
 * What leaves the machine, in one Korean sentence for the learner (Setup screen, call home).
 * Built from `/api/health` `engines` (API mode, 2026-11-26: each part picks its engine; by default the text goes
 * through the Claude CLI logged in on the server and voice stays on the server). A gateway that reports API mode
 * without `engines` is an older one that always used OpenAI and OpenRouter.
 */
import type { Health } from './types'

export function transferNotice(health: Health | null | undefined, tail: string): string {
  if (!health?.api) return `모든 처리는 이 컴퓨터 안에서만 이뤄져요. ${tail}`
  const e = health.engines
  if (!e) {
    return `말하는 목소리와 대화 글이 OpenAI·OpenRouter(미국)로 전송돼요. OpenRouter는 글을 OpenAI·Azure·Deepgram에 넘겨 처리해요. ${tail}`
  }
  const text = {
    'claude-cli': '대화 글은 서버 PC에 로그인된 Claude 계정으로 Anthropic(미국)에 보내고, 그 계정 약관에 따라 Anthropic이 보관할 수 있어요.',
    'codex-cli': '대화 글은 서버 PC에 로그인된 Codex 계정으로 OpenAI(미국)에 보내고, 그 계정 약관에 따라 OpenAI가 보관할 수 있어요.',
    anthropic: '대화 글은 Claude API로 Anthropic(미국)에 보내요.',
    openrouter: '대화 글은 OpenRouter(미국)로 보내고, OpenRouter가 OpenAI·Azure에 넘겨 처리해요.',
  }[e.llm]
  const asrHere = e.asr === 'local'
  const ttsHere = e.tts === 'kokoro'
  const voice = asrHere && ttsHere
    ? '말하는 목소리와 AI 목소리는 서버 PC 안에서 처리하고 밖으로 보내지 않아요.'
    : [
        asrHere ? '말하는 목소리는 서버 PC 안에서 글로 바꿔요.' : '말하는 목소리는 OpenAI(미국)로 보내 글로 바꿔요.',
        ttsHere ? 'AI 목소리는 서버 PC 안에서 만들어요.'
          : e.tts === 'deepgram' ? 'AI 대답 글은 Deepgram(미국)으로 보내 목소리로 만들어요.'
            : 'AI 대답 글은 OpenRouter를 거쳐 Deepgram(미국)에서 목소리로 만들어요.',
      ].join(' ')
  return `${text} ${voice} ${tail}`
}
