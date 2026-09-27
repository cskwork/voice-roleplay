import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Icon, type IconName } from '../components/Icon'
import { ActionButton, LevelMeter, Notice } from '../components/ui'
import { api } from '../lib/api'
import type { Caption } from '../lib/captions'
import { toApiError } from '../lib/errors'
import type { Difficulty, Scenario } from '../lib/types'
import { RealtimeSession, type HintView, type ServerState, type Snapshot } from '../session/realtimeSession'
import { navigate, useApp } from '../state'
import { DIFFICULTY_LABEL } from './Home'

const STATE_TEXT: Record<ServerState, [string, IconName, string]> = {
  CONNECTING: ['연결 중', 'circle', 'neutral'],
  READY: ['준비됨', 'check', 'neutral'],
  LISTENING: ['듣는 중', 'mic', 'listen'],
  FINALIZING: ['인식 마무리 중', 'circle', 'think'],
  RESPONDING: ['AI 응답 중', 'speaker', 'speak'],
  INTERRUPTING: ['끊는 중', 'stop', 'think'],
  PAUSED: ['일시정지', 'pause', 'neutral'],
  RECOVERABLE_ERROR: ['오류 — 다시 시도 가능', 'alert', 'warn'],
  CLOSED: ['종료됨', 'close', 'neutral'],
}

const HINT_STEP = ['한국어 뜻', '핵심 단어', '예시 문장'] as const

function StateChip({ s }: { s: Snapshot }) {
  const [text, icon, tone] = STATE_TEXT[s.state] ?? STATE_TEXT.READY
  return (
    <span className={`state-chip tone-${tone}`} role="status" aria-live="polite">
      <Icon name={icon} size={16} /> {text}
    </span>
  )
}

function MicStatus({ s, onToggleMute, onPtt }: { s: Snapshot; onToggleMute(): void; onPtt(pressed: boolean): void }) {
  if (s.pushToTalk) {
    return (
      <button
        type="button"
        className={`mic-orb ${s.pttPressed ? 'is-live' : ''}`}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId)
          onPtt(true)
        }}
        onPointerUp={() => onPtt(false)}
        onPointerCancel={() => onPtt(false)}
        onKeyDown={(e) => {
          if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
            e.preventDefault()
            onPtt(true)
          }
        }}
        onKeyUp={(e) => {
          if (e.key === ' ' || e.key === 'Enter') onPtt(false)
        }}
        aria-pressed={s.pttPressed}
        aria-label="누르고 있는 동안 말하기"
      >
        <Icon name={s.pttPressed ? 'mic' : 'hand'} size={40} />
        <span className="orb-label">{s.pttPressed ? '말하는 중… 떼면 제출' : '누르고 말하기'}</span>
      </button>
    )
  }
  const [label, icon] = s.paused
    ? ['일시정지됨', 'pause' as const]
    : s.muted
      ? ['마이크 꺼짐 · 눌러서 켜기', 'micOff' as const]
      : s.aiSpeaking
        ? ['AI가 말하는 중 · 말하면 끼어들어요', 'speaker' as const]
        : ['듣고 있어요 · 눌러서 음소거', 'mic' as const]
  return (
    <button
      type="button"
      className={`mic-orb ${!s.muted && !s.paused ? 'is-live' : 'is-muted'} ${s.aiSpeaking ? 'is-ai' : ''}`}
      onClick={onToggleMute}
      disabled={s.paused || s.state === 'CLOSED'}
      aria-pressed={s.muted}
      aria-label={s.muted ? '마이크 켜기' : '마이크 음소거'}
      aria-keyshortcuts="M"
    >
      <Icon name={icon} size={40} />
      <span className="orb-label">{label}</span>
    </button>
  )
}

