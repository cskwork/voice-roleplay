import { useRef, useState, type KeyboardEvent } from 'react'
import { ModelAudio } from '../components/ModelAudio'
import { Recorder } from '../components/Recorder'
import { Badge, JOB_LABEL, JobProgress, Notice, PronunciationNote } from '../components/ui'
import type { Take } from '../hooks/audio'
import { useAttemptRunner } from '../hooks/attempt'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import type { Difficulty, ExerciseType, Scenario } from '../lib/types'
import { navigate, useApp, useVoiceId } from '../state'
import { DIFFICULTY_LABEL } from './Home'

type Tab = Exclude<ExerciseType, 'drill'>

const TABS: { id: Tab; label: string; minMs: number; maxMs: number; help: string }[] = [
  { id: 'reading', label: '문장 읽기', minMs: 1000, maxMs: 30000, help: '문장을 소리 내어 읽어 보세요.' },
  { id: 'shadowing', label: '따라 말하기', minMs: 1000, maxMs: 30000, help: '모범 음성을 먼저 듣고 똑같이 따라 말해 보세요.' },
  { id: 'free_answer', label: '자유 답변', minMs: 5000, maxMs: 120000, help: '질문에 5초~2분 동안 자유롭게 대답해 보세요.' },
  { id: 'roleplay_turn', label: '턴제 역할극', minMs: 1000, maxMs: 45000, help: 'AI의 말을 읽고 녹음으로 대답하면, 피드백과 AI의 다음 말을 따로 보여 줘요.' },
]

export interface RoleplayTurn {
  who: 'ai' | 'me'
  text: string
  attemptId?: string
}

// Turn-based roleplay threads (a `turn_based` gateway session + what was said) live in memory only.
interface Thread {
  sessionId: string | null
  turns: RoleplayTurn[]
}
const threads = new Map<string, Thread>()
export function roleplayThread(sc: Scenario): Thread {
  let t = threads.get(sc.scenario_id)
  if (!t) threads.set(sc.scenario_id, (t = { sessionId: null, turns: [{ who: 'ai', text: sc.opening_line.en }] }))
  return t
}

function restartThread(sc: Scenario, done: () => void): void {
  const old = threads.get(sc.scenario_id)
  threads.delete(sc.scenario_id)
  if (old?.sessionId) api.endSession(old.sessionId).catch(() => {}) // best effort; the gateway also expires it
  done()
}

interface Item {
  id: string
  en: string
  ko: string
  textId?: string
  sample?: string
}

function itemsFor(sc: Scenario, tab: Tab): Item[] {
  switch (tab) {
    case 'reading':
      return sc.exercises.reading.map((t) => ({ id: t.text_id, en: t.en, ko: t.ko, textId: t.text_id }))
    case 'shadowing':
      return sc.exercises.shadowing.map((t) => ({ id: t.text_id, en: t.en, ko: t.ko, textId: t.text_id }))
    case 'free_answer':
      return sc.exercises.free_answer.map((q) => ({ id: q.exercise_id, en: q.question_en, ko: q.question_ko, sample: q.sample_answer_en }))
    case 'roleplay_turn':
      return []
  }
}