function CaptionList({ captions }: { captions: Caption[] }) {
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'end', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  }, [captions])
  return (
    <div className="captions" aria-label="대화 자막">
      {captions.length === 0 && <p className="muted center">AI가 먼저 말을 걸어요. 편하게 대답해 보세요.</p>}
      {captions.map((c) =>
        c.kind === 'user' ? (
          <div key={c.turn_id} className={`bubble user ${c.final ? 'final' : 'partial'}`}>
            <span className="who">{c.final ? '나' : '나 · 인식 중'}</span>
            <p lang="en">{c.text || '…'}</p>
          </div>
        ) : (
          <div key={c.response_id} className={`bubble ai ${c.state === 'cancelled' ? 'cancelled' : ''}`}>
            <span className="who">AI{c.state === 'cancelled' ? ' · 중단됨' : ''}</span>
            <p lang="en">
              {c.segments
                .filter((s) => s.status !== 'dropped')
                .map((s) => (
                  <span key={s.segment_id} className={`seg-${s.status}`}>
                    {s.text}
                    {s.status === 'interrupted' && <span className="sr-only"> (여기서 끊김)</span>}{' '}
                  </span>
                ))}
            </p>
          </div>
        ),
      )}
      <div ref={end} />
    </div>
  )
}

function HintPanel({ hint, level }: { hint: HintView; level: number }) {
  return (
    <div className="hint-panel" aria-live="polite">
      <p className="eyebrow">힌트 {level}/3</p>
      {hint.text_ko && <p>{hint.text_ko}</p>}
      {level >= 2 && hint.keywords && hint.keywords.length > 0 && (
        <p className="keywords" lang="en">
          {hint.keywords.map((k) => (
            <span key={k} className="kw">
              {k}
            </span>
          ))}
        </p>
      )}
      {level >= 3 && hint.example_en && (
        <p className="example" lang="en">
          “{hint.example_en}”
        </p>
      )}
    </div>
  )
}

function localHint(sc: Scenario, s: Snapshot, level: 1 | 2 | 3): HintView | null {
  const pending = sc.goals.find((g) => !s.goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')) ?? sc.goals[0]
  const h = sc.hints.find((x) => x.goal_id === pending?.goal_id) ?? sc.hints[0]
  return h ? { level, text_ko: h.ko, keywords: h.keywords, example_en: h.example_en } : null
}

export function Talk({ scenarioId, opts }: { scenarioId: string; opts: string }) {
  const app = useApp()
  const sc = app.scenario(scenarioId)
  const [difficultyRaw, optRaw] = (opts ?? '').split(':')
  const difficulty = (['easy', 'normal', 'hard'].includes(difficultyRaw ?? '') ? difficultyRaw : app.settings.difficulty) as Difficulty
  const historyOptIn = optRaw ? optRaw === '1' : app.settings.history_opt_in
  const [session, setSession] = useState<RealtimeSession | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [ending, setEnding] = useState(false)
  const [hintLevel, setHintLevel] = useState(0)

  useEffect(() => () => session?.shutdown(), [session])

  const begin = async () => {
    if (!sc) return
    setStarting(true)
    setStartError(null)
    let s: RealtimeSession | null = null
    try {
      const { session_id } = await api.createSession({ mode: 'realtime', scenario_id: sc.scenario_id, difficulty, history_opt_in: historyOptIn, feedback_policy: 'session_end' })
      s = new RealtimeSession(session_id, { autoBargeIn: app.settings.auto_barge_in })
      await s.start(app.micDeviceId || undefined, app.settings.silence_ms)
      setSession(s)
    } catch (e) {
      s?.shutdown()
      setStartError(toApiError(e).messageKo)
    } finally {
      setStarting(false)
    }
  }

  const finish = async () => {
    if (!session || !sc) return
    setEnding(true)
    session.shutdown()
    let summary = null
    let error = null
    try {
      summary = await api.endSession(session.sessionId)
    } catch (e) {
      error = toApiError(e).messageKo
    }
    app.setLastSession({ sessionId: session.sessionId, scenarioId: sc.scenario_id, summary, error, historyOptIn, endedAt: Date.now() })
    navigate('summary')
  }

  if (!sc) {
    return (
      <main className="page">
        <Notice tone="warn">시나리오를 찾을 수 없어요.</Notice>
        <a className="btn" href="#/home">홈으로</a>
      </main>
    )
  }

  if (!session) {
    const rt = app.health?.modes.realtime
    return (
      <main className="page narrow" aria-labelledby="talk-title">
        <header className="page-head">
          <p className="eyebrow">실시간 회화 · {DIFFICULTY_LABEL[difficulty]}</p>
          <h1 id="talk-title">{sc.title_ko}</h1>
          <p className="lead">{sc.setting_ko}</p>
        </header>
        <RoleCard sc={sc} goals={[]} />
        <Notice icon="headset">헤드셋을 쓰면 AI 목소리가 마이크로 다시 들어가지 않아 더 자연스럽게 대화할 수 있어요.</Notice>
        {startError && <Notice tone="danger" icon="alert">{startError}</Notice>}
        <div className="page-actions">
          <ActionButton
            className="btn btn-primary btn-lg"
            icon="mic"
            onClick={() => void begin()}
            disabledReason={starting ? '연결하는 중…' : rt && !rt.available ? rt.reason_ko ?? '실시간 회화를 지금 사용할 수 없어요' : !app.health ? '모델 상태를 확인하는 중이에요' : null}
          >
            대화 시작
          </ActionButton>
          <a className="btn btn-ghost" href="#/home">
            돌아가기
          </a>
        </div>
      </main>
    )
  }

  return <LiveTalk sc={sc} session={session} ending={ending} onEnd={() => void finish()} hintLevel={hintLevel} setHintLevel={setHintLevel} />
}

function RoleCard({ sc, goals }: { sc: Scenario; goals: Snapshot['goals'] }) {
  return (
    <section className="card role-card" aria-label="역할과 목표">
      <p className="roles">
        <span>
          AI · <strong>{sc.ai_role_ko}</strong>
        </span>
        <span>
          나 · <strong>{sc.user_role_ko}</strong>
        </span>
      </p>
      <h2 className="small-title">이번 대화 목표</h2>
      <ol className="goals">
        {sc.goals.map((g) => {
          const done = goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')
          return (
            <li key={g.goal_id} className={done ? 'done' : ''}>
              <span className="goal-mark">
                <Icon name={done ? 'check' : 'circle'} size={18} />
              </span>
              <span>
                {g.ko}
                <span className="sr-only">{done ? ' — 달성' : ' — 아직'}</span>
                <span className="muted small block" lang="en">
                  {g.en}
                </span>
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

export function LiveTalk({
  sc,
  session,
  ending,
  onEnd,
  hintLevel,
  setHintLevel,
}: {
  sc: Scenario
  session: RealtimeSession
  ending: boolean
  onEnd(): void
  hintLevel: number
  setHintLevel(n: number): void
}) {
  const s = useSyncExternalStore(session.subscribe, () => session.snapshot)
  const getLevel = useCallback(() => session.micLevel, [session])

  // New AI turn → hint ladder starts again.
  const lastAi = [...s.captions].reverse().find((c) => c.kind === 'ai')?.response_id
  useEffect(() => setHintLevel(0), [lastAi, setHintLevel])

  const nextHint = () => {
    const level = Math.min(3, hintLevel + 1) as 1 | 2 | 3
    setHintLevel(level)
    const local = localHint(sc, s, level)
    if (local) session.showLocalHint(local)
    session.requestHint(level)
  }

  const closed = s.state === 'CLOSED'
  const stopReason = !(s.aiSpeaking || s.responseActive) ? 'AI가 말하고 있을 때 쓸 수 있어요' : null

  // Keyboard shortcuts (ignored while typing or when a control has focus for Space/Enter).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || closed) return
      const k = e.key.toLowerCase()
      if (k === 'escape') session.interrupt()
      else if (k === 'm') session.setMuted(!session.snapshot.muted)
      else if (k === 'h') nextHint()
      else if (k === 'r') session.replayLast()
      else if (k === ' ' && t === document.body) {
        e.preventDefault()
        if (session.snapshot.pushToTalk) {
          if (!e.repeat) session.pushToTalk(true)
        } else session.commit()
      }
    }
    const onUp = (e: KeyboardEvent) => {
      if (e.key === ' ' && session.snapshot.pushToTalk) session.pushToTalk(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onUp)
    }
  })

  return (
    <main className="talk" aria-labelledby="live-title">
      <header className="talk-head">
        <div>
          <p className="eyebrow">실시간 회화</p>
          <h1 id="live-title">{sc.title_ko}</h1>
        </div>
        <StateChip s={s} />
        <button type="button" className="btn btn-danger-soft" onClick={onEnd} disabled={ending}>
          <Icon name="close" /> {ending ? '요약 만드는 중…' : '종료하고 요약 보기'}
        </button>
      </header>

      <div className="talk-body">
        <aside className="talk-side">
          <RoleCard sc={sc} goals={s.goals} />
          {s.hint && hintLevel > 0 && <HintPanel hint={s.hint} level={hintLevel} />}
        </aside>

        <section className="talk-main">
          {s.error && (
            <Notice tone={s.error.recoverable ? 'warn' : 'danger'} icon="alert">
              {s.error.message}{' '}
              {s.error.code === 'LLM_FAILED' && (
                <button type="button" className="link" onClick={() => session.retryResponse()}>
                  AI 답변 다시 받기
                </button>
              )}{' '}
              {s.error.recoverable && (
                <button type="button" className="link" onClick={() => session.clearError()}>
                  닫기
                </button>
              )}
            </Notice>
          )}
          {s.warning && <Notice tone="warn">{s.warning}</Notice>}
          {s.echoCount > 0 && !s.pushToTalk && (
            <Notice tone="warn" icon="headset">
              {s.echoCount >= 2 ? 'AI 목소리가 계속 마이크로 들어와서 자동 끼어들기를 껐어요. ' : 'AI 목소리가 마이크로 다시 들어간 것 같아요. 헤드셋을 권장해요. '}
              <button type="button" className="link" onClick={() => session.setPushToTalkMode(true)}>
                눌러 말하기로 전환
              </button>
            </Notice>
          )}
          <CaptionList captions={s.captions} />

          <div className="control-dock">
            <div className="dock-mic">
              <MicStatus s={s} onToggleMute={() => session.setMuted(!s.muted)} onPtt={(p) => session.pushToTalk(p)} />
              <LevelMeter getLevel={getLevel} label="내 목소리 크기" />
            </div>
            <div className="dock-actions">
              {!s.pushToTalk && (
                <ActionButton className="btn btn-primary" icon="send" onClick={() => session.commit()} disabledReason={closed ? '종료됨' : s.canCommit ? null : '말을 시작하면 누를 수 있어요'} aria-keyshortcuts="Space">
                  말하기 완료
                </ActionButton>
              )}
              <ActionButton className="btn" icon="stop" onClick={() => session.interrupt()} disabledReason={stopReason} aria-keyshortcuts="Escape">
                멈추기(끼어들기)
              </ActionButton>
              <ActionButton className="btn" icon="hint" onClick={nextHint} disabledReason={closed ? '종료됨' : hintLevel >= 3 ? '힌트를 모두 봤어요' : null} aria-keyshortcuts="H">
                힌트 {Math.min(3, hintLevel + 1)}/3 · {HINT_STEP[Math.min(2, hintLevel)]}
              </ActionButton>
              <ActionButton className="btn" icon="replay" onClick={() => session.replayLast()} disabledReason={!session.canReplay ? '다시 들을 AI 말이 아직 없어요' : s.aiSpeaking ? 'AI가 말하는 중이에요' : null} aria-keyshortcuts="R">
                다시 듣기
              </ActionButton>
              <ActionButton className="btn" icon="slow" onClick={() => session.setSlow(!s.slow)} aria-pressed={s.slow} disabledReason={closed ? '종료됨' : null}>
                {s.slow ? '더 천천히 켜짐' : '더 천천히'}
              </ActionButton>
              <ActionButton className="btn" icon={s.paused ? 'play' : 'pause'} onClick={() => session.setPaused(!s.paused)} aria-pressed={s.paused} disabledReason={closed ? '종료됨' : null}>
                {s.paused ? '다시 시작' : '일시정지'}
              </ActionButton>
            </div>
            <p className="shortcuts muted small">
              단축키: {s.pushToTalk ? 'Space 누르고 말하기' : 'Space 말하기 완료'} · Esc 멈추기 · M 음소거 · H 힌트 · R 다시 듣기
              {s.slow && ' · 더 천천히는 다음 문장부터 적용돼요'}
            </p>
            {s.pushToTalk && (
              <button type="button" className="link small" onClick={() => session.setPushToTalkMode(false)}>
                자동 대화로 돌아가기
              </button>
            )}
          </div>
        </section>
      </div>
    </main>
  )
}