export function Practice({ parts }: { parts: string[] }) {
  const [scenarioId, opts = '', tabRaw, itemRaw, prevAttempt] = parts
  const app = useApp()
  const sc = app.scenario(scenarioId)
  const voiceId = useVoiceId(sc)
  const [difficultyRaw, optRaw] = opts.split(':')
  const difficulty = (['easy', 'normal', 'hard'].includes(difficultyRaw ?? '') ? difficultyRaw : app.settings.difficulty) as Difficulty
  const historyOptIn = optRaw ? optRaw === '1' : app.settings.history_opt_in
  const [tab, setTab] = useState<Tab>(TABS.some((t) => t.id === tabRaw) ? (tabRaw as Tab) : 'reading')
  const [itemId, setItemId] = useState<string | undefined>(itemRaw)
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([])
  const [sessionError, setSessionError] = useState<string | null>(null)

  const runner = useAttemptRunner((r) => {
    const cur = sc ? itemsFor(sc, tab).find((i) => i.id === itemId) ?? itemsFor(sc, tab)[0] : undefined
    app.rememberResult({
      result: r,
      meta: {
        scenarioId: scenarioId!,
        exerciseType: tab,
        opts,
        itemId: cur?.id,
        target: cur ? { en: cur.en, ko: cur.ko, textId: cur.textId } : undefined,
        sample: tab === 'free_answer' ? cur?.sample : undefined,
        previousAttemptId: prevAttempt,
      },
    })
    if (tab === 'roleplay_turn' && sc) {
      const t = roleplayThread(sc).turns
      if (r.transcript) t.push({ who: 'me', text: r.transcript, attemptId: r.attempt_id })
      if (r.next_ai?.text) t.push({ who: 'ai', text: r.next_ai.text })
    }
    navigate('result', r.attempt_id)
  })

  if (!sc) {
    return (
      <main className="page">
        <Notice tone="warn">시나리오를 찾을 수 없어요.</Notice>
      </main>
    )
  }

  const tabInfo = TABS.find((t) => t.id === tab)!
  const items = itemsFor(sc, tab)
  const item = items.find((i) => i.id === itemId) ?? items[0]
  const thread = roleplayThread(sc).turns
  const lastAi = [...thread].reverse().find((t) => t.who === 'ai')
  const recorded = app.health?.modes.recorded
  const ttsReady = app.health?.components.tts?.ready ?? false
  const disabledReason = !app.health ? '모델 상태를 확인하는 중이에요' : recorded?.available ? null : (recorded?.reason_ko ?? '녹음형 분석을 지금 사용할 수 없어요')

  const submit = async (take: Take) => {
    let sessionId: string | undefined
    if (tab === 'roleplay_turn') {
      const t = roleplayThread(sc)
      try {
        t.sessionId ??= (
          await api.createSession({ mode: 'turn_based', scenario_id: sc.scenario_id, difficulty, history_opt_in: historyOptIn, feedback_policy: 'session_end' })
        ).session_id
      } catch (e) {
        setSessionError(toApiError(e).messageKo)
        return
      }
      sessionId = t.sessionId
    }
    setSessionError(null)
    await runner.submit(take, {
      exercise_type: tab,
      scenario_id: sc.scenario_id,
      history_opt_in: historyOptIn,
      text_id: tab === 'reading' || tab === 'shadowing' ? item?.textId : undefined,
      exercise_id: tab === 'free_answer' ? item?.id : undefined,
      session_id: sessionId,
    })
  }

  const onTabKey = (e: KeyboardEvent, i: number) => {
    const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    if (!d) return
    const n = (i + d + TABS.length) % TABS.length
    setTab(TABS[n]!.id)
    tabRefs.current[n]?.focus()
  }

  return (
    <main className="page" aria-labelledby="pr-title">
      <header className="page-head">
        <h1 id="pr-title">{sc.title_ko}</h1>
        <p className="page-meta">
          <Badge tone="brand">녹음형 연습</Badge> <Badge>난이도 {DIFFICULTY_LABEL[difficulty]}</Badge> <Badge>{historyOptIn ? '기록 저장' : '기록 저장 안 함'}</Badge>
        </p>
      </header>

      <div className="tabs" role="tablist" aria-label="연습 유형">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => {
              tabRefs.current[i] = el
            }}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls="practice-panel"
            tabIndex={tab === t.id ? 0 : -1}
            className="tab"
            disabled={runner.busy}
            onClick={() => {
              setTab(t.id)
              setItemId(undefined)
            }}
            onKeyDown={(e) => onTabKey(e, i)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <section id="practice-panel" role="tabpanel" aria-labelledby={`tab-${tab}`} className="card practice">
        <p className="practice-help">{tabInfo.help}</p>

        {tab !== 'roleplay_turn' && items.length > 1 && (
          <label className="field">
            <span>{tab === 'free_answer' ? '질문 고르기' : '문장 고르기'}</span>
            <select value={item?.id} onChange={(e) => setItemId(e.target.value)} disabled={runner.busy}>
              {items.map((i, n) => (
                <option key={i.id} value={i.id}>
                  {n + 1}. {i.en}
                </option>
              ))}
            </select>
          </label>
        )}

        {tab !== 'roleplay_turn' && item && (
          <div className="prompt">
            <p className="prompt-en" lang="en">
              {item.en}
            </p>
            <p className="prompt-ko">{item.ko}</p>
            {tab !== 'free_answer' && <ModelAudio voiceId={voiceId} source={{ textId: item.textId, text: item.en }} ttsReady={ttsReady} />}
          </div>
        )}

        {tab === 'roleplay_turn' && (
          <div className="thread" aria-label="지금까지의 대화">
            {thread.slice(-4).map((t, i) => (
              <div key={i} className={`bubble ${t.who === 'ai' ? 'ai' : 'user final'}`}>
                <span className="who">{t.who === 'ai' ? `AI · ${sc.ai_role_ko}` : '나'}</span>
                <p lang="en">{t.text}</p>
              </div>
            ))}
            {lastAi && <ModelAudio voiceId={voiceId} source={{ textId: thread.length === 1 ? sc.opening_line.text_id : undefined, text: lastAi.text }} ttsReady={ttsReady} label="AI 말 듣기" />}
            {thread.length > 1 && (
              <button type="button" className="link small" onClick={() => restartThread(sc, () => setItemId(String(Date.now())))}>
                처음부터 다시
              </button>
            )}
          </div>
        )}

        {prevAttempt && <p className="muted small">다시 시도하는 중이에요. 결과에서 이전 시도와 비교해 보여 드려요.</p>}

        <Recorder
          key={`${tab}:${item?.id ?? thread.length}`}
          deviceId={app.micDeviceId}
          minMs={tabInfo.minMs}
          maxMs={tabInfo.maxMs}
          submitting={runner.busy}
          submitDisabledReason={disabledReason}
          onSubmit={(take) => void submit(take)}
        />

        {runner.busy && (
          <div className="job" aria-live="polite">
            {runner.job ? <JobProgress state={runner.job.state} /> : <p>녹음을 보내는 중…</p>}
            <button type="button" className="btn btn-ghost" onClick={() => void runner.cancel()} disabled={!runner.job}>
              분석 취소
            </button>
          </div>
        )}
        {runner.endState && runner.endState !== 'completed' && (
          <Notice tone="warn" icon="alert">
            분석 {JOB_LABEL[runner.endState]}. {runner.endState === 'cancelled' ? '녹음 데이터는 서버에서 지워졌어요.' : '다시 녹음해서 제출해 주세요.'}
          </Notice>
        )}
        {(runner.error ?? sessionError) && <Notice tone="danger" icon="alert">{runner.error ?? sessionError}</Notice>}
        <PronunciationNote />
      </section>
    </main>
  )
}
